#!/usr/bin/env python3
"""Materialize the honest pre-remediation baseline and gap report.

This script only aggregates measured, already-committed artifacts.  It never
labels a mini-fixture or historical nightly run as fresh production traffic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "qa-backend" / "test_fixtures" / "remediation" / "baseline_phase00.json"
REPORT = ROOT / "docs" / "remediation" / "phase00_gap_report.md"
LOCKED_SOURCE_METADATA = {
    # The reviewed Phase-00 start commit may not exist in a pull-request
    # checkout (actions/checkout checks out only the synthetic merge commit by
    # default).  Its immutable commit timestamp is therefore part of the
    # baseline input, rather than an implicit dependency on local git history.
    "3439c27bcbd0087b9ee46d86aac4384fa9fcc74b": {
        "committed_at": "2026-08-18T13:45:18+08:00",
    },
}


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text("utf-8"))


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def source_commit_time(source_sha: str) -> str:
    """Resolve a commit time without requiring the locked commit object.

    Known baseline commits use checked-in immutable metadata.  Other SHAs may
    still be captured from a full local clone, but never silently inherit the
    current checkout's timestamp.
    """
    locked = LOCKED_SOURCE_METADATA.get(source_sha)
    if locked:
        return locked["committed_at"]
    try:
        return git("show", "-s", "--format=%cI", source_sha)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"source commit {source_sha} is unavailable; add reviewed immutable "
            "metadata before capturing it from a shallow checkout"
        ) from exc


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source_sha: str) -> tuple[dict, str]:
    spec = load("spec/spec_manifest.json")
    mini = load("qa-backend/test_fixtures/mini_runtime/manifest.json")
    raw_routes = load("qa-backend/test_fixtures/parity/baseline_mini.json")
    legacy = load("qa-backend/test_fixtures/parity/baseline_hybrid_legacy.json")
    agentic = load("qa-backend/test_fixtures/nightly/eval_report.json")
    latency = load("qa-backend/test_fixtures/ttfb/baseline_legacy.json")
    commit_time = source_commit_time(source_sha)

    def result_counts(doc: dict, field: str) -> dict:
        counts = [len(item.get(field, [])) for item in doc.get("results", [])]
        return {
            "query_count": len(counts),
            "mean_returned": round(sum(counts) / len(counts), 3) if counts else 0,
            "min_returned": min(counts) if counts else 0,
            "max_returned": max(counts) if counts else 0,
        }

    baseline = {
        "schema_version": "1.0.0",
        "baseline_id": f"phase00-{source_sha[:12]}",
        "captured_at": commit_time,
        "git_sha": source_sha,
        "environment": "committed-mini-runtime-plus-historical-locked-artifacts",
        "fresh_production_traffic": False,
        "spec_version": spec["spec_version"],
        "spec_sha256": spec["spec_sha256"],
        "decision_register_sha256": spec["decision_register_sha256"],
        "dataset_snapshot_id": mini["dataset_snapshot_id"],
        "identity_snapshot_id": mini["identity_snapshot_id"],
        "model_versions": {
            "embedding": "bge-m3 (frozen query/index embeddings)",
            "generator": "glm-5.2 (historical nightly artifact only)",
        },
        "config_versions": {
            "profile_registry": spec["profile_registry_version"],
            "ticket_registry": spec["ticket_registry_version"],
        },
        "paths": {
            "old_rrf_top25": {
                **result_counts(legacy, "rrf"),
                "artifact": "qa-backend/test_fixtures/parity/baseline_hybrid_legacy.json",
                "note": "legacy RRF artifact; some queries contain 25 fused candidates",
            },
            "legacy_hybrid_profile": {
                **result_counts(legacy, "rrf"),
                "profile": "legacy_hybrid",
                "artifact": "qa-backend/test_fixtures/parity/baseline_hybrid_legacy.json",
            },
            "current_agentic_path": {
                "profile": "agentic_full historical nightly mini run",
                "query_count": agentic.get("n"),
                "retrieval": {
                    "mean_returned": agentic.get("metrics", {}).get("mean_retrieval_n"),
                },
                "answer": {
                    "ok_rate": agentic.get("metrics", {}).get("ok_rate"),
                    "status_distribution": agentic.get("metrics", {}).get("answer_status_dist"),
                },
                "citation": {
                    "mean_cited": agentic.get("metrics", {}).get("mean_cited_n"),
                    "mean_grounded_rate": agentic.get("metrics", {}).get("mean_grounded_rate"),
                },
                "latency_ms": {
                    "mean_retrieval": agentic.get("metrics", {}).get("mean_retrieval_ms"),
                    "mean_generation": agentic.get("metrics", {}).get("mean_gen_ms"),
                },
                "error_rate": round(1 - float(agentic.get("metrics", {}).get("ok_rate", 0)), 6),
                "artifact": "qa-backend/test_fixtures/nightly/eval_report.json",
            },
        },
        "legacy_latency_ms": {
            "n": latency.get("n"), "p50": latency.get("p50_ms"),
            "p90": latency.get("p90_ms"), "p99": latency.get("p99_ms"),
            "artifact": "qa-backend/test_fixtures/ttfb/baseline_legacy.json",
        },
        "artifact_hashes": {
            "raw_routes": digest(ROOT / "qa-backend/test_fixtures/parity/baseline_mini.json"),
            "legacy": digest(ROOT / "qa-backend/test_fixtures/parity/baseline_hybrid_legacy.json"),
            "agentic": digest(ROOT / "qa-backend/test_fixtures/nightly/eval_report.json"),
            "latency": digest(ROOT / "qa-backend/test_fixtures/ttfb/baseline_legacy.json"),
        },
        "limitations": [
            "No fresh production request replay was available in this checkout.",
            "Answer/citation/error metrics are historical nightly mini-runtime measurements.",
            "This artifact is a locked pre-remediation reference, not evidence of production readiness.",
        ],
    }
    report = f"""# Phase 00 current-state gap report

