<!-- D7/D8 PR body — evidence-chain state; regenerate via
     scripts/build_phase09_evidence.py, do not hand-edit the machine block. -->

## Phase09 — Benchmarks, CI, release gates (D7 authority closure + D8 RT-101 repair & V6 candidate)

### CURRENT_STATE
- branch: `remediation/phase-09-benchmarks-ci-release-gates` — PR #10 **open / unmerged**
- evidence commit: this head; tested commit `434785032c9ed50485cb9375e649a8572bd11f0c`
  (clean worktree), generation base `434785032c9ed50485cb9375e649a8572bd11f0c`
  (tested == base; evidence-only descendant semantics, no self-reference fixed point)
- worktree clean; normal pushes only (no force push)

### MACHINE EVIDENCE
```text
total: 2026 passed, 0 failed across 56 suites
tested_git_sha: 434785032c9ed50485cb9375e649a8572bd11f0c
test_summary sha256: see docs/remediation/phase09_PHASE_RESULT.json (evidence_chain)
phase_status: NOT_SATISFIED
core_eligible: false
production_release_eligible: false
graph: NO_GAIN / OFF_NO_GAIN / NOT_ACTIVATED_BY_GAIN_GATE
external_blockers: none (RT-075, Q-336, RT-005 owner-satisfied via HMAC proofs)
rt101_authority_satisfied: false
NEXT_PROMPT_ALLOWED: false
phase10: NOT_STARTED
```

### RT-101 — V5 failure adjudication, runtime repair, fresh V6 candidate (D8)
- V5 formal blinded holdout FAIL adjudicated `ROOT_CAUSE_CLASS=B`
  (`docs/remediation/phase09_RT101_corpus_adjudication.json`): builder pinned the
  RAW spider tree while the runtime serves the INGESTED citation-eligible corpus
  (30,391 records, manifest `mini-runtime-a49a56f8861a0633`). V5 is
  CONSUMED-FAILED and immutable; never re-run.
- Permanent runtime repairs RD-1/RD-2/RD-3 (display-integrity citation
  withholding, canonical no-evidence abstention, bounded verifier transient
  retry) + fail-closed machine corpus-compatibility and source-coverage gates
  that block any future formal run whose candidate universe does not exact-match
  the live runtime binding BEFORE one-shot consumption.
- Fresh independent V6 blind-holdout candidate built in an isolated builder
  workspace (no gold content access; V5 material untouched; no formal execution
  this round — runner ships dry-run only):

```text
V6_SHA256      = 100a83b7faf9bb2539cde5c465fca626b2af6ad1dae6a60fce39af0c8b42955b
V6_LOCK_SHA256 = 034bd36b6c5c3f8ee0cec40cb99e7203a116f14746160406f68a07c1beec3d88
case_mix       = 11 ANSWER / 2 ABSTAIN / 2 MUTATION_WITH_LOYAL_ANSWER
citation bind  = record:<record_id> against the live ingested store
gatekeeper     = codex review C rounds REJECT → REJECT → APPROVE
                 (verbatim: docs/remediation/phase09_RT101_codex_review_C_round{1,2,3}.md)
status         = RT101_V6_OWNER_APPROVAL_REQUIRED
```

- Owner-side deliverables (owner secrets, 0700): `RT101_V6_CANDIDATE_APPROVAL_REQUEST.json`
  + `run_v6_after_owner_approval.sh` (one-shot formal runner; identity recomputation
  from bytes → head binding → corpus-compatibility gate → source-coverage gate →
  preflight → salt-leak grep → marker seal → pinned capture → digest-pinned scorer →
  hash-chained append-only decision log; dry-run only this round).

### AUTHORITY (P0 closure)
- Repo-editable files can only **declare** requirements — they can never declare
  satisfaction. Genuine RT-101 authority arrives ONLY via owner-provisioned GitHub
  Actions secrets (`PHASE09_RT101_AUTHORITY_PROOF`, `PHASE09_RT101_EXPECTED_HOLDOUT_LOCK_SHA256`,
  `PHASE09_RT101_AUTHORITY_HMAC_KEY`, plus the two external-satisfaction secrets),
  HMAC-SHA256-bound, key-strength-checked, constant-time compared, freshness-enforced.
- `MANDATORY_AUTHORITIES` pinned in code; deleting the policy entry fails closed.
- Contract: `docs/remediation/phase09_RT101_provisioning.md`. Gold never enters
  repo/git/PR/logs/traces/fixtures.

### EVIDENCE CHAIN
One-way machine-derived chain: `qa-backend/test_summary.json` → release/ticket
evidence (CI-generated, gitignored) → `docs/remediation/phase09_PHASE_RESULT.json` →
`phase09_NEXT_PROMPT_ALLOWED.json` → completion report.
`scripts/validate_phase09_evidence_chain.py --strict-machine`: **33/33 checks**
(counts, hashes, decision, blockers, graph, report machine-block, generation order,
SHA semantics `tested ≤ base ≤ head` with owned-only drift, artifact existence/hashes,
policy suites, live-env authority match, external-state freshness).

### TESTS
- `python3 qa-backend/run_all_tests.py --tier push` at clean `4347850`:
  **2026 passed / 0 failed / 56 suites**, finalize `verify_spec_manifest` PASS.
- Canonical gate exits 1 **by design**: the only failure reason is genuine RT-101
  authority absence (`scripts/verify_phase09_expected_block.py` certifies
  INTENTIONAL_FAIL_CLOSED, suites 5/5 PASS, invariants clean). This is the correct
  fail-closed state, not a regression.

### EXTERNAL CONTROLS (owner-satisfied, HMAC-bound at pushed head)
- RT-075 approved equivalent locked replay · Q-336 durable ≥180d retention receipt ·
  RT-005 enforce-admins branch protection — satisfaction proofs re-bound to the exact
  pushed head and provisioned via the secret channel; repository files still cannot
  self-attest (fail-closed by design since D7).

### BOUNDARY
`RT101_V6_OWNER_APPROVAL_REQUIRED` — `NEXT_PROMPT_ALLOWED=false`. The formal V6 run,
owner-authority provisioning, Phase10, RT110-116 and Graph activation all remain
gated behind explicit owner approval of the exact candidate+lock pair above. This is
the correct fail-closed state, not a defect.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
