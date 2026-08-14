"""
TK-01 — Registry IO unified reader/writer regression suite.

Guards the T011 crash class: any historical on-disk shape must load in both
V1 and V2 readers, and the single writer must emit canonical form.
Self-isolating (tmpdir) — safe to run in CI without touching runtime/.
"""
import json
import tempfile
import traceback
from pathlib import Path

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception:
        print(f"  ❌ {name}")
        traceback.print_exc()
        FAIL += 1


import registry_io
from entity_resolver import EntityRegistry
from entity_resolver_v2 import EntityRegistryV2


def tmp():
    return Path(tempfile.mkdtemp())


def test_v1_reader_accepts_v2_list():
    """Original crash: V1 reader + V2 list file → TypeError."""
    d = tmp()
    p = d / "reg.json"
    p.write_text(json.dumps({
        "schema_version": "2.0",
        "entities": [{"entity_id": "org:a", "canonical_name": "Alpha",
                      "entity_type": "ORG", "aliases": ["α"]}],
    }, ensure_ascii=False))
    r = EntityRegistry(registry_path=p)
    assert len(r.entities) == 1
    assert r.entities["org:a"]["canonical_name"] == "Alpha"
    assert r.all_entities()[0]["aliases"] == ["α"]  # Q29 regression


def test_v1_reader_accepts_bare_list():
    d = tmp()
    p = d / "reg.json"
    p.write_text(json.dumps({"entities": [
        {"id": "org:b", "canonical_name": "Beta", "type": "ORG", "aliases": []},
    ]}, ensure_ascii=False))
    r = EntityRegistry(registry_path=p)
    assert len(r.entities) == 1 and r.entities["org:b"]["canonical_name"] == "Beta"


def test_v2_reader_accepts_v1_dict():
    d = tmp()
    p = d / "reg.json"
    p.write_text(json.dumps({
        "version": "0.1.0",
        "entities": {"org:c": {"id": "org:c", "type": "ORG",
                               "canonical_name": "Gamma", "aliases": ["γ"],
                               "confidence": 0.9, "provenance": "seed",
                               "manual_override": False}},
    }, ensure_ascii=False))
    r = EntityRegistryV2(registry_path=p)
    assert len(r.entities) == 1
    assert list(r.entities.values())[0].canonical_name == "Gamma"


def test_v2_reader_skips_unknown_fields():
    d = tmp()
    p = d / "reg.json"
    p.write_text(json.dumps({"schema_version": "2.0", "entities": [
        {"entity_id": "org:d", "canonical_name": "Delta", "entity_type": "ORG",
         "aliases": [], "future_field": 123},
    ]}, ensure_ascii=False))
    r = EntityRegistryV2(registry_path=p)
    assert len(r.entities) == 1  # future_field stripped, no TypeError


def test_roundtrip_v1_write_v2_read_v1_read():
    d = tmp()
    p = d / "reg.json"
    r1 = EntityRegistry(registry_path=p)
    r1.add_entity("org:x", "organization", "XCorp", ["XC", "xcorp"])
    r1.save()
    # canonical form on disk
    raw = json.loads(p.read_text("utf-8"))
    assert raw["schema_version"] == "2.0" and isinstance(raw["entities"], list)
    assert isinstance(raw.get("alias_index"), dict)  # V1 extras present
    r2 = EntityRegistryV2(registry_path=p)
    assert len(r2.entities) == 1
    r1b = EntityRegistry(registry_path=p)
    assert len(r1b.entities) == 1
    assert {e["canonical_name"].lower() for e in r1b.all_entities()} == \
           {e.canonical_name.lower() for e in r2.entities.values()}
    # resolve still works after roundtrip
    assert r1b.resolve("XC") is not None


def test_roundtrip_v2_write_v1_read():
    d = tmp()
    p = d / "reg.json"
    r2 = EntityRegistryV2(registry_path=p)
    r2.add_entity("Epsilon", "ORG", aliases=["EPS"])
    r2.save()
    r1 = EntityRegistry(registry_path=p)
    assert len(r1.entities) == 1
    assert r1.resolve("EPS") is not None or r1.resolve("Epsilon") is not None


def test_corrupt_file_tolerated():
    d = tmp()
    p = d / "reg.json"
    p.write_text("{not json")
    assert EntityRegistry(registry_path=p).entities == {}
    assert len(EntityRegistryV2(registry_path=p).entities) == 0


def test_single_path():
    """Q7: both classes default to the same canonical runtime path."""
    import entity_resolver, entity_resolver_v2
    assert entity_resolver.REGISTRY_FILE == entity_resolver_v2._rio.registry_path()


if __name__ == "__main__":
    print("Registry IO — TK-01 regression suite")
    check("V1 reader accepts V2 list (original T011 crash)", test_v1_reader_accepts_v2_list)
    check("V1 reader accepts bare list", test_v1_reader_accepts_bare_list)
    check("V2 reader accepts V1 dict", test_v2_reader_accepts_v1_dict)
    check("V2 reader strips unknown fields", test_v2_reader_skips_unknown_fields)
    check("roundtrip V1write→V2read→V1read", test_roundtrip_v1_write_v2_read_v1_read)
    check("roundtrip V2write→V1read", test_roundtrip_v2_write_v1_read)
    check("corrupt file tolerated", test_corrupt_file_tolerated)
    check("single canonical path (Q7)", test_single_path)
    print("=" * 60)
    print(f"  Registry IO Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)
