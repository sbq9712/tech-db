# Tech-DB Evidence-Centric Adaptive Agentic RAG — Final Remediation Specification

Status: FINAL after adversarial review
Repository: `sbq9712/tech-db`
Reviewed code lineage: `f4a3de9…`; current main at finalization: `3439c27…` (subsequent tunnel-URL-only commit)
Decision Register SHA-256: `78bb1d2b539abd5d4bc195b79e93700f7d32ca132ab83fdec49c0b840b151048`
Adversarial Review SHA-256: `9565cd2bf493fc3ec11ee3c003c18701000795e1d4edb74643d735192f11fb62`

## 0. Executive contract

Tech-DB is considered remediated only when the **actual production path** implements an evidence-centric, fail-safe, version-pinned research pipeline. A module, import, green unit test, or historical ticket checkbox does not count as completion unless the behavior is wired, exercised, benchmarked where required, and accepted by the corresponding release gate.

The system MUST satisfy these core properties:

- stable logical record/entity identity independent of array positions and display names
- immutable, replayable evidence snapshots and exact locators
- strict separation of synthetic hints from factual evidence
- high-recall retrieval before content reranking and evidence selection
- verified conversation context and bounded research loops
- Generator context restricted to selected Evidence Package content
- claim-level support, exact grounding, deterministic checks, entailment, and independent final verification
- deterministic four-state answer semantics with bounded repair
- request-scoped state, cancellation, backpressure, and version pinning
- real integration/E2E acceptance, protected CI/release gates, and staged rollout

Graph-V2 production activation remains explicitly benchmark-gated and may stay off without blocking the non-Graph core, provided the Graph workstream is accurately reported as not activated.

## 1. Authority and governance

### 1.1 Authority order

1. Explicit user rulings captured in the versioned Decision Register.
2. This final specification.
3. Canonical machine-readable spec manifest.
4. Current code behavior.
5. Historical docs, old TK closures, code comments, test names, README checkboxes.

### 1.2 Version binding

The canonical spec manifest and every release manifest MUST record:

- `spec_version`
- `spec_sha256`
- `decision_register_version`
- `decision_register_sha256`
- ticket/ER registry version
- profile registry version

A later code/design change becomes normative only through an explicit spec/decision amendment. Commit messages and inline comments do not silently amend architecture.

### 1.3 Completion classes

Capabilities are classified as:

- **CORE_REQUIRED** — must be implemented and production-ready before core project completion.
- **PROFILE_REQUIRED** — required only for named profiles that enable them.
- **BENCHMARK_GATED_OPTIONAL** — may remain production-off if the gain gate fails; Graph-V2 activation is in this class.

Core-required includes stable identity, evidence snapshots, synthetic isolation, retrieval/rerank/selection ordering, Evidence Package generation boundary, state machine, fail-safe verification, cancellation/state isolation, real acceptance, release manifests, and CI/release gates.

## 2. Migration and compatibility policy

- Migration is incremental; no big-bang rewrite.
- Existing public endpoint URLs and required SSE semantics remain compatible where possible.
- Additive fields and schema-version fields are preferred over incompatible endpoint replacement.
- Introduce `/v2` only if an incompatible contract cannot be represented additively.
- Restore/retain a real named `legacy_hybrid` profile during migration; git-revert alone is not the dual-run/rollback mechanism.
- Keep the legacy profile through canary and at least two stable production releases after full activation before separate deprecation/removal.
- A correctness-critical failure may never silently fall back to a path that skips the failed check and still returns a normal trusted answer.

Emergency security/correctness fixes may use an expedited rollout, but still require targeted regression plus a small canary before 100% activation.

## 3. Stable record identity

### 3.1 Logical Record Registry

Introduce a persistent transactional Record Registry.

```text
RecordRegistryEntry {
  record_id,
  source_identity_key,
  canonical_url?,
  source_origin?,
  created_at,
  tombstoned_at?,
  redirect_from[]?,
  registry_version
}
```

`record_id` is an opaque immutable UUIDv7/ULID-class ID allocated exactly once for a logical record.

Before creating a record, ingest resolves through the registry under transactional uniqueness rules. Reprocessing the same logical source cannot generate duplicate record IDs simply because the job restarted.

### 3.2 SourceIdentityKey

Logical-record identity is governed by an explicit source-key policy, not content similarity alone. Inputs may include canonicalized source URL, upstream source ID, feed identity, and migration mapping.

Rules:

- same logical source with changed body -> same `record_id`, new source snapshot
- different sources with identical text -> separate records; provenance clustering may group them
- redirects/domain migration do not auto-merge solely from URL similarity; migration produces an audited redirect/provenance decision
- content similarity alone never merges logical records

### 3.3 Legacy `idx`

`idx` is snapshot-local compatibility metadata only.

Every dataset snapshot publishes:

```text
RecordIdMap {
  dataset_snapshot_id,
  legacy_idx,
  record_id,
  migration_version
}
```

