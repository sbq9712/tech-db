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


def rematerialization_plan(store, affected_entity_ids: list[str], *,
                           affected_mention_ids: list[str] | None = None) -> dict:
    """Build a bounded, deterministic evidence-lineage rebuild plan.

    Relation endpoint columns are deliberately not treated as authority.  The
    plan pins the exact mentions and EvidenceRefs from which every affected
    assertion must be rebuilt after an identity mutation.
    """
    affected_entity_ids = sorted(set(affected_entity_ids))
    affected_mention_ids = sorted(set(affected_mention_ids or []))
    conn = store._connect()
    try:
        marks = ",".join("?" for _ in affected_entity_ids)
        entity_mentions = [dict(row) for row in conn.execute(
            f"SELECT * FROM mentions WHERE entity_id IN ({marks}) ORDER BY mention_id",
            affected_entity_ids)] if affected_entity_ids else []
        affected_mentions = set(affected_mention_ids)
        affected_mentions.update(row["mention_id"] for row in entity_mentions)
        all_relations = [dict(row) for row in conn.execute(
            "SELECT * FROM relation_assertions WHERE assertion_status!='SUPERSEDED' "
            "ORDER BY relation_id")]
        relation_rows = []
        for row in all_relations:
            source_mentions = set(json.loads(row["source_mention_ids_json"]))
            if (row["subject_entity_id"] in affected_entity_ids
                    or row["object_entity_id"] in affected_entity_ids
                    or source_mentions & affected_mentions):
                relation_rows.append(row)
        lineage_mention_ids = sorted({mention_id for row in relation_rows
            for mention_id in json.loads(row["source_mention_ids_json"])})
        if lineage_mention_ids:
            lineage_marks = ",".join("?" for _ in lineage_mention_ids)
            mentions = [dict(row) for row in conn.execute(
                f"SELECT * FROM mentions WHERE mention_id IN ({lineage_marks}) ORDER BY mention_id",
                lineage_mention_ids)]
        else:
            mentions = entity_mentions
    finally:
        conn.close()
    if len(mentions) > 5000 or len(relation_rows) > 1000:
        raise ValueError("rematerialization scope exceeds configured bound")
    relations = []
    source_snapshot_ids = set()
    evidence_ref_ids = set()
    for row in relation_rows:
        refs = json.loads(row["evidence_refs_json"])
        sources = json.loads(row["source_mention_ids_json"])
        if not refs or not sources:
            raise ValueError("affected relation lacks rebuild authority")
        source_snapshot_ids.add(row["source_snapshot_id"])
        evidence_ref_ids.update(str(ref.get("evidence_id")) for ref in refs
                                if ref.get("evidence_id") is not None)
        lineage = {
            "relation_id": row["relation_id"],
            "predicate": row["predicate"],
            "object_value": row["object_value"],
            "assertion_status": row["assertion_status"],
            "source_snapshot_id": row["source_snapshot_id"],
            "source_mention_ids": sources,
            "evidence_refs": refs,
            "extraction_version": row["extraction_version"],
            "resolver_version": row["resolver_version"],
            "legacy_edge_hint": json.loads(row["provenance"] or "{}"),
        }
        lineage["lineage_hash"] = stable_hash(lineage)
        relations.append(lineage)
    mention_lineage = [{
        "mention_id": row["mention_id"], "record_id": row["record_id"],
        "source_snapshot_id": row["source_snapshot_id"],
        "start_offset": row["start_offset"], "end_offset": row["end_offset"],
        "surface": row["surface"], "resolver_version": row["resolver_version"],
        "identity_snapshot_id": row["identity_snapshot_id"],
    } for row in mentions]
    plan = {"affected_entity_ids": affected_entity_ids,
            "mention_ids": sorted(affected_mentions),
            "lineage_mention_ids": [row["mention_id"] for row in mentions],
            "affected_mentions": mention_lineage,
            "relation_ids": [row["relation_id"] for row in relations],
            "affected_relations": relations,
            "source_snapshot_ids": sorted(source_snapshot_ids),
            "evidence_ref_ids": sorted(evidence_ref_ids),
            "bounded_scope": {"max_mentions": 5000, "max_relations": 1000,
                              "mention_count": len(affected_mentions),
                              "lineage_mention_count": len(mentions),
                              "relation_count": len(relations)},
            "source_of_truth": "SourceSnapshot+MentionOffsets+EvidenceRef",
            "bounded": True}
    plan["plan_hash"] = stable_hash(plan)
    return plan


