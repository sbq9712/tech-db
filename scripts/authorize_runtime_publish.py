#!/usr/bin/env python3
"""Fail-closed authorization for every runtime publication path.

D7 hardening: the recorded evidence file alone never authorizes
publication.  The RT-101 release authority and every external blocker are
independently re-verified from the owner-controlled environment channel at
authorization time, so fabricated or stale "green" evidence cannot clear
the publication path.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from phase09_authority import (  # noqa: E402
    authority_results_from_env,
    external_satisfaction_proofs_from_env,
    validate_authority_requirements,
)
from phase09_release import (  # noqa: E402
    MANDATORY_AUTHORITIES,
    load_external_blockers,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path,
                        default=ROOT / "qa-backend/phase09_release_evidence.json")
    parser.add_argument("--expected-sha")
    parser.add_argument("--policy", type=Path,
                        default=ROOT / "spec/phase09_release_policy.json",
                        help="policy file to validate against (default: the "
                             "repo policy; tests may point at a temp copy)")
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
    if payload.get("core_eligible") is not True:
        errors.append("core_eligible is not true")
    if payload.get("production_release_eligible") is not True:
        errors.append("production_release_eligible is not true")
    if payload.get("external_blockers"):
        errors.append("external blockers remain")
    # Independent fresh re-verification (D7): repo files and recorded
    # evidence cannot fake these inputs; only the owner env channel can.
    try:
        policy = json.loads(args.policy.read_text("utf-8"))
        requirements = dict(policy.get("required_authorities", {}))
        # D7 review F1: the mandatory authority set is pinned at code level,
        # NOT inherited from the policy — a repo commit stripping RT-101
        # from required_authorities can never weaken this authorization.
        undeclared = sorted(MANDATORY_AUTHORITIES - set(requirements))
        if undeclared:
            errors.append("required authority undeclared in policy: "
                          + ", ".join(undeclared))
        validate_authority_requirements(requirements)
        fresh_authority = authority_results_from_env(requirements, root=ROOT)
        for authority_id in sorted(MANDATORY_AUTHORITIES | set(requirements)):
            result = fresh_authority.get(authority_id)
            if result is None or not result.satisfied:
                reasons = getattr(result, "reasons", None) or [
                    "required authority undeclared in policy"
                    if authority_id not in requirements
                    else "authority result missing"]
                errors.append(
                    f"required authority unsatisfied: {authority_id}: "
                    + "; ".join(reasons))
        fresh_blockers = load_external_blockers(
            ROOT / policy["external_state"],
            owner_proofs=external_satisfaction_proofs_from_env(root=ROOT))
        if fresh_blockers:
            errors.append("external blockers remain (freshly re-verified): "
                          + ", ".join(sorted(fresh_blockers)))
    except ValueError as exc:
        errors.append(f"authority/external-state validation failed: {exc}")
    if errors:
        print("PUBLISH_DENIED: " + "; ".join(errors))
        return 1
    print(f"PUBLISH_AUTHORIZED {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
