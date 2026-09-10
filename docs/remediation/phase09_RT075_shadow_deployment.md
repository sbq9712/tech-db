# RT-075 shadow deployment — the ONE owner action

Status: **BLOCKED on repository-owner external action.** No agent may
start the RT-075 timer, and no agent may touch the owner's running
services (`qa-backend` :8765, frontend :8097, cloudflared). This page
compresses the owner-side deployment to a single action.

## Why the timer has not started

- The running production server was launched WITHOUT shadow env
  (`TECH_DB_ENTITY_QUERY_SHADOW` unset) and runs `TECH_DB_RUNTIME_MODE=legacy_hybrid`.
- Server logs show **zero** real `POST /api/chat` requests so far — there is
  no production traffic to observe. A collector cannot manufacture
  qualifying events; representative REAL events must come from real usage.
- The RT-075 gate needs **>= 1,000 unique representative events over a
  >= 7-day window** from `origin="real"` observations (verified from
  persisted evidence, not memory). Nothing in this repository can fake,
  replay, or synthesize that (stores are origin-bound and mixing is
  refused; duplication is deduped; the verifier computes the window from
  persisted timestamps).

## The ONE owner action

Set two environment variables on the production backend and restart it
once:

```bash
export TECH_DB_ENTITY_QUERY_SHADOW=1                       # enables the shadow monitor (RT-075)
export TECH_DB_ENTITY_SHADOW_STORE=/var/lib/tech-db/shadow/rt075.jsonl   # enables durable collection
# optional: TECH_DB_ENTITY_SHADOW_ORIGIN=real  (default; replay|synthetic only for explicitly approved lab runs)
```

Then simply USE the system — every `/api/chat` query appends one
tamper-evident, redacted observation (no query text, no entity names;
allowlisted numeric/decision fields only). The collector is strictly
best-effort: any storage failure is swallowed and never affects serving.

The restart is the owner's call and the owner's action; agents will not
restart or modify the running service.

## Periodic verification (any machine that can read the store)

```bash
python scripts/verify_rt075_shadow_qualification.py \
  --store /var/lib/tech-db/shadow/rt075.jsonl --out rt075-verdict.json
# exit 0 + "qualifies_as_production_shadow": true  => RT-075 evidence ready
# exit 1 => keep RT-075 BLOCKED_EXTERNAL_ACTION (timer still running or insufficient)
```

The verdict is machine-readable (`rt075-shadow-qualification-1.0`) and
reports: unique event count, window in days (from persisted timestamps),
origin purity, schema validity, chain integrity, and tamper findings.

## Guarantees

- append-only + per-record fsync: crash-safe; a torn final line is
  detected and skipped, never healed silently
- hash chain (`prev_line_sha256`): post-hoc edits are detected
  (injected mismatch fails closed)
- content-keyed dedupe: re-delivered/replayed identical events are
  rejected, so counts cannot be inflated
- origin purity: `real` / `replay` / `synthetic` can never mix in one
  store; only `origin="real"` can ever qualify as production shadow
- shadow non-interference: the monitor and store have no store, graph,
  answer, or serving-decision mutation capability; persistence is opt-in

## Trust boundary (no overclaim)

The store's guarantees are STRUCTURAL, not authenticated: `origin` is
asserted by the environment that writes the store, and anyone with
filesystem write access to the store path can fabricate a chain-valid
file. The verifier therefore proves integrity and internal consistency
only — it does not by itself authenticate production origin. RT-075
qualification evidence must come from a store on owner-controlled
storage, operated by the owner's own deployment (the ONE action above),
and activation authority still flows through the existing
owner-provisioned external satisfaction/HMAC path. Repo-side actors can
no more clear RT-075 with a self-written store than they can clear any
other external control: the satisfaction proof must be owner-provisioned.

---

# Q-336 durable retention — owner path (companion)

Q-336 (artifact retention >=180d vs GitHub public-repo 90d cap) has the
same shape: code-side tooling is ready, the external store is not.

```bash
# 1. (agent-runnable, after any CI/evidence build) export the bundle:
python scripts/export_phase09_retention_bundle.py \
  --root . --out /path/phase09-retention.tar.gz

# 2. (OWNER) upload the bundle to a durable store (>=180 days) and save
#    the store's retention receipt JSON:
#    {"schema_version":"q336-retention-receipt-1.0","retention_days":N>=180,
#     "bundle_sha256":"...","stored_at_utc":"...","store_id":"...",
#     "store_uri":"...","issuer":"..."}

# 3. verify the receipt (any machine that can read the files):
python scripts/verify_phase09_retention_bundle.py \
  --bundle /path/phase09-retention.tar.gz \
  --manifest /path/phase09-retention.tar.gz.manifest.json \
  --receipt /path/receipt.json --out /path/verified-receipt.json
# stderr prints artifact_sha256 = sha256(verified-receipt.json bytes)

# 4. (OWNER, admin) provision PHASE09_EXTERNAL_SATISFACTION_PROOFS /
#    PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY declaring artifact_sha256
#    = that digest with decision SATISFIED for Q-336, and set the
#    Q-336 row satisfied=true with satisfaction_proof {artifact, sha256}
#    pointing at the verified-receipt file.
```

No step here lowers the 180-day threshold; step 2 is owner-only.
