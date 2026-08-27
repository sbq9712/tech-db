"""Authenticated Entity Admin service and controlled offline mutations."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

from entity_resolution_types import EntityLifecycle, sanitize_business_text, stable_hash
from identity_migration import (execute_relation_rematerialization,
                                rematerialization_plan)
from identity_snapshot import build_identity_snapshot
from identity_store import DependentMutationConflict, IdentityConflict, utc_now


class AdminAuthError(PermissionError):
    pass


@dataclass(frozen=True)
class MutationPreview:
    operation_id: str
    operation: str
    plan: dict
    dry_run_hash: str
    confirmation_token: str


class EntityAdminService:
    def __init__(self, store, *, operator_key: str,
                 publish_callback: Optional[Callable[[dict], str]] = None):
        if not operator_key or len(operator_key) < 12:
            raise ValueError("operator key must be configured server-side")
        self.store = store
        self._key = operator_key.encode("utf-8")
        self.publish_callback = publish_callback

    def authenticate(self, presented_key: str):
        if not hmac.compare_digest(self._key, str(presented_key or "").encode("utf-8")):
            raise AdminAuthError("operator authentication required")

    def _token(self, operation_id: str, digest: str) -> str:
        return hmac.new(self._key, f"{operation_id}:{digest}".encode(),
                        hashlib.sha256).hexdigest()

    def search(self, key: str, query: str) -> list[dict]:
        self.authenticate(key)
        return self.store.search_entities(query)

    def inspect(self, key: str, entity_id: str) -> dict:
        self.authenticate(key)
        entity = self.store.get_entity(entity_id)
        if not entity:
            raise KeyError(entity_id)
        return entity

    def rename(self, key: str, entity_id: str, name: str, *, actor: str, reason: str):
        self.authenticate(key)
        return self.store.update_entity(entity_id,
            canonical_name=sanitize_business_text(name, limit=256), actor=actor, reason=reason)

    def correct_type(self, key: str, entity_id: str, entity_type: str, *, actor: str, reason: str):
        self.authenticate(key)
        return self.store.update_entity(entity_id, entity_type=entity_type,
                                        actor=actor, reason=reason)

    def promote(self, key: str, entity_id: str, *, actor: str, reason: str,
                provenance: str):
        self.authenticate(key)
        if not provenance:
            raise ValueError("promotion provenance required")
        return self.store.update_entity(entity_id, lifecycle=EntityLifecycle.ACTIVE.value,
                                        actor=actor, reason=reason)

    def reject(self, key: str, entity_id: str, *, actor: str, reason: str):
        self.authenticate(key)
        return self.store.update_entity(entity_id, lifecycle=EntityLifecycle.REJECTED.value,
                                        actor=actor, reason=reason)

    def add_alias(self, key: str, entity_id: str, surface: str, *, actor: str, reason: str,
                  provenance="operator"):
        self.authenticate(key)
        return self.store.add_alias(entity_id, sanitize_business_text(surface, limit=256),
                                    actor=actor, reason=reason, provenance=provenance)

    def unlink_alias(self, key: str, alias_id: str, *, actor: str, reason: str):
        self.authenticate(key)
        return self.store.set_alias_status(alias_id, "REVOKED",
                                           actor=actor, reason=reason)

    def link_strong_id(self, key: str, entity_id: str, id_type: str, value: str,
                       *, provenance: str, actor: str, reason: str):
        self.authenticate(key)
        return self.store.add_strong_id(entity_id, id_type, value,
            provenance=provenance, actor=actor, reason=reason)

    def unlink_strong_id(self, key: str, strong_id_id: str, *, actor: str,
                         reason: str):
        self.authenticate(key)
        return self.store.set_strong_id_status(strong_id_id, "REVOKED",
                                               actor=actor, reason=reason)

    def override(self, key: str, condition: dict, target_entity_id: str, *,
                 actor: str, reason: str, **validity):
        self.authenticate(key)
        return self.store.add_rule("OVERRIDE", condition,
            target_entity_id=target_entity_id, actor=actor, reason=reason, **validity)

    def block(self, key: str, condition: dict, *, actor: str, reason: str, **validity):
        self.authenticate(key)
        return self.store.add_rule("BLOCK", condition, actor=actor, reason=reason, **validity)

    def revoke_rule(self, key: str, rule_id: str, *, actor: str, reason: str):
        self.authenticate(key)
        return self.store.set_rule_status(rule_id, "REVOKED",
                                          actor=actor, reason=reason)

    def _impact(self, source_ids: list[str], destination_id: str | None) -> dict:
        conn = self.store._connect()
        try:
            marks = ",".join("?" for _ in source_ids)
            aliases = conn.execute(f"SELECT COUNT(*) FROM aliases WHERE entity_id IN ({marks})", source_ids).fetchone()[0]
            strong = conn.execute(f"SELECT COUNT(*) FROM strong_ids WHERE entity_id IN ({marks})", source_ids).fetchone()[0]
            mentions = conn.execute(f"SELECT COUNT(*) FROM mentions WHERE entity_id IN ({marks})", source_ids).fetchone()[0]
            relations = conn.execute(f"SELECT COUNT(*) FROM relation_assertions WHERE subject_entity_id IN ({marks}) OR object_entity_id IN ({marks})", source_ids + source_ids).fetchone()[0]
            overrides = conn.execute(f"SELECT COUNT(*) FROM rules WHERE target_entity_id IN ({marks})", source_ids).fetchone()[0]
            return {"entity_count": len(source_ids), "alias_rows": aliases,
                    "strong_ids": strong, "mentions": mentions,
                    "relations": relations, "redirects": len(source_ids),
                    "overrides": overrides, "destination_id": destination_id,
                    "snapshot_rebuild_required": True,
                    "relation_rematerialization_required": relations > 0 or mentions > 0}
        finally:
            conn.close()

    def _checkpoint(self, operation_id: str, event_type: str, payload: dict,
                    actor: str, reason: str):
        with self.store.transaction() as conn:
            self.store._event(conn, event_type, payload, actor, reason,
                              operation_id=operation_id)

    def merge_dry_run(self, key: str, source_ids: list[str], destination_id: str, *,
                      actor: str, reason: str) -> MutationPreview:
        self.authenticate(key)
        unique = sorted(set(source_ids))
        if destination_id in unique or len(unique) < 1:
            raise ValueError("merge requires source IDs distinct from destination")
        entities = [self.store.get_entity(eid) for eid in unique + [destination_id]]
        if any(e is None for e in entities):
            raise KeyError("unknown merge entity")
        operation_id = __import__("entity_resolution_types").new_opaque_id("op")
        relation_plan = rematerialization_plan(self.store, unique)
        plan = {"operation": "MERGE", "operation_id": operation_id,
                "source_ids": unique, "destination_id": destination_id,
                "store_revision_before": self.store.revision(),
                "snapshot_before": build_identity_snapshot(
                    self.store)["identity_snapshot_id"], "actor": actor,
                "reason": sanitize_business_text(reason),
                "impact": self._impact(unique, destination_id),
                "rematerialization": relation_plan,
                "reversible_plan": "event-lineage-compensating-unmerge"}
        digest = stable_hash(plan)
        return MutationPreview(operation_id, "MERGE", plan, digest,
                               self._token(operation_id, digest))

    def confirm_merge(self, key: str, preview: MutationPreview, confirmation_token: str,
                      *, crash_hook=None) -> dict:
        self.authenticate(key)
        if stable_hash(preview.plan) != preview.dry_run_hash:
            raise IdentityConflict("dry-run plan changed")
        if not hmac.compare_digest(self._token(preview.operation_id, preview.dry_run_hash),
                                   confirmation_token):
            raise IdentityConflict("confirmation mismatch")
        existing = [e for e in self.store.mutation_events()
                    if e["operation_id"] == preview.operation_id
                    and e["event_type"] == "MERGE"]
        if not existing and self.store.revision() != preview.plan["store_revision_before"]:
            raise DependentMutationConflict("store changed after dry-run; regenerate impact report")
        sources = preview.plan["source_ids"]
        destination = preview.plan["destination_id"]
        if not existing:
          with self.store.transaction() as conn:
            before = {eid: dict(conn.execute("SELECT * FROM entities WHERE entity_id=?", (eid,)).fetchone())
                      for eid in sources}
            alias_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM aliases WHERE entity_id IN ({','.join('?' for _ in sources)})", sources)]
            strong_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM strong_ids WHERE entity_id IN ({','.join('?' for _ in sources)})", sources)]
            rule_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM rules WHERE target_entity_id IN ({','.join('?' for _ in sources)})", sources)]
            mention_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM mentions WHERE entity_id IN ({','.join('?' for _ in sources)})", sources)]
            destination_mention_ids = [row[0] for row in conn.execute(
                "SELECT mention_id FROM mentions WHERE entity_id=?", (destination,))]
            relation_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM relation_assertions WHERE subject_entity_id IN ({','.join('?' for _ in sources)}) OR object_entity_id IN ({','.join('?' for _ in sources)})",
                sources + sources)]
            for alias in alias_rows:
                duplicate = conn.execute("""SELECT alias_id FROM aliases
                    WHERE entity_id=? AND normalized_surface=? AND alias_type=? AND status=?""",
                    (destination, alias["normalized_surface"], alias["alias_type"],
                     alias["status"])).fetchone()
                if duplicate:
                    conn.execute("DELETE FROM aliases WHERE alias_id=?", (alias["alias_id"],))
                else:
                    conn.execute("UPDATE aliases SET entity_id=? WHERE alias_id=?",
                                 (destination, alias["alias_id"]))
            conn.execute(f"UPDATE mentions SET entity_id=? WHERE entity_id IN ({','.join('?' for _ in sources)})",
                         [destination] + sources)
            conn.execute(f"UPDATE strong_ids SET entity_id=? WHERE entity_id IN ({','.join('?' for _ in sources)})",
                         [destination] + sources)
            conn.execute(f"UPDATE rules SET target_entity_id=? WHERE target_entity_id IN ({','.join('?' for _ in sources)})",
                         [destination] + sources)
            conn.execute(f"UPDATE entities SET lifecycle='TOMBSTONED', redirect_entity_id=?, updated_at=?, version=version+1 WHERE entity_id IN ({','.join('?' for _ in sources)})",
                         [destination, utc_now()] + sources)
            payload = {**preview.plan, "before_entities": before,
                       "pre_merge_alias_rows": alias_rows,
                       "pre_merge_strong_id_rows": strong_rows,
                       "pre_merge_rule_rows": rule_rows,
                       "pre_merge_mention_rows": mention_rows,
                       "pre_destination_mention_ids": destination_mention_ids,
                       "pre_merge_relation_rows": relation_rows,
                       "dry_run_hash": preview.dry_run_hash,
                       "confirmation_token_hash": hashlib.sha256(
                           confirmation_token.encode()).hexdigest(),
                       "checkpoint": "DB_MUTATION_COMPLETE",
                       "intended_snapshot_after": "BUILD_REQUIRED"}
            self.store._event(conn, "MERGE", payload, preview.plan["actor"],
                              preview.plan["reason"], operation_id=preview.operation_id)
            if crash_hook:
                crash_hook("before_commit")
        rematerialized = execute_relation_rematerialization(
            self.store, preview.plan["rematerialization"],
            operation_id=preview.operation_id, actor=preview.plan["actor"],
            reason=preview.plan["reason"], crash_hook=crash_hook)
        snapshot = build_identity_snapshot(self.store)
        self._checkpoint(preview.operation_id, "SNAPSHOT_BUILD", {
            "identity_snapshot_id": snapshot["identity_snapshot_id"],
            "checkpoint": "SNAPSHOT_BUILD_COMPLETE"},
            preview.plan["actor"], preview.plan["reason"])
        if crash_hook:
            crash_hook("after_build_before_switch")
        manifest_id = self.publish_callback(snapshot) if self.publish_callback else None
        if manifest_id is not None:
            self._checkpoint(preview.operation_id, "SNAPSHOT_PUBLISH", {
                "identity_snapshot_id": snapshot["identity_snapshot_id"],
                "manifest_id": manifest_id, "checkpoint": "ATOMIC_SWITCH_COMPLETE"},
                preview.plan["actor"], preview.plan["reason"])
        return {"operation_id": preview.operation_id, "snapshot": snapshot,
                "published_manifest_id": manifest_id,
                "serving_changed": manifest_id is not None,
                "rematerialization": rematerialized}

    def split_dry_run(self, key: str, source_id: str, *, new_name: str,
                      entity_type: str, mention_ids: list[str], actor: str,
                      reason: str) -> MutationPreview:
        self.authenticate(key)
        if not self.store.get_entity(source_id) or not mention_ids:
            raise ValueError("split requires a source and explicit mention provenance")
        conn = self.store._connect()
        try:
            existing = {row[0] for row in conn.execute(
                f"SELECT mention_id FROM mentions WHERE entity_id=? AND mention_id IN ({','.join('?' for _ in mention_ids)})",
                [source_id] + mention_ids)}
        finally:
            conn.close()
        if existing != set(mention_ids):
            raise ValueError("split mentions must belong to source entity")
        operation_id = __import__("entity_resolution_types").new_opaque_id("op")
        relation_plan = rematerialization_plan(
            self.store, [source_id], affected_mention_ids=mention_ids)
        plan = {"operation": "SPLIT", "operation_id": operation_id,
                "source_id": source_id, "new_name": sanitize_business_text(new_name, limit=256),
                "entity_type": entity_type, "mention_ids": sorted(mention_ids),
                "store_revision_before": self.store.revision(), "actor": actor,
                "reason": sanitize_business_text(reason),
                "rematerialization": relation_plan,
                "impact": {"mentions": len(mention_ids),
                           "relation_rematerialization_required": True,
                           "snapshot_rebuild_required": True}}
        digest = stable_hash(plan)
        return MutationPreview(operation_id, "SPLIT", plan, digest,
                               self._token(operation_id, digest))

    def confirm_split(self, key: str, preview: MutationPreview,
                      confirmation_token: str, *, crash_hook=None) -> dict:
        self.authenticate(key)
        if stable_hash(preview.plan) != preview.dry_run_hash or not hmac.compare_digest(
                self._token(preview.operation_id, preview.dry_run_hash), confirmation_token):
            raise IdentityConflict("confirmation mismatch")
        existing = [e for e in self.store.mutation_events()
                    if e["operation_id"] == preview.operation_id
                    and e["event_type"] == "SPLIT"]
        if not existing and self.store.revision() != preview.plan["store_revision_before"]:
            raise DependentMutationConflict("store changed after dry-run")
        # Entity creation and mention reassignment must share one transaction,
        # so perform the complete operation against one DB connection.
        from entity_resolution_types import new_opaque_id, normalize_surface
        entity_id = (json.loads(existing[-1]["payload_json"])["new_entity_id"]
                     if existing else new_opaque_id("ent"))
        now = utc_now()
        plan = preview.plan
        if not existing:
          with self.store.transaction() as conn:
            conn.execute("INSERT INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                entity_id, plan["new_name"], normalize_surface(plan["new_name"]),
                plan["entity_type"], "PROVISIONAL", stable_hash({"split": preview.operation_id}),
                None, "split_operation", now, now, 1))
            self.store._insert_alias(conn, entity_id, plan["new_name"], "CANONICAL",
                                     "split_operation", plan["actor"], plan["reason"])
            conn.execute(f"UPDATE mentions SET entity_id=? WHERE mention_id IN ({','.join('?' for _ in plan['mention_ids'])})",
                         [entity_id] + plan["mention_ids"])
            self.store._event(conn, "SPLIT", {**plan, "new_entity_id": entity_id,
                "checkpoint": "DB_MUTATION_COMPLETE"}, plan["actor"], plan["reason"],
                operation_id=preview.operation_id)
            if crash_hook:
                crash_hook("before_commit")
        rematerialized = execute_relation_rematerialization(
            self.store, plan["rematerialization"], operation_id=preview.operation_id,
            actor=plan["actor"], reason=plan["reason"], crash_hook=crash_hook)
        snapshot = build_identity_snapshot(self.store)
        self._checkpoint(preview.operation_id, "SNAPSHOT_BUILD", {
            "identity_snapshot_id": snapshot["identity_snapshot_id"],
            "checkpoint": "SNAPSHOT_BUILD_COMPLETE"},
            plan["actor"], plan["reason"])
        manifest_id = self.publish_callback(snapshot) if self.publish_callback else None
        if manifest_id is not None:
            self._checkpoint(preview.operation_id, "SNAPSHOT_PUBLISH", {
                "identity_snapshot_id": snapshot["identity_snapshot_id"],
                "manifest_id": manifest_id, "checkpoint": "ATOMIC_SWITCH_COMPLETE"},
                plan["actor"], plan["reason"])
        return {"operation_id": preview.operation_id, "new_entity_id": entity_id,
                "snapshot": snapshot, "published_manifest_id": manifest_id,
                "relation_rematerialization_required": True,
                "rematerialization": rematerialized}

    def unmerge_dry_run(self, key: str, merge_operation_id: str, *, actor: str,
                        reason: str) -> MutationPreview:
        self.authenticate(key)
        events = [e for e in self.store.mutation_events()
                  if e["operation_id"] == merge_operation_id and e["event_type"] == "MERGE"]
        if not events:
            raise KeyError(merge_operation_id)
        event = events[-1]
        later = [e for e in self.store.mutation_events()
                 if e["store_revision"] > event["store_revision"]
                 and e["operation_id"] != merge_operation_id
                 and e["event_type"] not in {"SNAPSHOT_BUILD", "SNAPSHOT_PUBLISH",
                                              "MENTION_MATERIALIZE",
                                              "REMATERIALIZATION_COMPLETE"}]
        payload = json.loads(event["payload_json"])
        mention_rows = payload.get("pre_merge_mention_rows", [])
        pre_mentions = {row["mention_id"] for row in mention_rows}
        pre_destination_mentions = set(payload.get("pre_destination_mention_ids", []))
        conn = self.store._connect()
        try:
            current_destination_mentions = {row[0] for row in conn.execute(
                "SELECT mention_id FROM mentions WHERE entity_id=?",
                (payload["destination_id"],))}
        finally:
            conn.close()
        later_mention_ids = sorted(
            current_destination_mentions - pre_mentions - pre_destination_mentions)
        changing_mention_ids = sorted(pre_mentions | set(later_mention_ids))
        intended_assignments = {
            row["mention_id"]: row["entity_id"] for row in mention_rows}
        intended_assignments.update({mention_id: None
                                     for mention_id in later_mention_ids})
        relation_plan = rematerialization_plan(
            self.store, [], affected_mention_ids=changing_mention_ids)
        relation_plan.pop("plan_hash", None)
        relation_plan.update({
            "mutation": "UNMERGE",
            "merge_operation_id": merge_operation_id,
            "intended_entity_assignments": dict(sorted(intended_assignments.items())),
            "intended_endpoint_semantics": "DERIVE_FROM_RESTORED_CURRENT_SOURCE_MENTIONS",
        })
        relation_plan["plan_hash"] = stable_hash(relation_plan)
        compensated_relation_ids = set(relation_plan["relation_ids"])
        later = [e for e in later if not (
            e["event_type"] == "RELATION_MATERIALIZE"
            and json.loads(e["payload_json"]).get("relation_id")
                in compensated_relation_ids)]
        operation_id = __import__("entity_resolution_types").new_opaque_id("op")
        plan = {"operation": "UNMERGE", "operation_id": operation_id,
                "merge_operation_id": merge_operation_id,
                "store_revision_before": self.store.revision(),
                "actor": actor, "reason": sanitize_business_text(reason),
                "source_ids": payload["source_ids"],
                "destination_id": payload["destination_id"],
                "later_mention_ids": later_mention_ids,
                "later_mentions_re_resolve": True,
                "rematerialization": relation_plan,
                "dependent_event_ids": [e["event_id"] for e in later]}
        digest = stable_hash(plan)
        return MutationPreview(operation_id, "UNMERGE", plan, digest,
                               self._token(operation_id, digest))

    def confirm_unmerge(self, key: str, preview: MutationPreview,
                        confirmation_token: str, *, crash_hook=None) -> dict:
        self.authenticate(key)
        if stable_hash(preview.plan) != preview.dry_run_hash or not hmac.compare_digest(
                self._token(preview.operation_id, preview.dry_run_hash), confirmation_token):
            raise IdentityConflict("confirmation mismatch")
        if preview.plan.get("dependent_event_ids"):
            raise DependentMutationConflict("dependent mutations require compensating operation")
        existing = [e for e in self.store.mutation_events()
                    if e["operation_id"] == preview.operation_id
                    and e["event_type"] == "UNMERGE"]
        if not existing and self.store.revision() != preview.plan["store_revision_before"]:
            raise DependentMutationConflict("store changed after unmerge dry-run")
        merge_events = [e for e in self.store.mutation_events()
                        if e["operation_id"] == preview.plan["merge_operation_id"]
                        and e["event_type"] == "MERGE"]
        payload = json.loads(merge_events[-1]["payload_json"])
        source_ids = payload["source_ids"]
        mention_rows = payload.get("pre_merge_mention_rows", [])
        pre_mentions = {row["mention_id"] for row in mention_rows}
        pre_destination_mentions = set(
            payload.get("pre_destination_mention_ids", []))
        if not existing:
          with self.store.transaction() as conn:
            for eid, before in payload["before_entities"].items():
                conn.execute("UPDATE entities SET lifecycle=?, redirect_entity_id=?, updated_at=?, version=version+1 WHERE entity_id=?",
                             (before["lifecycle"], before.get("redirect_entity_id"), utc_now(), eid))
            for alias in payload.get("pre_merge_alias_rows", []):
                exists = conn.execute("SELECT 1 FROM aliases WHERE alias_id=?",
                                      (alias["alias_id"],)).fetchone()
                if exists:
                    conn.execute("UPDATE aliases SET entity_id=? WHERE alias_id=?",
                                 (alias["entity_id"], alias["alias_id"]))
                else:
                    conn.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(alias[key] for key in ("alias_id", "entity_id", "surface",
                            "normalized_surface", "alias_type", "language", "script",
                            "provenance", "evidence_ref_json", "valid_from", "valid_to",
                            "status", "created_at", "created_by", "reason")))
            for strong in payload.get("pre_merge_strong_id_rows", []):
                conn.execute("UPDATE strong_ids SET entity_id=? WHERE strong_id_id=?",
                             (strong["entity_id"], strong["strong_id_id"]))
            for rule in payload.get("pre_merge_rule_rows", []):
                conn.execute("UPDATE rules SET target_entity_id=? WHERE rule_id=?",
                             (rule["target_entity_id"], rule["rule_id"]))
            # Only pre-merge mentions are restored. Later mentions become
            # unresolved and must pass the current resolver again.
            for mention in mention_rows:
                conn.execute("UPDATE mentions SET entity_id=?, decision=? WHERE mention_id=?",
                    (mention["entity_id"], mention["decision"], mention["mention_id"]))
            preserved_mentions = sorted(pre_mentions | pre_destination_mentions)
            conn.execute("UPDATE mentions SET entity_id=NULL, decision='AMBIGUOUS' WHERE entity_id=? AND mention_id NOT IN ({})".format(
                ",".join("?" for _ in preserved_mentions) or "''"),
                [payload["destination_id"]] + preserved_mentions)
            self.store._event(conn, "UNMERGE", {**preview.plan,
                "checkpoint": "DB_MUTATION_COMPLETE"}, preview.plan["actor"],
                preview.plan["reason"], operation_id=preview.operation_id)
            if crash_hook:
                crash_hook("before_commit")
        rematerialized = execute_relation_rematerialization(
            self.store, preview.plan["rematerialization"],
            operation_id=preview.operation_id, actor=preview.plan["actor"],
            reason=preview.plan["reason"], crash_hook=crash_hook)
        snapshot = build_identity_snapshot(self.store)
        self._checkpoint(preview.operation_id, "SNAPSHOT_BUILD", {
            "identity_snapshot_id": snapshot["identity_snapshot_id"],
            "checkpoint": "SNAPSHOT_BUILD_COMPLETE"},
            preview.plan["actor"], preview.plan["reason"])
        if crash_hook:
            crash_hook("after_build_before_switch")
        manifest_id = self.publish_callback(snapshot) if self.publish_callback else None
        if manifest_id is not None:
            self._checkpoint(preview.operation_id, "SNAPSHOT_PUBLISH", {
                "identity_snapshot_id": snapshot["identity_snapshot_id"],
                "manifest_id": manifest_id, "checkpoint": "ATOMIC_SWITCH_COMPLETE"},
                preview.plan["actor"], preview.plan["reason"])
        return {"operation_id": preview.operation_id, "snapshot": snapshot,
                "published_manifest_id": manifest_id,
                "later_mentions_re_resolve": True,
                "rematerialization": rematerialized,
                "serving_changed": manifest_id is not None}

    def pending_operations(self, key: str) -> list[dict]:
        """Report committed mutations that have not completed atomic publish."""
        self.authenticate(key)
        grouped = {}
        for event in self.store.mutation_events():
            grouped.setdefault(event["operation_id"], []).append(event)
        pending = []
        for operation_id, events in grouped.items():
            kinds = {event["event_type"] for event in events}
            mutations = kinds & {"MERGE", "SPLIT", "UNMERGE"}
            if mutations and "SNAPSHOT_PUBLISH" not in kinds:
                pending.append({"operation_id": operation_id,
                    "mutation": sorted(mutations)[-1],
                    "rematerialization_complete": "REMATERIALIZATION_COMPLETE" in kinds,
                    "snapshot_built": "SNAPSHOT_BUILD" in kinds,
                    "publish_complete": False})
        return sorted(pending, key=lambda row: row["operation_id"])

    def resume_publish(self, key: str, operation_id: str, *, actor: str,
                       reason: str) -> dict:
        """Safely rebuild from committed truth and perform only atomic publish."""
        self.authenticate(key)
        if self.publish_callback is None:
            raise RuntimeError("publish callback required to resume")
        pending = {row["operation_id"] for row in self.pending_operations(key)}
        if operation_id not in pending:
            raise KeyError("operation is not pending publication")
        mutation = next(e for e in reversed(self.store.mutation_events())
                        if e["operation_id"] == operation_id
                        and e["event_type"] in {"MERGE", "SPLIT", "UNMERGE"})
        payload = json.loads(mutation["payload_json"])
        if payload.get("rematerialization"):
            execute_relation_rematerialization(
                self.store, payload["rematerialization"], operation_id=operation_id,
                actor=actor, reason=reason)
        snapshot = build_identity_snapshot(self.store)
        self._checkpoint(operation_id, "SNAPSHOT_BUILD", {
            "identity_snapshot_id": snapshot["identity_snapshot_id"],
            "checkpoint": "RESUME_BUILD_COMPLETE"}, actor, reason)
        manifest_id = self.publish_callback(snapshot)
        self._checkpoint(operation_id, "SNAPSHOT_PUBLISH", {
            "identity_snapshot_id": snapshot["identity_snapshot_id"],
            "manifest_id": manifest_id, "checkpoint": "ATOMIC_SWITCH_COMPLETE"},
            actor, reason)
        return {"operation_id": operation_id, "snapshot": snapshot,
                "published_manifest_id": manifest_id,
                "serving_changed": True, "resumed": True}
