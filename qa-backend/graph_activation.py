"""Phase07 RT-085/RT-086/RT-087 — activation evidence machinery.

Shadow non-interference monitor, the normative Graph-V2 activation gate,
and the machine-readable benchmark-result envelope.

Normative gate (final_spec §40/§41): representative shadow window is
>=1,000 events AND >=7 days, OR an equivalent locked replay plus EXPLICIT
external approval. Claude-side execution can never self-approve: the
approval token must be provided out-of-band via QA_GRAPH_V2_ACTIVATION_APPROVAL.
Until every condition holds with real evidence:

    graph_v2_activation_claim = false
    activation_gate_satisfied = false
    locked_replay_only = true   (when only CI replay exists)

A failed gain gate records ``NOT_ACTIVATED_BY_GATE`` — never a silent DONE.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

ACTIVATION_GATE_SCHEMA = "graph-v2-activation-gate-1.0"
MIN_SHADOW_EVENTS = 1000
MIN_SHADOW_DAYS = 7.0


def _stable_hash(payload: dict) -> str:
    import json
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()[:16]


class GraphShadowMonitor:
    """Non-interfering shadow observations for relation-aware retrieval.

    Shadow runs NEVER mutate serving outputs: observe() receives both
    result sets read-only and records agreement/diff events. The report is
    the machine-readable evidence surface for the activation gate.
    """

    def __init__(self):
        self.observations: List[dict] = []

    def observe(self, *, query_id: str, serving_record_ids: List[str],
                shadow_record_ids: List[str],
                top_k: int = 10) -> dict:
        serving = [str(r) for r in (serving_record_ids or [])][:top_k]
        shadow = [str(r) for r in (shadow_record_ids or [])][:top_k]
        s_set, h_set = set(serving), set(shadow)
        union = s_set | h_set
        inter = s_set & h_set
        jaccard = (len(inter) / len(union)) if union else 1.0
        event = {
            "event_id": _stable_hash({"q": query_id, "n": len(self.observations)}),
            "query_id": str(query_id),
            "serving_top": serving,
            "shadow_top": shadow,
            "overlap_jaccard": round(jaccard, 6),
            "identical": serving == shadow,
            "serving_only": sorted(s_set - h_set),
            "shadow_only": sorted(h_set - s_set),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.observations.append(event)
        return event

    def report(self, *, duration_days: float = 0.0) -> dict:
        n = len(self.observations)
        identical = sum(1 for e in self.observations if e["identical"])
        avg_overlap = (sum(e["overlap_jaccard"]
                           for e in self.observations) / n) if n else 0.0
        return {
            "schema_version": "graph-shadow-report-1.0",
            "events": n,
            "duration_days": float(duration_days),
            "identical_ratio": round(identical / n, 6) if n else 0.0,
            "avg_overlap_jaccard": round(avg_overlap, 6),
            "window_type": ("LIVE" if duration_days >= MIN_SHADOW_DAYS
                            else "CI_REPLAY"),
        }


def external_approval_token() -> str:
    """Out-of-band activation approval; absent/unset in CI by construction.

    An agent cannot mint this value: it must be injected into the process
    environment by the human authority that owns the rollout decision.
    """
    return os.environ.get("QA_GRAPH_V2_ACTIVATION_APPROVAL", "").strip()


class GraphActivationGate:
    """Machine-evaluable Graph-V2 full-activation gate (RT-087)."""

    def evaluate(self, *, benchmark_gain_conclusion: str = "",
                 core_regression_passed: bool = False,
                 canary_passed: Optional[bool] = None,
                 shadow_events: int = 0,
                 shadow_duration_days: float = 0.0,
                 locked_replay_available: bool = True,
                 approval_token: str = "") -> dict:
        reasons: List[str] = []
        gain_ok = str(benchmark_gain_conclusion or "").strip().upper() \
            in ("GAIN", "MEANINGFUL_GAIN")
        if not gain_ok:
            reasons.append("RELATION_SPECIFIC_GAIN_NOT_DEMONSTRATED")
        if not core_regression_passed:
            reasons.append("CORE_QA_REGRESSION_UNPROVEN_OR_FAILED")
        if canary_passed is not True:
            reasons.append("CANARY_NOT_PASSED")
        live_window = (int(shadow_events) >= MIN_SHADOW_EVENTS
                       and float(shadow_duration_days) >= MIN_SHADOW_DAYS)
        token = str(approval_token or external_approval_token())
        explicit_approval = bool(token) and bool(locked_replay_available)
        if not live_window and not explicit_approval:
            reasons.append(
                f"SHADOW_WINDOW_INSUFFICIENT(<{MIN_SHADOW_EVENTS} events "
                f"AND {MIN_SHADOW_DAYS:g} days) AND NO_EQUIVALENT_REPLAY_"
                "WITH_EXPLICIT_APPROVAL")
        satisfied = not reasons
        # honesty contract: even a fully-satisfied evaluation yields
        # ACTIVATION_ALLOWED_PENDING_RELEASE — flipping production traffic
        # remains a separate human release action (RT-005 authority split).
        status = ("ACTIVATION_ALLOWED_PENDING_RELEASE" if satisfied
                  else "NOT_ACTIVATED_BY_GATE")
        if not live_window:
            reasons.append("LOCKED_REPLAY_ONLY_WINDOW") \
                if "LOCKED_REPLAY_ONLY_WINDOW" not in reasons else None
        return {
            "schema_version": ACTIVATION_GATE_SCHEMA,
            "gate_status": status,
            "activation_gate_satisfied": satisfied,
            "locked_replay_only": not live_window,
            "live_shadow_window": live_window,
            "equivalent_replay_explicitly_approved": (
                explicit_approval and not live_window),
            "reasons": reasons,
            "thresholds": {"min_events": MIN_SHADOW_EVENTS,
                           "min_days": MIN_SHADOW_DAYS},
        }


def build_benchmark_result(*, fixture_name: str, fixture_sha256: str,
                           legacy_metrics: dict, graph_v2_metrics: dict,
                           deltas: dict, tuning_split: dict, eval_split: dict,
                           multihop: dict, hub_bias: dict,
                           core_regression: dict, reproducibility: dict,
                           shadow_report: dict, gate_report: dict,
                           extra: Optional[dict] = None) -> dict:
    """Honest, machine-readable phase07 benchmark envelope."""
    gain_conclusion = "GAIN" if (
        float(deltas.get("ndcg_delta") or 0) > 0
        and float(deltas.get("mrr_delta") or 0) > 0) else "NO_GAIN"
    result = {
        "schema_version": "phase07-graph-relation-benchmark-1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": fixture_name,
        "fixture_sha256": fixture_sha256,
        "tuning_eval_split_separated": {
            "tuning": dict(tuning_split), "eval": dict(eval_split)},
        "legacy_baseline": dict(legacy_metrics),
        "graph_v2": dict(graph_v2_metrics),
        "metrics_deltas": dict(deltas),
        "gain_conclusion": gain_conclusion,
        "useful_multihop": dict(multihop),
        "hub_bias_control": dict(hub_bias),
        "core_qa_non_regression": dict(core_regression),
        "reproducibility": dict(reproducibility),
        "shadow": dict(shadow_report),
        "activation_gate": {
            "graph_v2_activation_claim": False,
            **dict(gate_report),
        },
    }
    if extra:
        result.update(extra)
    return result
