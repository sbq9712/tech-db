#!/usr/bin/env python3
"""RT-106..RT-108 fail-closed release evidence-derived status tests.

D7 (P0 closure): required authorities are verified owner-provisioned
results, never repo-editable status strings.  The tests build genuine-shaped
proofs through the SAME production verifier with a test-local HMAC key —
the hermetic synthetic external-authority seam — and prove every bypass,
tamper, replay, and impersonation path fails closed.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import phase09_authority as A
from phase09_release import (EXTERNAL_BLOCKERS, PHASE09_TICKETS,
                             SuiteEvidence, build_provenance,
                             derive_ticket_status, evaluate_release,
                             load_external_blockers)

FIXTURE = HERE / "test_fixtures/phase09/benchmark_locked_v1.json"
BENCHMARK = HERE / "benchmark_phase09_result.json"
PASSED = 0
FAILED = 0

# ---------------------------------------------------------------------------
# Hermetic synthetic external-authority seam (TEST-ONLY).
# The key and proofs below exist only inside this test process.  Production
# providers read the owner environment channel exclusively and never import
# anything from test files; TEST_ONLY-style trust classes are rejected.
# ---------------------------------------------------------------------------
TEST_HMAC_KEY = "phase09-d7-hermetic-test-key-0123456789abcdef"  # >= 32 bytes
TEST_HOLDOUT_LOCK = "e" * 64
NOW = datetime.now(timezone.utc)

REQUIREMENT = {
    A.RT101_AUTHORITY_ID: {
        "proof_type": A.RT101_PROOF_TYPE,
        "schema_version": A.PROOF_SCHEMA_VERSION,
        "trust_class_required": A.OWNER_TRUST_CLASS,
        "bindings": sorted(A.MANDATORY_BINDINGS_RT101),
        "max_age_days": 180,
        "expected_holdout_lock_env": A.ENV_RT101_EXPECTED_HOLDOUT_LOCK,
        "manifest_identity_source":
            "qa-backend/test_fixtures/phase09/benchmark_locked_v1.json",
    }
}


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


def spec_identity():
    spec = json.loads((ROOT / "spec/spec_manifest.json").read_text("utf-8"))
    fixture = json.loads(FIXTURE.read_text("utf-8"))
    return (spec["spec_sha256"], spec["decision_register_sha256"],
            fixture["manifest_id"], fixture["identity_snapshot_id"])


def head_sha():
    return A.git_head(ROOT)


def non_head_sha():
    """A real, resolvable 40-hex repo object sha that is never HEAD.

    Uses the root tree so the value exists even in a CI shallow clone
    (fetch-depth: 1) where HEAD~1 is unresolvable.
    """
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()


def commit_time(sha):
    return A.git_commit_time(ROOT, sha)


def build_test_proof(*, hmac_key=TEST_HMAC_KEY, holdout=TEST_HOLDOUT_LOCK,
                     sha=None, spec=None, manifest=None, authority_id=None,
                     trust_class=None, generated_at=None, schema=None,
                     run_id="test-run-1", tamper=None, drop=None,
                     add=None) -> dict:
    """Genuine-shaped owner-equivalent proof for hermetic tests only."""
    spec_sha, decision_sha, manifest_id, snapshot_id = spec or spec_identity()
    if manifest:
        manifest_id, snapshot_id = manifest
    proof = {
        "schema_version": schema or A.PROOF_SCHEMA_VERSION,
        "proof_type": A.RT101_PROOF_TYPE,
        "authority_id": authority_id or A.RT101_AUTHORITY_ID,
        "trust_class": trust_class or A.OWNER_TRUST_CLASS,
        "holdout_lock_sha256": holdout,
        "evaluated_git_sha": sha if sha is not None else head_sha(),
        "spec_sha256": spec_sha,
        "decision_register_sha256": decision_sha,
        "manifest_id": manifest_id,
        "identity_snapshot_id": snapshot_id,
        "run_id": run_id,
        "generated_at": (generated_at or NOW).isoformat()
                        if isinstance(generated_at, datetime) else
                        (generated_at or NOW.isoformat()),
        "provenance": {"issued_by": "phase09-hermetic-test",
                       "channel": "in-process synthetic provisioning"},
    }
    if add:
        proof.update(add)
    if drop:
        for key in drop:
            proof.pop(key, None)
    proof.pop("integrity", None)
    mac = hmac.new(hmac_key.encode("utf-8"), A.canonical_bytes(proof),
                   hashlib.sha256).hexdigest()
    proof["integrity"] = {"alg": "HMAC-SHA256", "mac": mac}
    if tamper:
        field, value = tamper
        proof[field] = value
    return proof


def verify_test_proof(proof, *, hmac_key=TEST_HMAC_KEY,
                      holdout=TEST_HOLDOUT_LOCK, sha=None) -> A.AuthorityResult:
    spec_sha, decision_sha, manifest_id, snapshot_id = spec_identity()
    sha = sha if sha is not None else head_sha()
    return A.verify_rt101_proof(
        proof, requirement=REQUIREMENT[A.RT101_AUTHORITY_ID],
        current_git_sha=sha, spec_sha256=spec_sha,
        decision_register_sha256=decision_sha, manifest_id=manifest_id,
        identity_snapshot_id=snapshot_id,
        expected_holdout_lock_sha256=holdout, hmac_key=hmac_key,
        now=NOW, commit_time=commit_time(sha))


def satisfied_result(**kw) -> A.AuthorityResult:
    result = verify_test_proof(build_test_proof(**kw))
    assert result.satisfied, result.reasons
    return result


def provenance():
    fixture = json.loads(FIXTURE.read_text("utf-8"))
    return build_provenance(
        root=ROOT, dataset=FIXTURE, manifest_id=fixture["manifest_id"],
        identity_snapshot_id=fixture["identity_snapshot_id"],
        model=fixture["model"],
        prompt_config={"document_worker": "phase04-worker-1.0"},
        runtime_config={"tier": "PR_DETERMINISTIC", "network": False})


def good_rows(prov):
    return [SuiteEvidence(name=name, result="PASS", artifact=f"{name}.json",
                          provenance=prov)
            for name in ("benchmark_phase09", "e2e_phase09",
                         "failure_injection_phase09", "release_phase09")]


def decide(rows, prov, invariants=None, authority_results=None,
           requirements=None):
    return evaluate_release(
        required_suites=[row.name for row in good_rows(prov)], evidence=rows,
        expected_provenance=prov,
        hard_invariants=invariants or {
            "invalid_displayed_citation_zero": True,
            "verifier_technical_error_never_pass": True,
        },
        graph_gain_conclusion="NO_GAIN",
        authority_requirements=(REQUIREMENT if requirements is None
                                else requirements),
        authority_results=authority_results,
        external_blockers=EXTERNAL_BLOCKERS)


def test_release_matrix():
    """D7 P0: the 16 adversarial authority cases + positive seam."""
    prov = provenance()

    # --- positive path (case 15): genuine-shaped TEST-ONLY seam ---
    good = decide(good_rows(prov), prov,
                  authority_results={A.RT101_AUTHORITY_ID: satisfied_result()})
    check("RT101 D7 positive seam: verified test authority passes core",
          good.core_eligible,
          str(good.reasons))
    check("RT101 D7 positive seam keeps external production block",
          not good.production_release_eligible
          and set(good.external_blockers) == {"Q-336", "RT-005", "RT-075"})
    check("RT101 D7 sanitized result carries no proof material",
          "integrity" not in A.AuthorityResult(
              A.RT101_AUTHORITY_ID, True, A.OWNER_TRUST_CLASS, "d", "a", ()
          ).to_sanitized_dict()
          and all(k not in satisfied_result().to_sanitized_dict()
                  for k in ("holdout_lock_sha256", "provenance", "run_id")))

    # --- case 16: canonical production path without genuine authority ---
    env = {k: "" for k in (A.ENV_RT101_PROOF, A.ENV_RT101_HMAC_KEY,
                           A.ENV_RT101_EXPECTED_HOLDOUT_LOCK)}
    no_authority = A.authority_results_from_env(REQUIREMENT, root=ROOT, env=env)
    blocked = decide(good_rows(prov), prov, authority_results=no_authority)
    check("RT101 D7 canonical path without authority blocks (fail closed)",
          not blocked.core_eligible and not blocked.production_release_eligible
          and any(A.RT101_AUTHORITY_ID in reason
                  for reason in blocked.reasons),
          str(blocked.reasons))

    # --- case 1: repo-editable policy SATISFIED cannot pass ---
    legacy_blocked = None
    try:
        decide(good_rows(prov), prov,
               requirements={A.RT101_AUTHORITY_ID: "SATISFIED"})
        legacy_rejected = False
    except ValueError:
        legacy_rejected = True
    check("RT101 D7 legacy policy SATISFIED string rejected",
          legacy_rejected)
    try:
        polluted = copy.deepcopy(REQUIREMENT)
        polluted[A.RT101_AUTHORITY_ID]["satisfied"] = True
        decide(good_rows(prov), prov, requirements=polluted)
        satisfaction_key_rejected = False
    except ValueError:
        satisfaction_key_rejected = True
    check("RT101 D7 policy satisfaction-claiming key rejected",
          satisfaction_key_rejected)
    # D7 review F7 fix: this must be a real assertion, not a vacuous one —
    # even if the legacy string were accepted by validate, decide() with an
    # env channel that has no authority must still refuse core eligibility.
    legacy_env = {k: "" for k in (A.ENV_RT101_PROOF, A.ENV_RT101_HMAC_KEY,
                                  A.ENV_RT101_EXPECTED_HOLDOUT_LOCK)}
    try:
        blocked = decide(good_rows(prov), prov,
                         requirements={A.RT101_AUTHORITY_ID: "SATISFIED"},
                         authority_results=A.authority_results_from_env(
                             REQUIREMENT, root=ROOT, env=legacy_env))
        legacy_blocks_core = not blocked.core_eligible
    except ValueError:
        legacy_blocks_core = True  # rejected outright — also acceptable
    check("RT101 D7 policy-declared SATISFIED still blocks core",
          legacy_blocks_core)

    # --- case 2: fabricated repo proof file never consulted ---
    with tempfile.TemporaryDirectory() as tmp:
        proof_file = Path(tmp) / "proof.json"
        proof_file.write_text(json.dumps(build_test_proof()), "utf-8")
        # provider reads env only; a repo/cwd proof file is invisible
        ignored = A.authority_results_from_env(
            REQUIREMENT, root=ROOT,
            env={k: "" for k in (A.ENV_RT101_PROOF, A.ENV_RT101_HMAC_KEY,
                                 A.ENV_RT101_EXPECTED_HOLDOUT_LOCK)})
        check("RT101 D7 repo proof file ignored by production provider",
              not ignored[A.RT101_AUTHORITY_ID].satisfied)
        os.chdir(tmp)
        try:
            still_ignored = A.authority_results_from_env(
                REQUIREMENT, root=ROOT,
                env={k: "" for k in (A.ENV_RT101_PROOF,
                                     A.ENV_RT101_HMAC_KEY,
                                     A.ENV_RT101_EXPECTED_HOLDOUT_LOCK)})
        finally:
            os.chdir(ROOT)
        check("RT101 D7 cwd proof file cannot leak into provider",
              not still_ignored[A.RT101_AUTHORITY_ID].satisfied)

    # --- case 3: missing proof ---
    missing = A.authority_results_from_env(
        REQUIREMENT, root=ROOT,
        env={A.ENV_RT101_PROOF: "", A.ENV_RT101_HMAC_KEY: "",
             A.ENV_RT101_EXPECTED_HOLDOUT_LOCK: ""})
    check("RT101 D7 missing proof blocks", not missing[A.RT101_AUTHORITY_ID].satisfied)

    # --- case 4: malformed proof ---
    malformed = verify_test_proof({"not": "a proof"})
    check("RT101 D7 malformed proof blocks", not malformed.satisfied)
    bad_json = A.authority_results_from_env(
        REQUIREMENT, root=ROOT,
        env={A.ENV_RT101_PROOF: "{not json", A.ENV_RT101_HMAC_KEY: TEST_HMAC_KEY,
             A.ENV_RT101_EXPECTED_HOLDOUT_LOCK: TEST_HOLDOUT_LOCK})
    check("RT101 D7 non-JSON env proof blocks",
          not bad_json[A.RT101_AUTHORITY_ID].satisfied)

    # --- case 5: stale proof (older than max_age_days) ---
    stale = verify_test_proof(
        build_test_proof(generated_at=NOW - timedelta(days=200)))
    check("RT101 D7 stale proof (beyond max_age_days) blocks",
          not stale.satisfied and any("max_age_days" in r for r in stale.reasons),
          str(stale.reasons))

    # --- case 6: wrong git SHA ---
    wrong_sha = verify_test_proof(build_test_proof(sha="1" * 40))
    check("RT101 D7 wrong git SHA blocks",
          not wrong_sha.satisfied and any("evaluated_git_sha" in r
                                          for r in wrong_sha.reasons))

    # --- case 7: wrong holdout lock ---
    wrong_lock = verify_test_proof(build_test_proof(holdout="f" * 64))
    check("RT101 D7 wrong holdout lock blocks",
          not wrong_lock.satisfied and any("holdout_lock" in r
                                           for r in wrong_lock.reasons))

    # --- case 8: wrong spec identity ---
    spec_sha, decision_sha, manifest_id, snapshot_id = spec_identity()
    wrong_spec = verify_test_proof(
        build_test_proof(spec=("0" * 64, decision_sha, manifest_id, snapshot_id)))
    check("RT101 D7 wrong spec sha256 blocks",
          not wrong_spec.satisfied and any("spec_sha256" in r
                                           for r in wrong_spec.reasons))
    wrong_decision = verify_test_proof(
        build_test_proof(spec=(spec_sha, "1" * 64, manifest_id, snapshot_id)))
    check("RT101 D7 wrong decision-register sha blocks",
          not wrong_decision.satisfied)

    # --- case 9: wrong manifest identity ---
    wrong_manifest = verify_test_proof(
        build_test_proof(manifest=("wrong-manifest", snapshot_id)))
    check("RT101 D7 wrong manifest_id blocks",
          not wrong_manifest.satisfied and any("manifest_id" in r
                                               for r in wrong_manifest.reasons))
    wrong_snapshot = verify_test_proof(
        build_test_proof(manifest=(manifest_id, "wrong-snapshot")))
    check("RT101 D7 wrong identity_snapshot blocks",
          not wrong_snapshot.satisfied)

    # --- case 10: wrong authority id ---
    wrong_id = verify_test_proof(
        build_test_proof(authority_id="RT-999_something_else"))
    check("RT101 D7 wrong authority id blocks", not wrong_id.satisfied)

    # --- case 11: tampered proof / hash ---
    tampered = verify_test_proof(
        build_test_proof(tamper=("run_id", "evil-rewrite-after-signing")))
    check("RT101 D7 tampered proof (post-signing edit) blocks",
          not tampered.satisfied and any("mac" in r for r in tampered.reasons))
    bad_mac = build_test_proof()
    bad_mac["integrity"]["mac"] = "0" * 64
    check("RT101 D7 forged mac blocks",
          not verify_test_proof(bad_mac).satisfied)
    check("RT101 D7 weak HMAC key blocks",
          not verify_test_proof(build_test_proof(), hmac_key="short").satisfied)

    # --- case 12: dev/synthetic authority impersonation ---
    for cls in ("TEST_ONLY", "SYNTHETIC", "DEV_DATASET", "REPO_EDITABLE",
                "CI_REPLAY", "owner"):
        impersonator = verify_test_proof(build_test_proof(trust_class=cls))
        check(f"RT101 D7 trust class {cls} rejected",
              not impersonator.satisfied
              and any("trust_class" in r for r in impersonator.reasons))

    # --- case 13: replay old authority proof against new checkout ---
    # (any non-HEAD resolvable repo sha; must survive CI shallow clones)
    other_commit = non_head_sha()
    old_proof = build_test_proof(sha=other_commit)  # bound to another sha
    replay = verify_test_proof(old_proof)     # verified against HEAD
    check("RT101 D7 replayed proof from parent commit blocks",
          not replay.satisfied and any("evaluated_git_sha" in r
                                       for r in replay.reasons))

    # --- case 14: mismatched proof schema/version ---
    wrong_schema = verify_test_proof(build_test_proof(schema="phase09-authority-proof-0.9"))
    check("RT101 D7 mismatched proof schema blocks", not wrong_schema.satisfied)

    # --- undeclared mandatory authority (policy key removal attack) ---
    blocked_undeclared = decide(good_rows(prov), prov, requirements={})
    check("RT101 D7 removing policy requirement still blocks",
          not blocked_undeclared.core_eligible
          and any("undeclared in policy" in r for r in blocked_undeclared.reasons),
          str(blocked_undeclared.reasons))

    # --- external blocker cannot be cleared by repo self-hash proof ---
    state = json.loads((ROOT / "spec/phase09_external_state.json").read_text("utf-8"))
    optimistic = copy.deepcopy(state)
    optimistic["controls"]["RT-005"]["satisfied"] = True
    with tempfile.TemporaryDirectory() as tmp:
        decoy = Path(tmp) / "decoy-proof.bin"
        decoy.write_bytes(b"self-referential repo artifact")
        optimistic["controls"]["RT-005"]["satisfaction_proof"] = {
            "artifact": str(decoy),
            "sha256": hashlib.sha256(decoy.read_bytes()).hexdigest(),
        }
        candidate = Path(tmp) / "external.json"
        candidate.write_text(json.dumps(optimistic), "utf-8")
        try:
            load_external_blockers(candidate)
            cleared = True
        except ValueError:
            cleared = False
        check("RT108 D7 repo self-hash artifact cannot clear blocker "
              "without owner env proof", not cleared)
        # with a genuine owner-proof binding the SAME digest, clearing works
        owner_proof = A.verify_external_satisfaction_proof(
            build_external_proof(decoy), control_id="RT-005",
            current_git_sha=head_sha(), hmac_key=TEST_HMAC_KEY, now=NOW,
            commit_time=commit_time(head_sha()))
        try:
            load_external_blockers(candidate, owner_proofs={"RT-005": owner_proof})
            cleared_with_owner = True
        except ValueError:
            cleared_with_owner = False
        check("RT108 D7 genuine owner satisfaction proof clears blocker",
              cleared_with_owner)
        # D7 review F7: external satisfaction proofs honor the same
        # temporal discipline as RT-101 proofs — stale (expired) and
        # future-dated proof timestamps must be rejected.
        import datetime as _dt
        stale_proof = build_external_proof(decoy)
        stale_proof["generated_at"] = (
            NOW - _dt.timedelta(days=200)).isoformat()
        stale_proof.pop("integrity", None)
        stale_proof["integrity"] = {
            "alg": "HMAC-SHA256",
            "mac": hmac.new(TEST_HMAC_KEY.encode("utf-8"),
                            A.canonical_bytes(stale_proof),
                            hashlib.sha256).hexdigest(),
        }
        stale_result = A.verify_external_satisfaction_proof(
            stale_proof, control_id="RT-005", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW, commit_time=commit_time(head_sha()))
        check("RT108 D7 stale external satisfaction proof rejected",
              not stale_result.satisfied, str(stale_result.reasons))
        future_proof = build_external_proof(decoy)
        future_proof["generated_at"] = (
            NOW + _dt.timedelta(hours=1)).isoformat()
        future_proof.pop("integrity", None)
        future_proof["integrity"] = {
            "alg": "HMAC-SHA256",
            "mac": hmac.new(TEST_HMAC_KEY.encode("utf-8"),
                            A.canonical_bytes(future_proof),
                            hashlib.sha256).hexdigest(),
        }
        future_result = A.verify_external_satisfaction_proof(
            future_proof, control_id="RT-005", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW, commit_time=commit_time(head_sha()))
        check("RT108 D7 future-dated external satisfaction proof rejected",
              not future_result.satisfied, str(future_result.reasons))
        # D7 gatekeeper hardening: the publicly-committed hermetic test key
        # can never satisfy production providers, even with a cryptographically
        # valid proof — the production env channel must carry real secrets.
        prod_env = {
            A.ENV_RT101_PROOF: json.dumps(build_test_proof()),
            A.ENV_RT101_HMAC_KEY: TEST_HMAC_KEY,
            A.ENV_RT101_EXPECTED_HOLDOUT_LOCK: TEST_HOLDOUT_LOCK,
            A.ENV_EXTERNAL_PROOFS: json.dumps(
                {"RT-005": build_external_proof(decoy)}),
            A.ENV_EXTERNAL_HMAC_KEY: TEST_HMAC_KEY,
        }
        prod_rt101 = A.authority_results_from_env(
            REQUIREMENT, root=ROOT, env=prod_env)
        prod_ext = A.external_satisfaction_proofs_from_env(
            root=ROOT, env=prod_env)
        check("RT101 D7 production provider rejects publicly-known test key",
              not prod_rt101[A.RT101_AUTHORITY_ID].satisfied
              and any("publicly-known" in r for r in
                      prod_rt101[A.RT101_AUTHORITY_ID].reasons),
              str(prod_rt101[A.RT101_AUTHORITY_ID].reasons))
        check("RT108 D7 external provider rejects publicly-known test key",
              not prod_ext["RT-005"].satisfied,
              str(prod_ext["RT-005"].reasons))


def build_external_proof(artifact_path: Path) -> dict:
    proof = {
        "schema_version": A.PROOF_SCHEMA_VERSION,
        "proof_type": A.EXTERNAL_SATISFACTION_PROOF_TYPE,
        "authority_id": "RT-005",
        "trust_class": A.OWNER_TRUST_CLASS,
        "decision": "SATISFIED",
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "evaluated_git_sha": head_sha(),
        "run_id": "test-run-1",
        "generated_at": NOW.isoformat(),
        "provenance": {"issued_by": "phase09-hermetic-test"},
    }
    proof.pop("integrity", None)
    proof["integrity"] = {
        "alg": "HMAC-SHA256",
        "mac": hmac.new(TEST_HMAC_KEY.encode("utf-8"),
                        A.canonical_bytes(proof), hashlib.sha256).hexdigest(),
    }
    return proof


def test_authorization_requires_genuine_authority():
    """Publication path cannot be green via fabricated evidence files."""
    head = head_sha()
    base = {"provenance": {"git_sha": head},
            "core_eligible": True,
            "production_release_eligible": False,
            "external_blockers": ["Q-336"]}
    workflow = (ROOT / ".github/workflows/publish-runtime.yml").read_text("utf-8")
    gate_pos = workflow.index("Fresh Phase09 release evaluation")
    auth_pos = workflow.index("Authorize this exact publication SHA")
    side_effect_pos = workflow.index("Prepare current search indexes")
    check("RT106 publication workflow cannot bypass fresh gate",
          gate_pos < auth_pos < side_effect_pos
          and "continue-on-error" not in workflow[gate_pos:side_effect_pos])
    check("RT106 D7 publication workflow wires owner authority channel",
          "secrets.PHASE09_RT101_AUTHORITY_PROOF" in workflow
          and "secrets.PHASE09_RT101_AUTHORITY_HMAC_KEY" in workflow
          and "PHASE09_RT101_AUTHORITY_PROOF: ${{" in workflow)
    gates_wf = (ROOT / ".github/workflows/remediation-gates.yml").read_text("utf-8")
    check("RT106 D7 CI gate wires owner authority channel",
          "secrets.PHASE09_RT101_AUTHORITY_PROOF" in gates_wf)
    check("RT106 D7 import boundary: gate script never imports test modules",
          not any(marker in (ROOT / "scripts/run_phase09_release_gate.py").read_text("utf-8")
                  for marker in ("import tests_", "from tests_")))
    check("RT106 D7 import boundary: publish script never imports test modules",
          not any(marker in (ROOT / "scripts/authorize_runtime_publish.py").read_text("utf-8")
                  for marker in ("import tests_", "from tests_")))

    proof = build_test_proof()
    env_with_authority = {
        **os.environ,
        A.ENV_RT101_PROOF: json.dumps(proof),
        A.ENV_RT101_HMAC_KEY: TEST_HMAC_KEY,
        A.ENV_RT101_EXPECTED_HOLDOUT_LOCK: TEST_HOLDOUT_LOCK,
        A.ENV_EXTERNAL_PROOFS: "",
        A.ENV_EXTERNAL_HMAC_KEY: "",
    }
    env_without_authority = {
        **os.environ,
        A.ENV_RT101_PROOF: "",
        A.ENV_RT101_HMAC_KEY: "",
        A.ENV_RT101_EXPECTED_HOLDOUT_LOCK: "",
        A.ENV_EXTERNAL_PROOFS: "",
        A.ENV_EXTERNAL_HMAC_KEY: "",
    }

    def run_authorize(evidence_payload, env, policy_source=None):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_file = Path(tmp) / "evidence.json"
            evidence_file.write_text(json.dumps(evidence_payload), "utf-8")
            cmd = [sys.executable,
                   str(ROOT / "scripts/authorize_runtime_publish.py"),
                   "--evidence", str(evidence_file), "--expected-sha", head]
            if policy_source is not None:
                # D7 review F7 hardening: the strip attack is exercised on a
                # TEMP policy copy via --policy — the repo policy file is
                # never mutated, so a crash can never leave it stripped.
                policy_copy = Path(tmp) / "policy.json"
                policy_copy.write_text(
                    (ROOT / "spec/phase09_release_policy.json").read_text("utf-8")
                    if policy_source == "repo" else policy_source, "utf-8")
                cmd += ["--policy", str(policy_copy)]
            return subprocess.run(cmd,
                cwd=ROOT, capture_output=True, text=True, env=env)

    # blocked evidence denied
    denied = run_authorize(base, env_with_authority)
    check("RT106 publish path denies current blocked evidence",
          denied.returncode != 0 and "PUBLISH_DENIED" in denied.stdout,
          denied.stdout + denied.stderr)
    # fabricated green evidence WITHOUT genuine authority -> denied (D7)
    fabricated = copy.deepcopy(base)
    fabricated["production_release_eligible"] = True
    fabricated["external_blockers"] = []
    forged = run_authorize(fabricated, env_without_authority)
    check("RT106 D7 fabricated green evidence without authority cannot publish",
          forged.returncode != 0 and "PUBLISH_DENIED" in forged.stdout,
          forged.stdout + forged.stderr)
    # forged blockers-cleared evidence with external blocker still recorded in repo
    # (external controls remain unsatisfied in repo external-state; even with
    # authority env present, fresh blockers re-verification must deny)
    forged2 = run_authorize(fabricated, env_with_authority)
    check("RT106 D7 green evidence with unsatisfied external blockers denied",
          forged2.returncode != 0 and "PUBLISH_DENIED" in forged2.stdout,
          forged2.stdout + forged.stderr)

    # D7 review F1/F7: a repo commit stripping RT-101 from the policy's
    # required_authorities must NOT let the standalone publish path
    # authorize — the mandatory authority set is pinned in code.  The
    # stripped policy is a temp copy passed via --policy; the repo file is
    # never touched.
    repo_policy = (ROOT / "spec/phase09_release_policy.json").read_text("utf-8")
    stripped_policy = json.loads(repo_policy)
    stripped_policy.pop("required_authorities", None)
    stripped_run = run_authorize(fabricated, env_without_authority,
                                 policy_source=json.dumps(stripped_policy))
    check("RT106 D7 policy stripping RT-101 cannot authorize publish",
          stripped_run.returncode != 0 and "PUBLISH_DENIED" in stripped_run.stdout
          and "undeclared" in stripped_run.stdout,
          stripped_run.stdout + stripped_run.stderr)
    intact_run = run_authorize(fabricated, env_without_authority,
                               policy_source="repo")
    check("RT106 D7 intact temp policy still denies without authority",
          intact_run.returncode != 0 and "PUBLISH_DENIED" in intact_run.stdout,
          intact_run.stdout + intact_run.stderr)


def test_ticket_status_generation():
    matrix = json.loads((ROOT / "spec/acceptance_matrix.json").read_text("utf-8"))
    registered = {entry["ticket_id"] for entry in matrix["remediation_entries"]}
    if not set(PHASE09_TICKETS) <= registered:
        check("RT108 Phase09 entries registered", False,
              "acceptance matrix has not been extended")
        return
    suite_results = {
        "benchmark_phase09": "PASS", "e2e_phase09": "PASS",
        "failure_injection_phase09": "PASS", "release_phase09": "PASS",
    }
    if BENCHMARK.exists():
        artifact = json.loads(BENCHMARK.read_text("utf-8"))
    else:
        prov = provenance()
        artifact = {
            "schema_version": "phase09-benchmark-1.0", "verdict": "PASS",
            "provenance": prov,
            "metrics": {"standalone": {"value": 1, "threshold": 1,
                                       "direction": "gte", "passed": True}},
        }
    status = derive_ticket_status(
        matrix=matrix, suite_results=suite_results,
        artifact_results={"qa-backend/benchmark_phase09_result.json": artifact},
        external_blockers=EXTERNAL_BLOCKERS)
    check("RT108 status generated for every Phase09 ticket",
          set(status["tickets"]) == set(PHASE09_TICKETS))
    check("RT108 RT103 remains externally blocked",
          status["tickets"]["RT-103"]["status"] == "BLOCKED_EXTERNAL_ACTION"
          and status["tickets"]["RT-103"]["dependency_blockers"] == ["RT-075"])
    check("RT108 RT106 retention remains externally blocked",
          status["tickets"]["RT-106"]["status"] == "BLOCKED_EXTERNAL_ACTION"
          and status["tickets"]["RT-106"]["dependency_blockers"] == ["Q-336"])
    check("RT108 RT101 missing answer gold is not satisfied (authority hook)",
          status["tickets"]["RT-101"]["status"] == "NOT_SATISFIED"
          and any("authority" in reason
                  for reason in status["tickets"]["RT-101"]["reasons"]),
          json.dumps(status["tickets"]["RT-101"], ensure_ascii=False))
    # D7: even a repo-edited matrix claiming RT-101 SATISFIED stays NOT_SATISFIED
    optimistic_matrix = copy.deepcopy(matrix)
    for entry in optimistic_matrix["remediation_entries"]:
        if entry["ticket_id"] == "RT-101":
            for dod in entry["dods"]:
                if dod["status"] == "NOT_SATISFIED":
                    dod["status"] = "SATISFIED"
                    dod["test_cases"] = dod.get("planned_test_cases", []) or \
                        [{"suite": "e2e_phase09", "case": "d7-optimistic",
                          "command": "python qa-backend/tests_e2e_phase09.py"}]
    optimistic = derive_ticket_status(
        matrix=optimistic_matrix, suite_results=suite_results,
        artifact_results={"qa-backend/benchmark_phase09_result.json": artifact},
        external_blockers=EXTERNAL_BLOCKERS)
    check("RT108 D7 repo-edited matrix cannot satisfy RT-101",
          optimistic["tickets"]["RT-101"]["status"] == "NOT_SATISFIED"
          and optimistic["phase_status"] == "NOT_SATISFIED")
    check("RT108 other code-local tickets use executable evidence",
          all(row["status"] == "SATISFIED" for ticket, row in
              status["tickets"].items()
              if ticket not in {"RT-101", "RT-103", "RT-106"}),
          json.dumps(status["tickets"], ensure_ascii=False))
    missing = derive_ticket_status(
        matrix=matrix, suite_results={**suite_results, "benchmark_phase09": "MISSING"},
        artifact_results={}, external_blockers=EXTERNAL_BLOCKERS)
    check("RT108 missing suite/artifact removes completion",
          missing["tickets"]["RT-100"]["status"] == "NOT_SATISFIED"
          and missing["phase_status"] == "NOT_SATISFIED")


def _write_synthetic_chain(out: Path, head: str, now: str, blockers: list):
    """Create a minimal coherent upstream artifact set in a temp chain root."""
    qa = out / "qa-backend"
    qa.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": now, "git_sha": head, "worktree_dirty": False,
        "tier": "push", "all_passed": True, "total_passed": 14,
        "total_failed": 0,
        "suites": [
            {"tag": "benchmark_phase09", "file": "x.py", "status": "PASS",
             "passed": 3, "failed": 0, "exit_code": 0, "seconds": 0.1},
            {"tag": "e2e_phase09", "file": "x.py", "status": "PASS",
             "passed": 3, "failed": 0, "exit_code": 0, "seconds": 0.1},
            {"tag": "release_phase09", "file": "x.py", "status": "PASS",
             "passed": 4, "failed": 0, "exit_code": 0, "seconds": 0.1},
            {"tag": "failure_injection_phase09", "file": "x.py",
             "status": "PASS", "passed": 2, "failed": 0, "exit_code": 0,
             "seconds": 0.1},
            {"tag": "runtime_budget_repair", "file": "x.py",
             "status": "PASS", "passed": 2, "failed": 0, "exit_code": 0,
             "seconds": 0.1},
        ],
        "missing_suites": [],
    }
    (qa / "test_summary.json").write_text(json.dumps(summary), "utf-8")
    evidence = {
        "schema_version": "phase09-release-evidence-1.1",
        "generated_at": now,
        "core_eligible": False,
        "production_release_eligible": False,
        "graph_activation_eligible": False,
        "graph_state": "OFF_NO_GAIN",
        "reasons": [f"required authority unsatisfied: {A.RT101_AUTHORITY_ID}"],
        "external_blockers": blockers,
        "provenance": {"git_sha": head},
    }
    (qa / "release_evidence.json").write_text(json.dumps(evidence), "utf-8")
    ticket = {
        "schema_version": "phase09-ticket-status-1.0",
        "generated_at": now,
        "phase_status": "NOT_SATISFIED",
        "tickets": {},
        "external_blockers": {b: "x" for b in blockers},
        "release_decision": evidence,
    }
    (qa / "ticket_status.json").write_text(json.dumps(ticket), "utf-8")
    return qa / "test_summary.json", qa / "release_evidence.json", \
        qa / "ticket_status.json"


def _run_builder(chain_root: Path):
    s, e, t = (chain_root / "qa-backend" / name for name in
               ("test_summary.json", "release_evidence.json",
                "ticket_status.json"))
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_phase09_evidence.py"),
         "--summary", str(s), "--release-evidence", str(e),
         "--ticket-status", str(t),
         "--out-dir", str(chain_root / "docs" / "remediation")],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PHASE09_EVIDENCE_HERMETIC_TEST": "1"})


def _run_validator(chain_root: Path):
    return subprocess.run(
        [sys.executable,
         str(ROOT / "scripts/validate_phase09_evidence_chain.py"),
         "--chain-root", str(chain_root),
         "--release-evidence",
         str(chain_root / "qa-backend" / "release_evidence.json"),
         "--ticket-status",
         str(chain_root / "qa-backend" / "ticket_status.json")],
        cwd=ROOT, capture_output=True, text=True)


def test_evidence_chain_builder_and_validator():
    """P1: one-way chain derivation + validator green on a coherent chain."""
    head = head_sha()
    now = datetime.now(timezone.utc).isoformat()
    blockers = sorted(EXTERNAL_BLOCKERS)
    with tempfile.TemporaryDirectory() as tmp:
        chain = Path(tmp)
        _write_synthetic_chain(chain, head, now, blockers)
        built = _run_builder(chain)
        check("chain builder derives all artifacts from upstream set",
              built.returncode == 0
              and (chain / "docs/remediation/phase09_PHASE_RESULT.json").exists()
              and (chain / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json").exists()
              and (chain / "docs/remediation/phase09_completion_report.md").exists(),
              (built.stdout + built.stderr)[-500:])
        if built.returncode != 0:
            return
        pr = json.loads(
            (chain / "docs/remediation/phase09_PHASE_RESULT.json").read_text("utf-8"))
        npa = json.loads(
            (chain / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json").read_text("utf-8"))
        summary_sha = hashlib.sha256(
            (chain / "qa-backend/test_summary.json").read_bytes()).hexdigest()
        check("chain builder derives counts one-way from summary",
              pr["test_summary"]["total_passed"] == 14
              and pr["test_summary"]["suite_count"] == 5
              and pr["test_summary"]["sha256"] == summary_sha)
        check("chain builder records strict SHA semantics",
              pr["tested_git_sha"] == head
              and pr["evidence_generation_base_sha"] == head
              and "evidence_commit_sha" in pr["sha_semantics"])
        check("chain builder derives NEXT_PROMPT_ALLOWED=false while blocked",
              npa["NEXT_PROMPT_ALLOWED"] is False
              and npa["phase10"] == "NOT_STARTED")
        report = (chain / "docs/remediation/phase09_completion_report.md").read_text("utf-8")
        check("chain builder renders machine block into report",
              "total: 14 passed, 0 failed across 5 suites" in report
              and f"tested_git_sha: {head}" in report
              and "machine-block begin" in report)
        clean = _run_validator(chain)
        check("validator passes on coherent generated chain",
              clean.returncode == 0,
              clean.stdout[-800:] + clean.stderr[-300:])


def test_evidence_chain_drift_detection():
    """Validator must fail on every stale/tamper class (task section 6.2)."""
    head = head_sha()
    now = datetime.now(timezone.utc).isoformat()
    blockers = sorted(EXTERNAL_BLOCKERS)

    def fresh_chain(tmp: str) -> Path:
        chain = Path(tmp)
        _write_synthetic_chain(chain, head, now, blockers)
        assert _run_builder(chain).returncode == 0
        return chain

    def expect_fail(name, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            chain = fresh_chain(tmp)
            mutate(chain)
            result = _run_validator(chain)
            check(name, result.returncode != 0,
                  "validator unexpectedly passed")

    def expect_pass(name):
        with tempfile.TemporaryDirectory() as tmp:
            chain = fresh_chain(tmp)
            result = _run_validator(chain)
            check(name, result.returncode == 0,
                  result.stdout[-500:])

    expect_pass("validator green baseline (coherent chain)")

    def bump_counts(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["test_summary"]["total_passed"] = 1631  # stale-count class
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: summary count mismatch detected", bump_counts)

    def bump_hash(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["test_summary"]["sha256"] = "0" * 64
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: summary hash mismatch detected", bump_hash)

    def bump_suite_count(chain):
        qa = chain / "qa-backend"
        summary = json.loads((qa / "test_summary.json").read_text("utf-8"))
        summary["suites"] = summary["suites"][:2]  # arithmetic drift
        summary["total_passed"] = 6
        (qa / "test_summary.json").write_text(json.dumps(summary), "utf-8")
    expect_fail("stale class: suite arithmetic drift detected", bump_suite_count)

    def bump_decision(chain):
        qa = chain / "qa-backend"
        ev = json.loads((qa / "release_evidence.json").read_text("utf-8"))
        ev["core_eligible"] = True  # decision flip
        (qa / "release_evidence.json").write_text(json.dumps(ev), "utf-8")
    expect_fail("stale class: release decision mismatch detected", bump_decision)

    def bump_blockers(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["release_decision"]["external_blockers"] = []  # blocker erasure
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: blocker mismatch detected", bump_blockers)

    def bump_graph(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["graph"]["state"] = "ON_ELIGIBLE"  # graph-state mismatch
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: graph state mismatch detected", bump_graph)

    def bump_phase(chain):
        npa_path = chain / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json"
        npa = json.loads(npa_path.read_text("utf-8"))
        npa["phase_status"] = "PASS"  # PHASE_RESULT vs NEXT conflict
        npa_path.write_text(json.dumps(npa), "utf-8")
    expect_fail("stale class: PHASE_RESULT vs NEXT_PROMPT_ALLOWED conflict",
                bump_phase)

    def flip_allowed(chain):
        npa_path = chain / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json"
        npa = json.loads(npa_path.read_text("utf-8"))
        npa["NEXT_PROMPT_ALLOWED"] = True  # fabricated green gate
        npa_path.write_text(json.dumps(npa), "utf-8")
    expect_fail("stale class: fabricated NEXT_PROMPT_ALLOWED=true detected",
                flip_allowed)

    def tamper_report(chain):
        report_path = chain / "docs/remediation/phase09_completion_report.md"
        text = report_path.read_text("utf-8")
        text = text.replace("total: 14 passed, 0 failed across 5 suites",
                            "total: 999 passed, 0 failed across 5 suites")
        report_path.write_text(text, "utf-8")
    expect_fail("stale class: completion report hand-tampering detected",
                tamper_report)

    def stale_order(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["generated_at"] = "2000-01-01T00:00:00+00:00"  # generation order
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: generation-order regression detected", stale_order)

    def stale_sha(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["tested_git_sha"] = non_head_sha()  # stale referenced SHA vs base
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: stale referenced SHA detected", stale_sha)

    def fake_sha(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["tested_git_sha"] = "f" * 40  # nonexistent commit
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("stale class: fabricated/unresolvable SHA detected", fake_sha)

    def drop_artifact(chain):
        (chain / "docs/remediation/phase09_completion_report.md").unlink()
    expect_fail("stale class: missing referenced artifact detected",
                drop_artifact)

    def authority_flip(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["authorities"][A.RT101_AUTHORITY_ID]["satisfied"] = True
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("authority class: doc claims authority satisfied while "
                "live channel unsatisfied", authority_flip)

    def schema_bump(chain):
        pr_path = chain / "docs/remediation/phase09_PHASE_RESULT.json"
        pr = json.loads(pr_path.read_text("utf-8"))
        pr["schema_version"] = "phase09-phase-result-1.0"  # unsupported
        pr_path.write_text(json.dumps(pr), "utf-8")
    expect_fail("fabricated class: unsupported PHASE_RESULT schema detected",
                schema_bump)

    def reasons_erased(chain):
        qa = chain / "qa-backend"
        ev = json.loads((qa / "release_evidence.json").read_text("utf-8"))
        ev["reasons"] = []  # no authority reason anywhere
        (qa / "release_evidence.json").write_text(json.dumps(ev), "utf-8")
    # D7 review F3: reason erasure IS semantic drift now — the validator
    # must fail instead of tolerating the stripped authority reason.
    expect_fail("drift class: erased authority reason detected", reasons_erased)

    with tempfile.TemporaryDirectory() as tmp:
        chain = fresh_chain(tmp)
        qa = chain / "qa-backend"
        # byte-different but semantically identical regeneration: same
        # decision content, different key order/whitespace
        ev = json.loads((qa / "release_evidence.json").read_text("utf-8"))
        (qa / "release_evidence.json").write_text(
            json.dumps(ev, indent=4, sort_keys=True) + "\n", "utf-8")
        tamper_free = _run_validator(chain)
        # this pins that byte regeneration alone is not a failure signal.
        check("regenerated (non-identical) CI artifact passes semantic check",
              tamper_free.returncode == 0,
              tamper_free.stdout[-400:])


def main():
    test_release_matrix()
    test_authorization_requires_genuine_authority()
    test_ticket_status_generation()
    test_evidence_chain_builder_and_validator()
    test_evidence_chain_drift_detection()
    print("=" * 66)
    print(f"  Phase09 release evaluator: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
