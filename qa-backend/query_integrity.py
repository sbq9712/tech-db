"""
T042 — Query Integrity / Verified Conversation Context
=======================================================
Preserves original_query and tracks semantic_diff from rewrite.

Rules:
  1. Keep original_query throughout the pipeline
  2. Rewrite outputs rewritten_query + semantic_diff
  3. Router/Planner/Entity Resolver see both original + rewrite
  4. Follow-up only uses VERIFIED/SUPPORTED claims as factual premise
  5. Novelty uses soft penalty, not blind hard-exclude of authoritative sources
"""
import re
from typing import List, Dict, Optional


def compute_semantic_diff(original: str, rewritten: str) -> dict:
    """Compute semantic differences between original and rewritten query.

    Returns:
        {
            "entities_added": [...],
            "entities_removed": [...],
            "time_changed": bool,
            "negation_changed": bool,
            "comparison_objects_changed": bool,
            "scope_changed": str,
            "risk_level": "none" | "low" | "high",
        }
    """
    # Extract entities (simple: capitalized words + known org names)
    orig_entities = set(re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", original))
    rewrite_entities = set(re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", rewritten))

    # Chinese entity patterns
    orig_cn = set(re.findall(r"[一-鿿]{2,}", original))
    rewrite_cn = set(re.findall(r"[一-鿿]{2,}", rewritten))

    entities_added = list((rewrite_entities - orig_entities) | (rewrite_cn - orig_cn))
    entities_removed = list((orig_entities - rewrite_entities) | (orig_cn - rewrite_cn))

    # Check time changes
    orig_time = re.findall(r"\d{4}年?|\d{1,2}月|去年|今年|前年|最近|最新", original)
    rewrite_time = re.findall(r"\d{4}年?|\d{1,2}月|去年|今年|前年|最近|最新", rewritten)
    time_changed = orig_time != rewrite_time

    # Check negation changes
    orig_neg = bool(re.search(r"不|没有|无|非|didn't|doesn't|not|no\b", original, re.IGNORECASE))
    rewrite_neg = bool(re.search(r"不|没有|无|非|didn't|doesn't|not|no\b", rewritten, re.IGNORECASE))
    negation_changed = orig_neg != rewrite_neg

    # Check if removed entities are substrings of added ones (expansion, not loss)
    real_removed = [e for e in entities_removed
                    if not any(e in added or added in e for added in entities_added)]
    real_added = [e for e in entities_added
                  if not any(e in removed or removed in e for removed in entities_removed)]

    # Determine risk
    risk = "none"
    if negation_changed:
        risk = "high"  # Negation flip is very dangerous
    elif real_removed:
        risk = "high"  # Entity genuinely dropped
    elif time_changed:
        risk = "high"  # Time changed
    elif real_added:
        risk = "low"  # Added context is usually fine

    return {
        "entities_added": entities_added[:5],
        "entities_removed": entities_removed[:5],
        "time_changed": time_changed,
        "negation_changed": negation_changed,
        "comparison_objects_changed": bool(entities_removed or entities_added),
        "scope_changed": "expanded" if entities_added and not entities_removed else
                         "narrowed" if entities_removed and not entities_added else
                         "unchanged",
        "risk_level": risk,
    }


def should_revert_rewrite(diff: dict) -> bool:
    """Check if the rewrite should be reverted due to high-risk changes.

    Returns True if the rewrite introduced dangerous semantic changes.
    """
    return diff.get("risk_level") == "high"


def filter_verified_premises(history: list, verified_status: dict = None) -> list:
    """Filter conversation history to only include VERIFIED claims as premise.

    Args:
        history: Conversation history messages
        verified_status: {message_index: "SUPPORTED" | "PARTIALLY_SUPPORTED" | ...}

    Returns:
        Filtered history with only verified claims
    """
    if not verified_status:
        return history  # No verification info → keep all (legacy mode)

    filtered = []
    for i, msg in enumerate(history):
        status = verified_status.get(i, "SUPPORTED")  # Default: keep
        if status in ("SUPPORTED", "PARTIALLY_SUPPORTED"):
            filtered.append(msg)
        # UNSUPPORTED and UNVERIFIED claims are dropped from factual premise

    return filtered


def build_conversation_context(
    original_query: str,
    rewritten_query: str,
    history: list,
    verified_status: dict = None,
) -> dict:
    """Build safe conversation context.

    Returns:
        {
            "original_query": str,
            "rewritten_query": str,
            "semantic_diff": dict,
            "verified_history": list,
            "use_original": bool,  # True if rewrite was reverted
        }
    """
    diff = compute_semantic_diff(original_query, rewritten_query)
    use_original = should_revert_rewrite(diff)
    verified_history = filter_verified_premises(history, verified_status)

    return {
        "original_query": original_query,
        "rewritten_query": original_query if use_original else rewritten_query,
        "semantic_diff": diff,
        "verified_history": verified_history,
        "use_original": use_original,
    }
