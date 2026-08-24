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
from phase02_pipeline import run_phase02_verification, CITATION_SCHEMA_VERSION
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

# ── Global state / core retrieval (RT-030) ─────────────────────────────────
# Core Vector/BM25/Graph index loading + search algorithms MOVED to
# retrieval/runtime.py; server.py keeps API glue only (admission, request
# orchestration, profile dispatch, SSE serialization). The re-exports below
# preserve the frozen parity surfaces (tests_parity.py gate-1 baselines,
# parity.py, epistemic.load_records) — thin delegates, NOT a second parallel
# implementation.
import retrieval.runtime as _rt

graph_search = _rt.graph_search
_bm25_tokenize = _rt.bm25_tokenize

# Frozen constant surface (values moved verbatim; re-exported for consumers)
RRF_K = _rt.RRF_K
RETRIEVAL_TOP_K = _rt.RETRIEVAL_TOP_K
FINAL_TOP_K = _rt.FINAL_TOP_K
RELEVANCE_FLOOR = _rt.RELEVANCE_FLOOR
VEC_STRONG = _rt.VEC_STRONG
BM25_STRONG = _rt.BM25_STRONG
GRAPH_STRONG = _rt.GRAPH_STRONG
MIN_STRONG_RESULTS = _rt.MIN_STRONG_RESULTS
MAX_HOP1_DEGREE = _rt.MAX_HOP1_DEGREE
HOP1_WEIGHT = _rt.HOP1_WEIGHT
MAX_HOP1_ENTITIES = _rt.MAX_HOP1_ENTITIES
FETCH_K_CAP = _rt.FETCH_K_CAP



def _sync_shadow(attr: str, value) -> None:
    """Write loaded state back to an explicitly-shadowed server global.

    RT-030 moved the index globals into retrieval.runtime, but the frozen
    test seam resets them by assigning server.<attr> = None and expects the
    next loader call to reload from the (possibly patched) live paths.
    Shadow-sync keeps that contract: a None-shadow clears runtime state
    before loading; a loaded result refreshes any existing shadow.
    """
    g = globals()
    if attr in g:
        if g[attr] is None and value is None:
            setattr(_rt, attr, None)
        if value is not None:
            g[attr] = value


def load_vector_index():
    """Load vector index from the LIVE module paths (test patches honored)."""
    _sync_shadow("_index_meta", None)
    _sync_shadow("_vector_index", None)
    _rt.load_vector_index(vector_file=INDEX_FILE, fallback_file=WORKING_DIR / "vector_index.pkl")
    _sync_shadow("_index_meta", _rt._index_meta)
    _sync_shadow("_vector_index", _rt._vector_index)


def load_bm25_index():
    """Load BM25 index from the LIVE module paths (test patches honored)."""
    _sync_shadow("_bm25_index", None)
    _sync_shadow("_bm25_meta", None)
    _sync_shadow("_bm25_corpus", None)
    _rt.load_bm25_index(bm25_file=BM25_FILE, jieba_file=JIEBA_DICT)
    _sync_shadow("_bm25_index", _rt._bm25_index)
    _sync_shadow("_bm25_meta", _rt._bm25_meta)
    _sync_shadow("_bm25_corpus", _rt._bm25_corpus)


def load_graph_index():
    """Load graph export from the LIVE module path (test patches honored)."""
    _sync_shadow("_graph_data", None)
    _sync_shadow("_entity_index", None)
    _sync_shadow("_graph_adj", None)
    _sync_shadow("_graph_nodes", None)
    _rt.load_graph_index(graph_file=WORKING_DIR / "graph-export.json")
    _sync_shadow("_graph_data", _rt._graph_data)
    _sync_shadow("_entity_index", _rt._entity_index)
    _sync_shadow("_graph_adj", _rt._graph_adj)
    _sync_shadow("_graph_nodes", _rt._graph_nodes)


def build_idx_meta_lookup():
    """Rebuild the record_idx→meta fast lookup from loaded state."""
    _sync_shadow("_idx_to_meta", None)
    _rt.build_idx_meta_lookup()
    _sync_shadow("_idx_to_meta", _rt._idx_to_meta)

