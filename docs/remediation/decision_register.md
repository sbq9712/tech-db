# Tech-DB Remediation Decision Register

Baseline: repository `sbq9712/tech-db`, main branch reviewed against the Evidence-Centric Adaptive Agentic RAG adversarial ticket specification. These rulings are normative for the remediation spec.

## A. Authority, scope, and definition of complete

- **Q001 — Authority order.** Final user rulings in this register outrank the final remediation spec, which outranks the canonical spec manifest, which outranks current code behavior, older docs/tests, and historical ticket-closure notes.
- **Q002 — Code/spec conflict.** Current code must conform to the final spec. A later design may differ only through an explicit spec amendment backed by benchmark evidence; silent divergence is forbidden.
- **Q003 — What counts as a later architecture ruling.** Only versioned spec changes, decision-register changes, or an approved architecture decision record count. Commit comments or inline code comments alone are not normative.
- **Q004 — Validation scope.** Revalidate the entire applicable T001–T056 and ER workstream, not only the gaps already found. Existing passing artifacts are evidence, not presumptive completion.
- **Q005 — Module exists but is not wired.** Mark it `PARTIAL`, never `DONE`.
- **Q006 — Unit-tested but not production-consumed.** It is not implementation-complete until the production path consumes it and integration tests prove the behavior.
- **Q007 — Acceptance authority.** Ticket DoD plus final system gates define acceptance; a file named `tests_final_acceptance.py` has no special authority.
- **Q008 — Historical TK closures.** TK-01..TK-27 closure records are historical evidence only and cannot override the newer T/ER requirements.
- **Q009 — Graph-V2 gating.** Keep Graph-V2 benchmark-gated. It may remain production-off if it does not show justified value, without blocking the non-Graph correctness core.
- **Q010 — Project complete.** Completion requires implementation, real behavioral tests, required benchmarks, release-gate evidence, and rollout readiness for production-scoped items.

## B. Migration and compatibility

- **Q011 — Migration style.** Use incremental migration; no big-bang rewrite of `server.py`, the API, or the data pipeline.
- **Q012 — API compatibility.** Preserve endpoint URLs, SSE event contract, existing required fields, and HTTP semantics wherever possible; additive fields and versioned optional fields are allowed.
- **Q013 — API versioning.** Keep current endpoints and add schema-version fields. Introduce `/v2` only for an incompatible contract that cannot be represented additively.
- **Q014 — Old frontend compatibility.** Backend must keep old clients functional, but new correctness states cannot be hidden. Old clients receive safe fallback text; supported clients render the new states explicitly.
- **Q015 — Legacy pipeline lifetime.** Retain a named `legacy_hybrid` profile through canary and at least two stable production releases after full activation; after that it may be removed by a separate deprecation ticket.
- **Q016 — T037 versus deleted legacy path.** Restore a real profile-level legacy path during remediation; git-revert alone is insufficient for the dual-run/readiness requirement.
- **Q017 — Legacy removal gate.** Removal requires two stable releases, zero active rollback use, documented parity/known deltas, and explicit deprecation approval.
- **Q018 — Fallback granularity.** Retrieval-route degradation may be per request; full-pipeline fallback is allowed only when it preserves correctness semantics. Do not silently replace a stronger verified path with a weaker unverified one.
- **Q019 — Critical-check fallback.** A correctness-critical failure may never fall back to an answer path that skips the failed check and still returns a normal trusted answer.
- **Q020 — Rollout discipline.** Normal behavior changes go through shadow/canary. Emergency correctness/security fixes may use an expedited path, but still require targeted regression and at least a small canary before 100% rollout.

## C. Record identity and evidence metadata

- **Q021 — Stable record identity.** Introduce an opaque immutable `record_id` (UUIDv7/ULID-class identifier assigned at ingest). It is distinct from content hashes and array positions.
- **Q022 — Content hash role.** Content hashes identify immutable content versions, not logical records.
- **Q023 — Same URL, changed content.** Keep the same `record_id`; create a new immutable `source_snapshot_id`/content version.
- **Q024 — Same body at different URLs.** Keep distinct records/sources and cluster them under provenance; do not collapse source identity merely because content matches.
- **Q025 — Legacy `idx`.** Retain `idx` only as a snapshot-local storage position for compatibility; it is never a durable identity.
- **Q026 — Long-term APIs.** New APIs, Trace, Ledger, citations, and index metadata use stable IDs. `idx` may appear only as an explicitly legacy/debug field.
- **Q027 — Migration mapping.** Build a versioned `legacy_idx -> record_id` mapping per dataset snapshot and keep it for historical replay.
- **Q028 — Deleted records.** Stable IDs are never reused. Deletion creates a tombstone with provenance and last-valid snapshot metadata.
- **Q029 — Metadata-only change.** A metadata-only change does not create a new source-content snapshot; it creates a new metadata artifact/version in a later release manifest.
- **Q030 — Normalization-only change.** If raw source content is unchanged, keep the source snapshot; version the extraction/normalization transform and locator map separately.
- **Q031 — Metadata version versus retrieval indexes.** Rebuild only indexes whose indexed features changed. Release manifests must prove which metadata version each artifact consumed.
- **Q032 — Incremental enrichment.** Dirty detection uses stable `record_id` plus source-content hash plus metadata-input hash; unchanged records are not re-enriched.
- **Q033 — Enrichment failure.** An indexable record missing required evidence metadata blocks publication of the new release. It may be quarantined only by an explicit non-indexable status.
- **Q034 — Required metadata.** At minimum: stable record/source identity, source type/role, provenance key or unknown marker, temporal fields/status, content-risk flags, data-quality flags, metadata version, and source snapshot linkage.
- **Q035 — `source_org_id`.** Use a stable entity ID when resolved; otherwise store `null` plus the original source label. Do not fake an entity ID from a name string.
- **Q036 — Classifier upgrades.** Upgrade by incremental backfill keyed to classifier/schema version; material changes require a new metadata release.
- **Q037 — Serving during backfill.** Continue serving the current complete release. The partially backfilled release remains unpublished.
- **Q038 — Missing metadata at runtime.** Missing required metadata makes the candidate evidence-ineligible for claims that depend on that metadata. It may remain retrieval-only with a trace warning.

## D. Synthetic summaries

- **Q039 — Summary label.** Every model-generated summary is explicitly `synthetic_summary=true` with generator/model/version metadata.
- **Q040 — Faithful summary status.** Even a faithful summary is not primary evidence; fidelity is a property of the summary, not a replacement for source grounding.
- **Q041 — Vector primary index.** Remove generated summary text from primary evidence embeddings. If useful, place it in a separate auxiliary hint representation.
- **Q042 — BM25 primary index.** Do not index generated summary text in primary evidence BM25.
- **Q043 — Entity assertions.** Synthetic summary alone cannot create a canonical entity assertion.
- **Q044 — Graph relations.** Synthetic summary alone cannot create a production semantic relation.
- **Q045 — Numeric facts.** Synthetic summary alone cannot create a production numeric fact.
- **Q046 — Query expansion.** Synthetic summary may drive query expansion only in an auxiliary hint path clearly marked synthetic.
- **Q047 — Hint influence.** Hints may add bounded candidates/queries but cannot by themselves increase evidence sufficiency or claim support.
- **Q048 — Hint promotion.** A hinted record becomes normal evidence only after the claimed fact is found and grounded in an eligible source snapshot.
- **Q049 — Summary says fact but no source span.** Treat it as unsupported for factual answering.
- **Q050 — Summary-only historical records.** They are retrieval/admin hints only and cannot support ordinary factual claims.
- **Q051 — Attributed claim from summary-only record.** Do not use the synthetic summary as proof that the original source said X. At most state that the database contains a synthetic summary, which is not part of normal answer evidence.
- **Q052 — UI display.** UI may show summaries as clearly labeled navigation/overview text, never as quoted evidence.
- **Q053 — Existing contaminated indexes.** Rebuild all primary indexes that currently include synthetic text before marking T049 complete.
- **Q054 — Isolation proof.** Use both schema/content inspection and adversarial sentinel tests whose fabricated fact exists only in a synthetic summary.

