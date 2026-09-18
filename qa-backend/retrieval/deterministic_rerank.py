"""Deterministic deep-retrieval layer (RT101-V13 post-mortem, root cause C).

V13 formal failure (sanitized aggregate, 2026-09-18): flat retrieval — the
correct support records for multi-part research questions sat below the
legacy FINAL_TOP_K=25 fusion cut (RRF is deliberately flat: rank 1 ≈
1/61, rank 60 ≈ 1/119 — an order-of-magnitude-less spread than route
scores), so the served context never contained the supporting evidence
even though the corpus covered it.

This module adds a DETERMINISTIC, GENERALIZED repair with three parts:

  C1. Query normalization (:func:`normalize_retrieval_query`) — strip
      conversational boilerplate (请教/请问/帮我/根据资料/please/…)
      before embedding/BM25 so route queries concentrate content signal.
      Purely lexical (fixed pattern list), zero LLM, no holdout-derived
      constants; returns the ORIGINAL query when stripping would empty
      or over-shorten it (fail-safe).

  C2. Pool widening — callers opt into a larger pre-truncation candidate
      pool (run_hybrid(candidate_pool=RETRIEVAL_CANDIDATE_POOL)) so rank
      26+ fused candidates survive to be judged on content merit instead
      of being cut by the flat RRF order.

  C3. Deterministic rerank (:func:`apply_deterministic_rerank`) — a fixed
      weighted blend of route-score normalizations and lexical/constraint
      signals, then a STABLE sort (ties keep fused order). No LLM, no
      learned parameters, no randomness.

Activation (:func:`needs_deep_retrieval`) is a deterministic predicate on
QUERY SHAPE ONLY (filler present / multi-clause / quoted topical segments
/ dated-numeric constraints / long diluted queries) — the same long/multi-
part dilution class the existing weak-query admission repair (Class A)
was built for. Short focused keyword queries keep the frozen legacy
surface byte-identical (tests_parity.py gate-1 baselines stay green;
precision is protected — §23 of the V14 repair charter: no threshold is
lowered, no admission gate weakened; deep mode only re-ORDERS an enlarged
candidate pool before the SAME FINAL_TOP_K serving cut).
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

import jieba

# ── C1: deterministic conversational-boilerplate stripping ──────────────────
# Fixed, conservative pattern list. Longest-first so compound prefixes
# (请帮我 → 帮我) strip in one pass. Never touches content words.
_CJK_FILLER_PREFIXES = (
    "请帮我", "请帮忙", "麻烦你", "麻烦您", "谢谢你",
    "请问", "请查阅", "请查找", "请检索", "请介绍", "请回答",
    "请整理", "请说明", "请分析", "请总结", "请列出",
    "我想知道", "我想了解", "我想问问", "我想请教", "我想查询",
    "帮我看下", "帮我查下", "帮我看看", "帮我查查", "帮我查找",
    "帮我", "帮忙", "麻烦",
    "根据资料", "根据文档", "根据手册", "根据语料", "根据上述资料",
    "依据资料", "依据文档", "基于资料", "基于文档",
    "参考资料中", "资料库中", "资料库", "资料中",
    "你知道吗", "你知道", "告诉我", "回答一下", "说明一下",
    "介绍一下", "整理一下", "查一下", "查查", "查询",
)
_CJK_FILLER_PREFIXES = tuple(sorted(_CJK_FILLER_PREFIXES, key=len, reverse=True))
_CJK_FILLER_LEADINS = ("关于", "有关")  # safe topic lead-ins
_CJK_FILLER_SUFFIXES = ("是什么样的", "有哪些", "是什么", "怎么样", "如何", "哪些",
                        "什么", "吗", "呢")
_LEAD_PUNCT_RE = re.compile(r"^[，。、；：！？,.;:!?\s]+")
_TRAIL_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?\s]+$")


def _tidy(q: str) -> str:
    """Strip leading/trailing punctuation left behind by prefix/suffix cuts."""
    return _TRAIL_PUNCT_RE.sub("", _LEAD_PUNCT_RE.sub("", q))

_ASCII_FILLER_PREFIXES = (
    "could you please ", "could you ", "can you please ", "can you ",
    "please ", "tell me about ", "tell me ", "i want to know ",
    "i would like to know ", "i'd like to know ", "according to the docs ",
    "according to the corpus ", "according to the material ",
    "what is ", "what are ",
)
_ASCII_SUFFIXES = (" ?", "?", " ?", "?")

_MIN_NORMALIZED_LEN = 4  # fail-safe: never strip below this length


def normalize_retrieval_query(query: str) -> str:
    """Deterministically strip conversational boilerplate from a query.

    Returns the ORIGINAL query (whitespace-collapsed) when stripping would
    empty it or drop it below ``_MIN_NORMALIZED_LEN`` chars — fail-safe
    against over-stripping. Zero LLM; fixed pattern list only.
    """
    if not isinstance(query, str):
        return ""
    q = re.sub(r"\s+", " ", query).strip()
    if not q:
        return ""
    original = q
    for _ in range(6):  # bounded fixpoint
        changed = False
        low = q.lower()
        for p in _ASCII_FILLER_PREFIXES:
            if low.startswith(p) and len(q) - len(p) >= _MIN_NORMALIZED_LEN:
                q = q[len(p):].lstrip()
                changed = True
                break
        if changed:
            continue
        stripped = True
        while stripped:
            stripped = False
            for p in _CJK_FILLER_PREFIXES:
                if q.startswith(p) and len(q) - len(p) >= _MIN_NORMALIZED_LEN:
                    q = _tidy(q[len(p):])
                    stripped = True
                    break
            if not stripped:
                for lead in _CJK_FILLER_LEADINS:
                    if q.startswith(lead) and len(q) - len(lead) >= _MIN_NORMALIZED_LEN:
                        q = _tidy(q[len(lead):])
                        stripped = True
                        break
                if not stripped:
                    for s in _CJK_FILLER_SUFFIXES:
                        if q.endswith(s) and len(q) - len(s) >= _MIN_NORMALIZED_LEN:
                            q = _tidy(q[: len(q) - len(s)])
                            stripped = True
                            break
                    if not stripped:
                        for s in _ASCII_SUFFIXES:
                            if q.endswith(s) and len(q) - len(s) >= _MIN_NORMALIZED_LEN:
                                q = _tidy(q[: len(q) - len(s)])
                                stripped = True
                                break
        if q == original:
            break
        original = q
    return q.strip() or (re.sub(r"\s+", " ", query).strip())


# ── activation predicate (query SHAPE only — no content tuning) ─────────────
_QUOTED_RE = re.compile(r"「[^」]{1,60}」|『[^』]{1,60}』|\[[^\]]{1,60}\]")
_DATE_NUM_RE = re.compile(r"\d{4}(?:-\d{1,2}){0,2}")
_TOKEN_LEN_RE = re.compile(r"[一-鿿]|[a-zA-Z0-9]+")
_DEEP_TOKEN_MIN = 16          # CJK chars + latin words; baseline max is 13
_DEEP_MIN_CLAUSES = 2


def needs_deep_retrieval(query: str, *,
                         split_subqueries_fn: Optional[Callable] = None) -> bool:
    """Deterministic predicate: does this query need the deep-retrieval mode?

    True iff ANY of (all pure query-shape signals, zero content tuning):
      - conversational filler present (normalization changes the query);
      - quoted/bracketed topical segments present (「」/『』/[]);
      - explicit dates / dotted numerics present (constraint signals);
      - the deterministic clause splitter yields >= 2 substantive clauses;
      - token length (CJK chars + latin words) >= 16 (diluted embedding).
    Short focused keyword queries → False → the frozen legacy retrieval
    surface is preserved byte-identical (parity baselines stay green).
    """
    if not isinstance(query, str) or not query.strip():
        return False
    if normalize_retrieval_query(query) != re.sub(r"\s+", " ", query).strip():
        return True
    if _QUOTED_RE.search(query):
        return True
    if _DATE_NUM_RE.search(query):
        return True
    if split_subqueries_fn is None:
        try:
            from .runtime import split_subqueries as split_subqueries_fn  # lazy
        except Exception:  # standalone/self-test import context: shape-only
            split_subqueries_fn = lambda q: [q]  # noqa: E731
    try:
        if len(split_subqueries_fn(query)) >= _DEEP_MIN_CLAUSES:
            return True
    except Exception:
        pass
    if len(_TOKEN_LEN_RE.findall(query)) >= _DEEP_TOKEN_MIN:
        return True
    return False


# ── C3: deterministic pool scoring ──────────────────────────────────────────
# Fixed weights (documented; never tuned on any evaluation case).
# rrf = RRF-consensus prior: the fused rank IS orthogonal 3-route evidence,
# and keeping it in the blend prevents the rerank from DEMOTING pool-top
# consensus records when within-pool max-normalization is skewed by a few
# high-score hub rows (observed in the repair-C dev benchmark). The prior
# is COVERAGE-GATED (see _RRF_COV_FLOOR in det_rerank_score): zero-signal
# rows never receive it.
DET_WEIGHTS = {"vec": 0.35, "bm25": 0.15, "title": 0.15, "body": 0.05,
               "constraint": 0.10, "rrf": 0.20}
# Coverage gate for the RRF consensus prior: minimal IDF-weighted content
# signal (0.4*title_cov + 0.4*body_cov + 0.2*constraint_frac) a row must
# show for THIS query before its fused-position prior applies. Tiny by
# design (any distinctive-term hit or constraint match clears it).
_RRF_COV_FLOOR = 0.05
_BODY_SCAN_CAP = 4000  # chars; latency guard for body containment checks

_TERM_STRIP = "「」『』[]()（）\"'“”‘’ \t"


def extract_query_terms(query: str) -> list:
    """Ordered, de-duplicated content terms for lexical coverage scoring.

    Sources: the deterministic content-term regex (quoted segments, Latin
    runs, dates) + jieba cut_for_search tokens (the SAME tokenizer BM25
    uses, so coverage reads the same segmentation the route ranked on).
    Pure CJK single chars and pure punctuation are dropped (len>=2 rule).
    """
    if not isinstance(query, str) or not query.strip():
        return []
    toks = []
    try:
        from .runtime import _CONTENT_TERM_RE
    except ImportError:  # standalone/self-test import context
        _CONTENT_TERM_RE = re.compile(
            r"「[^」]{1,60}」|『[^』]{1,60}』|\[[^\]]{1,60}\]"
            r"|[A-Za-z][A-Za-z0-9\-]{2,}"
            r"|\d{4}(?:-\d{1,2}){0,2}")
    for m in _CONTENT_TERM_RE.finditer(query):
        t = m.group(0).strip(_TERM_STRIP).strip()
        if len(t) >= 2:
            toks.append(t)
    for t in jieba.cut_for_search(query):
        t = t.strip()
        if len(t) < 2:
            continue
        if not re.search(r"[一-鿿A-Za-z0-9]", t):
            continue
        toks.append(t)
    kept: list = []
    for t in toks:
        tl = t.lower()
        if any(tl == k.lower() or tl in k.lower() or k.lower() in tl
               for k in kept):
            continue
        kept.append(t)
    return kept


def extract_constraints(query: str) -> list:
    """Deterministic date/numeric constraint tokens (years, dotted dates,
    decimal numbers) — length>=2 only, order-preserving, de-duplicated."""
    if not isinstance(query, str):
        return []
    out: list = []
    for m in re.finditer(r"\d{4}(?:-\d{1,2}){0,2}|\d{2,3}(?:\.\d+)?", query):
        t = m.group(0)
        if t not in out:
            out.append(t)
    return out


def pool_term_idf(query_terms: list, pool_titles: list) -> dict:
    """Deterministic POOL-SCOPED IDF for query terms.

    df(t) = number of pool titles containing t; idf(t) = log(1 + N/(1+df)).
    Generic tokens (发展/情况/the/and — present in half the pool) collapse
    toward ~log(2); distinctive tokens keep full weight. Computed ONLY from
    the candidate pool itself — zero external constants, zero tuning data.
    """
    n = max(1, len(pool_titles))
    titles_l = [t.lower() for t in pool_titles]
    out = {}
    for t in query_terms:
        tl = t.lower()
        df = sum(1 for h in titles_l if tl in h)
        import math
        out[t] = math.log(1.0 + n / (1.0 + df))
    return out


def _coverage(query_terms: list, hay: str, idf: dict) -> float:
    """IDF-weighted, script-balanced term coverage of ``hay``.

    Per-script groups (CJK vs Latin) are scored separately then averaged —
    CJK glue words must not dilute matched Latin tokens (and vice versa)
    on a bilingual corpus — and within a group each term counts by its
    pool-scoped IDF so generic tokens cannot dominate coverage.
    """
    if not query_terms:
        return 0.0
    hay_l = hay.lower()
    groups = {}
    for t in query_terms:
        key = "cjk" if re.search(r"[一-鿿]", t) else "lat"
        groups.setdefault(key, []).append(t)
    fracs = []
    for ts in groups.values():
        num = sum(idf.get(t, 1.0) for t in ts if t.lower() in hay_l)
        den = sum(idf.get(t, 1.0) for t in ts)
        fracs.append(num / den if den > 0 else 0.0)
    return sum(fracs) / len(fracs)


def det_rerank_score(query_terms: list, constraints: list, row: dict,
                     pool_vec_max: float, pool_bm25_max: float,
                     pool_pos0: int = 0, pool_size: int = 1,
                     term_idf: dict | None = None) -> float:
    """Fixed-weight deterministic score for ONE pool row.

    Signals (§21 of the V14 charter): within-pool normalized vector score,
    within-pool normalized BM25 score, script-balanced query-term coverage
    of the title, coverage of the body, date/numeric constraint matches,
    and the RRF-consensus prior (position in the fused pool). Scale-free;
    deterministic given the pool; ties resolve to fused order via the
    caller's stable sort.
    """
    meta = row.get("meta") or {}
    title = str(meta.get("t") or "")
    body = str(meta.get("b") or "")[:_BODY_SCAN_CAP]
    date = str(meta.get("d") or "")

    w = DET_WEIGHTS
    s_vec = (float(row.get("vec_score", 0.0) or 0.0) / pool_vec_max
             if pool_vec_max > 0 else 0.0)
    s_bm = (float(row.get("bm25_score", 0.0) or 0.0) / pool_bm25_max
            if pool_bm25_max > 0 else 0.0)
    s_rrf = (pool_size - pool_pos0) / pool_size if pool_size > 0 else 0.0

    idf = term_idf or {}
    cov_t = _coverage(query_terms, title, idf)
    cov_b = _coverage(query_terms, body, idf)

    if constraints:
        hay_c = "\n".join((title, date, body)).lower()
        c_frac = sum(1 for c in constraints if c.lower() in hay_c) / len(constraints)
    else:
        c_frac = 0.0

    # Scoring balance (repair C follow-up): the RRF consensus prior
    # protects rows the three independent routes jointly surfaced — but
    # ONLY rows with at least minimal content signal for THIS query. A
    # zero-overlap fused-top row is exactly the generic-noise class the
    # flat RRF order is known to carry (V13 postmortem observation 3);
    # the full consensus prior would otherwise let it outrank genuine
    # content specialists deep in the pool. The floor is deliberately
    # tiny: any distinctive-term hit or constraint match keeps the prior;
    # generic-only overlap (single stop-word-class token) does not. Rows
    # below the floor fall back to their route scores alone and stay in
    # fused order among themselves (stable sort) — the opaque band never
    # outranks demonstrated content evidence.
    content_signal = 0.4 * cov_t + 0.4 * cov_b + 0.2 * c_frac
    if content_signal < _RRF_COV_FLOOR:
        s_rrf = 0.0

    return (w["vec"] * s_vec + w["bm25"] * s_bm + w["title"] * cov_t
            + w["body"] * cov_b + w["constraint"] * c_frac + w["rrf"] * s_rrf)


def apply_deterministic_rerank(query: str, results: list) -> list:
    """Re-score + stable-sort a fused candidate pool (C3).

    Contract: same row dicts (annotated additively with ``det_rerank_score``
    and ``det_rerank_rank``), ordered by descending deterministic score;
    exact ties keep fused order (stable sort). Never raises for malformed
    rows (they sort by route scores only). Caller still truncates to the
    SAME serving cut (FINAL_TOP_K).
    """
    if not isinstance(results, list) or len(results) < 2:
        return results
    terms = extract_query_terms(query)
    cons = extract_constraints(query)
    vec_max = max((float(r.get("vec_score", 0.0) or 0.0) for r in results
                   if isinstance(r, dict)), default=0.0)
    bm_max = max((float(r.get("bm25_score", 0.0) or 0.0) for r in results
                  if isinstance(r, dict)), default=0.0)
    n_pool = len(results)
    idf = pool_term_idf(terms, [str((r.get("meta") or {}).get("t") or "")
                                for r in results if isinstance(r, dict)])
    scored = []
    for pos, r in enumerate(results):
        if not isinstance(r, dict):
            scored.append((0.0, pos, r))
            continue
        try:
            s = det_rerank_score(terms, cons, r, vec_max, bm_max,
                                 pool_pos0=pos, pool_size=n_pool,
                                 term_idf=idf)
        except Exception:
            s = 0.0
        scored.append((s, pos, r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for rank, (s, fpos, r) in enumerate(scored, start=1):
        if isinstance(r, dict):
            r["det_rerank_score"] = round(s, 6)
            r["det_rerank_rank"] = rank
            r["det_pool_pos"] = fpos + 1  # 1-based fused position (anchor key)
        out.append(r)
    # Consensus anchor: re-admit fused-top-N rows the rerank evicted.
    # An anchor ALREADY in the window is never displaced by another anchor
    # (route consensus outranks single-view heuristics); re-admission only
    # displaces the det-worst NON-anchor window rows.
    try:
        def _is_anchor(r):
            fpos = r.get("det_pool_pos") if isinstance(r, dict) else None
            return isinstance(fpos, int) and fpos <= _ANCHOR_TOP_N

        window = out[:25]
        win_ids = {r.get("record_id") for r in window if isinstance(r, dict)}
        for r in out:
            if not _is_anchor(r):
                continue
            if r.get("record_id") in win_ids:
                continue
            slot = None
            for i in range(len(window) - 1, -1, -1):
                if isinstance(window[i], dict) and not _is_anchor(window[i]):
                    slot = i
                    break
            if slot is None:
                break  # window is all consensus rows; nothing to displace
            window[slot] = r
            win_ids.add(r.get("record_id"))
        out[:25] = window
    except Exception:
        pass  # fail-safe: main ranking stands
    # multi-part asks: guarantee per-clause evidence presence (deterministic)
    try:
        out = _clause_rescue(query, out, extract_query_terms,
                             pool_term_idf, _coverage)
    except Exception:
        pass  # fail-safe: main ranking stands
    return out


# ── per-clause coverage rescue (multi-part asks) ────────────────────────────
# The V13 failure class is precisely multi-part research questions: each
# sub-question needs ITS evidence in the served window, but a single
# clause's phrasing can dominate the blended score and crowd a sibling
# clause's specialist record out of the serving cut. For multi-clause
# queries, if some clause has a strong specialist in the pool that the
# served window under-represents, deterministically promote it into the
# tail of the served set. Bounded and fail-safe:
_MAX_CLAUSE_RESCUES = 2   # at most two promotions per request
_RESCUE_MIN_COV = 0.45    # specialist must cover its clause well
_RESCUE_MARGIN = 2.0      # and far exceed the served window's best coverage
# Consensus anchor: fused-pool top-N rows are 3-route RRF consensus; a
# rerank that only sees title-lexical/route-score proxies must not evict
# them from the serving window (evicted anchors are re-admitted at the
# tail, displacing the det-worst rows - bounded, deterministic).
_ANCHOR_TOP_N = 5


def _clause_rescue(query: str, scored: list, terms_fn, idf_fn, cov_fn) -> list:
    """Deterministic per-clause promotion into the serving tail.

    scored: pool rows already ordered by the main deterministic score.
    Returns the SAME list object semantics: a new list where up to
    ``_MAX_CLAUSE_RESCUES`` tail rows are replaced by clause specialists.
    """
    try:
        from .runtime import split_subqueries
    except ImportError:  # standalone context
        split_subqueries = None
    if split_subqueries is None:
        return scored
    try:
        clauses = [c for c in split_subqueries(query)
                   if isinstance(c, str) and len(c.strip()) >= 4]
    except Exception:
        return scored
    if len(clauses) < 2:
        return scored

    served = list(scored)
    n_serve = 25  # serving cut the caller applies; rescue within it
    window = served[:n_serve]
    pool_ids = set()
    for r in scored:
        if isinstance(r, dict):
            pool_ids.add(r.get("record_id"))
    rescued = 0
    for clause in clauses[:6]:
        if rescued >= _MAX_CLAUSE_RESCUES:
            break
        try:
            c_terms = terms_fn(clause)
        except Exception:
            continue
        if not c_terms:
            continue
        titles = [str((r.get("meta") or {}).get("t") or "")
                  for r in scored if isinstance(r, dict)]
        try:
            c_idf = idf_fn(c_terms, titles)
        except Exception:
            continue

        def cov_of(r, _ct=c_terms, _ci=c_idf):
            try:
                return cov_fn(_ct, str((r.get("meta") or {}).get("t") or ""), _ci)
            except Exception:
                return 0.0

        served_cov = 0.0
        for r in window:
            if isinstance(r, dict) and cov_of(r) > served_cov:
                served_cov = cov_of(r)
        if served_cov >= _RESCUE_MIN_COV:
            continue  # clause already represented in the window
        best, best_cov, best_pos = None, 0.0, -1
        for pos, r in enumerate(scored):
            if not isinstance(r, dict):
                continue
            c_ = cov_of(r)
            if c_ > best_cov:
                best, best_cov, best_pos = r, c_, pos
        if best is None or best_cov < _RESCUE_MIN_COV                 or best_cov < served_cov * _RESCUE_MARGIN:
            continue
        if best.get("record_id") in {r.get("record_id") for r in window}:
            continue
        # promote the specialist into the serving tail (displace last)
        window[-1] = best
        rescued += 1
    if rescued:
        served[:n_serve] = window
    return served


# ── C2 pool size (serving cut stays FINAL_TOP_K) ────────────────────────────
RETRIEVAL_CANDIDATE_POOL = 80  # pre-truncation pool; FINAL_TOP_K unchanged


if __name__ == "__main__":  # smoke self-test (synthetic only)
    Q = ("\u6839\u636e\u8d44\u6599\uff0c\u8bf7\u8bf4\u660e2024\u5e74"
         "\u9499\u94db\u77ff\u53e0\u5c42\u7535\u6c60\u7684\u6548\u7387"
         "\u7eaa\u5f55\u4ee5\u53ca\u7a33\u5b9a\u6027\u8fdb\u5c55")  # 根据…请说明…进展
    T0 = "\u56fa\u6001\u7535\u6c60\u4ea7\u4e1a\u5316\u8fdb\u5c55\u89c2\u5bdf"          # 固态电池产业化进展观察
    T1 = "\u9499\u94db\u77ff\u7845\u53e0\u5c42\u7535\u6c60\u6548\u7387\u521b\u65b0\u9ad8"  # 钙钛矿硅叠层电池效率创新高
    B1 = ("2024\u5e74\u9499\u94db\u77ff-\u7845\u53e0\u5c42\u7535\u6c60\u8f6c\u6362"
          "\u6548\u7387\u8fbe33.9%\uff0c\u521b\u65b0\u7eaa\u5f55")                    # 2024年…33.9%…纪录
    T2 = "\u9502\u77ff\u5f00\u91c7\u4e0e\u7535\u6c60\u56de\u6536"                     # 锂矿开采与电池回收
    T3 = "\u5149\u4f0f\u4ea7\u4e1a\u94fe\u4ef7\u683c\u8ffd\u8e2a"                     # 光伏产业链价格追踪
    T4 = "\u9499\u94db\u77ff\u7a33\u5b9a\u6027\u7814\u7a76"                           # 钙钛矿稳定性研究
    B4 = "\u7a33\u5b9a\u6027\u662f\u9499\u94db\u77ff\u5546\u4e1a\u5316\u7684\u5173\u952e"  # 稳定性是…关键
    K1 = "\u9499\u94db\u77ff\u592a\u9633\u80fd\u7535\u6c60"                            # 钙钛矿太阳能电池
    K2 = "lithium battery energy density"

    rows = [
        {"record_id": f"u-{i}", "meta": {"t": t, "b": b, "d": "2024-01-01"},
         "score": 1.0 / (i + 61), "vec_score": v, "bm25_score": bm}
        for i, (t, b, v, bm) in enumerate([
            (T0, "\u4e0e\u9499\u94db\u77ff\u65e0\u5173\u7684\u6982\u8ff0", 0.30, 0.0),
            (T1, B1, 0.28, 4.2),
            (T2, "\u56de\u6536\u4f53\u7cfb\u6982\u8ff0", 0.25, 0.8),
            (T3, "\u591a\u6676\u7845\u4ef7\u683c\u6ce2\u52a8", 0.22, 0.0),
            (T4, B4, 0.20, 2.5),
        ] * 12)
    ][:60]
    assert needs_deep_retrieval(Q), "multi-clause dated query must activate"
    assert not needs_deep_retrieval(K1), "short keyword must not activate"
    assert not needs_deep_retrieval(K2), "short latin keyword must not activate"
    nq = normalize_retrieval_query(Q)
    assert "\u8bf7" not in nq and "\u6839\u636e" not in nq and "\u9499\u94db\u77ff" in nq, repr(nq)
    out = apply_deterministic_rerank(Q, rows)
    assert out[0]["meta"]["t"] == T1, repr(out[0]["meta"]["t"])
    assert all("det_rerank_score" in r for r in out)
    out2 = apply_deterministic_rerank(Q, [dict(r) for r in rows])
    assert [r["record_id"] for r in out] == [r["record_id"] for r in out2]
    print("deterministic_rerank self-test PASS")
