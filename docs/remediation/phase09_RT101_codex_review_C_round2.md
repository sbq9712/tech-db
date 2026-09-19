# RT-101 V6 Candidate — Codex Gatekeeper Review C, Round 2 (verbatim final verdict)

- Reviewer: codex exec (--sandbox read-only), serial run, stdin: /dev/null
- Date: 2026-09-14 (session)
- Scope: delta re-review after all round-1 P0/P1 fixes (runner rewrite, scorer
  hardening, exhaustive gold schema validation, full canonical pin enforcement,
  source-coverage gate, capture driver digest pin, exact membership counters).
- Candidate state at review: post-fix freeze (V6_SHA256
  100a83b7faf9bb2539cde5c465fca626b2af6ad1dae6a60fce39af0c8b42955b at freeze time,
  gold lock 034bd36b6c5c3f8ee0cec40cb99e7203a116f14746160406f68a07c1beec3d88)
- Verdict: REJECT — remaining 2 P0 (placeholder EXPECTED_HEAD; scorer live-store
  verification) + 1 P1 + 1 P2. All fixed before round 3.
- Blinding: confirmed by reviewer (no gold content opened; sha256 only).

---

- VERDICT: REJECT

- REMAINING P0
  - `/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:51`: `EXPECTED_HEAD="__FINAL_HEAD__"` violates P0-5. Move the current 40-hex `git rev-parse HEAD` into this constant before approval/freeze; do not leave a freeze-time substitution expectation unresolved.
  - `/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:128-130`: `EXPECTED_WORKING_EVIDENCE_SHA` is checked at line 128 before its definition at line 130. Because the script uses `set -u`, runtime expansion aborts before dry-run isolation; also, the working-evidence hash is verified after the candidate identity check even though it is an identity input. Fix: define the constant with the other expected constants before line 117, immediately after line 115 is acceptable, and perform the byte verification before candidate recomputation or at least before accepting identity.

- REMAINING P1
  - `/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:254-278`: P0-4 is only partially implemented. The scorer compares embedded canonical values to gold `corpus_pinning`, then separately checks `recmap.dataset_snapshot_id` against the lite bytes; it does not recompute or compare actual store/catalog bindings to `_CANON`. Add direct byte/schema validation for store SHA, catalog ID, record count, model/profile fields (or their available runtime equivalents), and explicit `identity_snapshot_id`/manifest validation before metrics.
  - `/home/rhett/tech-db-owner-secrets/rt101/RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:72`: the request still says proof rebind occurs “at FINAL HEAD” while the runner requires a concrete head pre-approval. The request wording itself has the correct V6 and lock SHAs; the surrounding post-approval workflow wording should be changed to the same concrete 40-hex head.

- P2 notes
  - P0-1: `bash -n` passes, and lines 299-300 invoke the pinned driver without an inline comment in the continuation.
  - P0-2: `rid2idx` is built from `recmap["mappings"]`, skips tombstones, and rejects duplicates (`score_v6.py:286-298`).
  - P0-3: exhaustive field/type validation and exact 11/2/2 mix are present before metrics (`score_v6.py:206-250`).
  - P0-6 / P1-7: dry-run marker exclusion (`run_v6_after_owner_approval.sh:274-281`) and formal-log isolation (`:323-329`) are correct, subject to reaching that code after the P0 ordering fix.
  - P1-1: candidate recomputation independently reproduces `d0a5752d2070d37760392b2ee6511e407c210eacf0bb6bdf0636743260ad1db2`; every listed input hash matches actual bytes.
  - P1-2: `CORPUS_ROOT` is absent, and non-`record:` must-cite paths call `die()` (`score_v6.py:565-579`).
  - P1-3: the module-level fixture comparison against `_LOCKED_THRESHOLDS` exits 2 on drift (`score_v6.py:78-92`).
  - P1-4: step `[2.6/9]` checks `generated_from` against the pin and calls `validate_source_coverage` fail-closed (`run_v6_after_owner_approval.sh:225-253`).
  - P1-5: driver SHA matches the specified value, and the runner uses the pinned `.venv/bin/python` interpreter (`run_v6_after_owner_approval.sh:295-300`).
  - P1-6: membership aggregation must equal exactly `13 13 2 2` (`run_v6_after_owner_approval.sh:134-140`).
  - P1-8: all three packages have gzip `mtime=0`, sorted members, tar `mtime=0`, and exclude `candidate_identity.json` from the owner bundle; the working package also excludes it.
  - P2: mutation records exclude all prior hidden record IDs (`build_v6.py:250-256`); snapshot diversity is asserted (`:528-534`); audit schema 1.1 separates allowed-infrastructure hits and requires `forbidden_material_hits` to be empty (`:640-663`).
  - Additional risk: the owner-side and working copies of `score_v6.py` have different SHA-256 values. Confirm which copy is canonical and pin it if that file is intended to be reproducible evidence.

