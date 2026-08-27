"""
T056 — Trace Retention / Redaction / Audit Storage Policy
==========================================================
Defines which fields are stored in production traces, for how long,
and who can access them.

Default policy:
  - Store record/snapshot/evidence IDs + hashes + necessary short spans
  - Secret/sensitive query/redactable fields are scrubbed
  - Full debug trace requires explicit opt-in and short retention
  - Expired traces cleaned up with audit record
"""
import os
import json
import re
import hashlib
from pathlib import Path
from datetime import datetime, timedelta


REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
TRACE_DIR = RUNTIME_DIR / "traces"

# Retention policy (days)
DEFAULT_RETENTION_DAYS = int(os.environ.get("QA_TRACE_RETENTION_DAYS", "30"))
DEBUG_RETENTION_DAYS = int(os.environ.get("QA_TRACE_DEBUG_RETENTION_DAYS", "7"))

# Fields to always redact
ALWAYS_REDACT = [
    "api_key", "apikey", "secret", "password", "token",
    "authorization", "auth_header", "bearer",
    "ZAI_API_KEY", "zai_api_key",
]

# Fields to keep only in debug mode
DEBUG_ONLY_FIELDS = [
    "full_context", "full_answer_raw", "raw_llm_response",
    "raw_search_results", "full_body_text", "original_query",
    "raw_assistant_history", "full_evidence_package", "generator_draft",
]

_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:ZAI_API_KEY|api[_-]?key|authorization|token|secret|password)\s*[:=]\s*[^\s&]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)([?&](?:token|access_token|api_key|key|secret)=)[^&#\s]+"),
)

