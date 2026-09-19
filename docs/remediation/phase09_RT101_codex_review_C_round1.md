# RT-101 V6 Candidate — Codex Gatekeeper Review C, Round 1 (verbatim final verdict)

- Reviewer: codex exec (--sandbox read-only), serial run, stdin: /dev/null
- Date: 2026-09-14 (session)
- Scope: full RT101-V6 candidate + one-shot formal runner + V6 scorer review
  (builder workspace /home/rhett/rt101-v6-builder-v6-cbbf2b382711,
  owner secrets /home/rhett/tech-db-owner-secrets/rt101)
- Candidate state at review: pre-fix freeze (V6_SHA256 054bf25e24c50a3390633a5dd03bec88ba43891caee2187fe2fea64f69ac6b6b)
- Verdict: REJECT (6 P0 + 8 P1) — all findings fixed in-place before re-review;
  fixes are visible in freeze_v6.py (P1-8 packages/identity regeneration) and
  the repaired runner/scorer reviewed in rounds 2-3 below.
- Blinding: confirmed by reviewer (no gold content opened; sha256 only).

---

VERDICT: REJECT

**P0 Findings**

1. [run_v6_after_owner_approval.sh:220](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:220) is not a valid command invocation. The `#` before the line continuations comments out the escaped Python command, so a real run will skip capture, then fail at the missing `population.json` check after the one-shot marker has been consumed. Replace with a normal assignment/command sequence and remove the inline comment from the continuation:
   ```bash
   V5_FORMAL_CASES="$BLIND_CASES" \
     /home/rhett/rt101-v5-formal-runner/.venv/bin/python \
     /home/rhett/rt101-v5-formal-runner/capture/formal_runner_v5.py \
     "$STATE_DIR/capture" || die "capture driver failed" 2
   ```

2. [score_v6.py:221](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:221) builds `idx2rid`, then [score_v6.py:224](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:224) inverts it again. `citation_structural()` therefore receives the original legacy index rather than `len(record_id_map.mappings) - 1 - legacy_idx`. This can make structurally correct citations fail span and snapshot validation. All 13 currently selected record IDs happen to survive the inversion, but that is accidental and not a safe scorer property. Build one authoritative `rid2idx` map directly from `recmap["mappings"]`, honoring tombstones, and use it consistently.

3. [score_v6.py:202](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:202) validates only a subset of required gold fields. Missing `query`, `case_id`, `category`, `source_paths`, or malformed terminal-related gold fields can cause uncontrolled `KeyError`/`TypeError` exits and code paths such as [score_v6.py:410](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:410). Add exhaustive per-case schema/type validation before metrics; every missing or malformed field must call `die()` and exit 2.

4. [score_v6.py:211](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:211) checks only dataset, store, and manifest. It does not independently enforce the full canonical binding, including source catalog `91d8460a…`, identity snapshot `mini-identity-v1`, model, schema versions, and dataset record count. Enforce the complete `corpus_pinning.json` identity before parsing captures or scoring.

5. [run_v6_after_owner_approval.sh:51](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:51) still has `EXPECTED_HEAD="__FINAL_HEAD__"`, and the approval request likewise has `PENDING_FINAL_HEAD` at [RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:58](/home/rhett/tech-db-owner-secrets/rt101/RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:58). Fill both with the same final pushed 40-character commit SHA before any real run; also validate its syntax explicitly.

6. [run_v6_after_owner_approval.sh:207](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:207) creates the spent marker before entering the dry-run branch. A signal, terminal loss, crash, or permission error before [line 257](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:257) can leave the opportunity consumed even though dry-run promised restoration. For `V6_DRY_RUN=1`, do not create the marker at all; only real runs should create it after all gates pass.

**P1 Findings**

1. [run_v6_after_owner_approval.sh:85](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:85) merely reads the stored `V6_SHA256`; it does not recompute it. Recompute SHA-256 over canonical JSON of `identity_inputs`, require agreement with the stored and requested values, and cross-check every identity input digest against actual bytes.

2. [score_v6.py:76](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:76) retains a raw `sources` tree default and legacy file-containment branch at [score_v6.py:544](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:544). This is dead for the V6 record-bound gold, but the V5 root cause should not remain reachable. Remove raw-tree support or explicitly reject non-`record:` paths.

3. The scorer reads thresholds from the live repo fixture at [score_v6.py:78](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:78) without binding the scorer or thresholds by digest. Add an immutable expected threshold digest or freeze the approved threshold set in the candidate identity.

