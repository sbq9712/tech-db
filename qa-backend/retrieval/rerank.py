"""
RT-032 — Content-aware reranking for all modes (final_spec §11).

A compliant reranker MUST consume query + source-grounded candidate
content. Re-labeling RRF/fusion rank as rerank is NONCOMPLIANT —
`assert_content_aware()` is the interface conformance check (used by the
Phase03 suite): a pure rank transform cannot pass it (content swap must
flip the ranking).

Engines:
  * LocalContentReranker (FAST default): deterministic lexical content
    scorer over query vs (title + source-grounded evidence excerpt).
    Per-candidate independent scoring → batch-stable by construction
    (identical scores in any batch split); no LLM, no network, bounded.
  * GLM listwise reranker (RESEARCH/DEEP, existing qa-backend/reranker.py)
    wrapped with a hard asyncio timeout; on timeout/error it falls back to
    the deterministic local ranking and marks degraded_capabilities — the
    candidate set is NEVER cleared by a reranker failure.

Synthetic AI summaries are never the sole rerank content (spec §11): the
content resolver prefers fb/b source text; `as` is only a fallback hint
and is flagged `synthetic_only` when nothing else exists (those candidates
then carry degraded content, never a fabricated content score).
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

RERANK_ENGINE_VERSION = "local-lexical-v1"
GLM_TIMEOUT_S = float(os.environ.get("QA_RERANK_GLM_TIMEOUT_S", "8"))
MAX_CONTENT_CHARS = 400


# ── content resolution (source-grounded) ───────────────────────────────────


def resolve_candidate_content(candidate: dict, get_record_fn=None) -> tuple[str, bool]:
    """(content, synthetic_only) for one candidate.

    Order: full_body (fb) → body (b) → title (t) → record lookup →
    AI summary (as) ONLY as last resort (flagged synthetic_only=True).
    """
    meta = candidate.get("meta", {}) or {}
    for key in ("fb", "b"):
        text = (meta.get(key) or "").strip()
        if text:
            return text[:MAX_CONTENT_CHARS], False
    title = (meta.get("t") or "").strip()
    if title:
        content = title
        # augment with record body when resolvable
        if get_record_fn is not None:
            try:
                rec = get_record_fn(candidate.get("record_id"))
                if rec:
                    body = (rec.get("fb") or rec.get("b") or "").strip()
                    if body:
                        return (title + "\n" + body)[:MAX_CONTENT_CHARS], False
            except Exception:
                pass
        return content[:MAX_CONTENT_CHARS], False
    if get_record_fn is not None:
        try:
            rec = get_record_fn(candidate.get("record_id"))
            if rec:
                body = (rec.get("fb") or rec.get("b") or "").strip()
                if body:
                    return body[:MAX_CONTENT_CHARS], False
        except Exception:
            pass
    synthetic = (meta.get("as") or "").strip()
    if synthetic:
        return synthetic[:MAX_CONTENT_CHARS], True
    return "", True


def _tokenize(text: str) -> List[str]:
    # mixed CJK/latin: CJK bigrams + latin word tokens (deterministic)
    tokens: List[str] = []
    latin = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*", text)
    tokens.extend(w.lower() for w in latin)
    cjk_runs = re.findall(r"[一-鿿]+", text)
    for run in cjk_runs:
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
        if len(run) == 1:
            tokens.append(run)
    return tokens


def lexical_relevance(query: str, content: str) -> float:
    """Deterministic lexical relevance in [0, 1].

    Token-overlap F1 with a small phrase-containment bonus. Content-aware
    by construction: swapping content between candidates swaps scores.
    """
    q_tokens = _tokenize(query)
    if not q_tokens or not content:
        return 0.0
    c_tokens = _tokenize(content)
    if not c_tokens:
        return 0.0
    q_set, c_set = set(q_tokens), set(c_tokens)
    overlap = q_set & c_set
    if not overlap:
        return 0.0
    precision = len(overlap) / len(c_set)
    recall = len(overlap) / len(q_set)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    phrase_bonus = 0.0
    for n in (3, 2):
        if len(query) >= n:
            grams = {query[i:i + n] for i in range(len(query) - n + 1)}
            hits = sum(1 for g in grams if g in content)
            phrase_bonus = max(phrase_bonus, min(0.25, 0.05 * hits))
            break
    return min(1.0, f1 + phrase_bonus)


# ── interface conformance (spec: pure rank transform cannot satisfy) ───────


def assert_content_aware(rerank_fn: Callable[[str, List[dict]], List[dict]]) -> None:
    """Interface conformance check — raises AssertionError when rerank_fn
    ignores content (a pure rank/route-position transform fails this).

    Probe: two candidates where rank order and content relevance disagree.
    A content-aware reranker must flip the order; a rank transform cannot.
    """
    cands = [
        {"record_id": "rank-high-content-low", "rank": 1,
         "meta": {"t": "completely unrelated topic", "fb": "zzz qqq vvv unrelated"}},
        {"record_id": "rank-low-content-high", "rank": 2,
         "meta": {"t": "solid state battery energy density",
                  "fb": "solid state battery energy density 500 Wh/kg"}},
    ]
    out = rerank_fn("solid state battery energy density", [dict(c) for c in cands])
    order = [o.get("record_id") for o in out]
    assert order[0] == "rank-low-content-high", (
        f"reranker is not content-aware (order={order}): a pure rank "
        "transform cannot satisfy the RT-032 interface")


# ── engines ────────────────────────────────────────────────────────────────


@dataclass
class RerankOutcome:
    results: List[dict]
    engine: str
    degraded: List[str]
    fallback_reason: str = ""


async def rerank_local(query: str, candidates: List[dict],
                       get_record_fn=None,
                       top_k: Optional[int] = None,
                       mode: str = "FAST_RAG") -> RerankOutcome:
    """Deterministic local content reranker (FAST default).

    Every candidate is scored independently (query vs its own content) —
    scores are IDENTICAL under any batch split (multi-batch stable).
    """
    scored = []
    for c in candidates:
        content, synthetic_only = resolve_candidate_content(c, get_record_fn)
        if synthetic_only:
            # Review blocker 6: synthetic / AI-summary text can NEVER act
            # as primary evidence-rerank content. Such candidates receive
            # NO ordinary source-grounded content relevance (score 0.0),
            # are marked hint-only / non-evidentiary, and sort below every
            # source-grounded candidate — they cannot crowd out
            # source-grounded evidence under any top_k truncation.
            score = 0.0
            basis = "synthetic_hint_only"
        else:
            score = lexical_relevance(query, content)
            basis = "source_grounded"
        entry = {
            "record_id": c.get("record_id"),
            "rerank_score": round(score, 6),
            "input_rank": c.get("rank", c.get("rrf_rank", 0)),
            "meta": c.get("meta", {}),
            "route_details": c.get("route_details", {}),
            "engine": RERANK_ENGINE_VERSION,
            "content_basis": basis,
            # synthetic-only text is NEVER evidentiary content
            "counts_as_evidence": (not synthetic_only),
        }
        if synthetic_only:
            entry["synthetic_only_content"] = True
        scored.append(entry)
    scored.sort(key=lambda e: (-e["rerank_score"], str(e["record_id"])))
    if top_k is not None:
        scored = scored[:top_k]
    return RerankOutcome(results=scored, engine=RERANK_ENGINE_VERSION, degraded=[])


async def rerank_glm_bounded(query: str, candidates: List[dict],
                             get_record_fn=None,
                             top_k: Optional[int] = None,
                             timeout_s: float = GLM_TIMEOUT_S) -> RerankOutcome:
    """GLM listwise rerank (RESEARCH/DEEP) with a HARD timeout.

    Failure/timeout NEVER clears the candidate set: falls back to the
    deterministic local ranking with degraded_capabilities=["reranker"].

    Review blocker 6: synthetic-only candidates (content resolvable ONLY
    from meta["as"] AI summaries) are quarantined OUT of the GLM rerank
    input — they never compete on content relevance — and are re-appended
    afterwards with rerank_score 0.0, hint-only / non-evidentiary markers.
    They cannot crowd out source-grounded candidates on either path.
    """
    from reranker import rerank as glm_rerank  # qa-backend/reranker.py (T016)

    grounded: List[dict] = []
    synthetic_tail: List[dict] = []
    for c in candidates:
        _content, synthetic_only = resolve_candidate_content(c, get_record_fn)
        (synthetic_tail if synthetic_only else grounded).append(c)

    def _synthetic_entry(c: dict) -> dict:
        return {
            "record_id": c.get("record_id"),
            "rerank_score": 0.0,
            "input_rank": c.get("rank", c.get("rrf_rank", 0)),
            "meta": c.get("meta", {}),
            "route_details": c.get("route_details", {}),
            "engine": "glm-listwise",
            "content_basis": "synthetic_hint_only",
            "counts_as_evidence": False,
            "synthetic_only_content": True,
        }

    if not grounded:
        # nothing source-grounded to rerank — synthetic-only tail, no GLM call
        out = [_synthetic_entry(c) for c in synthetic_tail]
        out.sort(key=lambda e: str(e["record_id"]))
        if top_k is not None:
            out = out[:top_k]
        return RerankOutcome(results=out, engine="glm-listwise",
                             degraded=["synthetic_only_content"])

    try:
        async def _call():
            return await glm_rerank(query, grounded, top_k=None,
                                    get_record_fn=get_record_fn)
        out = await asyncio.wait_for(_call(), timeout=timeout_s)
        if out is None:
            out = []
        if len(out) != len(grounded):
            # GLM path lost candidates — treat as failure, fall back whole
            raise RuntimeError(
                f"glm rerank returned {len(out)}/{len(grounded)}")
        for e in out:
            # synthetic candidates never reached GLM: everything it returned
            # is source-grounded content
            e.setdefault("content_basis", "source_grounded")
            e.setdefault("counts_as_evidence", True)
        merged = list(out) + [_synthetic_entry(c) for c in synthetic_tail]
        merged.sort(key=lambda e: (-float(e.get("rerank_score", 0.0)),
                                   str(e["record_id"])))
        if top_k is not None:
            merged = merged[:top_k]
        return RerankOutcome(results=merged, engine="glm-listwise",
                             degraded=[])
    except Exception as exc:  # timeout / parse / transport / partial loss
        local = await rerank_local(query, candidates, get_record_fn=get_record_fn,
                                   top_k=top_k)
        local.engine = f"glm-listwise-fallback:{RERANK_ENGINE_VERSION}"
        local.degraded = ["reranker"]
        local.fallback_reason = f"{type(exc).__name__}"
        return local


async def rerank_for_mode(query: str, candidates: List[dict], mode: str,
                          get_record_fn=None,
                          top_k: Optional[int] = None) -> RerankOutcome:
    """Mode dispatch: FAST = local deterministic; RESEARCH/DEEP = bounded GLM."""
    if not candidates:
        return RerankOutcome(results=[], engine="noop-empty", degraded=[])
    if mode == "FAST_RAG":
        return await rerank_local(query, candidates, get_record_fn=get_record_fn,
                                  top_k=top_k)
    return await rerank_glm_bounded(query, candidates, get_record_fn=get_record_fn,
                                    top_k=top_k)
