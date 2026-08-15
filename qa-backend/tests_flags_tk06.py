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


def test_early_unsupported_boundary():
    """codex-review C3 P2: early unsupported exits (weak query / topic
    exhausted) must carry a boundary_message when the flag is ON, and none
    when OFF."""
    import server
    q = "超导量子比特纠错最新进展"
    msg = server._no_evidence_boundary(q, exhausted=False)
    assert isinstance(msg, str) and msg, "flag ON → early exit needs boundary message"
    assert ("不足以" in msg) or ("缺少" in msg) or ("未找到" in msg)
    msg2 = server._no_evidence_boundary(q, exhausted=True)
    assert msg2, "topic-exhausted variant must also produce a message"
    # flag OFF → empty (flag-gated, not unconditional)
    os.environ["QA_KNOWLEDGE_BOUNDARY_ENABLED"] = "0"
    try:
        for mod in ("feature_flags", "server"):
            sys.modules.pop(mod, None)
        import importlib
        import server as server2
        importlib.reload(server2)
        assert server2._no_evidence_boundary(q, exhausted=False) == ""
    finally:
        os.environ.pop("QA_KNOWLEDGE_BOUNDARY_ENABLED")
        for mod in ("feature_flags", "server"):
            sys.modules.pop(mod, None)


def test_selector_normalization_reranker_off():
    """codex-review C3 P2: with the reranker flag OFF, legacy retrieval dicts
    ({meta, score}) fed to select_evidence must not be mass-rejected as
    below_min_relevance — orchestrator._normalize_for_selector maps them to
    record_id/rerank_score first."""
    from orchestrator import _normalize_for_selector
    legacy = [{"meta": {"idx": 7, "t": "a"}, "score": 0.031},
              {"meta": {"idx": 9, "t": "b"}, "score": 0.027}]
    norm = _normalize_for_selector(legacy)
    assert [c["record_id"] for c in norm] == [7, 9]
    assert all(c["rerank_score"] > 0.15 for c in norm[:1]), "top candidate above min relevance"
    # already-normalized (rerank output) pass through unchanged
    done = [{"record_id": 5, "rerank_score": 0.82, "meta": {}}]
    assert _normalize_for_selector(done) == done
    # end-to-end: selector no longer rejects everything on legacy input
    from evidence_selector import select_evidence
    out = select_evidence(_normalize_for_selector(legacy))
    assert out["selected"], "selector must select from normalized legacy candidates"


def test_boundary_reads_claim_schema():
    """codex-review C3 P2: the main boundary block must read claim_mapping's
    real schema ({id, text, support_status}), not status/claim (always
    missing). Verified via source introspection of server.py."""
    src = (Path(__file__).resolve().parent / "server.py").read_text("utf-8")
    # the boundary block's aspect extraction
    bad = 'c.get("status")' in src and 'c.get("claim", "")' in src
    assert not bad, "boundary block still reads .status/.claim (wrong schema)"
    assert 'c.get("support_status") == "SUPPORTED"' in src
    assert '[c.get("text", "") for c in claim_map' in src


if __name__ == "__main__":
    import json
    print("TK-06 — flags wave 1 + knowledge boundary")
    check("wave-1 flags default ON", test_wave1_defaults_on)
    check("LLM group ON after gate 3 (TK-19 flip) + kill switch", test_llm_group_on_after_gate3)
    check("kill switch (QA_PROVENANCE_ENABLED=0)", test_kill_switch)
    check("health status includes knowledge_boundary", test_health_includes_knowledge_boundary)
    check("abstain boundary message", test_abstain_message)
    check("SEMANTIC_GRAPH build doesn't crash (Q29 regression)", test_semantic_graph_build_no_crash)
    check("early unsupported exits carry boundary message (C3)", test_early_unsupported_boundary)
    check("selector normalization with reranker OFF (C3)", test_selector_normalization_reranker_off)
    check("boundary block reads claim_mapping schema (C3)", test_boundary_reads_claim_schema)
    print("=" * 60)
    print(f"  TK-06 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)
