#!/usr/bin/env python3
"""FastAPI backend server for tech-db Q&A system.

Hybrid retrieval: BM25 (keyword) + vector (semantic) fused via RRF.
GLM-5.2 streaming for answer generation.

Endpoints:
  POST /api/chat/stream  - Streaming chat (SSE)
  GET  /api/graph       - Get knowledge graph data for visualization
  GET  /api/stats       - System statistics
  GET  /api/health      - Health check
  GET  /api/search       - Direct search (debug)

Run:
  .venv/bin/python qa-backend/server.py
"""
import os
from contextvars import ContextVar
import sys
import json
import pickle
import asyncio
import re
import time as _time
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

# Add paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import (
    WORKING_DIR, RUNTIME_DIR, ENV_FILE, llm_model_func, embedding_func,
    llm_stream_func, MODEL_NAME, llm_abandoned_stats,
)
from guardrails import (
    BudgetFuse,
    GuardrailSettings,
    RateLimiter,
    admin_bypass,
    client_identifier,
)
from epistemic import (
    classify_claims,
    verify_answer,
    build_epistemic_system_prompt,
    extract_relevant_excerpt,
    build_source_metadata,
)
from trace import TraceContext
from feature_flags import Flags
from citation_grounding import ground_citation_evidence, get_original_text
from verifier import verify_with_fail_safe, VerificationResult, VERIFY_PASSED, VERIFY_FAILED, VERIFY_UNVERIFIED
from claim_mapping import map_claims_to_citations, get_unsupported_major_claims
from budget_guard import BudgetExceededError, QueryBudget
from ttfb_guard import guard_budget_s, snapshot as ttfb_snapshot
from degraded_mode import build_user_warning, looks_like_api_failure
from answer_status import AnswerStatus, determine_answer_status, build_evidence_summary
from content_safety import scan_search_results, augment_system_prompt
from budget_guard import check_budget, BudgetDecision, is_correctness_critical

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
INDEX_FILE = WORKING_DIR / "vector_index_v2.pkl"
BM25_FILE = WORKING_DIR / "bm25_index.pkl"
JIEBA_DICT = WORKING_DIR / "jieba_custom_dict.txt"

# ── Global state ──
_vector_index = None  # numpy array (N, 1024)
_index_meta = None    # list of metadata dicts
_bm25_index = None    # BM25Okapi instance
_bm25_meta = None     # BM25 metadata list
_bm25_corpus = None   # tokenized corpus for BM25
_records = None       # full records from all-records-lite.json
_graph_data = None    # graph-export.json data (nodes, edges, entity_to_records)
_entity_index = None  # entity_name -> set of record indices
_graph_adj = None     # entity_name -> set of neighbor entity_names (adjacency from edges)
_graph_nodes = None   # entity_name -> node info dict (type, degree, description)
_idx_to_meta = None   # record_idx -> meta dict (fast lookup, avoids linear scan)
_request_runtime_snapshot = ContextVar("techdb_runtime_snapshot", default=None)


def _runtime_resource(name, legacy_value):
    snapshot = _request_runtime_snapshot.get()
    if snapshot is None:
        return legacy_value
    if name not in snapshot.resources:
        raise RuntimeError(f"pinned runtime {snapshot.manifest_id} missing resource {name}")
    return snapshot.resources[name]

# ── RRF parameters ──
RRF_K = 60          # RRF constant (1/(rank+k))
RETRIEVAL_TOP_K = 50  # candidates per route
FINAL_TOP_K = 25     # max records after fusion
RELEVANCE_FLOOR = 0.3  # vector similarity floor for "honest answer" trigger

# ── Quality gates ──
# Strict thresholds: a route must clear these to count as a "strong signal"
VEC_STRONG = 0.55    # PROVISIONAL: based on n=2 sample calibration (good ~0.65-0.71, bad ~0.50-0.54).
                      # Not a permanent value — needs validation with broader query types
                      # (specific entities, English, typos, ultra-broad queries) before fixing.
BM25_STRONG = 5.0    # BM25 score for confident match (noise queries still get ~2-4)
GRAPH_STRONG = 5.0   # Graph hit count for confident match (noise queries get ~4)
# Topic exhaustion: when excluding records (novelty follow-up), if NO remaining
# result clears ANY strong threshold, the topic is likely exhausted
MIN_STRONG_RESULTS = 3  # need at least this many strong results to avoid "exhausted"

# ── Graph hop=1 expansion parameters ──
MAX_HOP1_DEGREE = 20    # Skip hop=1 neighbors with degree > this (super-node filter)
HOP1_WEIGHT = 0.35      # Score weight for hop=1 entities (hop=0 = 1.0)
MAX_HOP1_ENTITIES = 40  # Cap total hop=1 expansion entities to prevent explosion

