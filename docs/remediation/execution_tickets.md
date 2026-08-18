# Tech-DB Final Remediation Execution Tickets

Basis: `techdb_final_spec.md`
Execution rule: inspect/reuse existing modules first; do not create duplicate parallel implementations merely because a ticket names a capability.
Completion rule: a ticket is DONE only when its behavioral DoD and required tests pass. Importability alone is never sufficient.

## Phase 00 — Freeze authority, baseline, and honest acceptance

### RT-001 — Freeze remediation spec and canonical manifest
**Priority:** P0  
**Depends on:** none  
**Maps to:** T040

**Do:** Add the final spec/Decision Register hashes to the canonical machine-readable spec manifest; register CORE_REQUIRED / PROFILE_REQUIRED / BENCHMARK_GATED_OPTIONAL capability classes; lint unknown/duplicate tickets, cycles, profile incompatibilities, and missing acceptance mappings.

**Done when:**
- one command validates spec + Decision Register + ticket registry + profiles
- release tooling can read the exact normative hashes
- intentionally corrupted duplicate/cycle/hash/profile fixtures fail

**Tests:** spec-lint unit + negative-fixture suite.

### RT-002 — Replace fake Final Acceptance assertions
**Priority:** P0  
**Depends on:** RT-001  
**Maps to:** T002, T034, T037

**Do:** Rewrite `tests_final_acceptance.py` so no DoD is passed by import-only checks, `or True`, hardcoded `True`, or manually fabricated Trace stages. Build an acceptance matrix mapping each active T/ER DoD to real tests/benchmarks.

**Done when:**
- grep/static test finds zero no-op acceptance assertions
- each core DoD has at least one named behavioral test
- cross-stage DoD points to integration/E2E, not import smoke

**Tests:** acceptance-matrix completeness test; mutation tests that deliberately break representative capabilities and make acceptance fail.

### RT-003 — Reproducible mini-runtime fixture
**Priority:** P0  
**Depends on:** RT-002  
**Maps to:** T002, T034

**Do:** Extend the committed mini-index fixture into a complete reproducible runtime fixture containing stable IDs, source snapshots, Vector/BM25 and optional chunk/Graph artifacts, metadata, identity snapshot, and manifest.

**Done when:**
- fresh checkout can rebuild/use the fixture without production gitignored data
- fixture has pinned hashes and manifest
- server/orchestrator E2E can run against it deterministically

**Tests:** rebuild digest parity; fixture startup health.

### RT-004 — Current-production baseline and gap report
**Priority:** P0  
**Depends on:** RT-003  
**Maps to:** T002, T034

**Do:** Capture pre-remediation retrieval, answer, citation, latency, and error baselines on the locked development/regression set; record known current gaps found in the audit.

**Done when:**
- machine-readable baseline artifact includes git SHA/config/model/index versions
- benchmark distinguishes old RRF Top25, current Agentic path, and legacy profile
- report is referenced by later before/after gates

**Tests:** report schema validation; reproducibility smoke.

### RT-005 — Protect main and required merge checks
**Priority:** P0  
**Depends on:** RT-001, RT-002  
**Maps to:** T054/T055 operational gate

**Do:** Configure main-branch protection/required checks for humans and automation; create path-scoped data-sync policy that cannot bypass code/spec gates.

**Done when:**
- protected paths cannot merge/push without required checks
- bot/auto-sync changes touching code/spec run the same protected gates
- audited emergency bypass process is documented

**Tests:** repository-policy verification script/API check.

## Phase 01 — Stable records, immutable evidence, atomic releases

### RT-010 — Persistent Record Registry and stable `record_id`
**Priority:** P0  
**Depends on:** RT-001, RT-003  
**Maps to:** T007, T041

**Do:** Add transactional Record Registry, opaque immutable `record_id`, SourceIdentityKey policy, tombstones, and persistent lookup before ID allocation.

**Done when:**
- reingesting the same logical source reuses record_id
- different source identities with same body do not collapse automatically
- IDs never depend on list ordering

**Tests:** reingest/idempotency, redirects, duplicate-content/different-source cases, concurrent allocation.

### RT-011 — Legacy `idx` -> stable ID migration map
**Priority:** P0  
**Depends on:** RT-010  
**Maps to:** T007, T035

**Do:** Build per-dataset `RecordIdMap`; adapt existing index metadata/Trace/citation APIs to carry stable ID while preserving legacy idx for compatibility.

**Done when:**
- all current records map exactly once
- historical fixture idx values resolve to stable IDs
- new durable schemas no longer require idx identity

**Tests:** one-to-one mapping, tombstone, replay compatibility.

### RT-012 — Immutable SourceSnapshot store
**Priority:** P0  
**Depends on:** RT-010  
**Maps to:** T047

**Do:** Implement immutable source snapshot/evidence_text catalog with SHA-256, extractor version, evidence eligibility, access scope, and optional raw object ref.

**Done when:**
- changed source body yields new snapshot under same record
- metadata-only changes do not rewrite source snapshot
- retrieval-only material cannot be mistaken for citation-eligible

**Tests:** versioning, content drift, eligibility enforcement.

### RT-013 — Reversible normalization and EvidenceLocator
**Priority:** P0  
**Depends on:** RT-012  
**Maps to:** T003, T047

**Do:** Implement versioned normalized views and segment offset maps; support TEXT_SPAN first and structured TABLE_CELL/FIGURE_CAPTION/STRUCTURED_FACT interfaces.

**Done when:**
- normalized hits map to exact immutable evidence_text ranges
- expansion/contraction Unicode cases map correctly
- unmappable hits fail rather than approximate

**Tests:** whitespace/NFKC/newline/full-width cases; table/structured locator fixtures.

### RT-014 — Incremental evidence metadata enrichment
**Priority:** P0  
**Depends on:** RT-010, RT-012  
**Maps to:** T007, T008, T009, T010, T012

**Do:** Refactor enrichment to stable IDs, dirty hashes, schema/classifier versions, evidence eligibility, provenance uncertainty, temporal/data-quality metadata.

**Done when:**
- unchanged records are skipped incrementally
- indexable records all have required metadata
- missing required metadata prevents new release publication
- source role never claims independence without evidence