New durable APIs, Trace, Ledger, citations, caches, and indexes MUST use stable IDs. Tombstoned IDs are never reused.

## 4. Immutable source/evidence model

### 4.1 SourceSnapshot

Citation-eligible source material is immutable and versioned.

```text
SourceSnapshot {
  source_snapshot_id,
  record_id,
  source_url,
  raw_object_ref?,
  raw_sha256?,
  evidence_text,
  evidence_text_sha256,
  ingest_time,
  extractor_version,
  source_format,
  evidence_eligibility,
  access_scope
}
```

`evidence_eligibility` is one of:

- `CITATION_ELIGIBLE`
- `RETRIEVAL_ONLY`
- `QUARANTINED`

Only CITATION_ELIGIBLE snapshots may contribute support to the Ledger or final citations.

If legal/retention constraints prevent retaining an immutable evidence representation, the record may remain retrieval-only. Hash+live URL alone is not enough for replayable final evidence.

### 4.2 Extraction versioning

The immutable evidence target is the stored `evidence_text`, not an implicitly regenerable extraction.

If raw bytes are unchanged but a new extractor produces materially different evidence text, publish a new evidence-text snapshot/version. Old locators remain pinned to the old evidence text.

### 4.3 Normalized view and exact mapping

```text
NormalizedView {
  source_snapshot_id,
  normalizer_version,
  normalized_text,
  offset_map_segments[]
}
```

Offset-map segments must represent one-to-one, one-to-many, and many-to-one normalization transformations.

Backend canonical offsets use Unicode code-point indexes into immutable evidence_text. APIs may additionally provide UTF-16 display offsets for browser rendering.

Any normalized/fuzzy match that cannot resolve to an exact evidence_text range is invalid evidence.

### 4.4 Locator types

```text
EvidenceLocator =
  TEXT_SPAN(start_cp, end_cp)
  TABLE_CELL(table_id, row_key/index, column_key/index, cell_hash, optional_span)
  FIGURE_CAPTION(page/object_id, bbox?, caption_hash, optional_span)
  STRUCTURED_FACT(source_path, exact_value, transform_provenance?)
```

PDF/figure locators include page/object data as applicable.

### 4.5 EvidenceRef

```text
EvidenceRef {
  evidence_id,
  record_id,
  source_snapshot_id,
  locator,
  exact_text,
  evidence_text_sha256,
  source_role,
  provenance_group_id,
  temporal_scope,
  data_quality_flags,
  access_scope
}
```

Citations, numeric facts, semantic graph edges, document-worker claims, and final support relations reference EvidenceRefs.

## 5. Evidence metadata enrichment

Enrichment is incremental, reproducible, and release-versioned.

Dirty detection keys on:

- stable `record_id`
- source snapshot/content hash
- metadata-input hash
- enrichment schema/classifier version

Required metadata for every indexable record includes:

- stable record/source identity
- SourceSnapshot linkage and evidence eligibility
- source type/level/role
- provenance identity or explicit uncertainty
- published/event times when available + temporal status
- content risk flags
- data quality flags
- metadata version
- access scope

Resolved `source_org_id` uses stable Entity ID; unresolved remains null plus raw source label.

An indexable record missing required metadata blocks publication of the new release unless explicitly moved to non-indexable quarantine.

Source-role independence must be conservative. Classifier output cannot upgrade a source to independent without sufficient provenance evidence; uncertain remains `unknown`.

## 6. Provenance policy

Provenance clustering is probabilistic and must not falsely collapse independent evidence.

- hard same-group collapse requires high-confidence lineage evidence
- uncertain lineage retains separate candidate records but receives reduced/uncertain independence weight
- same-origin probability, features, and decision version are traceable
- display representative and independence counting are separate concepts

Reposts may be displayed when useful, but one provenance group counts once toward independence.

## 7. Synthetic-content isolation

Every model-generated summary is marked `synthetic_summary=true` with generator/model/version.

Synthetic text MUST NOT enter primary:

- Vector evidence embeddings
- BM25 evidence corpus
- production semantic Graph relation/entity assertions
- production NumericFact index
- final factual citations

An optional auxiliary hint index may use synthetic text for query expansion/candidate discovery.

Hint-path controls:

- each hint-derived candidate carries `hint_reason`
- a hint cannot satisfy Ledger coverage
- a hint cannot create a factual relation/entity/numeric fact
- a hint cannot count as independent evidence
- supporting the hinted proposition requires a separately grounded EvidenceRef tied to that requirement

Summary-only historical records are retrieval/admin hints, not factual answer evidence.

### 7.1 Migration quality gate

Before removing summaries from primary indexes, establish the current retrieval baseline and build source-grounded replacement features such as title + eligible structured fields + source-grounded chunks.

Full activation of T049 requires:

- no synthetic-only sentinel leakage
- all contaminated primary indexes rebuilt
- critical recall/quality within approved non-regression gate

## 8. Global release manifest and artifact generations

### 8.1 Immutable manifest

Every named production profile references an immutable manifest containing:

