# D7 — Codex adversarial code review (round 2, post-implementation)

- Invoked: 2026-09-09, `codex exec -s read-only`, prompt scope: full diff
  67f78b2..a4d44a1 plus full current file contents of every touched file.
- Raw output preserved verbatim below (between the markers).
- Disposition of every finding: **accepted**; fixes landed in commit 8d4e121
  ("D7: close all Codex code-review findings (P0/P1 hardening)"), followed by
  a full push-tier re-run whose authoritative evidence binds the new HEAD.
- Note: the review's VERDICT lines (P0_NOT_CLOSED / P1_NOT_CLOSED) refer to
  the pre-fix tree a4d44a1. Re-closure is adjudicated by the Final Gatekeeper
  round (docs/remediation/phase09_D7_codex_gatekeeper.md) at the fixed head.

One finding to note for future callers: the review correctly flagged that the
first version of our policy-strip test mutated the repo policy file in place
(try/finally restore). During a test run the strip window was observed by a
concurrently generated chain (the 3 C13 failures in that run were the strip
state leaking). The fix removes the repo-mutation pattern entirely: the
authorize script now accepts --policy and the test feeds a stripped temp copy,
so no crash or concurrency window can ever leave the repo policy stripped.

<!-- codex output begin (verbatim) -->
1. **HIGH — Area 1** — [scripts/authorize_runtime_publish.py:53](/home/rhett/tech-db-phase09-repair/scripts/authorize_runtime_publish.py:53) reads `required_authorities` directly from policy and has no hardcoded mandatory-authority check. A repo commit may remove the RT-101 entry, make `required_authorities={}`, and leave fabricated evidence claiming `core_eligible=true`; with the external-state rows also normalized, this script prints `PUBLISH_AUTHORIZED` without consulting any RT-101 proof. The publish workflow’s preceding gate mitigates that specific YAML path, but this standalone authorization path violates the “only owner secrets can authorize publication” boundary. Enforce `MANDATORY_AUTHORITIES ⊆ requirements`, require an RT-101 entry, and fail before policy-driven verification.

2. **MEDIUM — Areas 1/2** — [scripts/validate_phase09_evidence_chain.py:312](/home/rhett/tech-db-phase09-repair/scripts/validate_phase09_evidence_chain.py:312) also derives its authority checks only from policy. If policy omits RT-101, C13 iterates nothing and C13b can be vacuously satisfied. The validator therefore cannot independently prove that the chain did not normalize away the mandatory authority. Import `MANDATORY_AUTHORITIES` from `phase09_release`, validate the full mandatory set, and require RT-101 authority state in `PHASE_RESULT`.

3. **MEDIUM — Area 2** — [scripts/validate_phase09_evidence_chain.py:143](/home/rhett/tech-db-phase09-repair/scripts/validate_phase09_evidence_chain.py:143) compares only four decision fields and blockers; `reasons` are ignored. A fabricated `PHASE_RESULT` with `core_eligible=false` but no authority reason, or a fabricated green release evidence file with reasons erased, can still satisfy C4 when live authority is absent. C13 catches only an explicit authority-section flip, not omission or divergent reason semantics. Compare the complete normalized decision, including `reasons` and per-authority sanitized results, against both fresh evidence and the live environment channel.

4. **MEDIUM — Area 2** — [scripts/validate_phase09_evidence_chain.py:253](/home/rhett/tech-db-phase09-repair/scripts/validate_phase09_evidence_chain.py:253) verifies that `tested_git_sha` is a real ancestor of HEAD, but never verifies `evidence_generation_base_sha`. An invalid or unrelated base can make `git diff tested base` fail, `git()` swallow the error at lines 62-67, return an empty drift list, and pass C9. Resolve and validate the base independently: require a real commit, ancestor-or-equal of HEAD, and ancestor-or-equal relationship supporting the declared descendant-base semantics.

5. **MEDIUM — Area 2** — [scripts/build_phase09_evidence.py:189](/home/rhett/tech-db-phase09-repair/scripts/build_phase09_evidence.py:189) checks divergence only when both recorded and fresh blocker sets are non-empty. If either side is empty, the condition is false and recorded/fresh divergence passes. Make it unconditional: compare `sorted(decision.get("external_blockers", [])) != sorted(fresh_blockers)` and fail on any mismatch.

