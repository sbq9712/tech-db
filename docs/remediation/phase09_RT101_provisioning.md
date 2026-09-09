# RT-101 Provisioning Contract — genuine answer-level blinded release-holdout authority

Status: **BLOCKED on repository-owner external action.** No agent may
execute this provisioning. The gold must never enter the repository, PRs,
logs, traces, test fixtures, or generated public artifacts.

## 1. What the repository owner must provide

| Item | Channel (GitHub) | Secret/Variable name |
|---|---|---|
| Canonical authority proof (JSON, HMAC-signed) | Actions **secret** | `PHASE09_RT101_AUTHORITY_PROOF` |
| Expected holdout lock digest (sha256 of the gold file, hex) | Actions **secret** | `PHASE09_RT101_EXPECTED_HOLDOUT_LOCK_SHA256` |
| HMAC key (>= 32 random bytes) | Actions **secret** | `PHASE09_RT101_AUTHORITY_HMAC_KEY` |
| (later, per control) External-control satisfactions | Actions **secret** | `PHASE09_EXTERNAL_SATISFACTION_PROOFS` + `PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY` |

All three RT-101 inputs MUST come from the owner's secure admin channel
(GitHub repository secrets via Settings UI/API with admin credentials, or
an equivalent owner-only secret manager wired into the same environment
variables). Anything committable (files, PRs, workflow YAML) is
repo-controlled and can never provision authority.

## 2. Gold placement (never in repo)

- Generate the answer-level blinded release-holdout gold **outside** the
  repository (owner workstation / owner-controlled secure storage).
- The gold file (answers, citations, mutation labels, abstention labels)
  lives only in owner-controlled storage with restricted access.
- Compute `holdout_lock_sha256 = sha256(gold_file_bytes)` and provision it
  as the expected-digest secret. **Only the digest enters the provisioning
  channel — never the gold.**

## 3. Proof schema (`phase09-authority-proof-1.0`)

Canonical JSON, exactly these fields, MAC = HMAC-SHA256 over
`canonical_json(proof_without_integrity)` with the HMAC key:

```json
{
  "schema_version": "phase09-authority-proof-1.0",
  "proof_type": "RT101_RELEASE_HOLDOUT_AUTHORITY",
  "authority_id": "RT-101_answer_level_blinded_release_holdout_gold",
  "trust_class": "OWNER_PROVISIONED_EXTERNAL",
  "holdout_lock_sha256": "<64 hex>",
  "evaluated_git_sha": "<40 hex = exact commit the authority is issued for>",
  "spec_sha256": "<from spec/spec_manifest.json at that commit>",
  "decision_register_sha256": "<from spec/spec_manifest.json at that commit>",
  "manifest_id": "<qa-backend/test_fixtures/phase09/benchmark_locked_v1.json manifest_id>",
  "identity_snapshot_id": "<same fixture identity_snapshot_id>",
  "run_id": "<owner-chosen evaluation id>",
  "generated_at": "<ISO-8601 tz-aware UTC, not before the commit time, not older than 180 days>",
  "provenance": {"issued_by": "...", "channel": "...", "...": "..."},
  "integrity": {"alg": "HMAC-SHA256", "mac": "<64 hex>"}
}
```

Reference generator (run on the owner's machine, key never shared):

```bash
python3 - <<'PY'
import hashlib, hmac, json, sys
key = open("hmac_key.txt","rb").read()          # >= 32 bytes, keep secret
gold = open(sys.argv[1], "rb").read()           # blinded gold, never uploaded
spec = json.load(open("spec/spec_manifest.json"))
fix  = json.load(open("qa-backend/test_fixtures/phase09/benchmark_locked_v1.json"))
head = sys.argv[2]
proof = {
  "schema_version": "phase09-authority-proof-1.0",
  "proof_type": "RT101_RELEASE_HOLDOUT_AUTHORITY",
  "authority_id": "RT-101_answer_level_blinded_release_holdout_gold",
  "trust_class": "OWNER_PROVISIONED_EXTERNAL",
  "holdout_lock_sha256": hashlib.sha256(gold).hexdigest(),
  "evaluated_git_sha": head,
  "spec_sha256": spec["spec_sha256"],
  "decision_register_sha256": spec["decision_register_sha256"],
  "manifest_id": fix["manifest_id"],
  "identity_snapshot_id": fix["identity_snapshot_id"],
  "run_id": sys.argv[3],
  "generated_at": __import__("datetime").datetime.now(
      __import__("datetime").timezone.utc).isoformat(),
  "provenance": {"issued_by": "repository-owner",
                 "channel": "github-actions-secret"},
}
mac = hmac.new(key, json.dumps(proof, sort_keys=True,
               separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
proof["integrity"] = {"alg": "HMAC-SHA256", "mac": mac}
print(json.dumps(proof, sort_keys=True, separators=(",", ":")))
PY
```

## 4. What must never enter the repo / channels

Answer-level blinded gold labels, holdout answers, mutation labels, any
secret eval material, the HMAC key, provider/API secrets. Not in git, PR
bodies, ordinary logs, traces, test fixtures, or public artifacts.

## 5. Why repo edits can never fake SATISFIED

- `spec/phase09_release_policy.json` may only *declare* the requirement;
  its schema rejects satisfaction-claiming fields, and the evaluator
  rejects legacy status maps outright (`validate_authority_requirements`).
- `RT-101_answer_level_blinded_release_holdout_gold` is in the evaluator's
  code-level `MANDATORY_AUTHORITIES` set — removing the policy entry fails
  closed instead of clearing the gate.
- Satisfaction requires `verify_rt101_proof` to pass: exact HMAC over the
  canonical payload with the owner key (>= 32 bytes, constant-time
  compare), exact field allowlist, trust class exactly
  `OWNER_PROVISIONED_EXTERNAL` (TEST_ONLY/synthetic/dev/CI-replay
  rejected), bindings to HEAD, spec digests, locked manifest identity,
  holdout lock digest, and commit-time ordering plus a 180-day freshness
  window. Proof values arrive only via the environment channel; no repo
  file is ever consulted as proof.
- Generator/mapper/verifier isolation: the pipeline code paths never read
  the gold (it exists nowhere they can access); they consume only
  evaluator outputs. The verifier (CI job) sees only the proof and digest,
  not the gold.

## 6. Revocation

Delete/rotate `PHASE09_RT101_AUTHORITY_HMAC_KEY` (and the proof secret).
All proofs immediately fail HMAC verification; the gate fails closed.

## 7. First verification command after provisioning

```bash
# on the exact commit bound by evaluated_git_sha, in CI or locally with
# the secrets exported into the environment:
python scripts/run_phase09_release_gate.py \
  --evidence-out qa-backend/phase09_artifacts/phase09_release_evidence.json \
  --status-out  qa-backend/phase09_artifacts/phase09_ticket_status.json
# expected: all 5 required suites PASS and core_eligible=true;
# then scripts/validate_phase09_evidence_chain.py --strict-machine
```

## 8. Formal V5 (fresh blinded run) may start only after

1. The gate reports `core_eligible=true` at the exact head (genuine
   authority), all suites PASS.
2. Provider readiness probe passes (see Phase09 completion report).
3. A fresh branch/CI run re-executes the full tier at that head.

The formal V5 run itself consumes the blinded opportunity — run it once,
owner-supervised. Until (1) is true, only readiness probes and dry-runs
are permitted (Fresh-V5 prohibition).