- manifest/schema ID/version
- spec + Decision Register hashes
- git SHA
- dataset snapshot
- Record Registry/RecordIdMap version
- source snapshot catalog
- evidence metadata/provenance versions
- identity snapshot
- required retrieval/chunk/graph/numeric artifacts
- prompt/schema/config/model versions
- full SHA-256 hashes
- profile and capability declarations
- publish actor/time/audit metadata

Production SHOULD enforce manifest signing when signing infrastructure exists. At minimum, immutable storage permissions, authenticated publish identity, and append-only publish audit are required.

### 8.2 Artifact dependency graph

The spec manifest declares which artifacts depend on which inputs. Data-only releases rebuild only affected artifacts, but a published release must still be a complete compatible artifact set for its active profile.

Experimental profile-disabled Graph artifacts do not block a non-Graph release.

### 8.3 Atomic publish

Build into immutable generation directories. Validate required artifacts and compatibility, then atomically replace a small `current` pointer with the manifest ID.

Partial builds never become current.

Unreferenced incomplete builds are GCed after a safe retention window.

### 8.4 Startup policy

Default production policy: `STRICT_FAIL_CLOSED`.

An optional named deployment policy may enable `EXPLICIT_PREVIOUS_FALLBACK`, but it must:

- be configured before startup
- emit a critical event
- expose the actual active previous manifest
- never pretend `current` succeeded

No silent fallback.

### 8.5 Request pinning and hot reload

Each admitted request acquires a reference-counted `RuntimeSnapshot` for exactly one manifest/profile/identity generation.

Hot reload swaps the generation factory for new requests only. Old artifact resources are retired after the last pinned request releases them.

### 8.6 Rollback and recovery

Rollback switches a complete previous profile + manifest pair, including its identity snapshot.

Serving identity is snapshot-based. Admin identity mutations build future snapshots and do not mutate the active serving snapshot in place.

Back up and restore-test:

- Record Registry
- manifest store/current pointers
- source snapshot catalog
- identity audit/mutation history
- critical release metadata

## 9. Retrieval layer boundary

Final `server.py` owns only:

- API/auth/validation/guardrails
- request admission + RuntimeSnapshot acquisition
- cancellation/task-group lifecycle
- SSE progress/final serialization

Core search/research/evidence algorithms live in typed modules/services with centralized artifact loading.

Unified result:

```text
RetrievalResult {
  record_id,
  route,
  raw_score,
  rank,
  query_id,
  round_id,
  route_details,
  hit_locators[],
  access_scope
}
```

ACL/access policy is a mandatory seam between candidate retrieval and any Agent/worker/Generator context. Public profile uses allow-all; future restricted profiles use actual filtering. Access-scope fingerprint is part of cache keys.

## 10. High-recall candidate pool

Initial profile defaults are provisional, not invariants:

- Vector TopK 50
- BM25 TopK 50
- Graph TopK 40
- Chunk TopK 50 where enabled
- deduplicated cap: FAST 80; RESEARCH/DEEP 180

Release benchmarks may adjust them through versioned config.

Fusion preserves route ranks/scores/features. RRF is candidate fusion, not trust/relevance truth.

If candidates exceed rerank capacity, apply in order:

1. minimum eligibility floor
2. critical-requirement reserves
3. per-route minimum quotas among eligible candidates
4. entity/dimension/source-independence reserves
5. remaining RRF/route features

A global RRF Top25 truncation before content rerank is forbidden.

Chunk hits aggregate under stable parent record while retaining multiple hit locators.

## 11. Content-aware reranking

A compliant reranker MUST consume query + source-grounded candidate content. Re-labeling RRF/fusion rank as rerank is noncompliant.

FAST prefers a deterministic/local content model. If unavailable, it may use a bounded configured remote reranker; it may not skip reranking and assume sufficiency.

RESEARCH/DEEP may use local first-stage plus bounded GLM listwise reranking when benchmark-justified.

Reranker input:

- title/minimal metadata
- best eligible source-grounded chunk(s)/excerpt(s)
- route features
- optional grounded Graph-path features

Synthetic summary cannot be sole rerank content.

Fallback after reranker failure uses an approved deterministic ranking and marks `degraded_capabilities`; evidence-policy and verification remain mandatory.

## 12. Mandatory EvidencePolicyEngine

Every mode runs a deterministic EvidencePolicyEngine before support can be declared.

It checks, as applicable:

- critical requirement coverage
- required entity/object/dimension coverage
- source eligibility
- provenance independence
- self-report versus requested validation type
- temporal freshness/supersession
- high-severity conflict
- numeric unit/scope/denominator/condition
- relation-claim evidence method
- citation eligibility
- data-quality blockers
- access scope

This engine exists independently of the semantic Grader and cannot be skipped by FAST.

Hard failures cannot be overridden by any model.

## 13. Requirement-aware reserve and Evidence Selector

Before rerank, reserve protects eligible candidates for:

- critical requirements
- comparison object x dimension coverage
- scarce independent groups
- plausible route outliers

