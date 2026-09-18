# RT101 V14 Sprint — Codex Review A (main review, §38)

- **Reviewer**: OpenAI Codex CLI v0.153.4, `codex exec --sandbox read-only`
  (model glm-5.3-flash, provider zhipu_glm, reasoning effort xhigh)
- **Date**: 2026-09-18
- **Reviewed HEAD**: `80c8c0a` — "RT101-V14 sprint: five generalized V13 failure repairs (A/B/C/D/E)"
  (branch `remediation/phase-09-benchmarks-ci-release-gates`, parent `1e6a5b1`)
- **Scope**: the single commit `80c8c0a`, exactly 11 files (verifier.py, answer_status.py,
  server.py, phase02_pipeline.py, record_id_authority.py, retrieval/deterministic_rerank.py,
  retrieval/runtime.py, run_all_tests.py, tests_rt101_v13_failure_repairs.py,
  scripts/repair_c_retrieval_benchmark.py, docs/remediation/phase09_V14_repair_c_benchmark.json)
- **Blinding**: reviewer instructed NOT to read anything under hidden/ or any *SECRET* file;
  static review only (no test execution — an execution constraint was imposed after a first
  review attempt stalled running suites inside its read-only sandbox and terminated with an
  internal session error without emitting a verdict; that attempt produced no findings and is
  superseded by this completed review).
- **Result**: 1,105,802 tokens used; verdict block emitted as required.

## Verdict (verbatim)

```
VERDICT: APPROVE
REMAINING_P0: 0
REMAINING_P1: 0
REMAINING_P2: 1
```

## Findings (verbatim)

1. [P2] `qa-backend/record_id_authority.py:37-56` — The guard's §12 universe-resolvability
   branch (`citation_record_authority_error(..., universe=...)`) is defined and tested but
   neither `server.py:4300` nor `phase02_pipeline.py:1368` passes `universe`. Pseudo-ID
   rejection itself is correctly wired and sufficient for the V13 regression; this leaves
   canonical-ID resolvability enforcement available but not active at production call sites.
   Required fix: none for this sprint's scope; wire `universe` in a follow-up if §12 is
   intended as a runtime (not merely API-level) contract.

## Reviewer closing statement (verbatim)

The terminal state-authority path is consistent: both production seams attach verdicts
before derivation, the machine snapshot is built from those same rows, and terminal
payloads project `verifier_verdict` into serialized claim rows, so the canonical-vs-requested
agreement check has matching inputs.

All other invariants verify statically: single commit, exactly the 11 declared files,
trailer present, no force-push artifacts or secrets in the diff. Repair A preserves the
bare shape and fails closed on contract violations. Repair C's deep mode is opt-in via
query shape, the frozen parity baselines cannot activate it, and rerank is pure. Rule 9b
fires only inside the `FAILED` branch and only when verdict-bearing rows are present,
making it downgrade-only. T004 runs once, before T005, and the rescue path cannot override
a terminal UNVERIFIED. Repair E bounds legacy retries to `MAX_VERIFY_RETRIES+1`,
context-owned callers raise on first transport error, semantic failures never retry, and
the string-return contract is unchanged. The push-tier suite is registered and pins all
five repair contracts, including the 20-query parity non-activation canary.

## Disposition

- P0 = 0, P1 = 0 → Review A gate SATISFIED (§38).
- P2-1 (universe wiring at production call sites): ACCEPTED AS FOLLOW-UP per the reviewer's
  own disposition ("Required fix: none for this sprint's scope"). Rationale: the V13 root
  cause B (legacy-idx pseudo-ID leak) is fully wired and tested; universe-resolvability is
  additional hardening whose activation changes runtime behavior beyond the frozen repair
  scope and must not be introduced mid-sprint (repair-freeze discipline, §33). Recorded as
  post-V14 follow-up work.
