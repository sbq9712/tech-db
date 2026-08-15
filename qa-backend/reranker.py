"""
T016 — Content-aware Reranker
==============================
Second-stage reranker using query + candidate content to judge relevance.

Uses GLM for batch/listwise reranking (not N individual calls).

Interface:
    async def rerank(query, candidates, top_k=None, context=None) -> list

Output per candidate:
    record_id, rerank_score, input_rank, reason(optional), route_features

Key rules:
  - Reranker only judges relevance/usefulness for this query
  - Does NOT judge source independence, coverage, or final trust
  - Error has fallback (returns original order, never empties all)
  - Does NOT use synthetic AI summary as sole rerank evidence
"""
import json
import os
from typing import List, Optional

from config import llm_model_func

# Max candidates per LLM batch (to avoid context overflow)
MAX_BATCH_SIZE = int(os.environ.get("QA_RERANKER_BATCH_SIZE", "20"))

# Max total candidates to rerank
MAX_RERANK_CANDIDATES = int(os.environ.get("QA_MAX_RERANK_CANDIDATES", "150"))


def llm_batch_count(n_candidates: int) -> int:
    """Number of GLM calls rerank() will issue for n candidates.

    Codex-review fix (P1): rerank() batches candidates at MAX_BATCH_SIZE per
    LLM call (one GLM request per batch). Loop-control budget accounting must
    count the ACTUAL number of LLM calls, not one per logical rerank() — with
    21–50 results a round silently spends 2–3 budget-worthy calls while the
    budget ledger recorded only one, violating the ≤12 hard cap guarantee.
    """
    n = max(0, min(int(n_candidates), MAX_RERANK_CANDIDATES))
    if n == 0:
        return 0
    return (n + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE


RERANK_PROMPT = """你是技术情报检索专家。判断以下候选文档对回答用户问题的相关性。

用户问题：{query}

候选文档列表（已按初步检索得分排序）：
{candidate_list}

规则：
1. 只判断"这条候选对当前query有多相关、有多有用"
2. 不考虑来源可信度、独立性、覆盖度（这些由后续模块处理）
3. 基于标题和正文片段判断，不只看AI摘要

只输出JSON数组，每项包含 id 和 score（0-1，越高越相关）：
[{{"id": 1, "score": 0.95}}, {{"id": 2, "score": 0.70}}, ...]"""


async def rerank(
    query: str,
    candidates: list,
    top_k: int = None,
    context: dict = None,
    get_record_fn=None,
) -> list:
    """Rerank candidates by content-level relevance to the query.

    Args:
        query: User query
        candidates: List of dicts with at least "record_id", "meta"
        top_k: Optional limit on results
        context: Additional context (e.g. conversation history)
        get_record_fn: Function(record_id) → record dict (for body text access)

    Returns:
        List of dicts with: record_id, rerank_score, input_rank, reason
    """
    if not candidates:
        return []

    # Limit total candidates
    candidates = candidates[:MAX_RERANK_CANDIDATES]

    # Build candidate list for prompt
    candidate_list = []
    for i, c in enumerate(candidates):
        rid = c.get("record_id", c.get("meta", {}).get("idx", -1))
        meta = c.get("meta", {})
        title = meta.get("t", "")
        source = meta.get("s", meta.get("a", ""))
        date = meta.get("d", "")
        category = meta.get("c", "")

        # Get body text if available
        body_snippet = ""
        if get_record_fn:
            try:
                rec = get_record_fn(rid)
                if rec:
                    body_snippet = (rec.get("b", "") or rec.get("as", ""))[:200]
            except Exception:
                pass
        else:
            body_snippet = meta.get("as", "")[:200] if meta else ""

        candidate_list.append(
            f"[{i+1}] 标题：{title}\n"
            f"    来源：{source} | 日期：{date}\n"
            f"    分类：{category}\n"
            f"    正文片段：{body_snippet}"
        )

    candidate_str = "\n".join(candidate_list)

    # Process in batches if needed
    all_scores = {}
    for batch_start in range(0, len(candidates), MAX_BATCH_SIZE):
        batch = candidates[batch_start:batch_start + MAX_BATCH_SIZE]
        batch_str = "\n".join(candidate_list[batch_start:batch_start + len(batch)])

        prompt = RERANK_PROMPT.format(query=query[:300], candidate_list=batch_str)

        try:
            result_text = await llm_model_func(
                prompt,
                system_prompt="你是技术情报检索专家。只输出JSON数组。",
                temperature=0.0,
                max_tokens=2048,
                allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
            )

            scores = _parse_rerank_result(result_text, len(batch))
            for i, score in enumerate(scores):
                orig_idx = batch_start + i
                if orig_idx < len(candidates):
                    rid = candidates[orig_idx].get("record_id",
                                                    candidates[orig_idx].get("meta", {}).get("idx", -1))
                    all_scores[rid] = score

        except Exception as e:
            print(f"[reranker] Error: {e}", flush=True)
            # Fallback: use original order with decreasing scores
            for i, c in enumerate(batch):
                orig_idx = batch_start + i
                rid = c.get("record_id", c.get("meta", {}).get("idx", -1))
                all_scores[rid] = 1.0 / (i + 2)  # RRF-like fallback

    # Build output sorted by rerank score
    results = []
    for c in candidates:
        rid = c.get("record_id", c.get("meta", {}).get("idx", -1))
        score = all_scores.get(rid, 0.0)
        results.append({
            "record_id": rid,
            "rerank_score": round(score, 4),
            "input_rank": c.get("rank", 0),
            "meta": c.get("meta", {}),
        })

    results.sort(key=lambda x: -x["rerank_score"])

    if top_k is not None:
        results = results[:top_k]

    return results


def _parse_rerank_result(text: str, expected_count: int) -> list:
    """Parse LLM rerank output into a list of scores."""
    import re

    if not text or not text.strip():
        return [0.5] * expected_count  # Neutral fallback

    try:
        # Strip code fences
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\[.*\]", stripped, flags=re.DOTALL)
        if not m:
            return [0.5] * expected_count

        data = json.loads(m.group(0))
        scores = []
        for item in data[:expected_count]:
            score = float(item.get("score", 0.5))
            scores.append(max(0.0, min(1.0, score)))
        # Pad if fewer scores returned
        while len(scores) < expected_count:
            scores.append(0.5)
        return scores

    except (json.JSONDecodeError, ValueError, TypeError):
        return [0.5] * expected_count
