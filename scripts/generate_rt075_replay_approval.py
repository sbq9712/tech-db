#!/usr/bin/env python3
"""Generate the RT-075 equivalent-locked-replay APPROVAL REQUEST artifact.

Binds, by sha256: the sealed replay dataset, the replay report artifact,
the sha-pinned production corpus/registry provenance, and the verbatim
independent Codex review outputs. The artifact ships with
approval.state = PENDING_OWNER_APPROVAL — it never asserts approval.

The OWNER's single approval operation (never performable by agents,
fail-closed in phase09_release.load_external_blockers):
  1. provision PHASE09_EXTERNAL_SATISFACTION_PROOFS +
     PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY with the RT-075 entry
     declaring artifact_sha256 = THIS FILE's sha256 (printed on stderr),
  2. set the RT-075 row satisfied=true in spec/phase09_external_state.json
     with satisfaction_proof {artifact: <this file>, sha256: <digest>}.

No signature is forged here; the artifact is a REQUEST, not an approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "rt075-equivalent-replay-approval-1.0"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_verdict(text: str) -> str:
    """Only a BARE verdict token counts — prompt-template echoes like
    'VERDICT: ACCEPT | ACCEPT_WITH_FINDINGS | REJECT' are NOT_EMITTED."""
    matches = re.findall(r"^\s*VERDICT:\s*(.+?)\s*$", text, re.M)
    for candidate in reversed(matches):
        token = candidate.strip()
        if token in ("ACCEPT", "ACCEPT_WITH_FINDINGS", "REJECT"):
            return token
    return "NOT_EMITTED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True,
                        help="replay report JSON (owner-side)")
    parser.add_argument("--dataset",
                        help="dataset JSON (optional cross-check)")
    parser.add_argument("--codex-output", action="append", default=[],
                        help="codex review output file (repeatable)")
    parser.add_argument("--codex-role", action="append", default=[],
                        help="role label matching each --codex-output")
    parser.add_argument("--out", required=True,
                        help="artifact path (committed to the repo)")
    args = parser.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("artifact_sha256") or \
            report.get("schema_version") != "rt075-replay-report-1.0":
        raise SystemExit("unrecognized replay report schema")
    dataset_sha = report["dataset_sha256"]
    if args.dataset:
        # the seal is a CANONICAL-JSON digest of the body without the
        # seal field (same construction as the builder's sealing step)
        dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
        body = {k: v for k, v in dataset.items() if k != "dataset_sha256"}
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()
        if actual != dataset_sha or \
                dataset.get("dataset_sha256") != dataset_sha:
            raise SystemExit(
                f"dataset seal drift: report binds {dataset_sha}, "
                f"canonical seal of file is {actual}")
    sessions = []
    for i, path in enumerate(args.codex_output):
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        role = args.codex_role[i] if i < len(args.codex_role) else \
            f"session-{i+1}"
        sessions.append({
            "role": role,
            "file": p.name,
            "sha256": sha256_file(p),
            "verdict": extract_verdict(text),
        })
    try:
        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True, timeout=10).stdout.strip()
    except Exception:
        git_head = "unknown"

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "RT075_EQUIVALENT_LOCKED_REPLAY_APPROVAL_REQUEST",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head,
        "spec_authority": {
            "execution_tickets_L920":
                ">=1,000 representative events + 7 days or approved "
                "equivalent replay before activation",
            "final_spec_L1217":
                "representative shadow window: >=1,000 events and 7 "
                "days, or equivalent locked replay + explicit approval",
            "final_spec_L1192":
                "Low-traffic exception requires equivalent locked "
                "replay plus explicit approval, not zero evidence",
        },
        "evidence": {
            "replay_dataset_sha256": dataset_sha,
            "replay_report_artifact_sha256": report["artifact_sha256"],
            "replay_corpus_sha256":
                report["corpus"]["sha256"],
            "replay_corpus_record_count":
                report["corpus"]["record_count"],
            "replay_registry_sha256": report["registry_sha256"],
            "identity_snapshot_id": report["identity_snapshot_id"],
            "replay_case_count": report["case_count"],
            "replay_observation_count": report["observation_count"],
            "replay_decision_counts": report["decision_counts"],
            "verifier_verdicts": report["verdicts"],
            "equivalent_window_gate": report["equivalent_window_gate"],
            "dataset_stored_at":
                "owner-side locked storage outside the repository; "
                "dataset carries locators only (no record text in the "
                "repository)",
            "codex_independent_review": {
                "tool": "codex exec --sandbox read-only",
                "sessions": sessions,
                "verbatim_outputs":
                    "archived owner-side under tech-db-overnight/"
                    "codex-K3/ (outside the repository)",
            },
        },
        "approval": {
            "state": "PENDING_OWNER_APPROVAL",
            "approver": None,
            "approved_at_utc": None,
            "mechanism": (
                "owner provisions PHASE09_EXTERNAL_SATISFACTION_PROOFS + "
                "PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY with the RT-075 "
                "entry declaring artifact_sha256 = this file's sha256, "
                "and sets the RT-075 row satisfied=true in "
                "spec/phase09_external_state.json with satisfaction_proof "
                "{artifact: this file, sha256: <this file's sha256>}"),
            "fallback_if_not_approved":
                "7-day live production shadow (Path A) remains the "
                "required completion path",
            "note": (
                "This artifact is a REQUEST. It never asserts approval; "
                "no signature is forged; repo-side or agent-side edits "
                "cannot clear RT-075 (fail-closed in "
                "phase09_release.load_external_blockers)."),
        },
    }
    body = json.dumps(artifact, ensure_ascii=False, indent=2,
                      sort_keys=False) + "\n"
    out = Path(args.out)
    out.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"approval artifact written: {out}")
    sys.stderr.write(
        f"rt075 approval artifact sha256 (owner provisions THIS): "
        f"{digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
