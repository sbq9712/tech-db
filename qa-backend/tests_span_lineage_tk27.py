"""
TK-27 (T048) — Claim-level Provenance / Span-level Source Role.

Document-level provenance (T008) says "this RECORD is a repost". Span
lineage says "THIS span quotes an official statement" vs "this span is
the outlet's own reporting/testing" — the same article can contain both.
Independence is then counted per claim/span:
  - media paragraphs quoting an official statement are NOT independent
    verification of that statement
  - the same media outlet's own interview/testing IS a distinct evidence
    role within the same article
  - provenance uncertainty is preserved (probability/confidence), never
    flattened to a binary verdict
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✅ {name}")
    except AssertionError as e:
        FAIL += 1
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


def _pm(**over):
    base = {"evidence_role": "secondary", "independent_group_id": "g1",
            "provenance_confidence": "medium", "same_origin_probability": 0.2}
    base.update(over)
    return base


def test_span_roles_distinguished():
    from provenance import span_lineage
    rec = {"t": "title", "source": "某媒体", "u": "https://media.com/x"}
    quote = span_lineage(rec, _pm(), "根据官方新闻稿，新产品的带宽达到1.8TB/s")
    assert quote["span_source_role"] == "quoted_primary_source", quote
    assert quote["quoted_primary_source"] and not quote["independent_reporting"]
    own = span_lineage(rec, _pm(), "本报实测确认带宽数据达标")
    assert own["span_source_role"] == "independent_reporting", own
    assert own["independent_reporting"] and not own["quoted_primary_source"]
    # same document, two spans, two roles — T048's core distinction
    assert quote["document_publisher"] == own["document_publisher"] == "某媒体"


def test_media_quote_not_counted_as_independent():
    """5 outlets quoting ONE press release + the vendor's own page = exactly
    1 provenance story and 0 independent validations."""
    from claim_mapping import attach_span_lineage, claim_independence
    pm = {**{f"m{i}": _pm(independent_group_id=f"g{i}") for i in range(5)},
          "vendor": _pm(evidence_role="self_reported",
                        independent_group_id="vendor")}
    citations = [{"id": i + 1, "record_id": f"m{i}"} for i in range(5)] + \
                [{"id": 6, "record_id": "vendor"}]
    claim = {"id": "c1", "type": "MAJOR_FACT", "support_status": "SUPPORTED",
             "supported_by":
                 [{"citation_id": i + 1, "relation": "DIRECT_SUPPORT",
                   "evidence_span": "根据官方新闻稿，带宽为1.8TB/s"} for i in range(5)] +
                 [{"citation_id": 6, "relation": "DIRECT_SUPPORT",
                   "evidence_span": "官方规格页标注1.8TB/s"}]}
    cm = {"claims": [claim]}
    attach_span_lineage(cm, citations, provenance_map=pm)
    rep = claim_independence(cm, provenance_map=pm)
    c = rep["per_claim"][0]
    assert c["groups_total"] == 6, c
    assert c["independent_groups"] == 0, \
        f"quotes of a press release must not count as independent validation: {c}"
    assert not c["independence_sufficient"]


def test_same_outlet_own_reporting_is_independent_role():
    from claim_mapping import attach_span_lineage, claim_independence
    pm = {"m0": _pm(independent_group_id="g1")}
    citations = [{"id": 1, "record_id": "m0"}, {"id": 2, "record_id": "m0"}]
    claim = {"id": "c1", "type": "MAJOR_FACT", "support_status": "SUPPORTED",
             "supported_by": [
                 {"citation_id": 1, "relation": "DIRECT_SUPPORT",
                  "evidence_span": "根据官方新闻稿，带宽为1.8TB/s"},
                 {"citation_id": 2, "relation": "DIRECT_SUPPORT",
                  "evidence_span": "本报实测确认带宽达标"}]}
    cm = {"claims": [claim]}
    attach_span_lineage(cm, citations, provenance_map=pm)
    rep = claim_independence(cm, provenance_map=pm)
    c = rep["per_claim"][0]
    assert c["independent_groups"] == 1, \
        f"the outlet's own testing must count as independent reporting: {c}"
    assert c["independence_sufficient"]
    # same article, both roles present and distinguishable
    roles = set(c["roles"])
    assert roles == {"quoted_primary_source", "independent_reporting"}


def test_uncertainty_preserved_not_binary():
    from provenance import span_lineage
    lin = span_lineage({"t": "t", "source": "s"}, _pm(provenance_confidence="low",
                                                      same_origin_probability=0.42),
                       "正文普通叙述段落")
    assert lin["provenance_confidence"] == "low", "confidence must pass through"
    assert abs(lin["same_origin_probability"] - 0.42) < 1e-9, \
        "probability must pass through, not be flattened to a boolean"
    assert lin["span_source_role"] == "unknown", \
        "no markers + secondary role → unknown, no forced binary guess"


def test_primary_statement_role_counts():
    from provenance import span_lineage, claim_independence_report
    lin = span_lineage({"t": "t", "source": "NVIDIA官网"},
                       _pm(evidence_role="primary", independent_group_id="p1"),
                       "NVIDIA官方规格页标注带宽1.8TB/s")
    assert lin["span_source_role"] == "primary_statement"
    claims = [{"claim_id": "c1", "support": [
        {"record_id": 1, "span_lineage": lin}]}]
    rep = claim_independence_report(claims, provenance_map={1: {}})
    assert rep["per_claim"][0]["independent_groups"] == 1, \
        "a primary source's own statement is first-party but counts as its group's statement"


def test_marker_beats_document_level_inference():
    from provenance import span_lineage
    # document says "independent" but the span explicitly quotes a press
    # release — the span-level marker wins over the document-level role
    lin = span_lineage({"t": "t", "source": "media"},
                       _pm(evidence_role="independent"),
                       "根据官方新闻稿，公司宣布...")
    assert lin["span_source_role"] == "quoted_primary_source"
    assert lin["provenance_confidence"] == "high", \
        "explicit in-span marker upgrades confidence over document inference"


def test_orchestrator_builds_provenance_map():
    from orchestrator import _build_provenance_map
    pm = _build_provenance_map(
        [{"record_id": 1, "meta": {"idx": 1, "t": "A公司发布X", "u": "https://a.com/1"}},
         {"record_id": 2, "meta": {"idx": 2, "t": "A公司发布X", "u": "https://a.com/1"}}],
        {})
    assert set(pm.keys()) == {1, 2}, f"must be keyed by record_id: {pm.keys()}"
    assert pm[1]["independent_group_id"], "entries must carry group ids"
    # same canonical URL → same group (exact_same_url = 0.99)
    assert pm[1]["independent_group_id"] == pm[2]["independent_group_id"]


def test_grader_counts_independence_from_provenance():
    """The grader's policy engine must consume the SAME provenance map the
    orchestrator builds (integration point, no more empty-map blindness)."""
    from evidence_grader import _run_rule_checks
    from evidence_ledger import EvidenceLedger
    led = EvidenceLedger("q", requirements=[{"id": "r1", "description": "d"}])
    ev = [{"record_id": 1}, {"record_id": 2}]
    pm = {1: {"evidence_role": "secondary", "independent_group_id": "same"},
          2: {"evidence_role": "secondary", "independent_group_id": "same"}}
    failures = _run_rule_checks("比较A和B", led, ev,
                                {"question_type": "COMPARISON"}, pm)
    rules = {f["rule"] for f in failures}
    assert any(r.startswith("sufficiency_policy:min_independent_groups") for r in rules), \
        f"comparison with 1 group must fail the policy: {rules}"


if __name__ == "__main__":
    print("TK-27 — span-level source lineage (T048)")
    check("span roles distinguished (quote vs own reporting)", test_span_roles_distinguished)
    check("5 quotes of 1 press release ≠ 5 independent sources", test_media_quote_not_counted_as_independent)
    check("same outlet's own testing IS independent role", test_same_outlet_own_reporting_is_independent_role)
    check("uncertainty preserved (probability/confidence pass through)", test_uncertainty_preserved_not_binary)
    check("primary statement counts in its group", test_primary_statement_role_counts)
    check("in-span marker beats document-level inference", test_marker_beats_document_level_inference)
    check("orchestrator builds provenance map (integration)", test_orchestrator_builds_provenance_map)
    check("grader counts independence from provenance (integration)", test_grader_counts_independence_from_provenance)
    print("=" * 60)
    print(f"  TK-27 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)