**Tests:** incremental add/change/no-change, missing metadata, source-role/provenance cases.

### RT-015 — Synthetic-summary isolation in primary indexes
**Priority:** P0  
**Depends on:** RT-012, RT-014, RT-004  
**Maps to:** T049, T028

**Do:** Remove generated summary text from primary Vector/BM25/chunk/Graph/Numeric evidence inputs; add optional separately labeled hint index/route.

**Done when:**
- synthetic-only sentinel fact is absent from all primary evidence indexes
- hint hit cannot support Ledger/citation without grounded source evidence
- primary indexes are rebuilt
- recall regression stays within approved gate or replacement source-grounded features restore it

**Tests:** adversarial sentinel E2E; index-content inspection; before/after retrieval benchmark.

### RT-016 — Global immutable release manifest
**Priority:** P0  
**Depends on:** RT-011, RT-012, RT-014, RT-015  
**Maps to:** T041

**Do:** Implement immutable manifest catalog binding spec/decision hashes, dataset, RecordIdMap, source catalog, metadata, identity snapshot, indexes, prompts/config/model, hashes, profile declarations.

**Done when:**
- partial build cannot become current
- incompatible artifacts are rejected
- manifest records full provenance/hashes
- current pointer references immutable manifest only

**Tests:** missing/mismatched artifact, wrong hash/schema/model dim, partial-build tests.

### RT-017 — Atomic activation, request pinning, and generation retention
**Priority:** P0  
**Depends on:** RT-016  
**Maps to:** T041, T053

**Do:** Add atomic current-pointer switch, reference-counted RuntimeSnapshot, hot-reload generation retention, strict startup policy, explicit previous fallback option.

**Done when:**
- in-flight request never mixes generations
- old resources remain alive until last pinned request ends
- invalid current does not silently masquerade as previous
- rollback switches a complete profile+manifest

**Tests:** concurrent request during reload; corrupted current; explicit rollback; resource-retirement test.

### RT-018 — Release backup/restore and GC
**Priority:** P1  
**Depends on:** RT-016, RT-017  
**Maps to:** T041, T056 operational

**Do:** Back up/restore Record Registry, manifest catalog, source catalog, identity metadata; add incomplete-build GC and restore validation.

**Done when:**
- disaster-recovery drill restores stable IDs and a valid prior runtime
- referenced manifests are never GCed
- incomplete unreferenced builds are safely cleaned

**Tests:** restore rehearsal on fixture; retention/GC tests.

## Phase 02 — Citation, claim support, state machine, verifier

### RT-020 — Exact grounding rewrite on SourceSnapshot
**Priority:** P0  
**Depends on:** RT-013  
**Maps to:** T003, T032

**Do:** Refactor citation grounding to immutable EvidenceRefs. Fuzzy methods may locate; accepted result must be an exact evidence_text locator. Remove AI-summary evidence fallback and body-start/query-snippet fallback.

**Done when:**
- final grounding is EXACT or INVALID
- invalid citation cannot enter final response
- multiple non-contiguous spans supported

**Tests:** exact/fuzzy-locate-to-exact, normalized mapping, no-match, summary-only, multiple-span cases.

### RT-021 — Typed claim/evidence relation and entailment
**Priority:** P0  
**Depends on:** RT-020  
**Maps to:** T004, T046, T048

**Do:** Make claim mapping produce DIRECT_SUPPORT/PREMISE_SUPPORT/ATTRIBUTION/CONTRADICTS/BACKGROUND relations over EvidenceRefs; wire entailment/support verification into production.

**Done when:**
- BACKGROUND/CONTRADICTS never counted as support
- vendor statement supports attribution, not unqualified truth
- relation failure cannot silently support claim

**Tests:** support/contradiction/attribution/background cases; malformed/timeout entailment path.

### RT-022 — NumericFact provenance and deterministic verification
**Priority:** P0  
**Depends on:** RT-020  
**Maps to:** T029, T046

**Do:** Store original numeric value/unit/scope EvidenceRef plus normalized value and transformation version; deterministic unit/dimension/scope validation.

**Done when:**
- Gb/s vs GB/s mismatch caught
- per-device vs aggregate mismatch caught
- converted value retains exact source provenance

**Tests:** unit conversion, dimension mismatch, temporal/scope numeric cases.

### RT-023 — Claim coverage gate
**Priority:** P0  
**Depends on:** RT-021, RT-022  
**Maps to:** T004, T052

**Do:** Classify every factual-looking declarative span in generated text into a typed claim or non-factual text; hedged/predictive/attributed language remains claim-bearing.

**Done when:**
- unmapped factual sentence blocks SUPPORTED
- hedged unsupported claims cannot escape checks
- claim coverage metric appears in Trace/benchmark

**Tests:** hidden factual sentence, modal/hedged claims, list/table wording.

### RT-024 — Canonical AnswerStateMachine
**Priority:** P0  
**Depends on:** RT-021, RT-023  
**Maps to:** T005, T006, T052

**Do:** Implement one versioned deterministic state machine as sole production answer-status authority; initial verification NOT_RUN; explicit technical-failure versus evidence-insufficiency transitions.

**Done when:**
- direct SUPPORTED assignments outside approved state module are absent
- critical missing/unsupported/conflict cannot yield SUPPORTED
- no-evidence deterministic abstention can be UNSUPPORTED without verifier
- technical inability to validate presented claims yields UNVERIFIED

**Tests:** complete transition-table suite; architecture scan for illegal terminal writes.

### RT-025 — Fail-safe final verifier contract
**Priority:** P0  
**Depends on:** RT-021, RT-024  
**Maps to:** T005, T046, T052

**Do:** Restrict verifier input to question/scope, atomic claims, exact EvidenceRefs, deterministic results; remove verifier-authored final answer behavior; map timeout/malformed/empty/429/5xx/exception to UNVERIFIED.

**Done when:**
- no technical failure can become PASSED
- verifier never sees Generator hidden reasoning/unselected text
- verifier output is structured findings only

**Tests:** failure injection matrix; input-leak test; semantic-fail case.