# ── Novelty exclusion ──
FETCH_K_CAP = 200       # Hard cap on fetch_k to bound retrieval cost for long conversations.

# ── Public-service guardrails ──
GUARDRAILS = GuardrailSettings()
RATE_LIMITER = RateLimiter(GUARDRAILS)
BUDGET_FUSE = BudgetFuse(GUARDRAILS, RUNTIME_DIR / "state" / "usage.json")
CHAT_SEMAPHORE = asyncio.Semaphore(GUARDRAILS.concurrency)


def load_vector_index():
    """Load the pre-built vector index."""
    global _vector_index, _index_meta
    if _vector_index is not None:
        return
    # Try v2 first, fall back to v1
    idx_file = INDEX_FILE if INDEX_FILE.exists() else WORKING_DIR / "vector_index.pkl"
    if idx_file.exists():
        print(f"[startup] Loading vector index from {idx_file.name}...", flush=True)
        with open(idx_file, "rb") as f:
            data = pickle.load(f)
        _vector_index = data["embeddings"]
        _index_meta = data["meta"]
        print(f"[startup] Vector index loaded: {len(_index_meta)} records, dim={data['dim']}", flush=True)
    else:
        print(f"[startup] WARNING: No vector index found", flush=True)


def load_bm25_index():
    """Load the pre-built BM25 index (and the query-side custom dict)."""
    global _bm25_index, _bm25_meta, _bm25_corpus
    if _bm25_index is not None:
        return
    if BM25_FILE.exists():
        print(f"[startup] Loading BM25 index...", flush=True)
        with open(BM25_FILE, "rb") as f:
            data = pickle.load(f)
        _bm25_index = data["bm25"]
        _bm25_meta = data["meta"]
        _bm25_corpus = data.get("corpus_tokens")
        # Codex-review C2 P1 fix: the custom jieba dict used at INDEX BUILD
        # time must also be loaded for QUERY tokenization wherever the BM25
        # index is loaded — parity.py calls this directly (bypassing the
        # FastAPI lifespan that used to be the only loader), so baselines
        # previously tokenized queries differently from the live path.
        if JIEBA_DICT.exists():
            import jieba
            jieba.load_userdict(str(JIEBA_DICT))
            print("[startup] Jieba custom dict loaded", flush=True)
        print(f"[startup] BM25 index loaded: {len(_bm25_meta)} documents", flush=True)
    else:
        print(f"[startup] BM25 index not found (hybrid search disabled)", flush=True)


def load_graph_index():
    """Load knowledge graph: entity→record mapping + adjacency list + node lookup."""
    global _graph_data, _entity_index, _graph_adj, _graph_nodes
    if _graph_data is not None:
        return
    graph_file = WORKING_DIR / "graph-export.json"
    if graph_file.exists():
        print(f"[startup] Loading knowledge graph...", flush=True)
        _graph_data = json.loads(graph_file.read_text("utf-8"))
        e2r = _graph_data.get("entity_to_records", {})
        _entity_index = {k: set(v) for k, v in e2r.items()}

        # Build adjacency list from edges (for hop=1 neighbor expansion)
        _graph_adj = {}
        for e in _graph_data.get("edges", []):
            s, t = e.get("source"), e.get("target")
            if s and t:
                _graph_adj.setdefault(s, set()).add(t)
                _graph_adj.setdefault(t, set()).add(s)

        # Build node lookup (for degree/type filtering)
        _graph_nodes = {n["id"]: n for n in _graph_data.get("nodes", [])}

        print(f"[startup] Graph loaded: {len(_graph_data.get('nodes',[]))} nodes, "
              f"{len(_graph_data.get('edges',[]))} edges, "
              f"{len(_entity_index)} entity→record mappings, "
              f"{len(_graph_adj)} adjacency entries", flush=True)
    else:
        print(f"[startup] Knowledge graph not found (graph search disabled)", flush=True)


def load_records():
    """Load all-records-lite.json for full record lookup."""
    global _records
    if _records is None:
        _records = json.loads(LITE_PATH.read_text("utf-8"))
    return _records


async def vector_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Search the vector index for the most similar records."""
    if _vector_index is None:
        return []

    # Embed the query
    query_emb = await embedding_func([query])
    query_vec = np.array(query_emb[0], dtype=np.float32)
    query_vec = query_vec / max(np.linalg.norm(query_vec), 1e-8)

    # Cosine similarity (embeddings are pre-normalized)
    scores = _vector_index @ query_vec

    # Get top_k indices
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        meta = _index_meta[idx]
        score = float(scores[idx])
        results.append({
            "meta": meta,
            "score": score,
        })
    return results


def bm25_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Search the BM25 index for keyword matches."""
    if _bm25_index is None:
        return []

    import jieba
    tokens = list(jieba.cut_for_search(query))
    if not tokens:
        return []

    scores = _bm25_index.get_scores(tokens)
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        score = float(scores[idx])
        if score <= 0:
            continue
        meta = _bm25_meta[idx]
        results.append({
            "meta": meta,
            "score": score,
        })
    return results


