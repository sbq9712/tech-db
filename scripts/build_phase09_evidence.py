#!/usr/bin/env python3
"""One-way authoritative Phase09 evidence-chain generator (D7, P1 closure).

Derivation direction (never reversed):

    real test execution (run_all_tests.py, clean tree at tested_git_sha)
      -> qa-backend/test_summary.json            (authoritative counts)
      -> canonical Phase09 release gate run      (release + ticket evidence)
      -> docs/remediation/phase09_PHASE_RESULT.json
      -> docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json
      -> docs/remediation/phase09_completion_report.md (human view)

All machine facts (counts, hashes, eligibility, phase status, blockers,
graph state, NEXT_PROMPT_ALLOWED) are derived here from the upstream
artifacts.  Downstream documents must never be hand-edited;
scripts/validate_phase09_evidence_chain.py enforces this.

SHA semantics (no self-referential fixed point is claimed):
  tested_git_sha              HEAD of the clean tree the push tier ran on
  evidence_generation_base_sha HEAD where this generator ran (a descendant
                               of tested_git_sha whose diff touches only
                               chain-owned evidence files)
  The evidence *commit* that stores these files is a descendant commit
  that adds evidence only; it never claims to have tested itself.  CI at
  the exact head binds exact-head evidence via its own artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from phase09_authority import (  # noqa: E402
    RT075_UNBLOCK_RULE,
    RT101_AUTHORITY_ID,
    authority_results_from_env,
    external_satisfaction_proofs_from_env,
)
from phase09_release import load_external_blockers  # noqa: E402

PHASE_RESULT_SCHEMA = "phase09-phase-result-2.0"
NEXT_PROMPT_SCHEMA = "phase09-next-prompt-gate-2.0"

# Files the evidence chain itself owns: the diff between the tested SHA
# and the generation base (and the dirty set at generation time) may only
# touch these evidence artifacts; anything else requires a fresh tier run.
OWNED_EVIDENCE_PATHS = frozenset({
    "qa-backend/test_summary.json", "qa-backend/test_summary.previous.json",
    "qa-backend/benchmark_phase09_result.json",
    "qa-backend/benchmark_phase03_production_result.json",
    "qa-backend/benchmark_phase03_result.json",
    "qa-backend/benchmark_phase04_result.json",
    "qa-backend/benchmark_phase05_result.json",
    "qa-backend/benchmark_phase06_result.json",
    "qa-backend/benchmark_phase07_result.json",
    "qa-backend/rt101_general_reliability_repair_result.json",
    "docs/remediation/phase09_PHASE_RESULT.json",
    "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json",
    "docs/remediation/phase09_completion_report.md",
    # Evidence-chain tooling: drift here between the tested sha and the
    # generation base is acceptable because the release suite exercises
    # this tooling hermetically at the tested sha and CI re-executes the
    # validator fresh at the exact pushed head.  Product code can never
    # hide behind these entries.
    "scripts/build_phase09_evidence.py",
    "scripts/validate_phase09_evidence_chain.py",
    "scripts/verify_phase09_expected_block.py",
    # D7 process-evidence documents: archived Codex rounds, decision
    # register entries, and the gatekeeper verdict history.  Narrative
    # provenance for the chain — never product code.
    "docs/remediation/phase09_D7_codex_authority_design.md",
    "docs/remediation/phase09_D7_codex_authority_design_prompt.md",
    "docs/remediation/phase09_D7_codex_code_review.md",
    "docs/remediation/phase09_D7_codex_gatekeeper.md",
    "docs/remediation/phase09_gatekeeper_history.json",
    "docs/remediation/phase09_RT101_provisioning.md",
    "docs/remediation/decision_register.md",
    "docs/remediation/phase09_PR_BODY.md",
})

SHA_SEMANTICS = {
    "tested_git_sha": "HEAD of the clean worktree the authoritative test "
                      "execution (push tier) ran on",
    "evidence_generation_base_sha": "HEAD where the release gate and this "
                                    "generator executed; a descendant of "
                                    "tested_git_sha whose diff touches only "
                                    "chain-owned evidence files and "
                                    "evidence-chain tooling scripts",
    "evidence_commit_sha": "the descendant commit that stores the generated "
                           "evidence files; adds evidence only and is NOT "
                           "claimed to have been tested by this chain",
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT,
                                   text=True).strip()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(msg: str) -> None:
    raise SystemExit(f"build_phase09_evidence: {msg}")


def build_next_prompt(*, phase_result: dict, decision: dict,
                      authority_results: dict, tested_sha: str,
                      evidence_generation_base_sha: str,
                      generated_at: str) -> dict:
    """Derive the canonical NEXT_PROMPT gate artifact.

    NEXT_PROMPT_ALLOWED requires: phase PASS + core eligible + production
    release eligible + no external blocker + every required authority
    satisfied (D7). The RT-075 unblock condition is the registered
    authority rule (phase09_authority.RT075_UNBLOCK_RULE), never a
    locally re-worded copy.
    """
    next_allowed = bool(
        phase_result["phase_status"] == "PASS"
        and decision.get("core_eligible") is True
        and decision.get("production_release_eligible") is True
        and not decision.get("external_blockers")
        and all(result.satisfied for result in authority_results.values())
    )
    reasons = []
    if phase_result["phase_status"] != "PASS":
        reasons.append(f"phase_status={phase_result['phase_status']} (not PASS)")
    if decision.get("core_eligible") is not True:
        reasons.append("core_eligible=false: " + "; ".join(decision.get("reasons", [])))
    if decision.get("external_blockers"):
        reasons.append("external blockers: " + ", ".join(sorted(decision["external_blockers"])))
    for authority_id, result in authority_results.items():
        if not result.satisfied:
            reasons.append(f"required authority unsatisfied: {authority_id}")

    return {
        "schema_version": NEXT_PROMPT_SCHEMA,
        "phase": "Phase09",
        "generated_at": generated_at,
        "tested_git_sha": tested_sha,
        "evidence_generation_base_sha": evidence_generation_base_sha,
        "NEXT_PROMPT_ALLOWED": next_allowed,
        "why": reasons,
        "phase_status": phase_result["phase_status"],
        "core_eligible": decision.get("core_eligible"),
        "production_release_eligible": decision.get("production_release_eligible"),
        "external_blockers": sorted(decision.get("external_blockers", [])),
        "graph": phase_result["graph"],
        "required_authorities": {
            authority_id: {"satisfied": result.satisfied}
            for authority_id, result in authority_results.items()
        },
        "unblock_conditions": [
            "Provision genuine RT-101 answer-level blinded release-holdout "
            "authority via the owner environment channel (see "
            "docs/remediation/phase09_RT101_provisioning.md); it cannot be "
            "provisioned by repository edits or by an agent.",
            "Clear Q-336 with durable >=180d artifact retention.",
            "Clear RT-005 with repository-admin branch-protection "
            "(enforce admins) configuration.",
            RT075_UNBLOCK_RULE,
        ],
        "forbidden": [
            "Fabricating or approximating blinded holdout gold",
            "Flipping external-state or authority status via repository "
            "edits (fail-closed by design since D7)",
            "Loosening required_authorities / hard invariants to force "
            "eligibility",
            "Starting Phase10 while NEXT_PROMPT_ALLOWED=false",
        ],
        "phase10": "NOT_STARTED" if not next_allowed else "ELIGIBLE_TO_START",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path,
                        default=ROOT / "qa-backend/test_summary.json")
    parser.add_argument("--release-evidence", type=Path,
                        default=ROOT / "qa-backend/phase09_release_evidence.json")
    parser.add_argument("--ticket-status", type=Path,
                        default=ROOT / "qa-backend/phase09_ticket_status.json")
    parser.add_argument("--history", type=Path,
                        default=ROOT / "docs/remediation/phase09_gatekeeper_history.json")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "docs/remediation")
    args = parser.parse_args()

    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    summary_str = str(args.summary)
    # Hermetic-test escape hatch: only when the caller opts in AND every
    # input artifact lives outside the repository (temp chain fixtures).
    # Real regeneration on a dirty repo worktree still fails closed.
    inputs_outside_repo = not any(
        str(path).startswith(str(ROOT))
        for path in (args.summary, args.release_evidence, args.ticket_status))
    hermetic_tests = (os.environ.get("PHASE09_EVIDENCE_HERMETIC_TEST") == "1"
                      and inputs_outside_repo)
    # Dirty-tolerant generation (see SHA semantics below): once the tier
    # summary/artifacts are committed, HEAD has advanced and the docs
    # being generated are themselves pending changes.  The *tested run*
    # must still be clean — enforced via the summary's worktree_dirty
    # field below.  Anything beyond chain-owned evidence files staying
    # dirty at generation time remains a hard failure.
    if dirty and not hermetic_tests:
        unowned = [p for p in git("status", "--porcelain").splitlines()
                   if p[3:].strip() not in OWNED_EVIDENCE_PATHS]
        if unowned:
            fail("worktree has non-evidence changes; commit code first, "
                 f"then run tests and generate evidence: {unowned}")

    summary = json.loads(args.summary.read_text("utf-8"))
    if summary.get("all_passed") is not True or summary.get("total_failed") != 0:
        fail("test_summary.json is not an all-pass summary; refusing to "
             "generate Phase09 evidence from failing tests")
    tested_sha = summary.get("git_sha")
    if not tested_sha or tested_sha == "unknown":
        fail("test_summary.json lacks git_sha binding (regenerate with the "
             "updated run_all_tests.py)")
    if summary.get("worktree_dirty") is not False:
        fail("summary was produced on a dirty worktree; re-run at clean HEAD")

    # SHA semantics (see sha_semantics below): the tier runs on the clean
    # code-final commit `tested_sha`; committing the summary/artifacts
    # necessarily advances HEAD to an evidence-only descendant.  No fixed
    # point is claimed: the generation HEAD must be a descendant of the
    # tested SHA and may differ from it ONLY in chain-owned evidence files
    # and evidence-chain tooling scripts.
    if tested_sha != head:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", tested_sha, head],
            cwd=ROOT, capture_output=True)
        if ancestor.returncode != 0:
            fail(f"summary git_sha {tested_sha} is not an ancestor of the "
                 f"generation HEAD {head}")
        drift = git("diff", "--name-only", tested_sha, head).splitlines()
        unowned = [p for p in drift if p not in OWNED_EVIDENCE_PATHS]
        if unowned:
            fail("diff between tested SHA and generation HEAD touches "
                 f"non-evidence files {unowned}; re-run the push tier at "
                 "the current HEAD first")
    evidence_generation_base_sha = head

    if not args.release_evidence.exists() or not args.ticket_status.exists():
        fail("run scripts/run_phase09_release_gate.py first; release/ticket "
             "evidence is required input")
    evidence = json.loads(args.release_evidence.read_text("utf-8"))
    ticket = json.loads(args.ticket_status.read_text("utf-8"))
    evidence_prov_sha = (evidence.get("provenance") or {}).get("git_sha")
    if evidence_prov_sha != head:
        fail(f"release evidence provenance git_sha {evidence_prov_sha} != HEAD {head}")

    policy = json.loads((ROOT / "spec/phase09_release_policy.json").read_text("utf-8"))
    external_state = json.loads(
        (ROOT / policy["external_state"]).read_text("utf-8"))

    # Fresh authority verification (env channel) — recorded, never fabricated.
    authority_results = authority_results_from_env(
        policy.get("required_authorities", {}), root=ROOT)
    fresh_blockers = load_external_blockers(
        ROOT / policy["external_state"],
        owner_proofs=external_satisfaction_proofs_from_env(root=ROOT))

    decision = evidence["release_decision"] if "release_decision" in evidence else {
        k: evidence[k] for k in ("core_eligible", "production_release_eligible",
                                 "graph_activation_eligible", "graph_state",
                                 "reasons", "external_blockers")}
    # D7 review F5: divergence must fail regardless of which side is empty
    # — an evidence file that silently dropped all blockers (or a state
    # file that silently gained one) is drift, not agreement.
    if sorted(decision.get("external_blockers") or []) != sorted(fresh_blockers):
        fail("recorded external blockers diverge from freshly verified "
             f"blockers: recorded={sorted(decision.get('external_blockers') or [])} "
             f"fresh={sorted(fresh_blockers)}")

    suites_by_tag = {s["tag"]: s for s in summary.get("suites", [])}
    phase09_suites = {}
    for name in policy["required_suites"]:
        row = suites_by_tag.get(name)
        phase09_suites[name] = (f"{row['passed']}/{row['passed'] + row['failed']}"
                                if row else "MISSING")

    history = json.loads(args.history.read_text("utf-8")) if args.history.exists() else {}

    generated_at = datetime.now(timezone.utc).isoformat()
    summary_sha = sha256_file(args.summary)
    now = datetime.now(timezone.utc)

    unsatisfied_authorities = {
        authority_id: {
            **result.to_sanitized_dict(),
            "requirement": policy["required_authorities"][authority_id],
        }
        for authority_id, result in authority_results.items()
    }

    phase_result = {
        "schema_version": PHASE_RESULT_SCHEMA,
        "phase": "Phase09",
        "title": "Benchmarks, CI, release gates + runtime-budget repair",
        "generated_at": generated_at,
        "tested_git_sha": tested_sha,
        "evidence_generation_base_sha": evidence_generation_base_sha,
        "sha_semantics": SHA_SEMANTICS,
        "test_summary": {
            "path": os.path.relpath(args.summary, ROOT),
            "sha256": summary_sha,
            "total_passed": summary["total_passed"],
            "total_failed": summary["total_failed"],
            "suite_count": len(summary.get("suites", [])),
            "all_passed": summary["all_passed"],
            "generated_at": summary.get("generated_at"),
            "git_sha": tested_sha,
        },
        "release_decision": decision,
        "authorities": unsatisfied_authorities,
        "phase_status": ticket["phase_status"],
        "ticket_status_path": os.path.relpath(args.ticket_status, ROOT),
        "external_blockers": {
            control_id: {
                "description": row.get("description"),
                "satisfied": row.get("satisfied") is True,
                "observed_at": (row.get("evidence") or {}).get("observed_at"),
                "re_verified_at": (row.get("evidence") or {}).get("re_verified_at"),
            }
            for control_id, row in external_state.get("controls", {}).items()
        },
        "graph": {
            "gain_conclusion": policy["graph_gain_conclusion"],
            "state": decision["graph_state"],
            "activation": "NOT_ACTIVATED_BY_GAIN_GATE",
        },
        "next_prompt_artifact": "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json",
        "evidence_chain": [
            {"path": "qa-backend/test_summary.json", "storage": "committed",
             "sha256": summary_sha, "role": "authoritative test counts"},
            {"path": os.path.relpath(args.release_evidence, ROOT),
             "storage": "ci_generated", "role": "canonical release decision",
             "sha256_at_generation": sha256_file(args.release_evidence),
             "generated_at": evidence.get("generated_at")},
            {"path": os.path.relpath(args.ticket_status, ROOT),
             "storage": "ci_generated", "role": "ticket/phase status",
             "sha256_at_generation": sha256_file(args.ticket_status),
             "generated_at": ticket.get("generated_at")},
            {"path": "docs/remediation/phase09_PHASE_RESULT.json",
             "storage": "committed", "role": "this file"},
            {"path": "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json",
             "storage": "committed", "role": "next-prompt gate"},
            {"path": "docs/remediation/phase09_completion_report.md",
             "storage": "committed", "role": "human-readable view"},
        ],
        "gatekeeper_history": history,
        "build": {
            "generator": "scripts/build_phase09_evidence.py",
            "builder": "Phase09 D7 evidence closure",
            "domain_now": now.isoformat(),
        },
    }

    next_prompt = build_next_prompt(
        phase_result=phase_result,
        decision=decision,
        authority_results=authority_results,
        tested_sha=tested_sha,
        evidence_generation_base_sha=evidence_generation_base_sha,
        generated_at=generated_at,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "phase09_PHASE_RESULT.json").write_text(
        json.dumps(phase_result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    (args.out_dir / "phase09_NEXT_PROMPT_ALLOWED.json").write_text(
        json.dumps(next_prompt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    (args.out_dir / "phase09_completion_report.md").write_text(
        render_report(phase_result, next_prompt), encoding="utf-8")
    print(f"evidence regenerated at tested_git_sha={tested_sha} "
          f"generation_base={evidence_generation_base_sha} "
          f"phase_status={phase_result['phase_status']} "
          f"NEXT_PROMPT_ALLOWED={next_prompt['NEXT_PROMPT_ALLOWED']}")
    return 0


def render_report(phase_result: dict, next_prompt: dict) -> str:
    ts = phase_result["test_summary"]
    decision = phase_result["release_decision"]
    blockers = phase_result["external_blockers"]
    authority = phase_result["authorities"].get(RT101_AUTHORITY_ID, {})
    gate = next_prompt
    blocker_lines = "\n".join(
        f"- **{cid}**: {row.get('description')} "
        f"(satisfied={str(row.get('satisfied')).lower()})" for cid, row in blockers.items())
    machine = "\n".join([
        f"tested_git_sha: {phase_result['tested_git_sha']}",
        f"evidence_generation_base_sha: {phase_result['evidence_generation_base_sha']}",
        f"test_summary_sha256: {ts['sha256']}",
        f"total: {ts['total_passed']} passed, {ts['total_failed']} failed across {ts['suite_count']} suites",
        f"phase_status: {phase_result['phase_status']}",
        f"core_eligible: {str(decision['core_eligible']).lower()}",
        f"production_release_eligible: {str(decision['production_release_eligible']).lower()}",
        f"graph_state: {phase_result['graph']['state']}",
        f"external_blockers: [{', '.join(sorted(blockers))}]",
        f"rt101_authority_satisfied: {str(authority.get('satisfied', False)).lower()}",
        f"NEXT_PROMPT_ALLOWED: {str(gate['NEXT_PROMPT_ALLOWED']).lower()}",
        f"phase10: {gate['phase10']}",
    ])
    return f"""# Phase09 — Benchmarks, CI, release gates