### RT-026 — Bounded repair loop
**Priority:** P0  
**Depends on:** RT-024, RT-025  
**Maps to:** T052

**Do:** Wire max-two repair cycles: relocate/remap, delete/qualify noncritical claim, targeted retrieval for critical gaps, regenerate from Evidence Package, reverify.

**Done when:**
- core unsupported claim cannot be deleted then declared complete
- repair exhaustion has deterministic terminal state
- every repair transition is traced

**Tests:** grounding repair, missing critical fact, unsupported minor claim, final-verifier failure after last attempt.

### RT-027 — Terminal renderer and post-verification SSE
**Priority:** P0  
**Depends on:** RT-024, RT-026  
**Maps to:** T037, T052

**Do:** Buffer factual draft until final state; apply boundary/uncertainty wording after verifier/state transition; stream finalized content while emitting progress early.

**Done when:**
- user never receives an unverified full factual draft in normal new profile
- final wording matches terminal status
- time-to-first-status/time-to-final-answer measured

**Tests:** SSE SUPPORTED/PARTIAL/UNSUPPORTED/UNVERIFIED; no-premature-token assertion.

### RT-028 — Citation/API schema hardening
**Priority:** P0  
**Depends on:** RT-020, RT-024  
**Maps to:** T032, T033, T037

**Do:** Emit stable record/snapshot IDs, locators, support relations, verification status, degraded capabilities, diagnostics profile/manifest IDs.

**Done when:**
- final response contains zero invalid citations
- old required fields remain compatible
- new schema version documented

**Tests:** serialization/backward compatibility; invalid-citation filtering.

### RT-029 — Frontend evidence-state rendering
**Priority:** P1  
**Depends on:** RT-028  
**Maps to:** T033

**Do:** Render support/contradiction/background separately; UNVERIFIED distinct; PARTIAL supported/unresolved sections; minimal policy-permitted spans only; cache schema invalidation.

**Done when:**
- invalid citation object cannot render even from stale client state
- mobile/desktop layouts pass
- source roles categorical and non-misleading

**Tests:** frontend integration/visual regression + stale-cache fixture.

## Phase 03 — Retrieval architecture and Evidence Package

### RT-030 — Finish retrieval extraction from `server.py`
**Priority:** P0  
**Depends on:** RT-004, RT-017  
**Maps to:** T014

**Do:** Move remaining core Vector/BM25/Graph/fusion implementation behind retrieval interfaces with centralized RuntimeSnapshot resources; server keeps API glue only.

**Done when:**
- server contains no core search algorithm implementation
- each route independently testable
- parity/known deltas documented

**Tests:** route unit tests; `/api/search` parity; degraded-index cases.

### RT-031 — High-recall fusion candidate pool
**Priority:** P0  
**Depends on:** RT-030, RT-011  
**Maps to:** T015

**Do:** Remove pre-rerank final Top25 truncation; use stable-ID union/RRF candidate pool and retain per-route ranks/features.

**Done when:**
- single-route relevant outliers survive to rerank under benchmark cases
- configurable caps/route floors active
- RRF never labeled final evidence score

**Tests:** old-Top25 outlier fixture; fusion strategy benchmark.

### RT-032 — Real content-aware reranker for all modes
**Priority:** P0  
**Depends on:** RT-031, RT-015  
**Maps to:** T016, T051

**Do:** Ensure reranker consumes query + source-grounded content. Implement/choose local FAST reranker or bounded remote fallback; calibrate multi-batch stability.

**Done when:**
- pure rank transform cannot satisfy reranker interface tests
- FAST actually reranks content
- failure fallback never clears candidate set

**Tests:** pairwise/listwise relevance benchmark; batch stability; timeout fallback.

### RT-033 — Requirement/route reserve pool
**Priority:** P0  
**Depends on:** RT-031  
**Maps to:** T050

**Do:** Protect eligible candidates for critical requirements, comparison object×dimension, independent sources, and plausible route outliers before rerank.

**Done when:**
- low-quality route junk below eligibility floor is not reserved
- dominant-entity comparison cannot crowd out other required entities

**Tests:** A/B/C comparison imbalance; route-noise case.

### RT-034 — Mandatory EvidencePolicyEngine
**Priority:** P0  
**Depends on:** RT-014, RT-021, RT-022  
**Maps to:** T009, T010, T022, T043

**Do:** Implement shared deterministic hard-rule engine across FAST/RESEARCH/DEEP for coverage, provenance, self-report, freshness, conflict, numeric/relation/citation eligibility, access scope.

**Done when:**
- FAST cannot bypass hard rules
- model Grader cannot override a hard fail
- reasons are traceable/machine-readable

**Tests:** self-report, stale-current, missing entity, conflict, numeric, relation-policy cases.

### RT-035 — Evidence Selector production integration
**Priority:** P0  
**Depends on:** RT-032, RT-033, RT-034  
**Maps to:** T017

**Do:** Make selected evidence—not raw reranked/all results—the only support candidate set; add safe deterministic fallback only if it enforces core policies.

**Done when:**
- Selector output controls downstream Ledger/Generator
- Selector empty -> gap/abstain, never raw dump fallback
- provenance redundancy and source diversity behave correctly

**Tests:** all_results contamination test; empty selection; repost cluster.

### RT-036 — Contextual chunk retrieval with exact parent locators
**Priority:** P0/P1  
**Depends on:** RT-013, RT-030, RT-015  
**Maps to:** T028

**Do:** Build/use source-grounded chunk indexes, parent aggregation, exact locator retention, no generated-summary chunks.

**Done when:**
- long-document tail facts recall improves/non-regresses
- chunk hit reliably returns parent stable ID + EvidenceLocator

**Tests:** long-document benchmark; parent/offset cases.

### RT-037 — Canonical Evidence Package builder
**Priority:** P0  
**Depends on:** RT-035, RT-034, RT-020  
**Maps to:** T031

**Do:** Replace raw `build_context` on new path with requirement-organized Evidence Package; mandatory support/conflict/conditions; source/provenance/time metadata; exact refs.

**Done when:**
- Generator new path can only accept EvidencePackage type
- critical conflicts cannot be token-pruned silently
- package hash/evidence IDs enter Trace

