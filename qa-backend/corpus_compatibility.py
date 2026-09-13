"""RT-101 corpus compatibility gate — blinded-safe machine preflight.

Root cause of the RT-101 V5 formal-holdout failure (consumed, immutable):
the candidate was built over a raw filesystem sources tree while the
evaluated runtime served the ingested, citation-eligible record corpus.
No machine check compared the two universes before the one-shot formal
run was consumed, so an impossible-in-principle benchmark ran for ~74
minutes before failing.

This module is the permanent anti-recurrence gate (phase09 remediation,
corpus adjudication ROOT_CAUSE_CLASS=B).  Contract:

  * the candidate side binds: manifest_id, dataset_snapshot_id,
    source_snapshot_catalog_id, identity_snapshot_id, corpus_sha256,
    evaluated_git_sha target, model and prompt/schema/config versions;
  * the runtime side is measured from the live index artifacts and must
    match EXACTLY (or via a canonical compatibility rule explicitly
    allowed by the release policy — currently: exact equality only);
  * hidden-evidence membership crosses the builder→implementation
    boundary ONLY as aggregate counters.  This module never accepts or
    returns locator text, per-case identifiers, question text, or any
    other gold-derived content;
  * a corpus mismatch raises before any formal traffic — the formal
    runner MUST call :func:`assert_formal_run_allowed` before consuming
    the one-shot marker.

Blinding properties (tested in tests_corpus_gates_phase09.py):
  * evaluate() output contains only booleans, aggregate integers and
    identity digests;
  * no function in this module has a parameter or return path carrying
    hidden-case content; membership is folded to counters by the caller
    (builder side) and enters here already aggregated.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

SCHEMA_VERSION = "rt101-corpus-compatibility-1.0"


class CorpusCompatibilityError(RuntimeError):
    """Raised when a formal run is attempted over an incompatible corpus."""


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def source_catalog_digest(rows: Iterable[Mapping]) -> str:
    """Stable digest over the source-snapshot catalog identity rows.

    ``rows`` are mappings with at least ``source_snapshot_id``,
    ``record_id`` and a content hash field (``content_hash`` or
    ``evidence_text_sha256``).  The digest is computed over the
    sorted canonical JSON of exactly those identity fields, so the
    catalog id is stable across storage backends (SQLite DB bytes may
    differ through page layout; the identity set may not).
    """
    identity = []
    for row in rows:
        identity.append({
            "source_snapshot_id": str(row.get("source_snapshot_id", "")),
            "record_id": str(row.get("record_id", "")),
            "content_hash": str(
                row.get("content_hash")
                if row.get("content_hash") is not None
                else row.get("evidence_text_sha256", "")),
        })
    identity.sort(key=lambda r: (r["source_snapshot_id"], r["record_id"],
                                 r["content_hash"]))
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def corpus_digest(rows: Iterable[Mapping]) -> str:
    """Alias of :func:`source_catalog_digest` (semantic alias for readers)."""
    return source_catalog_digest(rows)


@dataclass(frozen=True)
class CorpusBinding:
    """Version-pinned identity of one corpus universe (candidate OR runtime)."""

    manifest_id: str
    dataset_snapshot_id: str
    source_snapshot_catalog_id: str
    identity_snapshot_id: str
    corpus_sha256: str
    model: str = ""
    prompt_schema_config_versions: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "manifest_id": self.manifest_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "source_snapshot_catalog_id": self.source_snapshot_catalog_id,
            "identity_snapshot_id": self.identity_snapshot_id,
            "corpus_sha256": self.corpus_sha256,
            "model": self.model,
            "prompt_schema_config_versions": dict(
                self.prompt_schema_config_versions),
        }


@dataclass(frozen=True)
class MembershipAggregate:
    """Builder-side hidden-evidence membership, pre-aggregated.

    The builder knows hidden evidence truth; the implementation side may
    only see these counters.  ``answer_cases_checked`` counts the
    answer-required cases whose hidden evidence locator exists in the
    pinned source catalog with matching content hash; ``absence_cases``
    counts abstention-family cases validated against their pre-registered
    absence/conflict/insufficiency semantics.
    """

    answer_cases_checked: int
    answer_cases_member: int
    absence_cases_checked: int
    absence_cases_valid: int

    @property
    def missing_hidden_sources(self) -> int:
        return self.answer_cases_checked - self.answer_cases_member

    @property
    def invalid_absence_semantics(self) -> int:
        return self.absence_cases_checked - self.absence_cases_valid

    def to_dict(self) -> dict:
        return {
            "answer_cases_checked": self.answer_cases_checked,
            "answer_cases_member": self.answer_cases_member,
            "absence_cases_checked": self.absence_cases_checked,
            "absence_cases_valid": self.absence_cases_valid,
            "missing_hidden_sources": self.missing_hidden_sources,
            "invalid_absence_semantics": self.invalid_absence_semantics,
        }


def aggregate_membership(
    answer_membership_flags: Sequence[bool],
    absence_semantic_flags: Sequence[bool],
) -> MembershipAggregate:
    """Fold builder-side per-case booleans into the safe aggregate.

    This is the ONLY shape in which hidden-evidence truth may cross the
    builder→implementation boundary.
    """
    return MembershipAggregate(
        answer_cases_checked=len(answer_membership_flags),
        answer_cases_member=sum(1 for f in answer_membership_flags if f),
        absence_cases_checked=len(absence_semantic_flags),
        absence_cases_valid=sum(1 for f in absence_semantic_flags if f),
    )


def evaluate(
    candidate: CorpusBinding,
    runtime: CorpusBinding,
    membership: MembershipAggregate,
    *,
    evaluated_git_sha_target: str = "",
    evaluated_git_sha_runtime: str = "",
    min_answer_cases: int = 1,
) -> dict:
    """Blinded-safe compatibility decision (aggregate output only).

    Every check is exact-equality identity binding plus the membership
    aggregate.  The report never contains locator or case content.
    """
    identity_checks = {
        "manifest_id_exact":
            candidate.manifest_id == runtime.manifest_id,
        "dataset_snapshot_id_exact":
            candidate.dataset_snapshot_id == runtime.dataset_snapshot_id,
        "source_snapshot_catalog_id_exact":
            candidate.source_snapshot_catalog_id ==
            runtime.source_snapshot_catalog_id,
        "identity_snapshot_id_exact":
            candidate.identity_snapshot_id == runtime.identity_snapshot_id,
        "corpus_sha256_exact":
            candidate.corpus_sha256 == runtime.corpus_sha256,
        "model_exact":
            (not candidate.model or not runtime.model
             or candidate.model == runtime.model),
    }
    head_check = {
        "evaluated_git_sha_bound": (
            not evaluated_git_sha_target
            or not evaluated_git_sha_runtime
            or evaluated_git_sha_target == evaluated_git_sha_runtime),
    }
    membership_checks = {
        "hidden_source_membership_complete":
            membership.answer_cases_checked >= min_answer_cases
            and membership.missing_hidden_sources == 0,
        "absence_semantics_valid":
            membership.invalid_absence_semantics == 0,
    }
    checks: dict[str, bool] = {}
    checks.update(identity_checks)
    checks.update(head_check)
    checks.update(membership_checks)
    report = {
        "schema_version": SCHEMA_VERSION,
        "compatible": all(checks.values()),
        "checks": checks,
        "failed_checks": sorted(k for k, v in checks.items() if not v),
        "membership": membership.to_dict(),
        "candidate_binding": candidate.to_dict(),
        "runtime_binding": {
            **runtime.to_dict(),
            # runtime binding carries the measured head when provided
            "evaluated_git_sha": evaluated_git_sha_runtime or None,
        },
    }
    return report


def assert_formal_run_allowed(report: Mapping) -> None:
    """Fail-closed gate: raise unless the report proves compatibility.

    Formal runners MUST call this BEFORE consuming any one-shot marker.
    """
    if not isinstance(report, Mapping) or not report.get("compatible"):
        failed = []
        if isinstance(report, Mapping):
            failed = list(report.get("failed_checks") or [])
        raise CorpusCompatibilityError(
            "RT101 corpus compatibility gate: formal run forbidden; "
            f"failed checks={failed or ['report_missing_or_invalid']}")
    membership = report.get("membership") or {}
    if membership.get("missing_hidden_sources") not in (0, None):
        raise CorpusCompatibilityError(
            "RT101 corpus compatibility gate: hidden sources missing")
