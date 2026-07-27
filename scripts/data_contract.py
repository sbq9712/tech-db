#!/usr/bin/env python3
"""Immutable data contract shared by deterministic build and validation scripts."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "data" / "processed"
LITE_PATH = DATA_DIR / "all-records-lite.json"
MANIFEST_PATH = DATA_DIR / "manifest-data.js"
TAXONOMY_PATH = REPO / "data" / "category-taxonomy.json"
CATEGORY_ORDER_PATH = REPO / "data" / "category-order-data.js"
CHUNK_SIZE = 2000

VALID_NEWS_TAGS = frozenset({"技术突破", "产业进展", "政策监管", "资本运作", "行业观察"})
VALID_LIT_TAGS = frozenset({"研究论文", "观点评论"})
SPECIAL_CATEGORIES = frozenset({"不相关", "未分类"})
AI_DERIVED_FIELDS = ("sc", "scd", "aip", "as", "kp", "tp", "cl", "cp", "cln")


def is_relevant(record: dict) -> bool:
    return record.get("c", "") not in ("", "不相关", "未分类")


def enforce_terminal_categories(records: list[dict]) -> int:
    """Remove all AI-derived fields after a record becomes unrelated."""
    removed = 0
    for record in records:
        if record.get("c") != "不相关":
            continue
        for field in AI_DERIVED_FIELDS:
            if field in record:
                record.pop(field, None)
                removed += 1
    return removed


def load_taxonomy() -> list[str]:
    with TAXONOMY_PATH.open(encoding="utf-8") as f:
        leaves = json.load(f)["categories"]
    if len(leaves) != len(set(leaves)):
        raise ValueError("taxonomy contains duplicate leaves")
    return leaves


def parse_js_assignment(path: Path, variable: str) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    prefix = f"window.{variable}="
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError(f"invalid JS assignment format: {path}")
    return json.loads(text[len(prefix):-1])


def load_manifest() -> dict:
    return parse_js_assignment(MANIFEST_PATH, "__MANIFEST__")


def load_category_order() -> list[str]:
    return parse_js_assignment(CATEGORY_ORDER_PATH, "__CATEGORY_ORDER__")["categories"]


def expected_shard_count(record_count: int) -> int:
    return (record_count + CHUNK_SIZE - 1) // CHUNK_SIZE
