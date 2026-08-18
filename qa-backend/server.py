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
            GraphRetriever(graph_search_fn=graph_fn),
            RRFFusion(k=RRF_K, default_top_k=FINAL_TOP_K),
        )
        resources["retrieval_pipeline"] = pipeline
        resources["record_id_to_meta"] = {m["record_id"]: m for m in resources["index_meta"]}
        return pipeline
    if _retrieval_pipeline is None:
        from retrieval import VectorRetriever, BM25Retriever, GraphRetriever, RRFFusion
        load_vector_index()
        load_bm25_index()

        vr = VectorRetriever(embeddings=_vector_index, meta=_index_meta, allow_legacy_idx=True)
        br = BM25Retriever(bm25_index=_bm25_index, meta=_bm25_meta,
                           tokenize_fn=_bm25_tokenize, allow_legacy_idx=True)
        gr = GraphRetriever(graph_search_fn=lambda q, k: [
            (r["meta"]["idx"], r["score"]) for r in graph_search(q, k)
        ], allow_legacy_idx=True)
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
        meta_lookup = snapshot.resources["record_id_to_meta"]
    else:
        if _idx_to_meta is None:
            _idx_to_meta = {m["idx"]: m for m in _index_meta}
        meta_lookup = _idx_to_meta

    results = []
    for r in fused:
        meta = meta_lookup.get(r.record_id, r.meta or {})
        det = r.route_details or {}
        results.append({
            "record_id": r.record_id,
            "legacy_idx": r.legacy_idx,
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
        results = [r for r in results
                   if r["record_id"] not in exclude_ids and r.get("legacy_idx") not in exclude_ids]
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

    new_ids = [r.get("record_id") for r in (new_res or [])[:25]]
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
    pinned = _request_runtime_snapshot.get() is not None
    records = _runtime_resource("records", None) if pinned else load_records()
    records_by_id = _runtime_resource("records_by_id", None) if pinned else None
    citations = []
    context_parts = []

    for i, result in enumerate(search_results):
        meta = result["meta"]
        score = result["score"]
        record_id = result.get("record_id") or meta.get("record_id")
        orig_idx = result.get("legacy_idx", meta.get("legacy_idx", meta.get("idx", -1)))
        record = records_by_id.get(record_id) if records_by_id is not None else (
            records[orig_idx] if isinstance(orig_idx, int) and 0 <= orig_idx < len(records) else None)
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
            "record_id": record_id or record.get("record_id") or f"legacy-idx:{orig_idx}",
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
    global _runtime_snapshot_manager
    runtime_mode = os.environ.get("TECH_DB_RUNTIME_MODE", "manifest").strip().lower()
    if runtime_mode == "manifest":
        from functools import partial
        from release_manifest import ReleaseCatalog
        from runtime_snapshot import RuntimeSnapshotManager, load_release_resources
        release_root = Path(os.environ.get("TECH_DB_RELEASE_ROOT", RUNTIME_DIR)).resolve()
        catalog_dir = Path(os.environ.get("TECH_DB_RELEASE_CATALOG_DIR", release_root / "manifests"))
        catalog = ReleaseCatalog(catalog_dir, release_root)
        manager = RuntimeSnapshotManager(catalog, partial(load_release_resources, release_root=release_root))
        manager.startup(allow_previous_fallback=os.environ.get("TECH_DB_ALLOW_PREVIOUS_FALLBACK") == "1")
        _runtime_snapshot_manager = manager
        print(f"[startup] Strict manifest runtime ready: {manager.current_manifest_id}", flush=True)
        yield
        return
    if runtime_mode != "legacy_compat":
        raise RuntimeError(f"unsupported TECH_DB_RUNTIME_MODE: {runtime_mode}")
    print("[startup] Explicit legacy_idx compatibility runtime", flush=True)
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


class RuntimePinMiddleware:
    """Pin one immutable generation for the complete HTTP/stream lifetime."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or _runtime_snapshot_manager is None:
            await self.app(scope, receive, send)
            return
        with _runtime_snapshot_manager.pin() as runtime_snapshot:
            scope.setdefault("state", {})["runtime_manifest_id"] = runtime_snapshot.manifest_id
            token = _request_runtime_snapshot.set(runtime_snapshot)
            try:
                await self.app(scope, receive, send)
            finally:
                _request_runtime_snapshot.reset(token)

allowed_origins = [
    origin.strip()
    for origin in os.environ.get(
        "QA_CORS_ORIGINS",
        "https://sbq9712.github.io,http://localhost:8000,http://localhost:8097",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Key"],
)
app.add_middleware(RuntimePinMiddleware)


# ── Endpoints ──

@app.get("/api/shadow/report")
async def shadow_report():
    """TK-17 (Q18/R1): aggregate shadow diff report (id overlap / TTFB / errors).

    Only meaningful when QA_SHADOW_RETRIEVAL=1; always safe to call.
    """
    return {
        "shadow_enabled": _SHADOW_RETRIEVAL,
        **shadow_diff_report(),
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "api_key_configured": bool(os.environ.get("ZAI_API_KEY") or ENV_FILE.is_file()),
        "vector_index_ready": _vector_index is not None,
        "bm25_ready": _bm25_index is not None,
        "graph_ready": _graph_data is not None,
        "indexed_records": len(_index_meta) if _index_meta else 0,
        "bm25_records": len(_bm25_meta) if _bm25_meta else 0,
        "total_records": len(_records) if _records else 0,
        "feature_flags": Flags.status(),
        "shadow_enabled": _SHADOW_RETRIEVAL,          # TK-17 diagnostics
        "retrieval_legacy": None,  # TK-23 contract: legacy path removed (was escape hatch)
        "limits": {
            "per_minute": GUARDRAILS.per_minute,
            "per_client_day": GUARDRAILS.per_client_day,
            "global_day": GUARDRAILS.global_day,
            "concurrency": GUARDRAILS.concurrency,
        },
        "budget": BUDGET_FUSE.status(),
        # TK-09 (codex-review P2): LLM HTTP calls abandoned by TTFB-guard
        # timeouts — bounded executor + socket timeout keeps the tail short
        "llm_http_abandoned": llm_abandoned_stats(),
        "time": datetime.now().isoformat(),
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Streaming chat endpoint using SSE."""

    query = req.query.strip()
    if not query or len(query) > 2000:
        return JSONResponse(
            {"error": "问题不能为空，且长度不能超过 2000 个字符。"}, status_code=400
        )

    bypass = admin_bypass(GUARDRAILS.admin_key, request.headers.get("x-admin-key"))
    socket_ip = request.client.host if request.client else "unknown"
    client_id = client_identifier(request.headers, socket_ip)
    allowed, reason, retry_after = RATE_LIMITER.check(client_id, bypass=bypass)
    if not allowed:
        return JSONResponse(
            {"error": reason, "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        await asyncio.wait_for(CHAT_SEMAPHORE.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "当前问答请求较多，请稍后重试。", "retry_after": 5},
            status_code=429,
            headers={"Retry-After": "5"},
        )

    async def event_generator():
        trace = TraceContext.create(query, req.history[-1].get("content", "")[:100] if req.history else "")
        try:
            # Step 1: Retrieval
            yield {"event": "status", "data": json.dumps({
                "step": "retrieving",
                "message": "正在检索相关知识..."
            })}

            if _vector_index is None:
                yield {"event": "error", "data": json.dumps({
                    "message": "向量索引尚未构建。请运行: python qa-backend/vector_index.py"
                })}
                return

            # TK-09 (codex-review P1): TTFB 口径 = rewrite + retrieval + control
            # (all pre-first-answer-byte backend work) — the guard clock starts
            # BEFORE the follow-up rewrite so unbounded rewrite latency can't
            # hide outside the budget on follow-up queries with history.
            _t0_pre_answer = _time.perf_counter()
            _ttfb_degraded = False

            # Rewrite follow-up query + detect novelty intent (single LLM call)
            search_query, seeking_novelty, _reason = await rewrite_query(query, req.history)
            trace.add_stage("rewrite", {
                "original_query": query[:200],
                "rewritten_query": search_query[:200],
                "seeking_novelty": seeking_novelty,
                "reason": _reason[:100] if _reason else "",
            })

            # Check if previous round had no results (avoid "infinite no results" loop)
            prev_assistant = next((m for m in reversed(req.history)
                                   if m.get("role") == "assistant"), None)
            prev_has_results = bool(
                prev_assistant and
                (prev_assistant.get("searched_record_ids") or
                 prev_assistant.get("cited_record_ids"))
            )

            # Build exclude_ids: global accumulation of all prior assistant turns'
            # cited record_ids. Only triggered when seeking novelty AND previous
            # round had results.
            #
            # CRITICAL: use `is not None` to distinguish "field absent" (old data →
            # fall back to searched_record_ids) from "field present but empty"
            # (new data, LLM cited nothing → exclude nothing from this turn).
            exclude_ids = set()
            if seeking_novelty and prev_has_results:
                assistant_msgs = [m for m in req.history if m.get("role") == "assistant"]
                for msg in assistant_msgs:
                    cited = msg.get("cited_record_ids")
                    if cited is not None:
                        ids = cited
                    else:
                        ids = msg.get("searched_record_ids") or []
                    exclude_ids.update(ids)
                if exclude_ids:
                    print(f"[search] Novelty query, excluding {len(exclude_ids)} records "
                          f"(global accumulated)", flush=True)

            # ── Agentic RAG Path ──
            # When QA_AGENTIC_ENABLED is true, use the full agentic loop
            # (Router → Decompose → Plan → Iterative Retrieval → Grade → Gap → ...)
            # instead of the simple single-pass RAG path below.
            _agentic_succeeded = False
            if Flags.AGENTIC_ENABLED:
                from orchestrator import run_agentic_loop

                # Wrap hybrid_search for the orchestrator's search_fn interface.
                # Codex-review fix (P1): the novelty accumulator (all records
                # cited in prior assistant turns) must apply to the agentic
                # path too — a follow-up asking for NEW information must not
                # re-serve previously cited records.
                _novelty_exclude = set(exclude_ids or set())

                async def _orchestrator_search_fn(q, exclude=None):
                    merged = _novelty_exclude | (exclude or set())
                    return await hybrid_search(q, exclude_ids=merged or None)

                try:
                    # TK-09: TTFB guard — the agentic loop (router+retrieval+
                    # control, all pre-first-byte backend work) must fit within
                    # legacy-TTFB-baseline + Δ; on timeout the query degrades
                    # to the legacy single-pass path (spec Q10/R2).
                    # Codex-review fix (P1): the clock started before the
                    # follow-up rewrite, so the wait_for budget is the
                    # REMAINING time — rewrite latency counts against it.
                    _ttfb = ttfb_snapshot()
                    _elapsed_s = _time.perf_counter() - _t0_pre_answer
                    _remaining_s = guard_budget_s() - _elapsed_s
                    trace.add_stage("ttfb_guard", {
                        **_ttfb,
                        "elapsed_before_agentic_ms": round(_elapsed_s * 1000, 1),
                        "remaining_budget_ms": round(max(0.0, _remaining_s) * 1000, 1),
                    })
                    if _remaining_s <= 0.05:
                        # Rewrite (or earlier pre-answer work) already consumed
                        # the whole guard budget → degrade without attempting
                        # the agentic loop (attempting it can only overrun).
                        print(f"[agentic] TTFB budget spent pre-loop "
                              f"({ _elapsed_s:.1f}s), degrading to legacy", flush=True)
                        trace.add_stage("ttfb_degrade", {
                            "budget_ms": _ttfb["guard_ms"],
                            "elapsed_ms": round(_elapsed_s * 1000, 1),
                            "action": "degrade_to_legacy",
                            "reason": "budget_spent_before_loop",
                        })
                        _ttfb_degraded = True
                    else:
                        agentic_state = await asyncio.wait_for(
                            run_agentic_loop(
                                query=query,
                                rewritten_query=search_query,
                                history=req.history,
                                search_fn=_orchestrator_search_fn,
                                trace=trace,
                                bypass_budget=getattr(req, 'bypass_budget', False),
                            ),
                            timeout=_remaining_s,
                        )

                    if not _ttfb_degraded:
                        # Use agentic results for the rest of the pipeline
                        search_results = agentic_state.all_results[:RETRIEVAL_TOP_K]
                        is_relevant = len(search_results) > 0
                        search_status = agentic_state.stop_reason or "agentic_complete"
                        _agentic_succeeded = True

                        trace.add_stage("agentic_complete", {
                            "iterations": agentic_state.iteration,
                            "mode": agentic_state.router_result.get("mode", ""),
                            "stop_reason": agentic_state.stop_reason,
                            "total_results": len(agentic_state.all_results),
                            "answer_status": agentic_state.answer_status,
                        })
                except asyncio.TimeoutError:
                    # TK-09: agentic loop exceeded legacy-TTFB-baseline + Δ →
                    # degrade to legacy single-pass; answer still returns.
                    print(f"[agentic] TTFB guard tripped "
                          f"(>{guard_budget_s():.1f}s), degrading to legacy", flush=True)
                    trace.add_stage("ttfb_degrade", {
                        "budget_ms": ttfb_snapshot()["guard_ms"],
                        "elapsed_ms": round((_time.perf_counter() - _t0_pre_answer) * 1000, 1),
                        "action": "degrade_to_legacy",
                        # honest accounting: the legacy fallback re-runs
                        # retrieval outside the guard; the true total is
                        # measured at ttfb_total (generation start)
                        "fallback_outside_budget": True,
                    })
                    _ttfb_degraded = True
                    # Fall through to standard path
                except BudgetExceededError as be:
                    # TK-08: loop-control hard cap tripped → degrade the whole
                    # query to the legacy single-pass path; answer still returns.
                    print(f"[agentic] Budget exceeded ({be.component}), "
                          f"degrading to legacy: {be}", flush=True)
                    trace.add_stage("budget_degrade", {
                        "component": be.component,
                        "budget": be.budget.snapshot(),
                        "action": "degrade_to_legacy",
                    })
                    # Fall through to standard path
                except Exception as e:
                    print(f"[agentic] Orchestrator error, falling back: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
                    # Fall through to standard path

            # Standard RAG path (only if agentic didn't run or failed)
            if not _agentic_succeeded:
                # Hybrid search (vector + BM25 + graph → RRF)
                search_results, is_relevant, search_status = await hybrid_search(
                    search_query, exclude_ids=exclude_ids if exclude_ids else None
                )
                trace.add_stage("retrieval_hybrid", {
                    "query": search_query[:200],
                    "result_count": len(search_results),
                    "is_relevant": is_relevant,
                    "status": search_status,
                    "top_results": [
                        {"record_id": r.get("record_id"), "legacy_idx": r.get("legacy_idx"),
                         "score": round(r.get("score", 0), 4),
                         "title": r["meta"].get("t", "")[:80]}
                        for r in search_results[:10]
                    ],
                })

            # Searched record ids for done event (backend-authoritative)
            searched_record_ids = [r["record_id"] for r in search_results] if search_results else []

            if not search_results or not is_relevant:
                # Codex-review C3 P2 fix: early unsupported exits (weak query /
                # topic exhausted) previously returned WITHOUT the flag-gated
                # knowledge-boundary message even with the TK-06 flag ON —
                # the most common unsupported case missed the boundary.
                _early_boundary = _no_evidence_boundary(
                    query, search_status == "exhausted")
                if search_status == "exhausted":
                    trace.set_result(answer_status="UNSUPPORTED", stop_reason="topic_exhausted")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "前面的回答已经覆盖了这个话题的主要方面。当前数据库中暂未找到更多未讨论过的相关资料。\n\n如果你对某个具体方向感兴趣，可以换一个更精确的关键词提问，我会重新检索。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "topic_exhausted",
                        "boundary_message": _early_boundary,
                        "trace_id": trace.trace_id,
                    })}
                elif not prev_has_results and seeking_novelty:
                    trace.set_result(answer_status="UNSUPPORTED", stop_reason="weak_query")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "上一轮未找到相关资料，请尝试换个更具体的关键词提问。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "weak_query",
                        "boundary_message": _early_boundary,
                        "trace_id": trace.trace_id,
                    })}
                else:
                    trace.set_result(answer_status="UNSUPPORTED", stop_reason="weak_query")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "抱歉，数据库中没有足够的情报来回答这个问题。请尝试用更具体的关键词或换个角度提问。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "weak_query",
                        "boundary_message": _early_boundary,
                        "trace_id": trace.trace_id,
                    })}
                return

            # Build context and citations
            context, citations = build_context(search_results, query)

            # ── Epistemic Claim Classification ──
            # (skip if budget exhausted — epistemic is enhancement, not critical path)
            claim_metadata = []
            try:
                classify_budget_ok, _ = BUDGET_FUSE.reserve(bypass=bypass)
                if classify_budget_ok:
                    print(f"[epistemic] Classifying claims for top-5 chunks", flush=True)
                    claim_metadata = await classify_claims(query, search_results, top_k=5)
                    print(f"[epistemic] Classification done: {len(claim_metadata)} chunks classified", flush=True)
            except Exception as e:
                print(f"[epistemic-classify] {e}", flush=True)

            # Yield citations
            if citations:
                yield {"event": "citations", "data": json.dumps({"citations": citations})}

            # Step 2: Analysis
            yield {"event": "status", "data": json.dumps({
                "step": "analyzing",
                "message": f"找到 {len(citations)} 条相关记录，正在分析..."
            })}

            await asyncio.sleep(0.1)  # Small delay for UX

            # Step 3: Generation
            budget_ok, _ = BUDGET_FUSE.reserve(bypass=bypass)
            if not budget_ok:
                trace.set_result(answer_status="UNVERIFIED", stop_reason="budget_exceeded")
                trace.flush()
                yield {"event": "error", "data": json.dumps({
                    "message": "今日问答费用预算已达到上限，服务已自动暂停。",
                    "answer_status": "UNVERIFIED",
                    "stop_reason": "budget_exceeded",
                    "trace_id": trace.trace_id,
                })}
                return

            yield {"event": "status", "data": json.dumps({
                "step": "generating",
                "message": "正在生成回答..."
            })}

            # TK-09 (codex-review P1): total pre-first-answer-byte backend time
            # (rewrite + retrieval + agentic control + epistemic classify).
            # Measured at generation start so guard overruns — including the
            # legacy fallback retrieval performed AFTER a ttfb_degrade — are
            # visible rather than implicit.
            trace.add_stage("ttfb_total", {
                "pre_answer_ms": round((_time.perf_counter() - _t0_pre_answer) * 1000, 1),
                "guard_ms": ttfb_snapshot()["guard_ms"],
                "ttfb_degraded": _ttfb_degraded,
                "agentic_succeeded": _agentic_succeeded,
            })

            # Build source list for prompt
            source_list = "\n".join(
                f"[{i+1}] {c['title']} ({c['date']}, {c['source']})"
                for i, c in enumerate(citations)
            )

            # Build system prompt
            base_prompt = f"""你是技术情报分析专家。基于以下检索到的技术情报资料回答用户问题。

要求：
1. 只基于提供的资料回答，不要编造信息
2. 在回答中用 [1][2] 等标注引用来源（对应来源列表的序号）
3. 如果资料中没有相关信息，诚实回答"数据库中没有相关信息"
4. 简单问题简短回答，复杂问题详细分析
5. 使用中文回答，使用markdown格式
6. 如果用户在追问补充信息，优先展示上一轮回答中未讨论过的角度和数据

检索到的资料：
{context}

来源列表：
{source_list}"""

            # Enhance with epistemic protection rules
            system_prompt = build_epistemic_system_prompt(base_prompt, claim_metadata)

            # Build conversation history for LLM
            llm_history = []
            for msg in req.history[-6:]:
                llm_history.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

            full_answer = ""
            try:
                # Stream directly from LLM
                async for chunk in llm_stream_func(
                    prompt=query,
                    system_prompt=system_prompt,
                    history_messages=llm_history,
                ):
                    if chunk:
                        full_answer += chunk
                        yield {"event": "token", "data": json.dumps({"text": chunk})}
            except Exception as e:
                # Fallback: non-streaming
                print(f"[stream-fallback] {e}", flush=True)
                try:
                    answer = await llm_model_func(
                        query,
                        system_prompt=system_prompt,
                        history_messages=llm_history,
                    )
                    if answer:
                        full_answer = answer
                        for i in range(0, len(answer), 3):
                            yield {"event": "token", "data": json.dumps({"text": answer[i:i+3]})}
                            await asyncio.sleep(0.015)
                except Exception as e2:
                    yield {"event": "error", "data": json.dumps({
                        "message": f"生成失败: {e2}"
                    })}
                    return

            # Parse [N] citations from the generated answer
            cited_record_ids = _parse_citations_from_answer(full_answer, citations)

            # TK-08: post-processing budget — counted separately from the
            # loop-control class; these calls NEVER degrade the agentic path
            # and also run on the legacy path (spec Q4/R3).
            _pp_budget = QueryBudget()

            # ── T005: Fail-Safe Verification ──
            # Uses the new fail-safe verifier that NEVER returns PASSED on errors.
            # Correctness-critical: BudgetFuse cannot silently skip this.
            verification_status = "PASSED"
            verification_issues = []
            verification_error = ""  # TK-10: last failure cause, for the user warning
            if claim_metadata and full_answer.strip():
                try:
                    # Use budget_guard to ensure correctness-critical handling
                    budget_ok, _ = BUDGET_FUSE.reserve(bypass=bypass)
                    decision, should_call, status_override = check_budget("verifier", budget_ok)

                    if should_call:
                        print(f"[verify] Verifying answer ({len(full_answer)} chars) against {len(claim_metadata)} chunks", flush=True)
                        _pp_budget.record_post("verifier")  # TK-08: post-class, never degrades
                        vr = await verify_with_fail_safe(query, full_answer, claim_metadata)
                        verification_status = vr.status  # PASSED / FAILED / UNVERIFIED
                        if verification_status == VERIFY_UNVERIFIED:
                            verification_error = vr.failure_reason or "verification returned UNVERIFIED"
                        trace.add_stage("verification", {
                            "status": vr.status,
                            "issues": vr.issues[:5],
                            "failure_reason": vr.failure_reason,
                        })
                        print(f"[verify] Result: {vr.status}", flush=True)
                        if vr.status == VERIFY_FAILED:
                            verification_issues = vr.issues
                            rewritten = vr.rewritten_answer.strip()
                            if rewritten and len(rewritten) > 20:
                                full_answer = rewritten
                                cited_record_ids = _parse_citations_from_answer(full_answer, citations)
                                yield {"event": "replace", "data": json.dumps({
                                    "answer": full_answer,
                                    "verified": True
                                })}
                    elif status_override:
                        # Budget exhausted for correctness-critical verification
                        # MUST NOT silently pass. Mark as UNVERIFIED.
                        verification_status = status_override  # "UNVERIFIED"
                        verification_error = "verification skipped due to budget"
                        print(f"[verify] SKIPPED due to budget — marking {verification_status}", flush=True)
                        trace.add_stage("verification", {
                            "status": "SKIPPED_BUDGET",
                            "note": f"Verification skipped due to budget; answer marked {verification_status}",
                            "budget_guard": decision.value,
                        })
                except Exception as e:
                    # Any exception → UNVERIFIED, never PASS
                    verification_status = VERIFY_UNVERIFIED
                    verification_error = str(e)
                    print(f"[verify] Exception → UNVERIFIED: {e}", flush=True)
                    trace.add_stage("verification", {
                        "status": "EXCEPTION",
                        "error": str(e),
                        "api_failure": looks_like_api_failure(str(e)),  # TK-10
                    })

            # ── T004: Claim Mapping ──
            claim_map = {"claims": []}
            if Flags.CLAIM_MAPPING_ENABLED and full_answer.strip() and citations:
                try:
                    claim_budget_ok, _ = BUDGET_FUSE.reserve(bypass=bypass)
                    if claim_budget_ok:
                        _pp_budget.record_post("claim_mapping")  # TK-08: post-class, never degrades
                        claim_map = await map_claims_to_citations(query, full_answer, citations)
                        # T048: attach span-level source lineage so independence
                        # is counted per claim/span (quotes of a primary source
                        # never count as independent verification). Deterministic,
                        # no LLM; failures degrade to lineage-less claims.
                        # evidence_role uses the SAME canonical classifier as
                        # T007's offline enrichment (scripts/enrich_evidence_
                        # metadata.py), re-derived online for cited records.
                        try:
                            import sys as _sys, os as _os
                            _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                            _scripts = _os.path.join(_root, "scripts")
                            for _p in (_scripts,):
                                if _p not in _sys.path:
                                    _sys.path.insert(0, _p)
                            from enrich_evidence_metadata import infer_evidence_role
                            from claim_mapping import attach_span_lineage, claim_independence
                            _prov_map = {}
                            for c in citations:
                                rid = c.get("record_id")
                                if rid is not None and 0 <= rid < len(_records):
                                    _rec = _records[rid]
                                    _prov_map[rid] = {
                                        "evidence_role": infer_evidence_role(_rec),
                                        "independent_group_id": f"record:{rid}",
                                    }
                            attach_span_lineage(claim_map, citations,
                                                provenance_map=_prov_map)
                            _indep = claim_independence(claim_map, _prov_map)
                            trace.add_stage("claim_independence", {
                                "claims_total": _indep["claims_total"],
                                "claims_with_independent_support":
                                    _indep["claims_with_independent_support"],
                            })
                        except Exception as e:
                            print(f"[claim_lineage] Error: {e}", flush=True)
                        trace.add_stage("claim_mapping", {
                            "total_claims": len(claim_map.get("claims", [])),
                            "unsupported_major": len(get_unsupported_major_claims(claim_map)),
                        })
                except Exception as e:
                    print(f"[claim_mapping] Error: {e}", flush=True)
            # TK-12 (Q12/R8): supports_claim_ids — inverse of each claim's
            # supported_by map (citation_id → [claim ids]). Filled whenever
            # claim_mapping ran (agentic); stays [] otherwise and the UI hides
            # the mapping section.
            if claim_map.get("claims"):
                _by_cit = {}
                for cl in claim_map["claims"]:
                    for sup in (cl.get("supported_by") or []):
                        # Codex-review B2 P2 fix: only genuinely SUPPORTIVE
                        # relations belong in supports_claim_ids — including
                        # CONTRADICTS/BACKGROUND made contradictions render
                        # as claim support in the evidence card. Keep
                        # BACKGROUND-context relations out of the card too:
                        # the card's contract is "this citation supports this
                        # claim" (TK-12), not "merely related".
                        if sup.get("relation") not in ("DIRECT_SUPPORT",
                                                       "PREMISE_SUPPORT",
                                                       "ATTRIBUTION"):
                            continue
                        cid = sup.get("citation_id")
                        if cid is not None:
                            _by_cit.setdefault(cid, []).append(cl.get("id"))
                for c in citations:
                    c["supports_claim_ids"] = _by_cit.get(c.get("id"), [])

            # ── T003: Citation Evidence Grounding ──
            # Ground each citation to exact original text span
            if Flags.CITATION_GROUNDING_ENABLED and citations:
                for c in citations:
                    try:
                        rid = c.get("record_id", -1)
                        if rid >= 0 and rid < len(_records):
                            rec = _records[rid]
                            grounding = ground_citation_evidence(
                                rec,
                                proposed_span=c.get("excerpt") or c.get("body_snippet", ""),
                                claim_text="",
                                query=query,
                            )
                            if grounding["grounding_status"] != "GROUNDING_FAIL":
                                c["evidence_span"] = grounding["evidence_span"]
                                c["evidence_start"] = grounding["start_offset"]
                                c["evidence_end"] = grounding["end_offset"]
                                c["grounding_status"] = grounding["grounding_status"]
                                # TK-12: list-form spans + highlight (str, spec Q12)
                                c["evidence_spans"] = [{
                                    "text": grounding["evidence_span"],
                                    "start": grounding["start_offset"],
                                    "end": grounding["end_offset"],
                                }]
                                c["highlight"] = grounding["evidence_span"]
                                # TK-12: refine source_label when the grounded
                                # span actually came from the AI summary field
                                if grounding.get("source_field") == "as":
                                    c["source_label"] = "AI_SUMMARY"
                            else:
                                c["evidence_span"] = c.get("excerpt") or c.get("body_snippet", "")
                                c["grounding_status"] = "GROUNDING_FAIL"
                                c["evidence_spans"] = []
                    except Exception:
                        pass
                trace.add_stage("citation_grounding", {
                    "grounded": sum(1 for c in citations if c.get("grounding_status") in ("VALID", "FUZZY")),
                    "failed": sum(1 for c in citations if c.get("grounding_status") == "GROUNDING_FAIL"),
                })
            trace.add_stage("post_budget", _pp_budget.snapshot())

            # ── T006: Four-State Answer Status ──
            answer_status_str = "SUPPORTED"
            stop_reason = "evidence_sufficient"
            if Flags.ANSWER_STATUS_ENABLED:
                status_enum, stop_reason = determine_answer_status(
                    has_results=bool(search_results),
                    is_relevant=is_relevant,
                    verification_status=verification_status,
                    claim_mapping=claim_map,
                )
                answer_status_str = status_enum.value

            # ── TK-06 (R9): Knowledge boundary / calibrated abstention ──
            # Non-LLM: for UNSUPPORTED/PARTIALLY_SUPPORTED answers, attach a
            # calibrated boundary message ("当前数据库缺少…" ≠ "现实中不存在")
            # so the user understands the boundary is the DB's, not reality's.
            boundary_message = ""
            if Flags.KNOWLEDGE_BOUNDARY_ENABLED and answer_status_str in (
                    "UNSUPPORTED", "PARTIALLY_SUPPORTED"):
                try:
                    from knowledge_boundary import (
                        assess_coverage, format_boundary_message, AnswerStatus as KBStatus,
                    )
                    grounded = sum(1 for c in citations
                                   if c.get("grounding_status") in ("VALID", "FUZZY"))
                    independent = len({c.get("source", "") for c in citations})
                    claim_rows = claim_map.get("claims", [])[:5]
                    # codex-review C3 P2 fix: claim schema is
                    # {id, text, support_status} (claim_mapping.py) — the old
                    # reads of .status/.claim were always-missing keys, so
                    # requirements defaulted to all-MISSING and aspect lists
                    # were always empty. Also map the claim vocabulary
                    # (SUPPORTED/PARTIALLY_SUPPORTED/UNSUPPORTED) onto
                    # assess_coverage's requirement vocabulary
                    # (SUPPORTED/PARTIAL/MISSING).
                    _req_status = {"SUPPORTED": "SUPPORTED",
                                   "PARTIALLY_SUPPORTED": "PARTIAL",
                                   "UNSUPPORTED": "MISSING"}
                    requirements = [{"status": _req_status.get(
                                        c.get("support_status", "UNSUPPORTED"), "MISSING"),
                                     "text": c.get("text", "")}
                                    for c in claim_rows] or \
                                   [{"status": "MISSING", "text": query}]
                    coverage = assess_coverage(
                        requirements=requirements,
                        evidence_count=grounded,
                        independent_groups=independent,
                    )
                    kb_status = (KBStatus.UNSUPPORTED if answer_status_str == "UNSUPPORTED"
                                 else KBStatus.PARTIALLY_SUPPORTED)
                    supported_aspects = [c.get("text", "") for c in claim_map.get("claims", [])
                                         if c.get("support_status") == "SUPPORTED"][:5]
                    unsupported_aspects = [c.get("text", "") for c in claim_map.get("claims", [])
                                           if c.get("support_status") != "SUPPORTED"][:5]
                    boundary_message = format_boundary_message(
                        answer_status=kb_status,
                        supported_aspects=supported_aspects,
                        unsupported_aspects=unsupported_aspects or [query],
                        coverage_level=coverage,
                    )
                    trace.add_stage("knowledge_boundary", {
                        "coverage_level": coverage,
                        "independent_sources": independent,
                        "grounded_citations": grounded,
                    })
                except Exception as e:
                    print(f"[knowledge_boundary] Error: {e}", flush=True)

            # ── TK-10 (Q11): GLM API failure → legacy result UNVERIFIED + user warning ──
            user_warning = build_user_warning(
                answer_status=answer_status_str,
                verification_status=verification_status,
                verification_error=verification_error,
            )

            evidence_summary = build_evidence_summary(
                claim_mapping=claim_map,
                independent_sources=len(set(c.get("source", "") for c in citations)),
                iterations=1,
            )

            trace.set_result(
                answer=full_answer[:500],
                answer_status=answer_status_str,
                stop_reason=stop_reason,
                citations=citations,
                cited_record_ids=cited_record_ids,
                verification_status=verification_status,
            )

            yield {"event": "done", "data": json.dumps({
                "answer": full_answer,
                "citations": citations,
                "claims": [{"id": c.get("id"), "text": c.get("text", "")[:120],
                            "status": c.get("support_status", "")}
                           for c in claim_map.get("claims", [])[:12]],
                "cited_record_ids": cited_record_ids,
                "searched_record_ids": searched_record_ids,
                "answer_status": answer_status_str,
                "stop_reason": stop_reason,
                "boundary_message": boundary_message,
                "user_warning": user_warning,
                "evidence_summary": evidence_summary,
                "trace_id": trace.trace_id,
            })}

            trace.flush()

        except Exception as e:
            import traceback
            traceback.print_exc()
            trace.set_result(answer_status="UNVERIFIED", stop_reason="error", error=str(e)[:200])
            trace.flush()
            yield {"event": "error", "data": json.dumps({"message": str(e)})}
        finally:
            CHAT_SEMAPHORE.release()

    return EventSourceResponse(event_generator())


