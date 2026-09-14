# RT-101 — Runtime repair + fresh V6 blind-holdout candidate (owner approval boundary)

Session: phase09 remediation continuation (agent, owner-authorized round) —
2026-09-13/14. Branch `remediation/phase-09-benchmarks-ci-release-gates`, PR #10.

## 0. Standing constraints (binding for this whole document)

- No gold content access: V4/V5/V6 gold bodies, expected answers, hidden rubrics
  and per-case logs were never opened. Gold is handled exclusively as
  path + SHA-256 by the agent; content exists only inside the owner bundle.
- V5 is consumed and immutable: the failed formal run is never re-run, its
  candidate is superseded (`CONSUMED-FAILED`), and no V5 artifacts are reused.
- NO V6 formal execution this round: the one-shot runner
  (`run_v6_after_owner_approval.sh`) ships in dry-run form only
  (`V6_DRY_RUN=1` semantics; the spent-marker seal step is skipped in dry-run).
- Thresholds are never lowered; scoring locks are copied verbatim from the
  V5 lineage.
- The session stops exactly at the `RT101_V6_OWNER_APPROVAL_REQUIRED`
  boundary: `NEXT_PROMPT_ALLOWED=false`, Phase10 / RT110-116 / Graph remain
  NOT_ACTIVATED.

## 1. Starting state

RT-101 V5 formal blinded release-holdout FAILED and was adjudicated
(`phase09_RT101_corpus_adjudication.json`,
`ROOT_CAUSE_CLASS=B — RUNTIME_CORPUS_DEFECT` + `V5_BUILDER_SCOPE_DEFECT`):
the V5 builder pinned the RAW spider filesystem tree (1133 files,
471,874,635 bytes) as its holdout source universe while the runtime serves
the INGESTED citation-eligible corpus (30,391 records,
`dataset_snapshot_id sha256:950ff008…`, store `f100f456…`,
catalog `91d8460a…`, manifest `mini-runtime-a49a56f8861a0633`,
identity `mini-identity-v1`). Answer-required cases therefore demanded
evidence outside the product's citation-eligible universe — impossible in
principle — and no machine gate blocked the one-shot consumption.

## 2. Permanent repairs (landed commits)

| commit | content |
|---|---|
| `57e4f53` | RT101 corpus gates: corpus-compatibility preflight + source-coverage audit + contamination guards (fail-closed `evaluate` + `assert_formal_run_allowed` with exact-match bindings and membership aggregate counters) |
| `b8f56f9` | runtime repairs RD-1/RD-2/RD-3 from the adjudication: display-integrity citation filter, canonical abstention serialization, evidence-grounding hardening |
| `8752248` | RT101 DEVELOPMENT_ONLY coverage suite + verifier readiness probe (test-only) |
| `9af27cd` | codex review A1 P0 fixes — deep fail-closed validation |
| `7ad7b85` | codex review A2 P1 fix — legacy caller transient retry restored |
| `1c8acff` | codex review B P2 — provenance + zero-case floor |

## 3. Fresh V6 candidate — independent isolated builder

Workspace `/home/rhett/rt101-v6-builder-v6-cbbf2b382711` (mode 0700, outside
the repo). Builder role vs implementation role separation:

- **Universe pinning** (`working/corpus_pinning.json`): the builder pins the
  adjudicated CANONICAL universe only — 30,391-record ingested store,
  manifest `mini-runtime-a49a56f8861a0633`, identity `mini-identity-v1`,
  profile `legacy_hybrid` — and the scorer re-verifies the LIVE store bytes
  and record count against this pin before computing any metric.
- **Blind case construction**: 15 cases (11 ANSWER / 2 ABSTAIN /
  2 MUTATION_WITH_LOYAL_ANSWER) drawn from the pinned corpus with a
  fresh owner-side selection seed/salt-driven deterministic selection (the
  fresh seed/salt itself is owner-secret and intentionally NOT recorded in
  this repo; candidate identity remains fully verifiable via the committed
  digest set — `V6_SHA256`, `V6_LOCK_SHA256`, and the per-artifact SHA-256
  bindings — without any knowledge of the salt, which never leaves the
  owner-side builder workspace and is unavailable to the implementation
  role),
  distinctive key-term picker with ≤8-record discriminativeness filter,
  mutation pool excluding ALL already-used hidden record ids, and a
  snapshot-diversity assertion.
