"""Canonical Phase-05 request runtime-safety controls.

This module is deliberately a control-plane seam around the existing
Phase02/03/04 pipeline.  It does not decide factual support and it never
writes ``answer_status``.  The sole AnswerStateMachine remains the terminal
authority; this module only classifies technical failures, bounds work, and
records their upper-bound impact.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
import uuid
import contextvars
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


RUNTIME_SAFETY_PROFILE_VERSION = "runtime-safety-1.0"


class FailureClass(str, Enum):
    SEMANTIC_NO_EVIDENCE = "SEMANTIC_NO_EVIDENCE"
    HARD_POLICY_REJECTION = "HARD_POLICY_REJECTION"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    TIMEOUT = "TIMEOUT"
    TRANSIENT_TRANSPORT = "TRANSIENT_TRANSPORT"
    UPSTREAM_429 = "UPSTREAM_429"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    REQUIRED_BACKEND_UNAVAILABLE = "REQUIRED_BACKEND_UNAVAILABLE"
    CANCELLED = "CANCELLED"
    ABANDONED_NONCANCELLABLE = "ABANDONED_NONCANCELLABLE"
    INTERNAL_EXCEPTION = "INTERNAL_EXCEPTION"


class FailureEffect(str, Enum):
    CONTINUE_RECHECK = "CONTINUE_RECHECK"
    SAFE_FALLBACK_RECHECK = "SAFE_FALLBACK_RECHECK"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    SERVICE_ERROR = "SERVICE_ERROR"
    CANCELLED = "CANCELLED"


RETRYABLE_FAILURES = frozenset({
    FailureClass.TIMEOUT,
    FailureClass.TRANSIENT_TRANSPORT,
    FailureClass.UPSTREAM_429,
    FailureClass.UPSTREAM_5XX,
})


@dataclass(frozen=True)
class FailureDecision:
    capability: str
    failure_class: FailureClass
    effect: FailureEffect
    fallback: str
    reason_code: str
    correctness_critical: bool


@dataclass(frozen=True)
class DegradationRecord:
    capability: str
    failure_class: str
    reason_code: str
    requirement_id: str = ""
    correctness_critical: bool = False
    fallback_used: str = "none"
    retry_count: int = 0
    state_impact: str = "RECHECK_SUFFICIENCY"
    terminal_upper_bound: str = "SUPPORTED"

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "failure_class": self.failure_class,
            "reason_code": self.reason_code,
            "requirement_id": self.requirement_id,
            "correctness_critical": self.correctness_critical,
            "fallback_used": self.fallback_used,
            "retry_count": self.retry_count,
            "state_impact": self.state_impact,
            "terminal_upper_bound": self.terminal_upper_bound,
        }


# Explicit registry for every live factual-path capability.  Values describe
# whether a proven-safe deterministic fallback exists.  Request-specific
# criticality and sufficiency are evaluated below; the table is not itself a
# support authority.
CAPABILITY_REGISTRY = {
    "rewrite": "deterministic_rewrite",
    "router": "deterministic_router",
    "planner": "deterministic_requirements",
    "vector_search": "remaining_routes",
    "bm25_search": "remaining_routes",
    "graph_search": "remaining_routes",
    "retrieval": "remaining_routes",
    "reranker": "deterministic_content_ranker",
    "evidence_selector": "deterministic_safe_selector",
    "multi_document_worker": "isolate_document_recompute",
    "conflict_detector": "none",
    "evidence_grader": "none",
    "citation_grounding": "drop_semantic_miss_only",
    "entailment": "none",
    "claim_mapping": "bounded_repair",
    "answer_state_machine": "none",
    "verifier": "none",
    "final_verifier": "none",
    "generator": "none",
    "gap_analysis": "deterministic_boundary",
    "repair": "deterministic_boundary",
}


def classify_exception(exc: BaseException) -> FailureClass:
    if isinstance(exc, asyncio.CancelledError):
        return FailureClass.CANCELLED
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return FailureClass.TIMEOUT
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return FailureClass.UPSTREAM_429
    if isinstance(status, int) and 500 <= status <= 599:
        return FailureClass.UPSTREAM_5XX
    text = str(exc).lower()
    if "429" in text or "rate limit" in text:
        return FailureClass.UPSTREAM_429
    if any(x in text for x in ("http 5", "status 5", "service unavailable")):
        return FailureClass.UPSTREAM_5XX
    if any(x in text for x in ("connection", "transport", "urlopen", "socket")):
        return FailureClass.TRANSIENT_TRANSPORT
    if any(x in text for x in ("malformed", "invalid schema", "schema rejection")):
        return FailureClass.MALFORMED_MODEL_OUTPUT
    return FailureClass.INTERNAL_EXCEPTION


def decide_failure(
    capability: str,
    failure_class: FailureClass,
    *,
    requirement_critical: bool = False,
    alternative_evidence_sufficient: bool = False,
    safe_fallback_available: Optional[bool] = None,
) -> FailureDecision:
    """Return a request-aware technical disposition, never factual support."""
    if capability not in CAPABILITY_REGISTRY:
        return FailureDecision(
            capability, failure_class, FailureEffect.UNVERIFIED, "none",
            "RUNTIME_UNKNOWN_CAPABILITY_FAIL_SAFE", True)
    fallback = CAPABILITY_REGISTRY[capability]
    if safe_fallback_available is None:
        safe_fallback_available = fallback != "none"
    if failure_class == FailureClass.CANCELLED:
        return FailureDecision(capability, failure_class,
                               FailureEffect.CANCELLED, "none",
                               "RUNTIME_REQUEST_CANCELLED", True)
    if failure_class == FailureClass.REQUIRED_BACKEND_UNAVAILABLE:
        return FailureDecision(capability, failure_class,
                               FailureEffect.SERVICE_ERROR, "none",
                               "RUNTIME_REQUIRED_BACKEND_UNAVAILABLE", True)
    if failure_class == FailureClass.SEMANTIC_NO_EVIDENCE:
        return FailureDecision(capability, failure_class,
                               FailureEffect.UNSUPPORTED, "none",
                               "RUNTIME_SEMANTIC_NO_EVIDENCE",
                               requirement_critical)
    if failure_class == FailureClass.HARD_POLICY_REJECTION:
        return FailureDecision(capability, failure_class,
                               FailureEffect.UNSUPPORTED, "none",
                               "RUNTIME_HARD_POLICY_REJECTION", True)

    if capability in {"vector_search", "bm25_search", "retrieval"}:
        return FailureDecision(capability, failure_class,
                               FailureEffect.CONTINUE_RECHECK,
                               "remaining_routes",
                               "RUNTIME_ROUTE_FAILURE_RECHECK", requirement_critical)
    if capability == "graph_search":
        if requirement_critical and not alternative_evidence_sufficient:
            return FailureDecision(capability, failure_class,
                                   FailureEffect.UNVERIFIED, "none",
                                   "RUNTIME_RELATION_CRITICAL_GRAPH_UNAVAILABLE", True)
        return FailureDecision(capability, failure_class,
                               FailureEffect.CONTINUE_RECHECK,
                               "remaining_routes",
                               "RUNTIME_OPTIONAL_GRAPH_DEGRADED",
                               requirement_critical)
    if capability in {"rewrite", "router", "planner", "reranker"}:
        if safe_fallback_available:
            return FailureDecision(capability, failure_class,
                                   FailureEffect.SAFE_FALLBACK_RECHECK, fallback,
                                   "RUNTIME_DETERMINISTIC_FALLBACK", requirement_critical)
    if capability == "evidence_selector" and safe_fallback_available:
        return FailureDecision(capability, failure_class,
                               FailureEffect.SAFE_FALLBACK_RECHECK, fallback,
                               "RUNTIME_SAFE_SELECTOR_FALLBACK", True)
    if capability == "multi_document_worker":
        return FailureDecision(capability, failure_class,
                               FailureEffect.CONTINUE_RECHECK, fallback,
                               "RUNTIME_WORKER_ISOLATED_RECHECK", requirement_critical)
    if capability in {"gap_analysis", "repair"} and safe_fallback_available:
        return FailureDecision(capability, failure_class,
                               FailureEffect.SAFE_FALLBACK_RECHECK, fallback,
                               "RUNTIME_BOUNDARY_FALLBACK", requirement_critical)
    if capability == "generator":
        return FailureDecision(capability, failure_class,
                               FailureEffect.SERVICE_ERROR, "none",
                               "RUNTIME_GENERATOR_FAILURE", True)

    # Grader, technical grounding, entailment, verifier, claim mapping,
    # conflict detection and the AnswerStateMachine may never be skipped into
    # ordinary support when required.
    return FailureDecision(capability, failure_class,
                           FailureEffect.UNVERIFIED, "none",
                           "RUNTIME_CRITICAL_STAGE_UNVERIFIED", True)


@dataclass(frozen=True)
class RuntimeSafetyProfile:
    version: str = RUNTIME_SAFETY_PROFILE_VERSION
    rewrite: float = 3.0
    router: float = 3.0
    retrieval: float = 3.0
    reranker_local: float = 5.0
    reranker_remote: float = 8.0
    worker: float = 12.0
    grader: float = 8.0
    generator: float = 30.0
    verifier: float = 10.0
    planner: float = 8.0  # implementation choice; no normative numeric value
    selector: float = 5.0  # implementation choice; deterministic/local bound
    repair: float = 12.0  # implementation choice; bounded repair cycle
    fast_total: float = 60.0
    research_total: float = 120.0
    deep_total: float = 180.0
    max_attempts: int = 2
    backoff_seconds: float = 0.05

    def total_for(self, mode: str) -> float:
        name = (mode or "FAST").upper()
        if "DEEP" in name:
            return self.deep_total
        if "RESEARCH" in name:
            return self.research_total
        return self.fast_total

    def stage_for(self, stage: str) -> float:
        aliases = {
            "final_verifier": "verifier", "evidence_grader": "grader",
            "graph_search": "retrieval", "vector_search": "retrieval",
            "bm25_search": "retrieval", "multi_document_worker": "worker",
            "evidence_selector": "selector", "citation_grounding": "verifier",
            "entailment": "verifier", "claim_mapping": "verifier",
            "gap_analysis": "repair",
        }
        return float(getattr(self, aliases.get(stage, stage), self.verifier))


DEFAULT_PROFILE = RuntimeSafetyProfile(
    fast_total=float(os.environ.get("QA_RUNTIME_FAST_DEADLINE", "60")),
    research_total=float(os.environ.get("QA_RUNTIME_RESEARCH_DEADLINE", "120")),
    deep_total=float(os.environ.get("QA_RUNTIME_DEEP_DEADLINE", "180")),
)


class RequestCancelled(RuntimeError):
    pass


class StageExecutionError(RuntimeError):
    def __init__(self, decision: FailureDecision, cause: BaseException,
                 attempts: int):
        self.decision = decision
        self.cause = cause
        self.attempts = attempts
        super().__init__(f"{decision.reason_code}: {cause}")


_ABANDONED_LOCK = threading.Lock()
_ABANDONED = {"submitted": 0, "count": 0, "events": []}
_CURRENT_CONTEXT = contextvars.ContextVar("tech_db_request_execution", default=None)


def abandoned_call_stats() -> dict:
    with _ABANDONED_LOCK:
        return {"submitted": _ABANDONED["submitted"],
                "count": _ABANDONED["count"],
                "events": list(_ABANDONED["events"][-50:])}


def _record_abandoned(request_id: str, trace_id: str, stage: str,
                      reason: str) -> None:
    with _ABANDONED_LOCK:
        _ABANDONED["count"] += 1
        _ABANDONED["events"].append({
            "request_id": request_id, "trace_id": trace_id,
            "stage": stage, "reason": reason,
        })


def record_abandoned_call(request_id: str = "", trace_id: str = "",
                          stage: str = "llm_http", reason: str = "cancelled") -> None:
    context = _CURRENT_CONTEXT.get()
    _record_abandoned(
        request_id or getattr(context, "request_id", ""),
        trace_id or getattr(context, "trace_id", ""), stage, reason)


def bind_request_context(context):
    return _CURRENT_CONTEXT.set(context)


def reset_request_context(token) -> None:
    _CURRENT_CONTEXT.reset(token)


def current_request_context():
    return _CURRENT_CONTEXT.get()


@dataclass
class RequestExecutionContext:
    mode: str = "FAST"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = ""
    profile: RuntimeSafetyProfile = field(default_factory=lambda: DEFAULT_PROFILE)
    query_budget: Any = None
    started_at: float = field(default_factory=time.monotonic)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str = ""
    closed: bool = False
    degraded_capabilities: list[dict] = field(default_factory=list)
    retry_events: list[dict] = field(default_factory=list)
    _tasks: set = field(default_factory=set, repr=False)

    @property
    def deadline_at(self) -> float:
        return self.started_at + self.profile.total_for(self.mode)

    def remaining(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def stage_timeout(self, stage: str) -> float:
        return max(0.0, min(self.profile.stage_for(stage), self.remaining()))

    @property
    def active(self) -> bool:
        return not self.cancelled.is_set() and not self.closed and self.remaining() > 0

    def check_active(self) -> None:
        if self.cancelled.is_set() or self.closed:
            raise RequestCancelled(self.cancel_reason or "request_cancelled")
        if self.remaining() <= 0:
            self.cancel("total_deadline_exhausted")
            raise RequestCancelled("total_deadline_exhausted")

    def cancel(self, reason: str = "client_disconnect") -> None:
        if self.cancelled.is_set():
            return
        self.cancel_reason = reason
        self.cancelled.set()
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()

    def close(self) -> None:
        self.closed = True

    def add_degradation(self, decision: FailureDecision, *,
                        requirement_id: str = "", retry_count: int = 0) -> dict:
        upper = {
            FailureEffect.UNVERIFIED: "UNVERIFIED",
            FailureEffect.UNSUPPORTED: "UNSUPPORTED",
            FailureEffect.SERVICE_ERROR: "SERVICE_ERROR",
            FailureEffect.CANCELLED: "CANCELLED",
        }.get(decision.effect, "SUPPORTED_IF_CANONICAL_GATES_PASS")
        record = DegradationRecord(
            capability=decision.capability,
            failure_class=decision.failure_class.value,
            reason_code=decision.reason_code,
            requirement_id=requirement_id,
            correctness_critical=decision.correctness_critical,
            fallback_used=decision.fallback,
            retry_count=retry_count,
            state_impact=decision.effect.value,
            terminal_upper_bound=upper,
        ).to_dict()
        self.degraded_capabilities.append(record)
        return record

    def can_commit(self) -> bool:
        return self.active

    def commit_if_active(self, apply: Callable[[], Any]) -> bool:
        if not self.can_commit():
            return False
        apply()
        return True

    async def run_stage(
        self,
        stage: str,
        operation: Callable[[], Any],
        *,
        requirement_id: str = "",
        requirement_critical: bool = False,
        alternative_evidence_sufficient: bool = False,
        safe_fallback_available: Optional[bool] = None,
        query_budget_cost: int = 0,
    ) -> Any:
        attempts = 0
        last: BaseException = RuntimeError("stage did not run")
        while attempts < self.profile.max_attempts:
            self.check_active()
            if query_budget_cost and self.query_budget is not None \
                    and not self.query_budget.can_afford(query_budget_cost):
                decision = decide_failure(
                    stage, FailureClass.INTERNAL_EXCEPTION,
                    requirement_critical=requirement_critical,
                    safe_fallback_available=False)
                self.add_degradation(decision, requirement_id=requirement_id,
                                     retry_count=attempts)
                raise StageExecutionError(decision,
                                          RuntimeError("query_budget_exhausted"),
                                          attempts)
            timeout = self.stage_timeout(stage)
            if timeout <= 0:
                last = asyncio.TimeoutError("total deadline exhausted")
                failure_class = FailureClass.TIMEOUT
            else:
                attempts += 1
                try:
                    value = operation()
                    if inspect.isawaitable(value):
                        task = asyncio.ensure_future(value)
                        cancel_waiter = asyncio.create_task(
                            self.cancelled.wait())
                        self._tasks.add(task)
                        try:
                            done, _ = await asyncio.wait(
                                {task, cancel_waiter}, timeout=timeout,
                                return_when=asyncio.FIRST_COMPLETED)
                            if cancel_waiter in done and self.cancelled.is_set():
                                if not task.done():
                                    task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass
                                raise RequestCancelled(
                                    self.cancel_reason or
                                    "cancelled_during_stage")
                            if task not in done:
                                task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass
                                raise asyncio.TimeoutError(
                                    f"{stage} deadline exceeded")
                            return task.result()
                        finally:
                            # If the caller's async-generator scope is closed
                            # while this await is suspended, cancellation may
                            # bypass the ordinary exception branch.  Never
                            # detach a cancellable stage from request scope.
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass
                            self._tasks.discard(task)
                            if not cancel_waiter.done():
                                cancel_waiter.cancel()
                    return value
                except RequestCancelled:
                    raise
                except asyncio.CancelledError:
                    self.cancel(self.cancel_reason or "cancelled_during_stage")
                    raise
                except BaseException as exc:
                    last = exc
                    failure_class = classify_exception(exc)
            retryable = failure_class in RETRYABLE_FAILURES
            retry_allowed = (
                retryable and attempts < self.profile.max_attempts
                and self.active and self.remaining() > self.profile.backoff_seconds
                and (not query_budget_cost or self.query_budget is None
                     or self.query_budget.can_afford(query_budget_cost))
            )
            self.retry_events.append({
                "stage": stage, "attempt": attempts,
                "failure_class": failure_class.value,
                "retry": retry_allowed,
                "remaining_ms": round(self.remaining() * 1000, 1),
            })
            if retry_allowed:
                await asyncio.sleep(min(self.profile.backoff_seconds,
                                        self.remaining()))
                continue
            decision = decide_failure(
                stage, failure_class,
                requirement_critical=requirement_critical,
                alternative_evidence_sufficient=alternative_evidence_sufficient,
                safe_fallback_available=safe_fallback_available)
            self.add_degradation(decision, requirement_id=requirement_id,
                                 retry_count=max(0, attempts - 1))
            raise StageExecutionError(decision, last, attempts)

    async def run_noncancellable(self, stage: str,
                                 operation: Callable[[], Awaitable[Any]]) -> Any:
        """Detach an unavoidable late result from request authority."""
        self.check_active()
        with _ABANDONED_LOCK:
            _ABANDONED["submitted"] += 1
        task = asyncio.create_task(operation())
        cancel_waiter = asyncio.create_task(self.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancel_waiter},
                timeout=self.stage_timeout(stage),
                return_when=asyncio.FIRST_COMPLETED)
            if task in done and self.active:
                return task.result()
            _record_abandoned(self.request_id, self.trace_id, stage,
                              self.cancel_reason or "deadline_or_cancel")
            task.add_done_callback(
                lambda completed: completed.exception()
                if not completed.cancelled() else None)
            decision = decide_failure(stage,
                                      FailureClass.ABANDONED_NONCANCELLABLE,
                                      requirement_critical=True)
            self.add_degradation(decision)
            raise StageExecutionError(decision,
                                      RuntimeError("late result detached"), 1)
        finally:
            if not cancel_waiter.done():
                cancel_waiter.cancel()


class AdmissionOutcome(str, Enum):
    ADMITTED = "ADMITTED"
    QUEUE_FULL = "QUEUE_FULL"
    CANCELLED_WHILE_QUEUED = "CANCELLED_WHILE_QUEUED"


class AdmissionController:
    """Bounded active + waiting admission over the existing semaphore seam."""
    def __init__(self, active_limit: int, queue_capacity: int,
                 retry_after: int = 5, semaphore=None):
        if active_limit < 1 or queue_capacity < 0:
            raise ValueError("admission limits must be finite and non-negative")
        self.active_limit = active_limit
        self.queue_capacity = queue_capacity
        self.retry_after = max(1, int(retry_after))
        self._semaphore = semaphore
        self.active = 0
        self.queued = 0
        self.active_max = 0
        self.queue_rejections = 0
        self.backend_unavailable = 0
        self.cancelled_queued = 0
        self._condition = asyncio.Condition()

    async def acquire(self, context: RequestExecutionContext,
                      disconnect_checker: Optional[Callable[[], Awaitable[bool]]] = None,
                      wait_timeout: float = 5.0) -> AdmissionOutcome:
        async with self._condition:
            if self.active < self.active_limit:
                self.active += 1
                self.active_max = max(self.active_max, self.active)
                if self._semaphore is not None:
                    await self._semaphore.acquire()
                return AdmissionOutcome.ADMITTED
            if self.queued >= self.queue_capacity:
                self.queue_rejections += 1
                return AdmissionOutcome.QUEUE_FULL
            self.queued += 1
        deadline = time.monotonic() + max(0.05, wait_timeout)
        try:
            while True:
                if disconnect_checker is not None and await disconnect_checker():
                    context.cancel("disconnect_while_queued")
                    self.cancelled_queued += 1
                    return AdmissionOutcome.CANCELLED_WHILE_QUEUED
                if context.cancelled.is_set() or time.monotonic() >= deadline:
                    self.queue_rejections += 1
                    return AdmissionOutcome.QUEUE_FULL
                async with self._condition:
                    if self.active < self.active_limit:
                        self.active += 1
                        self.active_max = max(self.active_max, self.active)
                        if self._semaphore is not None:
                            await self._semaphore.acquire()
                        return AdmissionOutcome.ADMITTED
                    try:
                        await asyncio.wait_for(self._condition.wait(), 0.05)
                    except asyncio.TimeoutError:
                        pass
        finally:
            async with self._condition:
                self.queued = max(0, self.queued - 1)

    async def release(self) -> None:
        async with self._condition:
            if self.active > 0:
                self.active -= 1
                if self._semaphore is not None:
                    self._semaphore.release()
            self._condition.notify(1)

    def record_backend_unavailable(self) -> None:
        self.backend_unavailable += 1

    def snapshot(self) -> dict:
        return {
            "active": self.active, "queued": self.queued,
            "active_max": self.active_max,
            "active_limit": self.active_limit,
            "queue_capacity": self.queue_capacity,
            "queue_rejections": self.queue_rejections,
            "cancelled_queued": self.cancelled_queued,
            "backend_unavailable": self.backend_unavailable,
            "profile_version": RUNTIME_SAFETY_PROFILE_VERSION,
        }
