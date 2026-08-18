# Phase 02 completion evidence

Scope: RT-020 through RT-029 (citation grounding, typed relations, numeric
provenance, coverage gate, canonical state machine, fail-safe verifier, bounded
repair, terminal renderer/SSE, schema hardening, frontend evidence states) on
top of accepted Phase-01 baseline `cdc589646085d2aa770c9b6835c99b310a170ad2`.

## Behavioral evidence

- `python qa-backend/tests_remediation_phase02.py`: 106 passed, 0 failed
  (unit/integration cases per ticket + real ASGI SSE E2E + node-run frontend
  checks over the shipped `qa.js`).
- `python qa-backend/run_all_tests.py --tier push`: all suites green (final
  count recorded in `qa-backend/test_summary.json`).
- canonical spec lint + negative-fixture self-test: PASS.
- acceptance-matrix validation: PASS (36 legacy frozen DoDs upgraded to
  SATISFIED with named Phase-02 evidence; T037 untouched per L12 hard gate).
- CI: `phase02-citation-claim-verifier` job added to
  `.github/workflows/remediation-gates.yml`.

## Production wiring (no parallel un-wired modules)

`server.py` calls `phase02_pipeline.run_phase02_verification` after generation
under `Flags.TERMINAL_RENDERER_ENABLED`: draft tokens are buffered (no factual
token reaches the client before verification), the citations event is emitted
only with schema-2.0 VALID citations after grounding, and the final rendered
answer streams post-verification with `ttfs_ms`/`ttfa_ms` traced. The legacy
pre-Phase-02 path remains intact behind the flag off-branch.

## Honesty and scope

- 6 of the 43 Phase-02-owned legacy DoDs stay NOT_SATISFIED for lack of
  directly corresponding behavioral evidence (T033 expandable-context,
  jump-to-original, visual layout; T046 premise-chain traceability; T048
  grader independence statistics and provenance-uncertainty retention).
- RT-029 visual regression (desktop/mobile) is not claimed; no harness exists.
- RT-005 remains BLOCKED_EXTERNAL_ACTION (repository administrator action).
- `verify_final` retry exists and is covered (`RT025.transient_error_retries_
  then_succeeds`); a transient transport error retries once and passes, while
  timeout/429/5xx/malformed/missing-fields/invalid-verdict all map to
  UNVERIFIED — never PASSED.
- The verifier returns structured findings only; `rewritten_answer` no longer
  exists (RT-025). The legacy SSE replace-event path was removed accordingly.
