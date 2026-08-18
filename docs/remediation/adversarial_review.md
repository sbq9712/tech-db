# Tech-DB Remediation Spec — Adversarial Review and Disposition

Reviewed document: `techdb_spec_draft.md`
Review stance: assume every ambiguous sentence becomes a production failure; challenge safety, determinism, operability, testability, and internal consistency.

## AR-01 — P0 — Authority is normative but not operationally versioned
**Attack:** The draft says the Decision Register outranks the spec, but does not define how a deployed binary proves which exact decision register it implements. A later edited Markdown file could silently change authority without changing the release.

**Disposition: ACCEPT.** Add `decision_register_version/hash` to the canonical spec manifest and global release manifest. Every release/benchmark records it.

## AR-02 — P0 — Stable record IDs can duplicate during migration/reingest
**Attack:** “Assign UUIDv7/ULID at ingest” is not enough. Reprocessing the same logical source after a failed migration could create another logical record with a new ID.

**Disposition: ACCEPT.** Add a persistent Record Registry with transactional uniqueness on an explicit source-key policy. ID assignment occurs once; reingest resolves through the registry before creating a record.

## AR-03 — P1 — Logical-record boundary remains ambiguous
**Attack:** Same URL changed content is same record; different URLs same body are different records. But redirects, canonical URLs, migrated domains, and duplicate source feeds are not defined.

**Disposition: ACCEPT.** Define a `SourceIdentityKey` and explicit merge/redirect rules. Canonical URL changes do not automatically merge records; resolver/migration records provenance and redirects. Content similarity alone never merges logical records.

## AR-04 — P0 — Citation eligibility and legal retention are mixed
**Attack:** The draft allows retrieval-only material when source text cannot be retained, but other sections say all final evidence must be replayable. A developer could still build a final citation from transient fetched text.

**Disposition: ACCEPT.** Introduce explicit `evidence_eligibility = CITATION_ELIGIBLE | RETRIEVAL_ONLY | QUARANTINED`. Only CITATION_ELIGIBLE source snapshots may enter Ledger support or final citations.

## AR-05 — P0 — Canonical extracted text is itself a transformation
**Attack:** Calling extracted text “canonical source text” can hide extraction changes. If HTML extraction v2 changes paragraph order, old offsets break even if raw bytes are unchanged.

**Disposition: ACCEPT.** SourceSnapshot pins both raw object hash/reference and immutable `evidence_text` plus extractor version. Evidence locators target the immutable evidence_text of that snapshot; a new extraction output creates a new evidence-text version even when raw bytes are identical.

## AR-06 — P1 — Manifest integrity is hashed but not authenticated
**Attack:** SHA-256 detects corruption but does not prove who published a manifest. A malicious/accidental process with write access can rewrite manifest and hashes together.

**Disposition: ACCEPT.** Add optional/production-recommended manifest signing or authenticated publish metadata. At minimum, publish identity, immutable storage permissions, and audit log are required. Signature enforcement becomes mandatory where a signing key infrastructure exists.

## AR-07 — P1 — Fail-closed cold start can turn a safe rollback into an outage
**Attack:** Refusing startup when `current` is bad is correct for integrity, but if the previous validated manifest is known locally, forcing manual intervention can create avoidable downtime.

**Disposition: PARTIAL ACCEPT.** Keep “no silent rollback.” Add an explicit startup policy `STRICT_FAIL_CLOSED` (default) or `EXPLICIT_PREVIOUS_FALLBACK` enabled only by named deployment config and emitting a critical event with actual active manifest. The process must never pretend it runs `current` when it runs previous.

## AR-08 — P0 — Hot reload may free artifacts still used by pinned requests
**Attack:** Request pinning is meaningless if hot reload closes mmap/index/file handles while in-flight requests still reference them.

**Disposition: ACCEPT.** RuntimeSnapshot resources become reference-counted/generation-scoped. Old artifact generations are retired only after all pinned requests release them.

## AR-09 — P0 — Synthetic hint leakage can still influence factual support indirectly
**Attack:** A synthetic hint can add a record to the candidate pool; the Selector may accept the record based on metadata even if no source span corresponding to the hinted concept is found.