def execute_relation_rematerialization(store, plan: dict, *, operation_id: str,
                                       actor: str, reason: str,
                                       crash_hook=None) -> dict:
    """Rebuild affected assertions from pinned lineage in one transaction.

    A failure (including an injected crash) rolls back the entire relation
    rebuild.  A committed REMATERIALIZATION_COMPLETE event is the idempotent
    resume checkpoint.
    """
    completed = [e for e in store.mutation_events()
                 if e["operation_id"] == operation_id
                 and e["event_type"] == "REMATERIALIZATION_COMPLETE"]
    if completed:
        return json.loads(completed[-1]["payload_json"])
    expected = dict(plan)
    plan_hash = expected.pop("plan_hash", None)
    if not plan_hash or stable_hash(expected) != plan_hash:
        raise ValueError("rematerialization plan hash mismatch")
    mappings = []
    with store.transaction() as conn:
        for lineage in plan.get("affected_relations", []):
            old = conn.execute("SELECT * FROM relation_assertions WHERE relation_id=?",
                               (lineage["relation_id"],)).fetchone()
            if old is None:
                raise ValueError("planned relation disappeared")
            refs = lineage["evidence_refs"]
            mention_ids = lineage["source_mention_ids"]
            if (not refs or not mention_ids or
                    any(ref.get("source_snapshot_id") != lineage["source_snapshot_id"]
                        for ref in refs)):
                raise ValueError("relation lineage is not SourceSnapshot/EvidenceRef complete")
            placeholders = ",".join("?" for _ in mention_ids)
            current_mentions = {row["mention_id"]: dict(row) for row in conn.execute(
                f"SELECT * FROM mentions WHERE mention_id IN ({placeholders})", mention_ids)}
            if set(current_mentions) != set(mention_ids):
                raise ValueError("planned source mention disappeared")
            pinned_mentions = {m["mention_id"]: m for m in plan["affected_mentions"]}
            for mention_id in mention_ids:
                current = current_mentions[mention_id]
                pinned = pinned_mentions.get(mention_id)
                if (pinned is None or current["source_snapshot_id"] != pinned["source_snapshot_id"]
                        or current["start_offset"] != pinned["start_offset"]
                        or current["end_offset"] != pinned["end_offset"]
                        or current["surface"] != pinned["surface"]):
                    raise ValueError("mention offset lineage changed after dry-run")
            subject = current_mentions[mention_ids[0]]["entity_id"]
            obj = current_mentions[mention_ids[1]]["entity_id"] if len(mention_ids) > 1 else None
            if not subject or (not obj and lineage.get("object_value") is None):
                raise ValueError("rematerialized relation has unresolved endpoint")
            lifecycle = {row["entity_id"]: row["lifecycle"] for row in conn.execute(
                "SELECT entity_id,lifecycle FROM entities WHERE entity_id IN (?,?)",
                (subject, obj or subject))}
            status = lineage["assertion_status"]
            if status == "SUPERSEDED" or any(lifecycle.get(eid) != "ACTIVE"
                    for eid in (subject, obj) if eid):
                status = "PROVISIONAL"
            new_relation_id = new_opaque_id("rel")
            provenance = {
                "rematerialized_from_relation_id": lineage["relation_id"],
                "rematerialization_operation_id": operation_id,
                "rematerialization_plan_hash": plan_hash,
                "legacy_edge_hint": lineage.get("legacy_edge_hint"),
                "legacy_edge_is_authority": False,
            }
            conn.execute("INSERT INTO relation_assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                new_relation_id, subject, lineage["predicate"], obj,
                lineage.get("object_value"), status,
                json.dumps(refs, ensure_ascii=False, sort_keys=True),
                json.dumps(mention_ids), lineage["source_snapshot_id"],
                json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                lineage["extraction_version"], lineage["resolver_version"], utc_now()))
            rebuilt = conn.execute(
                "SELECT * FROM relation_assertions WHERE relation_id=?",
                (new_relation_id,)).fetchone()
            if (rebuilt is None or rebuilt["subject_entity_id"] != subject
                    or rebuilt["object_entity_id"] != obj
                    or rebuilt["source_snapshot_id"] != lineage["source_snapshot_id"]
                    or json.loads(rebuilt["source_mention_ids_json"]) != mention_ids
                    or json.loads(rebuilt["evidence_refs_json"]) != refs
                    or rebuilt["extraction_version"] != lineage["extraction_version"]
                    or rebuilt["resolver_version"] != lineage["resolver_version"]):
                raise ValueError("rebuilt relation assertion failed validation")
            conn.execute("UPDATE relation_assertions SET assertion_status='SUPERSEDED' WHERE relation_id=?",
                         (lineage["relation_id"],))
            mappings.append({"old_relation_id": lineage["relation_id"],
                             "new_relation_id": new_relation_id,
                             "subject_entity_id": subject,
                             "object_entity_id": obj,
                             "source_snapshot_id": lineage["source_snapshot_id"],
                             "evidence_ref_ids": [ref.get("evidence_id") for ref in refs],
                             "source_mention_ids": mention_ids})
        if crash_hook:
            crash_hook("during_rematerialization")
        payload = {"checkpoint": "REMATERIALIZATION_COMPLETE",
                   "plan_hash": plan_hash, "rebuilt_relations": mappings,
                   "validated": True,
                   "source_of_truth": plan["source_of_truth"]}
        store._event(conn, "REMATERIALIZATION_COMPLETE", payload, actor, reason,
                     operation_id=operation_id)
    return payload
