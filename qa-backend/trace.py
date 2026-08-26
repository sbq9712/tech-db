"""
T001 — QA Trace System
======================
Per-request trace recording for full-pipeline observability.

Records every stage from original_query → final_answer:
  rewrite → router → decomposition → retrieval (vec/bm25/graph) →
  RRF → rerank → evidence_selector → evidence_ledger →
  grader → gap_analysis → conflict_detection →
  generation → claim_mapping → citation_grounding → verification

Storage: runtime/traces/YYYY-MM-DD.jsonl (one JSON per line, per request)
Security: NO API keys, secrets, or auth headers are ever stored.
Fail-safe: Trace write failure NEVER breaks the main QA request.
"""
import json
import os
import uuid
import traceback
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from trace_retention import project_production_trace, scrub_secret_values


REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
TRACE_DIR = RUNTIME_DIR / "traces"

# Feature flag: allow disabling trace entirely (e.g. for load testing).
# Deferred to feature_flags.Flags (single source of truth) so a named
# pipeline profile applied at import applies here too — never a second,
# divergent env read.
try:
    from feature_flags import Flags as _Flags
    TRACE_ENABLED = bool(_Flags.TRACE_ENABLED)
except Exception:  # pragma: no cover — standalone import without package
    TRACE_ENABLED = os.environ.get("QA_TRACE_ENABLED", "true").lower() not in ("0", "false", "no")

# Secret patterns to scrub — extend as needed
_SECRET_PATTERNS = [
    "zai_api_key", "api_key", "authorization", "secret",
    "password", "token", "bearer", "x-admin-key",
]


def _scrub(obj: Any, field_name: str = "") -> Any:
    """Recursively remove secret keys and secret-looking values."""
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if any(s in k.lower() for s in _SECRET_PATTERNS)
            else _scrub(v, str(k))
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_scrub(item, field_name) for item in obj]
    elif isinstance(obj, str):
        # Redact long strings that look like keys (32+ hex/base64 chars)
        stable_digest = field_name.lower().endswith(("_sha256", "_hash"))
        if (not stable_digest and len(obj) >= 32 and
                all(c in "0123456789abcdefABCDEF_-+/=" for c in obj)):
            return "***POSSIBLE_SECRET***"
        return scrub_secret_values(obj)
    return obj


