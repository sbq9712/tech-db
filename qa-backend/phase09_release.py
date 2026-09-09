"""Phase09 machine evidence, release eligibility, and ticket status authority.

The evaluator in this module is intentionally fail closed.  It consumes
machine-produced suite/benchmark artifacts; it never accepts a README checkbox,
PR label, or caller-provided ``all_done`` value as release evidence.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from phase09_authority import (
    AuthorityResult,
    RT101_AUTHORITY_ID,
    authority_results_from_env,
    external_satisfaction_proofs_from_env,
    git_head,
    validate_authority_requirements,
)


SCHEMA_VERSION = "phase09-release-evidence-1.1"
BENCHMARK_SCHEMA_VERSION = "phase09-benchmark-1.0"
STATUS_SCHEMA_VERSION = "phase09-ticket-status-1.0"
VALID_RESULTS = {"PASS", "FAIL", "SKIP", "XFAIL", "CANCELLED", "MISSING"}
REQUIRED_PROVENANCE = {
    "git_sha", "spec_sha256", "decision_register_sha256", "manifest_id",
    "dataset_sha256", "identity_snapshot_id", "model", "prompt_sha256",
    "schema_version", "config_sha256",
}
PHASE09_TICKETS = tuple(f"RT-{number}" for number in range(100, 109))
REQUIRED_EXTERNAL_CONTROLS = {"Q-336", "RT-005", "RT-075"}
EXTERNAL_STATE = Path(__file__).resolve().parent.parent / "spec/phase09_external_state.json"

# D7 (P0 closure): RT-101 is mandatory at the code level.  Repo-editable
# policy may declare additional required authorities but can never remove
# or under-declare this set, and can never declare any of them satisfied.
MANDATORY_AUTHORITIES = frozenset({RT101_AUTHORITY_ID})


def load_external_blockers(path: Path = EXTERNAL_STATE,
                           *, owner_proofs: Mapping[str, AuthorityResult] | None = None
                           ) -> dict[str, str]:
    """Derive external blockers from validated evidence, never caller optimism.

    D7 hardening: a ``satisfied: true`` row additionally requires an
    owner-provisioned satisfaction proof (environment channel, HMAC-bound
    to the declared artifact digest).  A self-referential proof committed
    inside the repository is repo-controlled and clears nothing.
    """
    root = Path(path).resolve().parent.parent
    payload = json.loads(Path(path).read_text("utf-8"))
    if payload.get("schema_version") != "phase09-external-state-1.0":
        raise ValueError("unsupported Phase09 external-state schema")
    rows = payload.get("controls")
    if not isinstance(rows, dict):
        raise ValueError("Phase09 external controls missing")
    if set(rows) != REQUIRED_EXTERNAL_CONTROLS:
        raise ValueError("Phase09 external control set is incomplete or unregistered")
    blockers = {}
    for blocker_id, row in rows.items():
        if not isinstance(row, dict) or not row.get("description"):
            raise ValueError(f"{blocker_id}: invalid external evidence row")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("observed_at"):
            raise ValueError(f"{blocker_id}: evidence and observed_at required")
        satisfied = row.get("satisfied") is True
        proof = row.get("satisfaction_proof")
        if satisfied and (not isinstance(proof, dict) or
                          not proof.get("artifact") or
                          not proof.get("sha256")):
            raise ValueError(f"{blocker_id}: cannot clear without hashed proof")
        if satisfied:
            declared = str(proof["sha256"])
            artifact = Path(path).parent.parent / str(proof["artifact"])
            if (len(declared) != 64 or
                    any(c not in "0123456789abcdef" for c in declared) or
                    not artifact.is_file() or
                    hashlib.sha256(artifact.read_bytes()).hexdigest() != declared):
                raise ValueError(f"{blocker_id}: satisfaction proof is missing or stale")
            owner = (owner_proofs or {}).get(blocker_id)
            if (owner is None or not owner.satisfied
                    or owner.authority_id != blocker_id
                    or owner.artifact_sha256 != declared):
                raise ValueError(f"{blocker_id}: cannot clear without owner-provisioned "
                                 "satisfaction proof (repo files alone never clear blockers)")
        if not satisfied:
            blockers[blocker_id] = str(row["description"])
    return blockers


EXTERNAL_BLOCKERS = load_external_blockers(
    owner_proofs=external_satisfaction_proofs_from_env(root=EXTERNAL_STATE.parent.parent))


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_value(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_git_sha(root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        capture_output=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build_provenance(*, root: Path, dataset: Path, manifest_id: str,
                     identity_snapshot_id: str, model: str,
                     prompt_config, runtime_config,
                     git_sha: str | None = None) -> dict:
    """Build complete benchmark provenance from the checked-out authority."""
    root = Path(root)
    spec = json.loads((root / "spec/spec_manifest.json").read_text("utf-8"))
    return {
        "git_sha": git_sha or current_git_sha(root),
        "spec_sha256": spec["spec_sha256"],
        "decision_register_sha256": spec["decision_register_sha256"],
        "manifest_id": str(manifest_id),
        "dataset_sha256": file_sha256(dataset),
        "identity_snapshot_id": str(identity_snapshot_id),
        "model": str(model),
        "prompt_sha256": sha256_value(prompt_config),
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "config_sha256": sha256_value(runtime_config),
    }


def validate_provenance(provenance: Mapping, *, expected: Mapping | None = None,
                        require_git_sha: bool = True) -> list[str]:
    issues = []
    missing = sorted(REQUIRED_PROVENANCE - set(provenance or {}))
    if missing:
        issues.append("missing provenance fields: " + ",".join(missing))
    for key in ("spec_sha256", "decision_register_sha256", "dataset_sha256",
                "prompt_sha256", "config_sha256"):
        value = str((provenance or {}).get(key, ""))
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            issues.append(f"{key}: invalid sha256")
    git_sha = str((provenance or {}).get("git_sha", ""))
    if require_git_sha and (len(git_sha) != 40 or
                            any(c not in "0123456789abcdef" for c in git_sha)):
        issues.append("git_sha: invalid commit sha")
    for key in ("manifest_id", "identity_snapshot_id", "model", "schema_version"):
        if not str((provenance or {}).get(key, "")).strip():
            issues.append(f"{key}: empty")
    for key, wanted in (expected or {}).items():
        if provenance.get(key) != wanted:
            issues.append(f"{key}: stale or wrong provenance")
    return issues


@dataclass(frozen=True)
class SuiteEvidence:
    name: str
    result: str
    required: bool = True
    artifact: str = ""
    provenance: Mapping = field(default_factory=dict)
    semantic_regression: bool = False
    infrastructure_flake: bool = False

    def issues(self, *, expected_provenance: Mapping | None = None) -> list[str]:
        issues = []
        if self.result not in VALID_RESULTS:
            issues.append(f"{self.name}: invalid result {self.result}")
        if self.required and self.result != "PASS":
            issues.append(f"{self.name}: required suite {self.result.lower()}")
        if self.semantic_regression:
            issues.append(f"{self.name}: semantic regression")
        if self.required and not self.artifact:
            issues.append(f"{self.name}: required artifact missing")
        if self.artifact:
            issues.extend(f"{self.name}: {issue}" for issue in
                          validate_provenance(self.provenance,
                                              expected=expected_provenance))
        # An infra label is diagnostic only and can never erase a semantic
        # regression or a non-PASS required result.
        if self.infrastructure_flake and self.semantic_regression:
            issues.append(f"{self.name}: infra classification cannot erase semantic regression")
        return issues

    def to_dict(self) -> dict:
        return {
            "name": self.name, "result": self.result,
            "required": self.required, "artifact": self.artifact,
            "provenance": dict(self.provenance),
            "semantic_regression": self.semantic_regression,
            "infrastructure_flake": self.infrastructure_flake,
        }


@dataclass(frozen=True)
class ReleaseDecision:
    core_eligible: bool
    production_release_eligible: bool
    graph_activation_eligible: bool
    graph_state: str
    reasons: tuple[str, ...]
    external_blockers: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "core_eligible": self.core_eligible,
            "production_release_eligible": self.production_release_eligible,
            "graph_activation_eligible": self.graph_activation_eligible,
            "graph_state": self.graph_state,
            "reasons": list(self.reasons),
            "external_blockers": list(self.external_blockers),
        }


def evaluate_release(*, required_suites: Iterable[str],
                     evidence: Iterable[SuiteEvidence],
                     expected_provenance: Mapping,
                     hard_invariants: Mapping[str, bool],
                     graph_gain_conclusion: str,
                     authority_requirements: Mapping[str, Mapping] | None = None,
                     authority_results: Mapping[str, AuthorityResult] | None = None,
                     external_blockers: Mapping[str, str] | None = None) -> ReleaseDecision:
    """Single canonical Phase09 evaluator.

    Graph eligibility is independent from core eligibility.  External
    production blockers prevent production release but do not falsify the
    result of deterministic code-local gates.

    D7 (P0 closure): required authorities are consumed as
    (requirement declarations, verified results) pairs.  Requirement
    objects come from repo-editable policy but can only *declare* what is
    required — satisfaction-claiming fields raise.  Satisfaction comes
    only from verified owner-provisioned results (environment channel).
    Legacy status-string maps are rejected.
    """
    validate_authority_requirements(authority_requirements or {})
    rows = {row.name: row for row in evidence}
    reasons = []
    for name in sorted(set(required_suites)):
        row = rows.get(name)
        if row is None:
            reasons.append(f"{name}: required suite missing")
            continue
        reasons.extend(row.issues(expected_provenance=expected_provenance))
    for name, passed in sorted(hard_invariants.items()):
        if passed is not True:
            reasons.append(f"hard invariant failed: {name}")
    requirements = dict(authority_requirements or {})
    for name in sorted(MANDATORY_AUTHORITIES | set(requirements)):
        if name not in requirements:
            reasons.append(f"required authority undeclared in policy: {name}")
            continue
        result = (authority_results or {}).get(name)
        if result is None:
            reasons.append(f"required authority unsatisfied: {name}: no verified result")
        elif result.authority_id != name:
            reasons.append(f"required authority unsatisfied: {name}: result identity mismatch")
        elif not result.satisfied:
            detail = "; ".join(result.reasons) or "unverified"
            reasons.append(f"required authority unsatisfied: {name}: {detail}")
    core_eligible = not reasons
    blockers = tuple(sorted((external_blockers or EXTERNAL_BLOCKERS).keys()))
    graph_ok = graph_gain_conclusion == "GAIN"
    graph_state = "ON_ELIGIBLE" if graph_ok else "OFF_NO_GAIN"
    production_eligible = core_eligible and not blockers
    return ReleaseDecision(
        core_eligible=core_eligible,
        production_release_eligible=production_eligible,
        graph_activation_eligible=graph_ok,
        graph_state=graph_state,
        reasons=tuple(reasons), external_blockers=blockers)


def validate_benchmark_artifact(payload: Mapping,
                                *, expected_provenance: Mapping | None = None) -> list[str]:
    issues = []
    if payload.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        issues.append("unsupported benchmark schema")
    if payload.get("verdict") not in {"PASS", "FAIL"}:
        issues.append("invalid benchmark verdict")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        issues.append("benchmark metrics missing")
    else:
        for name, metric in metrics.items():
            if not isinstance(metric, dict):
                issues.append(f"{name}: metric is not an object")
                continue
            for field_name in ("value", "threshold", "direction", "passed"):
                if field_name not in metric:
                    issues.append(f"{name}: missing {field_name}")
    issues.extend(validate_provenance(payload.get("provenance", {}),
                                      expected=expected_provenance))
    return issues


def derive_ticket_status(*, matrix: Mapping,
                         suite_results: Mapping[str, str],
                         artifact_results: Mapping[str, Mapping],
                         external_blockers: Mapping[str, str] | None = None,
                         authority_results: Mapping[str, AuthorityResult] | None = None) -> dict:
    """Generate Phase09 status only from registered executable evidence.

    D7: RT-101 completion additionally requires a verified owner-provisioned
    release authority result; acceptance-matrix text alone (repo-editable)
    can never mark RT-101 satisfied.
    """
    blockers = external_blockers or EXTERNAL_BLOCKERS
    registered_suites = matrix.get("suite_registry", {})
    entries = {entry.get("ticket_id"): entry for entry in
               matrix.get("remediation_entries", [])}
    tickets = {}
    for ticket_id in PHASE09_TICKETS:
        entry = entries.get(ticket_id)
        reasons = []
        evidence_rows = []
        if entry is None:
            reasons.append("acceptance entry missing")
        else:
            for dod in entry.get("dods", []):
                blocked_dod = dod.get("status") == "BLOCKED_EXTERNAL_ACTION"
                if blocked_dod:
                    # External dependency is a truthful non-completion class,
                    # but it is not a code/test failure.  Preserve it below as
                    # a blocker after validating its named executable evidence.
                    pass
                elif dod.get("status") != "SATISFIED":
                    reasons.append(f"{dod.get('dod_id')}: {dod.get('status')}")
                refs = dod.get("test_cases", [])
                if not refs and not blocked_dod:
                    reasons.append(f"{dod.get('dod_id')}: no executable evidence")
                for ref in refs:
                    suite = ref.get("suite", "")
                    if suite not in registered_suites:
                        reasons.append(f"{suite}: unregistered suite")
                    result = suite_results.get(suite, "MISSING")
                    if result != "PASS":
                        reasons.append(f"{suite}: {result}")
                    artifact = ref.get("artifact")
                    if artifact:
                        payload = artifact_results.get(artifact)
                        if payload is None:
                            reasons.append(f"{artifact}: missing artifact")
                        else:
                            artifact_issues = validate_benchmark_artifact(payload)
                            reasons.extend(f"{artifact}: {issue}"
                                           for issue in artifact_issues)
                    evidence_rows.append({
                        "dod_id": dod.get("dod_id"), "suite": suite,
                        "case": ref.get("case"), "result": result,
                        "artifact": artifact or "",
                    })
        dependency_blockers = []
        if ticket_id == "RT-103" and "RT-075" in blockers:
            dependency_blockers.append("RT-075")
        if ticket_id == "RT-106" and "Q-336" in blockers:
            dependency_blockers.append("Q-336")
        if ticket_id == "RT-101":
            # D7: repo-editable acceptance-matrix text can never satisfy
            # RT-101; a verified owner-provisioned authority result is
            # required regardless of what the matrix declares.
            rt101 = (authority_results or {}).get(RT101_AUTHORITY_ID)
            if rt101 is None or not rt101.satisfied:
                reasons.append("RT-101 release authority not satisfied "
                               "(owner-provisioned proof required)")
        if reasons:
            status = "NOT_SATISFIED"
        elif dependency_blockers:
            status = "BLOCKED_EXTERNAL_ACTION"
        else:
            status = "SATISFIED"
        tickets[ticket_id] = {
            "status": status,
            "reasons": sorted(set(reasons)),
            "dependency_blockers": dependency_blockers,
            "evidence": evidence_rows,
        }
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickets": tickets,
        "phase_status": (
            "NOT_SATISFIED" if any(v["status"] == "NOT_SATISFIED"
                                   for v in tickets.values())
            else "PASS_WITH_EXTERNAL_BLOCKER" if any(
                v["status"] == "BLOCKED_EXTERNAL_ACTION"
                for v in tickets.values())
            else "PASS"),
        "external_blockers": dict(blockers),
    }


def write_json(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               indent=2) + "\n", "utf-8")
