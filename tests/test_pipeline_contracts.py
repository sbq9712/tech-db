#!/usr/bin/env python3
"""Deterministic regression tests for pipeline helpers; no network or model calls."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Importing production modules is deterministic; main() is guarded.
import auto_pipeline as pipeline
from data_contract import AI_DERIVED_FIELDS, enforce_terminal_categories


class PipelineContractTests(unittest.TestCase):
    def test_dedup_removes_duplicates_inside_same_new_batch(self):
        records = [
            {"t": "Same title", "b": "body", "u": "https://example.com/a"},
            {"t": "Same title", "b": "different", "u": "https://example.com/b"},
        ]
        unique, dupes = pipeline.dedup_check(records, [])
        self.assertEqual(len(unique), 1)
        self.assertEqual(dupes, 1)

    def test_normalize_url_collapses_doi_forms(self):
        a = pipeline.normalize_url("https://doi.org/10.1234/Test.1")
        b = pipeline.normalize_url("https://publisher.example/10.1234/Test.1")
        self.assertEqual(a, b)

    def test_state_hashes_are_namespaced_by_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = pipeline.STATE_FILE
            pipeline.STATE_FILE = os.path.join(tmp, "state.json")
            try:
                pipeline.commit_state(
                    {"news": ["same.csv"], "literature": ["same.csv"]},
                    {"news": [("same.csv", "hash-news")], "literature": [("same.csv", "hash-lit")]},
                )
                state = pipeline.load_state()
            finally:
                pipeline.STATE_FILE = old_state
        self.assertEqual(state["file_hashes"]["news/same.csv"], "hash-news")
        self.assertEqual(state["file_hashes"]["literature/same.csv"], "hash-lit")

    def test_unrelated_is_terminal_but_preserves_manual_fields(self):
        record = {
            "c": "不相关", "lv": 3, "cm": "人工评论", "wr": "性能突破||人工原因",
            "b": "原文", "sc": 8.0, "scd": {"b": 8}, "aip": 1,
            "as": "摘要", "kp": ["参数"], "tp": "主题", "cl": "x", "cp": 1, "cln": "事件",
        }
        removed = enforce_terminal_categories([record])
        self.assertEqual(removed, len(AI_DERIVED_FIELDS))
        self.assertEqual(record["cm"], "人工评论")
        self.assertEqual(record["wr"], "性能突破||人工原因")
        self.assertEqual(record["lv"], 3)
        self.assertEqual(record["b"], "原文")
        for field in AI_DERIVED_FIELDS:
            self.assertNotIn(field, record)


if __name__ == "__main__":
    unittest.main()