6. **MEDIUM — Areas 2/3** — [scripts/validate_phase09_evidence_chain.py:225](/home/rhett/tech-db-phase09-repair/scripts/validate_phase09_evidence_chain.py:225) enforces monotonic timestamps but has no future-time upper bound. A fabricated future ticket timestamp can satisfy generation order indefinitely. Require all timestamps to be valid, timezone-aware, not materially in the future, and define an explicit normalization rule for the runner’s naive local timestamp.

7. **MEDIUM — Area 5** — [qa-backend/tests_release_phase09.py:229](/home/rhett/tech-db-phase09-repair/qa-backend/tests_release_phase09.py:229) is vacuous: `legacy_blocked` is never assigned, so the expression reduces to `True`. The suite also lacks an adversarial test that `authorize_runtime_publish.py` rejects a policy with RT-101 removed, and it tests RT-101 proof expiry/future dates but not the equivalent external-satisfaction cases. Remove the dead assertion and add direct policy-strip and external-proof temporal adversarial tests.

8. **LOW — Area 2** — [scripts/validate_phase09_evidence_chain.py:280](/home/rhett/tech-db-phase09-repair/scripts/validate_phase09_evidence_chain.py:280) records nothing for a missing `ci_generated` chain entry unless `--strict-machine` happens to provide another C4 failure. In non-strict mode this is intentional, but the C10 wording overstates missing-artifact coverage. Distinguish “allowed absent in checkout mode” from “recorded and missing,” and fail in strict mode even if the entry is only referenced by `evidence_chain`.

9. **LOW — Area 3** — [qa-backend/phase09_authority.py:297](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_authority.py:297) rejects forbidden satisfaction keys recursively, but its allowlist is implemented as required-field checks rather than an exact requirement-object schema. An unusual non-satisfaction field can survive; that does not flip authority, but weakens policy canonicalization. Use an exact top-level field allowlist plus recursive forbidden-key scanning.

10. **LOW — Area 3** — [scripts/verify_phase09_expected_block.py:44](/home/rhett/tech-db-phase09-repair/scripts/verify_phase09_expected_block.py:44) excludes all `required authority undeclared in policy:` reasons from `other_reasons`. A policy-strip tamper can therefore be classified as an intentional authority absence. Treat undeclared mandatory authorities as unexpected structural failures rather than diagnostic authority absence.

11. **LOW — Area 1** — [qa-backend/phase09_authority.py:419](/home/rhett/tech-db-phase09-repair/qa-backend/phase09_authority.py:419) has a dead `if payload is None: continue` inside the per-control loop; payload validity is already handled before iteration. Remove the dead branch.

**Area 1:** HIGH present in finding 1.  
**Area 2:** no CRITICAL/HIGH; MEDIUM consistency gaps remain.  
**Area 3:** no CRITICAL/HIGH; the gate’s mandatory evaluator and proof checks fail closed for absent, malformed, weakly keyed, replayed, expired, future-dated, or wrong-trust proofs.  
**Area 4:** no CRITICAL/HIGH. Sanitized results omit proof payloads, keys, gold, and expected lock values; failures use field-level static reasons; workflow uploads only sanitized evidence and masks secrets.  
**Area 5:** no CRITICAL/HIGH beyond the MEDIUM coverage weakness in finding 7; production providers read only owner environment inputs, while tests use a local synthetic seam.  
**Area 6:** no CRITICAL/HIGH. Both workflows wire all five secrets, run fresh verification before authorization or side effects, do not use `continue-on-error`, and the upload step is sanitized and `if: always()` only.

VERDICT: P0_NOT_CLOSED because the standalone publication authorization path can be made to authorize with no RT-101 requirement or genuine owner secret by a repo policy edit.  
VERDICT: P1_NOT_CLOSED because the chain has a real generator and validator, but the validator still accepts unresolvable base SHAs, reason erasure, future generation timestamps, and asymmetric blocker divergence.
<!-- codex output end (verbatim) -->
