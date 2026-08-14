"""
Tests for Entity Resolution V2 (ER-001..ER-124).
Tests cover: registry, mention extraction, linking, disambiguation, pipeline.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ── TK-03 test isolation (Q22): redirect registry/index dirs to temp dirs so
# suites never pollute production runtime/indexes. setdefault: an explicit
# env (e.g. parity baseline runs) still wins.
import os as _os_t3, tempfile as _tf_t3
_os_t3.environ.setdefault("TECH_DB_INDEX_DIR", _tf_t3.mkdtemp(prefix="techdb-test-idx-"))
_os_t3.environ.setdefault("TECH_DB_RUNTIME_DIR", _tf_t3.mkdtemp(prefix="techdb-test-rt-"))

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


# ── Registry Tests ──
print("\n=== ER Registry Tests ===")
from entity_resolver_v2 import (
    EntityRegistryV2, CanonicalEntity, EntityMention, EntityLinkResult,
    EntityLinker, Disambiguator, MentionExtractor,
    EntityResolutionPipeline, LinkStatus,
    build_seed_registry_v2,
)

# Use temp file to avoid conflicts
tmp_path = Path(tempfile.mktemp(suffix=".json"))
registry = build_seed_registry_v2(tmp_path)
registry.save()  # Ensure seeds are persisted for pipeline reload

test("registry has entities", registry.stats()["total_entities"] > 0)
test("registry has aliases", registry.stats()["total_aliases"] > 0)

# Test ID generation (opaque, deterministic)
id1 = registry._generate_id("ORG", "NVIDIA")
id2 = registry._generate_id("ORG", "NVIDIA")
test("ID is deterministic", id1 == id2)
test("ID is opaque", "nvidia" not in id1.lower() and len(id1.split(":")[1]) == 8)

# Test add entity
new_id = registry.add_entity("Test Entity Corp", "ORG", aliases=["TEC"])
test("add entity returns ID", new_id is not None)
entity = registry.get_entity(new_id)
test("get entity by ID", entity is not None and entity.canonical_name == "Test Entity Corp")

# Test alias lookup
result_id = registry.alias_index.get("tec")
test("alias indexed", result_id == new_id)


# ── Mention Extraction Tests ──
print("\n=== ER Mention Extraction ===")
extractor = MentionExtractor(registry)

# Test Chinese entity extraction
mentions = extractor.extract("宁德时代发布了新电池技术", source="title")
test("extract Chinese entity", len(mentions) > 0)
test("mention has offsets", mentions[0].start_offset >= 0)

# Test English entity extraction
mentions = extractor.extract("NVIDIA released new Blackwell GPU", source="body")
found_texts = [m.text for m in mentions]
test("extract English entity", any("NVIDIA" in t or "Blackwell" in t for t in found_texts))

# Test no false positives on common words
mentions = extractor.extract("今天天气很好", source="body")
test("no false positive on common text", len(mentions) == 0)


# ── Entity Linking Tests ──
print("\n=== ER Entity Linking ===")
linker = EntityLinker(registry, use_llm=False)

# Test exact match
result = linker.link(EntityMention("英伟达", 0, 3))
test("exact match: 英伟达 → NVIDIA", result.status == LinkStatus.LINKED and result.canonical_name == "NVIDIA")
test("exact match confidence", result.confidence >= 0.95)

# Test case-insensitive match
result = linker.link(EntityMention("nvidia", 0, 6))
test("case-insensitive match", result.status == LinkStatus.LINKED)

# Test abbreviation match
result = linker.link(EntityMention("NVDA", 0, 4))
test("abbreviation match", result.status == LinkStatus.LINKED)

# Test alias match (Chinese → English)
result = linker.link(EntityMention("台积电", 0, 3))
test("alias match: 台积电 → TSMC", result.status == LinkStatus.LINKED and result.canonical_name == "TSMC")

# Test unknown entity
result = linker.link(EntityMention("完全不存在的公司XYZ", 0, 9))
test("unknown entity → NEW", result.status == LinkStatus.NEW)


# ── Disambiguation Tests ──
print("\n=== ER Disambiguation ===")
disambiguator = Disambiguator(registry)

# Single candidate → auto-resolve
single_result = disambiguator.disambiguate(
    "test",
    [("org:abc", 0.9)],
    context="battery technology",
)
test("single candidate resolves", single_result is not None and single_result.status == LinkStatus.LINKED)

# Multiple candidates with context
# Create ambiguous entity pair
ambig_id1 = registry.add_entity("Apple Inc", "ORG", aliases=["Apple"])
ambig_id2 = registry.add_entity("Apple (fruit)", "CONCEPT", aliases=["Apple"])

result = disambiguator.disambiguate(
    "Apple",
    [(ambig_id1, 0.9), (ambig_id2, 0.9)],
    context="Apple released new iPhone and Mac computers",
)
test("context disambiguation", result is not None)


# ── Pipeline Tests ──
print("\n=== ER Pipeline ===")
pipeline = EntityResolutionPipeline(registry_path=tmp_path, use_llm=False)

# Process single record
record = {
    "t": "NVIDIA发布Blackwell架构GPU",
    "b": "英伟达推出新一代Blackwell架构GPU，台积电负责代工。该产品使用先进制程。",
}
result = pipeline.process_record(record, record_id=0)
test("pipeline processes record", "linked_entities" in result)
test("pipeline finds entities", len(result["linked_entities"]) > 0)

# Check linked entities have correct fields
for le in result["linked_entities"]:
    test(f"linked entity has ID: {le['canonical_name']}",
         le.get("entity_id") is not None)
    test(f"linked entity has confidence: {le['canonical_name']}",
         "confidence" in le and 0 <= le["confidence"] <= 1)

# Test batch processing
records = [
    {"t": "宁德时代发布新电池", "b": "CATL发布磷酸铁锂电池"},
    {"t": "钙钛矿太阳能电池突破", "b": "效率达到26%"},
]
batch_result = pipeline.process_records(records)
test("batch processes records", batch_result["total_records"] == 2)
test("batch has stats", "registry_stats" in batch_result)


# ── Schema Versioning Tests ──
print("\n=== ER Schema Versioning ===")
test("registry has schema version", registry.SCHEMA_VERSION == "2.0")

# Save and reload
registry.save()
loaded = EntityRegistryV2(tmp_path)
test("reload preserves entities", len(loaded.entities) > 0)
test("reload preserves version", loaded.SCHEMA_VERSION == "2.0")


# ── Summary ──
print(f"\n{'='*70}")
print(f"  Entity Resolution V2 Results: {passed} passed, {failed} failed")
print(f"{'='*70}")

# Cleanup
tmp_path.unlink(missing_ok=True)

sys.exit(1 if failed > 0 else 0)
