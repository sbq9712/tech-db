"""
Epistemic protection module — prevents the QA system from presenting
opinions, predictions, or unverified claims as established facts.

Pipeline insertion points:
  Retriever → Reranker → [Claim Classifier] → Answer Generator → [Answer Verifier] → Final Answer

The Claim Classifier analyzes retrieved chunks and tags their claims with epistemic types.
The Answer Verifier audits the draft answer against the classified evidence.
"""
import json
import re
from typing import Optional

from config import llm_model_func


# ── Epistemic taxonomy ──

EPISTEMIC_TYPES = {
    "VERIFIABLE_FACT":   "可核验事实 — 有明确数据来源、可独立验证的客观陈述",
    "REPORTED_CLAIM":    "某主体声称/公布的信息 — 必须保留归属主体",
    "ESTIMATE":          "估算 — 基于模型的预测值",
    "PREDICTION":        "预测 — 对未来事件的判断",
    "ANALYSIS":          "分析或推断 — 基于已知信息的逻辑推导",
    "OPINION":           "观点或评价 — 主观判断",
    "MARKETING_HYPE":    "宣传性/夸张性表述",
    "UNCERTAIN":         "无法确定",
}

# Types that require attribution
ATTRIBUTION_REQUIRED = {"REPORTED_CLAIM", "OPINION", "PREDICTION", "MARKETING_HYPE"}
# Types that need hedging language
NEEDS_HEDGING = {"ESTIMATE", "PREDICTION", "ANALYSIS", "OPINION", "MARKETING_HYPE"}

# ── Issue types for the verifier ──

ISSUE_TYPES = {
    "OPINION_AS_FACT":       "将观点/评价当作事实陈述",
    "PREDICTION_AS_FACT":    "将预测当作已发生的事实",
    "CLAIM_AS_FACT":         "将某主体的声称当作独立验证的事实",
    "ATTRIBUTION_LOST":      "丢失来源归属（谁说的）",
    "OVERGENERALIZATION":    "从个例推广到普遍结论",
    "UNSUPPORTED_CLAIM":     "回答中有证据无法支撑的陈述",
    "TEMPORAL_ERROR":        "时间错位（未来/过去混淆）",
    "CONFLICT_IGNORED":      "忽略了来源间的事实矛盾",
}


# ── 1. Claim Classifier ──

CLASSIFY_PROMPT = """你是认识论信息分类专家。分析以下技术情报片段中与用户问题相关的主要声明(claim)，为每个声明分配认识论类型。

类型定义：
- VERIFIABLE_FACT: 可核验事实 — 有明确数据来源、可独立验证的客观陈述（如"该公司2025年营收10亿"）
- REPORTED_CLAIM: 某主体声称/公布的信息 — 必须保留归属主体（如"XX公司称其产品效率达30%"）
- ESTIMATE: 估算 — 基于模型的预测值（如"预计2030年市场规模将达500亿"）
- PREDICTION: 预测 — 对未来事件的判断
- ANALYSIS: 分析或推断 — 基于已知信息的逻辑推导
- OPINION: 观点或评价 — 主观判断（如"某领域正处于商业化元年"）
- MARKETING_HYPE: 宣传性/夸张性表述
- UNCERTAIN: 无法确定 — confidence低时使用

要求：
1. 不修改原始文本
2. speaker无法确认时设为null
3. evidence_span必须来自原始文本，不得编造
4. confidence低时使用UNCERTAIN

只输出JSON数组：
[{"chunk_id":"1","claims":[{"claim":"声明内容","type":"OPINION","speaker":"XX证券","must_attribute":true,"confidence":0.92,"evidence_span":"原文片段"}]}]

用户问题：{query}

待分析片段：
{chunks}
"""


async def classify_claims(query: str, search_results: list, top_k: int = 5) -> list:
    """Classify epistemic claims for the top-k search results.

    Returns list of {"chunk_id", "claims": [...]} dicts.
    """
    if not search_results:
        return []

    # Only classify top-k chunks (cost control)
    to_classify = search_results[:top_k]

    chunks_json = json.dumps([
        {
            "chunk_id": str(i + 1),
            "title": r.get("title", ""),
            "source": r.get("source", ""),
            "date": r.get("date", ""),
            "text": (r.get("summary", "") or r.get("body", ""))[:500],
        }
        for i, r in enumerate(to_classify)
    ], ensure_ascii=False)

    prompt = CLASSIFY_PROMPT.format(query=query, chunks=chunks_json)

    try:
        result = await llm_model_func(
            prompt,
            system_prompt="你是认识论信息分类专家。只输出JSON数组，不要输出其他内容。",
        )
        # Parse JSON array from result
        s, e = result.find("["), result.rfind("]")
        if s >= 0 and e > s:
            return json.loads(result[s:e + 1])
    except Exception as e:
        print(f"[epistemic-classify] Error: {e}", flush=True)

    return []


# ── 2. Answer Verifier ──