**Disposition: ACCEPT.** Every hint-derived candidate carries `hint_reason`; to support the hinted proposition, it must produce an independently grounded EvidenceRef tied to that requirement. Candidate-level eligibility alone is insufficient.

## AR-10 — P1 — Removing summaries from primary vector search may crater recall before replacement is ready
**Attack:** The remediation could satisfy isolation by deleting summary text but materially destroy semantic recall.

**Disposition: ACCEPT.** T049 migration requires a before/after recall benchmark and a source-grounded replacement embedding strategy (title + source-grounded chunks/fields). Index isolation cannot full-activate if critical retrieval regression exceeds gate.

## AR-11 — P0 — FAST “local reranker” is underspecified and may not exist
**Attack:** The spec mandates a real local reranker but repository/deployment may not have one. Developers might rename RRF rank as a “local reranker.”

**Disposition: ACCEPT.** Define reranker behavioral contract: score/order must consume query + source-grounded candidate content. A pure fusion-rank transform does not qualify. If no local model is available, FAST may use a bounded configured remote reranker, but cannot claim compliance by reusing RRF.

## AR-12 — P1 — Candidate quotas can preserve junk and crowd out better evidence
**Attack:** Per-route quotas are useful for outliers but can reserve low-quality Graph/BM25 noise.

**Disposition: ACCEPT.** Route/requirement reserves apply only above a minimum eligibility floor. Quotas protect plausible candidates, not arbitrary route output.

## AR-13 — P0 — `all_results` can leak through debugging/history even if not passed directly
**Attack:** The Generator might receive Trace summaries, previous assistant messages, or planner context containing raw retrieved text from unselected records.

**Disposition: ACCEPT.** Add a generation-input allowlist: only user query, verified conversation premises, Evidence Package, and approved style/system instructions. Trace/debug/raw retrieval text is forbidden from Generator inputs.

## AR-14 — P0 — Verified conversation state can become stale across later source versions
**Attack:** A prior claim may have been verified against an older snapshot but later superseded. Carrying it as a factual premise could produce stale “current/latest” answers.

**Disposition: ACCEPT.** Prior premises carry temporal scope + manifest/snapshot and are revalidated for freshness when the new question has a current/latest or incompatible temporal requirement.

## AR-15 — P1 — Semantic diff itself can fail or hallucinate
**Attack:** The rewrite integrity gate depends on semantic diff, which could be model-generated and wrong.

**Disposition: ACCEPT.** Critical diff fields use deterministic extraction/comparison where possible; model diff is advisory. Parse failure or uncertain critical diff falls back to original query / Research mode rather than trusting rewrite.

## AR-16 — P0 — Status semantics for technical failure versus partial evidence remain ambiguous
**Attack:** Q193/Q282 allow PARTIAL/UNSUPPORTED or UNVERIFIED depending on context. Implementers may choose whichever is convenient.

**Disposition: ACCEPT.** Define a deterministic decision rule: if the system can establish evidence insufficiency without the failed capability, status is PARTIAL/UNSUPPORTED; if a technical failure prevents determining support for a claim the answer would present, that claim is UNVERIFIED and aggregate becomes UNVERIFIED unless the claim is removed and answer is re-evaluated.

## AR-17 — P0 — `SUPPORTED` requires “final verifier passed,” but verifier may be unnecessary for deterministic-only factual answers
**Attack:** The draft says factual verification is required but also allows NOT_APPLICABLE for non-factual responses. It does not define deterministic-only claims.

**Disposition: ACCEPT.** All user-visible factual claims require the canonical verification pipeline. Individual deterministic checks may satisfy portions, but the final independent verifier is required unless a versioned claim class is explicitly declared deterministic-complete and covered by locked tests. Initial spec declares no such factual exemption.

## AR-18 — P0 — Final verifier after repair can fail after max repair cycles
**Attack:** The spec says two repair cycles, but what if the final verifier fails semantically after the second repair? It needs an unambiguous terminal transition.

**Disposition: ACCEPT.** After repair budget exhaustion: technical verifier failure => UNVERIFIED; semantic unsupported findings => remove affected claims if a deterministic finalization can do so without new generation, otherwise PARTIAL/UNSUPPORTED with no unsupported draft exposed.

