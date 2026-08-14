#!/usr/bin/env python3
"""One-time registry migration (TK-01/Q7).

Merges the legacy data/lightrag/entity_registry.json (V2 list, written by a
manual EntityRegistryV2 run in commit fa02a5b) into the canonical
runtime/indexes/entity_registry.json, then the legacy file is removed from
git tracking. Merge rule: union by canonical_name (case/space normalized);
on conflict keep the entry with higher mention_count, else the legacy one.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "qa-backend"))
import registry_io

LEGACY = REPO / "data" / "lightrag" / "entity_registry.json"
CANON = registry_io.registry_path()


def key(e):
    return (e.get("canonical_name") or "").strip().lower()


def main():
    if not LEGACY.exists():
        print(f"legacy file not found: {LEGACY} — nothing to migrate")
        return 0
    legacy = registry_io.read_registry(LEGACY)
    canon = registry_io.read_registry(CANON)

    merged = {}
    for e in canon["entities"] + legacy["entities"]:
        k = key(e)
        if not k:
            continue
        prev = merged.get(k)
        if prev is None or (e.get("mention_count", 0), e.get("provenance") != "seed") > \
                (prev.get("mention_count", 0), prev.get("provenance") != "seed"):
            merged[k] = e

    # Union aliases of duplicates
    for k, e in list(merged.items()):
        aliases = set(e.get("aliases") or [])
        for other in canon["entities"] + legacy["entities"]:
            if key(other) == k:
                aliases.update(other.get("aliases") or [])
                if other.get("description") and not e.get("description"):
                    e["description"] = other["description"]
        e["aliases"] = sorted(a for a in aliases if a)

    entities = sorted(merged.values(), key=lambda e: e.get("entity_id", ""))
    registry_io.write_registry(CANON, entities)
    print(f"merged: canon={len(canon['entities'])} + legacy={len(legacy['entities'])} "
          f"→ {len(entities)} entities → {CANON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
