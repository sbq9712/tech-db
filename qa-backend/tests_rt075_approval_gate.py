#!/usr/bin/env python3
"""RT-075 final evidence-consistency seal tests (hermetic, no network).

Three guarded invariants:

1. APPROVAL BINDING CURRENCY — the committed approval REQUEST artifact
   must bind, by sha256, exactly the final replay evidence recorded in
   spec/phase09_external_state.json (report, dataset seal, corpus,
   registry, identity snapshot, counts) and must remain an unapproved
   request (PENDING_OWNER_APPROVAL, no approver, no signature).
   Guards against the historical drift where the artifact kept binding a
   superseded replay report (9da4f69a...) after the final report
   (d495fc66...) was sealed.

2. CANONICAL NEXT_PROMPT RT-075 RULE — the NEXT_PROMPT gate artifact
   must express the authoritative rule (live >=1,000 representative
   events across >=7 days OR approved equivalent locked replay with
   owner-provisioned approval proof), derived from the registered
   constant phase09_authority.RT075_UNBLOCK_RULE. The WRONG historical
   ">=100 real ... across >=168h (CI replay does not qualify)" wording
   must never return. Replay unapproved => NEXT_PROMPT_ALLOWED=false.

3. FAIL-CLOSED APPROVAL MECHANISM — end-to-end over
   phase09_release.load_external_blockers with the RT-075 row: genuine
   owner-provisioned HMAC proof clears it hermetically; no proof, wrong
   artifact sha, stale approval request, wrong authority id,
   publicly-known test key material, or repo-only satisfied=true all
   fail closed. No production key material is used anywhere.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import phase09_authority as A  # noqa: E402
from phase09_release import load_external_blockers  # noqa: E402
import build_phase09_evidence as B  # noqa: E402

APPROVAL = ROOT / "docs/remediation/phase09_RT075_replay_approval.json"
EXTERNAL_STATE = ROOT / "spec/phase09_external_state.json"
NEXT_PROMPT = ROOT / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json"

# Hermetic seam: same TEST-ONLY key pattern as tests_release_phase09.py.
# Production providers reject it (PUBLICLY_KNOWN_TEST_KEYS); direct
# verify_external_satisfaction_proof calls with an explicit key are the
# sanctioned hermetic test path.
TEST_HMAC_KEY = "phase09-d7-hermetic-test-key-0123456789abcdef"
NOW = datetime.now(timezone.utc)

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def head_sha():
    return A.git_head(ROOT)


def build_rt075_owner_proof(artifact_path: Path, *, authority_id="RT-075",
                            artifact_sha256=None, hmac_key=TEST_HMAC_KEY,
                            generated_at=None) -> dict:
    """Genuine-shaped owner external-satisfaction proof (hermetic only)."""
    proof = {
        "schema_version": A.PROOF_SCHEMA_VERSION,
        "proof_type": A.EXTERNAL_SATISFACTION_PROOF_TYPE,
        "authority_id": authority_id,
        "trust_class": A.OWNER_TRUST_CLASS,
        "decision": "SATISFIED",
        "artifact_sha256": (artifact_sha256
                            if artifact_sha256 is not None
                            else hashlib.sha256(
                                artifact_path.read_bytes()).hexdigest()),
        "evaluated_git_sha": head_sha(),
        "run_id": "test-run-1",
        "generated_at": (generated_at or NOW).isoformat(),
        "provenance": {"issued_by": "phase09-hermetic-test"},
    }
    proof.pop("integrity", None)
    proof["integrity"] = {
        "alg": "HMAC-SHA256",
        "mac": hmac.new(hmac_key.encode("utf-8"),
                        A.canonical_bytes(proof),
                        hashlib.sha256).hexdigest(),
    }
    return proof


def aev_bound_digest() -> str:
    """SHA256 of the real committed RT-075 approval artifact (binding anchor)."""
    return hashlib.sha256(APPROVAL.read_bytes()).hexdigest()


def satisfied_rt075_state_copy(artifact_path: Path) -> Path:
    """External-state fixture with RT-075 satisfied=true and a valid
    satisfaction_proof hashing the REAL committed approval artifact."""
    state = json.loads(EXTERNAL_STATE.read_text("utf-8"))
    state["controls"]["RT-075"]["satisfied"] = True
    state["controls"]["RT-075"]["satisfaction_proof"] = {
        "artifact": str(artifact_path),
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    # hermetic isolation: RT-005 / Q-336 may be owner-cleared in the REAL
    # external state via the env proof channel; this scenario tests the
    # RT-075 approval mechanism only, so those rows are pinned unsatisfied
    # (hermetic w.r.t. every other owner-cleared control).
    state["controls"]["RT-005"]["satisfied"] = False
    state["controls"]["RT-005"].pop("satisfaction_proof", None)
    state["controls"]["Q-336"]["satisfied"] = False
    state["controls"]["Q-336"].pop("satisfaction_proof", None)
    return state


def write_state(base: Path, state: dict) -> Path:
    # load_external_blockers resolves proof artifact paths against
    # path.parent.parent, mirroring <root>/spec/...; absolute proof
    # artifact paths are used, so any layout works.
    spec_dir = base / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    p = spec_dir / "external.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def load(state_path: Path, owner_proofs=None):
    return load_external_blockers(state_path, owner_proofs=owner_proofs)


# ---------------------------------------------------------------------------
# 1. approval binding currency (issue A regression)
# ---------------------------------------------------------------------------
def test_approval_binding_currency():
    print("[1] RT-075 approval request binds FINAL replay evidence")
    check("approval artifact exists", APPROVAL.is_file())
    check("external state exists", EXTERNAL_STATE.is_file())

    artifact = json.loads(APPROVAL.read_text("utf-8"))
    state = json.loads(EXTERNAL_STATE.read_text("utf-8"))
    row = state["controls"]["RT-075"]
    ev = row["evidence"]
    aev = artifact["evidence"]

    actual_digest = hashlib.sha256(APPROVAL.read_bytes()).hexdigest()
    check("external state owner-provision digest == artifact file sha256",
          ev["approval_artifact_sha256_owner_provisions"] == actual_digest,
          f"state={ev['approval_artifact_sha256_owner_provisions'][:12]}.. "
          f"file={actual_digest[:12]}..")

    pairs = [
        ("replay_report_artifact_sha256", "replay_report_artifact_sha256"),
        ("replay_dataset_sha256", "replay_dataset_sha256"),
        ("replay_corpus_sha256", "replay_corpus_sha256"),
    ]
    for state_key, artifact_key in pairs:
        check(f"binding matches external state: {state_key}",
              aev[artifact_key] == ev[state_key],
              f"artifact={str(aev[artifact_key])[:16]}.. "
              f"state={str(ev[state_key])[:16]}..")
    check("corpus record count matches",
          aev["replay_corpus_record_count"] == ev["replay_corpus_record_count"])
    check("case count 5,866 matches",
          aev["replay_case_count"] == ev["replay_case_count"] == 5866)
    check("observation count 25,825 matches",
          aev["replay_observation_count"] == ev["replay_observation_count"] == 25825)
    check("decision counts match",
          aev["replay_decision_counts"] == ev["replay_decision_counts"])
    check("decision totals sum to observation count",
          sum(aev["replay_decision_counts"].values()) == 25825)

    # registry + identity snapshot: artifact must agree with the SEALED
    # DATASET itself (owner-side file; the seal is re-derived here)
    dataset_path = Path("/home/rhett/tech-db-replay/"
                        "rt075_locked_replay_dataset.json")
    if dataset_path.is_file():  # owner-side; skip hermetically if absent
        ds = json.loads(dataset_path.read_text("utf-8"))
        body = {k: v for k, v in ds.items() if k != "dataset_sha256"}
        seal = hashlib.sha256(json.dumps(
            body, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        check("sealed dataset seal intact and equals artifact binding",
              seal == aev["replay_dataset_sha256"]
              and ds.get("dataset_sha256") == seal)
        check("registry sha matches sealed dataset",
              aev["replay_registry_sha256"] == ds["registry"]["sha256"])
        check("identity snapshot matches sealed dataset",
              aev["identity_snapshot_id"]
              == ds["identity_snapshot"]["identity_snapshot_id"])
    else:
        check("registry sha present in artifact",
              re.fullmatch(r"[0-9a-f]{64}", aev["replay_registry_sha256"])
              is not None)
        check("identity snapshot present in artifact",
              str(aev["identity_snapshot_id"]).startswith("ids_"))

    check("artifact bound to a real 40-hex commit",
          re.fullmatch(r"[0-9a-f]{40}", artifact.get("git_head", "")) is not None)

    approval = artifact["approval"]
    check("approval state PENDING_OWNER_APPROVAL",
          approval["state"] == "PENDING_OWNER_APPROVAL")
    check("no approver recorded", approval["approver"] is None)
    check("no approval timestamp", approval["approved_at_utc"] is None)
    check("no signature fields added",
          set(approval) <= {"state", "approver", "approved_at_utc",
                            "mechanism", "fallback_if_not_approved", "note"})
    check("external state RT-075 owner-satisfied", row["satisfied"] is True
          and row["satisfaction_proof"]["sha256"] == aev_bound_digest())
    check("external state records owner-approved equivalent locked replay",
          ev["approval_state"] == "OWNER_APPROVED_EQUIVALENT_LOCKED_REPLAY")

    sessions = aev["codex_independent_review"]["sessions"]
    final = sessions[-1]
    check("final Codex session verdict ACCEPT",
          final["verdict"] == "ACCEPT", str(final))
    check("all Codex session hashes are 64-hex",
          all(re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) for s in sessions))
    check("Codex history ends ACCEPT after REJECT",
          any(s["verdict"] == "REJECT" for s in sessions)
          and final["verdict"] == "ACCEPT")

    spec_authority = json.dumps(artifact["spec_authority"], ensure_ascii=False)
    check("artifact cites >=1,000 / 7-day authority",
          ">=1,000" in spec_authority and "7 days" in spec_authority)


# ---------------------------------------------------------------------------
# 2. canonical NEXT_PROMPT RT-075 rule (issue B regression)
# ---------------------------------------------------------------------------
def build(phase_status="PASS_WITH_EXTERNAL_BLOCKER", core=False, prod=False,
          blockers=None, authority_satisfied=False):
    phase_result = {"phase_status": phase_status,
                    "graph": {"state": "OFF", "gain_conclusion": "PENDING"}}
    decision = {"core_eligible": core,
                "production_release_eligible": prod,
                "external_blockers": dict(blockers or {}),
                "reasons": ["x"]}
    authority_results = {
        A.RT101_AUTHORITY_ID: A.AuthorityResult(
            authority_id=A.RT101_AUTHORITY_ID,
            satisfied=authority_satisfied,
            trust_class=None, proof_digest=None,
            artifact_sha256=None, reasons=()),
    }
    return B.build_next_prompt(
        phase_result=phase_result, decision=decision,
        authority_results=authority_results,
        tested_sha="0" * 40, evidence_generation_base_sha="1" * 40,
        generated_at=NOW.isoformat())


def test_next_prompt_rt075_rule():
    print("[2] canonical NEXT_PROMPT RT-075 unblock rule")
    np = build(blockers={"RT-075": "shadow/replay approval pending",
                         "Q-336": "retention", "RT-005": "branch protection"})
    conditions = np["unblock_conditions"]
    rt075 = [c for c in conditions if "RT-075" in c]
    check("exactly one RT-075 unblock condition", len(rt075) == 1)
    rt075 = rt075[0] if rt075 else ""

    check("live path: >=1,000 representative events",
          ">=1,000" in rt075 and "live-shadow" in rt075, rt075)
    check("live path: >=7 days / 168h semantics",
          ">=7 days" in rt075 and "168h" in rt075, rt075)
    check("replay path: equivalent locked replay present",
          "equivalent locked replay" in rt075, rt075)
    check("replay path: registered verifier named",
          A.RT075_REGISTERED_REPLAY_VERIFIER in rt075, rt075)
    check("replay path: owner-provisioned approval proof required",
          "owner-provisioned external approval proof" in rt075
          and "PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY" in rt075, rt075)
    check("synthetic/CI evidence excluded from both paths",
          "Synthetic/CI-only evidence cannot impersonate" in rt075, rt075)
    check("unapproved replay never clears RT-075",
          "unapproved replay never clears" in rt075, rt075)

    check("condition derived from registered constant",
          rt075 == A.RT075_UNBLOCK_RULE,
          "NEXT_PROMPT no longer mirrors phase09_authority.RT075_UNBLOCK_RULE")

    everything = json.dumps(conditions, ensure_ascii=False)
    check("WRONG historical '>=100 real ...' rule absent",
          ">=100 real" not in everything
          and not re.search(r">=100\b(?!,)", everything), everything[:200])

    check("replay unapproved => NEXT_PROMPT_ALLOWED=false",
          np["NEXT_PROMPT_ALLOWED"] is False)
    check("blocked replay listed in why",
          any("RT-075" in r for r in np["why"]))
    check("unsatisfied required authority blocks continuation",
          any("unsatisfied" in r for r in np["why"]))
    check("phase10 stays NOT_STARTED", np["phase10"] == "NOT_STARTED")
    check("gate still forbids starting Phase10 while blocked",
          "Starting Phase10 while NEXT_PROMPT_ALLOWED=false" in np["forbidden"])

    # the gate itself must not have been loosened: all-clear still allows
    np_ok = build(phase_status="PASS", core=True, prod=True, blockers={},
                  authority_satisfied=True)
    check("fully cleared state still yields NEXT_PROMPT_ALLOWED=true",
          np_ok["NEXT_PROMPT_ALLOWED"] is True
          and np_ok["phase10"] == "ELIGIBLE_TO_START")


# ---------------------------------------------------------------------------
# 3. fail-closed approval mechanism, end-to-end (issue: owner-only clear)
# ---------------------------------------------------------------------------
def test_approval_mechanism_fail_closed():
    print("[3] RT-075 approval mechanism end-to-end (hermetic)")
    real_digest = hashlib.sha256(APPROVAL.read_bytes()).hexdigest()
    genuine = build_rt075_owner_proof(APPROVAL)

    with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
        base = Path(tmp)

        # (a) hermetic happy path: satisfied=true + valid owner proof clears
        state = write_state(base, satisfied_rt075_state_copy(APPROVAL))
        owner = {"RT-075": A.verify_external_satisfaction_proof(
            genuine, control_id="RT-075", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW,
            commit_time=A.git_commit_time(ROOT, head_sha()))}
        try:
            blockers = load(state, owner_proofs=owner)
            check("valid owner proof clears RT-075 hermetically",
                  "RT-075" not in blockers, str(sorted(blockers)))
        except ValueError as exc:
            check("valid owner proof clears RT-075 hermetically",
                  False, str(exc))

        # (b) no owner proof => fail
        try:
            load(state, owner_proofs=None)
            check("repo-only satisfied=true fails closed", False, "no raise")
        except ValueError:
            check("repo-only satisfied=true fails closed", True)

        # (c) owner proof binding a WRONG artifact sha => fail
        wrong_sha_owner = {"RT-075": A.verify_external_satisfaction_proof(
            build_rt075_owner_proof(APPROVAL, artifact_sha256="a" * 64),
            control_id="RT-075", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW,
            commit_time=A.git_commit_time(ROOT, head_sha()))}
        try:
            blockers = load(state, owner_proofs=wrong_sha_owner)
            check("wrong artifact sha fails closed",
                  "RT-075" in blockers, str(sorted(blockers)))
        except ValueError:
            check("wrong artifact sha fails closed", True)

        # (d) STALE approval request: satisfaction_proof hashes a digest
        # that no longer matches the committed artifact file => fail
        stale = satisfied_rt075_state_copy(APPROVAL)
        stale["controls"]["RT-075"]["satisfaction_proof"]["sha256"] = \
            "8b95b71bdc7b8d686e684d24723c243a0ad1b74bfd6983cf0a6d951f9ca70971"
        stale_path = write_state(base / "stale", stale)
        try:
            load(stale_path, owner_proofs=owner)
            check("stale approval request digest fails closed", False,
                  "old 8b95.. digest accepted")
        except ValueError:
            check("stale approval request digest fails closed", True)

        # (e) wrong authority id in the owner proof => fail
        wrong_id = {"RT-075": A.verify_external_satisfaction_proof(
            build_rt075_owner_proof(APPROVAL, authority_id="RT-005"),
            control_id="RT-075", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW,
            commit_time=A.git_commit_time(ROOT, head_sha()))}
        try:
            blockers = load(state, owner_proofs=wrong_id)
            check("wrong authority id fails closed",
                  "RT-075" in blockers, str(sorted(blockers)))
        except ValueError:
            check("wrong authority id fails closed", True)

        # (f) tampered (post-signing edit) owner proof => fail
        tampered = copy.deepcopy(genuine)
        tampered["run_id"] = "evil-rewrite-after-signing"
        tampered_owner = {"RT-075": A.verify_external_satisfaction_proof(
            tampered, control_id="RT-075", current_git_sha=head_sha(),
            hmac_key=TEST_HMAC_KEY, now=NOW,
            commit_time=A.git_commit_time(ROOT, head_sha()))}
        try:
            blockers = load(state, owner_proofs=tampered_owner)
            check("tampered owner proof fails closed",
                  "RT-075" in blockers, str(sorted(blockers)))
        except ValueError:
            check("tampered owner proof fails closed", True)

        # (g) publicly-known test key material through the PRODUCTION env
        # channel never satisfies the control (policy: PUBLICLY_KNOWN_TEST_KEYS;
        # external controls enforce it upstream via key rejection => the
        # proof cannot verify, fail-closed either way)
        check("hermetic key is registered as publicly-known test material",
              A._production_key_rejected(TEST_HMAC_KEY) is True)
        env_proofs = A.external_satisfaction_proofs_from_env(
            root=ROOT,
            env={A.ENV_EXTERNAL_PROOFS: json.dumps(
                     {"RT-075": build_rt075_owner_proof(APPROVAL)}),
                 A.ENV_EXTERNAL_HMAC_KEY: TEST_HMAC_KEY})
        check("production env channel rejects publicly-known key material",
              "RT-075" in env_proofs
              and not env_proofs["RT-075"].satisfied
              and bool(env_proofs["RT-075"].reasons),
              str(env_proofs.get("RT-075").reasons
                  if "RT-075" in env_proofs else "missing"))
        try:
            blockers = load(state, owner_proofs=env_proofs)
            check("env-channel public-key 'proof' fails closed",
                  "RT-075" in blockers, str(sorted(blockers)))
        except ValueError:
            check("env-channel public-key 'proof' fails closed", True)

    check("no production key material used (test key is the registered "
          "hermetic seam key)",
          TEST_HMAC_KEY in A.PUBLICLY_KNOWN_TEST_KEYS)


def main():
    test_approval_binding_currency()
    test_next_prompt_rt075_rule()
    test_approval_mechanism_fail_closed()
    print("=" * 66)
    print(f"RT-075 approval gate: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
