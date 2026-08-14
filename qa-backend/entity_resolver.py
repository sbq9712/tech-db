"""
T011 — Entity Canonicalization / Alias / Ontology
===================================================
Canonical entity registry for resolving entity mentions to stable IDs.

Key concepts:
  - mention ≠ entity (different surface forms can refer to same entity)
  - stable opaque entity_id (rename doesn't change ID)
  - alias one-to-many (ambiguous aliases not force-merged)
  - manual override mechanism for high-value entities
  - versioned registry with incremental updates

Entity Types:
  organization, product, technology, material, standard, metric,
  project, institution, person, location

Note: This is the initial implementation (Phase B foundation).
Full entity resolution V2 (ER-001 through ER-124) is a separate epic.
"""
import json
import os
import re
import hashlib
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
INDEX_DIR = Path(os.environ.get("TECH_DB_INDEX_DIR", RUNTIME_DIR / "indexes")).resolve()
REGISTRY_FILE = INDEX_DIR / "entity_registry.json"

ENTITY_TYPES = {
    "organization", "product", "technology", "material", "standard",
    "metric", "project", "institution", "person", "location",
}


class EntityRegistry:
    """Canonical entity registry with alias resolution.

    Registry Schema:
    {
        "version": "0.1.0",
        "entities": {
            "org:nvidia": {
                "id": "org:nvidia",
                "type": "organization",
                "canonical_name": "NVIDIA",
                "aliases": ["Nvidia", "英伟达", "NVIDIA Corporation"],
                "confidence": 1.0,
                "provenance": "seed|migration|llm",
                "manual_override": false
            }
        },
        "alias_index": {
            "nvidia": "org:nvidia",
            "英伟达": "org:nvidia",
            ...
        },
        "ambiguous_aliases": ["AI", "BT"]  # known ambiguous, not force-merged
    }
    """

    def __init__(self, registry_path: Path = None):
        self.path = registry_path or REGISTRY_FILE
        self.version = "0.1.0"
        self.entities: dict = {}
        self.alias_index: dict = {}
        self.ambiguous_aliases: set = set()
        self._load()

    def _load(self):
        """Load registry from file.

        Persistence is delegated to registry_io (single writer, Q6/R12):
        accepts both V1 (dict) and V2 (list) on-disk shapes; V2 files are
        converted to the internal V1 representation.
        """
        import registry_io
        data = registry_io.read_registry(self.path)
        if data["source_version"] in ("empty", "corrupt") and not data["entities"]:
            # keep an alias_index we may have salvaged from a corrupt file
            self.alias_index = data.get("alias_index") or {}
            self.ambiguous_aliases = set(data.get("ambiguous_aliases") or [])
            return
        self.entities = {
            e["entity_id"]: registry_io.v2_to_v1_entity(e)
            for e in data["entities"]
        }
        if data.get("alias_index") is not None:
            self.alias_index = data["alias_index"]
        else:
            # V2 files don't persist the alias index — rebuild from entities
            self._rebuild_alias_index()
        self.ambiguous_aliases = set(data.get("ambiguous_aliases") or [])
        self.version = "2.0-compatible"

    def _rebuild_alias_index(self):
        """Rebuild alias_index / ambiguous_aliases from loaded entities."""
        self.alias_index = {}
        self.ambiguous_aliases = set()
        for ent in self.entities.values():
            eid = ent["id"]
            for surface in [ent.get("canonical_name", "")] + list(ent.get("aliases", []) or []):
                norm = self._normalize(surface)
                if not norm:
                    continue
                if norm in self.alias_index and self.alias_index[norm] != eid:
                    self.ambiguous_aliases.add(norm)
                else:
                    self.alias_index[norm] = eid

    def save(self):
        """Save registry to file (canonical V2 shape via registry_io)."""
        import registry_io
        registry_io.write_registry(
            self.path,
            [registry_io.v1_to_v2_entity(e) for e in self.entities.values()],
            self.alias_index,
            self.ambiguous_aliases,
        )

    def resolve(self, mention: str) -> dict:
        """Resolve an entity mention to canonical entity.

        Returns:
            {
                "entity_id": str or None,
                "canonical_name": str,
                "confidence": float,
                "status": "LINKED" | "NEW" | "AMBIGUOUS",
                "candidates": list of (entity_id, confidence),
            }
        """
        if not mention or not mention.strip():
            return {"entity_id": None, "canonical_name": mention,
                    "confidence": 0.0, "status": "NEW", "candidates": []}

        normalized = self._normalize(mention)

        # Check if it's a known ambiguous alias
        if normalized in self.ambiguous_aliases:
            return {
                "entity_id": None,
                "canonical_name": mention,
                "confidence": 0.0,
                "status": "AMBIGUOUS",
                "candidates": [],
            }

        # Exact alias match
        entity_id = self.alias_index.get(normalized)
        if entity_id:
            entity = self.entities.get(entity_id)
            if entity:
                return {
                    "entity_id": entity_id,
                    "canonical_name": entity["canonical_name"],
                    "confidence": entity.get("confidence", 1.0),
                    "status": "LINKED",
                    "candidates": [(entity_id, 1.0)],
                }

        # Fuzzy match (substring matching for common cases)
        candidates = self._fuzzy_match(normalized)
        if len(candidates) == 1:
            eid, score = candidates[0]
            entity = self.entities[eid]
            return {
                "entity_id": eid,
                "canonical_name": entity["canonical_name"],
                "confidence": score,
                "status": "LINKED",
                "candidates": candidates,
            }
        elif len(candidates) > 1:
            return {
                "entity_id": None,
                "canonical_name": mention,
                "confidence": 0.0,
                "status": "AMBIGUOUS",
                "candidates": candidates,
            }

        return {
            "entity_id": None,
            "canonical_name": mention,
            "confidence": 0.0,
            "status": "NEW",
            "candidates": [],
        }

    def add_entity(self, entity_id: str, entity_type: str,
                   canonical_name: str, aliases: list = None,
                   confidence: float = 1.0, provenance: str = "manual"):
        """Add or update a canonical entity."""
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}")

        self.entities[entity_id] = {
            "id": entity_id,
            "type": entity_type,
            "canonical_name": canonical_name,
            "aliases": aliases or [],
            "confidence": confidence,
            "provenance": provenance,
            "manual_override": provenance == "manual",
        }

        # Update alias index
        self.alias_index[self._normalize(canonical_name)] = entity_id
        for alias in (aliases or []):
            norm = self._normalize(alias)
            if norm in self.alias_index and self.alias_index[norm] != entity_id:
                # Ambiguous alias
                self.ambiguous_aliases.add(norm)
            else:
                self.alias_index[norm] = entity_id

    def mark_ambiguous(self, alias: str):
        """Mark an alias as ambiguous (don't force-merge)."""
        self.ambiguous_aliases.add(self._normalize(alias))

    def _normalize(self, text: str) -> str:
        """Normalize text for alias matching."""
        return re.sub(r"\s+", "", text.lower().strip())

    def _fuzzy_match(self, normalized: str) -> list:
        """Find fuzzy matches for a normalized mention.

        Returns list of (entity_id, confidence).
        """
        candidates = []
        for alias, eid in self.alias_index.items():
            # Substring match
            if len(normalized) >= 3:
                if normalized in alias or alias in normalized:
                    # Calculate overlap ratio
                    shorter = min(len(normalized), len(alias))
                    longer = max(len(normalized), len(alias))
                    score = shorter / longer
                    if score >= 0.6:
                        candidates.append((eid, round(score, 2)))

        # Deduplicate by entity_id (keep highest score)
        best = {}
        for eid, score in candidates:
            if eid not in best or score > best[eid]:
                best[eid] = score

        return sorted(best.items(), key=lambda x: -x[1])[:5]

    def all_entities(self) -> list:
        """Return all entities in the registry.

        Fixed (Q29/R12): previously referenced a non-existent `self._registry`,
        raising AttributeError when SEMANTIC_GRAPH calls this.
        """
        return list(self.entities.values())

    def stats(self) -> dict:
        """Return registry statistics."""
        return {
            "version": self.version,
            "total_entities": len(self.entities),
            "total_aliases": len(self.alias_index),
            "ambiguous_aliases": len(self.ambiguous_aliases),
            "by_type": {
                t: sum(1 for e in self.entities.values() if e.get("type") == t)
                for t in ENTITY_TYPES
            }
        }


