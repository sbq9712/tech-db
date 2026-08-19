# Phase 02 completion evidence — RESULT: DONE

Per-ticket status (post third acceptance-review remediation 2026-08-18):

| Ticket | Status | Note |
|---|---|---|
| RT-020 exact grounding on immutable snapshots | DONE | DoDs incl. stable record identity + request-pinned source_catalog as sole snapshot authority |
| RT-021 typed relations / deterministic entailment | DONE | |
| RT-022 numeric provenance | DONE | facts keyed by stable record_id + snapshot; reorder-proven |
| RT-023 claim coverage gate | DONE | |
| RT-024 canonical AnswerStateMachine | DONE | |
| RT-025 fail-safe final verifier | DONE | complete exact EvidenceRefs + pinned-snapshot CONSISTENCY verification (hash/locators/exact_text/eligibility), fail-closed negatives A–F |
| RT-026 bounded repair loop | DONE | retrieve_fn wired; regeneration runs on an allowlisted evidence-scoped package; full post-repair re-check pass |
| RT-027 terminal renderer + post-verification SSE | DONE | incl. profile semantics (QA_PIPELINE_PROFILE applies at import) |
| RT-028 done-event / citation schema hardening | DONE | |
| RT-029 frontend evidence-state rendering | DONE | node-run behavioral checks + real-browser (Chromium) visual regression suite `tests_visual_rt029.py`: deterministic local fixtures (no live tunnel), desktop 1280×800 + mobile 390×844 viewports, committed golden screenshots with pixel diff, layout/geometry/computed-style assertions, and a mutation case proving the harness detects broken layouts. Registered as required CI gate `rt029-visual-regression` and in `run_all_tests.py` (suite `visual_rt029`). RT-029.DOD-03 SATISFIED with named executable evidence. |

Phase verdict: **DONE** — all Phase-02 DoDs carry named executable evidence;
no DoD is claimed SATISFIED without a runnable check (acceptance matrix stays
honest; RT-005 remains BLOCKED_EXTERNAL_ACTION outside Phase-02 scope).

Scope: RT-020 through RT-029 (citation grounding, typed relations, numeric
provenance, coverage gate, canonical state machine, fail-safe verifier, bounded
repair, terminal renderer/SSE, schema hardening, frontend evidence states) on
top of accepted Phase-01 baseline `cdc589646085d2aa770c9b6835c99b310a170ad2`.

## Behavioral evidence

- `python qa-backend/tests_remediation_phase02.py`: 155 passed, 0 failed
  (unit/integration cases per ticket + real ASGI SSE E2E + node-run frontend
  checks over the shipped `qa.js`; includes acceptance-review fix cases:
  stable record identity, request-pinned runtime E2E, complete verifier
  EvidenceRefs with fail-closed negatives, wired repair with full re-check,
  and fresh-process QA_PIPELINE_PROFILE semantics A-D; plus the second review
  round: request-pinned source_catalog binding, deterministic EvidenceRef
  consistency verification, and evidence-scoped regeneration; third review
  round: manifest-mode fail-closed source_catalog matrix at
  pipeline/build/store/startup levels with a real producer
  (`build_source_catalog` → mini-runtime `source_catalog.json` artifact →
  `scripts/build_mini_release.py` full validated release), and targeted
  retrieval → validated → UPDATED Evidence-Package → regenerate ordering
  proof).
- `python qa-backend/tests_visual_rt029.py`: 14 passed, 0 failed — real
  Chromium, desktop + mobile, golden pixel diff + mutation detection
  (also wired into `run_all_tests.py --tier push` as suite `visual_rt029`
  and into CI as required gate `rt029-visual-regression`).
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