def rrf_fuse(vec_results: list, bm25_results: list, graph_results: list = None,
             k: int = RRF_K, top_k: int = FINAL_TOP_K) -> list:
    """Reciprocal Rank Fusion of multiple result lists.

    Returns unified list of {meta, score} with RRF scores.
    """
    rrf_scores = {}  # record_idx -> {rrf, meta, vec_score, bm25_score, graph_score}
    all_routes = [
        ("vec", vec_results, "vec_score"),
        ("bm25", bm25_results, "bm25_score"),
    ]
    if graph_results:
        all_routes.append(("graph", graph_results, "graph_score"))

    for route_name, results, score_key in all_routes:
        for rank, result in enumerate(results):
            rec_idx = result["meta"]["idx"]
            if rec_idx not in rrf_scores:
                rrf_scores[rec_idx] = {"rrf": 0.0, "meta": result["meta"],
                                       "vec_score": 0.0, "bm25_score": 0.0,
                                       "graph_score": 0.0}
            rrf_scores[rec_idx]["rrf"] += 1.0 / (rank + k)
            rrf_scores[rec_idx][score_key] = result["score"]

    # Sort by RRF score
    fused = sorted(rrf_scores.values(), key=lambda x: -x["rrf"])[:top_k]

    return [{
        "meta": item["meta"],
        "score": item["rrf"],
        "vec_score": item["vec_score"],
        "bm25_score": item["bm25_score"],
        "graph_score": item.get("graph_score", 0),
    } for item in fused]


def graph_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Graph-based retrieval with hop=1 neighbor expansion.

    Hop=0: Match entities named in the query → get their associated records.
    Hop=1: Expand to neighbors of matched entities → discover related records
           the user didn't explicitly mention.

    Super-node filter: neighbors with degree > MAX_HOP1_DEGREE are skipped
    (e.g., "能量密度" degree=232 appears everywhere, not useful for expansion).

    Co-occurrence boost: if multiple matched entities share a common neighbor,
    that neighbor accumulates weight (naturally surfaces intersection topics).
    """
    if _entity_index is None:
        return []

    import jieba.posseg as pseg

    # ── Hop=0: Match entities directly named in the query ──
    words = pseg.cut(query)
    query_terms = []
    for word, flag in words:
        word = word.strip()
        if len(word) >= 2 and flag in ('n', 'nr', 'ns', 'nt', 'nz', 'vn', 'eng'):
            query_terms.append(word)

    matched_entities = set()
    for entity_name in _entity_index:
        if entity_name in query:
            matched_entities.add(entity_name)
    for term in query_terms:
        for entity_name in _entity_index:
            if term in entity_name or entity_name in term:
                matched_entities.add(entity_name)

    if not matched_entities:
        return []

    # ── Hop=1: Expand to neighbors of matched entities ──
    hop1_entities = {}  # entity_name -> accumulated weight
    if _graph_adj is not None:
        for entity in matched_entities:
            neighbors = _graph_adj.get(entity)
            if not neighbors:
                continue
            for nbr in neighbors:
                if nbr in matched_entities:
                    continue
                # Primary defense: super-node filter (high-degree entities)
                nbr_info = _graph_nodes.get(nbr) if _graph_nodes else None
                if nbr_info and nbr_info.get("degree", 0) > MAX_HOP1_DEGREE:
                    continue
                # Secondary heuristic: skip very short entity names (len<3).
                # NOTE: Effect is limited — the real noise comes from high record-count
                # entities with len>=3 (e.g. "人工智能（AI）" has 529 records at len=8).
                # The degree filter above is the main defense.
                if len(nbr) < 3:
                    continue
                hop1_entities[nbr] = hop1_entities.get(nbr, 0.0) + HOP1_WEIGHT

        # Cap hop=1 expansion to prevent signal dilution
        if len(hop1_entities) > MAX_HOP1_ENTITIES:
            hop1_entities = dict(
                sorted(hop1_entities.items(), key=lambda x: -x[1])[:MAX_HOP1_ENTITIES]
            )

    # ── Score records: hop=0 weight=1.0, hop=1 weight≈0.35 ──
    record_scores = {}  # record_idx -> score
    for entity in matched_entities:
        for rec_idx in _entity_index.get(entity, set()):
            record_scores[rec_idx] = record_scores.get(rec_idx, 0.0) + 1.0
    for entity, weight in hop1_entities.items():
        for rec_idx in _entity_index.get(entity, set()):
            record_scores[rec_idx] = record_scores.get(rec_idx, 0.0) + weight

    sorted_records = sorted(record_scores.items(), key=lambda x: -x[1])[:top_k]

    # Build results using fast index lookup
    results = []
    for rec_idx, score in sorted_records:
        meta = _idx_to_meta.get(rec_idx) if _idx_to_meta else None
        if meta is None:
            for m in _index_meta:
                if m.get("idx") == rec_idx:
                    meta = m
                    break
            if not meta:
                for m in _bm25_meta:
                    if m.get("idx") == rec_idx:
                        meta = m
                        break
        if meta:
            results.append({"meta": meta, "score": score})

    print(f"[graph] hop0={len(matched_entities)} entities, "
          f"hop1={len(hop1_entities)} neighbors, "
          f"{len(record_scores)} candidate records", flush=True)

    return results


def _keyword_fallback(query: str, history: list) -> str:
    """Extract most recent user question from history and combine with current query."""
    last_user_msg = ""
    for msg in reversed(history):
        if msg.get("role") == "user" and msg.get("content", "").strip():
            last_user_msg = msg["content"].strip()
            break
    if last_user_msg:
        context_topic = last_user_msg[:30]
        return f"{context_topic} {query}"
    return query


def _parse_rewrite_json(text: str, fallback_query: str) -> tuple:
    """Four-level JSON tolerance parser for GLM-5.2 output.

    Returns (rewritten_query, seeking_novelty, reason).
    """
    def _extract(obj):
        q = obj.get("rewritten_query") or obj.get("query") or ""
        q = q.strip().strip('"').strip("'").strip("。") if isinstance(q, str) else ""
        if not q:
            q = fallback_query
        n = bool(obj.get("seeking_novelty", obj.get("novelty", False)))
        r = obj.get("reason", "")
        r = r.strip() if isinstance(r, str) else ""
        return (q, n, r)

    # Level 1: direct json.loads
    try:
        return _extract(json.loads(text))
    except Exception:
        pass

    # Level 2: strip markdown code fences then json.loads
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        return _extract(json.loads(stripped))
    except Exception:
        pass

    # Level 3: regex extract first {...} object then json.loads
    m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if m:
        try:
            return _extract(json.loads(m.group(0)))
        except Exception:
            pass

    # Level 4: all failed → fallback
    return (fallback_query, False, "json parse failed")


async def rewrite_query(query: str, history: list) -> tuple:
    """Rewrite follow-up query + detect novelty intent in a single LLM call.

    Returns (rewritten_query, seeking_novelty, reason).
    - rewritten_query: standalone retrieval query with context filled in
    - seeking_novelty: user wants substantively different/new content
    - reason: short justification for logging only
    """
    # Fast path: no history → no rewrite, no novelty
    if not history:
        return (query, False, "")

    # Build compact dialogue context
    recent = history[-4:]  # last 2 turns (user + assistant)
    dialogue = ""
    for msg in recent:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = msg.get("content", "")[:200]
        dialogue += f"{role}: {content}\n"

    prompt = f"""根据以下对话历史，完成两个任务：

