"""Legacy identity migration and evidence-backed rematerialization helpers."""
from __future__ import annotations

import json
from collections import Counter

from entity_resolution_types import normalize_surface, new_opaque_id, stable_hash
from identity_store import utc_now


def migrate_legacy_registry(store, legacy_payload: dict, *, actor="migration") -> dict:
    """Idempotently migrate legacy nodes without force-merging collisions."""
    rows = legacy_payload.get("entities", legacy_payload if isinstance(legacy_payload, list) else [])
    if isinstance(rows, dict):
        rows = [{"entity_id": key, **(value if isinstance(value, dict) else
                 {"canonical_name": str(value)})} for key, value in rows.items()]
    report = Counter(total_legacy=len(rows))
    details = []
    for index, raw in enumerate(rows):
        legacy_id = str(raw.get("entity_id") or raw.get("id") or f"legacy:{index}")
        legacy_name = str(raw.get("canonical_name") or raw.get("name") or legacy_id)
        conn = store._connect()
        existing = conn.execute("SELECT * FROM migration_mappings WHERE legacy_id=?",
                                (legacy_id,)).fetchone()
        conn.close()
        if existing:
            report["rerun_existing"] += 1
            details.append(dict(existing)); continue
        aliases = list(dict.fromkeys([legacy_name] + list(raw.get("aliases") or []) +
                                     list(raw.get("abbreviations") or [])))
        collisions = []
        for alias in aliases:
            norm = normalize_surface(alias)
            collisions.extend(a for a in store.aliases()
                              if a["normalized_surface"] == norm)
        status = "MIGRATED"
        lifecycle = "PROVISIONAL"
        ambiguity = sorted({c["entity_id"] for c in collisions})
        if ambiguity:
            status = "AMBIGUOUS"
            entity_id = None
            report["ambiguous"] += 1
        else:
            entity, _ = store.create_entity(legacy_name,
                raw.get("entity_type", "OTHER_DOMAIN"), lifecycle=lifecycle,
                aliases=aliases[1:], provenance="legacy_migration", actor=actor,
                reason=f"migrate {legacy_id}", creation_key=stable_hash({"legacy_id": legacy_id}))
            entity_id = entity["entity_id"]
            report["migrated_unique"] += 1
            report["provisional"] += 1
        high_impact = int(raw.get("mention_count", 0)) >= 100 or int(raw.get("degree", 0)) >= 20
        review = "HIGH_IMPACT_REVIEW_REQUIRED" if high_impact else "PENDING_REVIEW"
        if high_impact: report["high_impact_review_required"] += 1
        with store.transaction() as conn:
            conn.execute("INSERT INTO migration_mappings VALUES(?,?,?,?,?,?,?,?,?)", (
                legacy_id, legacy_name, entity_id, status, "EXPLICIT_LEGACY_NODE",
                json.dumps({"source": "legacy_registry"}, sort_keys=True),
                json.dumps(ambiguity), review, utc_now()))
            store._event(conn, "LEGACY_MIGRATION", {"legacy_id": legacy_id,
                "entity_id": entity_id, "status": status, "ambiguity": ambiguity,
                "review_status": review}, actor, "legacy identity migration")
        details.append({"legacy_id": legacy_id, "legacy_name": legacy_name,
                        "entity_id": entity_id, "status": status,
                        "ambiguity": ambiguity, "review_status": review})
    report.update({"blocked": 0, "duplicate_candidates": report["ambiguous"],
                   "unresolvable": 0})
    result = {**dict(report), "mappings": sorted(details, key=lambda x: x["legacy_id"]),
              "graph_v2_activation_ready": False}
    result["report_hash"] = stable_hash(result)
    return result


