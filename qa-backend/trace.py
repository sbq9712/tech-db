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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
TRACE_DIR = RUNTIME_DIR / "traces"

# Feature flag: allow disabling trace entirely (e.g. for load testing)
TRACE_ENABLED = os.environ.get("QA_TRACE_ENABLED", "true").lower() not in ("0", "false", "no")

# Secret patterns to scrub — extend as needed
_SECRET_PATTERNS = [
    "zai_api_key", "api_key", "authorization", "secret",
    "password", "token", "bearer", "x-admin-key",
]


def _scrub(obj: Any) -> Any:
    """Recursively remove any key that looks like a secret."""
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if any(s in k.lower() for s in _SECRET_PATTERNS) else _scrub(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_scrub(item) for item in obj]
    elif isinstance(obj, str):
        # Redact long strings that look like keys (32+ hex/base64 chars)
        if len(obj) >= 32 and all(c in "0123456789abcdefABCDEF_-+/=" for c in obj):
            return "***POSSIBLE_SECRET***"
        return obj
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
        "trace_id", "timestamp", "conversation_id",
        "original_query", "stages", "result",
        "_flushed",
    )

    def __init__(self, trace_id: str, timestamp: str, conversation_id: str, original_query: str):
        self.trace_id = trace_id
        self.timestamp = timestamp
        self.conversation_id = conversation_id
        self.original_query = original_query
        self.stages: list[dict] = []
        self.result: dict = {}
        self._flushed = False

    @classmethod
    def create(cls, original_query: str, conversation_id: str = "") -> "TraceContext":
        """Create a new trace context for a request."""
        return cls(
            trace_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now().isoformat(),
            conversation_id=conversation_id or "",
            original_query=original_query[:500],  # cap to avoid huge traces
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
                    "idx": r.get("meta", {}).get("idx", -1),
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
                    "idx": r.get("meta", {}).get("idx", -1),
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
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            trace_file = TRACE_DIR / f"{date_str}.jsonl"

            record = {
                "trace_id": self.trace_id,
                "timestamp": self.timestamp,
                "conversation_id": self.conversation_id,
                "original_query": self.original_query,
                "stages": self.stages,
                "result": self.result,
            }

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