async def vector_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Frozen parity surface — delegates with the live embedding seam."""
    return await _rt.vector_search(query, top_k=top_k, embed_fn=embedding_func)


def bm25_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Frozen parity surface — delegates to retrieval.runtime."""
    return _rt.bm25_search(query, top_k=top_k)


def rrf_fuse(vec_results, bm25_results, graph_results=None, k=RRF_K, top_k=FINAL_TOP_K):
    """Frozen parity surface — delegates to retrieval.runtime."""
    return _rt.rrf_fuse(vec_results, bm25_results, graph_results=graph_results,
                        k=k, top_k=top_k)

_request_runtime_snapshot = ContextVar("techdb_runtime_snapshot", default=None)


def _runtime_resource(name, legacy_value):
    snapshot = _request_runtime_snapshot.get()
    if snapshot is None:
        return legacy_value
    if name not in snapshot.resources:
        raise RuntimeError(f"pinned runtime {snapshot.manifest_id} missing resource {name}")
    return snapshot.resources[name]

GUARDRAILS = GuardrailSettings()
RATE_LIMITER = RateLimiter(GUARDRAILS)
BUDGET_FUSE = BudgetFuse(GUARDRAILS, RUNTIME_DIR / "state" / "usage.json")
CHAT_SEMAPHORE = asyncio.Semaphore(GUARDRAILS.concurrency)


# Route/search functions and index loaders now live in retrieval/runtime.py
# (RT-030). The INJECTABLE PARITY SEAM defined above stays live: the frozen
# parity surface calls server.vector_search / server.bm25_search /
# server.rrf_fuse / server.graph_search, and tests patch
# server.embedding_func — server.vector_search therefore MUST resolve its
# embedding through the server module global at call time (review blocker 1:
# rebinding `vector_search = _rt.vector_search` here dropped the seam and
# made CI (no torch) crash through config.embedding_func).
# server.graph_search is bound above (no embedding seam involved).


_records_cache = []  # [(records_list)] — lifespan cache (legacy mode)


def _legacy_state(name):
    """Proxy legacy index globals to retrieval.runtime state (RT-030)."""
    return getattr(_rt, name)


def __getattr__(name):
    # Module-level proxy for the moved legacy globals: tests and internal
    # readers keep transparent access (server._index_meta, server._records,
    # ...) while the state itself lives in retrieval.runtime.
    _proxied = {
        "_vector_index", "_index_meta", "_bm25_index", "_bm25_meta",
        "_bm25_corpus", "_graph_data", "_entity_index", "_graph_adj",
        "_graph_nodes", "_idx_to_meta", "_records",
    }
    if name in _proxied:
        if name == "_records":
            # Parity: the pre-RT-030 global was None until lifespan loaded it —
            # the proxy must NOT eagerly load (tests patch LITE_PATH first).
            return _records_cache[0] if _records_cache else None
        return getattr(_rt, name)
    raise AttributeError(f"module 'server' has no attribute {name!r}")


def load_records():
    """Load full record lookup from the LIVE module path (legacy mode)."""
    _sync_shadow("_records", None)
    records = _rt.load_records(lite_file=LITE_PATH)
    _sync_shadow("_records", records)
    return records


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


def _get_retrieval_pipeline():
    """Unified retrieval pipeline (RT-030): delegate to retrieval.runtime.

    Snapshot-pinned in manifest mode; process-global legacy pipeline
    otherwise (loads through the live server paths first — the reviewed
    `_get_retrieval_pipeline` called load_vector_index()/load_bm25_index()
    before assembling).
    """
    snapshot = _request_runtime_snapshot.get()
    if snapshot is not None:
        return _rt.snapshot_pipeline(snapshot)
    load_vector_index()
    load_bm25_index()
    return _rt.legacy_pipeline()


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
    """Unified retrieval layer path (TK-05/RT-030). Same contract as legacy:
    (results, is_relevant) with legacy result dict shape.

    Implementation now lives in retrieval/runtime.run_hybrid (RT-030);
    server keeps only the request-pinned snapshot handoff. Parity
    invariants (locked by tests_parity.py frozen gate-1 baselines) are
    documented there and preserved bit-for-bit.
    """
    snapshot = _request_runtime_snapshot.get()
    # server-side pipeline resolution keeps the live-path loading seam
    # (tests patch server.INDEX_FILE/BM25_FILE/LITE_PATH before first use)
    pipeline = _get_retrieval_pipeline()
    return await _rt.run_hybrid(query, snapshot=snapshot, exclude_ids=exclude_ids,
                                embed_fn=embedding_func, pipeline=pipeline)


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