4. The runner calls corpus compatibility but never invokes the fail-closed source-coverage gate. Run and validate the aggregate source-coverage report before marker creation.

5. The runner does not pin or verify the V5-lineage capture driver, its Python environment, or its contract beyond population count. Bind its path, digest or exact runtime identity, and schema/version before formal use.

6. [membership_aggregate.json](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/membership_aggregate.json) reports 13 checked member cases, while the candidate charter has 11 ANSWER plus 2 mutation cases. The runner should enforce the exact expected 13/13 and 2/2 counters, not merely minimums of one.

7. Dry-run appends `DRY_RUN_NO_RESULT` to the formal decision chain at [run_v6_after_owner_approval.sh:248](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:248). Keep dry-run entirely outside the formal append-only decision log.

8. `build_v6.py` does not regenerate `working/candidate_identity.json` or package tarballs, so “rerunning the builder” reproduces gold/blind/manifest but not the complete candidate identity. Add a separate deterministic identity/package generation step or document and verify those artifacts independently.

9. [build_v6.py:78](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/build_v6.py:78) reads all snapshot rows without enforcing `CITATION_ELIGIBLE`, public `access_scope`, or the ingestion extractor/version expected by the canonical universe. Enforce those constraints while building the candidate pool.

**P2 Notes**

- The blind cases have exactly `case_id` and `query`; the manifest contains no answers, record IDs, or evidence. The blind tar contains only the two allowed `blind/` files.
- Candidate identity recomputes correctly to `054bf25e24c50a3390633a5dd03bec88ba43891caee2187fe2fea64f69ac6b6b`; the gold lock and actual gold bytes both hash to `d66d3040c3414cd9c3091ea6a9feba71977dc49dcbb2bad1ce37edc22f246221`. All blind, manifest, and package digests match.
- Selection has 13 distinct hidden-source record IDs and 11 distinct snapshot IDs. Zero hidden-source duplication is proven; one snapshot may legitimately contain multiple records, but this deserves an explicit builder assertion/comment.
- [contamination_self_audit.json](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/audits/contamination_self_audit.json) reports expected runtime-path marker hits. Restructure the audit to distinguish allowed evaluated-runtime infrastructure from forbidden V4/V5 material.
- [build_v6.py:250](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/build_v6.py:250) excludes only `dataset[0]`; select mutation records from the explicit set of already-selected answer records.
- Tar metadata is not canonicalized. This does not affect JSON byte determinism, but package regeneration should set normalized mode, ownership, and timestamps if the tarball itself is claimed reproducible.

BLINDING: Confirmed. I did not open, print, extract, or summarize any file under `/home/rhett/rt101-v6-builder-v6-cbbf2b382711/gold/`. I only computed the gold file’s SHA-256 and listed package entries without extracting contents.
tokens used
290,008
VERDICT: REJECT

**P0 Findings**

1. [run_v6_after_owner_approval.sh:220](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:220) is not a valid command invocation. The `#` before the line continuations comments out the escaped Python command, so a real run will skip capture, then fail at the missing `population.json` check after the one-shot marker has been consumed. Replace with a normal assignment/command sequence and remove the inline comment from the continuation:
   ```bash
   V5_FORMAL_CASES="$BLIND_CASES" \
     /home/rhett/rt101-v5-formal-runner/.venv/bin/python \
     /home/rhett/rt101-v5-formal-runner/capture/formal_runner_v5.py \
     "$STATE_DIR/capture" || die "capture driver failed" 2
   ```

2. [score_v6.py:221](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:221) builds `idx2rid`, then [score_v6.py:224](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:224) inverts it again. `citation_structural()` therefore receives the original legacy index rather than `len(record_id_map.mappings) - 1 - legacy_idx`. This can make structurally correct citations fail span and snapshot validation. All 13 currently selected record IDs happen to survive the inversion, but that is accidental and not a safe scorer property. Build one authoritative `rid2idx` map directly from `recmap["mappings"]`, honoring tombstones, and use it consistently.

3. [score_v6.py:202](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:202) validates only a subset of required gold fields. Missing `query`, `case_id`, `category`, `source_paths`, or malformed terminal-related gold fields can cause uncontrolled `KeyError`/`TypeError` exits and code paths such as [score_v6.py:410](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:410). Add exhaustive per-case schema/type validation before metrics; every missing or malformed field must call `die()` and exit 2.

4. [score_v6.py:211](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:211) checks only dataset, store, and manifest. It does not independently enforce the full canonical binding, including source catalog `91d8460a…`, identity snapshot `mini-identity-v1`, model, schema versions, and dataset record count. Enforce the complete `corpus_pinning.json` identity before parsing captures or scoring.