## E. SourceSnapshot and EvidenceLocator

- **Q055 — Snapshot storage.** For citation-eligible evidence, store an immutable canonical source-text snapshot or a content-addressed object reference under system control.
- **Q056 — When full text cannot be retained.** Such material may remain retrieval-only unless an immutable legal-to-store excerpt/structured source representation can support exact evidence. Hash+URL alone is insufficient for replayable citation.
- **Q057 — Snapshot identity.** Use opaque `source_snapshot_id` plus full SHA-256 content hash; do not overload one field for both purposes.
- **Q058 — Hash input.** Hash the exact stored canonical source bytes/text representation and record the encoding/extraction version.
- **Q059 — HTML.** Preserve raw source object when permitted and always preserve canonical extracted text used for evidence. The canonical text is what locators target.
- **Q060 — Offset basis.** Canonical evidence offsets target immutable canonical extracted text; a separate mapping links it to raw source coordinates.
- **Q061 — Offset unit.** Backend canonical offsets use Unicode code-point indexes and include the exact span text. Byte offsets may be stored additionally for binary formats.
- **Q062 — Frontend offsets.** API also provides UI-safe UTF-16 offsets or precomputed highlight ranges so browser indexing cannot corrupt the span.
- **Q063 — Normalization mapping.** Store a reversible segment map from normalized text ranges to canonical-text ranges, versioned with the normalizer.
- **Q064 — One-to-many normalization.** Mapping uses range segments rather than a single arithmetic delta and can represent expansion/contraction.
- **Q065 — Unmappable normalized hit.** If it cannot map to an exact canonical source range, it is not valid evidence.
- **Q066 — Table locator.** Use table identifier + row key/index + column key/index + cell hash + optional canonical text range.
- **Q067 — Table reorder.** Prefer stable row/column keys and cell hashes; if they no longer resolve in the pinned snapshot, the locator is invalid rather than guessed.
- **Q068 — PDF/figure support.** PDF evidence includes page number and, where relevant, bounding box plus extracted canonical text range.
- **Q069 — Figure captions.** Include page/object identifier and caption text hash so later extraction changes do not silently retarget the locator.
- **Q070 — Structured facts.** Store source field/path plus the exact source value and snapshot link; derived values also record transformation provenance.
- **Q071 — Repeated identical evidence.** A citation remains pinned to the exact snapshot used for the answer even if a later snapshot contains identical text.
- **Q072 — Changed live URL.** UI may open the live URL, but the citation audit view must show the pinned snapshot and warn when current content differs.

## F. Global release manifest

- **Q073 — Atomic activation.** Store immutable manifests/artifact directories and atomically replace a small `current` pointer file using same-filesystem rename.
- **Q074 — `current` content.** `current` contains only manifest identity/version metadata, not a mutable duplicate of the full manifest.
- **Q075 — Manifest contents.** Profile-declared artifacts include dataset/source snapshots, stable-ID map, evidence metadata, entity snapshot, provenance, vector/BM25/chunk/graph/numeric indexes, prompts, schemas, config, and model metadata.
- **Q076 — Optional artifacts.** A profile may explicitly declare an artifact absent. An undeclared missing required artifact fails release validation.
- **Q077 — Graph-off profile.** Yes, a named profile with Graph disabled may publish without a Graph-V2 artifact if the manifest explicitly records that state.
- **Q078 — Artifact hashes.** Use full SHA-256 for integrity. Short hashes may be display-only.
- **Q079 — Manifest schema.** Version with `manifest_schema_version`; validators must support explicit compatible versions only.
- **Q080 — Startup validation.** Validate existence, hash, schema, dataset version, stable-ID mapping, identity snapshot, embedding dimensions/model, and declared cross-artifact compatibility before activation.
- **Q081 — Required mismatch.** Reject activation of a mismatched release.
- **Q082 — Automatic startup rollback.** Do not silently auto-roll back a cold start. Fail closed and surface the invalid current release; rollback is an explicit operational action.
- **Q083 — Why no silent fallback.** Silent fallback can hide a broken deploy and make observability lie about the running release; therefore explicit rollback is required.
- **Q084 — Retention.** Keep at least the current plus two previous production manifests and any manifest still referenced by retained replay traces.
- **Q085 — Rollback mechanics.** Rollback atomically switches the manifest pointer; processes then perform validated hot reload or restart according to the artifact type.
- **Q086 — Publish locking.** Use a single release-publish lock/transaction; concurrent builders may build but only one validated manifest may advance `current` at a time.
- **Q087 — Crash garbage.** Unreferenced build directories are marked incomplete and removed by a GC job after a safe retention period.
- **Q088 — Post-publish tamper.** Health/audit jobs periodically verify artifact hashes; mismatch marks the instance unhealthy and blocks new activation.
- **Q089 — Trace release identity.** Every request pins and records one manifest ID at request start.
- **Q090 — Cross-release request.** A request never switches artifacts mid-flight; hot reload affects only newly admitted requests.

## G. Verifier and answer-state semantics