After rerank, Evidence Selector chooses support candidates based on:

- content relevance
- requirement coverage
- source/provenance role
- temporal fit
- conflict/condition state
- redundancy

Minimum eligibility/relevance comes before diversity. Quotas never preserve arbitrary junk below the eligibility floor.

Selector failure may use a deterministic safe-selector fallback only if it enforces the same minimum eligibility/provenance/coverage rules. Otherwise no normal factual answer is generated.

`all_results` are research memory only and never generation context.

## 14. Generation input allowlist

The Generator may receive only:

- current user query/scope
- verified structured conversation premises
- canonical Evidence Package
- approved system/style instructions

It MUST NOT receive:

- raw Trace/debug text
- unselected retrieval text
- prior unverified assistant prose as fact
- other workers’ conclusions outside the Evidence Package
- hidden verifier reasoning

This is enforced by typed interfaces and integration tests, not convention alone.

## 15. Query integrity and conversation state

### 15.1 RewriteResult

```text
RewriteResult {
  original_query,
  rewritten_query,
  semantic_diff {
    entities,
    time,
    negation,
    modality,
    numeric_quantities,
    comparison_set,
    dimensions,
    scope,
    intent
  },
  diff_confidence/diagnostics
}
```

Critical fields use deterministic extraction/comparison where practical. Model-generated diff is advisory.

Critical parse/diff uncertainty falls back to original query or escalates to Research; it never blindly trusts the rewrite.

### 15.2 Verified conversation premises

Persist structured claim state server-side when conversation ID exists.

A prior claim can be a premise only when its own claim state has verified supporting EvidenceRefs.

- PARTIAL answer: verified supported claim units may carry forward
- UNVERIFIED answer: only individually verified claim units may carry; prose cannot
- UNSUPPORTED answer: search metadata may help query expansion, not facts
- raw assistant prose: never factual premise

Premises carry temporal scope and source/manifest provenance. Current/latest questions revalidate freshness/supersession before reuse.

User corrections supersede affected conversation premises with traceable provenance.

Novelty uses soft penalties, never blind hard-exclusion of required authoritative baselines.

## 16. Research modes and boundedness

All modes share one typed ResearchState.

### FAST

May skip decomposition, full Planner, multi-document workers, and iterative gap search when policy says unnecessary.

MUST still run:

- retrieval
- content rerank
- EvidencePolicyEngine
- selection
- Evidence Package
- factual claim extraction/mapping
- exact grounding
- deterministic checks
- entailment/support
- AnswerStateMachine
- canonical final factual verification

### RESEARCH

Adds requirements/planning and targeted multi-round retrieval.

### DEEP

Adds broader multi-entity/time/conflict handling and larger but bounded research budget.

Timeouts and total deadlines are profile-configured and benchmark/SLO-derived. Any numeric defaults are provisional configuration, not normative truth.

## 17. Canonical research flow

```text
Original Query
-> Verified Rewrite + Semantic Diff
-> Query Entity Resolution (as needed)
-> Router
-> Decomposition / Planner (as needed)
-> Multi-route Retrieval
-> Eligibility + Requirement/Route Reserve
-> Content Rerank
-> Evidence Selector
-> Multi-Document Processing (when confirmed)
-> Evidence Ledger
-> Conflict Detector
-> EvidencePolicyEngine + Semantic Grader as needed
-> sufficient?
   NO -> Gap Analysis -> targeted retrieval -> bounded repeat
   YES -> Knowledge Boundary -> Evidence Package
-> Generation (buffered)
-> Atomic Claim Extraction + Claim Coverage Gate
-> Claim/Evidence Mapping
-> Exact Grounding
-> Deterministic ID/Numeric/Time/Scope Checks
-> Entailment / Claim Support
-> AnswerStateMachine + bounded repair
-> Independent Final Verifier
-> Final AnswerState transition
-> Terminal Answer Renderer
-> Reference Cards / final SSE
```

The final verifier feeds the state machine; the terminal renderer applies required uncertainty/boundary wording **after** final state is known.

No user factual content is emitted before terminal rendering.

## 18. Multi-document evidence processing

Router proposes `needs_multi_document_reasoning`; Planner/Orchestrator confirms from requirements.

Initial defaults: max 12 documents; 4 concurrent workers per request. Config is benchmarked/versioned.

Worker input is limited to:

- one immutable document snapshot
- assigned requirements/gaps
- current question/scope
- that document’s canonical entity/source/provenance/temporal metadata
- optional synthetic summary marked navigation-only

Worker MUST NOT receive other documents’ conclusions or Generator draft.

Output:

```text
DocumentEvidencePacket {
  record_id,
  source_snapshot_id,
  requirement_results[],
  local_claims[] { claim, typed_support_need, evidence_refs[] },
  numeric_facts[],
  temporal_scope,
  source_role,
  internal_conflicts[],
  unanswered_aspects[],
  worker_version
}
```

A local factual claim without exact eligible EvidenceRef does not enter Ledger support.

