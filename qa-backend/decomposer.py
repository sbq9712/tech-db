"""
T019 — Query Decomposition
===========================
Decomposes complex questions into verifiable requirements and
executable subqueries.

Output:
  {
    "requirements": [
      {
        "id": "r1",
        "description": "NVIDIA interconnect evolution",
        "importance": "critical",  # critical/important/optional
        "entities": ["org:nvidia"],
        "dimensions": ["interconnect"],
        "queries": ["NVIDIA NVLink bandwidth", "NVIDIA interconnect technology"]
      }
    ]
  }

Rules:
  - requirements must map back to original user intent
  - comparison type must consider object × dimension coverage
  - trend type must consider time stages
  - No large numbers of synonymous queries
  - MAX_SUBQUERIES configurable (default 8-12)
  - Ambiguous queries cover multiple interpretations
"""
import json
import os
from config import llm_model_func

MAX_SUBQUERIES = int(os.environ.get("QA_MAX_SUBQUERIES", "10"))


DECOMPOSE_PROMPT = """你是技术情报分析专家。将复杂问题拆解为可验证的需求和子查询。

规则：
1. 每个requirement必须能映射回原始用户意图
2. 比较类必须考虑 对象×维度 的覆盖
3. 趋势类必须考虑时间段
4. 不要生成大量同义查询
5. 最多{max_subqueries}个子查询
6. importance分为：critical（必须满足）、important、optional

只输出JSON：
{{
  "requirements": [
    {{
      "id": "r1",
      "description": "需求描述",
      "importance": "critical",
      "entities": ["相关实体"],
      "dimensions": ["维度1"],
      "queries": ["子查询1", "子查询2"]
    }}
  ]
}}

原始问题：{query}
问题类型：{question_type}
语义上下文：{context}}"""


async def decompose_query(
    query: str,
    question_type: str = "FACT_LOOKUP",
    context: str = "",
) -> dict:
    """Decompose a complex query into requirements and subqueries.

    Args:
        query: Original user query
        question_type: From router (COMPARISON, TREND, etc.)
        context: Additional context (conversation history summary)

    Returns:
        {"requirements": [...]}
    """
    # Simple queries don't need decomposition
    if question_type in ("FACT_LOOKUP", "FOLLOWUP"):
        return {
            "requirements": [{
                "id": "r1",
                "description": query[:200],
                "importance": "critical",
                "entities": [],
                "dimensions": [],
                "queries": [query],
            }]
        }

    try:
        prompt = DECOMPOSE_PROMPT.format(
            query=query[:500],
            question_type=question_type,
            context=context[:300] or "none",
            max_subqueries=MAX_SUBQUERIES,
        )

        result_text = await llm_model_func(
            prompt,
            system_prompt="你是查询分解专家。只输出JSON。",
            temperature=0.0,
            max_tokens=2048,
        )

        return _parse_decomposition(result_text, query)

    except Exception as e:
        print(f"[decomposer] Error: {e}", flush=True)
        # Fallback: single requirement
        return {
            "requirements": [{
                "id": "r1",
                "description": query[:200],
                "importance": "critical",
                "entities": [],
                "dimensions": [],
                "queries": [query],
            }]
        }


def _parse_decomposition(text: str, original_query: str) -> dict:
    """Parse decomposition LLM output."""
    import re
    if not text:
        return _fallback_decomposition(original_query)

    try:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            requirements = data.get("requirements", [])

            # Validate and clean
            cleaned = []
            for req in requirements:
                if not isinstance(req, dict):
                    continue
                rid = req.get("id", f"r{len(cleaned) + 1}")
                desc = req.get("description", "")
                importance = req.get("importance", "important")
                queries = req.get("queries", [original_query])

                # Limit subqueries
                queries = queries[:MAX_SUBQUERIES]

                cleaned.append({
                    "id": rid,
                    "description": desc,
                    "importance": importance,
                    "entities": req.get("entities", []),
                    "dimensions": req.get("dimensions", []),
                    "queries": queries,
                })

            if cleaned:
                return {"requirements": cleaned}

    except (json.JSONDecodeError, ValueError):
        pass

    return _fallback_decomposition(original_query)


def _fallback_decomposition(query: str) -> dict:
    """Fallback: single requirement."""
    return {
        "requirements": [{
            "id": "r1",
            "description": query[:200],
            "importance": "critical",
            "entities": [],
            "dimensions": [],
            "queries": [query],
        }]
    }
