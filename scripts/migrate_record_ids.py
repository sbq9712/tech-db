#!/usr/bin/env python3
"""Create a per-dataset stable RecordIdMap using the persistent registry."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qa-backend"))
from record_registry import RecordRegistry, build_record_id_map


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", type=Path)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.records.read_bytes()
    records = json.loads(raw)
    if not isinstance(records, list):
        raise SystemExit("records input must be a JSON list")
    mapping = build_record_id_map("sha256:" + hashlib.sha256(raw).hexdigest(), records, RecordRegistry(args.registry))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    temp.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")
    temp.replace(args.output)
    print(f"mapped {len(mapping['mappings'])} records exactly once -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
