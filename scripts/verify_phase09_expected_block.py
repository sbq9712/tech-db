#!/usr/bin/env python3
"""Diagnostic: prove a red Phase09 gate is ONLY the genuine RT-101 absence.

Exit 0  when the recorded gate evidence shows: every required suite PASS,
every hard invariant satisfied, and the ONLY core reason is the unsatisfied
RT-101 owner-provisioned release authority (intentional fail-closed).
Exit 1  for any other failure signature (a real code regression or tampered
evidence) — never mask a regression as an intentional block.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from phase09_authority import RT101_AUTHORITY_ID  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path,
                        default=ROOT / "qa-backend/phase09_release_evidence.json")
    args = parser.parse_args()
    if not args.evidence.exists():
        print("EXPECTED_BLOCK_UNVERIFIABLE: gate evidence missing")
        return 1
    payload = json.loads(args.evidence.read_text("utf-8"))
    problems = []
    suites = payload.get("suite_evidence", [])
    for row in suites:
        if row.get("result") != "PASS":
            problems.append(f"suite {row.get('name')}: {row.get('result')}")
    invariants = payload.get("policy", {}).get("hard_invariants", {})
    for name in invariants:
        if f"hard invariant failed: {name}" in (payload.get("reasons") or []):
            problems.append(f"hard invariant failed: {name}")
    reasons = payload.get("reasons") or []
    authority_reasons = [r for r in reasons
                         if r.startswith(f"required authority unsatisfied: {RT101_AUTHORITY_ID}")]
    # D7 review F10: a policy edit stripping the mandatory authority makes
    # the gate emit "required authority undeclared in policy" — that is an
    # unexpected structural failure (evidence of tampering), never the
    # intentional genuine-authority absence this diagnostic certifies.
    other_reasons = [r for r in reasons
                     if not r.startswith("required authority unsatisfied:")]
    if other_reasons:
        problems.extend(other_reasons)
    if not authority_reasons:
        problems.append("no genuine RT-101 authority-absence reason present")
    authority_section = (payload.get("authorities") or {}).get(RT101_AUTHORITY_ID) or {}
    if authority_section.get("satisfied") is not False:
        problems.append("authority section missing or unexpectedly satisfied")
    if payload.get("core_eligible") is not False:
        problems.append("core_eligible is not false")
    if problems:
        print("UNEXPECTED_FAILURE: " + " | ".join(problems[:8]))
        return 1
    print(f"INTENTIONAL_FAIL_CLOSED: only genuine {RT101_AUTHORITY_ID} absence "
          f"(suites {len(suites)}/{len(suites)} PASS, invariants clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