## AR-19 — P1 — Buffered verification breaks the meaning of streaming and may breach product latency expectations
**Attack:** “Generate fully, verify, then stream” preserves correctness but makes SSE a cosmetic chunker and may dramatically worsen perceived latency.

**Disposition: ACCEPT WITH DESIGN CHANGE.** Keep progress/status events immediately. Final factual content remains buffered until verified. Add measured `time_to_first_status`, `time_to_final_answer`, and baseline gates; do not relax correctness to preserve token TTFB. The API contract documents that token streaming is post-verification for the new profile.

## AR-20 — P0 — Generator may mention facts not extracted by claim parser
**Attack:** If claim extraction misses a factual sentence, it can escape mapping/verifier coverage and the answer may still be SUPPORTED.

**Disposition: ACCEPT.** Add claim-coverage gate: every declarative factual segment must be classified as a mapped claim or explicit non-factual text. Unclassified factual-looking spans block SUPPORTED and trigger repair/UNVERIFIED.

## AR-21 — P0 — Citation exactness and entailment can disagree on span granularity
**Attack:** An exact paragraph may contain the words but not the asserted relationship. Conversely, support may require two distant spans.

**Disposition: ACCEPT.** Support is claim-to-evidence-set, not claim-to-single-span. Entailment evaluates the minimal set of exact EvidenceRefs; multi-span support is explicit and bounded.

## AR-22 — P1 — `invalid citation displayed = 0` is measurable only after rendering
**Attack:** Backend tests can pass while frontend renders stale/invalid citation objects from cached state.

**Disposition: ACCEPT.** Add frontend integration tests and cache-version invalidation. Invalid citations are filtered both server-side and UI-side defensively; server remains authoritative.

## AR-23 — P0 — Evidence Package token truncation can delete a conflict/critical requirement
**Attack:** The draft allocates critical first but does not specify what happens when mandatory evidence exceeds model context.

**Disposition: ACCEPT.** Context packing has a hard “mandatory set”: critical support, critical conflict, and required conditions. If the mandatory set cannot fit, do not truncate it silently; summarize only with source-grounded structured compression or downgrade/abstain and record `context_capacity_exceeded`.

## AR-24 — P1 — Structured compression itself can become synthetic evidence
**Attack:** Compressing evidence to fit context creates new generated text that could be treated as evidence.

**Disposition: ACCEPT.** Any compression is a navigation representation carrying links to original EvidenceRefs; the Generator/verifier receives the original exact spans for support-critical claims, or the compressed representation is marked non-evidentiary.

## AR-25 — P1 — Cross-request DocumentEvidencePacket cache may leak sensitive/ACL-scoped data
**Attack:** Cache keys mention versions but not authorization/visibility scope.

**Disposition: ACCEPT.** Add `access_scope_fingerprint`/tenant policy to caches and Evidence Packages. Current public deployment uses a public scope; architecture must enforce future ACL filtering before context/caching.

## AR-26 — P0 — ACL is still mostly aspirational
**Attack:** The draft mentions future scope but does not state where ACL is applied. A later internal dataset could leak through retrieval cache or Graph.

**Disposition: ACCEPT.** Define ACL filter as a mandatory seam between route candidates and any Agent/worker/Generator context, and include access scope in cache keys. Public profile uses an allow-all policy implementation.

## AR-27 — P0 — SQLite WAL is unsafe if deployment has multiple writers/processes
**Attack:** “Current single-service deployment” may change; multiple Gunicorn/process/container writers can violate assumptions.

**Disposition: ACCEPT.** IdentityStore startup declares writer topology. SQLite backend is allowed only with a single writer service/process contract; multi-writer deployment requires Postgres/transactional shared DB before enabling mutations.

## AR-28 — P1 — Merge rollback can be computationally unbounded
**Attack:** Re-resolving all later mentions/relations after a high-impact merge could be huge and unsafe online.

**Disposition: ACCEPT.** Merge/split are offline/admin jobs with impact preview, bounded batch re-materialization, checkpointing, and atomic snapshot publish; never mutate serving graph in place.