- **Q091 — Initial verification state.** Initialize as `NOT_RUN`, never `PASSED`.
- **Q092 — Claim classification failure.** The auxiliary classifier may fail without immediate user failure only if canonical atomic-claim extraction later succeeds. If the canonical claim set cannot be established for a factual answer, final status is `UNVERIFIED`.
- **Q093 — Claim mapping failure.** A factual answer with mapping failure cannot be `SUPPORTED`; attempt repair, else `UNVERIFIED`.
- **Q094 — Evidence Grader failure.** Do not assume sufficiency. For a factual answer that requires grading, return `UNVERIFIED` or an explicit service-quality failure after bounded fallback.
- **Q095 — Grounding failure.** A semantic per-citation miss removes that citation and downgrades/repairs affected claims; a technical grounding subsystem failure yields `UNVERIFIED` for answers depending on it.
- **Q096 — Verifier technical errors.** Timeout, malformed response, empty response, 429/5xx, parser error, and unexpected exception all map to `UNVERIFIED`, never PASS.
- **Q097 — Verifier finds factual error.** Enter the bounded repair state machine first. If unrepaired, derive `PARTIALLY_SUPPORTED` or `UNSUPPORTED` from remaining claim support.
- **Q098 — Technical versus semantic failure.** Keep them distinct: technical inability => `UNVERIFIED`; verified semantic lack of support => `PARTIALLY_SUPPORTED`/`UNSUPPORTED`.
- **Q099 — No factual claims.** Non-factual operational/abstention responses may use `verification_status=NOT_APPLICABLE` and do not need the epistemic verifier.
- **Q100 — Knowledge-boundary text.** A deterministic boundary/refusal response does not need a factual verifier unless it itself introduces factual claims.
- **Q101 — “No evidence found.”** Use `verification_status=NOT_APPLICABLE`; answer status is `UNSUPPORTED` when retrieval/search conclusively lacks support.
- **Q102 — SUPPORTED requirements.** `SUPPORTED` requires sufficient graded evidence, all critical requirements covered, all major factual claims supported, valid exact citations where citations are applicable, and final verification passed.
- **Q103 — Minor unsupported claims.** A final `SUPPORTED` answer may not contain unsupported factual claims. Minor unsupported wording must be removed or rewritten as uncertainty before finalization.
- **Q104 — Minor claim repair.** Prefer deletion or explicit weakening plus re-verification; if it remains a factual unsupported claim, aggregate status becomes `PARTIALLY_SUPPORTED`.
- **Q105 — Missing critical requirement.** Always prohibits `SUPPORTED`.
- **Q106 — High-severity unresolved conflict.** Always prohibits deterministic `SUPPORTED` wording for the conflicted conclusion.
- **Q107 — Only self-report for “is it really true?”.** Return `PARTIALLY_SUPPORTED` if the self-report itself is verifiable and useful while clearly saying independent validation is missing; `UNSUPPORTED` if the user’s core question cannot be meaningfully answered without independent evidence.
- **Q108 — Status precedence.** Status is derived from claim/evidence state, not a simple global priority. Technical failure only forces `UNVERIFIED` when it prevents validating an answer that would otherwise be presented.
- **Q109 — No evidence plus verifier outage.** If no factual answer is generated because evidence is absent, return `UNSUPPORTED`; verifier availability is irrelevant to a deterministic abstention.
- **Q110 — State ownership.** The deterministic answer state machine owns final status. The Generator can suggest text but cannot set support state.

## H. Bounded answer repair

- **Q111 — Repair limit.** Allow at most two post-generation repair cycles; retrieval itself remains separately bounded by the research-loop limit.
- **Q112 — Repair order.** Prefer deterministic removal/weakening for noncritical claims, targeted retrieval for missing critical claims, regeneration from the updated Evidence Package, then complete re-verification.
- **Q113 — Critical unsupported claim.** First attempt targeted retrieval. Deletion is allowed only when the final answer explicitly becomes partial and still addresses the remaining user intent.
- **Q114 — Core claim cannot be deleted away.** If the unsupported claim is the essence of the question, deleting it cannot produce a `SUPPORTED` answer; return partial/unsupported.
- **Q115 — Grounding miss.** First retry deterministic/exact location with alternate eligible spans; then remap the claim; regenerate only if necessary.
- **Q116 — Verifier rewriting.** The verifier does not author final text. It returns structured issues/repair instructions; an isolated Generator/repairer produces any revised answer.
- **Q117 — Verifier role.** Direct verifier-authored final answers are forbidden.
- **Q118 — Revalidation after repair.** Any factual text change requires claim extraction/mapping, grounding, entailment, state evaluation, and final verification again for affected claims; aggregate status is recomputed.
- **Q119 — Local verification.** Local re-verification is allowed only if immutable unchanged claim/evidence states are reused by hash; final aggregate verification still checks the complete answer state.
- **Q120 — New citations.** New evidence must pass source/provenance/temporal eligibility and grounding before use.
- **Q121 — Repair exhaustion.** If reliable supported content remains, return `PARTIALLY_SUPPORTED`; if the core answer remains unsupported, return `UNSUPPORTED`.
- **Q122 — Repair technical failure.** If it prevents validation of the answer, return `UNVERIFIED` rather than pretending repair succeeded.
- **Q123 — Unverified draft visibility.** Production default does not expose the full factual draft before verification.
- **Q124 — Streaming model.** Generate and verify server-side before streaming final answer tokens. SSE may emit progress events while processing; `replace` remains compatibility-only, not the normal correctness path.

## I. Claims, citations, grounding, and entailment

- **Q125 — AI summary as citation.** Prohibited as final factual evidence.
- **Q126 — Fuzzy locate.** Fuzzy search is only a locator technique. A final accepted citation must resolve to an exact substring/range in the pinned canonical source snapshot.
- **Q127 — Final grounding vocabulary.** Final user-visible validity is `EXACT`/`INVALID`; internal trace may record that fuzzy methods located the exact range.
- **Q128 — Normalized matches.** They must map back to an exact canonical source span before validity is granted.
- **Q129 — Query-relevant sentence.** Relevance alone is insufficient; the span must also pass claim-evidence support/entailment when used as support.
- **Q130 — Support condition.** For a citation to count toward a factual claim, exact grounding and support/entailment must both succeed, plus any applicable numeric/time checks.
- **Q131 — Citation to claims.** A citation may support multiple claims; relations are explicit per claim.
- **Q132 — Multiple spans.** A claim may cite multiple non-contiguous spans represented as multiple locators.
- **Q133 — Cross-record claim.** Represent it as one claim with multiple support relations/evidence groups; no synthetic merged quote is created.
- **Q134 — Relation types.** API may expose direct support, premise support, attribution, contradiction, and background, but UI must not imply that background/contradiction supports the claim.
- **Q135 — Background sources.** They may appear in a separate related/background section, not in the claim’s support count.
- **Q136 — Contradictions.** Contradicting evidence must be visibly labeled as such when shown.
- **Q137 — Attributed self-report.** A vendor source can support “Vendor X states Y” but not automatically the unqualified factual proposition Y.
- **Q138 — Numeric conditions.** Missing/invalid unit, scope, denominator, or temporal condition blocks `SUPPORTED` for the numeric claim even if the text span is exact.
- **Q139 — Invalid citation handling.** Remove it from final citations. Preserve failure details only in Trace/review artifacts.
- **Q140 — Display invariant.** `invalid citation displayed = 0` is a hard release invariant.
- **Q141 — Title-only support.** A title may be evidence only if the immutable source snapshot defines the title itself as the exact source text supporting an attributed/title claim; otherwise it cannot substitute for missing body evidence.
- **Q142 — Citation link behavior.** Audit defaults to the pinned snapshot; live URL is an additional external link with drift warning if content changed.

## J. Retrieval candidate pool and reranking

