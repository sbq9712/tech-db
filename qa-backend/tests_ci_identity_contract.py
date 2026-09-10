#!/usr/bin/env python3
"""CI identity-contract regression tests (hermetic, no network).

Locks the D7 CI identity contract that fixed the permanent pull_request
merge-ref false-red (PR #10 run 34482190286/34482190298):

The GitHub `pull_request` event default-checkouts the merge ref
``refs/pull/N/merge`` whose SHA differs from the PR head commit that owner
external-satisfaction proofs bind to via ``evaluated_git_sha``. The contract:

- ``phase09_authority.resolve_authority_subject_sha`` resolves the authority
  subject: default checked-out HEAD; optional ``PHASE09_AUTHORITY_SUBJECT_SHA``
  override (40-hex, must be a commit known to the checkout) sourced in CI ONLY
  from the trusted GitHub event context (``github.event.pull_request.head.sha``);
  malformed or checkout-unknown values FAIL CLOSED (raise).
- Proof verification (equality, HMAC, temporal, artifact binding) is unchanged;
  the override only selects WHICH commit is the subject.
- A proof bound to the PR head verifies on a merge-ref checkout with the
  subject override and fails without it (wrong-SHA replay protection kept).
- Workflows (contract-pinned by assertions below):
  * push-tier injects the external-satisfaction secrets ONLY into the suites
    step (never job-wide, never GITHUB_ENV) plus the subject via GITHUB_ENV;
  * the merge ref is identity-checked: parents exactly
    [event base sha, event head sha], head contained in the merge ref;
  * the remediation phase09 job runs the canonical release gate on BOTH the
    verified merge ref AND an exact-head checkout whose HEAD is asserted
    equal to the event head sha before any secret-bearing step, validates the
    strict machine evidence chain at exact head, uploads exact-head artifacts,
    and keeps fail-closed gates red via expected-block verifiers.

No production key material is used anywhere; hermetic proof construction
follows the registered TEST-ONLY seam pattern of tests_rt075_approval_gate.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

import phase09_authority as A  # noqa: E402
from phase09_release import load_external_blockers  # noqa: E402

PASSED = 0
FAILED = 0
NOW = datetime.now(timezone.utc)
# Provider-level tests need a runtime-random key (production providers
# reject the registered seam key by design; that rejection is covered by
# tests_rt075_approval_gate.py).
RANDOM_KEY = secrets.token_hex(32)
# Registered hermetic seam key (production providers reject it).
TEST_HMAC_KEY = "phase09-d7-hermetic-test-key-0123456789abcdef"


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


# --------------------------------------------------------------------------
# hermetic git repo builder: main_tip <- pr_head <- merge_commit(+base_tip)
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, text=True,
                          capture_output=True, check=True)
    return proc.stdout.strip()


def build_merge_topology(base: Path) -> dict:
    """Creates base_tip -> pr_head -> merge(refs/pull/1/merge) like GitHub."""
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "f.txt").write_text("pr change\n")
    _git(repo, "commit", "-q", "-am", "pr head")
    pr_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "g.txt").write_text("main move\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main tip")
    main_tip = _git(repo, "rev-parse", "HEAD")
    # GitHub-style merge commit: parents [base_tip(main), pr_head]
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge pr", pr_head)
    merge_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", merge_sha)
    parents = _git(repo, "log", "-1", "--format=%P", merge_sha).split()
    assert parents == [main_tip, pr_head], parents
    return {"repo": repo, "base_tip": base_tip, "main_tip": main_tip,
            "pr_head": pr_head, "merge_sha": merge_sha}


def build_proof(*, subject: str, artifact: bytes, key: str = RANDOM_KEY,
                generated_at: datetime | None = None,
                control_id: str = "RT-075") -> dict:
    proof = {
        "schema_version": A.PROOF_SCHEMA_VERSION,
        "proof_type": A.EXTERNAL_SATISFACTION_PROOF_TYPE,
        "authority_id": control_id,
        "trust_class": A.OWNER_TRUST_CLASS,
        "decision": "SATISFIED",
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "evaluated_git_sha": subject,
        "run_id": "ci-identity-test-run",
        "generated_at": (generated_at or NOW).isoformat(),
        "provenance": {"issued_by": "ci-identity-hermetic-test"},
    }
    proof.pop("integrity", None)
    proof["integrity"] = {
        "alg": "HMAC-SHA256",
        "mac": hmac.new(key.encode("utf-8"), A.canonical_bytes(proof),
                        hashlib.sha256).hexdigest(),
    }
    return proof


def env_with(proofs: dict, key: str = RANDOM_KEY, **extra) -> dict:
    return {
        A.ENV_EXTERNAL_PROOFS: json.dumps(proofs),
        A.ENV_EXTERNAL_HMAC_KEY: key,
        **extra,
    }


# --------------------------------------------------------------------------
# 1. subject resolution
# --------------------------------------------------------------------------

def test_subject_resolution():
    print("subject resolution:")
    topo = build_merge_topology(Path(tempfile.mkdtemp(prefix="cicid1-")))
    repo = topo["repo"]
    # default: unset env -> checked-out HEAD (the merge commit here)
    check("default subject == checked-out HEAD",
          A.resolve_authority_subject_sha(repo, env={}) == topo["merge_sha"])
    # explicit subject override accepted when commit is known
    got = A.resolve_authority_subject_sha(
        repo, env={A.ENV_AUTHORITY_SUBJECT_SHA: topo["pr_head"]})
    check("explicit PR-head subject accepted", got == topo["pr_head"])
    # empty/blank env value means "unset" (GitHub exports unset secrets as
    # empty strings) -> falls back to checked-out HEAD
    check("empty subject env treated as unset",
          A.resolve_authority_subject_sha(repo, env={A.ENV_AUTHORITY_SUBJECT_SHA: ""})
          == topo["merge_sha"])
    # malformed -> raise (fail closed)
    for bad in ("zz", topo["pr_head"][:39] + "g",
                topo["pr_head"] + "0"):
        try:
            A.resolve_authority_subject_sha(
                repo, env={A.ENV_AUTHORITY_SUBJECT_SHA: bad})
            check(f"malformed subject rejected {bad!r}", False)
        except ValueError:
            check(f"malformed subject rejected {bad!r}", True)
    # well-formed but unknown commit -> raise (commit-time unverifiable)
    try:
        A.resolve_authority_subject_sha(
            repo, env={A.ENV_AUTHORITY_SUBJECT_SHA: "b" * 40})
        check("unknown 40-hex subject rejected", False)
    except ValueError:
        check("unknown 40-hex subject rejected", True)
    # workflow contract: subject is sourced from the event context, not repo
    wf = yaml.safe_load((ROOT / ".github/workflows/qa-tests.yml").read_text())
    step = next(s for s in wf["jobs"]["push-tier"]["steps"]
                if s.get("name", "").startswith("Resolve proof authority subject"))
    src = json.dumps(step)
    check("push-tier subject step reads event head/base sha from event context",
          "github.event.pull_request.head.sha" in src
          and "github.event.pull_request.base.sha" in src)
    check("push-tier subject step never reads repo files for the subject",
          "cat " not in src and "head_ref" not in src)


# --------------------------------------------------------------------------
# 2. proof verification against merge-ref checkout (the PR #10 false-red fix)
# --------------------------------------------------------------------------

def test_proof_on_merge_ref():
    print("proof identity on merge-ref checkout:")
    topo = build_merge_topology(Path(tempfile.mkdtemp(prefix="cicid2-")))
    repo = topo["repo"]
    artifact = b"approved artifact bytes"
    proof = build_proof(subject=topo["pr_head"], artifact=artifact)
    subject_env = {A.ENV_AUTHORITY_SUBJECT_SHA: topo["pr_head"]}
    ok = A.verify_external_satisfaction_proof(
        proof, control_id="RT-075", current_git_sha=topo["pr_head"],
        hmac_key=RANDOM_KEY, now=NOW,
        commit_time=A.git_commit_time(repo, topo["pr_head"]))
    check("direct verify binds subject pr_head", ok.satisfied)
    # provider-level: subject override lets a head-bound proof verify even
    # though the checked-out HEAD is the merge commit (GitHub merge ref)
    results = A.external_satisfaction_proofs_from_env(
        root=repo, env=env_with({"RT-075": proof}, **subject_env))
    check("head-bound proof verifies on merge-ref checkout via subject",
          results["RT-075"].satisfied, results["RT-075"].reasons)
    # without the override the same proof must NOT verify at the merge ref
    results = A.external_satisfaction_proofs_from_env(
        root=repo, env=env_with({"RT-075": proof}))
    check("head-bound proof fails closed at merge SHA without override",
          not results["RT-075"].satisfied)
    check("failure reason is the SHA binding, unchanged semantics",
          any("evaluated_git_sha does not match" in r
              for r in results["RT-075"].reasons), results["RT-075"].reasons)
    # replay to a WRONG subject: proof bound to pr_head must fail when the
    # workflow-declared subject is a different commit (e.g. main tip)
    results = A.external_satisfaction_proofs_from_env(
        root=repo,
        env=env_with({"RT-075": proof},
                     **{A.ENV_AUTHORITY_SUBJECT_SHA: topo["main_tip"]}))
    check("wrong-subject replay fails closed",
          not results["RT-075"].satisfied, results["RT-075"].reasons)
    # subject bound to OTHER existing commit with matching proof: verifies
    # (binding discipline is owner-side; equality semantics unchanged)
    proof_main = build_proof(subject=topo["main_tip"], artifact=artifact)
    results = A.external_satisfaction_proofs_from_env(
        root=repo,
        env=env_with({"RT-075": proof_main},
                     **{A.ENV_AUTHORITY_SUBJECT_SHA: topo["main_tip"]}))
    check("proof bound to its own subject still verifies (no weakening)",
          results["RT-075"].satisfied, results["RT-075"].reasons)
    # malformed subject env -> loud fail-closed crash (never silent unsat)
    try:
        A.external_satisfaction_proofs_from_env(
            root=repo,
            env=env_with({"RT-075": proof},
                         **{A.ENV_AUTHORITY_SUBJECT_SHA: "not-a-sha"}))
        check("malformed subject env crashes fail-closed", False)
    except ValueError:
        check("malformed subject env crashes fail-closed", True)
    # stale proof (older than max_age_days) still rejected under override
    stale = build_proof(subject=topo["pr_head"], artifact=artifact,
                        generated_at=NOW - timedelta(days=400))
    results = A.external_satisfaction_proofs_from_env(
        root=repo,
        env=env_with({"RT-075": stale}, **subject_env))
    check("stale proof fails closed under subject override",
          not results["RT-075"].satisfied, results["RT-075"].reasons)
    # missing secret -> unsatisfied (fail closed)
    results = A.external_satisfaction_proofs_from_env(
        root=repo,
        env={A.ENV_EXTERNAL_PROOFS: json.dumps({"RT-075": proof}),
             A.ENV_EXTERNAL_HMAC_KEY: ""})
    check("missing HMAC key fails closed", not results["RT-075"].satisfied)


# --------------------------------------------------------------------------
# 3. repo-only satisfied=true still fails closed (no self-attestation)
# --------------------------------------------------------------------------

def test_repo_only_self_attestation():
    print("repo-only satisfaction still fails closed:")
    topo = build_merge_topology(Path(tempfile.mkdtemp(prefix="cicid3-")))
    repo = topo["repo"]
    artifact = repo / "approved_artifact.json"
    artifact.write_bytes(b"owner artifact")
    spec_dir = repo / "spec"
    spec_dir.mkdir()
    state = {
        "schema_version": "phase09-external-state-1.0",
        "controls": {
            "Q-336": {"description": "d", "satisfied": False,
                      "evidence": {"observed_at": NOW.isoformat()}},
            "RT-005": {"description": "d", "satisfied": False,
                       "evidence": {"observed_at": NOW.isoformat()}},
            "RT-075": {
                "description": "d",
                "satisfied": True,
                "satisfaction_proof": {
                    "artifact": str(artifact),
                    "sha256": hashlib.sha256(
                        artifact.read_bytes()).hexdigest()},
                "evidence": {"observed_at": NOW.isoformat()},
            },
        },
    }
    (spec_dir / "phase09_external_state.json").write_text(json.dumps(state))
    # no proofs env at all -> ValueError (the exact PR-CI failure signature)
    try:
        load_external_blockers(spec_dir / "phase09_external_state.json",
                               owner_proofs={})
        check("repo-only satisfied raises without owner proof", False)
    except ValueError as e:
        check("repo-only satisfied raises without owner proof",
              "RT-075" in str(e) and "owner-provisioned" in str(e))
    # with a verified owner proof bound to the subject -> clears
    proof = build_proof(subject=topo["pr_head"], artifact=artifact.read_bytes())
    owner = A.external_satisfaction_proofs_from_env(
        root=repo, env=env_with({"RT-075": proof},
                                **{A.ENV_AUTHORITY_SUBJECT_SHA: topo["pr_head"]}))
    blockers = load_external_blockers(spec_dir / "phase09_external_state.json",
                                      owner_proofs=owner)
    check("verified owner proof bound to subject clears the control",
          "RT-075" not in blockers, blockers)
    # wrong artifact (owner proof digests different artifact than the repo
    # row declares) -> fail closed at the binding layer
    wrong = A.external_satisfaction_proofs_from_env(
        root=repo,
        env=env_with({"RT-075": build_proof(subject=topo["pr_head"],
                                            artifact=b"other bytes")},
                     **{A.ENV_AUTHORITY_SUBJECT_SHA: topo["pr_head"]}))
    try:
        load_external_blockers(spec_dir / "phase09_external_state.json",
                               owner_proofs=wrong)
        check("wrong-artifact owner proof fails closed at binding layer",
              False)
    except ValueError as e:
        check("wrong-artifact owner proof fails closed at binding layer",
              "RT-075" in str(e))


# --------------------------------------------------------------------------
# 4. workflow contract: qa-tests.yml push-tier
# --------------------------------------------------------------------------

def _suite_step(job):
    return next(s for s in job["steps"]
                if s.get("name", "").startswith("Push-tier suites"))


def test_qa_tests_contract():
    print("qa-tests.yml push-tier contract:")
    wf = yaml.safe_load((ROOT / ".github/workflows/qa-tests.yml").read_text())
    job = wf["jobs"]["push-tier"]
    co = job["steps"][0]
    check("push-tier full-history checkout",
          co.get("with", {}).get("fetch-depth") == 0)
    names = [s.get("name", "") for s in job["steps"]]
    check("identity step precedes suites step",
          any(n.startswith("Resolve proof authority subject") for n in names)
          and names.index(next(n for n in names
                               if n.startswith("Resolve proof authority subject")))
          < names.index(next(n for n in names if n.startswith("Push-tier suites"))))
    step = _suite_step(job)
    env = step.get("env", {})
    check("suites step injects external proofs + HMAC key (step-scoped)",
          str(env.get("PHASE09_EXTERNAL_SATISFACTION_PROOFS", "")).strip()
          == "${{ secrets.PHASE09_EXTERNAL_SATISFACTION_PROOFS }}"
          and str(env.get("PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY", "")).strip()
          == "${{ secrets.PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY }}")
    check("secrets are NOT job-wide (exposure boundary preserved)",
          not any("secrets." in str(job.get("env", {}))
                  for _ in [0]))
    check("secrets never placed in GITHUB_ENV by workflows",
          "GITHUB_ENV" not in json.dumps(step))
    check("subject flows to suites via trusted-context GITHUB_ENV export",
          "PHASE09_AUTHORITY_SUBJECT_SHA" in json.dumps(job["steps"]))
    nightly = wf["jobs"]["nightly"]
    check("nightly job untouched by external-proof injection",
          "PHASE09_EXTERNAL_SATISFACTION" not in json.dumps(nightly))


# --------------------------------------------------------------------------
# 5. workflow contract: remediation-gates.yml phase09 job
# --------------------------------------------------------------------------

def test_remediation_gates_contract():
    print("remediation-gates.yml phase09 contract:")
    wf = yaml.safe_load(
        (ROOT / ".github/workflows/remediation-gates.yml").read_text())
    job = wf["jobs"]["phase09-benchmarks-ci-release-gates"]
    steps = job["steps"]
    by_name = [s.get("name", "") for s in steps]

    def idx(prefix):
        return next(i for i, n in enumerate(by_name) if n.startswith(prefix))

    merge_id = steps[idx("Merge-ref identity proof")]
    msrc = json.dumps(merge_id)
    check("merge-ref identity asserts parents == [event base, event head]",
          "rev-parse HEAD^1" in msrc and "rev-parse HEAD^2" in msrc
          and "github.event.pull_request.base.sha" in msrc
          and "github.event.pull_request.head.sha" in msrc)
    check("merge containment asserted (merge-base --is-ancestor)",
          "merge-base --is-ancestor" in msrc)
    check("subject exported from trusted event context",
          "PHASE09_AUTHORITY_SUBJECT_SHA" in msrc)

    gate_merge = steps[idx("Execute canonical Phase09 release gate (merge integration")]
    check("merge-ref gate keeps running all required suites on the merge result",
          "run_phase09_release_gate.py" in json.dumps(gate_merge))
    check("merge-ref gate step-scoped secrets (5)",
          sum(1 for v in gate_merge.get("env", {}).values()
              if "secrets." in str(v)) == 5)
    check("merge-ref expected-block verifier present (if: failure())",
          steps[idx("Assert intentional fail-closed reason (merge result")]
          .get("if") == "failure()")

    head_co = steps[idx("Exact-head checkout")]
    hw = head_co.get("with", {})
    check("exact-head checkout pinned to event head sha",
          "github.event.pull_request.head.sha" in json.dumps(head_co)
          and hw.get("path") == "phase09-head")
    head_id = steps[idx("Exact-head identity assert")]
    check("exact-head identity assert precedes ALL secret-bearing steps",
          idx("Exact-head identity assert")
          < min(i for i, s in enumerate(steps)
                if i > idx("Exact-head identity assert")
                and "secrets." in json.dumps(s.get("env", {}))))
    check("exact-head assert compares HEAD to event head sha",
          "rev-parse HEAD" in json.dumps(head_id)
          and "EVENT_HEAD_SHA" in json.dumps(head_id))
    gate_head = steps[idx("Execute canonical Phase09 release gate (exact head")]
    check("exact-head gate present with step-scoped secrets (5)",
          sum(1 for v in gate_head.get("env", {}).values()
              if "secrets." in str(v)) == 5
          and gate_head.get("working-directory") == "phase09-head")
    check("exact-head expected-block verifier present (if: failure())",
          steps[idx("Assert intentional fail-closed reason (exact head")]
          .get("if") == "failure()")
    strict = steps[idx("Validate Phase09 evidence chain")]
    check("strict machine evidence chain validated at exact head",
          strict.get("working-directory") == "phase09-head"
          and "--strict-machine" in json.dumps(strict))
    check("strict validator cannot be skipped by earlier failure",
          strict.get("if") == "always()")
    up = next(s for s in steps if s.get("name", "").startswith("Upload Phase09"))
    check("artifacts uploaded from exact-head checkout only",
          "phase09-head/" in json.dumps(up.get("with", {}).get("path", "")))
    check("head-only pass cannot mask merge-ref gate (both gates required)",
          idx("Execute canonical Phase09 release gate (merge integration")
          < idx("Execute canonical Phase09 release gate (exact head")
          and "continue-on-error" not in json.dumps(steps))


def main():
    test_subject_resolution()
    test_proof_on_merge_ref()
    test_repo_only_self_attestation()
    test_qa_tests_contract()
    test_remediation_gates_contract()
    print("=" * 66)
    print(f"CI identity contract: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