This document is a generated human-readable view.  Machine authority is the
evidence chain: `qa-backend/test_summary.json` -> canonical release gate
artifacts -> `phase09_PHASE_RESULT.json` -> `phase09_NEXT_PROMPT_ALLOWED.json`.
It is regenerated by `scripts/build_phase09_evidence.py` and validated by
`scripts/validate_phase09_evidence_chain.py`; hand edits fail validation.

## Scope

- RT-100: locked retrieval/reranker/evidence benchmark
- RT-101: answer/citation/abstention hard gates
- RT-102: standard Research versus multi-document benchmark
- RT-103: deterministic ER benchmark with RT-075 dependency preserved
- RT-104: real `/api/chat/stream` and canonical orchestrator E2E
- RT-105: integrated failure injection
- RT-106: PR/nightly/release CI tiers and artifact provenance
- RT-107: single fail-closed release evaluator
- RT-108: ticket status derived from executable evidence

<!-- machine-block begin (generated — do not hand-edit) -->
```text
{machine}
```
<!-- machine-block end -->

## Machine result

The deterministic Phase09 release gate reports `core_eligible = false`:
the RT-101 answer-level blinded release-holdout gold is a REQUIRED
authority (declared in `spec/phase09_release_policy.json`), does not
genuinely exist, and cannot be fabricated.  Since the D7 closure, the
policy can only *declare* the requirement — satisfaction must arrive as an
owner-provisioned, HMAC-bound proof through the owner-controlled
environment channel (GitHub Actions secrets).  Repository edits, committed
"proof" files, and test fixtures can never satisfy it; the gate fails
closed without it.

