# RT-101 V8 prep — Codex Review Cluster B (verbatim final verdict)

- Reviewer: codex exec (--sandbox read-only), serial run (after Cluster A
  reached P1=0), stdin: prompt file
- Date: 2026-09-15 (session)
- Reviewed HEAD: 4e41a86
- Scope: Cluster B — scorer guard, formal pre-seal chain, runner identity /
  provenance / contamination, corpus + coverage gate APIs.
- Blinding: reviewer confirmed — "No hidden gold, holdout, owner-secret
  blind, or state artifact contents were opened, read, printed, or
  inferred; this review used public repository artifacts, file names
  already present in public scripts, aggregate counters, and SHA-256
  references only."
- Verdict: **REJECT** — REMAINING P0: 1, P1: 5, P2: 4.

## Verbatim verdict

```
VERDICT: REJECT
P0 COUNT: 1
P1 COUNT: 5
P2 COUNT: 4
1. [qa-backend/corpus_compatibility.py:256](...) + [qa-backend/source_coverage.py:307](...) — P0 — Both gates revalidate supplied report fields, but neither takes the freshly recomputed candidate identity, live measured identity, expected gold/holdout lock digest, or expected dataset/case counters. A structurally valid report generated from stale bytes, another dataset, or an incomplete lock can therefore pass at gate [2.5]/[2.6]. The planned order recomputes identity at [1], but the assertion API has no binding between that recompute and the report it accepts; this does not yet guarantee that captures are scored only against the exact locked gold. Fix before sealing: recompute candidate/runtime/lock identities from actual bytes immediately before assertion, pass expected SHA-256 references and aggregate counts into strict assert wrappers, reject any report whose bindings or counters do not equal those freshly measured values, and pass only a read-only locked-gold handle plus its lock digest to capture/scoring. Record digests only in the decision chain.
2. [qa-backend/tests_repair_v7_postmortem.py:268](...) — P1 — Existing provider tests cover verifier timeout, 429, malformed output, and bounded retries, but not formal pre-seal health ordering. A latency/health probe at [3] can reject an already unhealthy provider, yet the planned contract does not specify atomic pre-seal behavior or a same-route readiness test, and a probe cannot guarantee future provider latency. Fix: add a formal-runner test proving synthetic unhealthy, timeout, and non-canonical-model probe failures exit nonzero with no marker; use the same credential route/model/timeout class for the probe; and add a bounded synthetic readiness soak before marker creation. Post-seal provider degradation must remain a recorded one-shot failure with no semantic retry or rerun.
3. Planned gate [5] as specified — P1 — "marker seal" does not by itself prevent a TOCTOU double seal or an unnoticed predecessor marker. Two runners can both pass earlier checks and then race marker creation; stale/spent-marker handling and decision-chain fork behavior are also absent from the reviewed suites. [tests_ci_identity_contract.py:417](...) locks CI head/provenance ordering, not one-shot marker exclusivity; [tests_rt075_approval_gate.py:327](...) locks approval proof discipline, not runner marker/decision lifecycle. Fix: require `O_CREAT|O_EXCL` plus fsync for the real marker, inspect and reject a predecessor/spent marker before all authority provisioning, record candidate/head/gold-lock/run identity in the marker, and reject rerun if a compatible or forked decision-chain head exists. Verify the entire prior chain and previous head before any append; dry-run must create neither marker nor decision chain.
4. [qa-backend/source_coverage.py:132](...) and [qa-backend/source_coverage.py:307](...) — P1 — Coverage build counts missing raw object refs, but validation never rejects `raw_object_ref_missing_count`; it also computes only dataset-minus-index missing records, not extra/unexpected citation-eligible index records. Fix: require zero missing raw refs by default, derive the exact expected citation-eligible record set from the locked dataset bytes, and reject both missing and extra citation-eligible identities while reporting retrieval-only/quarantined counts under explicit policy.
5. [qa-backend/scorer_guard.py:45](...) — P1 — The guard correctly rejects empty surfaces, malformed counts, and silent abstentions, but it has no truncation/terminal-completion contract. A truncated response that still carries claims or requirement counts passes as scorable. Fix: require an explicit non-truncated terminal-completion field on ANSWER-family rows; reject missing, unknown, length-, abort-, or cancellation-class completions without changing scoring thresholds. Extend population checks to require a positive expected population size for formal captures.
6. Planned gate [4]/[7], no public lock test — P1 — Scorer digest pinning protects code bytes only if every threshold/config input is inside the pinned bytes. No reviewed test locks rejection of scorer environment overrides, external threshold sidecars, or post-pin threshold drift. Fix: define one canonical scorer/config bundle, digest its exact bytes, reject environment and sidecar overrides, record the bundle digest in the decision chain, and add drift tests that prove mismatched digest/configuration aborts before scoring and authority provisioning.
7. [qa-backend/source_coverage.py:307](...) — P2 — `require_no_missing` and `require_clean_scans` may be disabled, and validation does not verify arithmetic consistency such as `indexed_source_count == eligible + retrieval_only`, `empty_evidence_source_side_count <= empty_evidence_count`, or required non-null generated-from identity fields. Fix: add a formal-only strict wrapper that fixes both options to true, validates all count invariants and required identity fields, and does not expose relaxed coverage validation to runner call sites.
8. [qa-backend/corpus_compatibility.py:161](...) — P2 — Binding fields are compared as nonempty strings but not format-validated. Equal malformed values can satisfy `corpus_sha256`, snapshot IDs, and evaluated head; `prompt_schema_config_versions` can also match with empty values. Fix: require 64-lowercase-hex corpus/content digests, the exact registered snapshot/head formats, and a nonempty key/value schema for version maps in both evaluate and assert paths.
9. [qa-backend/scorer_guard.py:109](...) — P2 — Any calibrated-refusal marker exempts a non-substantive row with no scorable surface. A technical-failure row carrying `stop_reason="no_evidence"` would be exempt despite not being a calibrated refusal. Fix: require refusal marker precedence to be explicit and orthogonal to technical-failure evidence; a technical-failure component/status must remain `ANSWER_ROW_UNSCORABLE` even if a refusal-looking string is present.
10. [qa-backend/source_coverage.py:292](...) — P2 — A vacuous no-gold digest scan is visibly labeled non-meaningful but still validates clean when path markers are absent. Aggregate path and pattern scans are useful, but formal contamination readiness should not accept an empty forbidden-digest set as sufficient. Fix: require the formal runner to supply a nonzero aggregate forbidden-digest reference set while retaining aggregate-only handling and SHA-256-only logging.

Verification: scorer-guard T4 passed 21/0; corpus gates passed 82/0; CI identity contract passed 45/0; RT075 approval gate passed 51/0. The full repair suite reached T8 without failure but stalled in the later environment-dependent real-map section and was stopped; `/tmp` and `/var/tmp` were unusable, so temp-dependent suites required `TMPDIR=/dev/shm`.

Blinding confirmation: No hidden gold, holdout, owner-secret blind, or state artifact contents were opened, read, printed, or inferred; this review used public repository artifacts, file names already present in public scripts, aggregate counters, and SHA-256 references only.
```

