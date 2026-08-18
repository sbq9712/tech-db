# Phase 02 completion evidence — RESULT: PARTIAL

Per-ticket status (post acceptance re-audit 2026-08-18):

| Ticket | Status | Note |
|---|---|---|
| RT-020 exact grounding on immutable snapshots | DONE | DoDs incl. stable record identity (no legacy_idx degeneration) |
| RT-021 typed relations / deterministic entailment | DONE | |
| RT-022 numeric provenance | DONE | facts keyed by stable record_id + snapshot; reorder-proven |
| RT-023 claim coverage gate | DONE | |
| RT-024 canonical AnswerStateMachine | DONE | |
| RT-025 fail-safe final verifier | DONE | complete exact EvidenceRefs + fail-closed negative tests |
| RT-026 bounded repair loop | DONE | retrieve_fn/regenerate_fn wired; full post-repair re-check pass |
| RT-027 terminal renderer + post-verification SSE | DONE | incl. profile semantics (QA_PIPELINE_PROFILE applies at import) |
| RT-028 done-event / citation schema hardening | DONE | |
| RT-029 frontend evidence-state rendering | **PARTIAL** | node-run behavioral checks delivered; visual-regression (desktop/mobile) DoD NOT_SATISFIED — no harness exists |

Phase verdict: **PARTIAL** — RT-029's visual-regression DoD is unmet, so the
phase cannot claim full completion. No other Phase-02 DoD is claimed without
named executable evidence.

Scope: RT-020 through RT-029 (citation grounding, typed relations, numeric
provenance, coverage gate, canonical state machine, fail-safe verifier, bounded
repair, terminal renderer/SSE, schema hardening, frontend evidence states) on
top of accepted Phase-01 baseline `cdc589646085d2aa770c9b6835c99b310a170ad2`.

## Behavioral evidence

- `python qa-backend/tests_remediation_phase02.py`: 128 passed, 0 failed
  (unit/integration cases per ticket + real ASGI SSE E2E + node-run frontend
  checks over the shipped `qa.js`; includes acceptance-review fix cases:
  stable record identity, request-pinned runtime E2E, complete verifier
  EvidenceRefs with fail-closed negatives, wired repair with full re-check,
  and fresh-process QA_PIPELINE_PROFILE semantics A-D).
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

Request-pinned evidence (RT-017 extension): in manifest mode the pipeline
receives the request-pinned `records` / `records_by_id` / `record_id_map`
from the RuntimeSnapshot — never the mutable server-global `_records` — and
targeted re-retrieval (`retrieve_fn`) and regeneration (`regenerate_fn`)
closures run through the same pinned retrieval pipeline
(`X.pipeline_uses_pinned_records_e2e` proves a mid-request release switch
cannot change verification evidence).

Rollout semantics: `QA_PIPELINE_PROFILE` now applies at `feature_flags`
import — before any consumer reads a flag — and an explicitly-set `QA_*`
env var that deviates from the declared profile fails closed at startup.
`legacy_hybrid` is the pre-Phase-02 deployed activation state (shipped
flags at gate-3 defaults ON; `EXACT_GROUNDING`/`TERMINAL_RENDERER` OFF),
so applying the profile changes nothing the deployment already ran except
disabling the two new Phase-02 flags (fresh-process tests A-D:
`X.profile_applies_at_import`, `X.profile_env_conflict_fails_closed`,
`X.profile_env_agreement_applies`, `X.deployment_activation_state_preserved`,
`X.unknown_profile_fails_closed`).

## Honesty and scope

- 6 of the 43 Phase-02-owned legacy DoDs stay NOT_SATISFIED for lack of
  directly corresponding behavioral evidence (T033 expandable-context,
  jump-to-original, visual layout; T046 premise-chain traceability; T048
  grader independence statistics and provenance-uncertainty retention).
- RT-029 is PARTIAL, not DONE: the node-run behavioral checks (schema
  invalidation, four-state config/banner, role-distinct rendering chips,
  INVALID dropped from stale state) are real and delivered, but the frozen
  DoD "mobile/desktop visual regression coverage" has no harness and stays
  NOT_SATISFIED — therefore the phase verdict is PARTIAL.
- RT-005 remains BLOCKED_EXTERNAL_ACTION (repository administrator action).
- `verify_final` retry exists and is covered (`RT025.transient_error_retries_
  then_succeeds`); a transient transport error retries once and passes, while
  timeout/429/5xx/malformed/missing-fields/invalid-verdict all map to
  UNVERIFIED — never PASSED.
- The verifier returns structured findings only; `rewritten_answer` no longer
  exists (RT-025). The legacy SSE replace-event path was removed accordingly.
