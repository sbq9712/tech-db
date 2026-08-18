#!/usr/bin/env python3
"""Verify the externally configured GitHub main-branch protection policy.

Exit 2 means BLOCKED_EXTERNAL_ACTION (usually no administrative token).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


REPO = os.environ.get("GITHUB_REPOSITORY", "sbq9712/tech-db")
REQUIRED = {
    "canonical-spec-lint", "acceptance-matrix", "unit-and-security",
    "mini-runtime-e2e", "critical-failure-injection",
    "synthetic-isolation", "fast-regression",
}


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("BLOCKED_EXTERNAL_ACTION: GH_TOKEN/GITHUB_TOKEN is unavailable")
        return 2
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/branches/main/protection",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            policy = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            print(f"BLOCKED_EXTERNAL_ACTION: cannot read main protection (HTTP {exc.code})")
            return 2
        raise
    checks = policy.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    contexts.update(item.get("context") for item in checks.get("checks") or [])
    missing = sorted(REQUIRED - contexts)
    reviews = policy.get("required_pull_request_reviews") or {}
    failures = []
    if missing:
        failures.append(f"missing required checks: {missing}")
    if checks.get("strict") is not True:
        failures.append("strict/up-to-date checks not required")
    if int(reviews.get("required_approving_review_count") or 0) < 1:
        failures.append("at least one approving review is not required")
    if (policy.get("enforce_admins") or {}).get("enabled") is not True:
        failures.append("administrators are not covered")
    if failures:
        print("GitHub policy FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("GitHub policy PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
