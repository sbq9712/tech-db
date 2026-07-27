#!/usr/bin/env python3
"""Batch LLM adjudication from pre-computed candidates file, with checkpoint resume."""
import json, sys, os, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from pathlib import Path
import clustering

REPO = "/home/rhett/tech-db-fresh"
CANDIDATES_FILE = "/tmp/clustering-candidates.json"
DATA_FILE = os.path.join(REPO, "data", "processed", "all-records-lite.json")

data = json.load(open(DATA_FILE))
candidates = json.load(open(CANDIDATES_FILE))
existing = clustering.load_checkpoint()
print(f"Loaded {len(candidates)} candidates, {len(existing)} already in checkpoint", flush=True)

decisions = clustering.adjudicate(data, candidates, "zai", "glm-5.2")
accepted = [k for k, v in decisions.items() if v.get("accepted")]
print(f"Adjudicated {len(decisions)} pairs, accepted {len(accepted)}", flush=True)

judged_indices = sorted({idx for pair in decisions for idx in pair})
groups = clustering.complete_link_groups(judged_indices, decisions)
print(f"Complete-link groups (>1 member): {len(groups)}", flush=True)

applied = clustering.apply_groups(data, groups, decisions) if groups else []
print(f"Applied clusters: {len(applied)}", flush=True)

if applied:
    from build_snapshot import build_snapshot
    build_snapshot(data)
    print("Snapshot rebuilt", flush=True)
    r = subprocess.run(["python3", "scripts/validate_data_contract.py"], capture_output=True, text=True, cwd=REPO)
    print(r.stdout.strip(), flush=True)
    if r.returncode != 0:
        print("VALIDATION FAILED: " + r.stderr[:500], flush=True)
        sys.exit(1)

report = {"candidates": len(candidates), "accepted_pairs": len(accepted),
          "groups": groups, "applied": [{"cluster": a["cluster"], "root": a["root"],
          "members": a["members"], "name": a["name"]} for a in applied]}
Path("/tmp/clustering-full-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nDone. Clusters applied: {len(applied)}", flush=True)