**Tests:** type/interface rejection of raw results; context packing cases.

### RT-038 — Context-capacity and source-grounded compression
**Priority:** P1  
**Depends on:** RT-037  
**Maps to:** T031

**Do:** Define mandatory context set; if too large, use non-evidentiary structured compression linked to exact refs, narrower partial answer, or context_capacity_exceeded abstention.

**Done when:**
- mandatory evidence never silently truncated
- compressed text cannot itself count as evidence

**Tests:** tiny-context forced overflow; conflict-preservation case.

### RT-039 — Generation input allowlist enforcement
**Priority:** P0  
**Depends on:** RT-037  
**Maps to:** T037

**Do:** Typed Generator interface accepts only query/scope, verified premises, Evidence Package, approved system/style instructions; reject Trace/raw retrieval/prior unverified prose.

**Done when:**
- unselected candidate unique sentinel never appears in model input
- prior UNVERIFIED answer sentinel never enters factual context

**Tests:** input-capture integration tests.

## Phase 04 — Query integrity and agentic orchestration

### RT-040 — Structured verified conversation store
**Priority:** P0  
**Depends on:** RT-011, RT-020, RT-024  
**Maps to:** T042

**Do:** Persist claim-level verified premises/evidence refs by conversation; raw client history remains untrusted conversational context.

**Done when:**
- PARTIAL reuses only individually verified claims
- UNVERIFIED prose cannot become premise
- temporal provenance retained

**Tests:** multi-turn contamination/freshness cases.

### RT-041 — Deterministic semantic-diff safeguards
**Priority:** P0  
**Depends on:** RT-040  
**Maps to:** T042

**Do:** Compare entities/time/negation/modality/numbers/comparison/scope deterministically where possible; uncertain critical diff rejects rewrite/escalates.

**Done when:**
- entity/time/negation rewrite errors are caught
- model diff failure cannot bless a bad rewrite

**Tests:** adversarial rewrite cases.

### RT-042 — FAST mode correctness path
**Priority:** P0  
**Depends on:** RT-032, RT-034, RT-035, RT-037, RT-024  
**Maps to:** T018

**Do:** Remove FAST default SUFFICIENT shortcut. Route simple queries through bounded retrieval+rereank+policy+selection+Evidence Package+verification while skipping only unnecessary planning/loops.

**Done when:**
- FAST does not call full Planner unnecessarily
- FAST cannot skip evidence/verification gates
- simple-query latency benchmark is recorded

**Tests:** simple fact supported, stale/self-report/numeric hard-fail, latency baseline.

### RT-043 — Typed ResearchState and canonical orchestrator wiring
**Priority:** P0  
**Depends on:** RT-035, RT-040, RT-041  
**Maps to:** T018-T024, T037

**Do:** Refactor orchestrator into canonical flow using stable IDs, selected evidence, degradation state, manifest pinning, and shared mode semantics.

**Done when:**
- all mode state serializable/traceable
- `agentic_state.all_results` is not used as final generation context
- selected_evidence/Ledger/EvidencePackage stay connected

**Tests:** actual orchestrator integration with mini runtime.

### RT-044 — Requirement decomposition and Planner hardening
**Priority:** P0  
**Depends on:** RT-041, RT-043  
**Maps to:** T019, T020

**Do:** Ensure requirements map to original intent, comparison matrix/time/source needs, strict schema/fallback, no silent ambiguity choice.

**Done when:**
- comparison/trend/multi-entity coverage complete on eval set
- ambiguous scope produces requirements/assumption rather than hidden choice

**Tests:** decomposition evaluation; malformed planner output fallback.

### RT-045 — Multi-document workers wired into orchestrator
**Priority:** P0  
**Depends on:** RT-036, RT-043, RT-044  
**Maps to:** T038

**Do:** Confirm trigger in Planner/Orchestrator; select max bounded docs; run isolated document workers; exact EvidenceRefs; merge into Ledger before grading.

**Done when:**
- mode triggers for cross-document cases and not simple facts
- worker never sees other-document conclusions/draft
- relevant/no-evidence represented correctly

**Tests:** original multi-doc Cases A-F plus worker failure case.

### RT-046 — Cross-document packet cache scoping
**Priority:** P1  
**Depends on:** RT-045, RT-017  
**Maps to:** T038, T053

**Do:** Optional immutable cache keyed by manifest/profile/snapshot/requirement/model/prompt/schema/access scope.

**Done when:**
- cache cannot cross incompatible profiles/access scopes
- stale snapshot never reused

**Tests:** cache-key isolation, manifest change invalidation.

### RT-047 — Ledger + semantic Grader integration
**Priority:** P0  
**Depends on:** RT-034, RT-043, RT-045  
**Maps to:** T021, T022

**Do:** Ledger tracks requirement evidence/provenance/time/conflict/searched-no-evidence/degradation; semantic Grader runs only where needed but never overwrites hard rules.

**Done when:**
- hard fail persists despite model “sufficient”
- Grader technical failure cannot become SUFFICIENT

**Tests:** hard-rule override attack; timeout/malformed Grader.

### RT-048 — Gap analysis and targeted retrieval
**Priority:** P0  
**Depends on:** RT-047  
**Maps to:** T023, T024

**Do:** Generate gap-bound queries for missing fact/entity/time/independent source/conflict/numeric condition/ambiguous scope; dedup and anti-drift.

**Done when:**
- every new query points to an unresolved requirement/gap
- impossible gap can stop rather than loop

**Tests:** gap type suite; repeated-query prevention.

### RT-049 — Stopping and Knowledge Boundary
**Priority:** P0  
**Depends on:** RT-048, RT-024  
**Maps to:** T025, T026

**Do:** Bound rounds/tool calls; stop on sufficient, no-new-evidence, impossible gap, unresolved conflict, max rounds; build deterministic boundary without claiming “does not exist” from “not in DB”.

**Done when:**
- runaway loop impossible under config
- early no-evidence exits use canonical state builder

**Tests:** no-new-evidence, max-round, conflict, knowledge-boundary wording.

## Phase 05 — Runtime safety and degradation

