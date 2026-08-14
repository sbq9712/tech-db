"""
Tests for Phase B (Evidence Infrastructure) and Phase C (Retrieval Layer).
"""
import sys
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ── TK-03 test isolation (Q22): redirect registry/index dirs to temp dirs so
# suites never pollute production runtime/indexes. setdefault: an explicit
# env (e.g. parity baseline runs) still wins.
import os as _os_t3, tempfile as _tf_t3
_os_t3.environ.setdefault("TECH_DB_INDEX_DIR", _tf_t3.mkdtemp(prefix="techdb-test-idx-"))
_os_t3.environ.setdefault("TECH_DB_RUNTIME_DIR", _tf_t3.mkdtemp(prefix="techdb-test-rt-"))

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


# ── T008: Provenance ──
print("\n=== T008: Provenance Clustering ===")
from provenance import compute_provenance_similarity, count_independent_sources

# Test: Same URL → high probability
r1 = {"u": "https://example.com/article1", "t": "Test Article", "d": "2026-01-15"}
r2 = {"u": "https://example.com/article1", "t": "Test Article", "d": "2026-01-15"}
score, reason = compute_provenance_similarity(r1, r2)
test("same URL → high probability", score >= 0.9)

# Test: Same domain, similar title
r3 = {"u": "https://mp.weixin.qq.com/s/abc", "t": "固态电池重大突破", "d": "2026-01-15"}
r4 = {"u": "https://mp.weixin.qq.com/s/def", "t": "固态电池重大突破！", "d": "2026-01-15"}
score, reason = compute_provenance_similarity(r3, r4)
test("same domain + similar title", score >= 0.5)

# Test: Different everything → low score
r5 = {"u": "https://site1.com/a", "t": "固态电池技术发展", "d": "2026-01-15"}
r6 = {"u": "https://site2.com/b", "t": "量子计算应用前景", "d": "2026-06-20"}
score, reason = compute_provenance_similarity(r5, r6)
test("different sources → low score", score < 0.3)

# Test: count_independent_sources
provenance_map = {
    1: {"independent_group_id": "prov-1"},
    2: {"independent_group_id": "prov-1"},  # same group as 1
    3: {"independent_group_id": "prov-2"},
    4: {"independent_group_id": "prov-3"},
}
count = count_independent_sources([1, 2, 3, 4], provenance_map)
test("count independent sources", count == 3)


# ── T009: Source Suitability ──
print("\n=== T009: Source Suitability ===")
from source_suitability import assess_source_suitability

# Company spec → high for product_spec
rec = {"tg": "产业进展", "s": "NVIDIA", "c": "半导体"}
result = assess_source_suitability(rec, claim_type="product_spec")
test("company source + spec → decent suitability", result["source_suitability"] >= 0.6)

# Company performance claim → needs attribution
result = assess_source_suitability(rec, claim_type="performance_claim", is_self_reported=True)
test("company perf claim → needs attribution", result["attribution_required"])
test("company perf claim → independence penalty", result["independence_penalty"] > 0)

# Academic source for scientific result
academic_rec = {"tg": "研究论文", "s": "Nature"}
result = assess_source_suitability(academic_rec, claim_type="scientific_result")
test("academic → high suitability", result["source_suitability"] >= 0.7)


# ── T010: Temporal ──
print("\n=== T010: Temporal Metadata ===")
from temporal import parse_date, determine_temporal_status, extract_temporal_hints

# Test date parsing
dt = parse_date("2026-01-15")
test("date parsing", dt is not None and dt.year == 2026)

dt = parse_date("2026年1月15日")
test("Chinese date parsing", dt is not None and dt.year == 2026)