## Dispositions (all findings; serial discipline: P0/P1 → 0 before Cluster C)

- **P0-1 (gate assertion not bound to freshly recomputed identity)** → FIXED at
  4e41a86: `corpus_compatibility.assert_formal_run_allowed` now accepts
  `expected_candidate_binding` / `expected_membership` / `expected_head`
  and denies ANY drift between the report and caller-recomputed values
  (candidate binding fields, membership counters, measured head).  The V8
  runner's [2.5] block passes the freshly recomputed candidate binding
  (from pinned `corpus_pinning.json`), the builder membership ledger, and
  the measured HEAD into the assertion (runner update this commit cycle).
  Locked by tests_corpus_gates_phase09 `t_expected_binding_crosscheck`.
  Note: the "pass only a read-only locked-gold handle plus its lock digest
  to capture/scoring" half is already the V8 design — the scorer reads
  GOLD from the 0700 state dir, binds its sha in the identity recompute
  ([1/9]) and the decision chain now records candidate/lock/scorer/
  threshold digests.
- **P1-2 (no formal pre-seal health/latency tests; probe not same-route;
  no readiness soak)** → FIXED (this commit cycle):
  `qa-backend/formal_preflight.py` is the testable extraction of the
  provider health + latency sanity contract (stdlib-only, side-effect-free,
  never prints the key).  New T12 proves with synthetic HTTP servers:
  healthy probe passes (model echo verified); HTTP 500, unreachable,
  timeout/latency-exceeded, and silent model substitution all FAIL CLOSED
  with structured error classes; the soak requires consecutive successes
  with bounded waiting; the probe request uses the canonical contract
  (same credential route, canonical model, thinking disabled, bounded
  max_tokens); the module creates NO files (a dry run can never touch the
  marker).  The V8 runner [3/9] now invokes this module (health + 3×
  consecutive-success readiness soak, budget 40s / timeout 45s) and aborts
  pre-seal on failure.  Post-seal degradation remains a recorded one-shot
  failure (unchanged lineage).
