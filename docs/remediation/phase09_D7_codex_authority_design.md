# D7 — Codex adversarial confirmation: RT-101 authority trust boundary (Q1 design round)

Date: 2026-09-09; Codex CLI 0.153.4; sandbox: read-only; prompt: /tmp snapshot below

## Codex full output (verbatim)

## Q1

**ANSWER:** Confirmed. A repo edit setting RT-101 to `"SATISFIED"` makes `core_eligible=true`, provided all required suites and invariants pass. It does not make production eligible because Q-336/RT-005/RT-075 remain blockers. There is also a second repo-edit path: set an external-state row to `satisfied` and point its “hashed proof” at any committed repo file with the matching SHA; then `load_external_blockers` removes that blocker. Removing the authority requirement also clears the core reason.

**EVIDENCE:**
- Policy status: [spec/phase09_release_policy.json:14](/home/rhett/tech-db-phase09-repair/spec/phase09_release_policy.json:14)
- Gate passes policy map directly: [scripts/run_phase09_release_gate.py:89](/home/rhett/tech-db-phase09-repair/scripts/run_phase09_release_gate.py:89)
- Evaluator accepts any non-`SATISFIED` status as blocking: [qa-backend/phase09_release.py:226](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:226)
- Core decision ignores external blockers: [qa-backend/phase09_release.py:229](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:229)
- Publication uses production decision: [scripts/authorize_runtime_publish.py:29](/home/rhett/tech-db-phase09-repair/scripts/authorize_runtime_publish.py:29)
- External-state proof hash: [qa-backend/phase09_release.py:55](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:55)
- I executed a no-edit decision simulation with the existing evaluator: RT-101 `"SATISFIED"` yielded `core_eligible=True, production_release_eligible=False, reasons=[]`.

**RISKS:** Direct P0 bypass; repo-controlled hashed proof can clear production blockers; policy deletion removes the authority gate entirely.

**RECOMMENDATION:** Treat both required authorities and external controls as owner-bound. Reject satisfaction claims in policy schema, and stop treating same-repo artifact hashes as external proof.

---

## Q2

**ANSWER:** The current trust boundary is the repository itself, except for the three external-control booleans and descriptions. `required_authorities` is a self-declared status map, not proof verification. `external_state.json` verifies only that a declared repo-relative file exists and hashes to a declared SHA; it does not authenticate the owner, signer, freshness, target SHA, or artifact semantics.

**EVIDENCE:**
- Repo status is read directly: [spec/phase09_release_policy.json:15](/home/rhett/tech-db-phase09-repair/spec/phase09_release_policy.json:15)
- Evaluator does exact string comparison: [qa-backend/phase09_release.py:227](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:227)
- External proof is repo-relative and only SHA-verified: [qa-backend/phase09_release.py:57](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:57)
- CI reads the same repo policy/external state: [.github/workflows/remediation-gates.yml:189](/home/rhett/tech-db-phase09-repair/.github/workflows/remediation-gates.yml:189)

**RISKS:** Committer-controlled policy/status files and same-repo proofs are treated as authority evidence.

**RECOMMENDATION:** Separate requirements, which may be committed, from satisfaction, which must originate outside the commit tree.

---

## Q3

**ANSWER:** The proposed requirement-object plus signed owner proof design is fundamentally sound, minimal, and fail-closed, but it has concrete gaps as written. The expected holdout digest must be a separate owner input; embedding it in repo policy would restore the bypass. The verifier must also guard against self-referential spec/fixture bindings: those values may identify what is being evaluated, but they cannot come solely from mutable repo files inside the signed payload’s trust basis. HMAC key leakage is a real risk if results, reasons, workflows, artifacts, or diagnostics echo environment values.

**EVIDENCE:**
- Current policy has no schema validator: [scripts/run_phase09_release_gate.py:49](/home/rhett/tech-db-phase09-repair/scripts/run_phase09_release_gate.py:49)
- Current evaluator expects a string map: [qa-backend/phase09_release.py:207](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:207)
- Benchmark provenance reads repo manifest values: [qa-backend/phase09_release.py:101](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_release.py:101)
- Locked fixture supplies manifest ID: [qa-backend/test_fixtures/phase09/benchmark_locked_v1.json:5](/home/rhett/tech-db-phase09-repair/qa-backend/test_fixtures/phase09/benchmark_locked_v1.json:5)
- Existing repository holdout lock is repo-editable: [qa-backend/test_fixtures/holdout/holdout.lock.json:3](/home/rhett/tech-db-phase09-repair/qa-backend/test_fixtures/holdout/holdout.lock.json:3)

**RISKS:** A commit can change `spec_manifest.json`, benchmark fixture, expected bindings, or verifier code. Commit timestamps can be forged. Overly verbose error handling can leak proof or key material. HMAC verification without strict canonical JSON and key-length checks can create ambiguity.

