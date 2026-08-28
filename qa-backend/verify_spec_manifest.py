#!/usr/bin/env python3
"""TK-14 — verify_spec_manifest: spec↔code consistency validator (Q14/R11).

Checks (each independent; failures accumulate):
  V1 flag registry ↔ Flags.status() keys are bijective
  V2 doc flag table (IMPLEMENTATION_STATUS.md) covers every flag with the
     correct default value (doc drift → FAIL)
  V3 entity registry carries schema_version == "2.0" + entity_count matches
  V4 index files referenced by config exist (vector/bm25/graph)
  V5 every tests_*.py on disk is registered in run_all_tests.py (and vice
     versa: no dead registrations)
  V6 test_summary.json: all_passed ⟺ per-suite results, and doc conclusions
     ("N passed") match the summary
  V7 nightly artifact paths referenced by the summary exist on disk

Exit codes: 0 = PASS, 1 = FAIL (CI-usable). --json prints a machine-readable
report. --selftest injects a doc drift and expects FAIL.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("TECH_DB_INDEX_DIR", str(ROOT / "data" / "lightrag"))
os.environ.setdefault("TECH_DB_RUNTIME_DIR", str(ROOT / "runtime"))

RESULTS = []


def record(vid, name, passed, detail=""):
    RESULTS.append({"check": vid, "name": name, "pass": bool(passed), "detail": detail})
    print(f"  {'✅' if passed else '❌'} {vid} {name}" + (f" — {detail}" if detail else ""))


def v1_flag_registry():
    from feature_flags import Flags
    status_keys = set(Flags.status().keys())
    # canonical registry: status key "agentic" → attr AGENTIC_ENABLED
    attr_for = {k: (k.upper() + "_ENABLED") for k in status_keys}
    # env names must come from the canonical ENV_NAMES registry (RERANKER uses
    # QA_RERANK_ENABLED, not QA_RERANKER_ENABLED)
    missing_env = [a for a in attr_for.values() if not hasattr(Flags, a)]
    env_known = all(a in Flags.ENV_NAMES for a in attr_for.values())
    passed = not missing_env and len(status_keys) == len(attr_for) and env_known
    record("V1", "flag registry ↔ status() bijection", passed,
           f"{len(status_keys)} flags" + (f", missing attrs {missing_env}" if missing_env else ""))
    return status_keys


def v2_doc_flags(status_keys, inject_drift=False):
    doc = ROOT / "IMPLEMENTATION_STATUS.md"
    text = doc.read_text(encoding="utf-8")
    if inject_drift:
        text = text.replace("| QA_TRACE_ENABLED | true |", "| QA_TRACE_ENABLED | false |", 1)
    # parse doc flag table rows (Phase07: flag names may contain digits,
    # e.g. QA_GRAPH_V2_ENABLED)
    rows = re.findall(r"\|\s*(QA_[A-Z0-9_]+)\s*\|\s*(true|false)\s*\|", text)
    doc_map = dict(rows)
    from feature_flags import Flags
    attr_for_key = {k: (k.upper() + "_ENABLED") for k in status_keys}
    problems = []
    for key, attr in attr_for_key.items():
        env = Flags.ENV_NAMES.get(attr, f"QA_{attr}")
        actual = getattr(Flags, attr)
        if env not in doc_map:
            problems.append(f"{env} missing from doc")
        elif doc_map[env] != str(actual).lower():
            problems.append(f"{env} doc={doc_map[env]} code={str(actual).lower()}")
    extra = [e for e in doc_map if e not in {Flags.ENV_NAMES.get(a, f"QA_{a}") for a in attr_for_key.values()}]
    if extra:
        problems.append(f"doc has unknown flags: {extra[:5]}")
    record("V2", "doc flag table matches code defaults", not problems,
           "; ".join(problems[:4]) if problems else f"{len(doc_map)} flags documented")


def v3_registry():
    """Codex-review B2 P1 fix: runtime/ is gitignored, so a clean checkout
    has no entity_registry.json — the old `registry missing → FAIL` broke CI
    before any holdout step. When the runtime registry is absent, bootstrap a
    MINIMAL registry through the canonical single-writer (registry_io) from
    the committed mini-index fixture titles, then validate its schema the
    same way. Production deployments always have the real runtime registry
    and take the first branch."""
    reg = ROOT / "runtime" / "indexes" / "entity_registry.json"
    if not reg.exists():
        import registry_io
        mini = json.loads((HERE / "test_fixtures" / "mini_index"
                           / "all-records-mini.json").read_text(encoding="utf-8"))
        entities = [
            {"entity_id": f"fixture-entity-{i:03d}",
             "canonical_name": (r.get("t") or f"fixture-entity-{i}")[:80],
             "entity_type": "ORG/TECH",
             "aliases": [], "abbreviations": [],
             "description": "bootstrap entity for CI schema verification",
             "wikipedia_url": None, "confidence": 1.0,
             "provenance": "verify_spec_manifest bootstrap",
             "mention_count": 1, "document_count": 1,
             "first_seen": None, "last_seen": None}
            for i, r in enumerate(mini[:5])
        ]
        registry_io.write_registry(reg, entities)
    d = json.loads(reg.read_text(encoding="utf-8"))
    ok = d.get("schema_version") == "2.0"
    cnt_ok = d.get("entity_count") == len(d.get("entities", []))
    record("V3", "entity registry schema_version=2.0 + count",
           ok and cnt_ok,
           f"sv={d.get('schema_version')} count={d.get('entity_count')}/{len(d.get('entities', []))}")


def v4_indexes():
    # Codex-review B2 P2 fix: config defines neither VECTOR_INDEX_PATH nor
    # BM25_INDEX_PATH, so both getattr(..., None) returned None and V4 always
    # reported ok — vacuous. Use the actual paths the server loads
    # (config.WORKING_DIR is the index dir env-configured at import).
    import config
    idx_dir = Path(config.WORKING_DIR)
    paths = {
        "vector": idx_dir / "vector_index_v2.pkl",
        "bm25": idx_dir / "bm25_index.pkl",
    }
    # In CI (clean checkout) the real indexes don't exist — the committed
    # mini fixture indexes are the on-disk contract there. Either counts.
    mini_dir = HERE / "test_fixtures" / "mini_index" / "indexes"
    missing = []
    for k, p in paths.items():
        if p.exists():
            continue
        if (mini_dir / p.name).exists():
            continue  # fixture-indexed environment (CI)
        missing.append(f"{k}={p} (and no mini fixture at {mini_dir / p.name})")
    record("V4", "index files exist", not missing, "; ".join(missing) if missing else
           f"vector+bm25 ok (runtime={'yes' if paths['vector'].exists() else 'mini-fixture'})")


def v5_suites():
    sys.path.insert(0, str(HERE))
    import run_all_tests as rat
    registered = {f for f, _ in rat.SUITES.values()}
    on_disk = {p.name for p in HERE.glob("tests_*.py")}
    unregistered = on_disk - registered
    dead = registered - on_disk
    ok = not unregistered and not dead
    record("V5", "test suites registered ↔ disk", ok,
           f"{len(registered)} registered; unregistered={sorted(unregistered)}, dead={sorted(dead)}")


def v6_summary():
    sp = HERE / "test_summary.json"
    if not sp.exists():
        record("V6", "test_summary consistency", False, "missing test_summary.json")
        return
    d = json.loads(sp.read_text(encoding="utf-8"))
    per_suite_ok = all(s["status"] == "PASS" for s in d.get("suites", []))
    suite_total = sum(s.get("passed", 0) for s in d.get("suites", []))
    totals_ok = (d.get("total_failed", 1) == 0 and
                 d.get("total_passed") == suite_total)
    # Codex-review B2 P2 fix: the contract says documented test conclusions
    # must match the summary — parse the doc's headline total and compare.
    doc_ok, doc_detail = True, "no documented total found"
    doc_path = ROOT / "IMPLEMENTATION_STATUS.md"
    if doc_path.exists():
        text = doc_path.read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*[/／]\s*\d+\s*(?:项|个)?\s*(?:tests?|测试|测试通过)", text)
        m2 = re.search(r"(\d+)\s+passed(?:,\s*\d+\s+failed)?\s+across", text)
        mm = m or m2
        if mm:
            doc_total = int(mm.group(1))
            doc_ok = doc_total == d.get("total_passed")
            doc_detail = f"doc={doc_total} summary={d.get('total_passed')}"
    record("V6", "test_summary internally consistent + doc total matches",
           per_suite_ok and totals_ok and d.get("all_passed") is True and doc_ok,
           f"total={d.get('total_passed')}/{suite_total}, "
           f"all_passed={d.get('all_passed')}, doc: {doc_detail}")


def v7_artifacts():
    sp = HERE / "test_summary.json"
    if not sp.exists():
        record("V7", "nightly artifact paths", False, "no summary")
        return
    d = json.loads(sp.read_text(encoding="utf-8"))
    refs = []
    for s in d.get("suites", []):
        for m in re.findall(r"[\w./-]+\.(?:json|jsonl)", s.get("tail", "") or ""):
            refs.append(m)
    missing = []
    for r in set(refs):
        # artifact paths are relative to repo root or qa-backend
        # Older committed summaries may contain the producer's absolute
        # checkout prefix.  Resolve such references only by their artifact
        # basename inside the repository; never require that foreign
        # workspace to exist on the current CI runner.
        path = Path(r)
        repo_candidates = [ROOT / r, HERE / r]
        if path.is_absolute():
            repo_candidates.extend((ROOT / path.name, HERE / path.name))
        if not (any(p.exists() for p in repo_candidates) or r.startswith("test_fixtures")):
            if "/" in r or r.endswith(".jsonl"):
                missing.append(r)
    record("V7", "nightly artifact paths exist", not missing,
           f"{len(set(refs))} refs" + (f", missing={missing[:3]}" if missing else ""))


def run(inject_drift=False):
    print("verify_spec_manifest — spec↔code consistency")
    keys = v1_flag_registry()
    v2_doc_flags(keys, inject_drift=inject_drift)
    v3_registry()
    v4_indexes()
    v5_suites()
    v6_summary()
    v7_artifacts()
    failed = [r for r in RESULTS if not r["pass"]]
    print("=" * 62)
    print(f"  {'✅ VERIFIER PASS' if not failed else '❌ VERIFIER FAIL'} "
          f"({len(RESULTS) - len(failed)}/{len(RESULTS)} checks)")
    print("=" * 62)
    return 0 if not failed else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="inject a doc drift and expect FAIL (exit 0 if drift detected)")
    args = ap.parse_args()
    code = run(inject_drift=args.selftest)
    if args.selftest:
        drift_failed = any(not r["pass"] for r in RESULTS if r["check"] == "V2")
        print(f"selftest: injected drift {'detected ✅' if drift_failed else 'NOT detected ❌'}")
        code = 0 if drift_failed else 1
    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
