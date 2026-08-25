# Phase 04 implementation report — query integrity and agentic orchestration

Review base: `4ebb3470dba1bed65f0211fc11d6a0d7383b9aea`

Scope is limited to RT-040..RT-049. This implementation extends the existing
server, Phase02 terminal verifier and Phase03 retrieval-to-EvidencePackage
pipeline; it does not create a parallel answering pipeline.

## Implemented behavior

- RT-040: transactional claim-level conversation store. Only independently
  `SUPPORTED` claims with immutable EvidenceRefs and pinned runtime provenance
  can be reused; client history and client-supplied verification flags are not
  trusted.
- RT-041: deterministic semantic diff for entity, temporal, negation,
  modality, numeric, comparison, dimension, scope and intent changes. Unsafe
  or uncertain critical rewrites fail safe to the original query. Contextual
  entities may be authorized only by prior USER text or server-verified
  premises; assistant-only prose and client `verified` flags are diagnostic
  input only. Contextual pronouns bind only when the latest relevant USER turn
  has exactly one entity, or (when USER context has none) active verified
  premises have exactly one compatible entity. Multiple candidates reject the
  proposed rewrite and are recorded in the endpoint Trace.
- RT-042: FAST uses the real Phase03 retrieval, content rerank, evidence
  policy, selection, typed package and verification surfaces while skipping
  the Planner and research loop only.
- RT-043: one typed, serializable `ResearchState` and one canonical
  orchestrator entry. Selected evidence, Ledger and EvidencePackage remain
  connected; `all_results` is excluded from trusted generation input. The
  typed Phase04 terminal constraint is consumed by the existing Phase02
  `AnswerStateMachine`, which may downgrade but cannot upgrade its upper bound.
- RT-044: deterministic comparison object×dimension, trend/current and
  multi-entity requirements, explicit ambiguity, full semantic anti-drift and
  strict Planner-schema fallback. Planner-separated temporal, time, scope,
  provenance, relation and numeric requirements authoritatively drive the
  Phase03 policy engine. Missing/mismatched scope and explicit as-of periods,
  wrong value/unit, and deprecated, untyped or ungrounded relation evidence
  are requirement-scoped hard gates and leave the requirement unresolved.
- RT-045: bounded one-document worker inputs, exact snapshot locators and
  typed packets revalidated against the pinned snapshot and the canonical
  structured requirement-support policy before re-entering the final package.
  Raw worker packets never update the Ledger directly; only the policy-cleared
  package/view can do so. Worker `requirement_id`, prose, and model-authored
  `valid`/`typed`/`exact_grounded` flags are advisory. Relation assertions are
  independently revalidated by the canonical ontology against an exact
  EvidenceRef in the pinned SourceSnapshot before they can support anything.
- RT-046: optional packet cache scoped by manifest, profile, source snapshot,
  requirement fingerprint, model, prompt, schema and access scope. A disabled
  cache preserves behavior.
- RT-047: the Ledger records evidence/provenance/time/conflict/no-evidence and
  degradation state. Semantic Grader output cannot override hard policy, and a
  technical Grader failure cannot become sufficient. Planned targeted queries
  are distinguished from executed outcomes; searched-no-evidence is recorded
  only after an actual execution returns no new exact support.
- RT-048: typed gap analysis and requirement-bound targeted queries with
  normalized/semantic deduplication and anti-drift controls.
- RT-049: bounded canonical stop reasons and deterministic knowledge-boundary
  wording that does not infer real-world nonexistence from absence in Tech-DB.

## Behavioral evidence

- `qa-backend/tests_remediation_phase04.py`: 58 named unit, integration,
  security-adversarial and actual FastAPI/SSE endpoint checks. The endpoint
  matrix exercises six cases through the real endpoint, canonical orchestrator,
  pinned Phase03 mini runtime, real Phase02 pipeline/state machine/renderer and
  SSE: partial requirement coverage, unresolved conflict, required Grader
  technical failure, fully covered positive, assistant-only and multi-entity
  wrong-pronoun rewrites, and WorkerGap support exact-grounded to its own
  immutable snapshot. Only external model boundaries use deterministic stubs.
- `qa-backend/tests_benchmark_phase04.py`: committed deterministic mechanism
  and latency benchmark, bound to the accepted review base.
- `qa-backend/benchmark_phase04_result.json`: exact local-fixture output. Its
  latency is not a production SLO measurement.
- Required Gate job: `phase04-query-integrity-agentic-orchestration`.
- The acceptance matrix maps all 24 Phase04 DoDs to concrete named cases.

## Targeted acceptance review round 2

- An exact worker span containing only unrelated `industrial heat` is rejected
  and cannot close the WorkerGap requirement; the positive fixture contains
  the actual WorkerGap proposition and exact locator.
- `700 degrees` over `600 °C`, per-device/system scope mismatch, missing typed
  relation, and deprecated relation all fail the production requirement-policy
  composition. Source-grounded `600 °C` and a current ontology relation with
  an exact EvidenceRef pass.
- Those real policy failures remain unresolved in the Ledger and drive
  `MISSING_NUMERIC_CONDITION` / `MISSING_RELATION_METHOD` targeted queries.

## Targeted acceptance review round 3

- Production composition begins with `deterministic_requirements()` and keeps
  `numeric_conditions=["600°C"]`, `scope_constraints=["device"]`, and
  `time_constraints=["2025"]` separate through Phase03 policy, EvidencePackage
  and Ledger. System-total, unknown-scope, 2024, unknown-time and numeric/unit
  mismatch cases fail closed; per-device evidence with canonical 2025 metadata
  is the positive control.
- Scope/time policy findings remain machine-readable and produce
  `AMBIGUOUS_SCOPE` or `MISSING_TIME_PERIOD` targeted gaps after a real
  no-support package is recorded by the Ledger.
- Forged worker relation booleans, unknown predicates, deprecated assertions,
  wrong-snapshot refs and missing refs cannot close a relation requirement or
  reach a SUPPORTED terminal. An ontology-valid `USES` assertion with an exact
  pinned EvidenceRef is independently validated and may support.
- No production canary, shadow activation or external branch-protection action
  is claimed by these local/CI behavioral fixtures.

## Activation and rollback boundary

No production traffic, shadow run, canary or Graph-V2 activation is claimed.
The new trusted path remains behind the existing named profile/feature
surfaces. `legacy_hybrid` remains the explicitly configured compatibility and
rollback path until a later approved shadow/canary activation. Manifest mode
continues to require a valid immutable current release and fail closed.

RT-005 remains `BLOCKED_EXTERNAL_ACTION`; this report does not claim branch
protection or required status checks were configured by repository policy.
