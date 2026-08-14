"""TK-06 — flag wave-1 defaults + knowledge boundary wiring regression."""
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="tk06-idx-"))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def test_wave1_defaults_on():
    from feature_flags import Flags
    for n in ("PROVENANCE", "TEMPORAL", "ENTITY_RESOLUTION", "SEMANTIC_GRAPH",
              "CONTEXTUAL_CHUNKS", "NUMERIC_FACTS", "EVIDENCE_SELECTOR",
              "KNOWLEDGE_BOUNDARY"):
        assert getattr(Flags, f"{n}_ENABLED") is True, f"{n} should default on"


def test_llm_group_on_after_gate3():
    """TK-19 gate 3 flip: the LLM-call group now defaults ON (gate3_report.json
    PASS + replay evidence day1.json). Per-flag kill switch must still work."""
    from feature_flags import Flags
    for n in ("AGENTIC", "ROUTER", "DECOMPOSITION", "RERANKER", "CLAIM_MAPPING",
              "ITERATIVE_RETRIEVAL", "EVIDENCE_GRADER"):
        assert getattr(Flags, f"{n}_ENABLED") is True, f"{n} must be on after gate 3"
    # kill switch for the LLM group still honoured
    os.environ["QA_AGENTIC_ENABLED"] = "0"
    for mod in list(sys.modules):
        if mod == "feature_flags":
            del sys.modules[mod]
    from feature_flags import Flags as F2
    assert F2.AGENTIC_ENABLED is False, "QA_AGENTIC_ENABLED=0 must disable the group"
    os.environ.pop("QA_AGENTIC_ENABLED")
    for mod in list(sys.modules):
        if mod == "feature_flags":
            del sys.modules[mod]


def test_kill_switch():
    os.environ["QA_PROVENANCE_ENABLED"] = "0"
    for mod in list(sys.modules):
        if mod == "feature_flags":
            del sys.modules[mod]
    from feature_flags import Flags
    assert Flags.PROVENANCE_ENABLED is False
    os.environ.pop("QA_PROVENANCE_ENABLED")


def test_health_includes_knowledge_boundary():
    from feature_flags import Flags
    st = Flags.status()
    assert st.get("knowledge_boundary") is True


def test_abstain_message():
    from knowledge_boundary import assess_coverage, format_boundary_message
    from answer_status import AnswerStatus
    cov = assess_coverage(requirements=[{"status": "MISSING"}],
                          evidence_count=0, independent_groups=0)
    msg = format_boundary_message(
        answer_status=AnswerStatus.UNSUPPORTED, supported_aspects=[],
        unsupported_aspects=["量子计算机商业化"], coverage_level=cov)
    assert cov in ("LOW", "UNKNOWN")
    assert ("不足以" in msg) or ("缺少" in msg) or ("未找到" in msg)


def test_semantic_graph_build_no_crash():
    os.environ["QA_SEMANTIC_GRAPH_ENABLED"] = "1"
    from semantic_graph import build_graph_from_records
    mini = json.loads((Path(__file__).resolve().parent /
                       "test_fixtures/mini_index/all-records-mini.json").read_text("utf-8"))
    g = build_graph_from_records(mini[:8])
    assert g is not None and hasattr(g, "statements")
    os.environ.pop("QA_SEMANTIC_GRAPH_ENABLED")


if __name__ == "__main__":
    import json
    print("TK-06 — flags wave 1 + knowledge boundary")
    check("wave-1 flags default ON", test_wave1_defaults_on)
    check("LLM group ON after gate 3 (TK-19 flip) + kill switch", test_llm_group_on_after_gate3)
    check("kill switch (QA_PROVENANCE_ENABLED=0)", test_kill_switch)
    check("health status includes knowledge_boundary", test_health_includes_knowledge_boundary)
    check("abstain boundary message", test_abstain_message)
    check("SEMANTIC_GRAPH build doesn't crash (Q29 regression)", test_semantic_graph_build_no_crash)
    print("=" * 60)
    print(f"  TK-06 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)
