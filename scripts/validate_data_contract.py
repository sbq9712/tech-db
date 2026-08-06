#!/usr/bin/env python3
"""Fail closed when generated data violates any frontend/pipeline contract."""
from __future__ import annotations

import json
import re
import re
import sys
from collections import Counter, defaultdict

from data_contract import (
    AI_DERIVED_FIELDS,
    CHUNK_SIZE,
    DATA_DIR,
    LITE_PATH,
    SPECIAL_CATEGORIES,
    VALID_LIT_TAGS,
    VALID_NEWS_TAGS,
    expected_shard_count,
    load_category_order,
    load_manifest,
    load_taxonomy,
)


def load_shard(path):
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(
        r"window\.__LITE_PARTS__=window\.__LITE_PARTS__\|\|\[\];"
        r"window\.__LITE_PARTS__\.push\((.*)\);",
        text,
        re.S,
    )
    if not match:
        raise ValueError(f"invalid shard wrapper: {path.name}")
    return json.loads(match.group(1))


def validate() -> list[str]:
    errors = []
    with LITE_PATH.open(encoding="utf-8") as f:
        records = json.load(f)

    taxonomy = load_taxonomy()
    category_order = load_category_order()
    allowed_categories = set(taxonomy) | set(SPECIAL_CATEGORIES)
    if category_order != taxonomy:
        errors.append("category-order-data.js must exactly equal immutable taxonomy order")

    invalid_categories = [(i, r.get("c", "")) for i, r in enumerate(records) if r.get("c", "") not in allowed_categories]
    if invalid_categories:
        errors.append(f"{len(invalid_categories)} records outside immutable taxonomy")

    unrelated_derived = [
        (i, field)
        for i, record in enumerate(records) if record.get("c") == "不相关"
        for field in AI_DERIVED_FIELDS if field in record and record.get(field) not in (None, "", [], {}, 0)
    ]
    if unrelated_derived:
        errors.append(f"{len(unrelated_derived)} AI-derived fields remain on unrelated records")

    invalid_alerts = [i for i, record in enumerate(records) if record.get("lv", 0) >= 3 and not record.get("wr")]
    if invalid_alerts:
        errors.append(f"{len(invalid_alerts)} alert records missing warning reason")

    # sr (source tracking) validation: if present, must match repo/filename pattern
    sr_re = re.compile(r'^(wechat|news|literature)/.+$')
    invalid_sr = [(i, r.get("sr")) for i, r in enumerate(records)
                  if r.get("sr") and not sr_re.match(r["sr"])]
    if invalid_sr:
        errors.append(f"{len(invalid_sr)} records have invalid sr format")

    invalid_tags = []
    missing_tags = []
    for i, record in enumerate(records):
        tag = record.get("tg", "")
        valid = VALID_LIT_TAGS if record.get("i") == "l" else VALID_NEWS_TAGS
        if not tag:
            missing_tags.append(i)
        elif tag not in valid:
            invalid_tags.append((i, record.get("i"), tag))
    if missing_tags:
        errors.append(f"{len(missing_tags)} records missing tag")
    if invalid_tags:
        errors.append(f"{len(invalid_tags)} records have invalid type/tag combinations")

    manifest = load_manifest()
    meta = manifest.get("meta", {})
    expected = expected_shard_count(len(records))
    if meta.get("records_total") != len(records):
        errors.append(f"manifest records_total={meta.get('records_total')} != {len(records)}")
    if meta.get("total_shards") != expected:
        errors.append(f"manifest total_shards={meta.get('total_shards')} != {expected}")

    shard_paths = sorted(DATA_DIR.glob("lite-part-*.js"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    expected_names = [f"lite-part-{i}.js" for i in range(expected)]
    if [p.name for p in shard_paths] != expected_names:
        errors.append("shard files are missing, non-contiguous, or stale")
    else:
        shard_records = []
        for path in shard_paths:
            shard_records.extend(load_shard(path))
        if shard_records != records:
            errors.append("concatenated shard records differ from all-records-lite.json")

    clusters = defaultdict(list)
    for i, record in enumerate(records):
        if record.get("cl") not in (None, ""):
            clusters[str(record["cl"])].append((i, record))
    for cluster_id, members in clusters.items():
        # Historical convention: cp=0 is the visible parent; cp=1 records are hidden children.
        parents = [r for _, r in members if r.get("cp") == 0]
        children = [r for _, r in members if r.get("cp") == 1]
        names = {r.get("cln", "") for _, r in members if r.get("cln")}
        if len(parents) != 1 or not children:
            errors.append(f"cluster {cluster_id}: expected exactly 1 cp=0 parent and >=1 cp=1 children")
        if len(names) > 1:
            errors.append(f"cluster {cluster_id}: inconsistent cluster names")

    return errors


if __name__ == "__main__":
    problems = validate()
    if problems:
        print("DATA CONTRACT FAILED")
        for problem in problems:
            print(f"- {problem}")
        sys.exit(1)
    print("DATA CONTRACT OK")