5. [run_v6_after_owner_approval.sh:51](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:51) still has `EXPECTED_HEAD="__FINAL_HEAD__"`, and the approval request likewise has `PENDING_FINAL_HEAD` at [RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:58](/home/rhett/tech-db-owner-secrets/rt101/RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:58). Fill both with the same final pushed 40-character commit SHA before any real run; also validate its syntax explicitly.

6. [run_v6_after_owner_approval.sh:207](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:207) creates the spent marker before entering the dry-run branch. A signal, terminal loss, crash, or permission error before [line 257](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:257) can leave the opportunity consumed even though dry-run promised restoration. For `V6_DRY_RUN=1`, do not create the marker at all; only real runs should create it after all gates pass.

**P1 Findings**

1. [run_v6_after_owner_approval.sh:85](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:85) merely reads the stored `V6_SHA256`; it does not recompute it. Recompute SHA-256 over canonical JSON of `identity_inputs`, require agreement with the stored and requested values, and cross-check every identity input digest against actual bytes.

2. [score_v6.py:76](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:76) retains a raw `sources` tree default and legacy file-containment branch at [score_v6.py:544](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:544). This is dead for the V6 record-bound gold, but the V5 root cause should not remain reachable. Remove raw-tree support or explicitly reject non-`record:` paths.

3. The scorer reads thresholds from the live repo fixture at [score_v6.py:78](/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:78) without binding the scorer or thresholds by digest. Add an immutable expected threshold digest or freeze the approved threshold set in the candidate identity.

4. The runner calls corpus compatibility but never invokes the fail-closed source-coverage gate. Run and validate the aggregate source-coverage report before marker creation.

5. The runner does not pin or verify the V5-lineage capture driver, its Python environment, or its contract beyond population count. Bind its path, digest or exact runtime identity, and schema/version before formal use.

6. [membership_aggregate.json](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/membership_aggregate.json) reports 13 checked member cases, while the candidate charter has 11 ANSWER plus 2 mutation cases. The runner should enforce the exact expected 13/13 and 2/2 counters, not merely minimums of one.

7. Dry-run appends `DRY_RUN_NO_RESULT` to the formal decision chain at [run_v6_after_owner_approval.sh:248](/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:248). Keep dry-run entirely outside the formal append-only decision log.

8. `build_v6.py` does not regenerate `working/candidate_identity.json` or package tarballs, so “rerunning the builder” reproduces gold/blind/manifest but not the complete candidate identity. Add a separate deterministic identity/package generation step or document and verify those artifacts independently.

9. [build_v6.py:78](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/build_v6.py:78) reads all snapshot rows without enforcing `CITATION_ELIGIBLE`, public `access_scope`, or the ingestion extractor/version expected by the canonical universe. Enforce those constraints while building the candidate pool.

**P2 Notes**

- The blind cases have exactly `case_id` and `query`; the manifest contains no answers, record IDs, or evidence. The blind tar contains only the two allowed `blind/` files.
- Candidate identity recomputes correctly to `054bf25e24c50a3390633a5dd03bec88ba43891caee2187fe2fea64f69ac6b6b`; the gold lock and actual gold bytes both hash to `d66d3040c3414cd9c3091ea6a9feba71977dc49dcbb2bad1ce37edc22f246221`. All blind, manifest, and package digests match.
- Selection has 13 distinct hidden-source record IDs and 11 distinct snapshot IDs. Zero hidden-source duplication is proven; one snapshot may legitimately contain multiple records, but this deserves an explicit builder assertion/comment.
- [contamination_self_audit.json](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/audits/contamination_self_audit.json) reports expected runtime-path marker hits. Restructure the audit to distinguish allowed evaluated-runtime infrastructure from forbidden V4/V5 material.
- [build_v6.py:250](/home/rhett/rt101-v6-builder-v6-cbbf2b382711/working/build_v6.py:250) excludes only `dataset[0]`; select mutation records from the explicit set of already-selected answer records.
- Tar metadata is not canonicalized. This does not affect JSON byte determinism, but package regeneration should set normalized mode, ownership, and timestamps if the tarball itself is claimed reproducible.

BLINDING: Confirmed. I did not open, print, extract, or summarize any file under `/home/rhett/rt101-v6-builder-v6-cbbf2b382711/gold/`. I only computed the gold file’s SHA-256 and listed package entries without extracting contents.