- **P1-3 (TOCTOU double seal; predecessor marker; chain fork)** → FIXED
  (this commit cycle): the V8 runner [0/9] now inspects and prints any
  predecessor marker before all authority-relevant work and refuses re-run
  when a decision chain already exists; [5/9] creates the real marker
  ATOMICALLY (`set -o noclobber` == O_CREAT|O_EXCL semantics) with
  fsync of file + directory, recording run_id, candidate SHA, lock SHA,
  evaluated HEAD, scorer SHA, blind-cases SHA; marker-creation losers
  abort without consuming anything.  [8/9] verifies the ENTIRE prior
  chain before appending (recomputes the chain hash from the last log
  line and compares with the stored chain head; mismatch = forked or
  tampered → refuse), refuses a second decision for the same HEAD, and
  the chain entry binds prev/decision/report/head/candidate/lock/scorer/
  thresholds digests.  Dry-run creates neither marker nor decision chain
  (structural + line-ordered assertions locked in owner-side
  `rt101/tests/test_v8_runner_lifecycle.sh` — 19 checks, all passing:
  noclobber race, fsync, marker identity fields, dry-run ordering,
  chain digests, fork detection, same-head refusal).
- **P1-4 (coverage validation never rejects missing raw refs; no extra
  citation-eligible rejection)** → FIXED at 4e41a86:
  `validate_source_coverage_strict` / `assert_source_coverage_strict`
  (formal-only) enforce missing_count == 0 and
  `extra_citation_eligible_count == 0` (both computed during the build —
  `extra_citation_eligible` is now tracked in BOTH identity branches),
  plus evidence-storage pairing.  The live install report was regenerated
  and declares `evidence_storage="inline"` (all 30391 records carry
  inline evidence; `raw_object_ref` is NULL by deployment layout), so the
  pairing check rejects missing raw refs for raw-object layouts without
  faking policy for this install.  V8 runner [2.6] now runs the STRICT
  validator in addition to the CI validator.