# Production persistence is an explicit projection. Only these typed,
# machine-readable fields can cross the Trace storage boundary; arbitrary
# nested application state is never recursively retained.
_TRACE_ID_FIELDS = frozenset({
    "trace_id", "request_id", "manifest_id", "identity_snapshot_id",
    "evidence_package_id", "source_snapshot_id", "record_id",
    "requirement_id", "claim_id", "runtime_manifest_id",
    "provisional_id", "operation_id", "shadow_comparison_id",
})
_TRACE_CODE_FIELDS = frozenset({
    "profile", "profile_version", "answer_state_machine_version",
    "stage", "status", "answer_status", "stop_reason", "reason_code",
    "failure_class", "state_impact", "terminal_upper_bound",
    "fallback_used", "capability", "schema_version", "version", "cause",
    "match_type", "policy_verdict", "cancellation_reason",
    "verification_status", "retention_class", "admission", "route",
    "rewrite_action", "binding_status",
    "resolver_version", "decision", "decision_code", "strong_id_result_code",
})
_TRACE_HASH_FIELDS = frozenset({
    "query_sha256", "evidence_sha256", "evidence_text_sha256",
    "package_hash", "content_hash", "conversation_id_hash",
})
_TRACE_NUMBER_FIELDS = frozenset({
    "query_length", "deadline_ms", "remaining_ms", "pipeline_ms",
    "attempt", "retry_count", "total_claims", "unsupported_major",
    "result_count", "valid", "invalid", "pass", "refs", "round",
    "item_count", "claims_total", "claims_with_independent_support",
    "critical_missing", "conflicted", "scheduled_delay_seconds",
    "retry_after_seconds", "score", "rrf_score", "vec_score",
    "bm25_score", "graph_score", "rank", "legacy_idx",
    "candidate_count", "shadow_latency_ms", "shadow_cost_proxy",
})
_TRACE_BOOL_FIELDS = frozenset({
    "retry", "correctness_critical", "exact_replay_available",
    "raw_retained", "required", "deterministic_sufficient", "hard_fail",
    "mutable_store_read", "graph_v2_activated", "shadow_non_interference",
})
_TRACE_ID_LIST_FIELDS = frozenset({
    "evidence_ids", "cited_record_ids", "critical_missing_ids",
    "record_ids", "requirement_ids", "claim_ids",
    "candidate_ids", "reason_codes", "strong_id_result_codes",
    "override_rule_ids", "block_rule_ids", "shadow_comparison_ids",
})
_TRACE_ROW_LIST_FIELDS = frozenset({
    "degraded_capabilities", "retry_events",
})
_TRACE_TEXT_METADATA_FIELDS = frozenset({"answer", "rewritten_query"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_HASH = re.compile(r"^[0-9a-fA-F]{32,128}$")


def _safe_trace_scalar(key: str, value):
    if key in _TRACE_HASH_FIELDS and isinstance(value, str) \
            and _SAFE_HASH.fullmatch(value):
        return value
    if key in _TRACE_ID_FIELDS and isinstance(value, str) \
            and _SAFE_ID.fullmatch(value):
        return value
    if key in _TRACE_CODE_FIELDS and isinstance(value, str) \
            and _SAFE_CODE.fullmatch(value):
        return value
    if key in _TRACE_NUMBER_FIELDS and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return value
    if key in _TRACE_BOOL_FIELDS and isinstance(value, bool):
        return value
    return None


def _project_trace_data(value: dict) -> dict:
    out = {}
    for key, item in (value or {}).items():
        key = str(key)
        safe = _safe_trace_scalar(key, item)
        if safe is not None:
            out[key] = safe
        elif key in _TRACE_ID_LIST_FIELDS and isinstance(item, list):
            out[key] = [v for v in item[:100]
                        if isinstance(v, str) and _SAFE_ID.fullmatch(v)]
        elif key in _TRACE_ROW_LIST_FIELDS and isinstance(item, list):
            out[key] = [_project_trace_data(v) for v in item[:100]
                        if isinstance(v, dict)]
        elif key in _TRACE_TEXT_METADATA_FIELDS and isinstance(item, str):
            out[key] = {"sha256": _hash_text(item), "length": len(item),
                        "raw_retained": False}
        elif key == "rewrite_authority" and isinstance(item, dict):
            binding = _safe_trace_scalar(
                "binding_status", item.get("binding_status"))
            out[key] = ({"binding_status": binding}
                        if binding is not None else {})
    return out


def project_production_trace(record: dict) -> dict:
    """Build the only JSONL-safe production Trace representation."""
    if not isinstance(record, dict):
        return {}
    projected = {
        "timestamp": str(record.get("timestamp") or "")[:40],
        "retention_class": "production_default",
        "exact_replay_available": False,
    }
    for key in (
        "trace_id", "request_id", "conversation_id_hash", "query_sha256",
        "query_length", "profile", "manifest_id", "identity_snapshot_id",
        "answer_state_machine_version",
    ):
        safe = _safe_trace_scalar(key, record.get(key))
        if safe is not None:
            projected[key] = safe
    projected["stages"] = []
    for stage in (record.get("stages") or [])[:500]:
        if not isinstance(stage, dict):
            continue
        name = _safe_trace_scalar("stage", stage.get("stage"))
        if name is not None:
            projected["stages"].append({
                "stage": name,
                "data": _project_trace_data(stage.get("data") or {}),
            })
    projected["result"] = _project_trace_data(record.get("result") or {})
    return projected


def scrub_secret_values(value):
    """Scrub secrets by key *and value content* at arbitrary nesting."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(secret in str(key).lower() for secret in ALWAYS_REDACT):
                out[key] = "***REDACTED***"
            else:
                out[key] = scrub_secret_values(item)
        return out
    if isinstance(value, list):
        return [scrub_secret_values(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_secret_values(item) for item in value]
    if isinstance(value, BaseException):
        value = str(value)
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_VALUE_PATTERNS:
            text = pattern.sub(lambda m: (
                (m.group(1) + "***REDACTED***")
                if m.lastindex else "***REDACTED***"), text)
        return text
    return value


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _minimal_stage_value(key: str, value):
    """Production trace projection: IDs/codes/metrics, not raw payloads."""
    lower = key.lower()
    stable_digest = lower.endswith(("_sha256", "_hash"))
    if not stable_digest and any(term in lower for term in (
        "query", "prompt", "answer", "context", "history", "body",
        "excerpt", "full_text", "raw_", "llm_response", "draft")):
        if isinstance(value, str):
            return {"sha256": _hash_text(value), "length": len(value),
                    "raw_retained": False}
        if isinstance(value, (list, dict)):
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 default=str)
            return {"sha256": _hash_text(encoded),
                    "item_count": len(value), "raw_retained": False}
    if isinstance(value, dict):
        return {k: _minimal_stage_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        # Keep compact machine-readable IDs/reason-code rows; hash free prose.
        return [_minimal_stage_value(key, item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 240:
        return {"sha256": _hash_text(value), "length": len(value),
                "raw_retained": False}
    return value


def redact_trace(record: dict, debug_mode: bool = False, *,
                 debug_authorized: bool = False,
                 secure_storage: bool = False) -> dict:
    """Redact sensitive fields from a trace record.

    Args:
        record: Trace record dict
        debug_mode: If True, keep debug-only fields

    Returns:
        Redacted trace record
    """
    if not isinstance(record, dict):
        return record

    # A public request flag can never enable raw persistence.  Debug requires
    # server-side authorization AND an approved secure storage class.
    debug_enabled = bool(debug_mode and debug_authorized and secure_storage)
    redacted = {}
    for key, value in record.items():
        # Always redact secrets
        if any(s in key.lower() for s in ALWAYS_REDACT):
            redacted[key] = "***REDACTED***"
            continue

        # Remove debug-only fields in production
        if not debug_enabled and key in DEBUG_ONLY_FIELDS:
            continue

        # Recursively redact nested dicts/lists
        if isinstance(value, dict):
            redacted[key] = redact_trace(
                value, debug_enabled, debug_authorized=debug_authorized,
                secure_storage=secure_storage)
        elif isinstance(value, list):
            redacted[key] = [redact_trace(
                v, debug_enabled, debug_authorized=debug_authorized,
                secure_storage=secure_storage) if isinstance(v, dict)
                else scrub_secret_values(v) for v in value]
        elif isinstance(value, str) and len(value) > 32:
            # Redact long strings that look like keys
            stable_digest = key.lower().endswith(("_sha256", "_hash"))
            if (not stable_digest and
                    all(c in "0123456789abcdefABCDEF_-+/=" for c in value)):
                redacted[key] = "***POSSIBLE_SECRET***"
            else:
                redacted[key] = value if debug_enabled else _minimal_stage_value(key, value)
        else:
            redacted[key] = value
    redacted = scrub_secret_values(redacted)
    if not debug_enabled:
        redacted = {k: _minimal_stage_value(str(k), v)
                    for k, v in redacted.items()}
        redacted.setdefault("retention_class", "production_default")
        redacted.setdefault("exact_replay_available", False)
    else:
        redacted["retention_class"] = "debug_short_secure"
    return redacted


def cleanup_expired_traces(retention_days: int = None, *,
                           trace_dir: Path = None,
                           debug_retention_days: int = None,
                           now: datetime = None) -> dict:
    """Delete trace files older than retention period.

    Returns:
        {"deleted_files": int, "deleted_size_mb": float, "audit_entry": dict}
    """
    retention_days = retention_days or DEFAULT_RETENTION_DAYS
    debug_retention_days = debug_retention_days or DEBUG_RETENTION_DAYS
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    debug_cutoff = now - timedelta(days=debug_retention_days)
    trace_dir = trace_dir or TRACE_DIR

    deleted_count = 0
    deleted_size = 0

    if not trace_dir.exists():
        return {"deleted_files": 0, "deleted_size_mb": 0.0}

    for trace_file in trace_dir.glob("*.jsonl"):
        if trace_file.name == "cleanup_audit.jsonl":
            continue
        try:
            file_time = datetime.fromtimestamp(trace_file.stat().st_mtime)
            file_cutoff = debug_cutoff if trace_file.name.startswith("debug-") else cutoff
            if file_time < file_cutoff:
                deleted_size += trace_file.stat().st_size
                trace_file.unlink()
                deleted_count += 1
        except OSError:
            continue

    # Write audit entry
    audit_file = trace_dir / "cleanup_audit.jsonl"
    audit_entry = {
        "timestamp": now.isoformat(),
        "action": "trace_cleanup",
        "retention_days": retention_days,
        "debug_retention_days": debug_retention_days,
        "deleted_files": deleted_count,
        "deleted_size_mb": round(deleted_size / 1024 / 1024, 2),
    }

    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    return {
        "deleted_files": deleted_count,
        "deleted_size_mb": round(deleted_size / 1024 / 1024, 2),
        "audit_entry": audit_entry,
    }


def verify_no_secrets(trace_dir: Path = None) -> dict:
    """Scan all trace files for potential secret leakage.

    Returns:
        {"clean": bool, "suspicious_files": list, "total_files_scanned": int}
    """
    trace_dir = trace_dir or TRACE_DIR
    suspicious = []
    total = 0

    if not trace_dir.exists():
        return {"clean": True, "suspicious_files": [], "total_files_scanned": 0}

    secret_patterns = [
        "ZAI_API_KEY=", "Bearer ", "api_key=", "Authorization:",
        "sk-", "ghp_", "gho_", "xoxb-",
    ]

    for trace_file in trace_dir.glob("*.jsonl"):
        total += 1
        try:
            content = trace_file.read_text("utf-8")
            for pattern in secret_patterns:
                if pattern in content:
                    suspicious.append({
                        "file": str(trace_file.name),
                        "pattern": pattern,
                    })
                    break
        except OSError:
            continue

    return {
        "clean": len(suspicious) == 0,
        "suspicious_files": suspicious,
        "total_files_scanned": total,
    }


if __name__ == "__main__":
    # Run cleanup and verify
    result = cleanup_expired_traces()
    print(f"Cleanup: {result['deleted_files']} files, {result['deleted_size_mb']}MB")

    verify = verify_no_secrets()
    print(f"Secret scan: {'✅ clean' if verify['clean'] else '❌ ISSUES FOUND'}")
    for s in verify["suspicious_files"]:
        print(f"  ⚠️ {s}")
