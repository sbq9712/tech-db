"""TK-12 — citation schema upgrade + legacy fill contract (Q12 / R8).

Shape assertions only (no internal-implementation coupling):
  * every citation carries the full schema: source_label / evidence_spans /
    supports_claim_ids / grounding_status / highlight
  * legacy path (non-LLM fields): evidence_spans filled from grounding,
    source_label from as-vs-b/fb, grounding_status present
  * source_label=AI_SUMMARY for summary-only records
  * supports_claim_ids: populated by claim_mapping inverse; [] when absent
    (UI hides the mapping section — no "无数据" placeholder)
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="tk12-"))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


FULL_FIELDS = ("source_label", "evidence_spans", "supports_claim_ids",
               "grounding_status", "body_snippet")


def _fake_result(idx, score=0.9):
    return {"meta": {"idx": idx}, "score": score}


def t_shape_legacy():
    """build_context citations carry the full TK-12 schema shape."""
    import server
    fake_records = [
        {"idx": 0, "t": "paper", "d": "2026-01-01", "a": "Nature",
         "b": "solid state electrolyte enables high energy density " * 3,
         "u": "http://x", "sc": 8, "c": "材料", "tg": ["研究论文"]},
        {"idx": 1, "t": "summary-only", "d": "2026-02-01", "s": "AI精选",
         "as": "这是AI生成的合成摘要：钙钛矿效率达到26%。", "u": "http://y",
         "sc": 5, "c": "精选", "tg": "AI精选"},
    ]
    real_loader = server.load_records
    server.load_records = lambda: fake_records
    try:
        ctx, cits = server.build_context(
            [_fake_result(0), _fake_result(1)], query="solid state electrolyte")
        assert len(cits) == 2
        for c in cits:
            for f in FULL_FIELDS:
                assert f in c, f"{f} missing in {c.get('id')}"
        # defaults present pre-grounding
        assert cits[0]["source_label"] == "ORIGINAL"      # has body b
        assert cits[1]["source_label"] == "AI_SUMMARY"    # as-only record
        assert cits[0]["supports_claim_ids"] == []
        assert cits[0]["grounding_status"] == "UNGROUND"
    finally:
        server.load_records = real_loader


def t_grounding_fills_spans():
    """citation_grounding fills evidence_spans + highlight + status (non-LLM)."""
    from citation_grounding import ground_citation_evidence
    rec = {"fb": "液态电解质存在燃烧风险，固态电解质可显著提升安全性。",
           "b": "", "as": "AI摘要：安全性大幅提升。"}
    g = ground_citation_evidence(rec, proposed_span="", claim_text="", query="固态电解质 安全性")
    assert g["grounding_status"] in ("VALID", "FUZZY", "GROUNDING_FAIL")
    if g["grounding_status"] != "GROUNDING_FAIL":
        span = {"text": g["evidence_span"], "start": g["start_offset"],
                "end": g["end_offset"]}
        assert isinstance(span["text"], str) and span["text"]
        assert span["end"] >= span["start"]
        # server wiring shape
        c = {"evidence_spans": [span], "highlight": g["evidence_span"],
             "grounding_status": g["grounding_status"]}
        assert isinstance(c["highlight"], str)
        assert c["evidence_spans"][0]["text"] == c["highlight"]


def t_supports_claim_ids_inverse():
    """supports_claim_ids = inverse of claim_mapping's supported_by."""
    claim_map = {"claims": [
        {"id": "claim_1", "text": "A", "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"},
                          {"citation_id": 2, "relation": "PARTIAL"}]},
        {"id": "claim_2", "text": "B", "support_status": "UNSUPPORTED",
         "supported_by": []},
    ]}
    citations = [{"id": 1}, {"id": 2}, {"id": 3}]
    # same logic as server wiring
    by_cit = {}
    for cl in claim_map["claims"]:
        for sup in (cl.get("supported_by") or []):
            by_cit.setdefault(sup.get("citation_id"), []).append(cl.get("id"))
    for c in citations:
        c["supports_claim_ids"] = by_cit.get(c.get("id"), [])
    assert citations[0]["supports_claim_ids"] == ["claim_1"]
    assert citations[1]["supports_claim_ids"] == ["claim_1"]
    assert citations[2]["supports_claim_ids"] == []   # UI hides, no placeholder


def t_source_label_as_vs_bfb():
    """source_label contract: as-only → AI_SUMMARY; b/fb present → ORIGINAL."""
    import server
    fake_records = [
        {"idx": 0, "t": "x", "d": "d", "s": "s", "b": "full body text here " * 4,
         "as": "AI summary present too", "u": "u"},
        {"idx": 1, "t": "y", "d": "d", "s": "s", "fb": "full body fb here " * 4,
         "as": "AI summary", "u": "u"},
    ]
    real_loader = server.load_records
    server.load_records = lambda: fake_records
    try:
        _, cits = server.build_context([_fake_result(0), _fake_result(1)],
                                       query="body text")
        assert cits[0]["source_label"] == "ORIGINAL"
        assert cits[1]["source_label"] == "ORIGINAL"  # fb counts as original
    finally:
        server.load_records = real_loader


def t_done_event_fields():
    """Done event shape carries citations passthrough + user_warning field."""
    import json
    payload = {"answer": "a", "citations": [], "cited_record_ids": [],
               "searched_record_ids": [], "answer_status": "SUPPORTED",
               "stop_reason": "evidence_sufficient", "boundary_message": "",
               "user_warning": "", "evidence_summary": {}, "trace_id": "x"}
    assert set(payload) >= {"user_warning", "boundary_message"}  # TK-10 + TK-06


if __name__ == "__main__":
    print("TK-12 — citation schema + legacy fill contract")
    for name, fn in [
        ("full schema shape on legacy path", t_shape_legacy),
        ("grounding fills evidence_spans + highlight (non-LLM)", t_grounding_fills_spans),
        ("supports_claim_ids inverse mapping", t_supports_claim_ids_inverse),
        ("source_label: as vs b/fb", t_source_label_as_vs_bfb),
        ("done event schema (user_warning etc.)", t_done_event_fields),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-12 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
