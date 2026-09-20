# RT-115 PREP — Migration / Rollback / Operator Runbook
**STATUS: PREPARED — needs owner review + live drill before any DOD claim.**
**Not executable end-to-end until Phase10 gate opens (NEXT_PROMPT_ALLOWED).**

## 1. Release identity chain (what binds what)
```
git HEAD (qa-backend/) ──► deploy-sync pin (18 files)
                          ├─► vector/BM25 index dir (runtime/indexes)
                          ├─► corpus (data/processed/all-records-lite.json)
                          ├─► model (bge-m3, versioned asset)
                          └─► config (expected head + runtime endpoint)
```
Verify any running server: `python3 scripts/verify_runtime_deploy_sync.py
--endpoint http://127.0.0.1:8768 --require-mirror --pin <pin.json>` → exit 0
means GIT+CODE+CORPUS+MODEL+CONFIG all bound to one head.

## 2. Profile activation (Phase10, gated)
- Canary plan → `phase10/canary_controller.CanaryPlan(profile_name, flags)`
- Assignment is sticky by request key; promotion strictly
  1→5→25→50→100 % with `MIN_STAGE_HOURS`/`MIN_STAGE_SAMPLES`/stratum gates.
- **No arbitrary flag mixtures**: `validate_flag_mixture` rejects anything
  but the profile's exact flag set.

## 3. Rollback SOP (RT-112)
1. Confirm a hard trigger (verifier_false_pass / invalid_citation_response /
   state_leakage / manifest_corruption) or a baseline-relative breach.
2. `RollbackController.hard_trigger(trigger, attribution)` computes the
   atomic target: **previous profile + its manifest_id + identity_snapshot_id**.
3. Apply in ONE transaction: point activation at the restored profile,
   restart pinned runtime, re-run deploy-sync verify.
4. Frontend caches are NOT rolled blindly: a rollback without matching
   `manifest_id` attribution is rejected (PAUSE_INVESTIGATE).
5. Record the RollbackAction dict in the incident log (append-only).

## 4. Record/source/identity schema migration pointers
- Stable ids: `qa-backend/record_registry.py` (RecordRegistry, source-keyed,
  idempotent, order-invariant)
- Migration map: `qa-backend/index_build_view.py` (fail-closed without map)
- Snapshots: immutable SourceSnapshot store (RT-012)
- Entity identity: RT-060..070 IdentityStore (transactional, override store)

## 5. Replay fidelity (RT-092 vocabulary)
`exact` = byte-identical retrieval+generation inputs under the same pinned
manifest; `stage` = same evidence, regenerated answer; `loose` = legacy
compat. Any replay artifact must name its mode + pin hashes.

## 6. Incident quick reference
| Symptom | First action | Then |
|---|---|---|
| verifier FAILED spike | pause profile (baseline_relative) | triage traces |
| citation schema invalid | hard_trigger invalid_citation_response | rollback SOP |
| index manifest mismatch | stop activation | deploy-sync re-verify |
| server NameError on globals | check RT-030 accessor pattern | restart pinned |

## 7. DRY-RUN CHECKLIST (run when gate opens)
- [ ] simulate hard trigger in staging → assert atomic restore
- [ ] stale-cache rollback rejection drill
- [ ] deploy-sync verify after each step
- [ ] canary 1%→5% simulation with fabricated windows (controller only)
- [ ] rollback timing measured and recorded