- BLINDING: Confirmed. I opened no path under `/home/rhett/rt101-v6-builder-v6-cbbf2b382711/gold/`. The only gold operation was `sha256sum`, producing `034bd36b6c5c3f8ee0cec40cb99e7203a116f14746160406f68a07c1beec3d88`, exactly matching the stored lock.
tokens used
354,137
- VERDICT: REJECT

- REMAINING P0
  - `/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:51`: `EXPECTED_HEAD="__FINAL_HEAD__"` violates P0-5. Move the current 40-hex `git rev-parse HEAD` into this constant before approval/freeze; do not leave a freeze-time substitution expectation unresolved.
  - `/home/rhett/tech-db-owner-secrets/rt101/run_v6_after_owner_approval.sh:128-130`: `EXPECTED_WORKING_EVIDENCE_SHA` is checked at line 128 before its definition at line 130. Because the script uses `set -u`, runtime expansion aborts before dry-run isolation; also, the working-evidence hash is verified after the candidate identity check even though it is an identity input. Fix: define the constant with the other expected constants before line 117, immediately after line 115 is acceptable, and perform the byte verification before candidate recomputation or at least before accepting identity.

- REMAINING P1
  - `/home/rhett/tech-db-owner-secrets/rt101/score_v6.py:254-278`: P0-4 is only partially implemented. The scorer compares embedded canonical values to gold `corpus_pinning`, then separately checks `recmap.dataset_snapshot_id` against the lite bytes; it does not recompute or compare actual store/catalog bindings to `_CANON`. Add direct byte/schema validation for store SHA, catalog ID, record count, model/profile fields (or their available runtime equivalents), and explicit `identity_snapshot_id`/manifest validation before metrics.
  - `/home/rhett/tech-db-owner-secrets/rt101/RT101_V6_CANDIDATE_APPROVAL_REQUEST.json:72`: the request still says proof rebind occurs “at FINAL HEAD” while the runner requires a concrete head pre-approval. The request wording itself has the correct V6 and lock SHAs; the surrounding post-approval workflow wording should be changed to the same concrete 40-hex head.

- P2 notes
  - P0-1: `bash -n` passes, and lines 299-300 invoke the pinned driver without an inline comment in the continuation.
  - P0-2: `rid2idx` is built from `recmap["mappings"]`, skips tombstones, and rejects duplicates (`score_v6.py:286-298`).
  - P0-3: exhaustive field/type validation and exact 11/2/2 mix are present before metrics (`score_v6.py:206-250`).
  - P0-6 / P1-7: dry-run marker exclusion (`run_v6_after_owner_approval.sh:274-281`) and formal-log isolation (`:323-329`) are correct, subject to reaching that code after the P0 ordering fix.
  - P1-1: candidate recomputation independently reproduces `d0a5752d2070d37760392b2ee6511e407c210eacf0bb6bdf0636743260ad1db2`; every listed input hash matches actual bytes.
  - P1-2: `CORPUS_ROOT` is absent, and non-`record:` must-cite paths call `die()` (`score_v6.py:565-579`).
  - P1-3: the module-level fixture comparison against `_LOCKED_THRESHOLDS` exits 2 on drift (`score_v6.py:78-92`).
  - P1-4: step `[2.6/9]` checks `generated_from` against the pin and calls `validate_source_coverage` fail-closed (`run_v6_after_owner_approval.sh:225-253`).
  - P1-5: driver SHA matches the specified value, and the runner uses the pinned `.venv/bin/python` interpreter (`run_v6_after_owner_approval.sh:295-300`).
  - P1-6: membership aggregation must equal exactly `13 13 2 2` (`run_v6_after_owner_approval.sh:134-140`).
  - P1-8: all three packages have gzip `mtime=0`, sorted members, tar `mtime=0`, and exclude `candidate_identity.json` from the owner bundle; the working package also excludes it.
  - P2: mutation records exclude all prior hidden record IDs (`build_v6.py:250-256`); snapshot diversity is asserted (`:528-534`); audit schema 1.1 separates allowed-infrastructure hits and requires `forbidden_material_hits` to be empty (`:640-663`).
  - Additional risk: the owner-side and working copies of `score_v6.py` have different SHA-256 values. Confirm which copy is canonical and pin it if that file is intended to be reproducible evidence.

- BLINDING: Confirmed. I opened no path under `/home/rhett/rt101-v6-builder-v6-cbbf2b382711/gold/`. The only gold operation was `sha256sum`, producing `034bd36b6c5c3f8ee0cec40cb99e7203a116f14746160406f68a07c1beec3d88`, exactly matching the stored lock.