## AR-29 — P0 — Manual override precedence can freeze wrong facts forever
**Attack:** The draft says review-due overrides stop auto-overwrite, which can perpetuate stale wrong overrides indefinitely.

**Disposition: ACCEPT.** Overdue overrides become `STALE_REVIEW_REQUIRED`; they remain visible but may be excluded from high-confidence auto resolution depending on risk class. Critical conflicting strong-ID evidence blocks serving the override as authoritative until review.

## AR-30 — P1 — Entity candidate Recall@10 >=0.98 is an arbitrary target
**Attack:** The draft invents a numeric target without baseline evidence, violating its own “benchmark first” philosophy.

**Disposition: ACCEPT.** Remove the hard 0.98 target. Establish baseline and pre-register class-specific gates from the locked gold set before activation.

## AR-31 — P0 — Graph-V2 can remain off, but T034 depends on Graph metrics
**Attack:** Release benchmark could block non-Graph completion because some required suites expect Graph-V2 artifacts.

**Disposition: ACCEPT.** Acceptance matrix distinguishes profile-required versus benchmark-only experimental gates. Graph-V2 tests must run where artifacts exist, but failure to beat gain gate means Graph profile stays off rather than blocking `agentic_correctness_core`.

## AR-32 — P1 — Relation-critical detection can be wrong and silently skip Graph
**Attack:** Router/Planner may misclassify a query. The system could answer a relation question from weak text without Graph and claim SUPPORTED.

**Disposition: ACCEPT.** The Grader/claim-type policy independently detects relation claims and checks whether the required evidence method was satisfied. Router is not the sole guard.

## AR-33 — P0 — Degradation matrix lacks capability provenance in answer state
**Attack:** A final SUPPORTED answer after a route failure may be valid, but audit cannot tell whether a capability was unavailable.

**Disposition: ACCEPT.** Add `degraded_capabilities[]` to ResearchState/Trace and optional diagnostics. Support state remains evidence-based, but operational degradation is always observable.

## AR-34 — P1 — Stage timeout defaults are arbitrary and environment-specific
**Attack:** Hardcoded seconds in normative spec will become wrong immediately across models/hardware.

**Disposition: ACCEPT.** Move numeric timeouts to profile defaults, explicitly provisional. Release gates compare against measured baseline/SLO and versioned config; the invariant is bounded deadlines, not these exact seconds.

## AR-35 — P0 — One retry can double expensive LLM work after client disconnect
**Attack:** Retry policy must check cancellation and remaining total deadline/budget before retrying.

**Disposition: ACCEPT.** Retry requires request still active, retryable error, remaining deadline, and budget reservation. No retry after cancellation.

## AR-36 — P1 — 50-concurrency/32-worker test numbers are arbitrary
**Attack:** Fixed numbers can be too low or impossible depending on CI.

**Disposition: ACCEPT.** Define minimum logical stress behavior plus profile-specific capacity targets. Keep 50/32 as initial CI targets, not universal correctness thresholds; production readiness also tests at expected peak concurrency.

## AR-37 — P0 — Live-model release tests are nondeterministic and can block legitimate releases
**Attack:** A single live GLM probe can fail due provider noise.

**Disposition: ACCEPT.** Hard correctness is primarily deterministic/recorded. Live probes are release-health signals with bounded retry and trend thresholds, not sole truth for semantic correctness. Locked benchmark artifacts use controlled model/version and repeated aggregate metrics.

## AR-38 — P0 — Holdout contamination via Human Review pipeline
**Attack:** Turning production bad cases into golden data and then tuning on them can contaminate the release holdout.

**Disposition: ACCEPT.** Maintain separate development regression cases and locked release holdout. Human-confirmed cases enter development regression by default; periodic holdout refresh uses a controlled blinded process.

## AR-39 — P1 — Branch protection is not sufficient against bot auto-sync commits
**Attack:** Current main is updated by automation. Protection rules must cover bot identities or they can bypass required checks.

**Disposition: ACCEPT.** Required checks apply to automation too. Content/data-only sync may use a separate protected workflow with path-scoped checks; it cannot bypass code/spec gates when protected paths change.

## AR-40 — P0 — Canary “invalid citation => rollback” can be caused by old cached frontend data
**Attack:** Auto-rollback could target a backend release for a stale client cache artifact.