### RT-050 — Capability failure matrix
**Priority:** P0  
**Depends on:** RT-024, RT-043  
**Maps to:** T053

**Do:** Encode continue/degrade/block/unverified rules for all routes and critical stages; add `degraded_capabilities[]` to ResearchState/Trace.

**Done when:**
- relation-critical Graph differs from optional Graph failure
- Grader/grounding/entailment/verifier cannot silently skip
- no query-snippet valid-grounding fallback exists

**Tests:** matrix table-driven failure injection.

### RT-051 — Request task group and disconnect cancellation
**Priority:** P0  
**Depends on:** RT-043, RT-050  
**Maps to:** T053

**Do:** Propagate cancellation token/task group through retrieval, LLM, workers, repair; detect SSE disconnect; abandon non-cancellable remote calls safely.

**Done when:**
- disconnect stops useful work
- late results cannot mutate cancelled state
- semaphore/resources always released

**Tests:** real SSE disconnect integration; abandoned-call telemetry.

### RT-052 — Deadlines, retries, and remaining-budget checks
**Priority:** P0  
**Depends on:** RT-050, RT-051  
**Maps to:** T053

**Do:** Version profile-based stage/total deadlines; retry only retryable errors when request active and time/budget remains.

**Done when:**
- no retry after cancellation
- deadlines bounded in every stage
- timed-out factual draft not emitted

**Tests:** timeout/retry/cancel/deadline exhaustion cases.

### RT-053 — Queue/backpressure admission
**Priority:** P1  
**Depends on:** RT-051  
**Maps to:** T053

**Do:** Bounded request queues/semaphores; 429 Retry-After for admission limits; 503 for required backend outage; resource saturation telemetry.

**Done when:**
- queue cannot grow unbounded
- load test shows no state leakage or semaphore leak

**Tests:** burst/load, queue-full status codes.

### RT-054 — Request-state isolation stress
**Priority:** P0  
**Depends on:** RT-051, RT-053  
**Maps to:** T053

**Do:** Stress actual orchestrator/API with parallel queries carrying unique sentinel state.

**Done when:**
- zero sentinel leakage across requests
- Trace/manifest/selected evidence remain request-correct

**Tests:** initial CI target 50 concurrent; production-capacity nightly profile.

### RT-055 — Trace retention/redaction hardening
**Priority:** P1  
**Depends on:** RT-017, RT-043  
**Maps to:** T001, T056

**Do:** Default ID/hash/minimal-span traces, secret scrubbing, debug-mode access/retention, degradation/state transitions, manifest/identity IDs.

**Done when:**
- secrets never persist
- replay-required version refs survive retention
- expired traces cleanup/audit works

**Tests:** redaction fixtures; retention cleanup; trace completeness.

## Phase 06 — Entity Resolution V2 lifecycle completion

### RT-060 — Entity schema + opaque IDs + alias many-to-many
**Priority:** P0  
**Depends on:** RT-017  
**Maps to:** ER-010..014, ER-020..022

**Do:** Define Entity, Mention, Alias, ResolutionDecision, Merge/Split mutation schemas using opaque IDs and versioned provenance.

**Done when:**
- rename/type correction leaves ID stable
- alias ambiguity represented without forced uniqueness

**Tests:** schema/migration/alias ambiguity.

### RT-061 — Transactional IdentityStore backend
**Priority:** P0  
**Depends on:** RT-060  
**Maps to:** ER-082, ER-083

**Do:** Repository abstraction with SQLite single-writer guard or Postgres for multi-writer; uniqueness constraints and snapshots.

**Done when:**
- startup rejects unsafe SQLite multi-writer topology
- atomic writes/rollback work

**Tests:** transaction/constraint/backend topology.

### RT-062 — Candidate generators and blocking
**Priority:** P0  
**Depends on:** RT-060  
**Maps to:** ER-030..034

**Do:** Exact/normalized, fuzzy/trigram, transliteration/acronym, optional embedding recall; unified TopN interface and features.

**Done when:**
- hard negatives not auto-link
- ambiguous acronyms return multi-candidates
- candidate evaluation report generated

**Tests:** bilingual/acronym/typo/hard-negative gold cases.

### RT-063 — Formal resolver states and deterministic resolver
**Priority:** P0  
**Depends on:** RT-061, RT-062  
**Maps to:** ER-040, ER-043

**Do:** Replace terminal LOW_CONFIDENCE semantics with LINK/NEW/AMBIGUOUS/BLOCKED; manual/strong-ID/exact/block rules before LLM.

**Done when:**
- deterministic cases do not call LLM
- strong-ID conflict blocks auto-link
- provisional NEW policy works

**Tests:** state table; strong-ID conflict; block rules.

### RT-064 — Constrained LLM adjudicator
**Priority:** P0  
**Depends on:** RT-063  
**Maps to:** ER-041, ER-042, ER-053

**Do:** Build minimal context features; LLM can only choose supplied candidate or NEW/AMBIGUOUS/BLOCKED; evidence input marked untrusted.

**Done when:**
- fabricated entity ID rejected
- malformed/injection output safely falls back

**Tests:** adversarial resolver suite.

### RT-065 — Locked ER gold set and calibration
**Priority:** P0  
**Depends on:** RT-062, RT-064  
**Maps to:** ER-050..053

**Do:** Separate tuning/evaluation splits by entity class; establish baseline and pre-register class-specific auto-link/candidate/abstain gates.

**Done when:**
- no arbitrary self-confidence thresholds
- release report includes class-specific metrics and error analysis

**Tests:** evaluation reproducibility/holdout integrity.

### RT-066 — Manual Override Store
**Priority:** P0  
**Depends on:** RT-061, RT-063  
**Maps to:** ER-060

**Do:** Authenticated audited link/unlink, alias add/block, rename with owner/reason/validity/review_due/status; STALE_REVIEW_REQUIRED semantics.

**Done when:**
- rebuild does not erase active overrides
- conflicting strong ID can block stale override from high-confidence serving

**Tests:** precedence/expiry/conflict/audit.

### RT-067 — Merge operation with dry-run and audit
**Priority:** P0  
**Depends on:** RT-066  
**Maps to:** ER-061

