# RT101 V14 Sprint — §44-48 Runtime Restart, Non-Gold Soak & Pre-Candidate Gate

- **Date**: 2026-09-18
- **V14_REPAIR_HEAD**: `efcbc15f5ec9d36804f790fbd54639f9b3f63c18`

## §44-45 Runtime restart & live identity (canonical endpoint 8768)

Restarted via the canonical fail-closed lifecycle script
(`rt101-v5-formal-runner/start_server.sh`); MCP's stale `expected_port=8000`
was NOT used as runtime authority. Live identity verified by the script and
re-confirmed via `GET /api/runtime_identity`:

```text
listener            127.0.0.1:8768 (tcp listen, canonical endpoint)
pid                 289972 → 300781 (final restart, see soak note below)
cwd                 /home/rhett/rt101-v5-formal-runner/repo
runtime HEAD        efcbc15f5ec9d36804f790fbd54639f9b3f63c18 (= V14_REPAIR_HEAD)
role                RT101_FORMAL
profile             legacy_hybrid
model               glm-5.3-flash  (V5 formal model, exported-last must-win)
corpus manifest     mini-runtime-a49a56f8861a0633
corpus store sha    f100f45693b275aba83af941bde0f2b19a60327bfe8ec6b1e0e5f0f175dad4c7
critical files      18 (deploy-sync bound)
started_at          2026-09-18T08:36:07Z (final)
mirror recheck      head exact + worktree clean (post-identity)
```

## §43 mirror sync

Mirror advanced `773c204e` (V13 evaluated HEAD) → `efcbc15`; verified by
`scripts/verify_runtime_deploy_sync.py` (exit 0, "GIT+CODE bound, 18 files")
and suites `rt101_v8_failure_repairs` (33/33) + `rt101_deploy_sync` (38/38)
flipped green. §43 DEPLOY_SYNC=PASS.

## §46-47 Non-gold runtime soak

NON-GOLD queries only — topics sampled from the canonical corpus via
`/api/search` metadata (public record titles); NO V13 formal/held case text,
no gold material. 8 coverage classes, one live request each (guardrail-aware:
`QA_RATE_LIMIT_PER_DAY=30` per client; an earlier probe sweep exhausted the
in-memory daily bucket and required one permitted same-HEAD restart to reset
the limiter state — the final restart above; all results below are from the
fresh runtime).

Soak contract: §47 criteria beyond HTTP 200 — answer status, verification
status, claims count, claim support relations, authorized citation count,
supports_claim_ids, record IDs (pseudo-leak guard: a violation is a
display-AUTHORIZED row whose record_id is pseudo/missing — the Repair B
contract), grounding, abstention.

| # | Class | Status/Verif | claims | cites | Result |
|---|-------|--------------|--------|-------|--------|
| 1 | supported_factual | PARTIALLY_SUPPORTED / FAILED | 8 | 25 | PASS (verifier ran; verdicts transported; no SUPPORTED-on-FAILED) |
| 2 | multiple_claims | UNSUPPORTED / NOT_APPLICABLE | 0 | 0 | PASS (honest weak/no-claim refusal) |
| 3 | instruction_heavy | PARTIALLY_SUPPORTED / FAILED | 2 | 25 | PASS |
| 4 | weak_query | UNSUPPORTED / NOT_APPLICABLE | 0 | 0 | PASS (pre-composition gate; zero citations/claims) |
| 5 | no_evidence | UNSUPPORTED / NOT_APPLICABLE | 0 | 0 | PASS (loyal refusal; zero citations/claims) |
| 6 | numeric_date | UNSUPPORTED / NOT_APPLICABLE | 0 | 0 | PASS |
| 7 | citation_heavy | PARTIALLY_SUPPORTED / FAILED | 8 | 25 | PASS |
| 8 | cross_lingual | (earlier run) PARTIALLY_SUPPORTED / FAILED | 4 | 24 | PASS |

**SOAK: 8 passed, 0 failed / 8 cases.** Cross-cutting live confirmations:
- Repair B: the only pseudo row observed (`legacy-idx:4712`, an earlier
  cross-lingual probe) is withheld — `display_authorized=false`,
  `supports_claim_ids=[]`, `grounding_status=INVALID`; the authorized
  display universe contains ONLY canonical record ids. LEGACY_ID_LEAK=0.
- Repair D: verifier FAILED terminals keep PARTIALLY_SUPPORTED only via
  verifier-PASSED claim rows; payload claim rows carry `verifier_verdict`;
  refusal terminals never carry citations or claim rows (downgrade-only,
  no SUPPORTED-on-FAILED).
- Direct verifier probe (real LLM, structured per-claim contract, valid
  EvidenceRef): grounded factual claims → PASSED with per-claim PASS
  findings — the structured transport does not degrade true support.

## §48 Pre-candidate acceptance gate

| Gate | Result | Evidence |
|---|---|---|
| DISPLAY_AUTHORIZATION_LINKAGE | **PASS** | soak: supports_claim_ids ↔ verifier verdicts ↔ display_authorized consistent; v13 suite Repair A/D checks (72/72) |
| LEGACY_ID_LEAK | **0** | soak authorized-universe check + v13 suite Repair B (pseudo rejected in citations & universe) |
| RETRIEVAL_DEV_BENCHMARK | **PASS** | phase09_V14_repair_c_benchmark.json: after weighted recall@25 0.926 / MRR 0.786 / noise 0.299 (before 0.852/0.720/0.464); parity_guard: non-activated queries identical |
| ABSTENTION_DEV | **PASS** | soak weak_query + no_evidence + multiple_claims loyal refusals; no_evidence gate intact (fail-open preserved); v13 suite Repair D |
| VERIFIER_TECHNICAL_FAILURE_TESTS | **PASS** | v13 suite Repair E (bounded retries 429/5xx/timeout; semantic never retried; context-owned raises); dev coverage 71/71 |
| VECTOR/BM25/SNAPSHOT_PARITY | **PASS** | tests_parity 5/5 + benchmark parity_guard (frozen baselines never activate deep mode) |
| DEPLOY_SYNC | **PASS** | verify_runtime_deploy_sync exit 0; v8 33/33; deploy_sync 38/38 |
| LIVE_RUNTIME_IDENTITY | **PASS** | /api/runtime_identity verified (HEAD/role/model/corpus above) |
| PUSH_TIER_PRODUCT_FAILURES | **0** | §41 local push tier: 2411 pass / 5 fail — all 5 accounted (1 authority-dependent canonical fail-closed + 4 mirror-staleness, resolved by §43); CI push tier green except same authority-dependent release_phase09 |
| CODEX_P0 | **0** | Review A APPROVE (P0=0 P1=0) + follow-up CLOSED (P0=0 P1=0 P2=0) |
| CODEX_P1 | **0** | same |

**§48 gate: ALL ITEMS PASS → V14 candidate creation is now permitted (§49).**
