<!-- D7 PR body — generated from the final evidence chain; regenerate via
     scripts/build_phase09_evidence.py, do not hand-edit the machine block. -->

## Phase09 — Benchmarks, CI, release gates (D7 Final Authority & Evidence Closure)

### CURRENT_STATE
- branch: `remediation/phase-09-benchmarks-ci-release-gates` — PR #10 **open / draft / unmerged**
- final evidence commit: this head; tested commit `ab399f5ceed079e5094e6755aa56faa45af11847` (clean worktree), generation base `d4977c6ce8044187cb2dc8e1a816514cfda1aa65` (owned-evidence-only drift, descendant-base semantics — no self-reference fixed point)
- worktree clean; normal pushes only (no force push)

### MACHINE EVIDENCE
```text
total: 1689 passed, 0 failed across 49 suites
tested_git_sha: ab399f5ceed079e5094e6755aa56faa45af11847
test_summary sha256: see docs/remediation/phase09_PHASE_RESULT.json (evidence_chain)
phase_status: NOT_SATISFIED
core_eligible: false
production_release_eligible: false
graph: NO_GAIN / OFF_NO_GAIN / NOT_ACTIVATED_BY_GAIN_GATE
external_blockers: Q-336, RT-005, RT-075 (all satisfied=false)
rt101_authority_satisfied: false
NEXT_PROMPT_ALLOWED: false
phase10: NOT_STARTED
```

### AUTHORITY (P0 closure)
- Repo-editable files (policy JSON, external-state JSON, matrix, fixtures) can only **declare** requirements — they can never declare satisfaction.
- Genuine RT-101 authority arrives ONLY via owner-provisioned GitHub Actions secrets (`PHASE09_RT101_AUTHORITY_PROOF`, `PHASE09_RT101_EXPECTED_HOLDOUT_LOCK_SHA256`, `PHASE09_RT101_AUTHORITY_HMAC_KEY`, plus the two external-satisfaction secrets), verified as HMAC-SHA256-signed canonical-JSON proofs bound to git SHA, spec/decision-register hashes, locked manifest identity, holdout lock hash, trust class `OWNER_PROVISIONED_EXTERNAL`, timestamps (180-day max age, ≤5 min future skew, commit-time ordering), and constant-time integrity comparison.
- `MANDATORY_AUTHORITIES` is pinned in code: deleting RT-101 from the policy fails closed everywhere (gate, validator, standalone publish path). Publicly-known test key material is rejected by production providers.
- Contract + provisioning steps: `docs/remediation/phase09_RT101_provisioning.md`. Gold never enters repo/git/PR/logs/traces/fixtures (locked fixture carries query metadata only; no answer fields).

### EVIDENCE CHAIN (P1 closure)
One-way machine-derived chain: `qa-backend/test_summary.json` → release/ticket evidence (CI-generated, gitignored) → `docs/remediation/phase09_PHASE_RESULT.json` → `phase09_NEXT_PROMPT_ALLOWED.json` → completion report. `scripts/validate_phase09_evidence_chain.py --strict-machine` passes **31/31** checks (counts, hashes, decision incl. reasons/authorities, blockers, graph, phase implications, report machine-block, generation order with future-stamp rejection, SHA semantics `tested ≤ base ≤ head` with owned-only drift, artifact existence/hashes, policy suites, graph, live-env authority match, external-state freshness).

### TESTS
- `python3 qa-backend/run_all_tests.py --tier push` at clean `ab399f5`: **1689 passed / 0 failed / 49 suites**
- `qa-backend/tests_release_phase09.py`: 79/79 cases incl. 16 adversarial authority cases, publish-path denials, evidence-chain drift matrix
- Canonical gate exits 1 **by design**: the only failure reason is genuine RT-101 authority absence (`scripts/verify_phase09_expected_block.py` certifies INTENTIONAL_FAIL_CLOSED). This is the correct fail-closed state, not a regression — the only permitted CI red is the phase09 job failing for exactly this reason.

### CODEX
- Design round: `docs/remediation/phase09_D7_codex_authority_design.md`
- Adversarial code review (11 findings, all fixed): `docs/remediation/phase09_D7_codex_code_review.md`
- **Final Gatekeeper (final round, head d24ed1e+): AUTHORITY_BYPASS_CLOSED: YES · EVIDENCE_CHAIN_CONSISTENT: YES · HOLDOUT_ISOLATION_PRESERVED: YES · RELEASE_FAIL_CLOSED_WITHOUT_GENUINE_RT101: YES · SAFE_FOR_FRESH_V5: YES · FINDINGS: none** — `docs/remediation/phase09_D7_codex_gatekeeper.md`

### BLOCKERS (all external — agent cannot clear)
| ID | Blocker | Why blocked | Human action |
|----|---------|-------------|--------------|
| RT-101 | answer-level blinded release holdout gold + owner authority proof | gold/keys must never enter repo; provisioning requires owner-controlled secrets | follow `docs/remediation/phase09_RT101_provisioning.md` |
| Q-336 | artifact retention ≥180d not satisfiable on public-repo policy (cap 90d) | no threshold reduction permitted | configure durable external retention store |
| RT-005 | branch protection/required checks re-verification | requires repo-admin API access (11 required checks, enforce_admins=false) | admin re-reads protection rules via API |
| RT-075 | production-representative ER shadow evidence | CI replay ≠ production shadow | provision shadow environment |

### PHASE10
**NOT_STARTED** — `NEXT_PROMPT_ALLOWED=false`. No formal fresh V5 while genuine RT-101 authority is absent. Unblock path: owner provisions RT-101 via the contract above → gate passes → `NEXT_PROMPT_ALLOWED=true` → V5 becomes eligible.