**Do:** Offline merge plan, impact preview, confirmation, redirects/tombstones, immutable mutation event, checkpointed re-materialization.

**Done when:**
- no serving graph mutated in place
- aliases/mentions affected deterministically
- rollback plan recorded

**Tests:** merge small/high-impact fixture; interrupted batch resume.

### RT-068 — Split/unmerge and compensating mutations
**Priority:** P0  
**Depends on:** RT-067  
**Maps to:** ER-062, ER-063

**Do:** Reassign mentions/evidence explicitly; detect dependent later mutations; use compensating operation instead of blind rollback.

**Done when:**
- post-merge later mentions handled correctly
- conflicting mutation history blocks unsafe rollback

**Tests:** merge->new mentions->unmerge; alias/relation reassign.

### RT-069 — Atomic Entity create concurrency
**Priority:** P0  
**Depends on:** RT-061, RT-063  
**Maps to:** ER-082

**Do:** DB uniqueness/transaction/retry for simultaneous NEW candidates.

**Done when:**
- initial stress target 32 concurrent creates results in exactly one canonical/provisional entity

**Tests:** concurrency stress + crash/retry.

### RT-070 — Identity snapshot publish and global-manifest binding
**Priority:** P0  
**Depends on:** RT-067, RT-068, RT-069  
**Maps to:** ER-083, T041

**Do:** Build immutable identity snapshot, validate, then include in global release manifest; serving never reads partial identity build.

**Done when:**
- request pins exact identity snapshot
- previous snapshot rollback works

**Tests:** partial build, switch, rollback.

### RT-071 — Legacy node/alias migration and duplicate audit
**Priority:** P0  
**Depends on:** RT-070, RT-062  
**Maps to:** ER-090..093

**Do:** Assign stable IDs to legacy nodes as migration identities, seed aliases, generate duplicate/high-impact review report; no mass auto-merge.

**Done when:**
- all legacy nodes have IDs/migration provenance
- ambiguous alias remains multi-candidate
- high-impact review queue produced

**Tests:** migration fixture/review report.

### RT-072 — Rebuild evidence-backed V2 mentions/relations
**Priority:** P0  
**Depends on:** RT-071, RT-013  
**Maps to:** ER-094, ER-070..073

**Do:** Re-extract from original source snapshots; legacy edges only hints; relation mentions carry EvidenceRefs.

**Done when:**
- V2 relation materialization traceable to source mention/evidence
- merge/split can trigger affected rematerialization

**Tests:** source->mention->relation lineage; mutation rematerialization.

### RT-073 — Query entity parser/resolver
**Priority:** P0  
**Depends on:** RT-062, RT-065, RT-070  
**Maps to:** ER-100..104

**Do:** Parse entity mentions/strong IDs; resolve top-k candidates without graph mutation; ambiguity causes bounded graph expansion/downweight/skip.

**Done when:**
- exact/acronym/ambiguous/unknown/typo cases have metrics
- resolver failure never crashes Vector/BM25 QA

**Tests:** query resolver suite + fallback integration.

### RT-074 — Entity Admin API/CLI and sanitization
**Priority:** P1  
**Depends on:** RT-066, RT-067, RT-068  
**Maps to:** ER-120, ER-123

**Do:** Authenticated search/inspect/alias/link/unlink/merge/split/rename; sanitize display/control chars; append-only audit.

**Done when:**
- all mutations authenticated/audited
- XSS/control-character cases safe

**Tests:** auth, audit, sanitization, dry-run confirmation.

### RT-075 — Entity quality/performance monitoring and shadow
**Priority:** P0/P1  
**Depends on:** RT-065, RT-072, RT-073  
**Maps to:** ER-110, ER-111, ER-121, ER-122

**Do:** Shadow ingest/query resolution; report auto-link/new/ambiguous/false-link candidates, latency/cost/cache; minimum representative window.

**Done when:**
- >=1,000 representative events + 7 days or approved equivalent replay before activation
- class-specific rollback gates defined

**Tests:** shadow non-interference; report schema; injected mismatch.

## Phase 07 — Graph-V2 and relation-aware retrieval

### RT-080 — Relation ontology/versioned GraphStatement
**Priority:** P0  
**Depends on:** RT-072  
**Maps to:** T044, T027

**Do:** Version predicate ontology and typed GraphStatement with polarity/modality/time/scope/EvidenceRefs; co-occurrence separate.

**Done when:**
- ungrounded/synthetic-only relation cannot enter high-confidence graph
- direction/predicate/evidence saved

**Tests:** ontology validation; negated/planned/co-occurrence cases.

### RT-081 — Semantic edge extraction + validation
**Priority:** P0  
**Depends on:** RT-080, RT-020  
**Maps to:** T027, T046

**Do:** Extract relation candidate from source snapshot, exact-ground, validate predicate/direction against ontology/evidence.

**Done when:**
- wrong direction/predicate rejected
- multiple evidence refs allowed

**Tests:** relation extraction gold set.

### RT-082 — Graph Query Intent and composition validator
**Priority:** P0  
**Depends on:** RT-073, RT-080  
**Maps to:** T045

**Do:** Only ontology-known predicates/groups; unknown/unauthorized compositions discovery-only; bounded 2-hop.

**Done when:**
- fabricated predicate rejected
- A->B+B->C not automatically A->C

**Tests:** composition/adversarial intent suite.

### RT-083 — Relation-aware Graph Retriever
**Priority:** P0  
**Depends on:** RT-081, RT-082, RT-030  
**Maps to:** T039

**Do:** Stable-ID seeds, predicate/direction/time/grounding-aware traversal, hub penalty, path score breakdown, record aggregation via edge EvidenceRefs.

**Done when:**
- all 1-hop no longer uniform +0.35
- output explains matched paths and score features

**Tests:** relation retrieval benchmark; hub/direction/grounding cases.

### RT-084 — Independent relation-critical policy gate
**Priority:** P0  
**Depends on:** RT-034, RT-083  
**Maps to:** T043, T039

**Do:** EvidencePolicyEngine detects relation claims independently of Router and checks required graph/text evidence method.