@app.get("/api/graph")
async def get_graph(limit: int = 300):
    """Get knowledge graph data for visualization."""
    graph_file = WORKING_DIR / "graph-export.json"
    if graph_file.exists():
        data = json.loads(graph_file.read_text("utf-8"))
        nodes = sorted(data.get("nodes", []), key=lambda n: n.get("degree", 0), reverse=True)
        if limit and len(nodes) > limit:
            top_node_ids = {n["id"] for n in nodes[:limit]}
            nodes = nodes[:limit]
            edges = [e for e in data.get("edges", [])
                     if e["source"] in top_node_ids and e["target"] in top_node_ids]
        else:
            edges = data.get("edges", [])
        return {
            "nodes": nodes,
            "edges": edges,
            "total_nodes": len(data.get("nodes", [])),
            "total_edges": len(data.get("edges", [])),
        }
    return {
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "message": "Graph not yet built."
    }


@app.get("/api/stats")
async def stats():
    """Get system statistics."""
    total = len(_records) if _records else 0
    indexed = len(_index_meta) if _index_meta else 0

    graph_file = WORKING_DIR / "graph-export.json"
    nodes = edges = 0
    if graph_file.exists():
        graph = json.loads(graph_file.read_text("utf-8"))
        nodes = len(graph.get("nodes", []))
        edges = len(graph.get("edges", []))

    return {
        "total_records": total,
        "indexed_records": indexed,
        "bm25_records": len(_bm25_meta) if _bm25_meta else 0,
        "graph_nodes": nodes,
        "graph_edges": edges,
        "vector_index_ready": _vector_index is not None,
        "bm25_ready": _bm25_index is not None,
        "model": MODEL_NAME,
    }


@app.get("/api/search")
async def search(q: str, top_k: int = 10):
    """Quick vector search without LLM generation. Returns matching records."""
    snapshot = _request_runtime_snapshot.get()
    if snapshot is None and _vector_index is None:
        return JSONResponse(
            {"error": "Vector index not loaded", "results": []},
            status_code=503,
        )
    if not q.strip():
        return {"results": [], "query": q}

    results, _is_relevant, _status = await hybrid_search(q.strip())
    context, citations = build_context(results, q.strip())
    return {
        "query": q,
        "results": citations,
        "total": len(citations),
        "runtime_manifest_id": snapshot.manifest_id if snapshot else None,
    }


if __name__ == "__main__":
    import uvicorn
    # T040: production must run a named pipeline profile (no ad-hoc flag
    # combos). TECH_DB_ENV=production without a valid QA_PIPELINE_PROFILE
    # refuses to start — silent half-migrations are the failure mode.
    from feature_flags import assert_production_profile
    _profile = assert_production_profile()
    if _profile:
        print(f"[startup] pipeline profile: {_profile}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8765)