**RECOMMENDATION:** Use strict requirement allowlists and reject unknown keys plus any satisfaction-like key. Supply proof, expected holdout digest, and HMAC key only through separate owner-controlled secrets. Reject empty/weak keys, noncanonical JSON, extra proof keys, and ambiguous timestamps. Record only nonsecret proof digest, trust class, and reasons. Treat repo bindings as claims to be MAC’d, not as trust roots.

---

## Q4

**ANSWER:** Yes, the design can keep answer-level blinded holdout gold out of the repo if the proof schema carries only the lock digest and no case data, outputs, prompts, model responses, or raw results. Leakage channels that must be explicitly forbidden are logs, uploaded artifacts, PR bodies, issue comments, commit messages, generated evidence, and exception traces.

**EVIDENCE:**
- Current benchmark already publishes a holdout lock digest: [qa-backend/benchmark_phase09_result.json:8](/home/rhett/tech-db-phase09-repair/qa-backend/benchmark_phase09_result.json:8)
- Current CI uploads machine evidence even on failure: [.github/workflows/remediation-gates.yml:194](/home/rhett/tech-db-phase09-repair/.github/workflows/remediation-gates.yml:194)
- Current gate prints suite output tails: [scripts/run_phase09_release_gate.py:36](/home/rhett/tech-db-phase09-repair/scripts/run_phase09_release_gate.py:36)

**RISKS:** CI failure diagnostics can expose environment-derived values; evidence files can accidentally preserve full proof payloads.

**RECOMMENDATION:** Persist a sanitized `AuthorityResult` only. Do not persist the raw proof, expected digest, HMAC key, canonical signed payload, or secret-derived diagnostics. Upload redacted gate artifacts only; ideally make evidence upload nonblocking so it cannot make a failed authority gate green.

---

## Q5

**ANSWER:** The test-only seam can be safe if production entry points never import test helpers and production verification rejects `TEST_ONLY`. Having tests call `verify_authority_proof` with a test key is useful for adversarial coverage, but it must not be described as genuine external authority; it is a cryptographic schema/conformance fixture.

**EVIDENCE:**
- Current tests already construct a plain `"SATISFIED"` map: [qa-backend/tests_release_phase09.py:58](/home/rhett/tech-db-phase09-repair/qa-backend/tests_release_phase09.py:58)
- Production gate imports only the release module: [scripts/run_phase09_release_gate.py:17](/home/rhett/tech-db-phase09-repair/scripts/run_phase09_release_gate.py:17)
- Publication script does not import tests: [scripts/authorize_runtime_publish.py:1](/home/rhett/tech-db-phase09-repair/scripts/authorize_runtime_publish.py:1)

**RISKS:** A test helper could be promoted into production, or tests could be trusted as evidence in documentation.

**RECOMMENDATION:** Put helpers in test files or an explicitly test-only module with a constant marker trust class. Assert the production verifier rejects it. Use lint/import-boundary checks so production entry points cannot import test code.

---

## Q6

**ANSWER:** Proposed re-verification closes the fabricated/stale evidence-file path, but publication remains unsafe unless external blockers are also independently owner-verified. Current `authorize_runtime_publish.py` trusts `production_release_eligible` and `external_blockers` from JSON, while publish-runtime also has `workflow_dispatch` and write permissions.

**EVIDENCE:**
- Current authorizer trusts evidence fields: [scripts/authorize_runtime_publish.py:19](/home/rhett/tech-db-phase09-repair/scripts/authorize_runtime_publish.py:19)
- Workflow dispatch trigger: [.github/workflows/publish-runtime.yml:3](/home/rhett/tech-db-phase09-repair/.github/workflows/publish-runtime.yml:3)
- Release mutation permission: [.github/workflows/publish-runtime.yml:12](/home/rhett/tech-db-phase09-repair/.github/workflows/publish-runtime.yml:12)
- Fresh gate and exact-SHA authorization: [.github/workflows/publish-runtime.yml:32](/home/rhett/tech-db-phase09-repair/.github/workflows/publish-runtime.yml:32)

**RISKS:** A handcrafted green evidence file currently authorizes publication. Re-verifying only RT-101 still leaves Q-336, RT-005, and RT-075 bypassable through repo edits. Artifact confusion remains possible if later steps consume uploaded/generated files instead of fresh in-job outputs.

**RECOMMENDATION:** Re-run the full evaluator and independently re-verify every external blocker at authorization time. Make publication require a protected GitHub environment and owner review. Pass secrets only to the two authorization steps. Prefer a persisted, digest-bound authorization receipt over re-reading loose evidence JSON.

---