# Test temporal status for recent article
from datetime import datetime, timedelta
recent_rec = {"d": (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")}
temporal = determine_temporal_status(recent_rec, query_temporal_intent="current")
test("recent article → current", temporal["temporal_status"] == "current")
test("recent for current query → high relevance", temporal["temporal_relevance"] == "high")

# Test old article for current query
old_rec = {"d": "2023-01-01"}
temporal = determine_temporal_status(old_rec, query_temporal_intent="current")
test("old article → superseded", temporal["temporal_status"] in ("superseded", "historical"))
test("old for current → low relevance", temporal["temporal_relevance"] == "low")

# Test old article for historical query
temporal = determine_temporal_status(old_rec, query_temporal_intent="historical")
test("old for historical → high relevance", temporal["temporal_relevance"] == "high")

# Test temporal hints extraction
hints = extract_temporal_hints("预计2028年实现量产，到2030年市场规模达100亿")
test("extract prediction hints", len(hints) >= 1)


# ── T011: Entity Resolver ──
print("\n=== T011: Entity Canonicalization ===")
from entity_resolver import EntityRegistry, build_seed_registry

reg = build_seed_registry()
stats = reg.stats()
test("registry has entities", stats["total_entities"] > 0)

# Test exact match
result = reg.resolve("英伟达")
test("exact alias match", result["status"] == "LINKED" and result["entity_id"] == "org:nvidia")

# Test case insensitive
result = reg.resolve("nvidia")
test("case insensitive match", result["status"] == "LINKED")

# Test unknown entity
result = reg.resolve("某完全不存在的实体名称")
test("unknown entity → NEW", result["status"] == "NEW")


# ── T014: Retrieval Layer ──
print("\n=== T014: Retrieval Layer ===")
from retrieval.vector import VectorRetriever, RetrievalResult
from retrieval.bm25 import BM25Retriever
from retrieval.fusion import RRFFusion
import numpy as np

# Test vector retriever with fake data
embeddings = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
meta = [{"idx": 10, "t": "doc1"}, {"idx": 20, "t": "doc2"}, {"idx": 30, "t": "doc3"}]
vr = VectorRetriever(embeddings, meta)
query_vec = np.array([1, 0, 0], dtype=np.float32)
results = vr.search(query_vec, top_k=3)
test("vector search returns results", len(results) == 3)
test("vector top result correct", results[0].record_id == 10)
test("vector route name", results[0].route == "vector")
test("vector rank starts at 1", results[0].rank == 1)

# Test RRF fusion
from retrieval.vector import RetrievalResult
route_results = {
    "vector": [
        RetrievalResult(10, "vector", 0.9, 1, {"t": "doc1"}, {}),
        RetrievalResult(20, "vector", 0.7, 2, {"t": "doc2"}, {}),
    ],
    "bm25": [
        RetrievalResult(20, "bm25", 5.0, 1, {"t": "doc2"}, {}),
        RetrievalResult(30, "bm25", 3.0, 2, {"t": "doc3"}, {}),
    ],
}
fusion = RRFFusion()
fused = fusion.fuse(route_results)
test("RRF produces deduplicated results", len(fused) == 3)
test("RRF record 20 appears in both", any(r.record_id == 20 for r in fused))
test("RRF has per-route scores", "vector_score" in fused[0].route_details)
test("RRF has rrf_score", "rrf_score" in fused[0].route_details)


# ── T017: Evidence Selector ──
print("\n=== T017: Evidence Selector ===")
from evidence_selector import select_evidence

candidates = [
    {"record_id": 1, "rerank_score": 0.95},
    {"record_id": 2, "rerank_score": 0.90},
    {"record_id": 3, "rerank_score": 0.85},
    {"record_id": 4, "rerank_score": 0.80},
    {"record_id": 5, "rerank_score": 0.75},
]
provenance = {
    1: {"independent_group_id": "prov-1"},
    2: {"independent_group_id": "prov-1"},  # Same group as 1
    3: {"independent_group_id": "prov-2"},
    4: {"independent_group_id": "prov-3"},
    5: {"independent_group_id": "prov-1"},  # Same group as 1
}
result = select_evidence(candidates, provenance_map=provenance, max_slots=4)
test("selector returns selected", len(result["selected"]) > 0)
test("selector limits per group", 
     sum(1 for s in result["selected"] if provenance[s["record_id"]]["independent_group_id"] == "prov-1") <= 3)
test("selector has reasons", all("selection_reason" in s for s in result["selected"]))


# ── T021: Evidence Ledger ──
print("\n=== T021: Evidence Ledger ===")
from evidence_ledger import EvidenceLedger, REQ_MISSING, REQ_SUPPORTED

ledger = EvidenceLedger("test question", [
    {"id": "r1", "description": "NVIDIA specs", "importance": "critical"},
    {"id": "r2", "description": "AMD specs", "importance": "critical"},
])

status = ledger.get_status()
test("ledger starts with all MISSING", status["missing"] == 2)
test("ledger not sufficient", not ledger.has_sufficient_evidence())

# Add evidence for r1
ledger.update([], requirement_mapping={"r1": [1, 2]}, 
              provenance_map={1: {"independent_group_id": "g1"}, 2: {"independent_group_id": "g2"}})
status = ledger.get_status()
test("r1 now has evidence", status["requirements"][0]["evidence_count"] > 0)

# r2 still missing
test("r2 still missing", not ledger.has_sufficient_evidence())

# Add evidence for r2
ledger.update([], requirement_mapping={"r2": [3, 4]},
              provenance_map={3: {"independent_group_id": "g3"}, 4: {"independent_group_id": "g4"}})
status = ledger.get_status()
test("both requirements now have evidence", status["supported"] >= 1)
test("ledger sufficient", ledger.has_sufficient_evidence())

# Test snapshots
test("ledger has snapshots", len(ledger.snapshots) == 2)


# ── T025: Stopping Criteria ──
print("\n=== T025: Stopping Criteria ===")
from stopping import should_stop

# Sufficient evidence
stop, reason = should_stop(
    iteration=1,
    ledger_status={"requirements": []},
    grader_result={"overall": "SUFFICIENT"},
    gap_result={"should_stop": False, "queries": []},
    new_evidence_count=5,
    total_evidence_count=5,
)
test("sufficient → stop", stop and reason == "evidence_sufficient")

# Max iterations
stop, reason = should_stop(
    iteration=10,
    ledger_status={"requirements": []},
    grader_result={"overall": "INSUFFICIENT"},
    gap_result={"should_stop": False, "queries": [{"query": "test"}]},
    new_evidence_count=5,
    total_evidence_count=20,
)
test("max iterations → stop", stop and reason == "max_iterations_reached")


# ── T030: Conflict Detection ──
print("\n=== T030: Conflict Detection ===")
from conflict_detector import detect_conflicts, CONFLICT_CONTRADICT, CONFLICT_AGREE

# Test contradiction detection
evidence = [
    {"record_id": 1, "text": "带宽达到1.8TB/s", "date": "2026-01-01"},
    {"record_id": 2, "text": "带宽达到3.2TB/s", "date": "2026-01-01"},
]
result = detect_conflicts(evidence)
test("contradiction detected", result["has_conflicts"])

# Test agreement (same value)
evidence_agree = [
    {"record_id": 1, "text": "效率达到26.1%", "date": "2026-01-01"},
    {"record_id": 2, "text": "效率达到26.1%", "date": "2026-01-01"},
]
result = detect_conflicts(evidence_agree)
test("no false conflict for agreement", not result["has_conflicts"])


# ── T031: Context Builder ──
print("\n=== T031: Context Builder ===")
from context_builder import build_evidence_package

requirements = [
    {"id": "r1", "description": "NVIDIA specs"},
]
evidence_set = [
    {"record_id": 0, "requirement_id": "r1"},
]
records = [{"t": "Test Record", "b": "This is test content about NVIDIA.", "s": "source1", "d": "2026-01-01"}]
ledger_status = {"requirements": [{"id": "r1", "status": "SUPPORTED", "description": "NVIDIA specs"}]}

context = build_evidence_package(
    query="NVIDIA specs",
    requirements=requirements,
    evidence_set=evidence_set,
    ledger_status=ledger_status,
    records=records,
)
test("context has question", "用户问题" in context)
test("context has evidence", "证据资料" in context)
test("context wraps data", "RETRIEVED_DATA" in context)


# ── Summary ──
print(f"\n{'='*70}")
print(f"  Phase B+C Results: {passed} passed, {failed} failed")
print(f"{'='*70}")

sys.exit(1 if failed > 0 else 0)