- **Q143 — Default per-route TopK.** Start with Vector=50, BM25=50, Graph=40, Chunk routes=50 where enabled; all are profile-configurable and benchmark-tuned.
- **Q144 — Different route TopK.** Yes; routes may have different calibrated TopK.
- **Q145 — Candidate pool cap.** Default deduplicated high-recall pool cap is 180 for RESEARCH/DEEP and 80 for FAST, subject to benchmark.
- **Q146 — Coarse prune.** Above the cap, use requirement-aware reserve plus per-route minimum quotas, then RRF/route features for the remaining slots. Never pure global TopN without quotas.
- **Q147 — Route outliers.** Preserve a configured minimum quota per active route unless that route returns fewer candidates.
- **Q148 — Candidate dedup.** Use stable `record_id`; chunk hits aggregate under their parent record while retaining hit locators.
- **Q149 — Multiple chunks.** Allow multiple high-value chunks per parent into rerank features, but only one parent record occupies a record-level candidate slot.
- **Q150 — Chunk evidence.** Preserve multiple exact chunk/locator hits for later evidence selection and grounding.
- **Q151 — Fusion default.** Production chooses among large-pool RRF and union/dedupe through a locked benchmark; the spec does not assume one is universally superior.
- **Q152 — RRF semantics.** RRF score is a fusion/ranking feature, not final relevance or trust.
- **Q153 — Reranker content.** Give title, minimal metadata, best relevant source-grounded chunks/excerpts, and route features. Synthetic summary may be an explicitly marked hint but never the sole content.
- **Q154 — Graph reranker input.** Include matched relation paths and grounded edge evidence as optional route features.
- **Q155 — GLM batch size.** Configure and calibrate; initial ceiling 20 candidates per GLM listwise batch, never N independent calls.
- **Q156 — Cross-batch calibration.** Prefer a local/cross-encoder first stage plus a small GLM listwise rerank; if multiple GLM batches are used, calibrate with anchors/overlap and a held-out stability benchmark.
- **Q157 — Reranker timeout.** Fall back to the best available deterministic/local ranking, set a degraded trace flag, and continue only if downstream evidence checks still pass.
- **Q158 — Reranker user warning.** Not automatically user-visible if correctness checks still pass; it is trace/telemetry-visible. If degraded ranking causes evidence insufficiency, status reflects that.
- **Q159 — FAST rerank.** FAST still performs a real content-aware rerank, preferably deterministic/local so simple questions do not pay an extra control-model call.
- **Q160 — FAST basic evidence check.** Use deterministic eligibility/sufficiency rules plus claim/citation verification; full semantic Grader is optional only when the simple-case rules are decisive.
- **Q161 — Current FAST behavior.** Reject the current “skip reranker + grader and assume SUFFICIENT” behavior.
- **Q162 — FAST latency policy.** Use heuristic routing, bounded candidate pool, local reranker, deterministic sufficiency checks, and a compact final verification path. Cost/latency may optimize method choice, never skip correctness-critical checks.

## K. Evidence selection, requirement fusion, and context assembly

- **Q163 — Generator input.** Generator consumes only the canonical Evidence Package built from selected evidence; raw `all_results` are forbidden as final context.
- **Q164 — `all_results` role.** Keep for trace, dedup, gap analysis, and research memory only.
- **Q165 — Empty selector output.** Enter gap analysis or abstain; never dump raw retrieval results into the Generator as fallback.
- **Q166 — Requirement-aware fusion placement.** Preserve requirement/route reserves before rerank, rerank the retained pool, then perform requirement-aware evidence selection after rerank.
- **Q167 — ReservePool semantics.** Reserve candidates that protect critical requirements, entity/dimension coverage, and route outliers that global ranking might discard.
- **Q168 — Comparison coverage.** Enforce configurable minimum candidate/evidence opportunities for each required object × dimension before declaring sufficiency.
- **Q169 — Relevance versus diversity.** Minimum relevance/grounding eligibility is non-negotiable; among eligible evidence, coverage and source diversity can outweigh marginal relevance differences.
- **Q170 — Reposts.** Count a provenance cluster once for independence; one representative may be displayed when useful, with lineage preserved.
- **Q171 — Primary + independent.** For claims that need external validation, retain both the primary/self-report and at least one independent group when available.
- **Q172 — Context token budget.** Allocate by requirements: critical requirements first, then conflicts, then supporting diversity, then noncritical context.
- **Q173 — Critical quota.** Critical requirements get explicit minimum budget reservations.
- **Q174 — Conflicts under token pressure.** Unresolved high-severity contradictory evidence cannot be silently dropped; compress elsewhere or downgrade/abstain.
- **Q175 — Comparison balance.** Each required comparison object gets a minimum context/evidence allocation where evidence exists.
- **Q176 — Redundancy handling.** Selector removes provenance/content redundancy; Context Builder may further compress presentation without changing support counts.
- **Q177 — Rerank scores to Generator.** Do not expose raw scores unless needed for ordering metadata; the Generator must not translate arbitrary ranking numbers into factual confidence.
- **Q178 — Evidence metadata to Generator.** Provide source role, provenance group, temporal scope, and conflict labels as structured metadata, not as pseudo-probability truth scores.
- **Q179 — Canonical schema.** Evidence Package is the sole canonical generation-context schema.
- **Q180 — Old `build_context()`.** Remove it from the new production path; it may survive temporarily inside the isolated legacy profile only.

## L. Multi-document evidence processing

- **Q181 — Trigger.** Router flags it for cross-entity comparison, trend, conflict resolution, distributed evidence, or source-independence needs; Planner/Orchestrator confirms against requirements before launching workers.
- **Q182 — Second confirmation.** Yes; Router is a proposal, not the sole trigger authority.
- **Q183 — Max documents.** Default 12 selected parent documents per multi-document stage; configurable and benchmarked.
- **Q184 — Worker concurrency.** Default 4 concurrent workers per request with global backpressure controls.
- **Q185 — Worker granularity.** One worker invocation may process one document against multiple assigned requirements and emits per-requirement packets; do not multiply calls unnecessarily.
- **Q186 — Worker model.** Default to the same quality tier as the evidence Grader unless benchmark proves a cheaper model preserves extraction/grounding quality.
- **Q187 — Local claim without exact span.** It does not enter the Ledger as supporting evidence.
- **Q188 — Relevant but no evidence.** Record this as search/work evidence (`relevant=true, evidence_found=false`) for gap/stopping logic, not as support.
- **Q189 — Summary for navigation.** Worker may see an explicitly marked synthetic summary only as navigation/query aid; every accepted local claim must ground in eligible source text.
- **Q190 — Document metadata.** Worker may receive canonical entity/source/provenance/temporal metadata for that document.
- **Q191 — Cross-document contamination.** Worker does not receive other documents’ conclusions or the Generator draft.
- **Q192 — Single worker failure.** Continue other workers, mark the failed document/requirement degraded, and let the Grader determine whether coverage remains sufficient.
- **Q193 — Unique critical document failure.** If the core answer depends on processing it and the failure is technical, do not claim support; after bounded alternatives, return `UNVERIFIED` for attempted factual answering or partial/unsupported if evidence insufficiency can be established independently.
- **Q194 — In-request cache.** Cache by document snapshot + requirement set + worker schema/prompt version during the request.
- **Q195 — Cross-request cache.** Allowed only as a versioned immutable artifact keyed by snapshot, requirement fingerprint, model, prompt, and schema; stale packets are never reused across incompatible manifests.
- **Q196 — Dedup.** First dedup by provenance/source lineage, then by semantically equivalent atomic claim + scope.
- **Q197 — Conflict layers.** Worker reports internal inconsistencies; cross-document merge forwards normalized candidate conflicts to the central Conflict Detector.
- **Q198 — Feature-off fallback.** Fall back to ordinary Research RAG but lower sufficiency when the question intrinsically needs cross-document processing; never pretend full coverage simply because the mode is disabled.

## M. Query integrity and verified conversation context

