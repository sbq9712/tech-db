#!/usr/bin/env python3
"""Deterministic clustering regression tests; no model/network calls."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import clustering


class ClusteringContractTests(unittest.TestCase):
    def article(self, title, date="2026-07-01", summary=""):
        return {"t": title, "d": date, "as": summary, "c": "AI与智能科技/AI软件层/底座大模型/文本模型"}

    def test_specific_model_entity_recall(self):
        a = self.article("OpenAI将公开发布GPT-5.6系列模型")
        b = self.article("GPT-5.6突然上线，普通用户暂不可用")
        # scheme-1: entity cosine floor is 0.70; 0.60 must NOT pass.
        self.assertIsNone(clustering.candidate_reason(a, b, 0.60))
        self.assertEqual(clustering.candidate_reason(a, b, 0.72), "specific-entity")

    def test_generic_sodium_keyword_cannot_bypass_embedding(self):
        a = self.article("钠离子电池正极材料研究")
        b = self.article("钠电池工厂正式投产")
        # cosine 0.60 is below both the 0.82 embedding floor and the 0.70 entity floor,
        # and generic words like 钠电池 are not specific entities.
        self.assertIsNone(clustering.candidate_reason(a, b, 0.60))

    def test_time_window_is_hard_limit(self):
        a = self.article("GPT-5.6发布", "2026-06-01")
        b = self.article("GPT-5.6上线", "2026-07-01")
        self.assertIsNone(clustering.candidate_reason(a, b, 0.99))

    def test_release_lifecycle_accepts_exact_model(self):
        a = self.article("OpenAI将公开发布GPT-5.6系列模型", "2026-07-09")
        b = self.article("GPT-5.6突然上线，普通用户暂不可用", "2026-06-27")
        accepted, key = clustering.release_lifecycle_pair(a, b)
        self.assertTrue(accepted)
        self.assertIn("GPT-5.6", key)

    def test_release_lifecycle_rejects_evaluation_or_leak(self):
        release = self.article("OpenAI正式发布GPT-5.6", "2026-06-27")
        leak = self.article("GPT-5.6 Pro泄露，实测流出", "2026-06-21")
        self.assertFalse(clustering.release_lifecycle_pair(release, leak)[0])

    def test_incidental_rumor_word_in_summary_does_not_block_release(self):
        a = self.article("OpenAI发布GPT-5.6", summary="正式发布")
        b = self.article("GPT-5.6突然上线", summary="比此前传闻的Mythos更强")
        self.assertTrue(clustering.release_lifecycle_pair(a, b)[0])

    def test_release_lifecycle_rejects_generic_sodium(self):
        a = self.article("企业A发布钠离子电池", "2026-07-01")
        b = self.article("企业B钠电池工厂上线", "2026-07-02")
        self.assertFalse(clustering.release_lifecycle_pair(a, b)[0])

    def test_complete_link_blocks_transitive_pollution(self):
        decisions = {
            (1, 2): {"accepted": True},
            (2, 3): {"accepted": True},
            (1, 3): {"accepted": False},
        }
        groups = clustering.complete_link_groups([1, 2, 3], decisions)
        self.assertEqual(groups, [[1, 2]])

    def test_low_confidence_same_is_not_accepted(self):
        result = {"same": True, "confidence": 0.70}
        result["accepted"] = bool(result["same"]) and result["confidence"] >= clustering.MIN_CONFIDENCE
        self.assertFalse(result["accepted"])


if __name__ == "__main__":
    unittest.main()
