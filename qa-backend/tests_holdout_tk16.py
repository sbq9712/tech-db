"""TK-16 — holdout set construction + SHA256 lock (Q16/Q17).

  * 100 entries, 10-entry smoke subset, deterministic origins
  * lock verifies on the current tree
  * tamper (any entry mutation) → lock FAIL (exit 1)
  * smoke ⊂ full, ids stable
"""
import hashlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOLD = HERE / "test_fixtures" / "holdout"
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def _sha(entries):
    return hashlib.sha256(
        json.dumps({"entries": entries}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def t_shape():
    doc = json.loads((HOLD / "holdout.json").read_text(encoding="utf-8"))
    lock = json.loads((HOLD / "holdout.lock.json").read_text(encoding="utf-8"))
    assert len(doc["entries"]) == 100 == lock["size"]
    ids = [e["id"] for e in doc["entries"]]
    assert len(set(ids)) == 100 and ids == sorted(ids)
    origins = {e["origin"] for e in doc["entries"]}
    assert {"parity_seed", "entity_registry", "record_title", "handwritten"} <= origins
    smoke = set(lock["smoke_subset_ids"])
    assert len(smoke) == 10 and smoke <= set(ids)


def t_lock_verifies():
    doc = json.loads((HOLD / "holdout.json").read_text(encoding="utf-8"))
    lock = json.loads((HOLD / "holdout.lock.json").read_text(encoding="utf-8"))
    assert _sha(doc["entries"]) == lock["sha256_entries"]


def t_tamper_fails():
    p = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "holdout_run.py"),
                        "--mode", "smoke"], capture_output=True, timeout=120, text=True)
    assert p.returncode == 0, p.stdout[-200:]

    doc_path = HOLD / "holdout.json"
    orig = doc_path.read_text(encoding="utf-8")
    doc = json.loads(orig)
    doc["entries"][5]["query"] += " TAMPER"
    doc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        p2 = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "holdout_run.py"),
                             "--mode", "smoke"], capture_output=True, timeout=120, text=True)
        assert p2.returncode == 1, f"tamper not caught: exit={p2.returncode}"
        assert "lock mismatch" in p2.stdout
    finally:
        doc_path.write_text(orig, encoding="utf-8")
    # restored
    p3 = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "holdout_run.py"),
                         "--mode", "smoke"], capture_output=True, timeout=120, text=True)
    assert p3.returncode == 0


def t_determinism():
    """Same entries → same sha (regeneration stability)."""
    doc = json.loads((HOLD / "holdout.json").read_text(encoding="utf-8"))
    s1 = _sha(doc["entries"])
    s2 = _sha(json.loads(json.dumps(doc))["entries"])
    assert s1 == s2


if __name__ == "__main__":
    print("TK-16 — holdout set + SHA256 lock")
    for name, fn in [
        ("100 entries, 10 smoke, stable ids", t_shape),
        ("lock verifies on current tree", t_lock_verifies),
        ("tamper → lock FAIL (exit 1)", t_tamper_fails),
        ("sha determinism", t_determinism),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-16 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
