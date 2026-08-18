"""
TK-20 (T049) — eval-side synthetic isolation (Q19).

Policy: the retrieval INDEX keeps ai-summary text (`as`) — it has retrieval
value and may surface in answer citations (marked source_label=AI_SUMMARY,
TK-12). But eval-side ground truth (holdout anchors / golden answers) must
never be validated against synthetic-only records: a record whose only
content is `as` has no original evidence behind it.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

PASS, FAIL = 0, 0

# The full corpus + real index live outside git (1.2G). In CI the real-data
# checks degrade to a skip with an explicit reason (same pattern as
# tests_shadow_tk17). Synthetic-isolation of the MINI fixture still runs.
_REAL_LITE = ROOT / "data" / "processed" / "all-records-lite.json"
_REAL_IDX = ROOT / "data" / "lightrag" / "vector_index_v2.pkl"
HAVE_REAL = _REAL_LITE.exists() and _REAL_IDX.exists()


def _require_real(name, fn):
    def wrapped():
        if not HAVE_REAL:
            print(f"  ⏭  {name}: skipped (real corpus/index not present)")
            return
        return fn()
    return wrapped


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception:
        import traceback
        print(f"  ❌ {name}")
        traceback.print_exc()
        FAIL += 1


def _records():
    return json.loads((ROOT / "data" / "processed" / "all-records-lite.json")
                      .read_text("utf-8"))


def _as_only(record):
    return bool((record.get("as") or "").strip()) and not bool((record.get("b") or "").strip())


def test_holdout_has_no_as_only_anchors():
    """DoD 1: eval ground truth contains no as-only records."""
    import pickle
    records = _records()
    meta = pickle.load(open(ROOT / "data" / "lightrag" / "vector_index_v2.pkl", "rb"))["meta"]
    by_idx = {m["idx"]: m for m in meta}
    d = json.loads((HERE / "test_fixtures" / "holdout" / "holdout.json").read_text("utf-8"))
    anchored = [e for e in d["entries"] if e.get("expected_idx") is not None]
    assert anchored, "no anchored entries found"
    for e in anchored:
        m = by_idx[e["expected_idx"]]
        cands = [r for r in records if r.get("t") == m.get("t")
                 and (r.get("d") or "") == (m.get("d") or "")]
        assert cands, f"{e['id']}: anchor record unresolvable"
        assert not _as_only(cands[0]), \
            f"{e['id']}: anchor is as-only (synthetic ground truth, Q19)"


def test_unanchored_entries_are_documented():
    """Every unanchored title entry must carry the TK-20 note (audit trail)."""
    d = json.loads((HERE / "test_fixtures" / "holdout" / "holdout.json").read_text("utf-8"))
    for e in d["entries"]:
        if e.get("origin") == "record_title" and e.get("expected_idx") is None:
            assert "TK-20" in (e.get("note") or ""), \
                f"{e['id']}: unanchored title entry without TK-20 rationale"


def test_as_only_hint_is_not_citable():
    """RT-015 supersedes Q19: as-only material cannot become evidence."""
    import server
    # find one as-only record and build its citation via build_context
    records = _records()
    as_only_pos, as_only = next((i, r) for i, r in enumerate(records) if _as_only(r))
    fake_result = [{
        "meta": {"idx": as_only_pos, "t": as_only.get("t", ""), "s": as_only.get("a", ""),
                 "d": as_only.get("d", ""), "u": as_only.get("u", "")},
        "score": 1.0, "vec_score": 0.5, "bm25_score": 0.5,
    }]
    server._records = records  # inject so the seam resolves the record
    ctx, citations = server.build_context(fake_result, "测试")
    assert not citations, "as-only retrieval hint must not become a citation"
    assert not ctx, "as-only retrieval hint must not enter factual context"


def test_body_records_label_original():
    """Records with an original body are labelled ORIGINAL — the isolation
    boundary between synthetic and original text is enforced at the citation
    seam, not just in eval."""
    import server
    records = _records()
    wb_pos, with_body = next((i, r) for i, r in enumerate(records)
                             if (r.get("b") or "").strip() and not (r.get("as") or "").strip())
    fake_result = [{
        "meta": {"idx": wb_pos, "t": with_body.get("t", ""), "s": with_body.get("a", ""),
                 "d": with_body.get("d", ""), "u": with_body.get("u", "")},
        "score": 1.0, "vec_score": 0.5, "bm25_score": 0.5,
    }]
    server._records = records
    ctx, citations = server.build_context(fake_result, "测试")
    c = citations[0]
    assert c.get("source_label") == "ORIGINAL", c.get("source_label")


def test_lock_covers_current_entries():
    """The TK-20 re-issue is a dedicated lock edit (Q17): sha must match."""
    import hashlib
    d = json.loads((HERE / "test_fixtures" / "holdout" / "holdout.json").read_text("utf-8"))
    lock = json.loads((HERE / "test_fixtures" / "holdout" / "holdout.lock.json").read_text("utf-8"))
    payload = json.dumps({"entries": d["entries"]}, ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(payload).hexdigest() == lock["sha256_entries"], "lock drift"
    assert len(d["entries"]) == lock["size"]
    assert "TK-20" in lock.get("unlocked_by", ""), "lock must record the TK-20 unlock"


if __name__ == "__main__":
    print("TK-20 / RT-015 — synthetic evidence isolation")
    check("holdout anchors contain no as-only records",
          _require_real("anchors", test_holdout_has_no_as_only_anchors))
    check("unanchored entries carry TK-20 rationale", test_unanchored_entries_are_documented)
    check("as-only hint cannot enter factual context/citations",
          _require_real("as-isolation", test_as_only_hint_is_not_citable))
    check("original-body citation labelled ORIGINAL",
          _require_real("orig-label", test_body_records_label_original))
    check("holdout lock re-issued (Q17 dedicated unlock)", test_lock_covers_current_entries)
    print("=" * 60)
    print(f"  TK-20 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)