# ── Phase 03 (RT-030..039): EvidencePackage generation context ─────────────

_PHASE03_MODE = os.environ.get("QA_PHASE03_DEFAULT_MODE", "RESEARCH_RAG").strip() \
    or "RESEARCH_RAG"


class Phase03AuthorityError(RuntimeError):
    """Trusted EvidencePackage mode requires a request-pinned RuntimeSnapshot
    carrying a source_catalog (Phase-02 RT-020 contract). Fabricating
    source_snapshot_ids from record ids or hashing mutable global text are
    FORBIDDEN authority sources (review blocker 7) — fail closed instead."""


def _phase03_runtime_inputs():
    """Request-pinned records + pinned source-catalog authority.

    TRUSTED-MODE CONTRACT (review blocker 7): with EVIDENCE_PACKAGE_ENABLED
    the typed EvidencePackage path may build evidence ONLY from a
    request-pinned RuntimeSnapshot whose resources carry a source_catalog
    (content-addressed, startup-validated). Resolution follows the exact
    Phase-02 RT-020 rules (phase02_pipeline._resolve_snapshot):

      * no pinned snapshot / no catalog → Phase03AuthorityError (fail closed)
      * record not in pinned catalog   → authority gap (never fabricated)
      * declared text hash mismatch    → authority gap (tamper fail-closed)
      * declared eligibility missing / mismatch → authority gap

    Identity (source_snapshot_id) always comes from the PINNED catalog —
    never content-addressed ad hoc from mutable text. Records without
    catalog authority are returned as authority_gaps and can never enter
    the trusted EvidencePackage as support.

    Returns (records, records_by_id, snapshot_index, evidence_metadata,
    authority_gaps).
    """
    pinned = _request_runtime_snapshot.get()
    if pinned is None:
        raise Phase03AuthorityError(
            "EVIDENCE_PACKAGE_ENABLED requires manifest runtime mode with a "
            "request-pinned RuntimeSnapshot — legacy_hybrid global state is "
            "not a trusted evidence authority (fail closed)")
    records = _runtime_resource("records", None) or []
    records_by_id = _runtime_resource("records_by_id", None) or {}
    catalog = _runtime_resource("source_catalog", None) or {}
    catalog_entries = catalog.get("snapshots") or []
    if not catalog_entries:
        raise Phase03AuthorityError(
            "request-pinned runtime snapshot carries no source_catalog — "
            "cannot build trusted evidence refs (fail closed)")

    from source_snapshot import SourceSnapshot
    catalog_by_rid = {}
    for entry in catalog_entries:
        rid = entry.get("record_id")
        if isinstance(rid, str) and rid.strip():
            catalog_by_rid[rid] = entry

    snapshot_index = {}
    evidence_metadata = {}
    authority_gaps = []
    for rec in records:
        rid = rec.get("record_id")
        if not (isinstance(rid, str) and rid.strip()):
            continue
        entry = catalog_by_rid.get(rid)
        try:
            snap = SourceSnapshot.from_record(rid, rec)
        except Exception:
            authority_gaps.append({"record_id": rid,
                                   "reason": "snapshot_error"})
            continue
        if entry is None:
            authority_gaps.append(
                {"record_id": rid,
                 "reason": "record_not_in_pinned_source_catalog"})
            continue
        declared_hash = (entry.get("evidence_text_sha256")
                         or entry.get("content_hash") or "")
        if isinstance(declared_hash, str) and declared_hash.strip() \
                and declared_hash.strip().lower() != snap.content_hash.lower():
            authority_gaps.append({"record_id": rid,
                                   "reason": "pinned_snapshot_hash_mismatch"})
            continue
        declared_elig = entry.get("evidence_eligibility")
        if not (isinstance(declared_elig, str) and declared_elig.strip()):
            authority_gaps.append({"record_id": rid,
                                   "reason": "pinned_eligibility_missing"})
            continue
        if declared_elig.strip() != snap.evidence_eligibility:
            authority_gaps.append({"record_id": rid,
                                   "reason": "pinned_eligibility_mismatch"})
            continue
        eligibility = declared_elig.strip()
        snapshot_index[rid] = {
            "record_id": rid,
            # identity from the PINNED catalog (Phase-02 contract)
            "source_snapshot_id": str(entry.get("source_snapshot_id")
                                      or snap.source_snapshot_id),
            "evidence_text": snap.raw_text,
            "evidence_text_sha256": snap.content_hash,
            "evidence_eligibility": eligibility,
        }
        evidence_metadata[rid] = {
            "evidence_eligibility": eligibility,
            "evidence_role": str(rec.get("evidence_role") or "unknown"),
            "source_type": rec.get("tp") or rec.get("source_type") or "unknown",
        }
    return records, records_by_id, snapshot_index, evidence_metadata, authority_gaps


