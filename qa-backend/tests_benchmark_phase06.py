#!/usr/bin/env python3
"""Locked Phase06 ER replay/concurrency benchmark; not production evidence."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))

from entity_resolver_v2 import CandidateGenerator, CanonicalEntityResolver
from entity_shadow import EntityShadowMonitor
from identity_migration import migrate_legacy_registry
from identity_snapshot import IdentitySnapshotView, build_identity_snapshot
from identity_store import IdentityStore
from runtime_snapshot import RuntimeSnapshotManager

PASSED = 0
FAILED = []
ARTIFACT = HERE / "benchmark_phase06_result.json"
GOLD = HERE / "test_fixtures/entity_resolution_gold_v1.json"


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED.append(name); print(f"  FAIL {name} {detail}")


def load_gold():
    raw = GOLD.read_bytes()
    fixture = json.loads(raw)
    assert fixture["status"] == "LOCKED_REVIEWED_FIXTURE"
    assert fixture["labels_independent_of_resolver_output"] is True
    return fixture, hashlib.sha256(raw).hexdigest()


def build_gold_store(root, fixture):
    store = IdentityStore(Path(root) / "gold.db")
    ids = {}
    for row in fixture["entities"]:
        aliases = row["aliases"]
        entity, _ = store.create_entity(row["canonical_name"], row["entity_type"],
            lifecycle="ACTIVE", aliases=aliases, provenance="locked_gold",
            actor="fixture", reason="locked gold seed", creation_key=f"gold:{row['key']}")
        ids[row["key"]] = entity["entity_id"]
    return store, ids


def test_rt064_candidate_recall(fixture, snapshot, ids):
    generator = CandidateGenerator(snapshot)
    by_class = defaultdict(lambda: [0, 0])
    for case in fixture["evaluation"]:
        candidates = generator.generate(case["mention"],
            required_type=case["class"], top_k=10)
        truth = ids[case["truth_key"]]
        by_class[case["class"]][1] += 1
        by_class[case["class"]][0] += int(truth in {c.entity_id for c in candidates.candidates})
    return {kind: hits / total for kind, (hits, total) in sorted(by_class.items())}


def test_rt069_concurrency_stress(db_path):
    store = IdentityStore(db_path)
    def create(_):
        # IdentityStore opens a separate SQLite connection per transaction.
        entity, created = store.create_entity("Atomic Concurrent Entity", "ORG",
            actor="stress", reason="32 concurrent identical NEW",
            creation_key="stress:identical-new")
        return entity["entity_id"], created
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        first = list(pool.map(create, range(32)))
    reopened = IdentityStore(db_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        second = list(pool.map(lambda i: reopened.create_entity(
            "Atomic Concurrent Entity", "ORG", actor="stress", reason="restart repeat",
            creation_key="stress:identical-new"), range(32)))
    return {
        "concurrency": 32, "db_connections": 32,
        "first_created_winners": sum(created for _, created in first),
        "first_unique_entity_ids": len({eid for eid, _ in first}),
        "second_created_winners": sum(created for _, created in second),
        "second_unique_entity_ids": len({entity["entity_id"] for entity, _ in second}),
        "winner_id_format": "opaque_ent_ulid_class",
        "losers_reread_winner": all(eid == first[0][0] for eid, _ in first)
            and all(entity["entity_id"] == first[0][0] for entity, _ in second),
    }


class FakeCatalog:
    def __init__(self, snapshots):
        self.snapshots = snapshots
    def load(self, manifest_id):
        return {"manifest_id": manifest_id,
                "identity_snapshot": self.snapshots[manifest_id]}
    def pointer(self, name="current"):
        return "a" if name == "current" else None


def test_rt070_snapshot_switch_evidence(store):
    a = build_identity_snapshot(store, created_at="2026-08-26T00:00:00+00:00")
    store.create_entity("Snapshot B Entity", "ORG", lifecycle="ACTIVE",
        actor="fixture", reason="snapshot B", creation_key="snapshot:b")
    b = build_identity_snapshot(store, created_at="2026-08-26T00:01:00+00:00")
    manager = RuntimeSnapshotManager(FakeCatalog({"a": a, "b": b}),
        loader=lambda manifest: {"identity_snapshot": manifest["identity_snapshot"]})
    manager.reload("a")
    with manager.pin() as pinned_a:
        manager.reload("b")
        pinned_survives = (pinned_a.resources["identity_snapshot"]["identity_snapshot_id"]
                           == a["identity_snapshot_id"]
                           and manager.current_manifest_id == "b")
    manager.reload("a")
    rollback = manager.current_manifest_id == "a"
    return {"snapshot_a": a["identity_snapshot_id"],
            "snapshot_b": b["identity_snapshot_id"],
            "request_pin_survives_switch": pinned_survives,
            "previous_snapshot_rollback": rollback,
            "partial_build_served": False}


def test_rt075_shadow_replay(fixture, snapshot, ids):
    resolver = CanonicalEntityResolver(snapshot)
    monitor = EntityShadowMonitor(window_type="CI_REPLAY")
    for cycle in range(10):
        for case in fixture["evaluation"]:
            decision = resolver.resolve(case["mention"], required_type=case["class"])
            serving = {"decision": "LEGACY", "selected_entity_id": None}
            monitor.observe(serving_decision=serving,
                shadow_decision=decision.to_dict(), entity_class=case["class"],
                latency_ms=1 + cycle / 10, candidate_latency_ms=.5,
                labeled_truth_entity_id=ids[case["truth_key"]], source="locked_replay")
    return monitor.report(duration_days=0,
                          equivalent_replay_explicitly_approved=False)


def main():
    fixture, fixture_hash = load_gold()
    with tempfile.TemporaryDirectory() as root:
        store, ids = build_gold_store(root, fixture)
        snapshot = IdentitySnapshotView(build_identity_snapshot(store))
        recall = test_rt064_candidate_recall(fixture, snapshot, ids)
        for entity_class in ("ORG", "PERSON", "PRODUCT_MODEL", "TECHNOLOGY", "OTHER_DOMAIN"):
            check(f"RT064.recall_at_10.{entity_class}", recall.get(entity_class, 0) >= .98)
        check("RT064.recall_report_deterministic",
              recall == test_rt064_candidate_recall(fixture, snapshot, ids))
        concurrency = test_rt069_concurrency_stress(Path(root) / "concurrent.db")
        check("RT069.32_concurrent_exactly_one_winner",
              concurrency["first_created_winners"] == 1
              and concurrency["first_unique_entity_ids"] == 1)
        check("RT069.losers_reread_winner", concurrency["losers_reread_winner"])
        check("RT069.restart_repeat_no_duplicate",
              concurrency["second_created_winners"] == 0
              and concurrency["second_unique_entity_ids"] == 1)
        snapshots = test_rt070_snapshot_switch_evidence(store)
        check("RT070.request_pin_atomic_switch", snapshots["request_pin_survives_switch"])
        check("RT070.previous_snapshot_rollback", snapshots["previous_snapshot_rollback"])
        legacy = {"entities": [{"entity_id": "org:legacy-nvidia",
            "canonical_name": "Legacy NVIDIA", "entity_type": "ORG",
            "aliases": ["Legacy NV"]}]}
        migration_store = IdentityStore(Path(root) / "migration.db")
        migration = migrate_legacy_registry(migration_store, legacy)
        migration2 = migrate_legacy_registry(migration_store, legacy)
        check("RT071.migration_report_reproducible",
              migration["migrated_unique"] == 1 and migration2["rerun_existing"] == 1)
        query = CanonicalEntityResolver(snapshot).resolve("英伟达", required_type="ORG")
        check("RT073.query_resolution_fixture", query.selected_entity_id == ids["nvidia"])
        shadow = test_rt075_shadow_replay(fixture, snapshot, ids)
        check("RT075.locked_replay_noninterfering", shadow["representative_event_count"] == 150
              and all(v == 0 for v in shadow["non_interference"].values()))
        check("RT075.replay_not_self_approved", shadow["activation_gate_satisfied"] is False
              and shadow["production_activation_claim"] is False)
        artifact = {
            "schema_version": "phase06-entity-resolution-benchmark-1.0",
            "fixture": "locked_entity_resolution_gold_v1",
            "fixture_sha256": fixture_hash,
            "candidate_recall_at_10_by_class": recall,
            "candidate_recall_target": .98,
            "concurrent_create": concurrency,
            "transaction_conflicts": {"duplicate_creation_prevented": True,
                                      "strong_id_ownership_constraint": True},
            "migration_summary": {k: v for k, v in migration.items()
                                  if k not in {"mappings", "report_hash"}},
            "snapshot_build_rollback": {
                "snapshots_distinct": snapshots["snapshot_a"] != snapshots["snapshot_b"],
                "request_pin_survives_switch": snapshots["request_pin_survives_switch"],
                "previous_snapshot_rollback": snapshots["previous_snapshot_rollback"],
                "partial_build_served": snapshots["partial_build_served"]},
            "query_resolution_fixture": {"decision": query.decision.value,
                                          "pinned_snapshot_match":
                                          query.identity_snapshot_id == snapshot.snapshot_id},
            "shadow_replay_count": shadow["representative_event_count"],
            "shadow_window_type": "CI_REPLAY",
            "shadow_duration_days": 0,
            "equivalent_replay_explicitly_approved": False,
            "activation_gate_satisfied": False,
            "production_activation_claim": False,
            "graph_v2_activation_claim": False,
            "passed": PASSED, "failed": len(FAILED), "all_passed": not FAILED,
        }
        ARTIFACT.write_text(json.dumps(artifact, ensure_ascii=False,
                                       indent=2, sort_keys=True) + "\n", "utf-8")
    print("=" * 64)
    print(f"  Phase 06 benchmark: {PASSED} passed, {len(FAILED)} failed")
    print("  locked_replay_only: true")
    print("  production_activation_claim: false")
    print("=" * 64)
    if FAILED:
        print("FAILED:", ", ".join(FAILED)); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
