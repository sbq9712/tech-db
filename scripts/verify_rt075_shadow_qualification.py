#!/usr/bin/env python3
"""RT-075 REAL_WINDOW qualification verifier (machine-readable, fail-closed).

Reads an entity-shadow store (entity_shadow_store.py, append-only JSONL),
validates its tamper-evidence chain and origin purity, and decides the
RT-075 activation gate from PERSISTED evidence only:

- >= 1,000 unique representative observations
- >= 7-day window computed from record timestamps (never caller-supplied)
- single origin, schema-valid records, intact hash chain

Synthetic or replay origins are reported truthfully and never qualify as
production shadow evidence. Torn tails (crash mid-append) are tolerated
and reported; tampered records fail the store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime

QUALIFICATION_SCHEMA_VERSION = "rt075-shadow-qualification-1.0"
MIN_EVENTS = 1000
MIN_DAYS = 7.0


def _parse_ts(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def verify_store(path: str) -> dict:
    gate = {
        "store_header": False,
        "origin_pure": False,
        "schema_valid": False,
        "chain_intact": False,
        "event_count": False,
        "window_days": False,
    }
    origin = ""
    unique_events = 0
    window_days = 0.0
    torn_tail = False
    tamper = []
    timestamps = []
    header = None
    prev_sha = ""
    seen_keys = set()

    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                torn_tail = True  # last line torn by crash: tolerated
            else:
                tamper.append({"line": index + 1, "reason": "unparsable"})
            break
        line_sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
        if record.get("record_type") == "store_header":
            header = record
            origin = record.get("origin", "")
            gate["store_header"] = (
                record.get("schema_version") == "entity-shadow-store-1.0"
                and origin in ("real", "replay", "synthetic"))
            prev_sha = line_sha
            continue
        # observation record
        if record.get("prev_line_sha256") != prev_sha:
            tamper.append({"line": index + 1,
                           "reason": "chain break (prev_line_sha256)"})
        if record.get("origin") != origin:
            tamper.append({"line": index + 1,
                           "reason": f"origin mixing ({record.get('origin')!r})"})
        if record.get("schema_version") != "entity-shadow-store-1.0" or \
                record.get("record_type") != "shadow_observation" or \
                not isinstance(record.get("observation"), dict):
            tamper.append({"line": index + 1, "reason": "schema"})
        else:
            key = record.get("content_key")
            if key in seen_keys:
                tamper.append({"line": index + 1, "reason": "duplicate content_key"})
            else:
                seen_keys.add(key)
                unique_events += 1
            ts = _parse_ts(record.get("observed_at_utc", ""))
            if ts is not None:
                timestamps.append(ts)
        prev_sha = line_sha

    gate["chain_intact"] = not any("chain" in t["reason"] for t in tamper)
    gate["origin_pure"] = gate["store_header"] and \
        not any("origin" in t["reason"] for t in tamper)
    gate["schema_valid"] = header is not None and \
        not any(t["reason"] in ("schema", "duplicate content_key")
                for t in tamper)
    gate["event_count"] = unique_events >= MIN_EVENTS
    if len(timestamps) >= 2:
        window_days = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
        gate["window_days"] = window_days >= MIN_DAYS
    qualified = all(gate.values()) and origin == "real" and not tamper

    verdict = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "store": path,
        "origin": origin,
        "qualifies_as_production_shadow": bool(qualified),
        "unique_observations": unique_events,
        "window_days": round(window_days, 3),
        "torn_tail_tolerated": torn_tail,
        "tamper_findings": tamper,
        "gate": gate,
        "thresholds": {"min_events": MIN_EVENTS, "min_days": MIN_DAYS},
    }
    payload = dict(verdict)
    payload.pop("store")
    verdict["verdict_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="path to shadow store JSONL")
    parser.add_argument("--out", help="optional path for the JSON verdict")
    args = parser.parse_args()
    try:
        verdict = verify_store(args.store)
    except OSError as exc:
        print(json.dumps({"schema_version": QUALIFICATION_SCHEMA_VERSION,
                          "store": args.store,
                          "qualifies_as_production_shadow": False,
                          "error": f"store unreadable: {exc}"}, indent=2))
        return 2
    rendered = json.dumps(verdict, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    print(rendered)
    return 0 if verdict.get("qualifies_as_production_shadow") else 1


if __name__ == "__main__":
    raise SystemExit(main())