`relevant=true/evidence_found=false` is research-state information, not support.

Cross-request packet caches, if enabled, are keyed by:

- manifest/profile
- source snapshot
- requirement fingerprint
- worker model/prompt/schema
- access scope fingerprint

## 19. Evidence Ledger and grading

Ledger tracks per requirement:

- evidence refs
- provenance/independence groups
- source roles
- object/dimension coverage
- temporal fit
- relation/numeric conditions
- conflicts
- searched-but-no-evidence attempts
- degradation state affecting the requirement

Grading consists of:

1. mandatory EvidencePolicyEngine
2. semantic Grader only where sufficiency cannot be safely determined deterministically

Semantic Grader failure cannot be converted to SUFFICIENT. If the answer depends on semantic grading and it cannot run after bounded recovery, support cannot be declared.

## 20. Temporal policy

Current/latest requirements have explicit as-of semantics.

- superseded-only evidence cannot satisfy current/latest
- prior conversation premises are revalidated for temporal compatibility
- time/version updates are distinguished from true contradiction
- Evidence Package carries source/event time and supersession state

## 21. NumericFact model

```text
NumericFact {
  subject_id?,
  metric,
  original_value,
  original_unit,
  original_scope/condition,
  evidence_ref,
  normalized_value?,
  normalized_unit?,
  transform_rule_version?,
  temporal_scope
}
```

Normalized values never lose the original source value/unit/condition provenance.

Dimensionally incompatible values (e.g. Gb/s vs GB/s, device vs system total) cannot be compared as equivalent facts.

## 22. Conflict policy

Normalize by subject, predicate/metric, direction, time, scope/condition, value, and source role.

States:

- AGREE
- CONTRADICT
- DIFFERENT_SCOPE
- VERSION_UPDATE
- UNKNOWN

High-severity contradiction triggers targeted resolution search. Unresolved conflict remains visible in Ledger/Evidence Package/final wording and blocks deterministic supported wording for that proposition.

## 23. Claim coverage and support

### 23.1 Atomic claims

Every declarative factual-looking segment in the generated draft must be classified as:

- mapped factual/epistemic claim
- explicitly non-factual/structural text

Unclassified factual-looking spans trigger repair or UNVERIFIED; they cannot silently escape verification.

Hedged, modal, predictive, and attributed statements are still typed epistemic claims and require appropriate evidence.

### 23.2 Grounding

Fuzzy/semantic methods may locate candidate passages internally, but accepted evidence is exact in pinned evidence_text.

User-facing grounding validity is exact/invalid. Invalid citations are removed.

### 23.3 Claim-to-evidence-set support

Support can use a bounded set of exact EvidenceRefs, including non-contiguous spans and multiple sources.

Relations:

- DIRECT_SUPPORT
- PREMISE_SUPPORT
- ATTRIBUTION
- CONTRADICTS
- BACKGROUND

Only support relations count toward claim support. Background and contradictory evidence may be shown but are not support.

Exact grounding + semantic support + applicable deterministic condition checks are all required.

## 24. Context packing

Evidence Package allocates tokens by requirement, not global score alone.

Mandatory set:

- critical requirement support
- critical unresolved conflict evidence
- required numeric/time/scope conditions
- minimum evidence needed to express source-role/independence distinctions

If the mandatory set exceeds model context, the system must not silently truncate it.

Allowed responses:

1. source-grounded structured compression that retains links to original EvidenceRefs and supplies exact spans for support-critical claims
2. narrower partial answer
3. abstention with `context_capacity_exceeded`

Generated compression is navigation/presentation, not new evidence.

## 25. AnswerStateMachine

Initial verification state is `NOT_RUN`.

User-visible states:

- SUPPORTED
- PARTIALLY_SUPPORTED
- UNSUPPORTED
- UNVERIFIED

Auxiliary verification states include `NOT_APPLICABLE`.

Only the versioned AnswerStateMachine may commit terminal `answer_status`.

### 25.1 SUPPORTED gate

Requires all applicable conditions:

- evidence sufficiency established
- every critical requirement covered
- every factual claim supported
- no unresolved high-severity conflict contradicting asserted certainty
- exact eligible evidence
- deterministic ID/numeric/time/scope checks passed
- claim-coverage gate passed
- final verifier passed

No current factual claim class is exempt from the canonical final verification pipeline. A future deterministic-complete exemption requires explicit claim-class specification, locked tests/benchmark, and spec amendment.

### 25.2 Technical failure rule

If the system can establish evidence absence/insufficiency despite a component failure, use PARTIAL/UNSUPPORTED.

If a technical failure prevents determining support for a claim the answer would present, that claim is UNVERIFIED. Aggregate becomes UNVERIFIED unless the claim is removed and the remaining answer is re-evaluated.

UNVERIFIED is not permission to show arbitrary speculative draft text.

## 26. Bounded repair

Maximum two post-generation repair cycles.

Priority:

1. exact re-location/remapping where evidence exists
2. delete/qualify noncritical unsupported claims
3. targeted retrieval for missing critical support
4. regenerate from updated Evidence Package
5. re-run affected claim/evidence checks
6. recompute terminal state

Verifier does not write final answers; it returns structured findings/repair guidance only.

After repair budget exhaustion:

- verifier technical failure -> UNVERIFIED, with factual draft withheld except verified supported portions
- semantic unsupported claims -> deterministically remove if possible without new generation, otherwise PARTIAL/UNSUPPORTED
- a core unsupported claim cannot be deleted and rebranded as complete

## 27. Independent final verifier

Verifier input is restricted to:

- user question/scope
- atomic claims
- exact EvidenceRefs and source metadata
- deterministic-check outputs

It does not receive Generator hidden reasoning, raw unselected retrieval context, or prior answer prose as authority.

Using the same strongest model is allowed; independence is enforced by isolated call, prompt, and input context.

Timeout, malformed response, empty response, parser error, 429/5xx, or exception never becomes PASS.

## 28. Streaming/final rendering

New profile semantics prioritize verified final content over pre-verification token TTFB.

SSE emits immediate progress/status events.

Factual answer text is buffered until state finalization and terminal rendering. Then finalized content may be streamed in chunks for compatibility/UI behavior.

Measure and gate:

- time to first status
- time to finalized answer
- end-to-end p50/p95

Do not weaken correctness to preserve old first-token latency.

## 29. Entity Resolution V2

### 29.1 Identity model

Stable opaque Entity IDs; canonical names are mutable attributes.

Aliases are many-to-many with provenance, validity, type, block status.

Formal terminal states:

- LINK
- NEW
- AMBIGUOUS
- BLOCKED

`LOW_CONFIDENCE` is diagnostic only.

### 29.2 Resolver layers

1. active manual overrides/block rules
2. validated typed strong IDs
3. exact/normalized aliases
4. fuzzy/trigram
5. transliteration/acronym
6. optional embedding recall
7. constrained LLM adjudication over Top-N candidates
8. abstain/provisional policy

LLM may select provided candidate or NEW/AMBIGUOUS/BLOCKED; fabricated Entity IDs are rejected.

LLM self-confidence is not a production threshold.

Candidate/auto-link/abstain gates are class-specific and are pre-registered from the locked gold set; no arbitrary 0.98 target is normative.

### 29.3 Provisional entities

NEW normally creates a provisional entity.

Provisional identity can annotate retrieval but cannot participate in high-confidence semantic graph traversal/support until promoted through policy.

### 29.4 IdentityStore topology

IdentityStore uses a transactional repository abstraction.

SQLite WAL is allowed only under an explicit single-writer service/process contract.

Any multi-writer deployment must use a shared transactional database such as Postgres before identity mutations are enabled.

Atomic creation is enforced by database uniqueness constraints + transaction/retry.

### 29.5 Manual overrides and mutations

Authenticated append-only audit records:

- actor/owner
- reason
- timestamps
- valid range
- review due
- status
- referenced evidence/decision

Overdue overrides become `STALE_REVIEW_REQUIRED`. Conflicting strong-ID evidence may block them from high-confidence serving pending review.

Merge/split/unmerge are offline/admin jobs:

- dry-run/impact preview
- explicit confirmation
- checkpointed/bounded re-materialization
- no in-place serving-graph mutation
- immutable new identity snapshot published only after validation

Source entity IDs remain tombstoned redirects after merge.

Later dependent mutations prevent blind rollback; use compensating operations.

## 30. Graph-V2

Production Graph-V2 uses stable Entity IDs and evidence-grounded typed edges.

```text
GraphStatement {
  subject_entity_id,
  predicate,
  object_entity_id/value,
  direction,
  polarity,
  modality,
  temporal_scope,
  evidence_refs[],
  extraction_version,
  validation_version
}
```

Co-occurrence is weak discovery only.

Unknown predicates and unauthorized relation compositions are discovery/query expansion only.

Two-hop traversal is bounded and only enabled for explicit multi-hop/relation needs.

Router/Planner are not the sole relation-critical gate: EvidencePolicyEngine independently identifies relation claims and checks required evidence method.

Path scoring is held-out-benchmark calibrated and traceable by feature.

Graph-V2 acceptance is profile-specific:

- experimental benchmark may fail the gain gate without blocking core completion
- full Graph-V2 activation requires no core QA regression + meaningful relation-specific gain + successful canary

Legacy Graph remains available through two stable releases after V2 activation before deprecation.

## 31. Degraded-mode runtime

Every request records `degraded_capabilities[]` regardless of final answer status.

Failure classes:

- retrieval-route failure: may continue if remaining evidence suffices
- reranker failure: approved deterministic fallback + degradation flag
- selector failure: safe deterministic fallback or no normal factual answer
- worker failure: isolate affected requirements and recompute sufficiency
- Grader/grounding/entailment/verifier technical failure: cannot be silently skipped where required
- relation-critical Graph failure: changes requirement sufficiency unless independently satisfied through approved alternative evidence
- Generator failure: error response; no reused/stale answer