## Q7

**ANSWER:** One-way derivation plus a validator is sufficient only if it validates freshness, exact SHA binding, artifact hashes, generation ordering, authority/provider results, and human-report rendering. The current tree demonstrates the stale class: `test_summary.json` says 1633 passing, while committed Phase09 documents record 1631.

**EVIDENCE:**
- Current summary is 1633: [qa-backend/test_summary.json:4](/home/rhett/tech-db-phase09-repair/qa-backend/test_summary.json:4)
- Phase result records 1631: [docs/remediation/phase09_PHASE_RESULT.json:31](/home/rhett/tech-db-phase09-repair/docs/remediation/phase09_PHASE_RESULT.json:31)
- Phase result records the old summary hash: [docs/remediation/phase09_PHASE_RESULT.json:28](/home/rhett/tech-db-phase09-repair/docs/remediation/phase09_PHASE_RESULT.json:28)
- Current summary hash is `f877dd8bf0f68f4ee9abace1f5369fb829fa72d7e65abd1ca848aa022f9a8a7d`, not the recorded `02ba...`.
- No build/validate evidence scripts exist: `rg build_phase09_evidence|validate_phase09_evidence_chain` returned no implementation.

**RISKS:** Regenerating one artifact invalidates every dependent hash/count; hand-edited reports can diverge; documentation can claim coverage from a different checkout.

**RECOMMENDATION:** The proposed checks are necessary but add: source-artifact schema/version checks; monotonic generation timestamps; exact `tested_git_sha`, `evidence_generation_base_sha`, CI run ID, and workflow URL binding; prohibition of dirty-tree generation for committed docs; deterministic report rendering; explicit assertion that PR/release claims do not exceed machine artifacts; and a no-upload rule for committed docs on failure.

---

## Q8

**ANSWER:** RT-101 must be the sole expected red, while every other regression remains required. To ensure repo edits alone cannot clear it: reject satisfaction in policy schema; read proof material only from owner secrets; remove repo-file fallbacks for authority; make external blockers owner-bound; independently re-verify at publication; and protect the required checks and publish environment.

**EVIDENCE:**
- Required gate is the Phase09 job: [.github/workflows/remediation-gates.yml:175](/home/rhett/tech-db-phase09-repair/.github/workflows/remediation-gates.yml:175)
- Gate exits zero only for `core_eligible`: [scripts/run_phase09_release_gate.py:118](/home/rhett/tech-db-phase09-repair/scripts/run_phase09_release_gate.py:118)
- Publish workflow has `contents: write`: [.github/workflows/publish-runtime.yml:12](/home/rhett/tech-db-phase09-repair/.github/workflows/publish-runtime.yml:12)
- Current external controls are false: [spec/phase09_external_state.json:6](/home/rhett/tech-db-phase09-repair/spec/phase09_external_state.json:6)

**RISKS:** “Always upload” can soften the red-signal semantics; nonrequired status checks can create ambiguity; admin bypass can defeat branch protection.

**RECOMMENDATION:** Keep the canonical authority-absence gate required and red. Add a separate diagnostic job that asserts the exact expected absence reason and all product suites pass. Do not upload raw secrets or full proofs. Enforce GitHub environment reviewers on the publish job and enable enforce-admins branch protection.

---

**FINAL VERDICT**

DESIGN_CONFIRMED: YES

BYPASS_CONFIRMED_CURRENT: YES

MANDATORY_CHANGES:
- Replace authority status strings with a strict requirement-object schema that rejects satisfaction keys, unknown keys, and missing/extra controls.
- Add an env-only production authority provider; never read proof, expected holdout digest, or HMAC key from repo files.
- Use exact canonical JSON, strict proof schema, constant-time HMAC, empty-key rejection, normalized UTC time, commit-bound freshness, and sanitized persisted results.
- Change `evaluate_release()` to consume verified authority results only and reject legacy status maps.
- Re-run fresh evaluation and independent authority verification in both CI and publication; do not trust green evidence JSON alone.
- Make external-state satisfaction owner-bound; same-repo hashed artifacts are not external proof.
- Implement both evidence builder and validator, regenerate all dependent committed docs, and fail on stale hashes/counts/SHAs.
- Add adversarial tests for policy edits, repo proof files, stale/replayed proofs, wrong bindings, HMAC tampering, trust-class attacks, publication evidence forgery, and test/production import boundaries.
- Restrict and redact workflow secret handling; use a protected publish environment and protect required checks.

NICE_TO_HAVE:
- Signed transparency log or append-only external authority ledger.
- GitHub App/OIDC-issued proof receipts.
- Immutable run summaries binding workflow run ID, commit SHA, suite results, and authority outcomes.
- Deterministic evidence renderer with snapshot tests.
