"""RT-094 server-authorized audit/trace projection.

This service reads only the canonical retained Trace store, re-applies the
production allowlist, enforces retention and snapshot scope, and labels replay
fidelity using RT-092.  Frontend visibility is never authorization.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eval.replay import classify_replay_fidelity
from trace_retention import DEFAULT_RETENTION_DAYS, project_production_trace


class AuditAuthorizationError(PermissionError):
    pass


class AuditTraceUnavailable(LookupError):
    pass


class TraceAuditService:
    def __init__(self, trace_dir: Path, *, operator_key: str,
                 retention_days: int = DEFAULT_RETENTION_DAYS):
        self.trace_dir = Path(trace_dir)
        self._operator_key = str(operator_key or "")
        self.retention_days = int(retention_days)

    def _authenticate(self, supplied_key: str) -> None:
        if not self._operator_key or not supplied_key or not hmac.compare_digest(
                self._operator_key, str(supplied_key)):
            raise AuditAuthorizationError("operator authorization required")

    def _find(self, trace_id: str) -> dict:
        wanted = str(trace_id or "")
        if not wanted or len(wanted) > 128:
            raise AuditTraceUnavailable("trace unavailable")
        for path in sorted(self.trace_dir.glob("*.jsonl"), reverse=True):
            if path.name == "cleanup_audit.jsonl":
                continue
            try:
                rows = path.read_text("utf-8").splitlines()
            except OSError:
                continue
            for line in rows:
                try:
                    row = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if row.get("trace_id") == wanted:
                    return row
        raise AuditTraceUnavailable("trace unavailable")

    def view(self, supplied_key: str, trace_id: str, *,
             permitted_snapshot_ids: set[str] | None = None,
             now: datetime | None = None) -> dict:
        self._authenticate(supplied_key)
        raw = self._find(trace_id)
        now = now or datetime.now(timezone.utc)
        try:
            created = datetime.fromisoformat(str(raw.get("timestamp") or ""))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            raise AuditTraceUnavailable("trace timestamp invalid")
        if created < now - timedelta(days=self.retention_days):
            raise AuditTraceUnavailable("trace retention expired")

        projected = project_production_trace(raw)
        allowed = permitted_snapshot_ids
        bound_snapshot = str(projected.get("identity_snapshot_id") or "")
        if allowed is not None and bound_snapshot and bound_snapshot not in allowed:
            projected["identity_snapshot_id"] = "REDACTED_BY_ACCESS_SCOPE"
            projected["stages"] = []
            projected["result"] = {
                "reason_code": "RESTRICTED_SNAPSHOT_REDACTED",
            }
        replay_case = {
            "trace_id": projected.get("trace_id"),
            "manifest_id": projected.get("manifest_id"),
            "profile": projected.get("profile"),
            "historical_artifacts_available": bool(projected.get("manifest_id")),
        }
        fidelity = classify_replay_fidelity(
            replay_case, historical_model_available=False)
        return {
            "schema_version": "operator-audit-view-1.0",
            "trace": projected,
            "replay": fidelity,
            "raw_trace_exposed": False,
            "authorization": "OPERATOR",
        }
