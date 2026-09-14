# Phase09 / RT101 — Codex Review Cluster C (final package) — verdict and dispositions

Scope: V8 candidate packaging + owner runner lifecycle + approval-gate
surface (builder `build_v8.py` / `freeze_v8.py` / `fill_v8_runner.py`,
owner runner `run_v8_after_owner_approval.sh`, canonical scorer
`score_v8.py`). Serial cluster discipline: A (APPROVE) → B (REJECT → all
findings dispositioned, commit 149a736) → C (this document).

## Verdict (verbatim, first lines)

```
VERDICT: REJECT
P0 COUNT: 1
P1 COUNT: 3
P2 COUNT: 2
```

Reviewer blinding confirmation (verbatim): "No hidden gold, expected
answers, hidden rubric, per-case sealed detail, salts, owner secrets,
blind-case file contents, V6/V7 markers, V6/V7 decision/evidence contents
opened or read; assessment used scoped code, sanitized metadata, aggregate
counters, permissions, digests, structural state only."

## Dispositions (all findings repaired; none waived)

### C-P0-1 — raw salt exposure in logs / blind package / manifest / candidate identity / runner embedding — REPAIRED
Confirmed and root-caused: the salt is the blinding root (raw salt + the
deterministic selection algorithm over the public corpus recovers exact
holdout membership). Repairs:
- `build_v8.py`: raw salt never logged (digest+prefix only); blind package
  is QUERIES-ONLY (raw `salt` field removed); blind manifest and
  selection ledger now carry `salt_digest` + `salt_prefix` only;
  `salt_binding.json` is digest/prefix-only (schema -1.1). Sole remaining
  raw-salt artifact is the gold file, which ships ONLY inside the 0600
  owner bundle as the owner's own verification anchor (owner holds
  salt.txt) — documented in-code.
- `freeze_v8.py`: candidate identity carries `salt_digest` + `salt_prefix`
  only (schema `rt101-v8-candidate-identity-1.1`); the working-evidence
  package now EXCLUDES `salt.txt` entirely.
- `fill_v8_runner.py`: retired the `__SALT__` runner-embedding token; now
  installs the owner-side 0600 `v8_salt.txt` and verifies it against the
  identity `salt_digest` before filling; template→runner fill flow
  (pristine `run_v8_after_owner_approval.sh.template`).
- Owner runner: `V8_SALT_FILE` sourced at runtime for the repo salt-leak
  precheck (0600 enforced); raw salt never embedded in runner bytes.
- Candidate rebuilt from the same owner salt (selection invariant proven
  by case-id sequence identity); V6/V7 salts remain untouched and
  reuse-forbidden.

### C-P1-2 — fill identity cross-checks omit working_evidence_sha256 — REPAIRED
`fill_v8_runner.py` identity-input cross-check loop now includes
`working_evidence_sha256` vs the actual package bytes.

### C-P1-3 — preflight allows ZAI_MODEL env substitution for the model — REPAIRED
Runner [3/9] now fails closed when `ZAI_MODEL` is set and differs from the
corpus-pinned `pin["model"]` (filled as `V8_PINNED_MODEL` at freeze); the
preflight probe always uses the pinned model — no environment substitution.

### C-P1-4 — post-seal failures report pre-seal exit semantics — REPAIRED
Runner header exit-code contract now defines `4 = post-seal technical
failure (opportunity CONSUMED)`. `die_consumed` (exit 4) replaces exit 2
for every post-marker failure (capture digest re-checks, capture driver,
population, case count, scorer digest/nonzero/missing report, chain
fork/double-decision refusals). An ERR trap converts any unhandled
`set -e` failure after `POST_SEAL=1` (set the moment the marker exists on
disk) into the same consumed-run report. Pre-seal abort codes 2/3
unchanged; dry-run never sets POST_SEAL.

### C-P2-5 — decision_chain.sha256 accepted with missing/empty log — REPAIRED
[0/9] predecessor inspection now refuses a chain head that exists without
a non-empty `decision_log.appendonly` (pre-seal, exit 2); post-seal chain
verification unchanged (fork/double-decision refusals now consumed-exit 4).

### C-P2-6 — scorer carries stale V6 terminology and a V6-named default gold path — REPAIRED
`score_v8.py` docstring/metadata/comments/report label now say V8; the
default gold path is the V8 gold (`rt101_v8_gold.json`, env-overridable
via `V8_GOLD_PATH`); no V6 references remain. Latent hazard removed: the
old default pointed at a V6-named path (schema check would have failed
closed, but the reference itself was wrong).

## Verification state after repairs
- Runner template: `bash -n` OK; fill tokens verified; retired `__SALT__`
  token absent.
- Lifecycle suite (`tests/test_v8_runner_lifecycle.sh`): extended with
  Cluster C checks (template purity, salt-file sourcing + 0600, pinned
  model fail-closed, consumed-exit-4 semantics, chain-head refusal, fill
  cross-check completeness, working-evidence salt exclusion).
- Scorer selftest: `{"selftest": "REQUIREMENT_EXTRACTION_SELFTEST",
  "PASS": true, "fixtures": 14}` after the terminology/path repairs.
- Rebuild invariants: blind case-id sequence identical to the pre-repair
  candidate (same owner salt, same selection); deterministic rebuild
  re-verified post-repair.
