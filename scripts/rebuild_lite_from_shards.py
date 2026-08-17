#!/usr/bin/env python3
"""Rebuild all-records-lite.json from lite-part-*.js shard files.
Used in CI environments where the lite JSON is not in git.
"""
import json, re, glob, os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARD_DIR = os.path.join(REPO, "data", "processed")
LITE_PATH = os.path.join(SHARD_DIR, "all-records-lite.json")

def main():
    parts = []
    # Sort by NUMERIC shard suffix — string sort would order part-10 before
    # part-2 once shard count exceeds 10, silently scrambling record order
    # (breaks validate_data_contract's lite==shards check and every idx-keyed
    # index built from the rebuilt lite).
    def _shard_num(path):
        name = os.path.basename(path)
        return int(name.rsplit("-", 1)[1].split(".")[0])

    for f in sorted(glob.glob(os.path.join(SHARD_DIR, "lite-part-*.js")), key=_shard_num):
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        # Extract the JSON array from: window.__LITE_PARTS__.push([...]);
        marker = "window.__LITE_PARTS__.push("
        start = text.find(marker)
        end = text.rfind(");")
        if start < 0 or end <= start:
            continue
        payload = text[start + len(marker):end]
        parts.extend(json.loads(payload))
    
    with open(LITE_PATH, "w", encoding="utf-8") as f:
        json.dump(parts, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Rebuilt {LITE_PATH}: {len(parts)} records from {len(glob.glob(os.path.join(SHARD_DIR, 'lite-part-*.js')))} shards")

if __name__ == "__main__":
    main()
