#!/usr/bin/env python3
"""Owner-provisioned external authority proofs for Phase09 (D7, RT-101).

Trust model
-----------
Repository-controlled files (policy JSON, external-state JSON, acceptance
matrix, fixtures) can only *declare requirements*.  They can never declare
that a required authority or external control is satisfied.  Satisfaction
must come from an owner-provisioned proof delivered through the runtime
environment (in CI: GitHub Actions repository secrets, which live outside
ordinary repo content and cannot be modified by commits or PRs).

A proof is canonical JSON with an allowlisted key set, bound to the exact
checkout (git SHA), the spec identity (spec-manifest digests), the locked
benchmark manifest identity, the holdout lock digest (never the gold
itself), and an HMAC-SHA256 integrity tag over the canonical serialization.
The HMAC key lives only in the owner-controlled environment; it is never
read from the repository and never persisted.

Fail closed: any absent, malformed, stale, replayed, tampered, or
wrongly-trusted input yields an unsatisfied result with sanitized reasons.
Reasons never echo proof values or key material.

``TEST_ONLY`` / synthetic trust classes are always rejected by production
verifiers; hermetic tests build genuine-shaped proofs with a test-local
key instead (the seam is the construction site, not a weaker trust class).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

PROOF_SCHEMA_VERSION = "phase09-authority-proof-1.0"
OWNER_TRUST_CLASS = "OWNER_PROVISIONED_EXTERNAL"

RT101_AUTHORITY_ID = "RT-101_answer_level_blinded_release_holdout_gold"
RT101_PROOF_TYPE = "RT101_RELEASE_HOLDOUT_AUTHORITY"
EXTERNAL_SATISFACTION_PROOF_TYPE = "EXTERNAL_CONTROL_SATISFACTION"

# RT-075 canonical unblock rule — single registered source of truth.
# Authority: execution tickets L920 (">=1,000 representative events +
# 7 days or approved equivalent replay before activation"), final spec
# L1217/L1192 (equivalent locked replay + explicit approval; low-traffic
# exception never means zero evidence). Consumed verbatim by the Phase09
# NEXT_PROMPT artifact (scripts/build_phase09_evidence.py); regression-
# tested in qa-backend/tests_rt075_approval_gate.py. The historical
# ">=100 events across >=168h" wording was WRONG and must not return.
RT075_MIN_EVENTS = 1000
RT075_MIN_DAYS = 7
RT075_REGISTERED_REPLAY_VERIFIER = "scripts/verify_rt075_locked_replay.py"
RT075_UNBLOCK_RULE = (
    "Clear RT-075 by either: "
    f"(A) >={RT075_MIN_EVENTS:,} qualifying representative live-shadow "
    f"ER events across >={RT075_MIN_DAYS} days (>=168h) of real "
    "production traffic, or "
    "(B) an equivalent locked replay that passes the registered replay "
    f"verifier ({RT075_REGISTERED_REPLAY_VERIFIER}) and receives the "
    "required owner-provisioned external approval proof "
    "(PHASE09_EXTERNAL_SATISFACTION_PROOFS + "
    "PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY with the RT-075 entry "
    "binding artifact_sha256 = the approval request artifact's sha256). "
    "Synthetic/CI-only evidence cannot impersonate either path; an "
    "unapproved replay never clears RT-075."
)

# D7 gatekeeper hardening: HMAC key material that is publicly committed in
# this repository can never count as owner-provisioned secret material.  A
# proof signed with such a key is rejected by the PRODUCTION providers
# regardless of cryptographic validity (the hermetic test path calls the
# verify_* functions with an explicit key and is unaffected).
PUBLICLY_KNOWN_TEST_KEYS = frozenset({
    # qa-backend/tests_release_phase09.py hermetic seam key
    "phase09-d7-hermetic-test-key-0123456789abcdef",
})


def _production_key_rejected(key: str) -> bool:
    return (not key or len(key) < 32
            or key in PUBLICLY_KNOWN_TEST_KEYS)

ENV_RT101_PROOF = "PHASE09_RT101_AUTHORITY_PROOF"
ENV_RT101_EXPECTED_HOLDOUT_LOCK = "PHASE09_RT101_EXPECTED_HOLDOUT_LOCK_SHA256"
ENV_RT101_HMAC_KEY = "PHASE09_RT101_AUTHORITY_HMAC_KEY"
ENV_EXTERNAL_PROOFS = "PHASE09_EXTERNAL_SATISFACTION_PROOFS"
ENV_EXTERNAL_HMAC_KEY = "PHASE09_EXTERNAL_SATISFACTION_HMAC_KEY"

PROOF_KEYS_RT101 = frozenset({
    "schema_version", "proof_type", "authority_id", "trust_class",
    "holdout_lock_sha256", "evaluated_git_sha", "spec_sha256",
    "decision_register_sha256", "manifest_id", "identity_snapshot_id",
    "run_id", "generated_at", "provenance", "integrity",
})
PROOF_KEYS_EXTERNAL = frozenset({
    "schema_version", "proof_type", "authority_id", "trust_class",
    "decision", "artifact_sha256", "evaluated_git_sha",
    "run_id", "generated_at", "provenance", "integrity",
})
HEX_DIGITS = frozenset("0123456789abcdef")
CLOCK_SKEW = timedelta(minutes=5)
COMMIT_TIME_GRACE = timedelta(hours=24)
MIN_HMAC_KEY_BYTES = 32

# Satisfaction-claiming field names are forbidden inside requirement
# declarations: policy can only declare requirements.
FORBIDDEN_REQUIREMENT_KEYS = frozenset({
    "status", "satisfied", "state", "approved", "available", "granted",
    "eligible", "verified", "ok", "passed", "result",
})
MANDATORY_BINDINGS_RT101 = frozenset({
    "holdout_lock_sha256", "evaluated_git_sha", "spec_sha256",
    "decision_register_sha256", "manifest_id", "identity_snapshot_id",
})


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _is_hex(value: str, length: int) -> bool:
    return (isinstance(value, str) and len(value) == length
            and set(value) <= HEX_DIGITS)


def _parse_iso_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def git_head(root: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          text=True, capture_output=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_commit_time(root: Path, sha: str) -> datetime | None:
    """Committer time of ``sha`` (ordering sanity check, not a trust anchor)."""
    if not _is_hex(sha, 40):
        return None
    proc = subprocess.run(["git", "show", "-s", "--format=%cI", sha],
                          cwd=root, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return _parse_iso_timestamp(proc.stdout.strip())


def _hmac_tag(proof: Mapping, key: str) -> str:
    payload = {k: v for k, v in proof.items() if k != "integrity"}
    return hmac.new(key.encode("utf-8"), canonical_bytes(payload),
                    hashlib.sha256).hexdigest()


def _integrity_ok(proof: Mapping, key: str) -> bool:
    if not isinstance(key, str) or len(key.encode("utf-8")) < MIN_HMAC_KEY_BYTES:
        return False
    integrity = proof.get("integrity")
    if (not isinstance(integrity, dict)
            or integrity.get("alg") != "HMAC-SHA256"
            or not _is_hex(str(integrity.get("mac", "")).strip(), 64)):
        return False
    expected = _hmac_tag(proof, key)
    return hmac.compare_digest(expected, str(integrity.get("mac")))


@dataclass(frozen=True)
class AuthorityResult:
    """Sanitized verification outcome; never carries proof/key material."""

    authority_id: str
    satisfied: bool
    trust_class: str | None = None
    proof_digest: str | None = None
    artifact_sha256: str | None = None
    reasons: tuple[str, ...] = ()

    def to_sanitized_dict(self) -> dict:
        return {
            "authority_id": self.authority_id,
            "satisfied": self.satisfied,
            "trust_class": self.trust_class,
            "proof_digest": self.proof_digest,
            "artifact_sha256": self.artifact_sha256,
            "reasons": list(self.reasons),
        }


def unsatisfied(authority_id: str, *reasons: str) -> AuthorityResult:
    return AuthorityResult(authority_id=authority_id, satisfied=False,
                           reasons=tuple(r for r in reasons if r))


def _common_proof_checks(proof, *, expected_keys: frozenset, proof_type: str,
                         authority_id: str, hmac_key: str, now: datetime,
                         max_age_days: int, current_git_sha: str,
                         commit_time: datetime | None,
                         reasons: list[str]) -> str | None:
    """Shared structural/trust/temporal/integrity checks.

    Returns the canonical proof digest when the proof is structurally sound
    enough for a digest, else ``None``.  Appends sanitized reasons only.
    """
    if not isinstance(proof, dict):
        reasons.append("proof is not a JSON object")
        return None
    if set(proof) != set(expected_keys):
        unknown = sorted(set(proof) - set(expected_keys))
        missing = sorted(set(expected_keys) - set(proof))
        if unknown:
            reasons.append(f"proof has unknown fields: {unknown}")
        if missing:
            reasons.append(f"proof missing fields: {missing}")
        return None
    digest = hashlib.sha256(canonical_bytes(proof)).hexdigest()
    if proof.get("schema_version") != PROOF_SCHEMA_VERSION:
        reasons.append("unsupported proof schema_version")
    if proof.get("proof_type") != proof_type:
        reasons.append("proof_type mismatch")
    if proof.get("authority_id") != authority_id:
        reasons.append("authority_id mismatch")
    if proof.get("trust_class") != OWNER_TRUST_CLASS:
        reasons.append("trust_class is not owner-provisioned")
    if not isinstance(proof.get("run_id"), str) or not proof.get("run_id"):
        reasons.append("run_id missing")
    if not isinstance(proof.get("provenance"), dict) or not proof["provenance"]:
        reasons.append("provenance missing")
    generated = _parse_iso_timestamp(proof.get("generated_at"))
    if generated is None:
        reasons.append("generated_at is not an ISO-8601 tz-aware timestamp")
    else:
        if generated > now + CLOCK_SKEW:
            reasons.append("generated_at is in the future")
        if generated < now - timedelta(days=max_age_days):
            reasons.append("generated_at is older than max_age_days")
        if commit_time is None:
            reasons.append("authoring commit time unverifiable")
        elif generated < commit_time - COMMIT_TIME_GRACE:
            reasons.append("generated_at predates the bound commit")
    if proof.get("evaluated_git_sha") != current_git_sha:
        reasons.append("evaluated_git_sha does not match the checked-out HEAD")
    if not _integrity_ok(proof, hmac_key):
        reasons.append("integrity mac mismatch")
    return digest


def verify_rt101_proof(proof, *, requirement: Mapping, current_git_sha: str,
                       spec_sha256: str, decision_register_sha256: str,
                       manifest_id: str, identity_snapshot_id: str,
                       expected_holdout_lock_sha256: str, hmac_key: str,
                       now: datetime, commit_time: datetime | None) -> AuthorityResult:
    """Verify an owner-provisioned RT-101 release-holdout authority proof.

    Every binding is compared against independently read values; a mismatch
    only records a sanitized reason (never the conflicting value).
    """
    reasons: list[str] = []
    max_age_days = requirement.get("max_age_days")
    max_age_days = max_age_days if isinstance(max_age_days, int) and max_age_days > 0 else 180
    digest = _common_proof_checks(
        proof, expected_keys=PROOF_KEYS_RT101,
        proof_type=str(requirement.get("proof_type") or RT101_PROOF_TYPE),
        authority_id=RT101_AUTHORITY_ID, hmac_key=hmac_key, now=now,
        max_age_days=max_age_days, current_git_sha=current_git_sha,
        commit_time=commit_time, reasons=reasons)
    if isinstance(proof, dict) and set(proof) == set(PROOF_KEYS_RT101):
        if not _is_hex(proof.get("holdout_lock_sha256", ""), 64):
            reasons.append("holdout_lock_sha256 is not a sha256 digest")
        elif not _is_hex(expected_holdout_lock_sha256, 64):
            reasons.append("expected holdout lock digest not provisioned")
        elif proof["holdout_lock_sha256"] != expected_holdout_lock_sha256:
            reasons.append("holdout_lock_sha256 does not match the provisioned lock")
        if not _is_hex(proof.get("spec_sha256", ""), 64):
            reasons.append("spec_sha256 is not a sha256 digest")
        elif proof["spec_sha256"] != spec_sha256:
            reasons.append("spec_sha256 does not match the checked-out spec manifest")
        if not _is_hex(proof.get("decision_register_sha256", ""), 64):
            reasons.append("decision_register_sha256 is not a sha256 digest")
        elif proof["decision_register_sha256"] != decision_register_sha256:
            reasons.append("decision_register_sha256 does not match the spec manifest")
        if proof.get("manifest_id") != manifest_id:
            reasons.append("manifest_id does not match the locked benchmark manifest")
        if proof.get("identity_snapshot_id") != identity_snapshot_id:
            reasons.append("identity_snapshot_id does not match the locked benchmark manifest")
    if reasons:
        return AuthorityResult(authority_id=RT101_AUTHORITY_ID, satisfied=False,
                               trust_class=(proof.get("trust_class")
                                            if isinstance(proof, dict) else None),
                               proof_digest=digest, reasons=tuple(reasons))
    return AuthorityResult(authority_id=RT101_AUTHORITY_ID, satisfied=True,
                           trust_class=proof["trust_class"], proof_digest=digest,
                           reasons=())


def verify_external_satisfaction_proof(proof, *, control_id: str,
                                       current_git_sha: str, hmac_key: str,
                                       now: datetime,
                                       commit_time: datetime | None) -> AuthorityResult:
    """Verify an owner-provisioned external-control satisfaction proof."""
    reasons: list[str] = []
    digest = _common_proof_checks(
        proof, expected_keys=PROOF_KEYS_EXTERNAL,
        proof_type=EXTERNAL_SATISFACTION_PROOF_TYPE, authority_id=control_id,
        hmac_key=hmac_key, now=now, max_age_days=180,
        current_git_sha=current_git_sha, commit_time=commit_time,
        reasons=reasons)
    artifact_sha256 = None
    if isinstance(proof, dict) and set(proof) == set(PROOF_KEYS_EXTERNAL):
        if proof.get("decision") != "SATISFIED":
            reasons.append("decision must be SATISFIED")
        if not _is_hex(proof.get("artifact_sha256", ""), 64):
            reasons.append("artifact_sha256 is not a sha256 digest")
        else:
            artifact_sha256 = proof["artifact_sha256"]
    if reasons:
        return AuthorityResult(authority_id=control_id, satisfied=False,
                               trust_class=(proof.get("trust_class")
                                            if isinstance(proof, dict) else None),
                               proof_digest=digest,
                               artifact_sha256=artifact_sha256,
                               reasons=tuple(reasons))
    return AuthorityResult(authority_id=control_id, satisfied=True,
                           trust_class=proof["trust_class"], proof_digest=digest,
                           artifact_sha256=artifact_sha256, reasons=())


def validate_authority_requirements(requirements: Mapping) -> None:
    """Policy ``required_authorities`` may only declare requirements.

    Raises ValueError on legacy status-string maps, satisfaction-claiming
    keys anywhere in a requirement subtree, unknown/missing requirement
    fields, or a non-owner trust class requirement.
    """
    if requirements is None:
        return
    if not isinstance(requirements, dict):
        raise ValueError("required_authorities must be a mapping of requirement objects")
    for authority_id, requirement in requirements.items():
        where = f"required_authorities[{authority_id}]"

        def _scan(node, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in FORBIDDEN_REQUIREMENT_KEYS:
                        raise ValueError(f"{path}: satisfaction-claiming field '{key}' is forbidden")
                    _scan(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    _scan(value, f"{path}[{index}]")
            elif isinstance(node, str) and node in {"SATISFIED", "UNAVAILABLE", "APPROVED"}:
                raise ValueError(f"{path}: legacy status value '{node}' is forbidden")

        _scan(requirement, where)
        if not isinstance(requirement, dict):
            raise ValueError(f"{where}: requirement must be an object (legacy status maps are rejected)")
        # D7 review F9: exact top-level allowlist — an unexpected extra
        # field (a smuggling channel for satisfaction claims) fails closed.
        allowed_fields = {"description", "proof_type", "schema_version",
                          "trust_class_required", "bindings", "max_age_days",
                          "expected_holdout_lock_env",
                          "manifest_identity_source"}
        unexpected_fields = sorted(set(requirement) - allowed_fields)
        if unexpected_fields:
            raise ValueError(f"{where}: unexpected requirement field(s) "
                             f"{unexpected_fields}")
        for field in ("proof_type", "schema_version", "trust_class_required",
                      "max_age_days", "expected_holdout_lock_env",
                      "manifest_identity_source"):
            if field not in requirement:
                raise ValueError(f"{where}: missing requirement field '{field}'")
        if requirement["schema_version"] != PROOF_SCHEMA_VERSION:
            raise ValueError(f"{where}: unsupported requirement schema_version")
        if requirement["trust_class_required"] != OWNER_TRUST_CLASS:
            raise ValueError(f"{where}: trust_class_required must be {OWNER_TRUST_CLASS}")
        if requirement["proof_type"] != RT101_PROOF_TYPE:
            raise ValueError(f"{where}: unsupported proof_type")
        if not (isinstance(requirement["max_age_days"], int)
                and 0 < requirement["max_age_days"] <= 3650):
            raise ValueError(f"{where}: max_age_days must be a positive integer")
        bindings = requirement.get("bindings")
        if not isinstance(bindings, list) or not set(bindings) >= MANDATORY_BINDINGS_RT101:
            raise ValueError(f"{where}: bindings must include {sorted(MANDATORY_BINDINGS_RT101)}")
        if not isinstance(requirement["expected_holdout_lock_env"], str) or \
                not requirement["expected_holdout_lock_env"]:
            raise ValueError(f"{where}: expected_holdout_lock_env must name the owner env var")
        if not isinstance(requirement["manifest_identity_source"], str) or \
                not requirement["manifest_identity_source"]:
            raise ValueError(f"{where}: manifest_identity_source must name the locked fixture")


def _read_json_env(env: Mapping[str, str], name: str) -> tuple[bool, object]:
    raw = env.get(name)
    if not raw or not raw.strip():
        return False, None
    try:
        return True, json.loads(raw)
    except ValueError:
        return True, None


def _manifest_identity(root: Path, requirement: Mapping) -> tuple[str, str, str, str]:
    """Read (spec_sha256, decision_register_sha256, manifest_id, identity_snapshot_id)."""
    spec = json.loads((root / "spec/spec_manifest.json").read_text("utf-8"))
    fixture = json.loads((root / requirement["manifest_identity_source"]).read_text("utf-8"))
    return (str(spec["spec_sha256"]), str(spec["decision_register_sha256"]),
            str(fixture["manifest_id"]), str(fixture["identity_snapshot_id"]))


def authority_results_from_env(requirements: Mapping, *, root: Path,
                               env: Mapping[str, str] | None = None,
                               now: datetime | None = None) -> dict[str, AuthorityResult]:
    """Production provider: RT-101 authority comes ONLY from the environment.

    Absent/empty environment inputs yield unsatisfied results (fail closed).
    This function never reads repo files as proof sources.
    """
    env = dict(env if env is not None else __import__("os").environ)
    now = now or datetime.now(timezone.utc)
    results: dict[str, AuthorityResult] = {}
    for authority_id in sorted(requirements):
        requirement = requirements[authority_id]
        head = git_head(root)
        present, payload = _read_json_env(env, ENV_RT101_PROOF)
        key = env.get(ENV_RT101_HMAC_KEY, "")
        expected_lock = env.get(ENV_RT101_EXPECTED_HOLDOUT_LOCK, "")
        if _production_key_rejected(key):
            results[authority_id] = unsatisfied(
                authority_id,
                "owner HMAC key rejected (absent, weak, or publicly-known "
                "test material)")
            continue
        if not present:
            results[authority_id] = unsatisfied(
                authority_id, "authority proof not provisioned (environment absent)")
            continue
        spec_sha, decision_sha, manifest_id, snapshot_id = _manifest_identity(root, requirement)
        commit_time = git_commit_time(root, head) if head else None
        if payload is None:
            results[authority_id] = unsatisfied(authority_id, "authority proof is not valid JSON")
            continue
        results[authority_id] = verify_rt101_proof(
            payload, requirement=requirement, current_git_sha=head,
            spec_sha256=spec_sha, decision_register_sha256=decision_sha,
            manifest_id=manifest_id, identity_snapshot_id=snapshot_id,
            expected_holdout_lock_sha256=expected_lock, hmac_key=key,
            now=now, commit_time=commit_time)
    return results


def external_satisfaction_proofs_from_env(*, root: Path,
                                          env: Mapping[str, str] | None = None,
                                          now: datetime | None = None) -> dict[str, AuthorityResult]:
    """Production provider: external-control satisfactions ONLY from env."""
    env = dict(env if env is not None else __import__("os").environ)
    now = now or datetime.now(timezone.utc)
    present, payload = _read_json_env(env, ENV_EXTERNAL_PROOFS)
    if not present:
        return {}
    if not isinstance(payload, dict):
        return {}
    key = env.get(ENV_EXTERNAL_HMAC_KEY, "")
    key_rejected = _production_key_rejected(key)
    if key_rejected:
        key = ""
    head = git_head(root)
    commit_time = git_commit_time(root, head) if head else None
    results: dict[str, AuthorityResult] = {}
    for control_id, proof in sorted(payload.items()):
        results[control_id] = verify_external_satisfaction_proof(
            proof, control_id=control_id, current_git_sha=head,
            hmac_key=key, now=now, commit_time=commit_time)
    return results
