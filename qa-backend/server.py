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
import sys
import json
import pickle
import asyncio
import re
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
    llm_stream_func, MODEL_NAME,
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
)

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
    """Load the pre-built BM25 index."""
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
        if c and c.get("record_id", -1) >= 0:
            result.append(c["record_id"])
    return list(dict.fromkeys(result))  # dedupe, preserve order


async def _search_with_quality(query: str, exclude_ids: set = None) -> tuple:
    """Run three-route retrieval + RRF fusion + exclusion + quality gate.

    Returns (results, is_relevant).
    """
    fetch_k = min(RETRIEVAL_TOP_K + (len(exclude_ids) if exclude_ids else 0), FETCH_K_CAP)

    vec_task = vector_search(query, top_k=fetch_k)
    bm25_task = asyncio.to_thread(bm25_search, query, fetch_k)
    graph_task = asyncio.to_thread(graph_search, query, fetch_k)

    vec_results, bm25_results, graph_results = await asyncio.gather(
        vec_task, bm25_task, graph_task
    )

    fuse_top_k = fetch_k if exclude_ids else FINAL_TOP_K
    results = rrf_fuse(
        vec_results, bm25_results,
        graph_results if graph_results else None,
        top_k=fuse_top_k,
    )

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
    records = load_records()
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
        body = record.get("b", "") or record.get("fb", "") or ""
        ai_summary = record.get("as", "") or ""
        cat = record.get("c", "") or ""
        tags = record.get("tg", [])
        if isinstance(tags, str):
            tags = [tags]
        url = record.get("u", "") or ""
        sc = record.get("sc", 0)

        # Add to context
        context_parts.append(
            f"[{i+1}] [{cat}] {title} ({date})\n"
            f"来源: {source}\n"
            f"摘要: {ai_summary[:300] if ai_summary else body[:300]}\n"
            f"相似度: {score:.2f}"
        )

        # Build citation with query-relevant excerpt
        if query:
            snippet = extract_relevant_excerpt(body, query, ai_summary, max_length=200)
        else:
            snippet = body[:200] if body else (ai_summary[:200] if ai_summary else "")

        citations.append({
            "id": i + 1,
            "record_id": orig_idx,
            "title": title,
            "date": date,
            "source": source,
            "score": sc,
            "tag": tags[0] if tags else "",
            "category": cat,
            "url": url,
            "body_snippet": snippet,
            "similarity": round(score, 3),
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

    # Load jieba custom dictionary for query tokenization
    if JIEBA_DICT.exists():
        import jieba
        jieba.load_userdict(str(JIEBA_DICT))
        print("[startup] Jieba custom dict loaded", flush=True)

    load_records()
    print(f"[startup] Records loaded: {len(_records)}", flush=True)
    print("[startup] Ready!", flush=True)
    yield
    print("[shutdown] Cleaning up...", flush=True)


# ── FastAPI App ──
app = FastAPI(title="Tech-DB Q&A API", lifespan=lifespan)

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


# ── Endpoints ──

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "api_key_configured": bool(os.environ.get("ZAI_API_KEY") or ENV_FILE.is_file()),
        "vector_index_ready": _vector_index is not None,
        "bm25_ready": _bm25_index is not None,
        "indexed_records": len(_index_meta) if _index_meta else 0,
        "bm25_records": len(_bm25_meta) if _bm25_meta else 0,
        "total_records": len(_records) if _records else 0,
        "limits": {
            "per_minute": GUARDRAILS.per_minute,
            "per_client_day": GUARDRAILS.per_client_day,
            "global_day": GUARDRAILS.global_day,
            "concurrency": GUARDRAILS.concurrency,
        },
        "budget": BUDGET_FUSE.status(),
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

            # Rewrite follow-up query + detect novelty intent (single LLM call)
            search_query, seeking_novelty, _reason = await rewrite_query(query, req.history)

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

            # Hybrid search (vector + BM25 + graph → RRF)
            search_results, is_relevant, status = await hybrid_search(
                search_query, exclude_ids=exclude_ids if exclude_ids else None
            )

            # Searched record ids for done event (backend-authoritative)
            searched_record_ids = [r["meta"]["idx"] for r in search_results] if search_results else []

            if not search_results or not is_relevant:
                if status == "exhausted":
                    yield {"event": "done", "data": json.dumps({
                        "answer": "前面的回答已经覆盖了这个话题的主要方面。当前数据库中暂未找到更多未讨论过的相关资料。\n\n如果你对某个具体方向感兴趣，可以换一个更精确的关键词提问，我会重新检索。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": []
                    })}
                elif not prev_has_results and seeking_novelty:
                    yield {"event": "done", "data": json.dumps({
                        "answer": "上一轮未找到相关资料，请尝试换个更具体的关键词提问。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": []
                    })}
                else:
                    yield {"event": "done", "data": json.dumps({
                        "answer": "抱歉，数据库中没有足够的情报来回答这个问题。请尝试用更具体的关键词或换个角度提问。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": []
                    })}
                return

            # Build context and citations
            context, citations = build_context(search_results, query)

            # ── Epistemic Claim Classification ──
            claim_metadata = []
            try:
                claim_metadata = await classify_claims(query, search_results, top_k=5)
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
                yield {"event": "error", "data": json.dumps({
                    "message": "今日问答费用预算已达到上限，服务已自动暂停。"
                })}
                return

            yield {"event": "status", "data": json.dumps({
                "step": "generating",
                "message": "正在生成回答..."
            })}

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

            # ── Epistemic Answer Verification ──
            # Verify draft answer against classified evidence
            if claim_metadata and full_answer.strip():
                try:
                    verification = await verify_answer(query, full_answer, claim_metadata)
                    if not verification.get("passed"):
                        rewritten = verification.get("rewritten_answer", "").strip()
                        if rewritten and len(rewritten) > 20:
                            full_answer = rewritten
                            cited_record_ids = _parse_citations_from_answer(full_answer, citations)
                except Exception as e:
                    print(f"[epistemic-verify] {e}", flush=True)

            yield {"event": "done", "data": json.dumps({
                "answer": full_answer,
                "citations": citations,
                "cited_record_ids": cited_record_ids,
                "searched_record_ids": searched_record_ids
            })}

        except Exception as e:
            import traceback
            traceback.print_exc()
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
    if _vector_index is None:
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
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
