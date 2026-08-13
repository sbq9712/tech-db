#!/usr/bin/env python3
"""
Final quality verification (Ticket 06).
Run after ingest completes to verify:
  1. GraphML node/edge counts match scale
  2. VDB embeddings complete (no missing)
  3. graph-export.json generated and complete
  4. 50-record random sampling: kp/as match source records
  5. No embedding flush failures
"""
import json
import random
import re
import sys
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
INDEX_DIR = REPO / "runtime" / "indexes"
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
WARN = "\033[93m⚠ WARN\033[0m"

results = []


def report(name, passed, detail=""):
    status = PASS if passed else FAIL
    results.append((name, passed))
    print(f"  {status} {name}: {detail}")


def check_graphml():
    """Check 1: GraphML node/edge counts."""
    print("\n[1/5] GraphML Integrity")
    graphml = INDEX_DIR / "graph_chunk_entity_relation.graphml"
    if not graphml.exists():
        report("GraphML exists", False, "file not found")
        return 0, 0

    # Parse GraphML to count nodes and edges
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(graphml)
        root = tree.getroot()
        # Handle namespaces
        ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
        nodes = root.findall('.//g:node', ns)
        edges = root.findall('.//g:edge', ns)
        if not nodes:
            nodes = root.findall('.//{http://graphml.graphdrawing.org/xmlns}node')
            edges = root.findall('.//{http://graphml.graphdrawing.org/xmlns}edge')
        node_count = len(nodes)
        edge_count = len(edges)
    except Exception as e:
        report("GraphML parseable", False, str(e))
        return 0, 0

    report("GraphML parseable", True, f"{node_count} nodes, {edge_count} edges")
    report("Node count > 1000", node_count > 1000, f"{node_count}")
    report("Edge count > 1000", edge_count > 1000, f"{edge_count}")

    # Check for chunk nodes vs entity nodes
    chunk_nodes = sum(1 for n in nodes if n.get('id', '').startswith('doc-'))
    entity_nodes = node_count - chunk_nodes
    report("Entity nodes > 500", entity_nodes > 500, f"{entity_nodes} entities, {chunk_nodes} chunks")

    return node_count, edge_count


def check_vdb():
    """Check 2: VDB embeddings complete."""
    print("\n[2/5] VDB Embedding Completeness")
    for name in ['vdb_chunks', 'vdb_entities', 'vdb_relationships']:
        fpath = INDEX_DIR / f"{name}.json"
        if not fpath.exists():
            report(f"{name} exists", False, "file not found")
            continue
        try:
            d = json.loads(fpath.read_text("utf-8"))
            data = d.get("data", [])
            matrix = d.get("matrix", [])
            # Check every entry has a corresponding embedding
            missing = sum(1 for i, entry in enumerate(data)
                          if i >= len(matrix) or not matrix[i])
            report(f"{name} no missing embeddings",
                   missing == 0,
                   f"{len(data)} vectors, {missing} missing")
            report(f"{name} count > 0", len(data) > 0, f"{len(data)} vectors")
        except Exception as e:
            report(f"{name} loadable", False, str(e))


def check_export():
    """Check 3: graph-export.json complete."""
    print("\n[3/5] Graph Export Completeness")
    export_file = INDEX_DIR / "graph-export.json"
    if not export_file.exists():
        report("graph-export.json exists", False, "file not found")
        return

    try:
        d = json.loads(export_file.read_text("utf-8"))
        nodes = d.get("nodes", [])
        edges = d.get("edges", [])

        report("Export has nodes", len(nodes) > 0, f"{len(nodes)} nodes")
        report("Export has edges", len(edges) > 0, f"{len(edges)} edges")

        # Check no zero-degree nodes (all nodes should have at least one edge)
        zero_degree = sum(1 for n in nodes if n.get("degree", 0) == 0)
        zero_pct = 100 * zero_degree / max(len(nodes), 1)
        report("Zero-degree nodes < 20%",
               zero_pct < 20,
               f"{zero_degree}/{len(nodes)} ({zero_pct:.1f}%)")

        # Check edge connectivity (source and target should be valid node IDs)
        node_ids = {n["id"] for n in nodes}
        broken_edges = sum(1 for e in edges
                          if e.get("source") not in node_ids
                          or e.get("target") not in node_ids)
        report("Edge connectivity valid",
               broken_edges == 0,
               f"{broken_edges} broken edges")

        # Check for doc- prefixed nodes/edges (should be filtered out)
        doc_nodes = sum(1 for n in nodes if n.get("id", "").startswith("doc-"))
        doc_edges = sum(1 for e in edges
                       if e.get("source", "").startswith("doc-")
                       or e.get("target", "").startswith("doc-"))
        report("No chunk nodes in export", doc_nodes == 0, f"{doc_nodes} chunk nodes")
        report("No chunk edges in export", doc_edges == 0, f"{doc_edges} chunk edges")

    except Exception as e:
        report("graph-export.json loadable", False, str(e))