Retry requires:

- retryable error class
- request not cancelled
- remaining total deadline
- remaining stage/dependency budget

No retry after disconnect cancellation.

## 32. Cancellation, backpressure, and state isolation

All work runs in a request task group/cancellation context.

On SSE disconnect:

- cancel pending retrieval/model/worker/repair tasks where supported
- mark non-cancellable remote calls abandoned
- never apply late results to cancelled request state
- release semaphores/temp resources in `finally`

Admission:

- 429 + Retry-After for rate/concurrency/queue rejection
- 503 for required backend unavailability independent of client quota

ResearchState, Ledger, selected evidence, repair state, and mutable conversation state are never shared mutable globals.

Stress targets are profile-specific. Initial CI targets (50 concurrent QA requests; 32 concurrent identical entity creates) are starting targets, not universal SLOs. Production readiness also tests expected peak load.

## 33. API and UI

`done` response includes:

```text
answer
answer_status
verification_status
citations[]
claims[]
evidence_summary
searched_record_ids
stop_reason
boundary_message
user_warning?
trace_id
degraded_capabilities[]?
diagnostics? { manifest_id, identity_snapshot_id, profile }
```

Citation schema includes stable record/snapshot identity, exact locators, source role, provenance, support relations, and live URL if available.

UI requirements:

- invalid citations filtered server-side and defensively client-side
- cache schema/version prevents stale invalid-card rendering
- UNVERIFIED styling is never normal trusted styling
- PARTIAL shows supported/unresolved aspects
- SUPPORT / CONTRADICTS / BACKGROUND are visibly distinct
- source role is categorical, not fake probability
- end users see only policy-permitted minimal evidence spans; full snapshots may remain restricted/internal
- dead/changed live URL does not erase pinned audit evidence where retention permits

## 34. ACL and cache boundaries

All candidate-to-context paths pass an AccessPolicy seam before Agent/worker/Generator use.

Cache keys include, as applicable:

- manifest ID
- profile
- source snapshot
- requirement fingerprint
- model/prompt/schema
- access-scope fingerprint

Public deployment uses a public allow-all scope; the architecture cannot assume that forever.

## 35. Trace, privacy, and replay

Trace records:

- profile + manifest/identity snapshot + state-machine version
- original/rewrite/diff/context provenance
- route/round/candidate IDs
- degradation flags
- rerank/selection reason codes
- worker packet IDs
- Ledger/grader/gaps/conflicts
- Evidence Package hash/evidence IDs
- claims, claim-coverage result, mappings, groundings, deterministic checks, verifier result
- retries/repairs/state transitions
- terminal renderer outcome

Default production Trace stores IDs/hashes/minimal spans with secret scrubbing. Full-text debug mode is opt-in, encrypted/access-controlled where appropriate, and short-retention.

Replay modes are explicit:

- `HISTORICAL_EXACT`
- `HISTORICAL_ARTIFACTS_CURRENT_MODEL`
- `CURRENT_COMPARISON`
- `PARTIAL_REPLAY`

Replay never calls itself exact when required raw query/model/artifact state is unavailable.

## 36. Evaluation integrity

Maintain separate datasets:

- development regression set
- tuning/calibration sets
- locked release holdout

Human-confirmed bad cases enter development regression by default. They do not automatically enter the locked holdout.

Holdout refresh uses a controlled blinded process.

Reranker calibration and final evaluation use different splits. Graph scoring calibration and relation final evaluation use different splits.

## 37. Acceptance matrix

Maintain machine-readable:

```text
Ticket/ER DoD -> behavior test(s) -> integration/E2E -> benchmark -> artifact -> required tier/profile
```

Rules:

- importability is smoke only
- ban `or True`, unconditional `True`, no-op assertions
- cross-stage DoD requires real orchestrator/server execution
- required suites must report pass/fail/skip/xfail explicitly
- release-required tests cannot be skipped and still yield green

### 37.1 Required E2E/adversarial coverage

At minimum:

- summary-only fabricated sentinel cannot become evidence/citation
- verifier timeout/malformed/exception never PASS
- grounding miss does not render citation
- old RRF Top25 outlier recovered by high-recall pool+rereank case
- Generator cannot receive unselected retrieval text
- prior UNVERIFIED/unsupported assistant prose cannot become factual premise
- temporal current/latest rejects superseded-only evidence
- comparison object x dimension coverage enforced
- self-report does not become independent validation
- VERSION_UPDATE vs CONTRADICT distinction
- unit/scope numeric mismatch
- relation-critical Graph policy independently enforced
- claim parser coverage catches unmapped factual-looking text
- mandatory context overflow produces explicit capacity handling
- SSE disconnect cancels/abandons work correctly
- concurrent entity create produces one canonical entity
- high-impact merge/split publishes new snapshot atomically
- rollback restores complete compatible profile+manifest+identity snapshot

## 38. Model testing policy

Normal merge CI uses deterministic fake/recorded adapters for external models.