**Disposition: ACCEPT.** Hard rollback trigger requires attribution to the active release/profile or server response. Unknown-source incidents pause rollout and investigate; do not auto-roll back blindly.

## AR-41 — P1 — Canary minimum samples ignore query mix
**Attack:** 2,000 easy FAST queries do not validate DEEP/multi-document/entity/Graph behavior.

**Disposition: ACCEPT.** Add stratified minimum coverage by mode/question type/critical feature. Overall sample counts alone are insufficient.

## AR-42 — P0 — Full answer shadow may expose doubled sensitive data to model providers
**Attack:** Shadowing the complete pipeline can duplicate LLM transmission, increasing privacy exposure and cost.

**Disposition: ACCEPT.** Shadow eligibility honors data classification and provider policy. Sensitive/private profiles may use local replay/stage shadow instead. Shadow telemetry records whether a case was eligible and why.

## AR-43 — P1 — Manifest/profile rollback may be incompatible with current DB mutations
**Attack:** Rolling back code/artifacts while identity/admin DB mutations have advanced can create incompatible state.

**Disposition: ACCEPT.** Serving identity is snapshot-based; admin mutations build the next snapshot and do not mutate the active snapshot. Rollback switches to the previous immutable identity snapshot, while mutation audit history remains forward-only.

## AR-44 — P0 — Final status can be downgraded after answer text is finalized but not repaired
**Attack:** The draft says final verifier comes after repair, but if it downgrades support, the answer wording may still sound certain.

**Disposition: ACCEPT.** Finalization order changes: verifier findings feed the state machine; terminal answer renderer applies required uncertainty/boundary wording after final state. No user content is emitted until this terminal rendering is complete.

## AR-45 — P0 — State-machine lint alone cannot prevent direct SUPPORTED behavior
**Attack:** Developers can return `"SUPPORTED"` through serialization helpers or hardcoded fixtures in production code.

**Disposition: ACCEPT.** Add architectural tests scanning production modules for direct terminal-state construction plus runtime invariant tests that all done responses carry a state-machine transition trace ID/version.

## AR-46 — P1 — Evidence role/source classification may be wrong
**Attack:** Source-role metadata is treated as a hard Grader input, but the offline classifier can mislabel a media repost as independent.

**Disposition: ACCEPT.** Source-role hard rules use deterministic provenance where available; uncertain role remains `unknown`. A model/classifier cannot upgrade independence without provenance evidence. Evaluation includes source-role accuracy.

## AR-47 — P0 — Provenance clustering probabilities can falsely collapse independent sources
**Attack:** Overaggressive clustering could erase genuinely independent evidence and cause false abstention.

**Disposition: ACCEPT.** Use conservative thresholds with `same_origin_probability` and preserve uncertain groups. Hard collapse requires high-confidence provenance; ambiguous lineage reduces independence weight rather than forcing same-group identity.

## AR-48 — P1 — Temporal “current/latest” freshness policy is not explicit enough
**Attack:** Revalidating prior claims is good, but retrieval itself needs a freshness/supersession rule. Old high-ranking sources may dominate.

**Disposition: ACCEPT.** Add claim-type temporal policy: current/latest requirements require recent/supersession-aware evidence and explicit cutoff/as-of semantics; superseded-only evidence cannot satisfy them.

## AR-49 — P0 — Numeric facts need provenance through normalization/conversion
**Attack:** The draft says deterministic checks, but converted values can become detached from the exact source unit/value.

**Disposition: ACCEPT.** NumericFact stores original value/unit/scope locator plus normalized value/unit and transformation rule/version. Final claims reference both.

## AR-50 — P1 — Reference UI could expose copyrighted full snapshots
**Attack:** “Audit view shows pinned snapshot” may violate content-retention/display rights even if storage is permitted internally.

**Disposition: ACCEPT.** UI exposure is separate from backend retention. Reference cards show only policy-permitted minimal spans; full snapshot access is role/policy-controlled and may be unavailable to end users.

## AR-51 — P0 — Replay can accidentally use current model/config and call itself historical
**Attack:** “Replay” must distinguish exact historical replay, artifact replay with substitute model, and current comparison.

