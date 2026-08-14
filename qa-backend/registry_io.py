"""
Registry IO — the single canonical reader/writer for the entity registry.

Why this exists (R10/Q6): the registry historically had two writers with two
on-disk shapes:
  V1 (entity_resolver.EntityRegistry):
      {"version": "0.1.0", "entities": {id: {...}}, "alias_index": {...},
       "ambiguous_aliases": [...]}
  V2 (entity_resolver_v2.EntityRegistryV2):
      {"schema_version": "2.0", "saved_at": ..., "entity_count": N,
       "entities": [CanonicalEntity asdict ...]}

A V1 reader handed a V2 file got `entities` as a list and crashed
(`TypeError: list indices must be integers, not str`) — the T011 crash.

Rules established here:
  - Reading accepts any historical shape (V1 dict / V2 list / list without
    schema_version) and returns a normalized in-memory form.
  - Writing always emits the canonical form: V2 list shape with
    `schema_version: "2.0"`, plus `alias_index` / `ambiguous_aliases` extras
    (V1 readers consume them; V2 readers ignore unknown top-level keys).
  - Both registry classes delegate persistence to this module — it is the
    single writer (Q6/R12).
"""
import json
import os
from datetime import datetime
from pathlib import Path

CANONICAL_SCHEMA_VERSION = "2.0"

# Fields of the canonical (V2) entity shape. Unknown fields are dropped on
# load so downstream `CanonicalEntity(**fields)` construction never crashes.
V2_FIELDS = (
    "entity_id", "canonical_name", "entity_type", "aliases", "abbreviations",
    "description", "wikipedia_url", "confidence", "provenance",
    "mention_count", "document_count", "first_seen", "last_seen",
)


def v1_to_v2_entity(d: dict) -> dict:
    """Convert a V1-shaped entity dict to the canonical V2 shape."""
    return {
        "entity_id": d.get("id", d.get("entity_id", "")),
        "canonical_name": d.get("canonical_name", ""),
        "entity_type": d.get("type", d.get("entity_type", "")),
        "aliases": list(d.get("aliases", []) or []),
        "abbreviations": list(d.get("abbreviations", []) or []),
        "description": d.get("description", ""),
        "wikipedia_url": d.get("wikipedia_url", ""),
        "confidence": d.get("confidence", 1.0),
        "provenance": d.get("provenance", "manual"),
        "mention_count": d.get("mention_count", 0),
        "document_count": d.get("document_count", 0),
        "first_seen": d.get("first_seen", ""),
        "last_seen": d.get("last_seen", ""),
    }


def v2_to_v1_entity(d: dict) -> dict:
    """Convert a canonical V2 entity dict to the V1 internal shape."""
    return {
        "id": d.get("entity_id", ""),
        "type": d.get("entity_type", ""),
        "canonical_name": d.get("canonical_name", ""),
        "aliases": list(d.get("aliases", []) or []),
        "confidence": d.get("confidence", 1.0),
        "provenance": d.get("provenance", "manual"),
        "manual_override": d.get("provenance", "manual") == "manual",
    }


def _default_for(field: str):
    """Default value for a missing canonical field."""
    if field in ("aliases", "abbreviations"):
        return []
    if field in ("mention_count", "document_count"):
        return 0
    if field == "confidence":
        return 1.0
    if field == "provenance":
        return "manual"
    return ""


def _normalize_entry(raw: dict) -> dict:
    """Normalize one raw entity entry (either shape) to canonical V2 shape."""
    if "id" in raw and "entity_id" not in raw:
        return v1_to_v2_entity(raw)
    return {k: raw.get(k, _default_for(k)) for k in V2_FIELDS}


def read_registry(path) -> dict:
    """Read a registry file of any historical shape.

    Returns a normalized dict:
      {
        "schema_version": "2.0",           # canonical version after read
        "source_version": "1.x"|"2.x"|"empty"|"corrupt",
        "entities": [ canonical V2 entity dicts ],
        "alias_index": dict | None,        # stored only by V1 files
        "ambiguous_aliases": list | None,
      }
    """
    path = Path(path)
    empty = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "source_version": "empty",
        "entities": [],
        "alias_index": None,
        "ambiguous_aliases": None,
    }
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {**empty, "source_version": "corrupt"}

    if not isinstance(data, dict):
        return {**empty, "source_version": "corrupt"}

    entities_raw = data.get("entities", None)
    version = str(data.get("schema_version", data.get("version", "1.0")))

    if isinstance(entities_raw, dict):
        # V1 shape: {id: {...}}
        source = "1.x" if not version.startswith("2.") else "2.x-as-dict"
        entities = [_normalize_entry({**v, "id": eid}) for eid, v in entities_raw.items()
                    if isinstance(v, dict)]
    elif isinstance(entities_raw, list):
        source = "2.x"
        entities = [_normalize_entry(v) for v in entities_raw if isinstance(v, dict)]
    else:
        # No/invalid entities key → empty registry
        return {**empty, "alias_index": data.get("alias_index"),
                "ambiguous_aliases": data.get("ambiguous_aliases")}

    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "source_version": source,
        "entities": entities,
        "alias_index": data.get("alias_index") if isinstance(data.get("alias_index"), dict) else None,
        "ambiguous_aliases": data.get("ambiguous_aliases") if isinstance(data.get("ambiguous_aliases"), list) else None,
    }


def write_registry(path, entities: list, alias_index: dict = None,
                   ambiguous_aliases=None) -> None:
    """Write the registry in canonical form (single writer, Q6/R12).

    `entities` must be a list of canonical (V2-shaped) dicts — use
    v1_to_v2_entity() first if you hold V1-shaped entities.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    canonical = [{k: e.get(k, _default_for(k)) for k in V2_FIELDS} for e in entities]

    data = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "saved_at": datetime.now().isoformat(),
        "entity_count": len(canonical),
        "entities": canonical,
    }
    # Extras for V1 readers; V2 readers ignore unknown top-level keys.
    if alias_index is not None:
        data["alias_index"] = alias_index
    if ambiguous_aliases is not None:
        data["ambiguous_aliases"] = sorted(ambiguous_aliases)

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def registry_path() -> Path:
    """The single authoritative registry location (Q7).

    Honors the standard TECH_DB_INDEX_DIR / TECH_DB_RUNTIME_DIR overrides so
    tests can redirect it to a temp dir.
    """
    repo = Path(__file__).resolve().parent.parent
    runtime_dir = Path(os.environ.get("TECH_DB_RUNTIME_DIR", repo / "runtime")).resolve()
    index_dir = Path(os.environ.get("TECH_DB_INDEX_DIR", runtime_dir / "indexes")).resolve()
    return index_dir / "entity_registry.json"
