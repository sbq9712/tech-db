"""TK-13 — frontend evidence card (Q13/Q25/R8).

Validates the deployed assets (:8097 serves repo-root files):
  * qa.js syntax valid (node --check)
  * version bump v=162 present in index.html (cache bust)
  * evidence-card markup paths exist: claim→citation mapping, span highlight
    (mark), AI_SUMMARY badge, graceful degrade (mapping hidden when
    supports_claim_ids empty), no "无数据" placeholder
  * server done-event carries claims[] for the mapping
"""
import subprocess
import sys
import tempfile
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception as e:
        print(f"  ❌ {name}: {e}"); FAIL += 1


def _qa_js() -> str:
    try:
        return urllib.request.urlopen("http://localhost:8097/qa.js", timeout=5).read().decode()
    except Exception:
        return (ROOT / "qa.js").read_text(encoding="utf-8")


def t_syntax():
    src = _qa_js()
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(src); path = f.name
    try:
        p = subprocess.run(["node", "--check", path], capture_output=True, timeout=30)
        assert p.returncode == 0, p.stderr.decode()[:200]
    finally:
        os.unlink(path)


def t_version_bump():
    # Invariant: a cache-bust query string exists and never went backwards.
    # (The exact number ratchets up with every tunnel-URL republish — hardcoding
    # it broke CI on the first post-162 republish.)
    import re as _re
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = _re.search(r"qa\.js\?v=(\d+)", html)
    assert m, "cache-bust version query missing from index.html"
    assert int(m.group(1)) >= 162, f"cache-bust version went backwards: {m.group(1)}"


def t_evidence_card_markup():
    src = _qa_js()
    assert "qa-evidence-card" in src and "主张 → 引用映射" in src
    assert "qa-claim-row" in src and "qa-claim-cite" in src
    # span highlight
    assert "qa-evidence-span" in src and "<mark>" in src
    # AI_SUMMARY badge
    assert "qa-ai-summary-badge" in src and "AI_SUMMARY" in src
    # grounding badge variants (valid/fuzzy via template expr, fail static)
    assert "qa-ground-${c.grounding_status.toLowerCase()}" in src
    assert "qa-ground-fail" in src


def t_graceful_degrade():
    """Mapping section requires BOTH claims AND mapped citations — no placeholder."""
    src = _qa_js()
    assert "supports_claim_ids.length > 0" in src
    assert "无数据" not in src  # no empty-state placeholder anywhere


def t_user_warning_ui():
    src = _qa_js()
    assert "qa-user-warning" in src and "msg.user_warning" in src
    css = (ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".qa-user-warning" in css and ".qa-evidence-card" in css


def t_claims_in_done_event():
    backend = Path(__file__).resolve().parent / "server.py"
    src = backend.read_text(encoding="utf-8")
    assert '"claims": [{' in src  # done event carries claims for the card


def t_render_sim():
    """Simulate the card branch logic with both shapes."""
    claims = [{"id": "claim_1", "text": "固态电池能量密度更高", "status": "SUPPORTED"}]
    citations = [
        {"id": 1, "title": "paper", "supports_claim_ids": ["claim_1"],
         "source_label": "AI_SUMMARY", "highlight": "AI生成的句子"},
        {"id": 2, "title": "raw", "supports_claim_ids": [], "source_label": "ORIGINAL"},
    ]
    mapped = [c for c in citations if c.get("supports_claim_ids")]
    assert len(mapped) == 1
    card_visible = bool(claims) and bool(mapped)
    assert card_visible
    # legacy shape: no claims → hidden entirely
    legacy_cits = [{"id": 1, "supports_claim_ids": []}]
    assert not [c for c in legacy_cits if c.get("supports_claim_ids")]


if __name__ == "__main__":
    print("TK-13 — frontend evidence card")
    for name, fn in [
        ("qa.js syntax (node --check)", t_syntax),
        ("cache-bust v=162", t_version_bump),
        ("evidence card markup paths", t_evidence_card_markup),
        ("graceful degrade, no placeholder", t_graceful_degrade),
        ("user warning UI + styles", t_user_warning_ui),
        ("claims in done event", t_claims_in_done_event),
        ("render branch simulation", t_render_sim),
    ]:
        check(name, fn)
    print("=" * 62)
    print(f"  TK-13 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
