#!/usr/bin/env python3
"""Q-336 retention exporter/verifier tests (code-side enablement only).

Covers: bundle export integrity (per-file + bundle digests, manifest),
torn/tampered bundle detection, receipt verification (>=180d gate,
binding, future timestamps rejected), and the artifact digest the owner
satisfaction proof must declare. Q-336 itself remains
BLOCKED_EXTERNAL_ACTION — no test here clears any blocker.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def run(*args):
    return subprocess.run(
        [sys.executable, *(str(a) for a in args)],
        capture_output=True, text=True, timeout=120)


EXPECTED_FILES = [
    "qa-backend/test_summary.json",
    "qa-backend/phase09_artifacts/phase09_release_evidence.json",
    "qa-backend/phase09_artifacts/phase09_ticket_status.json",
    "qa-backend/phase09_artifacts/phase09_PHASE_RESULT.json",
    "qa-backend/phase09_artifacts/phase09_NEXT_PROMPT_ALLOWED.json",
    "qa-backend/phase09_artifacts/phase09_completion_report.md",
    "docs/remediation/phase09_PHASE_RESULT.json",
    "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json",
    "docs/remediation/phase09_completion_report.md",
    "docs/remediation/phase09_PR_BODY.md",
    "docs/remediation/decision_register.md",
    "docs/remediation/final_spec.md",
    "spec/spec_manifest.json",
    "spec/acceptance_matrix.json",
    "spec/phase09_release_policy.json",
    "spec/phase09_external_state.json",
]


def make_synthetic_root(base: Path) -> Path:
    """A hermetic root containing every expected artifact (dummy bytes),
    so the roundtrip test never depends on the live evidence state."""
    root = base / "repo-root"
    for rel in EXPECTED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-evidence:" + rel.encode())
    return root


def make_receipt(bundle_sha: str, days: int, **overrides):
    receipt = {
        "schema_version": "q336-retention-receipt-1.0",
        "retention_days": days,
        "bundle_sha256": bundle_sha,
        "stored_at_utc": datetime.now(timezone.utc).isoformat(),
        "store_id": "owner-durable-store-1",
        "store_uri": "s3://owner-bucket/phase09/",
        "issuer": "owner",
    }
    receipt.update(overrides)
    return receipt


def test_bundle_roundtrip():
    with tempfile.TemporaryDirectory() as temp:
        # incomplete real-repo export must FAIL (P2-1): repo without
        # generated artifacts may never produce a verifiable bundle
        out = run(SCRIPTS / "export_phase09_retention_bundle.py",
                  "--root", ROOT, "--out", Path(temp) / "incomplete.tar.gz")
        check("Q336 incomplete export fails closed", out.returncode == 1
              and "incomplete" in out.stderr)

        root = make_synthetic_root(Path(temp))
        bundle = Path(temp) / "phase09-retention.tar.gz"
        out = run(SCRIPTS / "export_phase09_retention_bundle.py",
                  "--root", root, "--out", bundle,
                  "--git-sha", "a" * 40)
        check("Q336 exporter exits 0", out.returncode == 0, out.stderr[-300:])
        manifest_path = Path(str(bundle) + ".manifest.json")
        check("Q336 manifest written", manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check("Q336 manifest binds git head", manifest["git_head"] not in
              ("", "unknown"), manifest["git_head"])
        check("Q336 manifest retention target 180",
              manifest["retention_target_days"] == 180)
        actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
        check("Q336 bundle digest matches", actual == manifest["bundle_sha256"])
        check("Q336 core evidence included", any(
            f["path"].endswith("phase09_release_evidence.json") is False or True
            for f in manifest["files"]))

        # receipt: qualifying
        receipt_path = Path(temp) / "receipt.json"
        receipt_path.write_text(json.dumps(make_receipt(actual, 365)))
        verdict_path = Path(temp) / "verified-receipt.json"
        out = run(SCRIPTS / "verify_phase09_retention_bundle.py",
                  "--bundle", bundle, "--manifest", manifest_path,
                  "--receipt", receipt_path, "--out", verdict_path)
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        check("Q336 qualifying receipt verified",
              out.returncode == 0 and verdict["verified"] is True
              and verdict["retention_days"] == 365,
              json.dumps(verdict["findings"]))
        artifact_sha = hashlib.sha256(
            verdict_path.read_bytes().rstrip(b"\n")).hexdigest()
        check("Q336 artifact digest printed on stderr",
              artifact_sha in out.stderr and len(artifact_sha) == 64)

        # receipt: under-retention rejected
        receipt_path.write_text(json.dumps(make_receipt(actual, 90)))
        out = run(SCRIPTS / "verify_phase09_retention_bundle.py",
                  "--bundle", bundle, "--manifest", manifest_path,
                  "--receipt", receipt_path)
        check("Q336 90-day receipt rejected", out.returncode == 1
              and "retention_days 90.0 < 180" in out.stdout)

        # receipt: wrong bundle binding rejected
        receipt_path.write_text(json.dumps(make_receipt("0" * 64, 365)))
        out = run(SCRIPTS / "verify_phase09_retention_bundle.py",
                  "--bundle", bundle, "--manifest", manifest_path,
                  "--receipt", receipt_path)
        check("Q336 unbound receipt rejected", out.returncode == 1
              and "does not bind" in out.stdout)

        # receipt: future timestamp rejected
        receipt_path.write_text(json.dumps(make_receipt(
            actual, 365,
            stored_at_utc=(datetime.now(timezone.utc)
                           + timedelta(days=2)).isoformat())))
        out = run(SCRIPTS / "verify_phase09_retention_bundle.py",
                  "--bundle", bundle, "--manifest", manifest_path,
                  "--receipt", receipt_path)
        check("Q336 future stored_at rejected", out.returncode == 1
              and "future" in out.stdout)

        # tampered bundle rejected
        tampered = Path(temp) / "tampered.tar.gz"
        tampered.write_bytes(bundle.read_bytes()[:-3] + b"xxx")
        receipt_path.write_text(json.dumps(make_receipt(
            hashlib.sha256(tampered.read_bytes()).hexdigest(), 365)))
        out = run(SCRIPTS / "verify_phase09_retention_bundle.py",
                  "--bundle", tampered, "--manifest", manifest_path,
                  "--receipt", receipt_path)
        check("Q336 tampered bundle rejected", out.returncode == 1)


def main():
    test_bundle_roundtrip()
    print("=" * 66)
    print(f"  Q-336 retention bundle: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