def _phase03_provenance(records):
    """Real provenance groups for the Phase03 reserves (RT-033 wiring).

    Uses the Phase-02 reviewed clustering (provenance.cluster_provenance)
    over the REQUEST-PINNED records and remaps its legacy list-position
    keys to stable record_id keys (the Phase-03 stable-ID contract).
    """
    try:
        from provenance import cluster_provenance
        by_idx = cluster_provenance(records or [])
    except Exception:
        return {}
    out = {}
    for idx, info in (by_idx or {}).items():
        try:
            rec = (records or [])[int(idx)]
        except (ValueError, IndexError, TypeError):
            continue
        rid = rec.get("record_id")
        if isinstance(rid, str) and rid.strip():
            out[rid] = {
                "independent_group_id": info.get("independent_group_id", rid),
                "source_role": info.get("provenance_reason", ""),
            }
    return out


async def _run_phase03_context(query: str, exclude_ids: set | None = None,
                               access_scope: str = "public") -> dict:
    """Run the Phase03 retrieval->evidence-package pipeline for one chat
    request and return the phase03_pipeline contract dict.

    HIGH-RECALL POOL SOURCE (review blocker 2): the pipeline consumes RAW
    per-route RetrievalResults (_rt.run_routes at per-route fetch caps)
    captured BEFORE the legacy global FINAL_TOP_K=25 fusion truncation —
    never the already-truncated fused search_results. The legacy flag-off
    path keeps run_hybrid unchanged.

    Deterministic, request-pinned; the rendered context is the ONLY
    generation context (RT-039 allowlist). Errors bubble — no silent
    fallback to raw build_context dumps. Phase03AuthorityError is handled
    explicitly by the chat endpoint (explicit UNSUPPORTED, fail closed).
    """
    from phase03_pipeline import run_phase03_retrieval
    from retrieval.chunk_route import ChunkRetriever

    (records, records_by_id, snapshot_index, evidence_metadata,
     authority_gaps) = _phase03_runtime_inputs()
    pinned = _request_runtime_snapshot.get()

    chunk_retriever = None
    if Flags.CONTEXTUAL_CHUNKS_ENABLED:
        chunk_retriever = ChunkRetriever.from_snapshots(
            list(snapshot_index.values()))

    # RT-033 real provenance: Phase-02 reviewed clustering over the pinned
    # request records (stable record_id keyed)
    provenance_map = _phase03_provenance(records)

    def _get_record(record_id):
        return records_by_id.get(record_id)

    # Raw per-route candidates (rank-26+ survives HERE, before any fusion):
    route_results = await _rt.run_routes(
        query, snapshot=pinned, exclude_ids=exclude_ids,
        embed_fn=embedding_func, pipeline=_get_retrieval_pipeline())

    return await run_phase03_retrieval(
        query=query,
        route_results=route_results,
        mode=_PHASE03_MODE,
        records_by_id=records_by_id,
        snapshot_index=snapshot_index,
        chunk_retriever=chunk_retriever,
        get_record_fn=_get_record,
        evidence_metadata=evidence_metadata,
        authority_gaps=authority_gaps,
        provenance_map=provenance_map,
        access_scope=access_scope or "public",
    )


