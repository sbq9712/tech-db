#!/usr/bin/env python3
"""RT101 deploy-sync guard (V9 postmortem generalized repair, ONE-SHOT §8).

Postmortem (owner-v9-formal-20260915T070020Z, SCORER_INTEGRITY_VIOLATION,
consumed): the corpus-compatibility gate binds corpus/model/manifest bytes
exactly, but nothing verified that the SERVING runtime checkout carried the
evaluated code. The formal server's repo mirror sat at the V8-era head, so
committed zero-claim repairs were never loaded into the serving process and
claimless PARTIALLY_SUPPORTED terminals reached the frozen scorer again.

Generalized invariant (repo-owned, host-verifiable):

    A runtime repair that is not byte-present in the serving checkout is
    NOT deployed. Any formal-host environment carrying a serving runtime
    mirror must hold the repair-critical files byte-identical to the
    evaluated working tree.

This guard never mutates anything. It exits
  0  synced (or no serving mirror on this host — CI/agent-only hosts)
  2  DESYNC DETECTED — serving checkout is stale; formal capture FORBIDDEN
  3  usage error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# Repair-critical runtime files (V8/V9 postmortem surface). Deliberately
# explicit and small: these are the files whose stale copies produced the
# V8/V9 unscoreable payloads.
CRITICAL_FILES = (
    "qa-backend/answer_status.py",
    "qa-backend/phase02_pipeline.py",
    "qa-backend/claim_mapping.py",
    "qa-backend/server.py",
)

DEFAULT_MIRROR = "/home/rhett/rt101-v5-formal-runner/repo"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-repo", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--runtime-repo", default=os.environ.get(
        "RT101_RUNTIME_REPO", DEFAULT_MIRROR))
    ap.add_argument("--json-out", default=None,
                    help="optional path for the structured report")
    args = ap.parse_args(argv)

    src = Path(args.source_repo).resolve()
    mirror = Path(args.runtime_repo).resolve()

    if not (src / "qa-backend/server.py").is_file():
        print(f"source repo invalid: {src}", file=sys.stderr)
        return 3

    if not (mirror / ".git").is_dir() or not (mirror / "qa-backend").is_dir():
        # No serving runtime on this host (CI / agent-only): nothing to
        # enforce — the guard's contract is host-scoped by design.
        report = {"schema_version": "rt101-deploy-sync-1.0",
                  "checked": False,
                  "reason": "no serving runtime mirror on this host",
                  "runtime_repo": str(mirror), "synced": True,
                  "critical_files": {}}
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(report, indent=1, sort_keys=True))
        print("deploy-sync: no serving runtime mirror (host-scoped skip)")
        return 0

    desynced, files = [], {}
    for rel in CRITICAL_FILES:
        a, b = src / rel, mirror / rel
        if not b.is_file():
            desynced.append(f"{rel}: missing in serving checkout")
            files[rel] = {"source": None, "serving": None, "equal": False}
            continue
        sa, sb = sha256_file(a), sha256_file(b)
        equal = sa == sb
        files[rel] = {"source": sa, "serving": sb, "equal": equal}
        if not equal:
            desynced.append(f"{rel}: serving checkout is STALE")

    report = {"schema_version": "rt101-deploy-sync-1.0",
              "checked": True, "source_repo": str(src),
              "runtime_repo": str(mirror),
              "synced": not desynced, "desync": desynced,
              "critical_files": files}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1,
                                                  sort_keys=True))
    if desynced:
        print("FAIL_CLOSED: runtime deploy DESYNC — formal capture forbidden",
              file=sys.stderr)
        for d in desynced:
            print(f"  - {d}", file=sys.stderr)
        return 2
    print("deploy-sync: serving checkout byte-identical for all "
          f"{len(CRITICAL_FILES)} critical files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