**Disposition: ACCEPT.** Add explicit replay modes and fidelity labels: `HISTORICAL_EXACT`, `HISTORICAL_ARTIFACTS_CURRENT_MODEL`, `CURRENT_COMPARISON`, `PARTIAL_REPLAY`.

## AR-52 — P1 — Public/admin cache keys omit pipeline profile
**Attack:** A cached Evidence Package or worker result from one profile can leak semantics into another.

**Disposition: ACCEPT.** Cache keys include manifest ID/profile plus source snapshot, access scope, schema, prompt/model where relevant.

## AR-53 — P0 — Grader hard rules may be bypassed by FAST path
**Attack:** FAST is allowed to avoid full semantic Grader, but relation/self-report/current/numeric hard rules must still run.

**Disposition: ACCEPT.** Create a mandatory deterministic `EvidencePolicyEngine` shared by all modes. The semantic Grader is additional, never the only source of hard rules.

## AR-54 — P1 — “No factual claim exemption” may force an expensive final LLM verifier on trivial facts
**Attack:** This conflicts with the goal that FAST not pay unnecessary control-model cost.

**Disposition: REBUT IN PART.** Correctness takes precedence. However the verifier implementation may be a deterministic verifier for deterministic-complete claim classes once such classes are explicitly versioned and benchmarked. Until then, no factual exemption is assumed. This is intentionally conservative.

## AR-55 — P0 — Same strongest model for Generator and Verifier is not independent enough if prompts share context
**Attack:** Role separation alone may not prevent correlated errors if verifier sees the same generated rationale/context.

**Disposition: ACCEPT.** Verifier input is restricted to question/scope, atomic claim, exact EvidenceRefs/metadata, and deterministic-check outputs. It never receives Generator hidden reasoning or unselected context. Independence is contextual, not model-vendor identity.

## AR-56 — P1 — User-facing `UNVERIFIED` can be abused as a dump-anything escape hatch
**Attack:** A system could generate speculative text, mark UNVERIFIED, and claim compliance.

**Disposition: ACCEPT.** UNVERIFIED is not permission to return arbitrary draft. Default terminal rendering either returns verified supported portions plus a verification-warning boundary or withholds factual draft that could not be validated.

## AR-57 — P0 — No explicit invariant prevents unsupported claim from surviving as hedged language
**Attack:** “可能/据说” can still be a factual attribution requiring support.

**Disposition: ACCEPT.** Claim parser/state machine treats hedged, modal, prediction, and attribution claims as factual/epistemic claims with typed support requirements; hedging does not make evidence unnecessary.

## AR-58 — P1 — Release gate may deadlock on graph/ER artifacts during unrelated data-only sync
**Attack:** Every auto-sync could trigger huge rebuilds and block operational data refresh.

**Disposition: ACCEPT.** Define artifact dependency graph and incremental release classes. Data changes rebuild only affected artifacts; a data release still must produce a compatible complete manifest for its active profile, but experimental Graph artifacts are not required when profile-disabled.

## AR-59 — P0 — No disaster recovery/backup rule for identity and manifests
**Attack:** Atomic release is not enough if the manifest store or Identity audit DB is lost.

**Disposition: ACCEPT.** Add backup/restore and restore-validation requirements for manifests, snapshot catalogs, identity mutation audit, and stable-ID registry. Recovery tests are part of operations readiness.

## AR-60 — P1 — Spec completion can be gamed by declaring features profile-disabled
**Attack:** Teams could disable hard unfinished modules in a named profile and call the whole project complete.

**Disposition: ACCEPT.** Distinguish `core-required` capabilities from `optional benchmark-gated` capabilities. Synthetic isolation, stable identity, evidence package, state machine, verification, cancellation, and real acceptance are core-required. Only explicitly listed experimental capabilities (notably Graph-V2 production activation) may remain disabled without blocking core completion.

# Review outcome

The draft is directionally correct but required material hardening in identity deduplication, evidence eligibility, runtime resource generations, state-machine determinism, context/claim coverage, ACL/cache scoping, mutation/release rollback compatibility, evaluation integrity, and rollout attribution. All accepted changes are incorporated into the final specification.