# ── Request Models ──
class ChatRequest(BaseModel):
    query: str
    conversation_id: str = ""
    history: list = []
    # Request access scope for the Phase03 evidence policy engine
    # (RT-034 POLICY_ACCESS_SCOPE); default public keeps v1 clients
    # byte-compatible.
    access_scope: str = "public"


# ── Phase 02 (RT-020): persistent SourceSnapshot store ──────────────────────
# Lazy singleton — snapshots are content-addressed and reused across requests
# (RT-012 stability), written under WORKING_DIR like the other runtime data.
_SOURCE_SNAPSHOT_STORE = None


def _get_source_snapshot_store():
    global _SOURCE_SNAPSHOT_STORE
    if _SOURCE_SNAPSHOT_STORE is None:
        from source_snapshot import SourceSnapshotStore
        _SOURCE_SNAPSHOT_STORE = SourceSnapshotStore(WORKING_DIR / "source_snapshots")
    return _SOURCE_SNAPSHOT_STORE


# ── Lifespan ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runtime_snapshot_manager
    runtime_mode = os.environ.get("TECH_DB_RUNTIME_MODE", "").strip().lower()
    if not runtime_mode:
        raise RuntimeError(
            "TECH_DB_RUNTIME_MODE must be explicitly configured as "
            "legacy_hybrid or manifest; implicit startup is forbidden"
        )
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
        try:
            yield
        finally:
            _runtime_snapshot_manager = None
        return
    if runtime_mode != "legacy_hybrid":
        raise RuntimeError(f"unsupported TECH_DB_RUNTIME_MODE: {runtime_mode}")
    _runtime_snapshot_manager = None
    print("[startup] Explicit legacy_hybrid runtime-v1 profile", flush=True)
    # RT-030: index loading + meta-lookup build delegated to retrieval.runtime
    print("[startup] Loading vector index...", flush=True)
    load_vector_index()
    print("[startup] Loading BM25 index...", flush=True)
    load_bm25_index()
    print("[startup] Loading knowledge graph...", flush=True)
    load_graph_index()
    # Build fast record_idx → meta lookup (avoids linear scan in graph_search)
    build_idx_meta_lookup()

    _records_cache[:] = [load_records()]
    print(f"[startup] Records loaded: {len(_records_cache[0])}", flush=True)
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
    snapshot = _request_runtime_snapshot.get()
    resources = snapshot.resources if snapshot is not None else {}
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "api_key_configured": bool(os.environ.get("ZAI_API_KEY") or ENV_FILE.is_file()),
        "runtime_mode": os.environ.get("TECH_DB_RUNTIME_MODE", "UNCONFIGURED"),
        "runtime_manifest_id": snapshot.manifest_id if snapshot else None,
        "vector_index_ready": resources.get("vector_index") is not None if snapshot else _vector_index is not None,
        "bm25_ready": resources.get("bm25_index") is not None if snapshot else _bm25_index is not None,
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

            if (_request_runtime_snapshot.get() is None and _vector_index is None):
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

            # ── Phase 03 (RT-030..039): typed EvidencePackage path ──
            # Review round 2 (blocker A, RT-031): with EVIDENCE_PACKAGE_ENABLED
            # ON, Phase03 runs BEFORE the legacy weak-query gate. The legacy
            # Top25 / is_relevant profile is a LEGACY decision surface — it
            # must never act as an authoritative pre-gate that terminates a
            # request the evidence pipeline could still answer (e.g. a valid
            # raw-route candidate fusing past legacy FINAL_TOP_K=25, or a
            # query the legacy relevance profile would reject). When Phase03
            # is active its no_evidence / policy / capacity outcomes ARE the
            # evidence decision; the legacy gate below only applies on the
            # legacy path. With the flag OFF this block is inert and the
            # legacy path stays byte-compatible with pre-Phase03 behavior
            # (no FINAL_TOP_K increase, no weakened legacy profile).
            context = None
            citations = []
            _phase03_active = False
            if Flags.EVIDENCE_PACKAGE_ENABLED:
                try:
                    _p03 = await _run_phase03_context(
                        query,
                        exclude_ids=exclude_ids if exclude_ids else None,
                        access_scope=req.access_scope)
                except Phase03AuthorityError as pae:
                    # Review blocker 7: trusted EvidencePackage mode REQUIRES
                    # a pinned source authority. Without it the request fails
                    # closed — explicit UNSUPPORTED, never fabricated
                    # snapshot ids / ad-hoc text hashes.
                    print(f"[phase03] authority fail-closed: {pae}", flush=True)
                    trace.add_stage("phase03_authority_fail_closed",
                                    {"error": str(pae)[:200]})
                    trace.set_result(
                        answer_status="UNSUPPORTED",
                        stop_reason="phase03_missing_pinned_authority")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "证据模式需要固定的发布清单权威（manifest 运行时），"
                                  "当前环境缺少 pinned source authority，已按规范拒绝回答。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "phase03_missing_pinned_authority",
                        "boundary_message": "evidence authority fail-closed",
                        "trace_id": trace.trace_id,
                    })}
                    return
                except Exception as e:
                    print(f"[phase03] pipeline error: {e}", flush=True)
                    trace.add_stage("phase03_error", {"error": str(e)[:200]})
                    raise
                trace.add_stage("phase03_retrieval", _p03["trace_facts"])
                if _p03["status"] == "no_evidence":
                    trace.set_result(answer_status="UNSUPPORTED",
                                     stop_reason="phase03_no_evidence")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "数据库中没有足够的、满足证据标准的资料来回答这个问题。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "phase03_no_evidence",
                        "trace_id": trace.trace_id,
                    })}
                    return
                if _p03["status"] == "context_capacity_exceeded":
                    trace.set_result(answer_status="UNSUPPORTED",
                                     stop_reason="phase03_context_capacity_exceeded")
                    trace.flush()
                    yield {"event": "done", "data": json.dumps({
                        "answer": "证据包超出上下文容量且强制证据无法压缩，按规范 abstain："
                                  "请缩小问题范围后重试。",
                        "citations": [],
                        "cited_record_ids": [],
                        "searched_record_ids": [],
                        "answer_status": "UNSUPPORTED",
                        "stop_reason": "phase03_context_capacity_exceeded",
                        "trace_id": trace.trace_id,
                    })}
                    return
                # When Phase03 is active the generation context is the
                # allowlisted GeneratorInput rendering of the typed Evidence
                # Package (pool -> reserves -> rerank -> policy -> selection
                # -> package -> capacity fit) — raw build_context dumps are
                # forbidden generation context on this path. Failure must be
                # loud (bubble to the SSE error handler), never a silent
                # fallback to the legacy dump.
                context = _p03["context"]
                citations = _p03["citations"]
                if _p03.get("selected_record_ids"):
                    # honest searched-surface under evidence mode: the
                    # searched ids are the evidence pool's selected records
                    searched_record_ids = list(_p03["selected_record_ids"])
                _phase03_active = True

            if not _phase03_active and (not search_results or not is_relevant):
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

            if context is None:
                # Legacy path (flag off / phase03 inactive): raw context
                # build, unchanged.
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

            # Yield citations — LEGACY PATH ONLY (RT-027: the Phase-02 path
            # buffers citations until exact grounding + verification finalize,
            # then emits only the verified ones)
            if citations and not Flags.TERMINAL_RENDERER_ENABLED:
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
                        if not Flags.TERMINAL_RENDERER_ENABLED:
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
                            if not Flags.TERMINAL_RENDERER_ENABLED:
                                yield {"event": "token", "data": json.dumps({"text": answer[i:i+3]})}
                            await asyncio.sleep(0.015)
                except Exception as e2:
                    yield {"event": "error", "data": json.dumps({
                        "message": f"生成失败: {e2}"
                    })}
                    return

            # ══════════════════════════════════════════════════════════════
            # Phase 02 — RT-020..028 verification pipeline (terminal renderer)
            # ══════════════════════════════════════════════════════════════
            # Draft was buffered (no factual tokens streamed yet). The full
            # pipeline — claim mapping → coverage gate → exact grounding →
            # relation checks → numeric checks → bounded repair → fail-safe
            # verifier → AnswerStateMachine → terminal renderer — runs in
            # phase02_pipeline.run_phase02_verification, then and only then
            # is verified content streamed.
            if Flags.TERMINAL_RENDERER_ENABLED:
                yield {"event": "status", "data": json.dumps({
                    "step": "verifying",
                    "message": "回答已生成，正在核验引用证据与声明支持..."
                })}
                _p02_t0 = _time.perf_counter()
                try:
                    from feature_flags import active_profile
                    _p02_profile = active_profile() or ""
                except Exception:
                    _p02_profile = ""
                _p02_snap = _request_runtime_snapshot.get()
                _p02_store = None
                try:
                    _p02_store = _get_source_snapshot_store()
                except Exception:
                    _p02_store = None
                # Request-pinned runtime resources (RT-017 / Phase-02 review):
                # in manifest mode the pipeline MUST see the records pinned at
                # request start — never the mutable server-global _records —
                # so a mid-request release switch cannot change the evidence.
                _p02_records = (_runtime_resource("records", _records)
                                if _p02_snap is not None else _records)
                _p02_by_id = (_runtime_resource("records_by_id", None)
                              if _p02_snap is not None else None)
                _p02_rid_map = (_runtime_resource("record_id_map", None)
                                if _p02_snap is not None else None)
                # Request-pinned snapshot AUTHORITY (Phase-02 review): in
                # manifest mode resources["source_catalog"] is the ONLY
                # snapshot authority for grounding/refs/numeric provenance;
                # the WORKING_DIR SourceSnapshotStore must NOT be consulted
                # (it can reflect a newer generation than the pinned one).
                if _p02_snap is not None:
                    _p02_catalog = _runtime_resource("source_catalog", None)
                    _p02_store = None
                else:
                    _p02_catalog = None

                # RT-026 full wiring: server-injected closures.
                async def _p02_retrieve(claim_text: str):
                    """Targeted re-retrieval for an unsupported claim, run
                    through the SAME request-pinned retrieval pipeline."""
                    if not (claim_text or "").strip():
                        return []
                    try:
                        results, _rel = await _search_with_quality(claim_text, None)
                    except Exception:
                        return []
                    out = []
                    for r in (results or [])[:5]:
                        meta = r.get("meta") or {}
                        rid = r.get("record_id") or meta.get("record_id")
                        if not isinstance(rid, str) or not rid.strip():
                            continue  # no stable id → never fabricate evidence
                        rec = None
                        if _p02_by_id is not None:
                            rec = _p02_by_id.get(rid)
                        excerpt = (r.get("excerpt") or meta.get("as")
                                   or str(rec.get("fb") or rec.get("b") or "")
                                   if rec else "") or ""
                        out.append({
                            "record_id": rid,
                            "legacy_idx": r.get("legacy_idx", meta.get("legacy_idx")),
                            "excerpt": str(excerpt)[:200],
                            "source": str((rec or meta).get("a", "")),
                            "title": str((rec or meta).get("t", "")),
                        })
                    return out

                async def _p02_regenerate(current_answer: str, drop_ids=None,
                                          evidence_package=None):
                    """RT-026 evidence-scoped regeneration: the LLM sees ONLY
                    the allowlisted Evidence-Package-compatible input built
                    by the pipeline (question/scope, VALID exact EvidenceRefs
                    with stable record_id + source_snapshot_id + locators +
                    exact_text, verified support relations, deterministic
                    numeric results, keep/drop/core-gap instructions). Raw
                    retrieval dumps, synthetic summaries, ungrounded text
                    and generator reasoning are structurally absent. Failure
                    → None (repair keeps its deterministic answer)."""
                    try:
                        from phase02_pipeline import render_repair_evidence_input
                        rendered = render_repair_evidence_input(evidence_package)
                        return await llm_model_func(
                            "根据以下可支持证据重写回答。只能使用证据包中的精确引用"
                            "与确定性检查结果作为事实依据，不得引入证据之外的新事实，"
                            "删除标记为缺乏支持的要点。\n\n" + rendered,
                            system_prompt="你是严谨的技术问答助手，只输出重写后的回答。",
                        )
                    except Exception:
                        return None

                p02 = await run_phase02_verification(
                    query=query,
                    draft_answer=full_answer,
                    citations=citations,
                    records=_p02_records,
                    records_by_id=_p02_by_id,
                    record_id_map=_p02_rid_map,
                    trace=trace,
                    budget_reserve=lambda: BUDGET_FUSE.reserve(bypass=bypass),
                    retrieve_fn=_p02_retrieve,
                    regenerate_fn=_p02_regenerate,
                    active_profile=_p02_profile,
                    runtime_manifest_id=_p02_snap.manifest_id if _p02_snap else "",
                    source_snapshot_store=_p02_store,
                    source_catalog=_p02_catalog,
                    manifest_mode=_p02_snap is not None,
                )
                final_answer = p02["answer"]

                # Verified citations only (RT-020/027): INVALID-grounded
                # citations were dropped inside the pipeline and never
                # reach the client.
                if p02["citations"]:
                    yield {"event": "citations", "data": json.dumps({
                        "citations": p02["citations"],
                        "citation_schema_version": CITATION_SCHEMA_VERSION,
                    })}

                # Stream the FINAL rendered answer — terminal content only,
                # already filtered by the state machine + renderer.
                _p02_ttfs = _time.perf_counter()
                for i in range(0, max(len(final_answer), 1), 3):
                    chunk = final_answer[i:i + 3]
                    if chunk:
                        yield {"event": "token", "data": json.dumps({"text": chunk})}
                        await asyncio.sleep(0.015)
                _p02_ttfa = _time.perf_counter()
                trace.add_stage("sse_timing", {
                    "buffered_generation": True,
                    "ttfs_ms": round((_p02_ttfs - _p02_t0) * 1000, 1),
                    "ttfa_ms": round((_p02_ttfa - _p02_t0) * 1000, 1),
                    "verify_pipeline_ms": p02["diagnostics"]["pipeline_ms"],
                    "renderer": "terminal_v2",
                })
                trace.set_result(
                    answer=final_answer[:500],
                    answer_status=p02["answer_status"],
                    stop_reason=p02["stop_reason"],
                    citations=p02["citations"],
                    cited_record_ids=p02["cited_record_ids"],
                    verification_status=p02["verification_status"],
                )
                yield {"event": "done", "data": json.dumps({
                    "answer": final_answer,
                    "citations": p02["citations"],
                    "citation_schema_version": CITATION_SCHEMA_VERSION,
                    "claims": p02["claims_payload"],
                    "cited_record_ids": p02["cited_record_ids"],
                    "searched_record_ids": searched_record_ids,
                    "answer_status": p02["answer_status"],
                    "stop_reason": p02["stop_reason"],
                    "verification_status": p02["verification_status"],
                    "boundary_message": p02["boundary_message"],
                    "user_warning": p02["user_warning"],
                    "evidence_summary": p02["evidence_summary"],
                    "support_relations": {
                        str(c.get("id")): c.get("supports_claim_ids", [])
                        for c in p02["citations"]},
                    "degraded_capabilities": p02["degraded_capabilities"],
                    "numeric_facts": p02["numeric_facts"],
                    "diagnostics": p02["diagnostics"],
                    "trace_id": trace.trace_id,
                })}
                trace.flush()
            else:
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
                                # Phase 02 (RT-025, final spec §26): the verifier
                                # returns structured findings ONLY — it never
                                # authors/rewrites the final answer. The legacy
                                # "replace" event is retired; answer surgery is
                                # owned by the RT-026 bounded repair loop on the
                                # Phase-02 path.
                                verification_issues = vr.issues
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
                    # Review round 2 (blocker A, RT-031): when the Phase03
                    # evidence pipeline is active, the legacy is_relevant /
                    # Top25 profile must not act as an authoritative verdict
                    # on evidence sufficiency — the Phase03 no_evidence /
                    # policy / capacity outcomes (returned earlier) ARE the
                    # evidence decision. Legacy relevance only informs the
                    # status on the legacy path.
                    status_enum, stop_reason = determine_answer_status(
                        has_results=bool(search_results) or _phase03_active,
                        is_relevant=is_relevant or _phase03_active,
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