- **Q199 — Prior factual premise.** Carry only structured claim states that were individually verified/supported with pinned evidence, not arbitrary answer sentences.
- **Q200 — Prior PARTIAL answer.** Individually verified supported claims from a PARTIAL answer may be reused; unsupported/partial claims may not become premises.
- **Q201 — Prior UNVERIFIED answer.** Reuse only claim/evidence units whose own claim-level state is independently verified; answer-level UNVERIFIED prose is never trusted as premise.
- **Q202 — Prior UNSUPPORTED retrieval.** Previous search terms/result IDs may inform query expansion and dedup, but not factual premises.
- **Q203 — Raw assistant prose.** Treat as untrusted conversational text, never as evidence or a factual premise.
- **Q204 — User repeats prior error.** Treat it as a user assertion to be checked, not as verified context.
- **Q205 — User rejects prior context.** Clear inherited factual premises for the affected scope and rebuild from source evidence.
- **Q206 — User correction.** Mark conflicting prior premise states superseded for the conversation and preserve provenance in Trace.
- **Q207 — Semantic diff fields.** Compare entities/IDs, time ranges, negation, modality, numeric quantities, comparison set, requested dimensions, scope/conditions, and core intent.
- **Q208 — Changed key entity.** If rewrite changes a key entity without explicit user support, reject the rewrite and use original/safer interpretation; escalation to Research occurs when ambiguity remains.
- **Q209 — New entity insertion.** A rewrite cannot add a factual entity not implied by verified conversation context or the current user message.
- **Q210 — Negation drift.** Any lost/added negation causes rewrite rejection or manual ambiguity handling.
- **Q211 — Temporal drift.** Any material temporal change causes rewrite rejection unless explicitly supported by verified context.
- **Q212 — Ambiguity.** Cover multiple plausible interpretations when bounded; otherwise state the assumption/ambiguity in the final answer rather than silently choosing.
- **Q213 — Novelty.** Do not hard-exclude prior authoritative sources needed as baselines.
- **Q214 — Novelty penalty.** Use a soft novelty penalty/quota on already-covered evidence while preserving critical baseline sources.
- **Q215 — History truncation.** Maintain server-side structured verified conversation state separately from raw message-window truncation.
- **Q216 — Conversation state storage.** Persist verified claim/evidence references server-side by conversation ID where available; client history remains untrusted presentation context.

## N. Entity Resolution V2

- **Q217 — Opaque IDs.** Entity IDs are opaque and not derived from canonical names.
- **Q218 — ID format.** Use UUIDv7/ULID-class sortable opaque IDs generated at canonical-entity creation.
- **Q219 — Type changes.** Entity type correction does not change entity ID.
- **Q220 — Rename.** Canonical rename never changes entity ID.
- **Q221 — Alias cardinality.** Alias mapping is many-to-many with provenance and validity metadata.
- **Q222 — Ambiguous exact aliases.** Do not auto-force a unique entity without a stronger deterministic discriminator.
- **Q223 — Strong IDs.** Support typed external identifiers with namespace validation: DOI, LEI where available, domain/official URL, exchange+ticker, authoritative product/model IDs, and other ontology-approved IDs.
- **Q224 — Strong-ID conflict.** Conflicting authoritative IDs block auto-link and enter `BLOCKED`/manual review.
- **Q225 — Formal resolution states.** Canonical decision states are `LINK`, `NEW`, `AMBIGUOUS`, `BLOCKED`.
- **Q226 — LOW_CONFIDENCE.** Keep only as an internal score/diagnostic label, not a terminal decision state.
- **Q227 — NEW entities.** Create provisional entities first unless a deterministic strong-ID/approved rule permits immediate canonical activation.
- **Q228 — Provisional metadata use.** Provisional entities may annotate retrieval metadata but must be clearly marked provisional.
- **Q229 — Provisional graph use.** They cannot participate in high-confidence semantic traversal or support canonical identity claims.
- **Q230 — BLOCKED semantics.** Use for explicit manual block rules, identity conflicts, unsafe/malicious resolution contexts, or policy-forbidden auto-resolution.
- **Q231 — LLM candidate constraint.** LLM adjudication may only choose among provided entity candidates or `NEW/AMBIGUOUS/BLOCKED`; fabricated entity IDs are rejected.
- **Q232 — LLM NEW.** Allowed as a proposal, producing a provisional NEW state, never an unreviewed high-confidence canonical merge.
- **Q233 — Confidence.** Ignore raw LLM self-reported confidence for thresholding; use calibrated resolver features/policies.
- **Q234 — Candidate recall gate.** Set the numeric threshold from the locked ER gold set before production activation; until measured, the gate is “no worse than baseline and target >= 0.98 Recall@10 for high-impact classes,” subject to calibration evidence.
- **Q235 — Type-specific gates.** Report and gate separately for ORG, PERSON, PRODUCT/MODEL, TECHNOLOGY, and OTHER/DOMAIN identifiers because ambiguity differs by class.
- **Q236 — Short acronyms.** Require contextual/type/strong-ID evidence; ambiguous short aliases return multiple candidates or AMBIGUOUS.
- **Q237 — Cross-language identity.** Chinese/English/transliteration aliases remain aliases with provenance; parent/subsidiary or brand/company distinctions remain separate entities linked by relations.
- **Q238 — Product versions.** Distinct materially versioned products/models are distinct entities or versioned instances, linked to a product family when appropriate.
- **Q239 — Company rename/acquisition.** Pure rename keeps the entity; acquisition/merger is modeled as a relation/event unless legal identity truly merges.
- **Q240 — Manual override precedence.** Active, non-conflicting manual overrides take precedence over automated resolver decisions.
- **Q241 — Override expiry.** Expired/review-due overrides do not silently disappear; they become review-required and stop auto-overwriting until resolved according to policy.
- **Q242 — Override audit fields.** Store actor/owner, reason, creation time, valid range, review due, status, and referenced evidence/decision.
- **Q243 — Merge rollback data.** MergeEvent records source/target IDs, pre-merge aliases, mentions, relations, redirects, overrides, snapshot/version, actor/reason, and a reversible migration plan.
- **Q244 — Merge redirects.** Source entity IDs remain tombstoned redirects, never reused.
- **Q245 — Split/unmerge.** Reassign mentions/evidence explicitly from the mutation history; do not blindly restore a stale whole snapshot if later valid changes exist.
- **Q246 — Relation rebuild.** Re-materialize affected relations incrementally from source mention/evidence inputs after merge/split.
- **Q247 — Rollback with later mentions.** Re-resolve post-merge mentions against the restored identity set; do not assign them by old snapshot position.
- **Q248 — Mutation conflicts.** Disallow automatic rollback when later dependent mutations exist; require a planned compensating operation with conflict report.
- **Q249 — Identity store.** Use a transactional repository abstraction; SQLite WAL is acceptable for the current single-service deployment, with schema/constraints compatible with migration to Postgres for multi-node writes.
- **Q250 — Atomic create.** Enforce database uniqueness constraints on normalized strong IDs and candidate identity keys plus transaction/retry logic.
- **Q251 — Identity snapshot.** Every released identity snapshot is an artifact referenced by the global manifest and pinned by requests/Trace.
- **Q252 — Legacy graph migration.** Legacy nodes/edges are seeds/hints only; semantic truth is rebuilt from source-backed mentions/relations.
- **Q253 — High-impact review.** High-degree/high-frequency legacy entities require explicit review or benchmark-backed deterministic resolution before Graph-V2 full activation.
- **Q254 — Admin authentication.** Reuse a strong operator/admin authentication boundary initially, separate from public QA access; all mutations require authenticated identity and audit.
- **Q255 — Dangerous mutation approval.** High-impact merge/split operations require a preview/dry-run and a second explicit confirmation; organizations may configure dual-person approval later.
- **Q256 — Audit log.** Mutation audit is append-only and included in retention/backup policy.
- **Q257 — ER shadow duration.** Require both a minimum sample and time window before activation: at least 1,000 representative resolution events and 7 calendar days unless the environment cannot produce that traffic, in which case an equivalent replay benchmark plus explicit approval is required.
- **Q258 — ER rollback triggers.** Any material regression in high-impact false-link rate, duplicate canonical creation, blocked-rule violation, or graph identity corruption is an immediate rollback; ambiguity-rate thresholds are calibrated from baseline and can trigger staged rollback.