class TraceContext:
    """Accumulates trace data for a single QA request.

    Usage:
        trace = TraceContext.create(query, conversation_id)
        trace.add_stage("rewrite", {"rewritten_query": "...", "novelty": False})
        ...
        trace.set_result(answer="...", status="SUPPORTED", ...)
        trace.flush()  # write to JSONL
    """

    __slots__ = (
        "trace_id", "request_id", "timestamp", "conversation_id_hash",
        "query_sha256", "query_length", "profile", "manifest_id",
        "identity_snapshot_id", "state_machine_version",
        "stages", "result", "retention_class", "exact_replay_available",
        "_flushed",
    )

    def __init__(self, trace_id: str, timestamp: str, conversation_id: str,
                 original_query: str, *, request_id: str = "", profile: str = "",
                 manifest_id: str = "", identity_snapshot_id: str = "",
                 state_machine_version: str = "answer-state-2.0"):
        self.trace_id = trace_id
        self.request_id = request_id or uuid.uuid4().hex
        self.timestamp = timestamp
        self.conversation_id_hash = hashlib.sha256(
            (conversation_id or "").encode("utf-8")).hexdigest()
        self.query_sha256 = hashlib.sha256(
            (original_query or "").encode("utf-8")).hexdigest()
        self.query_length = len(original_query or "")
        self.profile = profile
        self.manifest_id = manifest_id
        self.identity_snapshot_id = identity_snapshot_id
        self.state_machine_version = state_machine_version
        self.stages: list[dict] = []
        self.result: dict = {}
        self.retention_class = "production_default"
        self.exact_replay_available = False
        self._flushed = False

    @classmethod
    def create(cls, original_query: str, conversation_id: str = "", **metadata) -> "TraceContext":
        """Create a new trace context for a request."""
        return cls(
            trace_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now().isoformat(),
            conversation_id=conversation_id or "",
            original_query=original_query,
            **metadata,
        )

    def add_stage(self, name: str, data: dict) -> None:
        """Add a named pipeline stage's data to the trace.

        Args:
            name: Stage identifier (e.g. "rewrite", "retrieval", "rerank")
            data: Arbitrary dict of stage-specific data (will be scrubbed for secrets)
        """
        if not TRACE_ENABLED:
            return
        try:
            self.stages.append({
                "stage": name,
                "data": _scrub(data),
            })
        except Exception:
            pass  # Never break QA for trace

    def add_retrieval(self, route: str, results: list, top_k: int = 10) -> None:
        """Convenience method to record a retrieval route's results."""
        if not TRACE_ENABLED:
            return
        try:
            compact = [
                {
                    "record_id": r.get("record_id") or r.get("meta", {}).get("record_id"),
                    "legacy_idx": r.get("legacy_idx", r.get("meta", {}).get("legacy_idx", r.get("meta", {}).get("idx"))),
                    "score": round(r.get("score", 0), 4),
                    "title": r.get("meta", {}).get("t", "")[:80],
                }
                for r in results[:top_k]
            ]
            self.add_stage(f"retrieval_{route}", {
                "route": route,
                "result_count": len(results),
                "top_results": compact,
            })
        except Exception:
            pass

    def add_rrf(self, fused_results: list, top_k: int = 25) -> None:
        """Record RRF fusion results."""
        if not TRACE_ENABLED:
            return
        try:
            compact = [
                {
                    "record_id": r.get("record_id") or r.get("meta", {}).get("record_id"),
                    "legacy_idx": r.get("legacy_idx", r.get("meta", {}).get("legacy_idx", r.get("meta", {}).get("idx"))),
                    "rrf_score": round(r.get("score", 0), 6),
                    "vec_score": round(r.get("vec_score", 0), 4),
                    "bm25_score": round(r.get("bm25_score", 0), 4),
                    "graph_score": round(r.get("graph_score", 0), 4),
                }
                for r in fused_results[:top_k]
            ]
            self.add_stage("rrf_fusion", {"fused": compact})
        except Exception:
            pass

    def set_result(self, **kwargs) -> None:
        """Set the final result data (answer, status, citations, etc.)."""
        if not TRACE_ENABLED:
            return
        try:
            self.result.update(_scrub(kwargs))
        except Exception:
            pass

    def flush(self) -> None:
        """Write the trace to the daily JSONL file. Idempotent."""
        if not TRACE_ENABLED or self._flushed:
            return
        self._flushed = True
        try:
            try:
                from runtime_safety import current_request_context
                runtime_context = current_request_context()
                if runtime_context is not None:
                    self.result.setdefault(
                        "degraded_capabilities",
                        list(runtime_context.degraded_capabilities))
                    self.result.setdefault(
                        "retry_events", list(runtime_context.retry_events))
                    self.result.setdefault(
                        "cancellation_reason", runtime_context.cancel_reason)
            except Exception:
                pass
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            trace_file = TRACE_DIR / f"{date_str}.jsonl"

            record = {
                "trace_id": self.trace_id,
                "request_id": self.request_id,
                "timestamp": self.timestamp,
                "conversation_id_hash": self.conversation_id_hash,
                "query_sha256": self.query_sha256,
                "query_length": self.query_length,
                "profile": self.profile,
                "manifest_id": self.manifest_id,
                "identity_snapshot_id": self.identity_snapshot_id,
                "answer_state_machine_version": self.state_machine_version,
                "retention_class": self.retention_class,
                "exact_replay_available": self.exact_replay_available,
                "stages": self.stages,
                "result": self.result,
            }
            # Production persistence always passes the centralized retention
            # policy.  Full-text debug is intentionally disabled unless a
            # future approved encrypted/access-controlled storage adapter is
            # configured; a request parameter alone cannot enable it.
            record = project_production_trace(record)
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(trace_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            # Trace write failure must NEVER break QA
            print(f"[trace] WARNING: flush failed: {e}", flush=True)


def get_trace_dir() -> Path:
    """Return the trace directory path (for testing/inspection)."""
    return TRACE_DIR


def is_enabled() -> bool:
    """Check if tracing is enabled."""
    return TRACE_ENABLED
