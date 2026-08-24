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
  input only.
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
  strict Planner-schema fallback. Structured temporal/provenance/relation/
  numeric requirements authoritatively drive the Phase03 policy engine.
- RT-045: bounded one-document worker inputs, exact snapshot locators and
  typed packets revalidated against the pinned snapshot, merged into the
  Ledger and re-entered through the same Phase03 policy and final package.
  Worker prose never enters generation context.
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

- `qa-backend/tests_remediation_phase04.py`: 48 named unit, integration,
  security-adversarial and actual FastAPI/SSE endpoint checks. The endpoint
  matrix exercises six cases through the real endpoint, canonical orchestrator,
  pinned Phase03 mini runtime, real Phase02 pipeline/state machine/renderer and
  SSE: partial requirement coverage, unresolved conflict, required Grader
  technical failure, fully covered positive, wrong-pronoun rewrite, and a
  worker closing a missing requirement. Only external model boundaries use
  deterministic stubs.
- `qa-backend/tests_benchmark_phase04.py`: committed deterministic mechanism
  and latency benchmark, bound to the accepted review base.
- `qa-backend/benchmark_phase04_result.json`: exact local-fixture output. Its
  latency is not a production SLO measurement.
- Required Gate job: `phase04-query-integrity-agentic-orchestration`.
- The acceptance matrix maps all 24 Phase04 DoDs to concrete named cases.

## Activation and rollback boundary

No production traffic, shadow run, canary or Graph-V2 activation is claimed.
The new trusted path remains behind the existing named profile/feature
surfaces. `legacy_hybrid` remains the explicitly configured compatibility and
rollback path until a later approved shadow/canary activation. Manifest mode
continues to require a valid immutable current release and fail closed.

RT-005 remains `BLOCKED_EXTERNAL_ACTION`; this report does not claim branch
protection or required status checks were configured by repository policy.
