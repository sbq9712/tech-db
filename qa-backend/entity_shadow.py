"""Non-interfering ER shadow observations and activation evidence (RT-075)."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean

from entity_resolution_types import stable_hash


def resolve_ingest_shadow(record: dict, snapshot_payload: dict,
                          monitor: "EntityShadowMonitor") -> list[dict]:
    """Run canonical resolution as an ingest shadow with zero mutations."""
    from entity_resolver_v2 import QueryEntityResolver
    text = " ".join(str(record.get(key) or "") for key in ("t", "fb", "b"))[:4000]
    resolver = QueryEntityResolver(snapshot_payload)
    rows = []
    for decision in resolver.resolve_query(text):
        rows.append(monitor.observe(
            serving_decision={"decision": "LEGACY_UNCHANGED",
                              "selected_entity_id": None},
            shadow_decision=decision.to_dict(),
            entity_class=(decision.provisional_proposal or {}).get(
                "entity_type", "OTHER_DOMAIN"),
            latency_ms=0, source="ingest"))
    return rows


@dataclass
class EntityShadowMonitor:
    window_type: str = "CI_REPLAY"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    observations: list[dict] = field(default_factory=list)
    production_activation_claim: bool = False

    def observe(self, *, serving_decision: dict, shadow_decision: dict,
                entity_class: str, latency_ms: float, candidate_latency_ms: float = 0,
                adjudicator_latency_ms: float = 0, model_calls: int = 0,
                cost_proxy: float = 0, cache_hit: bool = False,
                labeled_truth_entity_id=None, source="query"):
        # This method only appends comparison telemetry. It has no store,
        # graph, answer, or serving-decision mutation capability.
        selected = shadow_decision.get("selected_entity_id")
        false_link = (shadow_decision.get("decision") == "LINK"
                      and labeled_truth_entity_id is not None
                      and selected != labeled_truth_entity_id)
        row = {
            "event_id": stable_hash({"n": len(self.observations), "shadow": shadow_decision,
                                     "serving": serving_decision})[:24],
            "source": source, "entity_class": entity_class,
            "serving_decision": serving_decision.get("decision"),
            "shadow_decision": shadow_decision.get("decision"),
            "serving_entity_id": serving_decision.get("selected_entity_id"),
            "shadow_entity_id": selected,
            "agreement": (serving_decision.get("decision") == shadow_decision.get("decision")
                          and serving_decision.get("selected_entity_id") == selected),
            "false_link_candidate": false_link,
            "latency_ms": float(latency_ms),
            "candidate_latency_ms": float(candidate_latency_ms),
            "adjudicator_latency_ms": float(adjudicator_latency_ms),
            "model_calls": int(model_calls), "cost_proxy": float(cost_proxy),
            "cache_hit": bool(cache_hit),
            "block_violation": (serving_decision.get("decision") == "BLOCKED"
                                and shadow_decision.get("decision") == "LINK"),
        }
        self.observations.append(row)
        return row

    def report(self, *, duration_days: float = 0,
               equivalent_replay_explicitly_approved: bool = False) -> dict:
        counts = Counter(row["shadow_decision"] for row in self.observations)
        by_class = defaultdict(Counter)
        for row in self.observations:
            by_class[row["entity_class"]][row["shadow_decision"]] += 1
            by_class[row["entity_class"]]["events"] += 1
            by_class[row["entity_class"]]["false_link_candidates"] += int(row["false_link_candidate"])
            by_class[row["entity_class"]]["agreements"] += int(row["agreement"])
        real_gate = (self.window_type == "REAL_WINDOW" and len(self.observations) >= 1000
                     and duration_days >= 7)
        replay_gate = (self.window_type == "CI_REPLAY"
                       and equivalent_replay_explicitly_approved)
        activation = real_gate or replay_gate
        latencies = [r["latency_ms"] for r in self.observations]
        report = {
            "schema_version": "entity-shadow-report-1.0",
            "window_type": self.window_type,
            "started_at": self.started_at,
            "representative_event_count": len(self.observations),
            "duration_days": float(duration_days),
            "decision_counts": {state: counts[state] for state in
                                ("LINK", "NEW", "AMBIGUOUS", "BLOCKED")},
            "per_class": {key: dict(value) for key, value in sorted(by_class.items())},
            "top1_agreement": (sum(r["agreement"] for r in self.observations)
                               / max(1, len(self.observations))),
            "false_link_candidates": sum(r["false_link_candidate"] for r in self.observations),
            "latency_ms_mean": mean(latencies) if latencies else 0,
            "model_calls": sum(r["model_calls"] for r in self.observations),
            "cost_proxy": sum(r["cost_proxy"] for r in self.observations),
            "cache_hits": sum(r["cache_hit"] for r in self.observations),
            "rollback_triggers": {
                "high_impact_false_link": any(r["false_link_candidate"] for r in self.observations),
                "duplicate_canonical_creation": False,
                "block_rule_violation": any(r["block_violation"] for r in self.observations),
                "graph_identity_corruption": False,
            },
            "real_window_gate_satisfied": real_gate,
            "equivalent_replay_explicitly_approved": equivalent_replay_explicitly_approved,
            "activation_gate_satisfied": activation,
            "production_activation_claim": bool(self.production_activation_claim and activation),
            "non_interference": {"identity_store_mutations": 0,
                                 "serving_decision_mutations": 0,
                                 "graph_mutations": 0, "answer_mutations": 0},
        }
        report["report_hash"] = stable_hash(report)
        return report