## Evidence chain (one-way derivation)

1. Real test execution on the clean tree at `tested_git_sha`
2. `qa-backend/test_summary.json` (authoritative counts,
   sha256 `{ts['sha256'][:16]}…`)
3. Canonical release gate artifacts (`phase09_release_evidence.json`,
   `phase09_ticket_status.json`, CI-generated, uploaded with ≥180d request)
4. `docs/remediation/phase09_PHASE_RESULT.json`
5. `docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json`
6. This report (human view only)

SHA semantics: `tested_git_sha` = the checkout the tests actually ran on
(`{phase_result['tested_git_sha'][:12]}`); `evidence_generation_base_sha` =
the HEAD this evidence was generated at
(`{phase_result['evidence_generation_base_sha'][:12]}`).  When the two
differ, the base is a descendant of the tested commit whose diff touches
chain-owned evidence files and evidence-chain tooling scripts only.
  The commit storing these
files adds evidence only and is not claimed to have tested itself; CI at
the exact pushed head binds exact-head evidence via its own artifacts.

## External blockers

{blocker_lines or "- none recorded"}

## Gatekeeper history

Historical Codex gatekeeper verdicts are recorded in
`docs/remediation/phase09_gatekeeper_history.json` and the verbatim round
files `docs/remediation/phase09_D*_*.md`.

## Next-prompt gate

`NEXT_PROMPT_ALLOWED = {str(gate['NEXT_PROMPT_ALLOWED']).lower()}` —
Phase10 remains NOT_STARTED.  See
`docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json` for unblock conditions
and forbidden actions.
"""


if __name__ == "__main__":
    raise SystemExit(main())