## O. Graph-V2 and relation-aware retrieval

- **Q259 — Node keys.** Production Graph-V2 uses stable entity IDs, never display names, as graph identity.
- **Q260 — Legacy substring scan.** Keep only during migration/shadow and rollback windows; remove after Graph-V2 deprecation gate.
- **Q261 — Partial activation eligibility.** Enable V2 only when query entities resolve above calibrated high-confidence thresholds, required predicates are supported, and the referenced identity/graph snapshots are healthy.
- **Q262 — Ambiguous entity handling.** Use bounded candidate expansion with reduced graph weight when safe; otherwise skip Graph while Vector/BM25 continue.
- **Q263 — Graph failure semantics.** Non-relation-critical questions may continue without Graph; relation-critical questions must reflect the missing graph capability in sufficiency/status.
- **Q264 — Relation-critical detection.** Router/Planner marks a requirement relation-critical when its answer depends on a typed relation, direction, graph path, or identity composition that text retrieval alone is not configured to establish.
- **Q265 — Ontology changes.** Predicate additions/removals are versioned schema changes and require manifest/benchmark updates.
- **Q266 — Unknown relations.** Unknown/unvalidated predicates can aid discovery only and cannot directly support factual relation claims.
- **Q267 — Co-occurrence.** `RELATED_CO_OCCURRENCE` is weak discovery evidence, never direct proof of a semantic relation.
- **Q268 — Edge grounding.** Every production semantic edge carries stable subject/object IDs, predicate/direction/modality/time, source snapshot, and exact locator(s).
- **Q269 — Predicate validation.** Deterministic ontology/schema checks run first; semantic predicate fit is verified by a constrained relation validator using the grounded source span.
- **Q270 — Direction validation.** Direction is part of the typed extraction and is independently checked against the evidence; invalid direction makes the edge ineligible.
- **Q271 — Non-asserted relations.** Negated/planned/possible relations are stored with polarity/modality and may aid retrieval, but cannot be treated as asserted facts.
- **Q272 — Two-hop policy.** Two-hop traversal activates only for explicit multi-hop/relation-composition needs and stays bounded.
- **Q273 — Allowed composition.** Only ontology-declared composition patterns may produce inference-eligible paths; all others are discovery-only.
- **Q274 — Unauthorized composition.** Discovery/query expansion only, never factual conclusion.
- **Q275 — Path scoring.** Calibrate weights/version on a held-out relation-retrieval benchmark and record score breakdown in Trace.
- **Q276 — No Graph gain.** Keep Graph-V2 production-off and continue shadow/research; the wider Agentic correctness project may still be complete because Graph-V2 is explicitly benchmark-gated.
- **Q277 — Full activation threshold.** Require no regression on core QA metrics and a statistically/operationally meaningful gain on relation-specific accuracy/usefulness, not merely “it runs.”
- **Q278 — Legacy Graph rollback window.** Retain the legacy graph profile for two stable releases after full V2 activation, matching the general legacy deprecation policy.

## P. Runtime degradation, deadlines, cancellation

- **Q279 — Vector failure.** Continue with remaining routes if sufficiency can still be established; trace the degraded route.
- **Q280 — BM25 failure.** Same policy as Vector.
- **Q281 — Noncritical Graph failure.** It may degrade without user warning if remaining evidence passes all correctness gates; telemetry must record it.
- **Q282 — Relation-critical Graph failure.** Do not return a normal SUPPORTED relation answer. Attempt text-based alternatives if policy allows; otherwise partial/unsupported/unverified depending on whether the limitation is evidence absence or technical verification inability.
- **Q283 — Reranker failure.** Fall back to an approved deterministic/local ranking, never to an empty set; downstream evidence gates still decide sufficiency.
- **Q284 — Selector failure.** Use a small deterministic safe selector fallback only if it enforces minimum eligibility/provenance/coverage rules. If that fallback fails or is inapplicable, do not generate a normal factual answer.
- **Q285 — Worker failure.** Isolate the failure to affected document/requirements and recompute sufficiency.
- **Q286 — Grader failure.** For questions requiring semantic grading, technical failure prevents `SUPPORTED`; return UNVERIFIED or explicit service-quality failure after bounded retry/fallback.
- **Q287 — Entailment failure.** A technical entailment subsystem failure prevents affected factual claims from being supported; if core answer depends on them, final status is UNVERIFIED.
- **Q288 — Grounding fallback.** `use_query_snippet`, record beginning, or synthetic summary is forbidden as a “valid citation” fallback.
- **Q289 — Final verifier failure.** Technical failure => UNVERIFIED for factual answering.
- **Q290 — Generator failure.** Return an SSE/error outcome; do not fabricate or reuse an old answer as if current generation succeeded.
- **Q291 — Retry count.** Default one retry for retryable remote-model/network failures per correctness-critical LLM stage; deterministic parsers may retry locally without external calls. Further retries require explicit stage policy.
- **Q292 — Retryable errors.** Retry timeout/transient transport/429/5xx with bounded backoff; do not retry deterministic schema/policy rejection unless a repair prompt is explicitly defined.
- **Q293 — Stage timeouts.** Configure per stage and profile; defaults: rewrite/router 3s each, local retrieval 3s total, rerank 5s local or 8s GLM, worker 12s, grader 8s, generator 30s, verifier 10s. Benchmarks may adjust but changes are versioned.
- **Q294 — Request deadline.** Use a total request deadline in addition to stage deadlines; default 60s FAST, 120s RESEARCH, 180s DEEP unless deployment constraints require lower values.
- **Q295 — Deadline response.** Prefer a fully verified partial answer or deterministic boundary response; never expose an unverified factual draft merely because time ran out.
- **Q296 — Disconnect detection.** Poll/await framework-supported client-disconnect state and propagate cancellation through a request-scoped cancellation token/task group.
- **Q297 — Cancellation coverage.** Cancel pending LLM calls, retrieval tasks, document workers, and repair work where the underlying client supports cancellation.
- **Q298 — Non-cancellable calls.** Mark abandoned calls, detach results from request state, bound executor/socket resources, and expose telemetry so they cannot accumulate silently.
- **Q299 — Resource release.** Semaphores, temp files, handles, and request caches are released in `finally`/task-group cleanup on success, error, timeout, and disconnect.
- **Q300 — Queue admission status.** Use 429 + `Retry-After` for rate/concurrency/queue admission rejection; use 503 when a required backend capability is unavailable independently of client quota.
- **Q301 — Backpressure signals.** Gate on active requests plus bounded queues and critical resource saturation; record CPU/memory/LLM-concurrency metrics but do not use a single unbounded queue.
- **Q302 — State isolation.** ResearchState, Ledger, selected evidence, repair state, and conversation-local mutable data are request/conversation scoped; no shared mutable globals.
- **Q303 — Hot reload.** Each admitted request holds references to its pinned manifest artifacts; reload swaps only the factory/pointer for new requests.
- **Q304 — Shutdown.** Stop admitting new work, allow a bounded drain window, then cancel remaining request task groups and flush audit/trace buffers.

