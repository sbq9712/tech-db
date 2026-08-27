"""Immutable IdentitySnapshot build/validation/read model (RT-070)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from entity_resolution_types import normalize_surface, stable_hash

SCHEMA_VERSION = "1.0.0"


def _unsigned(snapshot: dict) -> dict:
    value = dict(snapshot)
    value.pop("content_hash", None)
    value.pop("identity_snapshot_id", None)
    return value


def validate_identity_snapshot(snapshot: dict) -> list[str]:
    issues = []
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        return ["unsupported identity snapshot schema"]
    expected_hash = stable_hash(_unsigned(snapshot))
    if snapshot.get("content_hash") != expected_hash:
        issues.append("identity snapshot content hash mismatch")
    if snapshot.get("identity_snapshot_id") != f"ids_{expected_hash[:24]}":
        issues.append("identity snapshot ID mismatch")
    entities = snapshot.get("entities")
    aliases = snapshot.get("aliases")
    strong_ids = snapshot.get("strong_ids")
    rules = snapshot.get("rules")
    if not all(isinstance(v, list) for v in (entities, aliases, strong_ids, rules)):
        return issues + ["identity snapshot collections must be lists"]
    entity_ids = [e.get("entity_id") for e in entities if isinstance(e, dict)]
    if len(entity_ids) != len(set(entity_ids)) or None in entity_ids:
        issues.append("duplicate or missing entity IDs")
    known = set(entity_ids)
    entities_by_id = {e["entity_id"]: e for e in entities if e.get("entity_id")}
    for alias in aliases:
        if alias.get("entity_id") not in known:
            issues.append(f"alias targets unknown entity: {alias.get('alias_id')}")
    ownership = set()
    for strong in strong_ids:
        key = (strong.get("id_type"), strong.get("normalized_value"), strong.get("status"))
        if key in ownership:
            issues.append(f"duplicate strong identifier: {key[0]}:{key[1]}")
        ownership.add(key)
        if strong.get("entity_id") not in known:
            issues.append(f"strong ID targets unknown entity: {strong.get('strong_id_id')}")
    for entity in entities:
        if entity.get("lifecycle") == "TOMBSTONED":
            target = entity.get("redirect_entity_id")
            if target and (target not in known or entities_by_id[target].get("lifecycle") == "TOMBSTONED"):
                issues.append(f"invalid tombstone redirect: {entity.get('entity_id')}")
        elif entity.get("redirect_entity_id"):
            issues.append(f"active entity has redirect: {entity.get('entity_id')}")
    for rule in rules:
        target = rule.get("target_entity_id")
        if target and target not in known:
            issues.append(f"rule targets unknown entity: {rule.get('rule_id')}")
    return sorted(set(issues))


def build_identity_snapshot_payload(*, entities=(), aliases=(), strong_ids=(),
                                    rules=(), source_store_revision=0,
                                    resolver_version="er-v2.0",
                                    policy_version="resolution-policy-1.0",
                                    created_at="1970-01-01T00:00:00+00:00") -> dict:
    entities = list(entities); aliases = list(aliases)
    strong_ids = list(strong_ids); rules = list(rules)
    # Serving artifacts contain only active/review-required rule versions.
    rules = [r for r in rules if r.get("effective_status") in
             {"ACTIVE", "STALE_REVIEW_REQUIRED"}]
    body = {
        "schema_version": SCHEMA_VERSION,
        "source_store_revision": int(source_store_revision),
        "created_at": created_at,
        "resolver": {"version": resolver_version,
                     "policy_version": policy_version},
        "entities": entities,
        "aliases": aliases,
        "strong_ids": strong_ids,
        "tombstone_redirects": {
            e["entity_id"]: e["redirect_entity_id"] for e in entities
            if e.get("lifecycle") == "TOMBSTONED" and e.get("redirect_entity_id")
        },
        "rules": rules,
        "identity_is_evidence": False,
    }
    content_hash = stable_hash(body)
    body["content_hash"] = content_hash
    body["identity_snapshot_id"] = f"ids_{content_hash[:24]}"
    issues = validate_identity_snapshot(body)
    if issues:
        raise ValueError("invalid identity snapshot: " + "; ".join(issues))
    return body


def build_identity_snapshot(store, *, resolver_version="er-v2.0",
                            policy_version="resolution-policy-1.0",
                            created_at="1970-01-01T00:00:00+00:00") -> dict:
    rules = store.active_rules(at=created_at) if created_at != "1970-01-01T00:00:00+00:00" else store.active_rules()
    return build_identity_snapshot_payload(
        entities=store.list_entities(), aliases=store.aliases(),
        strong_ids=store.strong_ids(), rules=rules,
        source_store_revision=store.revision(), resolver_version=resolver_version,
        policy_version=policy_version, created_at=created_at)


def write_identity_snapshot(snapshot: dict, path: Path | str) -> Path:
    issues = validate_identity_snapshot(snapshot)
    if issues:
        raise ValueError("invalid identity snapshot: " + "; ".join(issues))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".incomplete",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


@dataclass(frozen=True)
class IdentitySnapshotView:
    payload: dict

    def __post_init__(self):
        issues = validate_identity_snapshot(self.payload)
        if issues:
            raise ValueError("invalid identity snapshot: " + "; ".join(issues))

    @property
    def snapshot_id(self) -> str:
        return self.payload["identity_snapshot_id"]

    @property
    def entities(self) -> dict[str, dict]:
        return {e["entity_id"]: e for e in self.payload["entities"]}

    def alias_candidates(self, surface: str) -> list[dict]:
        norm = normalize_surface(surface)
        return [a for a in self.payload["aliases"]
                if a.get("normalized_surface") == norm and a.get("status") == "ACTIVE"]

    def active_entities(self) -> list[dict]:
        return [e for e in self.payload["entities"]
                if e.get("lifecycle") in {"ACTIVE", "PROVISIONAL", "REVIEW_REQUIRED"}]