- **P1-5 (no truncation/terminal-completion contract; population
  positivity)** → FIXED (this commit cycle): `scorer_guard.py` gains the
  terminal-completion classification (R7) over the runtime's REAL
  stop_reason vocabulary (answer_status._derive_terminal + server
  early-exit terminals — every real completion is classified, so
  legitimate captures never hit the unknown rejection): missing, unknown,
  truncation-class (length/max_tokens/truncated/budget_exceeded/
  context-capacity) and abort/cancellation-class (client_disconnect/
  request_scope_finalized/cancel*/timeout/rate_limited/admission) fail
  closed on ANSWER-family rows even with a full surface; technical-class
  stops (`technical_failure:*`, `coverage_gate_technical:*`,
  verification_not_run/incomplete, grader/claim-results/undetermined)
  deliberately remain on the scorer's verifier-technical threshold
  pathway — scoring thresholds unchanged (P1-5's own constraint).
  `validate_capture_population` gains `require_positive` and
  `expected_total` (defaults backward-compatible; formal captures bind
  both).  New T11 (28 checks) + extended module selftest + extended
  scorer REQUIREMENT_EXTRACTION_SELFTEST (14 fixtures) lock the contract.
  The embedded scorer guard copy (score_v8.py, pre-freeze) mirrors the
  module byte-for-byte in behavior.
- **P1-6 (no lock test for threshold/sidecar/env overrides)** → ALREADY
  BOUND + drift test added (this commit cycle): score_v8.py bakes
  `_LOCKED_THRESHOLDS` as a literal INSIDE the pinned scorer bytes and
  cross-checks the repo thresholds file at module load (any drift →
  FAIL_CLOSED exit 2 BEFORE scoring; thresholds enter only via that one
  file — no env/sidecar threshold override exists).  The V8 runner [8/9]
  decision chain now records scorer_sha256 AND thresholds_sha256 along
  with candidate/lock/head digests.  Owner-side
  `rt101/tests/test_v8_runner_lifecycle.sh` proves: mutated threshold
  fixture → scorer exits 2 with the drift message; unmutated control →
  selftest passes; thresholds literal present in the pinned bytes.
- **P2-7 (strict coverage wrapper)** → FIXED at 4e41a86 (see P1-4):
  `validate_source_coverage_strict` fixes the relaxed options to true and
  validates arithmetic invariants (`indexed == eligible + retrieval_only`,
  `empty_source_side <= empty`), required generated-from identity fields,
  evidence-storage pairing, and non-vacuous gold scan; runner call sites
  use the strict path.
- **P2-8 (binding format validation)** → FIXED at 4e41a86:
  `binding_formats_valid` check (64-lowercase-hex digests, sha256:-prefixed
  dataset snapshot ids, 40-hex heads, both evaluate and assert paths);
  fixtures updated to realistic formats (105/105 corpus gates).
- **P2-9 (refusal precedence vs technical failure)** → FIXED (this commit
  cycle): `scorer_guard._technical_failure_evidence` detects technical
  stop tokens (top-level AND canonical state-machine), non-empty
  state-machine technical_failures, and verification_status
  TECHNICAL_FAILURE; while ANY is present the calibrated-refusal exemption
  is unavailable (`ANSWER_ROW_UNSCORABLE` stands even with a
  refusal-looking string).  Production-safe: every component recorded via
  `record_technical_failure` is validation-blocking, so technical_failures
  non-empty always accompanies a technical terminal — no false positives
  on clean SUPPORTED rows.  Locked in T11 (laundering fixtures for all
  three evidence channels + honest-row controls).
- **P2-10 (vacuous no-gold scan accepted)** → FIXED at 4e41a86: strict
  validation requires `no_gold_scan.digest_scan_meaningful is True`; the
  formal runner additionally supplies a nonzero aggregate forbidden-digest
  reference set at freeze (salt/case digests), satisfying the "nonzero
  aggregate reference set" requirement while keeping SHA-256-only logging.

## Cluster B verification state (post-repair, this session)

- qa-backend/tests_repair_v7_postmortem.py: **232 passed, 0 failed**
  (incl. T11 completion contract + T12 formal preflight synthetic servers)
- qa-backend/tests_corpus_gates_phase09.py: **105 passed, 0 failed**
- qa-backend/tests_ci_identity_contract.py: **45 passed, 0 failed**
- qa-backend/tests_rt075_approval_gate.py: **51 passed, 0 failed**
- qa-backend/tests_failure_injection_phase09.py: **20 passed, 0 failed**
- score_v8.py --selftest (builder working copy): **PASS, 14 fixtures**
- owner rt101/tests/test_v8_runner_lifecycle.sh: **19/19 PASS**
- live provider readiness soak (real GLM route via local proxy, thinking
  disabled, model echo verified): **ok, latencies 1.2s/1.5s**
- Live dev-chain sanity after strict legacy-map change: full SSE chain
  PARTIALLY_SUPPORTED with stable-UUID citations (requirements_total=17).

Blinding note (this repair session): no hidden gold, holdout, owner-secret
blind, or sealed state artifact contents were read, printed, committed, or
inferred; all fixtures synthetic; salts/keys never printed or committed.
