"""
RT-030 — Retrieval runtime: index resources + core route algorithms.

Everything here moved OUT of server.py (TK-05/T014 wrapper migration was
phase 1; this finishes it): the vector/BM25/graph index loading, the graph
hop-0/hop-1 entity-expansion algorithm, the BM25 query tokenizer and the
legacy hybrid pipeline assembly. server.py keeps API glue only (request
orchestration / profile dispatch / SSE serialization).

Byte-compatibility contract:
  * legacy hybrid_search RRF output semantics (rank base, per-route score
    keys, FINAL_TOP_K truncation of the *legacy* surface) are preserved —
    tests_parity frozen gate-1 baselines and tests_shadow drift-watch keep
    guarding them from the new module.
  * graph hop-0/hop-1 constants (MAX_HOP1_DEGREE, HOP1_WEIGHT,
    MAX_HOP1_ENTITIES) keep their reviewed values.

RuntimeSnapshot remains the unified resource entry point: run_hybrid() first
resolves the request-pinned snapshot resources (manifest mode) and only
falls back to process-global lazily-loaded indexes in legacy_hybrid mode.
"""
from __future__ import annotations

import asyncio
import json
import os
import pickle
from pathlib import Path
from typing import Optional

import numpy as np


class RouteResults(dict):
    """Backward-compatible route mapping with typed degradation metadata."""
    def __init__(self, *args, degraded_capabilities=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.degraded_capabilities = list(degraded_capabilities or [])

REPO = Path(__file__).resolve().parent.parent.parent
WORKING_DIR = None  # resolved lazily from config (avoids import-order cycles)
INDEX_FILE = None
BM25_FILE = None
JIEBA_DICT = None

# ── RRF parameters (moved verbatim from server.py; frozen semantics) ──
RRF_K = 60              # RRF constant (1/(rank+k))
RETRIEVAL_TOP_K = 50    # candidates per route
FINAL_TOP_K = 25        # max records after fusion (LEGACY surface only)
RELEVANCE_FLOOR = 0.3   # vector similarity floor for "honest answer" trigger

# ── Quality gates (moved verbatim) ──
VEC_STRONG = 0.55
BM25_STRONG = 5.0
GRAPH_STRONG = 5.0
MIN_STRONG_RESULTS = 3

# Graph hop-1 expansion limits (moved verbatim)
MAX_HOP1_DEGREE = 20    # skip super-node neighbors (hop=1)
HOP1_WEIGHT = 0.35      # hop=1 weight (hop=0 is 1.0)
MAX_HOP1_ENTITIES = 40  # cap hop=1 expansion

FETCH_K_CAP = 200

# ── Process-global legacy index state (legacy_hybrid mode only) ──
_vector_index = None
_index_meta = None
_bm25_index = None
_bm25_meta = None
_bm25_corpus = None
_graph_data = None
_entity_index = None      # entity_name -> set(record indices)
_graph_adj = None         # entity_name -> set(neighbor entity_names)
_graph_nodes = None       # entity_name -> node info (type, degree, description)
_idx_to_meta = None       # record_idx -> meta dict
_pipeline = None


def _paths():
    """Resolve WORKING_DIR-derived paths lazily (import order safety)."""
    global WORKING_DIR, INDEX_FILE, BM25_FILE, JIEBA_DICT
    if WORKING_DIR is None:
        from config import WORKING_DIR as _WD
        WORKING_DIR = _WD
        INDEX_FILE = WORKING_DIR / "vector_index_v2.pkl"
        BM25_FILE = WORKING_DIR / "bm25_index.pkl"
        JIEBA_DICT = WORKING_DIR / "jieba_custom_dict.txt"
    return WORKING_DIR


def bm25_tokenize(query: str) -> list:
    """Tokenize a query the same way the BM25 corpus was tokenimized."""
    import jieba
    return list(jieba.cut_for_search(query))


# ── Index loading (legacy_hybrid global state) ─────────────────────────────


def load_vector_index(vector_file=None, fallback_file=None):
    """Load pre-built vector index into process globals (legacy mode).

    Files resolve from explicit args first (server passes its live module
    paths so test-time patches stay effective), then config defaults.
    """
    global _vector_index, _index_meta
    if _vector_index is not None:
        return
    _paths()
    vf = Path(vector_file) if vector_file else INDEX_FILE
    fb = Path(fallback_file) if fallback_file else WORKING_DIR / "vector_index.pkl"
    idx_file = vf if vf.exists() else fb
    if idx_file.exists():
        print(f"[startup] Loading vector index {idx_file.name}...", flush=True)
        with open(idx_file, "rb") as f:
            data = pickle.load(f)
        _vector_index = data["embeddings"]
        _index_meta = data["meta"]
        print(f"[startup] Vector index loaded: {len(_index_meta)} records, dim={data['dim']}", flush=True)
    else:
        print("[startup] WARNING: No vector index found", flush=True)


def load_bm25_index(bm25_file=None, jieba_file=None):
    """Load pre-built BM25 index (query-side dict) into process globals."""
    global _bm25_index, _bm25_meta, _bm25_corpus
    if _bm25_index is not None:
        return
    _paths()
    bf = Path(bm25_file) if bm25_file else BM25_FILE
    if bf.exists():
        print("[startup] Loading BM25 index...", flush=True)
        with open(bf, "rb") as f:
            data = pickle.load(f)
        _bm25_index = data["bm25"]
        _bm25_meta = data["meta"]
        _bm25_corpus = data.get("corpus_tokens")
        # Codex-review C2 P1 fix: custom jieba dict used at INDEX BUILD time
        # must also be loaded for QUERY tokenization wherever the BM25 index
        # is loaded outside FastAPI lifespan (parity.py calls directly).
        ensure_jieba(jieba_file)
    else:
        print("[startup] WARNING: No BM25 index found", flush=True)


def load_graph_index(graph_file=None):
    """Load graph-export.json (entity→records, adjacency, nodes) if present."""
    global _graph_data, _entity_index, _graph_adj, _graph_nodes
    if _graph_data is not None:
        return
    _paths()
    gf = Path(graph_file) if graph_file else WORKING_DIR / "graph-export.json"
    if gf.exists():
        _graph_data = json.loads(gf.read_text("utf-8"))
        _entity_index = {e: set(rs) for e, rs in _graph_data.get("entity_to_records", {}).items()}
        _graph_adj = {}
        for e in _graph_data.get("edges", []):
            s, t = e.get("source"), e.get("target")
            if s and t:
                _graph_adj.setdefault(s, set()).add(t)
                _graph_adj.setdefault(t, set()).add(s)
        _graph_nodes = {n["id"]: n for n in _graph_data.get("nodes", [])}
        print(f"[startup] Graph loaded: {len(_graph_data.get('nodes', []))} nodes, "
              f"{len(_graph_data.get('edges', []))} edges, "
              f"{len(_entity_index)} entity→record mappings, "
              f"{len(_graph_adj)} adjacency entries", flush=True)
    else:
        print("[startup] Knowledge graph not found (graph search disabled)", flush=True)


def ensure_jieba(jieba_file=None):
    """Load the custom jieba dict used at index-build time (query parity)."""
    _paths()
    jf = Path(jieba_file) if jieba_file else JIEBA_DICT
    if jf.exists():
        import jieba
        jieba.load_userdict(str(jf))


# ensure_jieba_dict kept as the reviewed name (load_bm25_index used to call it)
def ensure_jieba_dict():
    ensure_jieba()


def build_idx_meta_lookup():
    """Fast record_idx → meta lookup for graph_search (lifespan parity).

    Mirrors the reviewed lifespan block: vector meta first, BM25 meta fills
    gaps. Called by server lifespan after load_* — keeps graph_search
    byte-identical whether reached through lifespan or lazy first use.
    """
    global _idx_to_meta
    # Unconditional rebuild (lifespan parity): indexes are loaded by the
    # caller (server lifespan / _get_retrieval_pipeline) beforehand.
    _idx_to_meta = {}
    for m in _index_meta or []:
        _idx_to_meta[m["idx"]] = m
    for m in _bm25_meta or []:
        if m["idx"] not in _idx_to_meta:
            _idx_to_meta[m["idx"]] = m
    print(f"[startup] Index meta lookup: {len(_idx_to_meta)} records", flush=True)


def load_records(lite_file=None):
    """Load full record lookup (legacy mode; default the production lite file).

    ``_records_state_file`` records which lite file the cached ``_records_state``
    was loaded from so a path change triggers a reload.  Both names are
    process-global state: the ``global`` declaration must cover both, otherwise
    the assignment below makes ``_records_state_file`` function-local and the
    second ``load_records(file)`` call raises ``UnboundLocalError`` (and a
    changed path would silently never reload).
    """
    global _records_state, _records_state_file
    if lite_file is not None:
        if _records_state is None or _records_state_file != str(lite_file):
            _records_state = json.loads(Path(lite_file).read_text("utf-8"))
            _records_state_file = str(lite_file)
        return _records_state
    if _records_state is None:
        lite = Path(lite_file) if lite_file else REPO / "data" / "processed" / "all-records-lite.json"
        _records_state = json.loads(lite.read_text("utf-8"))
        _records_state_file = str(lite)
    return _records_state


_records_state = None
_records_state_file = None


# ── Graph route algorithm (moved verbatim from server.py) ─────────────────


def graph_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Graph-based retrieval with hop=1 neighbor expansion.

    Hop=0: match entities named in query → associated records.
    Hop=1: expand neighbors of matched entities → related records.

    Super-node filter + co-occurrence boost behave exactly as reviewed.
    """
    if _entity_index is None:
        return []
    import jieba.posseg as pseg

    # ── Hop=0: match entities directly named in query ──
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

    # ── Hop=1: expand neighbors of matched entities ──
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
                # Secondary heuristic: skip short entity names (len<3).
                # NOTE: Effect limited — real noise comes from high record-count
                # entities with len>=3. Degree filter is the main defense.
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
            for m in _index_meta or []:
                if m.get("idx") == rec_idx:
                    meta = m
                    break
        if meta is None:
            for m in _bm25_meta or []:
                if m.get("idx") == rec_idx:
                    meta = m
                    break
        if meta:
            results.append({"meta": meta, "score": score})

    print(f"[graph] hop0={len(matched_entities)} entities, "
          f"hop1={len(hop1_entities)} neighbors, "
          f"{len(record_scores)} candidate records", flush=True)
    return results


# ── Legacy pipeline assembly (per-request snapshot first) ─────────────────


def build_pipeline(embeddings=None, meta=None, bm25_index=None, bm25_meta=None,
                   graph_search_fn=None, *, allow_legacy_idx: bool = False):
    """Assemble (VectorRetriever, BM25Retriever, GraphRetriever, RRFFusion)."""
    from .vector import VectorRetriever
    from .bm25 import BM25Retriever
    from .graph import GraphRetriever
    from .fusion import RRFFusion
    vr = VectorRetriever(embeddings=embeddings, meta=meta, allow_legacy_idx=allow_legacy_idx)
    br = BM25Retriever(bm25_index=bm25_index, meta=bm25_meta,
                       tokenize_fn=bm25_tokenize, allow_legacy_idx=allow_legacy_idx)
    gr = GraphRetriever(graph_search_fn=graph_search_fn, allow_legacy_idx=allow_legacy_idx)
    fuse = RRFFusion(k=RRF_K, default_top_k=FINAL_TOP_K)
    return vr, br, gr, fuse


def legacy_pipeline():
    """Process-global legacy pipeline from CURRENT loaded state.

    Parity note: the reviewed `_get_retrieval_pipeline` called the
    server-side load_vector_index()/load_bm25_index() (which resolve the
    live module paths, honoring test patches) BEFORE assembling; the graph
    route was always wired (graph_search returns [] with no graph index).
    Replicated: no auto-loading here — callers preload state via the
    parameterized loaders, then this assembles from globals.
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    def graph_fn(q, k):
        return [(r["meta"]["idx"], r["score"]) for r in graph_search(q, k)]

    _pipeline = build_pipeline(
        embeddings=_vector_index, meta=_index_meta,
        bm25_index=_bm25_index, bm25_meta=_bm25_meta,
        graph_search_fn=graph_fn,
        allow_legacy_idx=True,
    )
    return _pipeline


def snapshot_pipeline(snapshot):
    """Pipeline from a request-pinned RuntimeSnapshot's resources.

    Byte-parity with the reviewed `_get_retrieval_pipeline` manifest branch:
    required resources validated (fail-closed), graph route optional
    (default no-candidates fn), fail-closed identity (no allow_legacy_idx),
    pipeline + record_id_to_meta cached ON the snapshot (request-pinned).
    """
    resources = snapshot.resources
    if "retrieval_pipeline" in resources:
        return resources["retrieval_pipeline"]
    required = ("vector_index", "index_meta", "bm25_index", "bm25_meta")
    missing = [name for name in required if name not in resources]
    if missing:
        raise RuntimeError(f"pinned runtime {snapshot.manifest_id} incomplete: {missing}")
    graph_fn = resources.get("graph_search", lambda q, k: [])
    vr, br, gr, fuse = build_pipeline(
        embeddings=resources["vector_index"], meta=resources["index_meta"],
        bm25_index=resources["bm25_index"], bm25_meta=resources["bm25_meta"],
        graph_search_fn=graph_fn,
    )
    pipeline = (vr, br, gr, fuse)
    resources["retrieval_pipeline"] = pipeline
    resources["record_id_to_meta"] = {m["record_id"]: m for m in resources["index_meta"]}
    return pipeline


async def embed_query(query: str, embed_fn=None):
    """Embed one query (embed_fn injectable — server passes its module-global
    so tests that patch server.embedding_func keep working)."""
    if embed_fn is None:
        from config import embedding_func
        embed_fn = embedding_func
    qe = await embed_fn([query])
    return np.array(qe[0], dtype=np.float32)


async def run_hybrid(query: str, snapshot=None, exclude_ids: set | None = None,
                     embed_fn=None, pipeline=None):
    """Run the legacy hybrid search (vector + BM25 + graph + RRF).

    Returns (results, is_relevant) — the byte-compatible legacy surface:
    list of dicts {record_id, legacy_idx, meta, score(rrf), vec_score,
    bm25_score, graph_score}, truncated to FINAL_TOP_K (25).

    Parity invariants (locked by tests_parity.py frozen gate-1 baselines):
      - RRF score = 1/(position0 + k), position over each route's own ranking
      - route scores carried through as vec/bm25/graph_score
      - BM25 drops score<=0 candidates
      - meta taken from the record index (identical across routes)

    Degraded-index behavior: a missing route contributes no candidates
    (exactly the pre-contract behavior); meta falls back per-candidate.
    """
    if pipeline is not None:
        vr, br, gr, fuse = pipeline
    else:
        vr, br, gr, fuse = (snapshot_pipeline(snapshot) if snapshot is not None
                            else legacy_pipeline())

    fetch_k = min(RETRIEVAL_TOP_K + (len(exclude_ids) if exclude_ids else 0), FETCH_K_CAP)
    qv = await embed_query(query, embed_fn=embed_fn)
    qv = qv / max(np.linalg.norm(qv), 1e-8)
    vec_res = vr.search(qv, top_k=fetch_k)
    bm25_res, graph_res = await asyncio.gather(
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
    fused = fuse.fuse({"vector": vec_res, "bm25": bm25_res, "graph": graph_res},
                      top_k=fuse_top_k)

    meta_lookup = _meta_lookup(snapshot)
    results = []
    for r in fused:
        meta = meta_lookup.get(r.record_id, r.meta or {})
        det = r.route_details or {}
        results.append({
            "record_id": r.record_id,
            "legacy_idx": r.legacy_idx,
            "meta": meta,
            "score": r.raw_score,
            # RRFFusion route_details keys are {route}_score
            # (retrieval/fusion.py) — feed the relevance quality gate
            # (VEC_STRONG / GRAPH_STRONG); must never default to 0.
            "vec_score": det.get("vector_score", det.get("vector", 0.0)),
            "bm25_score": det.get("bm25_score", det.get("bm25", 0.0)),
            "graph_score": det.get("graph_score", det.get("graph", 0.0)),
        })

    if exclude_ids:
        before = len(results)
        results = [r for r in results
                   if r["record_id"] not in exclude_ids
                   and r.get("legacy_idx") not in exclude_ids]
        excluded_count = before - len(results)
        results = results[:FINAL_TOP_K]
        if excluded_count:
            print(f"[search] Excluded {excluded_count} previously cited, "
                  f"{len(results)} remaining", flush=True)
    else:
        results = results[:FINAL_TOP_K]

    # Phase-02 baseline semantics (review blocker 1): is_relevant =
    # strong_vector OR strong_graph. BM25 alone was NEVER sufficient —
    # `or any(bm25_score >= BM25_STRONG)` was an unauthorized Phase-03
    # behavior change and is removed. BM25_STRONG stays defined for the
    # log/diagnostic surface only.
    is_relevant = (
        any(r.get("vec_score", 0) >= VEC_STRONG for r in results)
        or any(r.get("graph_score", 0) >= GRAPH_STRONG for r in results)
    )
    return results, is_relevant


# ── Phase 03 (RT-031) high-recall per-route retrieval ───────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def route_fetch_caps(route_top_k=None) -> dict:
    """Per-route fetch caps for the Phase03 high-recall pool source.

    Default: retrieval.pool.ROUTE_TOP_K (QA_POOL_ROUTE_TOP_K — the versioned
    pool config), overridable per route via QA_ROUTE_TOP_K_VECTOR /
    _BM25 / _GRAPH, or wholesale via the route_top_k argument (int → every
    route; dict → per-route). Legacy run_hybrid NEVER uses these caps —
    its RETRIEVAL_TOP_K fetch / FINAL_TOP_K=25 fused surface is untouched.
    """
    from .pool import ROUTE_TOP_K as _DEFAULT_ROUTE_TOP_K
    caps = {"vector": _DEFAULT_ROUTE_TOP_K, "bm25": _DEFAULT_ROUTE_TOP_K,
            "graph": _DEFAULT_ROUTE_TOP_K}
    if route_top_k is None:
        for route in caps:
            caps[route] = _env_int(f"QA_ROUTE_TOP_K_{route.upper()}", caps[route])
    elif isinstance(route_top_k, int):
        caps = {route: route_top_k for route in caps}
    elif isinstance(route_top_k, dict):
        for route in caps:
            try:
                caps[route] = int(route_top_k.get(route, caps[route]))
            except (TypeError, ValueError):
                pass
    return caps


async def run_routes(query: str, *, snapshot=None, exclude_ids: set | None = None,
                     embed_fn=None, pipeline=None, route_top_k=None,
                     relation_critical: bool = False,
                     relation_requirement_ids: list[str] | None = None) -> dict:
    """Phase03 high-recall per-route retrieval (RT-031 pool source).

    Runs vector / BM25 / graph routes at HIGH-RECALL per-route fetch caps
    and returns the RAW route results BEFORE any fusion and BEFORE the
    legacy global FINAL_TOP_K=25 truncation:

        {"vector": [RetrievalResult...], "bm25": [...], "graph": [...]}

    Each RetrievalResult carries its TRUE per-route rank (1-based list
    position), per-route score (raw_score) and route features
    (route_details) — exactly what build_candidate_pool (RT-031) and the
    RT-033 route-outlier reserve need to see rank-26+ candidates that the
    fused legacy surface would already have dropped.

    Contract (review blocker 2): the Phase03 pool must be built from THESE
    route results, never from run_hybrid's already-truncated fused output.
    The legacy flag-off path keeps run_hybrid unchanged.
    """
    if pipeline is not None:
        vr, br, gr, _fuse = pipeline
    else:
        vr, br, gr, _fuse = (snapshot_pipeline(snapshot) if snapshot is not None
                             else legacy_pipeline())

    caps = route_fetch_caps(route_top_k)
    from runtime_safety import (FailureClass, classify_exception,
                                decide_failure)
    degraded = []

    async def _vector():
        qv = await embed_query(query, embed_fn=embed_fn)
        qv = qv / max(np.linalg.norm(qv), 1e-8)
        return vr.search(qv, top_k=caps["vector"])

    vec_res, bm25_res, graph_res = await asyncio.gather(
        _vector(),
        asyncio.to_thread(br.search, query, caps["bm25"]),
        asyncio.to_thread(gr.search, query, caps["graph"]),
        return_exceptions=True)
    rows = {
        "vector_search": vec_res,
        "bm25_search": bm25_res,
        "graph_search": graph_res,
    }
    for capability, value in list(rows.items()):
        if not isinstance(value, BaseException):
            continue
        decision = decide_failure(
            capability, classify_exception(value),
            requirement_critical=(relation_critical and
                                  capability == "graph_search"))
        requirement_ids = (relation_requirement_ids or [""]) \
            if capability == "graph_search" else [""]
        for requirement_id in requirement_ids:
            degraded.append({
                "capability": capability,
                "failure_class": decision.failure_class.value,
                "reason_code": decision.reason_code,
                "requirement_id": requirement_id,
                "correctness_critical": decision.correctness_critical,
                "fallback_used": decision.fallback,
                "retry_count": 0,
                "state_impact": decision.effect.value,
                "terminal_upper_bound": (
                    "UNVERIFIED" if decision.effect.value == "UNVERIFIED"
                    else "SUPPORTED_IF_CANONICAL_GATES_PASS"),
            })
        rows[capability] = []
    vec_res = rows["vector_search"]
    bm25_res = rows["bm25_search"]
    graph_res = rows["graph_search"]

    def _true_ranks(res):
        # TRUE per-route rank: 1-based list position of the route's own
        # ranking (run_hybrid resets these to 0-based for legacy RRF byte
        # parity — that reset must NEVER leak into the pool source).
        for pos, rr_ in enumerate(res):
            rr_.rank = pos + 1
        return res

    routes = {"vector": _true_ranks(vec_res),
              "bm25": _true_ranks(bm25_res),
              "graph": _true_ranks(graph_res)}
    if exclude_ids:
        for name, res in list(routes.items()):
            routes[name] = [r for r in res
                            if r.record_id not in exclude_ids
                            and getattr(r, "legacy_idx", None) not in exclude_ids]
    return RouteResults(routes, degraded_capabilities=degraded)


def _meta_lookup(snapshot=None) -> dict:
    """Meta lookup for fused candidates (byte-parity with the old seam).

    Snapshot mode: resources["record_id_to_meta"] (stable-id keyed).
    Legacy mode: {m["idx"]: m} keyed by idx — looked up by record_id this
    intentionally mirrors the reviewed legacy seam (lookup misses fall back
    to the per-candidate meta), never "improved" here to keep the frozen
    parity baselines exact.
    """
    if snapshot is not None:
        return snapshot.resources["record_id_to_meta"]
    global _idx_to_meta
    if _idx_to_meta is None:
        _idx_to_meta = {m["idx"]: m for m in _index_meta}
    return _idx_to_meta


# ── Route-level legacy surfaces (moved verbatim from server.py) ────────────
# These are the frozen gate-1 parity interfaces (parity.py calls them
# directly). Byte-for-byte moved, not reimplemented.


async def vector_search(query: str, top_k: int = None, embed_fn=None) -> list:
    """Search the vector index for the most similar records."""
    if top_k is None:
        top_k = RETRIEVAL_TOP_K
    if _vector_index is None:
        return []

    # Embed the query (embed_fn injectable for the server seam)
    if embed_fn is None:
        from config import embedding_func
        embed_fn = embedding_func
    query_emb = await embed_fn([query])
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


def bm25_search(query: str, top_k: int = None) -> list:
    """Search the BM25 index for keyword matches."""
    if top_k is None:
        top_k = RETRIEVAL_TOP_K
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
             k: int = None, top_k: int = None) -> list:
    """Reciprocal Rank Fusion of multiple result lists.

    Returns unified list of {meta, score} with RRF scores.
    """
    if k is None:
        k = RRF_K
    if top_k is None:
        top_k = FINAL_TOP_K
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