- **V6 citation binding**: `must_cite_paths` carry `record:<record_id>`
  tokens — satisfied iff a structurally-valid displayed citation resolves to
  exactly that record id in the live store. The V5 raw-file shingle
  containment path is removed (root cause made unreachable).
- **Gold enrichment**: scorer-mirror probes (identifiers, quoted strings,
  CJK/latin terms, (value,unit) Decimal-exact pairs, date tuples, clause
  facts) with a satisfiability self-check — every expected answer passes its
  own rubric against the pinned evidence text.
- **Byte determinism**: `freeze_v6.py` regenerates all three packages
  (blind handoff, owner gold bundle, working evidence) + candidate identity
  byte-identically (gzip mtime=0, sorted tar members, normalized TarInfo,
  no self-referencing digest — `candidate_identity.json` is not inside the
  bundle it digests).

### Candidate identity (final freeze)

```text
V6_SHA256     = 100a83b7faf9bb2539cde5c465fca626b2af6ad1dae6a60fce39af0c8b42955b
V6_LOCK_SHA256= 034bd36b6c5c3f8ee0cec40cb99e7203a116f14746160406f68a07c1beec3d88
blind cases   = 2b063ddaf1a38fee5b0c3ebb576da6b52aeddb0e81d6d4d6932e9a3bb4bcca6f
blind package = eb09d8a1cd6e237f0dfd81c2f887f31d9f504694c25bc1ed1b670b832605ff88
owner bundle  = 7a5dae3a49b8e6260d4a5516041df4e59965e158014c486a933d2bf35450dc96
working evid. = b9481b34f72b4dfa807269e9097538cfa353fae3bed1449ae200ac4783b56e06
scorer        = 8c8ef1d03eae32a83df7a01a36b636fe3c3c925b28be222abbf18de7307d02ad
```

`V6_SHA256 = sha256(canonical_json(identity_inputs))` recomputed from the
actual package bytes at every gate step of the runner.

## 4. Codex gatekeeper review C — 3 serial rounds

Serial `codex exec --sandbox read-only` reviews (stdin `/dev/null`), verbatim
final verdicts archived beside this file:

- Round 1 — **REJECT** (6 P0 + 8 P1 + P2 notes): all fixed in-place
  (`phase09_RT101_codex_review_C_round1.md`).
- Round 2 — **REJECT** (2 P0 + 1 P1 + 1 P2): all fixed in-place
  (`phase09_RT101_codex_review_C_round2.md`).
- Round 3 — **APPROVE**, REMAINING P0/P1: **None**
  (`phase09_RT101_codex_review_C_round3.md`).

Blinding was confirmed by the reviewer in all three rounds: no file under the
builder's `gold/` was opened; gold was referenced only as a path for byte-size
SHA-256 verification.

## 5. Owner-side deliverables (owner secrets, mode 0700)

- `RT101_V6_CANDIDATE_APPROVAL_REQUEST.json` — schema
  `rt101-v6-candidate-approval-request-1.0`; supersedes the V5 candidate
  (`CONSUMED-FAILED`); carries all candidate digests, case mix, membership
  aggregate (13 13 2 2), the structured 3-round gatekeeper record, target
  bindings (branch, PR #10, head binding with explicit re-pin note), and the
  exact approval wording.
- `run_v6_after_owner_approval.sh` — one-shot formal runner, dry-run only
  this round: approval gate → identity recomputation from bytes → head
  binding → corpus-compatibility gate → source-coverage gate → preflight →
  salt-leak grep → marker seal (real runs only) → pinned capture → scorer
  (digest-checked) → canonical scoring → hash-chained append-only decision
  log → provisioning only on PASS. `bash -n` clean; exit codes 0/1/2/3.

## 6. Boundary

`NEXT_PROMPT_ALLOWED=false`. The formal V6 run, owner-authority provisioning,
Phase10, RT110-116 and Graph activation are all gated behind explicit owner
approval of the candidate above. This is the correct fail-closed state, not a
defect.
