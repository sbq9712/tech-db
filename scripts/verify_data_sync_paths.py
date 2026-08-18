#!/usr/bin/env python3
"""Fail closed unless an automation diff contains data-sync paths only."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_EXACT = {".pipeline_state.json"}
ALLOWED_PREFIXES = ("data/processed/", "data/reports/")
PROTECTED_PREFIXES = ("docs/remediation/", "spec/", "qa-backend/", "scripts/", ".github/")


def allowed(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized in ALLOWED_EXACT or normalized.startswith(ALLOWED_PREFIXES)


def validate(paths: list[str]) -> list[str]:
    return sorted(path for path in paths if path and not allowed(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    paths = args.paths
    if not paths:
        output = subprocess.check_output(
            ["git", "diff", "--name-only", f"{args.base}...HEAD"], text=True)
        paths = output.splitlines()
    blocked = validate(paths)
    if blocked:
        print("data-sync policy BLOCKED paths:")
        for path in blocked:
            print(f"  {path}")
        return 1
    print(f"data-sync policy PASS: {len(paths)} path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