Live-model tests run nightly/release as health/trend evidence and may have one bounded rerun for transport/transient failure. Semantic regression is not retried away.

Hard correctness invariants rely primarily on deterministic/recorded tests and locked controlled benchmarks, not a single live provider call.

## 39. CI and branch protection

Protect main with required checks and reviewed merges.

Automation/bot commits are subject to protection too. A data-only sync workflow may use path-scoped checks, but protected code/spec paths cannot bypass required gates.

Required merge checks:

- canonical spec/dependency/schema/profile lint
- acceptance-matrix completeness
- unit suites
- deterministic mini-index integration/E2E
- critical failure-injection tests
- synthetic-isolation tests
- fast regression
- security/safety checks

Expensive nightly benchmarks do not block every merge, but publishing/full-activating a release requires a fresh passing release gate.

Machine-readable evidence, not README status text, determines completion.

## 40. Shadow/canary rollout

Production runs named profiles only.

Shadow/canary eligibility honors data classification and provider/privacy policy. Sensitive/private requests may be excluded from full double-model shadow and instead validated through approved replay/stage-shadow methods.

Default progression:

- shadow: 10% eligible requests, cost-budget constrained but minimum sample required
- 1% canary: >=4h and >=200 eligible
- 5%: >=12h and >=500
- 25%: >=24h and >=1,000
- 50%: >=24h and >=2,000
- 100% after all gates

Sample counts MUST be stratified across applicable modes/question types/critical features; a pile of easy FAST queries does not validate DEEP/multi-document/ER/Graph behavior.

Low-traffic exception requires equivalent locked replay plus explicit approval, not zero evidence.

Hard stop triggers include attributable:

- verifier technical failure treated as PASS
- displayed invalid citation from active release response
- cross-request state leakage
- incompatible/corrupt manifest activation

If incident attribution is unknown (for example stale frontend cache), pause rollout and investigate rather than blindly auto-rolling back the backend.

Quality/latency gates compare against approved baseline/SLO. Initial default pause rule for latency is sustained p95 >30% regression without justified quality gain; exact thresholds are versioned release policy, not immutable architecture.

Rollback switches a complete previous profile + manifest + identity snapshot.

Keep 1–5% drift shadow for at least two stable releases after full activation.

## 41. ER/Graph rollout

Entity Resolution activation requires:

- class-specific locked resolver metrics/gates
- no duplicate atomic-create failure
- no strong-ID/block-rule integrity failure
- high-impact legacy entity review according to policy
- representative shadow window: >=1,000 events and 7 days, or equivalent locked replay + explicit approval

Graph-V2 full activation additionally requires:

- stable-ID graph
- exact grounded semantic edges
- relation-intent/path evaluation
- no core QA regression
- meaningful relation-specific gain
- successful canary

If gain is not demonstrated, Graph-V2 remains off and the core profile can still complete.

## 42. Operational disaster recovery

Maintain tested backup/restore for:

- Record Registry
- immutable manifest catalog/current pointer history
- source snapshot catalog/object references
- identity mutation audit + snapshot metadata
- Human Review/golden-case metadata

Recovery validation must prove restored stable IDs, manifest compatibility, identity snapshot consistency, and trace/replay references.

## 43. Final completion gate

Core remediation is complete only when all are true:

1. Decision/spec hashes are machine-bound to release artifacts.
2. Stable Record Registry/record IDs replace array-position identity in durable paths.
3. Citation-eligible immutable SourceSnapshots and exact reversible locators exist.
4. Primary indexes contain no synthetic-only factual text; sentinel tests pass.
5. Requests pin complete compatible manifest/profile/identity generations.
6. Server no longer owns core retrieval/research algorithms.
7. RRF/union produces high-recall candidates before real content reranking.
8. Evidence Selector controls what may become generation evidence.
9. Generator input allowlist prevents raw retrieval/debug/prior-unverified leakage.
10. Verified conversation premises are structured, source-pinned, and freshness-aware.
11. FAST cannot assume SUFFICIENT; all modes run EvidencePolicyEngine.
12. Multi-document processing is wired where requirements demand it.
13. Evidence Package is the only factual generation context.
14. Claim-coverage gate ensures no factual-looking sentence escapes verification.
15. Exact grounding + typed support + deterministic numeric/time/scope checks pass for supported claims.
16. Final verifier is fail-safe; technical failure never becomes PASS.
17. AnswerStateMachine is the only production terminal-status authority.
18. UNVERIFIED does not expose arbitrary speculative drafts.
19. Cancellation/backpressure/request-state isolation and artifact-generation pinning pass stress/E2E tests.
20. Entity lifecycle operations are transactional/audited/snapshot-published before Graph-V2 use.
21. Real server/orchestrator acceptance replaces import-only/fake E2E.
22. Evaluation holdout integrity is enforced.
23. Main protection and required CI gates are active for humans and automation.
24. Release benchmark and canary gates pass before full activation.
25. Only explicitly benchmark-gated optional capabilities may remain production-off without blocking core completion.