Baseline: `qa-backend/test_fixtures/remediation/baseline_phase00.json`  
Reviewed start SHA: `{source_sha}`  
Environment: committed mini runtime plus locked historical artifacts; **no fresh production traffic was claimed**.

## Confirmed gaps at the reviewed HEAD

| Area | Evidence at start SHA | Phase-00 disposition |
|---|---|---|
| Normative authority | Final spec and Decision Register were absent from the repository and the manifest did not bind their hashes. | Fixed by RT-001 artifacts and lint. |
| Final Acceptance | `tests_final_acceptance.py` contained `or True`, import-only checks, hardcoded `True`, and a manually fabricated E2E Trace. | Replaced under RT-002 by matrix validation and real suite execution. |
| Runtime fixture | The mini index had Vector/BM25 files but no stable record IDs, immutable SourceSnapshots, identity snapshot, chunks, evidence metadata, or complete pinned manifest. | Added under RT-003. |
| Baseline provenance | Existing parity/nightly artifacts were fragmented and did not bind spec, decision, dataset, identity, model, config, and git SHA in one schema. | Aggregated honestly under RT-004. |
| Main protection | Repository metadata showed no verifiable required-check protection, and auto-sync pushed directly to `main`. | Workflow/policy remediation is implemented; enabling the GitHub ruleset remains an external action. |

## Known non-Phase-00 remediation risks

The audit starting points in the frozen prompt remain open until their later RT tickets pass: stable production identity, synthetic-summary isolation in primary indexes, atomic release pinning, exact normalization mapping, fail-safe FAST_RAG behavior, Evidence Package-only generation, verification default semantics, exact citation filtering, deterministic AnswerStateMachine ownership, and Graph-V2 gain-gated activation.

## Reproducibility

Run `python scripts/capture_phase00_baseline.py --source-sha {source_sha}`. The JSON output is deterministic for the locked input artifacts and source SHA.
"""
    return baseline, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    source_sha = args.source_sha or git("merge-base", "origin/main", "HEAD")
    baseline, report = build(source_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", "utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, "utf-8")
    print(f"wrote {args.output} and {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
