#!/usr/bin/env python3
"""Q-336 retention exporter — build the durable-retention bundle.

GitHub's public-repository artifact policy caps retention at 90 days; the
Phase09 gate requires >=180-day durable external retention (Q-336). This
script packs the canonical Phase09 evidence artifacts into one bundle with
a machine-readable manifest (per-file sha256 + bundle digest + git
provenance). The OWNER then uploads the bundle to a durable external
store (>=180 days) and collects that store's retention receipt; the
receipt is what the external-satisfaction proof binds via artifact_sha256.

This tool is code-side enablement only: it does NOT configure any store,
does NOT clear Q-336, and never fabricates a receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_SCHEMA_VERSION = "phase09-retention-bundle-1.0"
RETENTION_TARGET_DAYS = 180

BUNDLE_FILES = [
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


def git_head(root: Path, override: str | None = None) -> str:
    if override:
        return override.strip()
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             capture_output=True, text=True, check=True,
                             timeout=10)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--out", required=True, help="output .tar.gz path")
    parser.add_argument("--git-sha",
                        help="explicit provenance sha (use when the "
                             "bundle root is not a git worktree)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    present = [rel for rel in BUNDLE_FILES if (root / rel).is_file()]
    missing = [rel for rel in BUNDLE_FILES if rel not in present]
    files = []
    with tarfile.open(args.out, "w:gz") as bundle:
        for rel in present:
            path = root / rel
            data = path.read_bytes()
            files.append({
                "path": rel,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            bundle.add(path, arcname=rel)
    bundle_bytes = Path(args.out).read_bytes()
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head(root, args.git_sha),
        "retention_target_days": RETENTION_TARGET_DAYS,
        "files": files,
        "missing_files": missing,
        "bundle_bytes": len(bundle_bytes),
        "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
    }
    manifest_path = str(args.out) + ".manifest.json"
    Path(manifest_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({"bundle": str(args.out),
                      "manifest": manifest_path,
                      "files": len(files),
                      "missing": len(missing),
                      "bundle_sha256": manifest["bundle_sha256"]},
                     indent=2))
    # An INCOMPLETE bundle exports (for inspection) but fails: only a
    # complete evidence bundle may feed the Q-336 retention path.
    if missing:
        print(f"export_phase09_retention_bundle: bundle incomplete; "
              f"missing {len(missing)} expected artifact(s): "
              + ", ".join(missing), file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