# ── Seed registry with common entities ──

SEED_ENTITIES = [
    # Organizations
    ("org:nvidia", "organization", "NVIDIA", ["Nvidia", "英伟达", "NVIDIA Corporation", "NVDA"]),
    ("org:tsmc", "organization", "TSMC", ["台积电", "Taiwan Semiconductor", "台湾积体电路"]),
    ("org:amd", "organization", "AMD", ["Advanced Micro Devices", "超威半导体"]),
    ("org:intel", "organization", "Intel", ["英特尔"]),
    ("org:catl", "organization", "CATL", ["宁德时代", "Contemporary Amperex"]),
    ("org:byd", "organization", "BYD", ["比亚迪"]),
    ("org:google", "organization", "Google", ["谷歌", "Alphabet"]),
    ("org:microsoft", "organization", "Microsoft", ["微软"]),
    ("org:openai", "organization", "OpenAI", []),
    ("org:meta", "organization", "Meta", ["Facebook", "脸书"]),
    ("org:apple", "organization", "Apple", ["苹果", "苹果公司"]),
    ("org:samsung", "organization", "Samsung", ["三星", "三星电子"]),
    ("org:toyota", "organization", "Toyota", ["丰田", "丰田汽车"]),
    ("org:huawei", "organization", "Huawei", ["华为"]),
    ("org:claude", "organization", "Anthropic", ["Anthropic", "Claude"]),

    # Technologies
    ("tech:solid_state_battery", "technology", "固态电池", ["solid-state battery", "全固态电池"]),
    ("tech:perovskite_solar", "technology", "钙钛矿太阳能电池", ["perovskite solar cell", "PSC"]),
    ("tech:lidar", "technology", "LiDAR", ["激光雷达", "lidar"]),
    ("tech:quantum_computing", "technology", "量子计算", ["quantum computing", "量子计算机"]),
    ("tech:brain_computer_interface", "technology", "脑机接口", ["BCI", "brain-computer interface"]),

    # Materials
    ("mat:sulfide_electrolyte", "material", "硫化物电解质", ["sulfide electrolyte"]),
    ("mat:perovskite", "material", "钙钛矿", ["perovskite"]),
    ("mat:lithium_metal", "material", "锂金属", ["lithium metal anode", "锂负极"]),
]


def build_seed_registry():
    """Build and save the initial seed registry."""
    registry = EntityRegistry()
    for eid, etype, name, aliases in SEED_ENTITIES:
        registry.add_entity(eid, etype, name, aliases, confidence=1.0, provenance="seed")
    registry.save()
    return registry


if __name__ == "__main__":
    reg = build_seed_registry()
    stats = reg.stats()
    print(f"Entity Registry: {stats['total_entities']} entities, {stats['total_aliases']} aliases")
    # Test resolution
    for q in ["英伟达", "Nvidia", "宁德时代", "固态电池", "OpenAI"]:
        result = reg.resolve(q)
        print(f"  '{q}' → {result['status']}: {result.get('entity_id', 'N/A')} ({result.get('canonical_name', '')})")