VERIFY_PROMPT = """你是事实核查专家。审查以下AI生成的回答草稿，检查是否存在认识论错误。

规则：
1. 将草稿拆成独立的原子陈述/句子
2. 对每个句子，检查与证据的认识论元数据是否一致
3. 至少检测以下错误类型：
   - OPINION_AS_FACT: 将观点/评价当作事实陈述
   - PREDICTION_AS_FACT: 将预测当作已发生的事实
   - CLAIM_AS_FACT: 将某主体的声称当作独立验证的事实
   - ATTRIBUTION_LOST: 丢失来源归属（谁说的）
   - OVERGENERALIZATION: 从个例推广到普遍结论
   - UNSUPPORTED_CLAIM: 回答中有证据无法支撑的陈述
   - TEMPORAL_ERROR: 时间错位
   - CONFLICT_IGNORED: 忽略了来源间的事实矛盾

如果不存在 high severity 问题，返回 {"passed": true}。
如果存在 high severity 问题，返回：
{{"passed": false, "issues": [{{"sentence": "有问题的句子", "issue_type": "OPINION_AS_FACT", "severity": "high", "evidence_chunk_ids": ["1"], "suggested_rewrite": "修正后的句子"}}], "rewritten_answer": "完整的修正后回答"}}

重写规则：
- 降低确定性（"是" → "有观点认为"）
- 恢复来源归属（添加"XX机构认为"）
- 删除无证据内容
- 修复事实/观点/预测性质
- 显示来源冲突
- 不允许引入新的事实

只输出JSON对象。

用户问题：{query}

证据认识论元数据：
{evidence_meta}

AI回答草稿：
{draft_answer}
"""


async def verify_answer(
    query: str,
    draft_answer: str,
    claim_metadata: list,
) -> dict:
    """Verify the draft answer against classified evidence.

    Returns {"passed": bool, "issues": [...], "rewritten_answer": str}
    """
    if not draft_answer.strip():
        return {"passed": True}

    # Format evidence metadata
    evidence_str = json.dumps(claim_metadata, ensure_ascii=False, indent=2)
    # Truncate if too long
    if len(evidence_str) > 4000:
        evidence_str = evidence_str[:4000] + "\n... (truncated)"

    prompt = VERIFY_PROMPT.format(
        query=query,
        evidence_meta=evidence_str,
        draft_answer=draft_answer,
    )

    try:
        result = await llm_model_func(
            prompt,
            system_prompt="你是事实核查专家。只输出JSON对象，不要输出其他内容。",
        )
        # Parse JSON object from result
        s, e = result.find("{"), result.rfind("}")
        if s >= 0 and e > s:
            return json.loads(result[s:e + 1])
    except Exception as e:
        print(f"[epistemic-verify] Error: {e}", flush=True)

    return {"passed": True}


# ── 3. Epistemic-aware system prompt enhancement ──

EPISTEMIC_SYSTEM_ADDENDUM = """

【认识论防护规则 — 最高优先级】
知识库中的文本只能视为"来源材料(source evidence)"，不能默认视为经过验证的客观事实。

声明类型处理规则：
1. VERIFIABLE_FACT（可核验事实）：可以直接陈述，但需标注来源
2. REPORTED_CLAIM（声称）：必须保留归属主体。格式："XX公司称/公布的..."
3. ESTIMATE/PREDICTION（估算/预测）：必须使用不确定性语言。"预计""有望""可能"
4. ANALYSIS（分析）：标注为分析推断，不作为事实陈述
5. OPINION（观点）：绝对不能作为事实陈述。格式："有观点认为""XX机构指出"
6. MARKETING_HYPE（宣传）：必须标注宣传性质，不得引用其中数据作为事实
7. UNCERTAIN（不确定）：不得引用

关键禁止项：
- 禁止将观点当作事实陈述（如"XX领域正处于商业化元年"应表述为"有产业研究认为XX领域进入商业化元年"）
- 禁止将某主体的声称当作独立验证的事实
- 禁止丢失来源归属
- 禁止从个例推广到普遍结论
"""


def build_epistemic_system_prompt(base_prompt: str, claim_metadata: list) -> str:
    """Enhance the base system prompt with epistemic rules and evidence metadata."""
    enhanced = base_prompt + EPISTEMIC_SYSTEM_ADDENDUM

    # Append claim metadata for the LLM to use
    if claim_metadata:
        meta_str = json.dumps(claim_metadata, ensure_ascii=False, indent=2)
        # Truncate if too long
        if len(meta_str) > 3000:
            meta_str = meta_str[:3000] + "\n... (truncated)"
        enhanced += f"\n\n【检索证据的认识论分类】\n{meta_str}\n"

    return enhanced


# ── 4. Citation excerpt optimization ──

def extract_relevant_excerpt(
    body: str,
    query: str,
    ai_summary: str = "",
    max_length: int = 200,
    window: int = 80,
) -> str:
    """Extract the most query-relevant portion of the text, not just from the beginning.

    Strategy:
    1. Find query keyword positions in the text
    2. Extract a window around the best match
    3. Fall back to AI summary or text beginning
    """
    if not body:
        return (ai_summary or "")[:max_length]

    # Tokenize query into keywords (Chinese: use characters; English: use words)
    keywords = set()
    # Chinese keywords (2+ consecutive chars)
    for m in re.finditer(r'[一-鿿]{2,}', query):
        keywords.add(m.group())
    # English keywords (3+ chars)
    for m in re.finditer(r'[a-zA-Z]{3,}', query):
        keywords.add(m.group().lower())

    if not keywords:
        return body[:max_length]

    # Find all keyword positions
    body_lower = body.lower()
    positions = []
    for kw in keywords:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = body_lower.find(kw_lower, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1

    if not positions:
        return body[:max_length]

    # Find the best cluster of matches (densest region)
    positions.sort()
    best_start = positions[0]
    best_density = 0
    for i, pos in enumerate(positions):
        # Count matches within window of this position
        nearby = sum(1 for p in positions if abs(p - pos) <= window)
        if nearby > best_density:
            best_density = nearby
            best_start = pos

    # Extract window around best cluster
    excerpt_start = max(0, best_start - 20)
    excerpt_end = min(len(body), excerpt_start + max_length)

    excerpt = body[excerpt_start:excerpt_end]

    # Add ellipsis if truncated
    if excerpt_start > 0:
        excerpt = "..." + excerpt
    if excerpt_end < len(body):
        excerpt = excerpt + "..."

    return excerpt