## Q. Real acceptance, evaluation, and CI

- **Q305 — Import-only tests.** Importability is a smoke check, never sufficient DoD proof for behavior tickets.
- **Q306 — Hardcoded truth.** Ban `or True`, unconditional `True`, and equivalent no-op acceptance assertions.
- **Q307 — Ticket-to-test map.** Every Ticket/ER DoD maps to one or more named behavioral tests/benchmarks in a machine-readable acceptance matrix.
- **Q308 — Test levels.** Pure schema/utilities may use unit tests; production behavior tickets require integration tests, and cross-stage tickets require E2E tests in addition to units.
- **Q309 — T037 E2E.** Must run the actual orchestrator/server path; hand-inserting Trace stages does not count.
- **Q310 — SSE E2E.** Yes, include real `/api/chat/stream` tests for success, partial, unsupported, unverified, cancellation, and malformed-dependency cases.
- **Q311 — Mini indexes.** E2E uses committed reproducible mini Vector/BM25/Graph/chunk artifacts or rebuilds them from tracked fixtures.
- **Q312 — LLM testing.** Merge CI uses deterministic fake/recorded model adapters; nightly/release validation includes live configured GLM probes on a bounded locked set.
- **Q313 — Mock policy.** Mock external nondeterminism and network failures; do not mock away the orchestration/state transitions being tested.
- **Q314 — Failure injection.** Correctness-critical tests inject timeout, malformed JSON, empty result, exceptions, 429, 5xx, and grounding/entailment failure paths.
- **Q315 — Synthetic adversary.** Yes, use sentinel fabricated facts present only in synthetic summaries and prove they cannot produce supported evidence/citations.
- **Q316 — Outlier benchmark.** Include cases where old RRF Top25 drops a single-route relevant candidate and the new pool+rereank path recovers it.
- **Q317 — Generator isolation test.** Assert that unselected `all_results` text cannot appear in the canonical Generator input.
- **Q318 — Conversation contamination test.** Assert prior UNVERIFIED/unsupported assistant claims cannot become later factual premises.
- **Q319 — Disconnect test.** Simulate real SSE disconnect and assert worker/model tasks are cancelled or marked abandoned and request resources are released.
- **Q320 — Concurrency stress.** Minimum 50 concurrent QA requests in CI stress mode, plus a higher nightly target based on production capacity; correctness checks include zero cross-request state leakage.
- **Q321 — Entity atomic-create stress.** Minimum 32 concurrent workers attempting the same candidate identity, with exactly one canonical creation.
- **Q322 — Holdout integrity.** Evaluation holdouts are locked and separate from prompt/rule/threshold tuning data.
- **Q323 — Reranker splits.** Calibration/tuning and final evaluation use separate splits.
- **Q324 — Graph splits.** Path-weight calibration and final relation evaluation use separate splits.
- **Q325 — Benchmark provenance.** Every report records git SHA, spec hash/version, release/manifest ID, dataset snapshot, identity snapshot, model, prompt, schema, and config versions.
- **Q326 — Skips.** Reports explicitly count pass/fail/skip/xfail; release-required tests may not be skipped unless the release is blocked.
- **Q327 — Final green.** Final Acceptance is green only when every required suite actually executes and all hard gates pass.
- **Q328 — Flaky live GLM tests.** Allow one bounded rerun for transport/transient failure; semantic regressions are not retried away. Track flake rate separately.

## R. Merge gates and branch protection

- **Q329 — Branch protection.** Enable main-branch protection with required status checks and no direct unreviewed production-code pushes except audited emergency policy.
- **Q330 — Required merge checks.** At minimum: canonical spec lint, unit suites, core integration/E2E, acceptance-matrix coverage, synthetic-isolation tests, security/safety lint, and fast regression baseline.
- **Q331 — Nightly versus merge gate.** Expensive nightly benchmarks need not block every merge, but manifest publication/full activation must require a fresh passing release benchmark artifact.
- **Q332 — CI tiers.** Push/PR: deterministic tests, mini-index E2E, spec/schema lint, critical regression. Nightly: full replay, broader concurrency, live-model probes, relation/ER/reranker benchmarks. Release: all hard gates plus rollout eligibility report.
- **Q333 — Live GLM in merge CI.** No mandatory live network dependence for normal merges; use deterministic adapters. Live GLM probes run nightly/release.
- **Q334 — Spec-change checks.** Any spec/manifest/schema/profile change triggers dependency/schema validator and acceptance-matrix completeness checks.
- **Q335 — Ticket status generation.** Generate status from machine-readable DoD evidence; README checkboxes are views, not the source of truth.
- **Q336 — CI artifact retention.** Keep release-gate artifacts at least 180 days and any artifact referenced by a retained production manifest/incident longer as required by policy.
- **Q337 — Release evidence.** Release gate consumes machine-readable benchmark/acceptance output; manual approval may add a decision but cannot replace failed hard metrics.
- **Q338 — Failed benchmark.** Blocks publication/full activation for the affected profile unless an explicit, documented gate waiver is added to the spec decision log; hard correctness invariants are never waivable.

## S. Shadow, canary, staged rollout