def check_sampling():
    """Check 4: 50-record random sampling."""
    print("\n[4/5] 50-Record Random Sampling")
    data = json.loads(LITE_PATH.read_text("utf-8"))

    # Get curated records
    curated = [(i, r) for i, r in enumerate(data) if r.get('aip') or r.get('lv', 0) >= 1]

    # Sample 50 random records
    random.seed(42)
    sample = random.sample(curated, min(50, len(curated)))

    kp_ok = 0
    as_ok = 0
    kp_issues = []
    as_issues = []

    for idx, r in sample:
        # Check kp
        kp = r.get('kp', [])
        if kp:
            title = r.get('t', '')
            body = (r.get('b', '') or r.get('fb', '') or '')
            combined = f"{title} {body}"
            # Simple check: at least one kp term should appear in the record
            has_match = False
            for param in kp[:3]:
                param_str = str(param)
                # Extract bracket terms
                for bracket in re.findall(r'\[([^\]]+)\]', param_str):
                    if bracket in combined:
                        has_match = True
                        break
                # Extract prefix
                prefix = param_str.split('[')[0].strip()
                if prefix and len(prefix) >= 2 and prefix in combined:
                    has_match = True
                    break
            if has_match or not kp:
                kp_ok += 1
            else:
                kp_issues.append((idx, r.get('t', '')[:50], kp[:1]))
        else:
            kp_ok += 1  # No kp is valid

        # Check as
        summary = r.get('as', '').strip()
        if summary:
            title = r.get('t', '')
            # Check at least some title chars appear in summary
            title_chars = set(re.findall(r'[一-鿿]', title))
            sum_chars = set(re.findall(r'[一-鿿]', summary))
            if title_chars:
                overlap = len(title_chars & sum_chars) / len(title_chars)
                if overlap >= 0.3:
                    as_ok += 1
                else:
                    as_issues.append((idx, title[:50], summary[:60]))
            else:
                as_ok += 1  # Can't check non-Chinese titles
        else:
            as_ok += 1  # No summary is valid for some records

    report("KP sampling ≥90%", kp_ok >= 45, f"{kp_ok}/50 passed")
    report("AS sampling ≥90%", as_ok >= 45, f"{as_ok}/50 passed")

    if kp_issues:
        print(f"  KP issues ({len(kp_issues)}):")
        for idx, title, kp in kp_issues[:3]:
            print(f"    [{idx}] {title} → {kp}")
    if as_issues:
        print(f"  AS issues ({len(as_issues)}):")
        for idx, title, summary in as_issues[:3]:
            print(f"    [{idx}] {title} → {summary}")


def check_flush_failures():
    """Check 5: No embedding flush failures."""
    print("\n[5/5] Flush Failure Check")
    # Check doc_status for failed entries
    ds_file = INDEX_DIR / "kv_store_doc_status.json"
    if ds_file.exists():
        ds = json.loads(ds_file.read_text("utf-8"))
        failed = [(k, v) for k, v in ds.items()
                  if isinstance(v, dict) and v.get('status') == 'failed']
        # Filter out duplicate-related failures (expected)
        real_failures = [(k, v) for k, v in failed
                        if 'DUPLICATE' not in str(v.get('content_summary', ''))]
        report("No real flush failures",
               len(real_failures) == 0,
               f"{len(real_failures)} real failures ({len(failed)} total, {len(failed)-len(real_failures)} duplicates)")
    else:
        report("doc_status exists", False, "file not found")


def main():
    print("=" * 60)
    print("  FINAL QUALITY VERIFICATION (Ticket 06)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Check ingest progress
    progress_file = INDEX_DIR / "ingest_progress.json"
    if progress_file.exists():
        p = json.loads(progress_file.read_text("utf-8"))
        done = len(p.get("done_ids", []))
        print(f"\n  Ingest progress: {done} records done")

    check_graphml()
    check_vdb()
    check_export()
    check_sampling()
    check_flush_failures()

    # Summary
    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed}/{total} checks passed")
    print(f"{'='*60}")

    if passed == total:
        print(f"\n  {PASS} ALL CHECKS PASSED")
    else:
        failed_names = [name for name, p in results if not p]
        print(f"\n  {FAIL} {len(failed_names)} checks failed:")
        for name in failed_names:
            print(f"    - {name}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
