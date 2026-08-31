#!/usr/bin/env python3
"""Fail-closed authorization for every runtime publication path."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path,
                        default=ROOT / "qa-backend/phase09_release_evidence.json")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    payload = json.loads(args.evidence.read_text("utf-8"))
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    expected = args.expected_sha or head
    observed = payload.get("provenance", {}).get("git_sha")
    errors = []
    if expected != head:
        errors.append("requested publication SHA is not checked-out HEAD")
    if observed != head:
        errors.append("release evidence is stale for checked-out HEAD")
    if payload.get("production_release_eligible") is not True:
        errors.append("production_release_eligible is not true")
    if payload.get("external_blockers"):
        errors.append("external blockers remain")
    if errors:
        print("PUBLISH_DENIED: " + "; ".join(errors))
        return 1
    print(f"PUBLISH_AUTHORIZED {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