- **Q339 — Shadow sampling.** Default 10% of eligible production requests for the full new-path shadow, configurable downward for cost while maintaining minimum sample targets.
- **Q340 — Assignment.** Use deterministic conversation/client hashing for sticky shadow/canary assignment where privacy rules permit; otherwise request hashing with correlation tracking.
- **Q341 — Shadow depth.** Support stage-level shadow for expensive experiments, but release-candidate validation includes full-pipeline shadow through final answer/state on the sampled set.
- **Q342 — Cost ceiling.** Shadow percentage is capped by a configured daily budget, but the system must accumulate the minimum required sample before advancing rollout.
- **Q343 — Canary start.** Begin user-visible canary at 1%.
- **Q344 — Stages.** Default progression: 1% -> 5% -> 25% -> 50% -> 100%.
- **Q345 — Minimum observation.** 1%: >=4h; 5%: >=12h; 25% and 50%: >=24h each, all subject to sample minimums.
- **Q346 — Sample minimums.** 1% >=200 eligible queries, 5% >=500, 25% >=1,000, 50% >=2,000. Low-traffic environments may use equivalent locked replay plus explicit release approval, never zero evidence.
- **Q347 — Canary unit.** Canary is by named pipeline profile. Module-level experiments may shadow independently but do not create arbitrary production flag combinations.
- **Q348 — Production profiles.** Production runs named profiles only; ad-hoc flag mixtures are rejected.
- **Q349 — Graph versus Agentic canary.** They may roll independently through separate named profiles/gates because Graph-V2 is benchmark-gated.
- **Q350 — Rollback classes.** Define zero-tolerance correctness triggers plus baseline-relative quality/latency/error triggers before rollout begins.
- **Q351 — Verifier false-PASS trigger.** Any observed verifier technical failure treated as PASS is immediate rollback/blocker.
- **Q352 — Invalid citation trigger.** Any displayed invalid citation attributable to the new release is an immediate stop/rollback blocker until root cause is understood.
- **Q353 — Unsupported-claim monitoring.** Use sampled automated adjudication plus human review on a labeled audit stream; do not pretend it is exactly measurable from unlabeled production traffic.
- **Q354 — False-abstain monitoring.** Estimate via sampled reviewed queries and replay/golden sets; online proxy metrics are advisory.
- **Q355 — Entity ambiguity.** Compare calibrated ambiguity/false-link rates by class; a statistically meaningful rise above the approved gate pauses/rolls back the ER/Graph profile.
- **Q356 — Graph conflict rate.** Do not treat raw conflict frequency alone as failure; inspect conflict precision, false-conflict rate, and downstream unresolved-conflict accuracy.
- **Q357 — Latency rollback.** Default pause threshold: p95 end-to-end latency >30% worse than approved baseline for a sustained window without compensating quality justification; severe SLO breach may trigger immediate rollback.
- **Q358 — Error/timeout rollback.** Pause when 5xx/timeout rate is >2x baseline and exceeds the absolute SLO threshold for the observation window; correctness hard triggers override these relative rules.
- **Q359 — Automatic versus manual rollback.** Zero-tolerance integrity/cross-request/citation/verifier invariants may auto-disable the affected profile; quality/latency regressions normally pause rollout for human review.
- **Q360 — Auto-rollback scope.** Automatic action is allowed for objective hard invariants (state leakage, invalid citation display, corrupted manifest, verifier false-PASS); subjective answer-quality changes require reviewed evidence.
- **Q361 — Rollback granularity.** Roll back to a complete previous named profile + manifest pair, not an ad-hoc patchwork of flags/artifacts.
- **Q362 — Full activation.** Requires both locked benchmark/release gates and canary gates.
- **Q363 — Post-activation shadow.** Keep a lower-rate drift shadow (default 1–5%) for at least two stable releases.
- **Q364 — Bad canary cases.** Automatically create Human Review drafts for sampled severe failures; they enter golden/regression only after human confirmation.

## T. API, UI, Trace, and audit

- **Q365 — Verification field.** Expose both `answer_status` and `verification_status`; they answer different questions.
- **Q366 — Manifest IDs to clients.** `trace_id` is public; manifest/identity snapshot IDs are returned in an optional diagnostics object for supported clients and always stored in Trace.
- **Q367 — Citation schema.** Citations carry `record_id`, `source_snapshot_id`, locator(s), content hash/reference, source metadata, support relations, and live URL where applicable.
- **Q368 — Source role UI.** Display clear categorical labels such as primary/self-reported/independent/commentary/unknown without invented precision scores.
- **Q369 — Support versus background UI.** Explicitly separate “supports this claim,” “contradicts,” and “related/background.”
- **Q370 — UNVERIFIED UI.** Never render it with the normal trusted/supported styling.
- **Q371 — PARTIAL UI.** Show what is supported and what remains unresolved when that structure is available.
- **Q372 — UNSUPPORTED references.** May show relevant-but-insufficient sources in a separate section if clearly labeled as non-supporting; no fake citations to an answer claim.
- **Q373 — Invalid citations.** Never render them.
- **Q374 — Dead live URL.** Audit view still presents the pinned system-held snapshot/locator if retention policy allows.
- **Q375 — Generator-context trace.** Record Evidence Package ID/hash and constituent evidence IDs, subject to trace-redaction policy.
- **Q376 — Selection trace.** Record candidate IDs and selection/rejection reason codes without necessarily storing entire raw text.
- **Q377 — State transitions.** Trace every answer/claim state transition, repair action, retry, and terminal reason.
- **Q378 — Debug trace.** Full-text debug traces are opt-in, access-controlled, and short-retention; production default stores IDs/hashes and minimal spans.
- **Q379 — Sensitive queries.** Default trace stores redacted/hashed forms plus only the minimum text needed for diagnosis. Secrets are always scrubbed.
- **Q380 — Replay/privacy balance.** Keep raw query/evidence only under an explicit retention class with access controls/encryption and expiry; otherwise replay uses stable IDs/snapshots plus redacted query features and cannot claim perfect historical semantic replay when raw text is unavailable.

## U. Canonical production pipeline and ownership

- **Q381 — Final pipeline.** Canonical flow is: preserve original -> verified rewrite/semantic diff -> query entity resolution as needed -> router -> plan/decompose as needed -> high-recall multi-route retrieval -> requirement/route reserve -> content rerank -> Evidence Selector -> optional document workers -> Ledger -> conflict detection -> Grader -> bounded gap/retrieval loop -> Knowledge Boundary -> Evidence Package -> generation -> atomic claims -> claim/evidence mapping -> exact grounding -> deterministic numeric/time/ID checks -> entailment -> answer state/repair -> independent final verifier -> final response/reference cards.
- **Q382 — Search dump bypass.** No new-path stage may bypass Evidence Package and send raw Search Results directly to the Generator.
- **Q383 — `server.py` role.** End state: API/auth/guardrails/SSE lifecycle/dependency wiring only; retrieval and research algorithms live behind typed services/orchestrator interfaces.
- **Q384 — Core modules.** Yes, core algorithms move into testable modules with centralized resource loading and manifest pinning.
- **Q385 — FAST omissions.** FAST may skip decomposition, full Planner, multi-document workers, and iterative gap search when deterministic rules say they are unnecessary. It may not skip source eligibility, content rerank, minimum sufficiency checks, exact grounding, claim support checks, final state machine, or required verification for factual claims.
- **Q386 — Shared state model.** FAST/RESEARCH/DEEP use the same typed state schema; modes change bounds and activated stages, not semantics.
- **Q387 — Status write authority.** Only the final AnswerStateMachine commits user-visible `answer_status`; earlier stages emit evidence/coverage/verification inputs.
- **Q388 — Pre/post status conflict.** Pre-generation research status is provisional. Final post-generation claim verification can only maintain or downgrade support unless new validated evidence is added through a repair retrieval cycle.
- **Q389 — Early exits.** No-evidence/topic-exhausted/service-boundary exits also go through the canonical terminal state builder so API/Trace semantics remain uniform.
- **Q390 — Illegal SUPPORTED writes.** CI tests/lint must forbid direct production assignment of `SUPPORTED` outside the state-machine implementation and approved test fixtures.

## Decision summary

The governing principles are: **fail closed on correctness, degrade only where correctness can still be established, pin every request to immutable versioned evidence, separate retrieval hints from evidence, keep stable identity independent of array positions/content versions, make Evidence Package the only generation context, and prove production behavior with real integration/E2E tests before rollout.**