**Done when:**
- router misclassification cannot make weak relation evidence SUPPORTED

**Tests:** intentionally wrong router result relation cases.

### RT-085 — Graph-V2 benchmark versus legacy
**Priority:** P0  
**Depends on:** RT-083, RT-084  
**Maps to:** T034, ER-112

**Do:** Locked relation-specific precision/recall/MRR/nDCG/path/grounding/hub/useful-multihop benchmark plus core QA non-regression.

**Done when:**
- gain/no-gain conclusion machine-readable
- tuning and final eval splits separate

**Tests:** benchmark reproducibility.

### RT-086 — Partial Graph-V2 activation profile
**Priority:** P0  
**Depends on:** RT-075, RT-085, RT-017  
**Maps to:** ER-112, T055

**Do:** Named profile enabling Graph-V2 only for high-confidence eligible queries/entities; legacy Graph remains rollback.

**Done when:**
- shadow/canary diff available
- low-confidence ambiguity safely skips/downweights

**Tests:** profile switching; partial eligibility.

### RT-087 — Full Graph-V2 activation gate
**Priority:** P1/conditional  
**Depends on:** RT-086, RT-112  
**Maps to:** ER-113

**Do:** Activate full stable-ID Graph only if relation-specific meaningful gain + no core regression + canary passes.

**Done when:**
- if gain gate fails, ticket records NOT_ACTIVATED_BY_GATE rather than falsely DONE
- if pass, full profile has rollback to previous graph/identity snapshot

**Tests:** release-gate evaluation.

## Phase 08 — API, UI, trace, replay

### RT-090 — Unified done-event/state API
**Priority:** P0  
**Depends on:** RT-027, RT-028, RT-050  
**Maps to:** T006, T037

**Do:** Standardize answer_status, verification_status, evidence_summary, degraded capabilities, trace/profile diagnostics across all exits including early abstention/error.

**Done when:**
- every terminal response uses canonical state builder
- legacy clients remain functional

**Tests:** endpoint contract matrix.

### RT-091 — Claim-aware reference cards
**Priority:** P1  
**Depends on:** RT-029, RT-090  
**Maps to:** T033

**Do:** Show exact policy-permitted spans, claim support IDs, contradiction/background states, source role, snapshot drift warning.

**Done when:**
- user can audit what each source supports without implying full snapshot exposure

**Tests:** UI integration/permissions fixtures.

### RT-092 — Replay fidelity modes
**Priority:** P1  
**Depends on:** RT-055, RT-017  
**Maps to:** T035

**Do:** Label HISTORICAL_EXACT / HISTORICAL_ARTIFACTS_CURRENT_MODEL / CURRENT_COMPARISON / PARTIAL_REPLAY; report manifest/model/prompt differences.

**Done when:**
- replay never claims exact when required historical inputs absent
- one command replays case group and outputs machine diff

**Tests:** exact vs partial replay fixtures.

### RT-093 — Human Review separation from locked holdout
**Priority:** P1  
**Depends on:** RT-002, RT-092  
**Maps to:** T036, T054

**Do:** Human-confirmed cases enter development regression by default; separate blinded holdout refresh process; failure-stage labels.

**Done when:**
- unconfirmed feedback never becomes ground truth
- release holdout not auto-contaminated by tuned production cases

**Tests:** dataset provenance/holdout integrity.

### RT-094 — Full audit/trace UI policy
**Priority:** P2  
**Depends on:** RT-055, RT-091  
**Maps to:** T056

**Do:** Operator-only trace/audit views respect redaction/access scopes; no secrets/full restricted snapshots exposed.

**Done when:**
- permissions and retention enforced

**Tests:** authorization/redaction fixtures.

## Phase 09 — Benchmarks, CI, release gates

### RT-100 — Retrieval/reranker/evidence benchmark suite
**Priority:** P0  
**Depends on:** RT-032, RT-035, RT-037  
**Maps to:** T034

**Do:** Measure route recall, union recall, outlier retention, reranker nDCG/pairwise, requirement coverage, source independence, redundancy, temporal fit.

**Done when:**
- before/after report tied to spec/git/manifest/model configs

**Tests:** benchmark schema/reproducibility.

### RT-101 — Answer/citation/abstention hard-gate suite
**Priority:** P0  
**Depends on:** RT-025, RT-027, RT-028  
**Maps to:** T034

**Do:** Locked metrics for correctness/completeness/unsupported claims/attribution/time/numeric, exact citations, invalid display, abstention classes.

**Done when:**
- exact citation validity hard gate satisfied
- invalid displayed citation = 0
- verifier technical error treated PASS = 0

**Tests:** release holdout run.

### RT-102 — Multi-document benchmark suite
**Priority:** P0/P1  
**Depends on:** RT-045  
**Maps to:** T034, T038

**Do:** Trigger accuracy, worker precision/span validity, cross-document coverage/redundancy/conflict and answer gain.

**Done when:**
- standard Research vs multi-doc comparison produced

**Tests:** locked multi-doc set.

### RT-103 — ER benchmark suite
**Priority:** P0  
**Depends on:** RT-065, RT-075  
**Maps to:** ER-051/052/102/122, T034

**Do:** Candidate recall, top1/topK, abstain/false-link, class-specific latency/cost/cache and adversarial resolver metrics.

**Done when:**
- class-specific activation gates pre-registered and reported

**Tests:** locked ER eval.

### RT-104 — Real server/orchestrator E2E suite
**Priority:** P0  
**Depends on:** RT-043, RT-049, RT-051, RT-090  
**Maps to:** T037

**Do:** Run actual `/api/chat/stream`/orchestrator with committed mini runtime and deterministic model adapter for all four statuses, repair, cancellation, multi-doc, conversation integrity.

**Done when:**
- hand-crafted Trace simulation no longer stands in for E2E
- required production stages verified by captured execution

**Tests:** E2E suite itself.

### RT-105 — Critical failure-injection suite
**Priority:** P0  
**Depends on:** RT-050, RT-104  
**Maps to:** T005, T053

**Do:** Inject route failures, malformed/timeout/429/5xx for Grader/grounding/entailment/verifier, cache mismatch, manifest corruption.

