"""Retrieval and answer quality metrics for evaluation.

Metrics:
  Retrieval:
    - Recall@K: Fraction of correct records in top-K
    - MRR: Mean Reciprocal Rank
    - nDCG@K: Normalized Discounted Cumulative Gain

  Answer:
    - Citation Precision: Correct citations / total citations
    - Exact Span Validity: Fraction of citations with valid exact spans
    - Claim Support Rate: Fraction of major claims with support
    - Unsupported Claim Rate: Fraction of major claims unsupported

  Abstention:
    - Correct Abstention Rate: Correctly abstained / should abstain
    - False Refusal Rate: Incorrectly abstained / should answer
"""
import math
from typing import List


def recall_at_k(retrieved_ids: list, correct_ids: list, k: int = 25) -> float:
    """Recall@K: fraction of correct items in top-K retrieved."""
    if not correct_ids:
        return 0.0
    retrieved_set = set(retrieved_ids[:k])
    correct_set = set(correct_ids)
    return len(retrieved_set & correct_set) / len(correct_set)


def mrr(retrieved_ids: list, correct_ids: list) -> float:
    """Mean Reciprocal Rank: 1/rank of first correct item."""
    if not correct_ids:
        return 0.0
    correct_set = set(correct_ids)
    for rank, rid in enumerate(retrieved_ids, 1):
        if rid in correct_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: list, correct_ids: list, k: int = 25) -> float:
    """Normalized Discounted Cumulative Gain at K."""
    if not correct_ids:
        return 0.0
    correct_set = set(correct_ids)
    k = min(k, len(retrieved_ids))

    # DCG
    dcg = 0.0
    for i in range(k):
        rel = 1.0 if retrieved_ids[i] in correct_set else 0.0
        dcg += rel / math.log2(i + 2)

    # IDCG (ideal: all correct items first)
    ideal_hits = min(len(correct_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))

    if idcg == 0:
        return 0.0
    return dcg / idcg


def citation_precision(cited_ids: list, correct_ids: list) -> float:
    """Fraction of cited records that are actually correct."""
    if not cited_ids:
        return 0.0
    correct_set = set(correct_ids) if correct_ids else set()
    if not correct_set:
        return 0.0  # Can't evaluate without ground truth
    correct_cited = sum(1 for cid in cited_ids if cid in correct_set)
    return correct_cited / len(cited_ids)


def claim_support_rate(claims: list) -> float:
    """Fraction of major claims that have support."""
    major_types = {"MAJOR_FACT", "NUMERIC_FACT", "COMPARISON", "CAUSAL", "ATTRIBUTED_CLAIM"}
    major = [c for c in claims if c.get("type") in major_types]
    if not major:
        return 1.0  # No major claims → trivially 100%
    supported = [c for c in major if c.get("support_status") == "SUPPORTED"]
    return len(supported) / len(major)


def unsupported_claim_rate(claims: list) -> float:
    """Fraction of major claims that are unsupported."""
    major_types = {"MAJOR_FACT", "NUMERIC_FACT", "COMPARISON", "CAUSAL", "ATTRIBUTED_CLAIM"}
    major = [c for c in claims if c.get("type") in major_types]
    if not major:
        return 0.0
    unsupported = [c for c in major if c.get("support_status") == "UNSUPPORTED"]
    return len(unsupported) / len(major)


def abstention_accuracy(
    actual_statuses: list,
    expected_statuses: list,
) -> dict:
    """Calculate abstention metrics.

    Args:
        actual_statuses: List of actual answer statuses
        expected_statuses: List of expected statuses (SHOULD_ANSWER, SHOULD_PARTIAL, SHOULD_ABSTAIN)

    Returns:
        {
            "correct_abstention_rate": float,
            "false_refusal_rate": float,
            "partial_answer_accuracy": float,
        }
    """
    should_abstain = []
    should_answer = []
    should_partial = []

    for i, expected in enumerate(expected_statuses):
        actual = actual_statuses[i] if i < len(actual_statuses) else "UNSUPPORTED"
        if expected == "SHOULD_ABSTAIN":
            should_abstain.append(actual)
        elif expected == "SHOULD_PARTIAL":
            should_partial.append(actual)
        else:
            should_answer.append(actual)

    # Correct abstention: SHOULD_ABSTAIN → got UNSUPPORTED
    correct_abstain = sum(1 for s in should_abstain if s == "UNSUPPORTED")
    abstain_rate = correct_abstain / len(should_abstain) if should_abstain else 0.0

    # False refusal: SHOULD_ANSWER → got UNSUPPORTED
    false_refusal = sum(1 for s in should_answer if s == "UNSUPPORTED")
    refusal_rate = false_refusal / len(should_answer) if should_answer else 0.0

    # Partial accuracy: SHOULD_PARTIAL → got PARTIALLY_SUPPORTED
    correct_partial = sum(1 for s in should_partial if s == "PARTIALLY_SUPPORTED")
    partial_rate = correct_partial / len(should_partial) if should_partial else 0.0

    return {
        "correct_abstention_rate": abstain_rate,
        "false_refusal_rate": refusal_rate,
        "partial_answer_accuracy": partial_rate,
    }


def source_diversity(citations: list, provenance_groups: dict = None) -> float:
    """Calculate source diversity (unique independent groups / total citations)."""
    if not citations:
        return 0.0
    if provenance_groups:
        # Use provenance groups if available
        groups = set()
        for c in citations:
            rid = c.get("record_id", -1)
            groups.add(provenance_groups.get(rid, rid))  # fall back to record_id
        return len(groups) / len(citations)
    # Fall back to unique sources
    sources = set(c.get("source", "") for c in citations)
    return len(sources) / len(citations) if citations else 0.0