def materialize_mention(store, *, record_id: str, source_snapshot_id: str,
                        canonical_text: str, surface: str, start_offset: int,
                        decision, resolver_version: str,
                        identity_snapshot_id: str) -> dict:
    end_offset = start_offset + len(surface)
    if start_offset < 0 or canonical_text[start_offset:end_offset] != surface:
        raise ValueError("mention span does not match immutable SourceSnapshot text")
    mention_id = new_opaque_id("men")
    selected = getattr(decision, "selected_entity_id", None)
    state = getattr(getattr(decision, "decision", None), "value", None) or str(decision.decision)
    with store.transaction() as conn:
        conn.execute("INSERT INTO mentions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            mention_id, selected, state, record_id, source_snapshot_id,
            start_offset, end_offset, surface, resolver_version,
            identity_snapshot_id, utc_now()))
        store._event(conn, "MENTION_MATERIALIZE", {"mention_id": mention_id,
            "record_id": record_id, "source_snapshot_id": source_snapshot_id,
            "selected_entity_id": selected}, "materializer", "source-backed mention")
    return {"mention_id": mention_id, "record_id": record_id,
            "source_snapshot_id": source_snapshot_id, "start_offset": start_offset,
            "end_offset": end_offset, "surface": surface, "entity_id": selected,
            "decision": state, "identity_snapshot_id": identity_snapshot_id}


def materialize_relation(store, *, subject_mention: dict, predicate: str,
                         object_mention: dict | None = None, object_value=None,
                         evidence_refs: list[dict], assertion_status="ASSERTED",
                         extraction_version="relation-extract-v1",
                         resolver_version="er-v2.0", legacy_edge_hint=None) -> dict:
    if not evidence_refs:
        raise ValueError("legacy edge without SourceSnapshot evidence is ineligible")
    subject = subject_mention.get("entity_id")
    obj = object_mention.get("entity_id") if object_mention else None
    if not subject or (not obj and object_value is None):
        raise ValueError("relation endpoints must be resolved or an exact object value supplied")
    snapshot_id = subject_mention["source_snapshot_id"]
    if any(ref.get("source_snapshot_id") != snapshot_id for ref in evidence_refs):
        raise ValueError("relation EvidenceRefs must bind the source mention snapshot")
    relation_id = new_opaque_id("rel")
    mention_ids = [subject_mention["mention_id"]]
    if object_mention: mention_ids.append(object_mention["mention_id"])
    with store.transaction() as conn:
        # Provisional endpoints remain lower-authority relation assertions.
        lifecycle = {row["entity_id"]: row["lifecycle"] for row in conn.execute(
            "SELECT entity_id,lifecycle FROM entities WHERE entity_id IN (?,?)",
            (subject, obj or subject))}
        effective_status = assertion_status
        if any(lifecycle.get(eid) != "ACTIVE" for eid in (subject, obj) if eid):
            effective_status = "PROVISIONAL"
        conn.execute("INSERT INTO relation_assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            relation_id, subject, predicate, obj, object_value, effective_status,
            json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True),
            json.dumps(mention_ids), snapshot_id,
            json.dumps({"legacy_edge_hint": legacy_edge_hint,
                        "legacy_edge_is_authority": False}, sort_keys=True),
            extraction_version, resolver_version, utc_now()))
        store._event(conn, "RELATION_MATERIALIZE", {"relation_id": relation_id,
            "source_mention_ids": mention_ids, "evidence_ref_ids":
            [r.get("evidence_id") for r in evidence_refs]}, "materializer",
            "source-evidence relation materialization")
    return {"relation_id": relation_id, "subject_entity_id": subject,
            "predicate": predicate, "object_entity_id": obj,
            "object_value": object_value, "assertion_status": effective_status,
            "evidence_refs": evidence_refs, "source_mention_ids": mention_ids,
            "source_snapshot_id": snapshot_id,
            "legacy_edge_is_authority": False}


def rematerialization_plan(store, affected_entity_ids: list[str]) -> dict:
    conn = store._connect()
    try:
        mentions = [row[0] for row in conn.execute(
            f"SELECT mention_id FROM mentions WHERE entity_id IN ({','.join('?' for _ in affected_entity_ids)})",
            affected_entity_ids)] if affected_entity_ids else []
        relations = [row[0] for row in conn.execute(
            f"SELECT relation_id FROM relation_assertions WHERE subject_entity_id IN ({','.join('?' for _ in affected_entity_ids)}) OR object_entity_id IN ({','.join('?' for _ in affected_entity_ids)})",
            affected_entity_ids + affected_entity_ids)] if affected_entity_ids else []
    finally:
        conn.close()
    plan = {"affected_entity_ids": sorted(set(affected_entity_ids)),
            "mention_ids": sorted(mentions), "relation_ids": sorted(relations),
            "source_of_truth": "SourceSnapshot+EvidenceRef", "bounded": True}
    plan["plan_hash"] = stable_hash(plan)
    return plan