**Done when:**
- every failure matches capability matrix/state transition

**Tests:** table-driven chaos integration.

### RT-106 — CI tiering and artifact provenance
**Priority:** P0  
**Depends on:** RT-002, RT-100, RT-104  
**Maps to:** T034, T054

**Do:** PR deterministic tier, nightly broader/live-model tier, release gate; artifacts record git/spec/decision/manifest/dataset/identity/model/prompt/schema/config.

**Done when:**
- skipped required suite blocks release green
- live provider flake cannot erase semantic regression

**Tests:** CI config lint; synthetic failed artifact gate.

### RT-107 — Release eligibility evaluator
**Priority:** P0  
**Depends on:** RT-101, RT-102, RT-103, RT-105, RT-106  
**Maps to:** T054, T055

**Do:** Machine-evaluate hard invariants and profile-specific benchmark gates; distinguish core-required from optional Graph activation.

**Done when:**
- failed core hard gate blocks manifest activation
- failed Graph gain gate keeps Graph profile off without blocking core profile

**Tests:** gate matrix fixtures.

### RT-108 — Ticket status from evidence
**Priority:** P1  
**Depends on:** RT-002, RT-107  
**Maps to:** T040

**Do:** Generate ticket/phase completion report from acceptance matrix and artifacts; remove manual “all done” authority.

**Done when:**
- a missing test/artifact automatically leaves ticket incomplete

**Tests:** report generator fixtures.

## Phase 10 — Shadow/canary, rollout, operations, docs

### RT-110 — Full-pipeline shadow framework
**Priority:** P0  
**Depends on:** RT-017, RT-104, RT-107  
**Maps to:** T055

**Do:** Sticky sampled shadow for eligible requests; full/stage modes; privacy/provider eligibility; compare route/evidence/status/citation/latency without affecting user result.

**Done when:**
- shadow cannot alter user output
- sensitive-ineligible cases are skipped with reason
- diff report stratified by query mode/type

**Tests:** shadow non-interference/privacy eligibility.

### RT-111 — Named-profile canary controller
**Priority:** P0  
**Depends on:** RT-110  
**Maps to:** T055

**Do:** Sticky 1->5->25->50->100 rollout by named profile; enforce duration/sample and stratified feature coverage.

**Done when:**
- arbitrary production flag mixtures rejected
- easy FAST-only traffic cannot satisfy DEEP/ER/Graph coverage requirement

**Tests:** assignment/stage-gate simulation.

### RT-112 — Rollback triggers and attribution
**Priority:** P0  
**Depends on:** RT-111, RT-107  
**Maps to:** T055

**Do:** Hard attributable triggers for verifier false-PASS, invalid citation response, state leakage, manifest corruption; baseline-relative quality/latency/error pause rules; unknown attribution pauses investigation.

**Done when:**
- hard trigger can atomically disable affected profile/restore previous profile+manifest+identity
- stale frontend cache does not blindly roll backend without attribution

**Tests:** simulated rollback/pause cases.

### RT-113 — Post-activation drift and Human Review feed
**Priority:** P1  
**Depends on:** RT-111, RT-093  
**Maps to:** T036, T055

**Do:** 1–5% drift shadow for two stable releases; severe sampled failures create review drafts, not automatic golden truth.

**Done when:**
- review drafts carry trace/profile/manifest provenance

**Tests:** drift->review pipeline.

### RT-114 — Production capacity/SLO benchmark
**Priority:** P1  
**Depends on:** RT-053, RT-104  
**Maps to:** T034, T053

**Do:** Establish real expected peak concurrency, p50/p95 final-answer latency, queue/error SLO per mode/profile; replace provisional timeout/load numbers with measured config.

**Done when:**
- production profile has versioned capacity/SLO artifact

**Tests:** load benchmark.

### RT-115 — Migration/rollback/operator documentation
**Priority:** P1  
**Depends on:** RT-112, RT-074  
**Maps to:** ER-124, T037

**Do:** Document record/source/identity schemas, Evidence Package, state machine, profile activation, admin mutation SOP, incident rollback, replay fidelity.

**Done when:**
- a new engineer can operate migration/rollback from docs without relying on hidden tribal knowledge

**Tests:** documentation checklist/runbook dry-run.

### RT-116 — Final core acceptance and completion declaration
**Priority:** P0  
**Depends on:** RT-108, RT-112, RT-114, RT-115  
**Maps to:** final system gate

**Do:** Run all CORE_REQUIRED acceptance, release benchmark, DR/rollback drill, canary criteria for core production profile; produce signed/immutable completion report.

**Done when:**
- all 25 final-spec completion gates pass with artifact links/hashes
- no required suite skipped
- any optional Graph-V2 non-activation is explicitly recorded rather than called done

**Tests:** release gate evaluator over complete artifact set.

---

# Dependency summary

Critical core chain:

```text
RT-001 -> RT-002 -> RT-003 -> RT-004
RT-010 -> RT-011/012 -> RT-013/014 -> RT-015 -> RT-016 -> RT-017
RT-020 -> RT-021/022 -> RT-023 -> RT-024 -> RT-025 -> RT-026 -> RT-027
RT-030 -> RT-031 -> RT-032 -> RT-033/034 -> RT-035 -> RT-037 -> RT-039
RT-040 -> RT-041 -> RT-043 -> RT-044 -> RT-045/047 -> RT-048 -> RT-049
RT-050 -> RT-051/052/053 -> RT-054
RT-104/105 + RT-100/101 -> RT-106 -> RT-107 -> RT-108
RT-110 -> RT-111 -> RT-112 -> RT-116
```

Entity/Graph branch:

```text
RT-060 -> RT-061/062 -> RT-063 -> RT-064 -> RT-065
RT-066 -> RT-067 -> RT-068/069 -> RT-070 -> RT-071 -> RT-072 -> RT-073/075
RT-080 -> RT-081/082 -> RT-083 -> RT-084 -> RT-085 -> RT-086 -> (gate) RT-087
```

Graph-V2 full activation is not on the CORE_REQUIRED critical path; its honest state may be `NOT_ACTIVATED_BY_GAIN_GATE` while the non-Graph core completes.
