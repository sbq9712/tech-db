"""Tests for Phase D modules: T028, T029, T044, T041, T056."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


# ── T028: Chunking ──
print("\n=== T028: Contextual Chunking ===")
from chunking import chunk_record, CHUNK_VERSION

# Test: short record → no chunks
short_rec = {"t": "Test", "b": "short", "fb": ""}
chunks = chunk_record(short_rec, 0)
test("short record → no chunks", len(chunks) == 0)

# Test: normal record → multiple chunks
long_text = "这是第一段内容。" * 20 + "\n\n" + "这是第二段内容。" * 20 + "\n\n" + "第三段。" * 20
long_rec = {"t": "Long Article", "b": long_text, "s": "source", "d": "2026-01-01", "c": "tech/sub"}
chunks = chunk_record(long_rec, 0)
test("long record produces chunks", len(chunks) >= 2)
test("chunks have offsets", all("start_offset" in c and "end_offset" in c for c in chunks))
test("chunks have context_prefix", all("context_prefix" in c for c in chunks))
test("chunks have version", all(c.get("chunk_version") == CHUNK_VERSION for c in chunks))
test("chunks have record_id", all(c.get("record_id") == 0 for c in chunks))

# Test: chunk text is within original text
if chunks:
    c = chunks[0]
    test("chunk text from original", c["text"] in long_text or long_text[c["start_offset"]:c["end_offset"]].strip())


# ── T029: Numeric Facts ──
print("\n=== T029: Numeric Facts ===")
from numeric_facts import extract_numeric_facts, compare_numeric_facts

rec = {"b": "该芯片带宽达到1.8TB/s，能效提升了30%。", "d": "2026-01-01", "_idx": 0}
facts = extract_numeric_facts(rec)
test("extracted numeric facts", len(facts) >= 1)
if facts:
    test("fact has metric", "metric" in facts[0])
    test("fact has value", "value" in facts[0])
    test("fact has unit", "unit" in facts[0])

# Compare facts
f1 = {"metric": "bandwidth", "value": 1.8, "unit": "TB/s", "scope": "per_device", "condition": "unknown"}
f2 = {"metric": "bandwidth", "value": 1.8, "unit": "TB/s", "scope": "per_device", "condition": "unknown"}
result = compare_numeric_facts(f1, f2)
test("same facts → AGREE", result == "AGREE")

f3 = {"metric": "bandwidth", "value": 3.2, "unit": "TB/s", "scope": "per_device", "condition": "unknown"}
result = compare_numeric_facts(f1, f3)
test("different facts → CONTRADICT", result == "CONTRADICT")

f4 = {"metric": "bandwidth", "value": 1.8, "unit": "TB/s", "scope": "system_total", "condition": "unknown"}
result = compare_numeric_facts(f1, f4)
test("different scope → DIFFERENT_SCOPE", result == "DIFFERENT_SCOPE")


# ── T044: Relation Ontology ──
print("\n=== T044: Relation Ontology ===")
from relation_ontology import (
    RELATIONS, GraphStatement, AssertionStatus, Polarity,
    validate_predicate, get_predicate_info, is_symmetric, get_inverse,
)

test("ontology has predicates", len(RELATIONS) >= 10)
test("USES predicate exists", validate_predicate("USES"))
test("invalid predicate rejected", not validate_predicate("FAKE_RELATION"))
test("COMPETES_WITH is symmetric", is_symmetric("COMPETES_WITH"))
test("USES is not symmetric", not is_symmetric("USES"))
test("USES inverse is USED_BY", get_inverse("USES") == "USED_BY")

# Test GraphStatement
stmt = GraphStatement(
    "org:nvidia", "RELEASED", "product:blackwell",
    assertion_status=AssertionStatus.ASSERTED,
    evidence_refs=[{"record_id": 123, "span": "NVIDIA released Blackwell"}],
)
test("statement created", stmt.predicate == "RELEASED")
test("statement valid for current", stmt.is_valid_for_query("current"))

# Test deprecated statement
stmt_dep = GraphStatement(
    "org:nvidia", "RELEASED", "product:old",
    assertion_status=AssertionStatus.DEPRECATED,
)
test("deprecated invalid for current", not stmt_dep.is_valid_for_query("current"))
test("deprecated valid for historical", stmt_dep.is_valid_for_query("historical"))

# Test planned statement
stmt_plan = GraphStatement(
    "org:nvidia", "RELEASED", "product:future",
    assertion_status=AssertionStatus.PLANNED,
)
test("planned invalid for current (default)", not stmt_plan.is_valid_for_query("current"))
test("planned valid with include_planned", stmt_plan.is_valid_for_query("current", include_planned=True))


# ── T041: Release Manifest ──
print("\n=== T041: Release Manifest ===")
from release_manifest import build_manifest, validate_manifest_compatibility

manifest = build_manifest()
test("manifest has id", bool(manifest.get("manifest_id")))
test("manifest has dataset", "dataset" in manifest)
test("manifest has indexes", "indexes" in manifest)
test("manifest has models", "models" in manifest)

compatible, issues = validate_manifest_compatibility(manifest)
test("manifest validation runs", isinstance(compatible, bool))


# ── T056: Trace Retention ──
print("\n=== T056: Trace Retention ===")
from trace_retention import redact_trace, verify_no_secrets, cleanup_expired_traces

# Test redaction
trace_record = {
    "query": "test query",
    "api_key": "secret123",
    "ZAI_API_KEY": "abc123def456",
    "data": {"nested_key": "value", "password": "hidden"},
}
redacted = redact_trace(trace_record)
test("api_key redacted", redacted["api_key"] == "***REDACTED***")
test("ZAI_API_KEY redacted", redacted["ZAI_API_KEY"] == "***REDACTED***")
test("nested password redacted", redacted["data"]["password"] == "***REDACTED***")
test("query preserved", redacted["query"] == "test query")

# Test no secrets
secret_check = verify_no_secrets()
test("trace secret scan runs", isinstance(secret_check["clean"], bool))

# Test cleanup
cleanup_result = cleanup_expired_traces(retention_days=365)  # Keep everything
test("cleanup runs", "deleted_files" in cleanup_result)


# ── Summary ──
print(f"\n{'='*70}")
print(f"  Phase D Results: {passed} passed, {failed} failed")
print(f"{'='*70}")

sys.exit(1 if failed > 0 else 0)