1. 将用户最新问题改写为独立、完整的检索查询（补全代词和省略的上下文）
2. 判断用户是否在寻求与已有回答实质不同的新内容/新方向/未涉及的角度

seeking_novelty 判断依据（语义意图，非关键词匹配）：
- true：用户希望看到之前回答中未涉及的新内容、新方向、新角度
- false：用户在深挖当前话题的细节、追问澄清、或继续同一子话题
- 示例（非穷举）："还有别的吗"→true，"刚才提到的XX具体是多少"→false

输出格式（严格JSON，不要其他内容）：
{{"rewritten_query": "...", "seeking_novelty": true/false, "reason": "..."}}

对话历史：
{dialogue}

用户最新问题：{query}"""

    try:
        raw = await llm_model_func(
            prompt,
            temperature=0.0,
            max_tokens=1024,  # GLM-5.2 reasoning model: needs 150-300 reasoning_tokens + content
            allow_reasoning_fallback=True,  # JSON caller (_parse_rewrite_json) downstream
        )
        if raw and raw.strip():
            rewritten, seeking_novelty, reason = _parse_rewrite_json(raw, query)
            print(f"[query-rewrite] '{query}' → '{rewritten}' "
                  f"| novelty={seeking_novelty} | reason={reason}", flush=True)
            return (rewritten, seeking_novelty, reason)
    except Exception as e:
        print(f"[query-rewrite] LLM failed ({e}), using keyword fallback", flush=True)

    # Fallback: keyword concatenation, no novelty detection
    fallback = _keyword_fallback(query, history)
    print(f"[query-rewrite] Fallback: '{query}' → '{fallback}' | novelty=False",
          flush=True)
    return (fallback, False, "llm fallback")


def _parse_citations_from_answer(full_answer: str, citations: list) -> list:
    """Extract [N] markers from LLM answer and map to record_ids via citations array.

    Returns deduplicated list of record_id ints, preserving first-seen order.
    Hallucinated [N] (out of range) are silently dropped.
    """
    if not full_answer or not citations:
        return []
    nums = re.findall(r'\[(\d+)\]', full_answer)
    result = []
    for n_str in nums:
        n = int(n_str)
        c = next((c for c in citations if c["id"] == n), None)
        if c and c.get("record_id") not in (None, -1, ""):
            result.append(c["record_id"])
    return list(dict.fromkeys(result))  # dedupe, preserve order


def _no_evidence_boundary(query: str, exhausted: bool) -> str:
    """Codex-review C3 P2 fix: boundary message for early unsupported exits.

    The weak-query / prior-weak-query / topic-exhausted branches return
    answer_status=UNSUPPORTED before the main knowledge-boundary block
    (which needs claim mapping + grounding) runs — with the TK-06 flag ON
    they must still carry a non-LLM boundary message instead of none.
    """
    try:
        from feature_flags import Flags
        from knowledge_boundary import (
            assess_coverage, determine_answer_boundary, format_boundary_message,
            AnswerStatus as KBStatus,
        )
        if not Flags.KNOWLEDGE_BOUNDARY_ENABLED:
            return ""
        coverage = assess_coverage(
            requirements=[{"status": "MISSING", "text": query}],
            evidence_count=0,
            independent_groups=0,
        )
        _status, _msg = determine_answer_boundary(
            coverage_level=coverage,
            critical_missing=[{"description": query[:40]}] if exhausted else [],
            conflicts=[],
            grader_overall="INSUFFICIENT",
        )
        return format_boundary_message(
            answer_status=KBStatus.UNSUPPORTED,
            supported_aspects=[],
            unsupported_aspects=[query],
            coverage_level=coverage,
        )
    except Exception as e:
        print(f"[knowledge_boundary] early-exit error: {e}", flush=True)
        return ""


async def _search_with_quality_legacy(query: str, exclude_ids: set = None) -> tuple:
    """REMOVED in TK-23 (R5 contract phase).

    The pre-TK-05 inline retrieval implementation and its QA_RETRIEVAL_LEGACY
    escape hatch were deleted after gate 3 passed (test_fixtures/gate3_report.json).
    Rollback semantics post-contract (Q2): the escape hatch is GONE — roll back
    by reverting the TK-23 commit, not by an env var. Ongoing regression
    watching: tests_parity (frozen gate-1 baselines) + QA_SHADOW_RETRIEVAL
    (drift vs the frozen shadow_diff_full.json reference ids).
    """
    raise NotImplementedError(
        "legacy retrieval path removed (TK-23 contract phase); "
        "rollback = git revert, not QA_RETRIEVAL_LEGACY")


# ── TK-05 (Q8/R5): unified retrieval layer wiring — contract phase ───────────
# hybrid_search now orchestrates the retrieval/ layer (T013/T014) through a
# single seam. Output shape and RRF semantics are byte-compatible with the
# pre-contract legacy path (parity-guarded by tests_parity.py against the
# frozen gate-1 baselines). TK-23: the legacy implementation and the
# QA_RETRIEVAL_LEGACY escape hatch are deleted; rollback is git-revert.
_SHADOW_RETRIEVAL = os.environ.get("QA_SHADOW_RETRIEVAL", "").strip() == "1"
_retrieval_pipeline = None
_shadow_diffs = []          # per-query drift records (bounded)


def _bm25_tokenize(query: str) -> list:
    """Tokenize a query the same way the BM25 corpus was tokenized."""
    import jieba
    return list(jieba.cut_for_search(query))


def _get_retrieval_pipeline():
    """Build the unified retrieval pipeline over the loaded indexes (lazy)."""
    global _retrieval_pipeline
    snapshot = _request_runtime_snapshot.get()
    if snapshot is not None:
        resources = snapshot.resources
        if "retrieval_pipeline" in resources:
            return resources["retrieval_pipeline"]
        from retrieval import VectorRetriever, BM25Retriever, GraphRetriever, RRFFusion
        required = ("vector_index", "index_meta", "bm25_index", "bm25_meta")
        missing = [name for name in required if name not in resources]
        if missing:
            raise RuntimeError(f"pinned runtime {snapshot.manifest_id} incomplete: {missing}")
        graph_fn = resources.get("graph_search", lambda q, k: [])
        pipeline = (
            VectorRetriever(embeddings=resources["vector_index"], meta=resources["index_meta"]),
            BM25Retriever(bm25_index=resources["bm25_index"], meta=resources["bm25_meta"], tokenize_fn=_bm25_tokenize),
            GraphRetriever(graph_search_fn=lambda q, k: [(r["meta"]["idx"], r["score"]) for r in graph_fn(q, k)]),
            RRFFusion(k=RRF_K, default_top_k=FINAL_TOP_K),
        )
        resources["retrieval_pipeline"] = pipeline
        resources["idx_to_meta"] = {m["idx"]: m for m in resources["index_meta"]}
        return pipeline
    if _retrieval_pipeline is None:
        from retrieval import VectorRetriever, BM25Retriever, GraphRetriever, RRFFusion
        load_vector_index()
        load_bm25_index()

        vr = VectorRetriever(embeddings=_vector_index, meta=_index_meta)
        br = BM25Retriever(bm25_index=_bm25_index, meta=_bm25_meta,
                           tokenize_fn=_bm25_tokenize)
        gr = GraphRetriever(graph_search_fn=lambda q, k: [
            (r["meta"]["idx"], r["score"]) for r in graph_search(q, k)
        ])
        fuse = RRFFusion(k=RRF_K, default_top_k=FINAL_TOP_K)
        _retrieval_pipeline = (vr, br, gr, fuse)
    return _retrieval_pipeline


async def _search_with_quality(query: str, exclude_ids: set = None) -> tuple:
    """Retrieval seam dispatcher (TK-05 + TK-17 + TK-23).

    Order: shadow drift-watch (records overlap vs the frozen gate-3 reference)
    → new retrieval-layer path. Contract: (results, is_relevant).
    """
    if _SHADOW_RETRIEVAL:
        # QA_SHADOW_RETRIEVAL=1 → run the live path, record drift vs the
        # frozen reference ids (shadow_diff_full.json), return live result.
        return await _search_with_shadow(query, exclude_ids)
    return await _search_with_quality_new(query, exclude_ids)


async def _search_with_quality_new(query: str, exclude_ids: set = None) -> tuple:
    """Unified retrieval layer path (TK-05). Same contract as legacy:
    (results, is_relevant) with legacy result dict shape.

    Parity invariants vs legacy (locked by tests_parity.py):
      - RRF score = 1/(position0 + k), position over each route's own ranking
      - route scores carried through as vec/bm25/graph_score
      - BM25 drops score<=0 candidates
      - meta taken from the record index (identical across routes)
    """

    fetch_k = min(RETRIEVAL_TOP_K + (len(exclude_ids) if exclude_ids else 0), FETCH_K_CAP)
    vr, br, gr, fuse = _get_retrieval_pipeline()

    async def _vector_route():
        query_emb = await embedding_func([query])
        qv = np.array(query_emb[0], dtype=np.float32)
        qv = qv / max(np.linalg.norm(qv), 1e-8)
        return vr.search(qv, top_k=fetch_k)

    vec_res, bm25_res, graph_res = await asyncio.gather(
        _vector_route(),
        asyncio.to_thread(br.search, query, fetch_k),
        asyncio.to_thread(gr.search, query, fetch_k),
    )

    # Parity invariant: legacy rrf_fuse scores 1/(position0 + k); the
    # retrieval layer's RetrievalResult.rank is 1-based. Reset ranks to the
    # 0-based list position so fused scores are bit-identical to legacy.
    for route_results in (vec_res, bm25_res, graph_res):
        for pos, rr_ in enumerate(route_results):
            rr_.rank = pos

    fuse_top_k = fetch_k if exclude_ids else FINAL_TOP_K
    fused = fuse.fuse({"vector": vec_res, "bm25": bm25_res,
                       "graph": graph_res}, top_k=fuse_top_k)

    # Rebuild meta lookup lazily (graph-only records need full meta)
    global _idx_to_meta
    snapshot = _request_runtime_snapshot.get()
    if snapshot is not None:
        meta_lookup = snapshot.resources["idx_to_meta"]
    else:
        if _idx_to_meta is None:
            _idx_to_meta = {m["idx"]: m for m in _index_meta}
        meta_lookup = _idx_to_meta

    results = []
    for r in fused:
        meta = meta_lookup.get(r.record_id, r.meta or {})
        det = r.route_details or {}
        results.append({
            "meta": meta,
            "score": r.raw_score,
            # RRFFusion route_details keys are {route}_score (retrieval/fusion.py):
            # vector_score / bm25_score / graph_score. These feed the relevance
            # quality gate (VEC_STRONG / GRAPH_STRONG) — must never default to 0.
            "vec_score": det.get("vector_score", det.get("vector", 0.0)),
            "bm25_score": det.get("bm25_score", det.get("bm25", 0.0)),
            "graph_score": det.get("graph_score", det.get("graph", 0.0)),
        })

    if exclude_ids:
        before = len(results)
        results = [r for r in results if r["meta"].get("idx") not in exclude_ids]
        excluded_count = before - len(results)
        results = results[:FINAL_TOP_K]
        if excluded_count:
            print(f"[search] Excluded {excluded_count} previously cited, "
                  f"{len(results)} remaining", flush=True)
    else:
        results = results[:FINAL_TOP_K]

    # Quality gate: require semantic signal (vec or graph), not BM25 alone
    is_relevant = False
    if results:
        has_strong_vec = any(r.get("vec_score", 0) >= VEC_STRONG for r in results)
        has_strong_graph = any(r.get("graph_score", 0) >= GRAPH_STRONG for r in results)
        is_relevant = has_strong_vec or has_strong_graph

    return results, is_relevant
# ── end TK-05 wiring ─────────────────────────────────────────────────────────


async def _search_with_shadow(query: str, exclude_ids: set = None) -> tuple:
    """TK-17 → TK-23 contract phase: retrieval drift-watch.

    The legacy implementation was deleted (TK-23). Shadow mode now compares
    the LIVE retrieval-layer path against the frozen gate-3 reference ids
    (test_fixtures/holdout/shadow_diff_full.json — the last artifact recorded
    against the legacy path). For queries not in the reference, the record
    carries latency/relevance only (overlap=None). The returned value is the
    live result — shadowing never changes user-visible output.
    """
    import time as _time
    ref_ids = _shadow_reference_ids().get(query[:120])
    t1 = _time.perf_counter()
    try:
        new_res, new_rel = await _search_with_quality_new(query, exclude_ids)
        new_err = None
    except Exception as e:  # path failure must NOT affect the response
        new_res, new_rel, new_err = [], False, str(e)[:200]
    new_ms = (_time.perf_counter() - t1) * 1000.0

    new_ids = [r.get("meta", {}).get("idx") for r in (new_res or [])[:25]]
    if ref_ids is not None:
        inter = set(ref_ids) & set(new_ids)
        union = set(ref_ids) | set(new_ids)
        overlap = round(len(inter) / len(union), 4) if union else 1.0
    else:
        overlap = None
    _shadow_diffs.append({
        "query": query[:120],
        "reference_top25": ref_ids,
        "new_top25": new_ids,
        "id_overlap": overlap,
        "reference_source": "frozen:shadow_diff_full.json" if ref_ids is not None
                            else "none (query not in reference set)",
        "new_relevant": new_rel,
        "new_error": new_err,
        "reference_ms": None,
        "new_ms": round(new_ms, 1),
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    del _shadow_diffs[:-2000]  # bounded memory
    return new_res, new_rel


_shadow_ref_cache = None


def _shadow_reference_ids() -> dict:
    """Query → top-25 ids from the frozen gate-3 shadow reference artifact."""
    global _shadow_ref_cache
    if _shadow_ref_cache is None:
        ref_path = Path(__file__).resolve().parent / "test_fixtures" / "holdout" / \
            "shadow_diff_full.json"
        cache = {}
        try:
            doc = json.loads(ref_path.read_text("utf-8"))
            for q in doc.get("per_query", []):
                cache[q.get("query", "")[:120]] = q.get("legacy_top25") or []
        except Exception as e:
            print(f"[shadow] reference artifact unreadable: {e}", flush=True)
        _shadow_ref_cache = cache
    return _shadow_ref_cache


def shadow_diff_report() -> dict:
    """Aggregate the recorded shadow diffs (consumed by /api/shadow/report)."""
    import statistics as _st
    import time as _time
    if not _shadow_diffs:
        return {"n": 0, "note": "no shadow queries recorded"}
    ov = [d["id_overlap"] for d in _shadow_diffs if d["id_overlap"] is not None]
    n_ref = len(ov)
    out = {
        "n": len(_shadow_diffs),
        "n_vs_reference": n_ref,
        "reference": "frozen:shadow_diff_full.json (gate-3 artifact; legacy path "
                     "removed in TK-23)",
        "new_path_errors": sum(1 for d in _shadow_diffs if d["new_error"]),
        "generated_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if n_ref:
        out["id_overlap"] = {
            "mean": round(_st.mean(ov), 4),
            "min": round(min(ov), 4),
            "below_0.8": sum(1 for o in ov if o < 0.8),
        }
    out["ttfb_ms"] = {
        "new_p50": _pctl([d["new_ms"] for d in _shadow_diffs], 50),
        "new_p90": _pctl([d["new_ms"] for d in _shadow_diffs], 90),
    }
    return out


def _pctl(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return round(s[k], 1)


async def hybrid_search(query: str, exclude_ids: set = None) -> tuple:
    """Hybrid retrieval with dual relevance check for topic exhaustion.

    Returns (results, is_relevant, status) where status ∈ {"ok", "weak_query", "exhausted"}.
    """
    results, is_relevant = await _search_with_quality(query, exclude_ids)

    status = "ok"
    if not is_relevant:
        if exclude_ids:
            # Dual check: search without exclusion to distinguish causes
            _, relevant_without_exclude = await _search_with_quality(query, None)
            if relevant_without_exclude:
                status = "exhausted"
                print(f"[search] Topic exhausted: excluded results weak, "
                      f"but unexcluded results strong", flush=True)
            else:
                status = "weak_query"
                print(f"[search] Weak query: results poor with or without exclusion",
                      flush=True)
        else:
            status = "weak_query"

    return results, is_relevant, status


def build_context(search_results: list, query: str = "") -> tuple:
    """Build context string and citations from search results."""
    records = (_runtime_resource("records", None)
               if _request_runtime_snapshot.get() is not None else load_records())
    citations = []
    context_parts = []

    for i, result in enumerate(search_results):
        meta = result["meta"]
        score = result["score"]
        orig_idx = meta.get("idx", -1)
        record = records[orig_idx] if 0 <= orig_idx < len(records) else None
        if not record:
            continue

        title = record.get("t", "") or ""
        date = record.get("d", "") or ""
        source = record.get("a", record.get("s", "")) or ""
        from primary_evidence import source_evidence_text
        body = source_evidence_text(record)
        if not body or record.get("evidence_eligibility") in {"RETRIEVAL_ONLY", "QUARANTINED"}:
            continue
        cat = record.get("c", "") or ""
        tags = record.get("tg", [])
        if isinstance(tags, str):
            tags = [tags]
        url = record.get("u", "") or ""
        sc = record.get("sc", 0)

        # Add to context
        citation_number = len(citations) + 1
        context_parts.append(
            f"[{citation_number}] [{cat}] {title} ({date})\n"
            f"来源: {source}\n"
            f"证据摘录: {body[:300]}\n"
            f"相似度: {score:.2f}"
        )

        # Build citation with query-relevant excerpt
        if query:
            snippet = extract_relevant_excerpt(body, query, "", max_length=200)
        else:
            snippet = body[:200]

        citations.append({
            "id": citation_number,
            "record_id": meta.get("record_id") or record.get("record_id") or orig_idx,
            "legacy_idx": orig_idx,
            "title": title,
            "date": date,
            "source": source,
            "score": sc,
            "tag": tags[0] if tags else "",
            "category": cat,
            "url": url,
            "body_snippet": snippet,
            "similarity": round(score, 3),
            "source_type": build_source_metadata(record).get("source_type", "unknown") if record else "unknown",
            # ── TK-12: full citation schema (Q12/R8) ──
            # source_label: snippet provenance — records whose only text is the
            # AI-generated summary (as, no b/fb body) are labeled AI_SUMMARY.
            "source_label": "ORIGINAL",
            # evidence_spans: filled by T003 grounding; empty until grounded.
            "evidence_spans": [],
            # supports_claim_ids: filled by T004 claim mapping (agentic only —
            # legacy keeps [] and the frontend hides the mapping section).
            "supports_claim_ids": [],
            # grounding_status: filled by T003 grounding.
            "grounding_status": "UNGROUND",
        })

    context = "\n\n---\n\n".join(context_parts)
    return context, citations


# ── Request Models ──
class ChatRequest(BaseModel):
    query: str
    conversation_id: str = ""
    history: list = []


# ── Lifespan ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] Loading vector index...", flush=True)
    load_vector_index()
    print("[startup] Loading BM25 index...", flush=True)
    load_bm25_index()

    print("[startup] Loading knowledge graph...", flush=True)
    load_graph_index()

    # Build fast record_idx → meta lookup (avoids linear scan in graph_search)
    global _idx_to_meta
    _idx_to_meta = {}
    for m in _index_meta:
        _idx_to_meta[m["idx"]] = m
    for m in _bm25_meta:
        if m["idx"] not in _idx_to_meta:
            _idx_to_meta[m["idx"]] = m
    print(f"[startup] Index meta lookup: {len(_idx_to_meta)} records", flush=True)

    # Load jieba custom dictionary for query tokenization — moved into
    # load_bm25_index() (codex-review C2 P1) so non-lifespan callers share
    # the same tokenization; the lifespan block now only needs idempotence.
    if JIEBA_DICT.exists():
        import jieba
        jieba.load_userdict(str(JIEBA_DICT))

    load_records()
    print(f"[startup] Records loaded: {len(_records)}", flush=True)
    print("[startup] Ready!", flush=True)
    yield
    print("[shutdown] Cleaning up...", flush=True)


# ── FastAPI App ──
app = FastAPI(title="Tech-DB Q&A API", lifespan=lifespan)

# RT-017: deployments configure this with a validated RuntimeSnapshotManager.
# The wrapper below holds the pin for the complete SSE iterator lifetime.
_runtime_snapshot_manager = None


def configure_runtime_snapshot_manager(manager):
    global _runtime_snapshot_manager
    _runtime_snapshot_manager = manager

allowed_origins = [
    origin.strip()
    for origin in os.environ.get(
        "QA_CORS_ORIGINS",
        "https://sbq9712.github.io,http://localho