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
import shutil
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
    "raw_search_results", "full_body_text",
]


def redact_trace(record: dict, debug_mode: bool = False) -> dict:
    """Redact sensitive fields from a trace record.

    Args:
        record: Trace record dict
        debug_mode: If True, keep debug-only fields

    Returns:
        Redacted trace record
    """
    if not isinstance(record, dict):
        return record

    redacted = {}
    for key, value in record.items():
        # Always redact secrets
        if any(s in key.lower() for s in ALWAYS_REDACT):
            redacted[key] = "***REDACTED***"
            continue

        # Remove debug-only fields in production
        if not debug_mode and key in DEBUG_ONLY_FIELDS:
            continue

        # Recursively redact nested dicts/lists
        if isinstance(value, dict):
            redacted[key] = redact_trace(value, debug_mode)
        elif isinstance(value, list):
            redacted[key] = [redact_trace(v, debug_mode) if isinstance(v, dict) else v
                            for v in value]
        elif isinstance(value, str) and len(value) > 32:
            # Redact long strings that look like keys
            if all(c in "0123456789abcdefABCDEF_-+/=" for c in value):
                redacted[key] = "***POSSIBLE_SECRET***"
            else:
                redacted[key] = value
        else:
            redacted[key] = value

    return redacted


def cleanup_expired_traces(retention_days: int = None) -> dict:
    """Delete trace files older than retention period.

    Returns:
        {"deleted_files": int, "deleted_size_mb": float, "audit_entry": dict}
    """
    retention_days = retention_days or DEFAULT_RETENTION_DAYS
    cutoff = datetime.now() - timedelta(days=retention_days)

    deleted_count = 0
    deleted_size = 0

    if not TRACE_DIR.exists():
        return {"deleted_files": 0, "deleted_size_mb": 0.0}

    for trace_file in TRACE_DIR.glob("*.jsonl"):
        try:
            file_time = datetime.fromtimestamp(trace_file.stat().st_mtime)
            if file_time < cutoff:
                deleted_size += trace_file.stat().st_size
                trace_file.unlink()
                deleted_count += 1
        except OSError:
            continue

    # Write audit entry
    audit_file = TRACE_DIR / "cleanup_audit.jsonl"
    audit_entry = {
        "timestamp": datetime.now().isoformat(),
        "action": "trace_cleanup",
        "retention_days": retention_days,
        "deleted_files": deleted_count,
        "deleted_size_mb": round(deleted_size / 1024 / 1024, 2),
    }

    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
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