Request-pinned snapshot AUTHORITY: in manifest mode the pipeline now resolves
every immutable snapshot from the request-pinned
`resources["source_catalog"]` — citation `source_snapshot_id`, verifier
EvidenceRefs and numeric-fact provenance all bind to that catalog's snapshot
ids, a record absent from the catalog (or whose content hash / eligibility
diverges from its declaration) is dropped fail-closed, and the WORKING_DIR
`SourceSnapshotStore` is not consulted at all in manifest mode (legacy mode
keeps its historical store path). `X.pinned_source_catalog_binds_e2e` proves
generation A's snapshot binding survives a mid-request switch to B, and
`X.new_request_binds_new_generation_e2e` proves a NEW request binds to B.

EvidenceRef integrity: `verify_final` (and the pipeline, before any verifier
call) deterministically verifies each ref's VALUES against the pinned
snapshot — record_id/snapshot-id binding, evidence-text hash equality,
in-range offsets, `snapshot.evidence_text[start:end]` equality with the
claimed exact_text (multi-span concatenation rebuilt from the locators), and
eligibility. Correct-format-but-wrong-value refs fail closed to UNVERIFIED
(`RT025.ref_wrong_hash_value_unverified`, `ref_locator_points_elsewhere`,
`ref_exact_text_tamper`, `ref_foreign_generation_snapshot`,
`ref_record_id_mismatch`), and non-empty claims with zero refs can never
verify to PASSED (`RT025.claims_without_refs_cannot_pass`).

Evidence-scoped regeneration (RT-026): the repair loop's regeneration input
is an allowlisted Evidence-Package-compatible package
(`build_repair_evidence_package` / `render_repair_evidence_input`) —
question/scope, the still-VALID exact EvidenceRefs (stable record_id,
snapshot id, locators, exact_text, sha256), verified support relations,
applicable deterministic numeric results, and explicit keep/drop/core-gap
instructions. Raw all_results, synthetic summaries, ungrounded retrieval
text, generator hidden reasoning and stale answer prose are structurally
absent (they are not inputs to the builder). Whatever regeneration
reintroduces is re-checked by the full second pass: a new unsupported fact
is blocked by the coverage gate (`RT026.regen_unsupported_fact_blocked`),
a tampered number by the numeric check (`RT026.regen_number_tamper_blocked`),
and ungroundable retrieved text never becomes support
(`RT026.retrieved_ungroundable_evidence_dropped`).

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
- RT-029 is DONE as of the third review round: the node-run behavioral
  checks (schema invalidation, four-state config/banner, role-distinct
  rendering chips, INVALID dropped from stale state) are real and delivered,
  AND the frozen DoD "mobile/desktop visual regression coverage" is now
  SATISFIED by `qa-backend/tests_visual_rt029.py` — a real-browser
  (Playwright/Chromium) suite with deterministic local SSE fixtures (no
  live-tunnel dependency), desktop (1280×800) and mobile (390×844)
  viewports, committed golden screenshots
  (`qa-backend/test_fixtures/visual_goldens/rt029/`) compared with a
  deterministic pixel diff, real layout/geometry/computed-style assertions
  (distinct CONTRADICTS/BACKGROUND/support colors, PARTIALLY_SUPPORTED
  supported/unresolved sections, UNVERIFIED banner + degraded chips,
  TEXT_SPAN locator chips, stale/INVALID citations never rendered, long
  source titles single-line with ellipsis), plus a mutation/sanity case
  that deliberately breaks the layout and MUST fail the diff. The suite is
  CI-repeatable (registered as required gate `rt029-visual-regression` in
  `.github/workflows/remediation-gates.yml`, suite `visual_rt029` in
  `run_all_tests.py`) — therefore the phase verdict is DONE.
- RT-005 remains BLOCKED_EXTERNAL_ACTION (repository administrator action).
- `verify_final` retry exists and is covered (`RT025.transient_error_retries_
  then_succeeds`); a transient transport error retries once and passes, while
  timeout/429/5xx/malformed/missing-fields/invalid-verdict all map to
  UNVERIFIED — never PASSED.
- The verifier returns structured findings only; `rewritten_answer` no longer
  exists (RT-025). The legacy SSE replace-event path was removed accordingly.
