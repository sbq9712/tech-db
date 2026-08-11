"""
T004 — Atomic Claim + Claim-level Citation Mapping
===================================================
Splits the generated answer into atomic claims, classifies each claim's
type, and maps each claim to the citations that support it.

Claim Classification:
  MAJOR_FACT          — Key factual statement that requires citation
  NUMERIC_FACT        — Numbers, statistics, specifications
  COMPARISON          — A > B, A is better/worse than B
  CAUSAL              — A causes/enables/leads to B
  ATTRIBUTED_CLAIM    — Someone claims/states X
  MINOR_EXPLANATION   — Connective text, transitions, general context
                        (does NOT require citation)

Support Relations:
  DIRECT_SUPPORT      — Citation directly contains and confirms the claim
  PREMISE_SUPPORT     — Citation provides a premise from which claim follows
  ATTRIBUTION         — Citation is the source of an attributed claim
  CONTRADICTS         — Citation contradicts the claim
  BACKGROUND          — Citation provides relevant context but not direct support

Rules:
  - Major claims without support are UNSUPPORTED → must be deleted/weakened/re-retrieved
  - Every major claim must be mapped to at least one citation
"""
import json
import os
import re
from typing import Optional

from config import llm_model_func


# ── Claim types ──

CLAIM_TYPES = {
    "MAJOR_FACT": "关键事实 — 需要引用支持",
    "NUMERIC_FACT": "数字事实 — 参数、统计数据、规格",
    "COMPARISON": "比较 — A优于/劣于B",
    "CAUSAL": "因果 — A导致/促进/阻碍B",
    "ATTRIBUTED_CLAIM": "归属声明 — 某主体声称/公布的信息",
    "MINOR_EXPLANATION": "解释性文字 — 连接、过渡、背景（不要求引用）",
}

# Claims that REQUIRE citation support
MAJOR_CLAIM_TYPES = {"MAJOR_FACT", "NUMERIC_FACT", "COMPARISON", "CAUSAL", "ATTRIBUTED_CLAIM"}

# ── Support relations ──

SUPPORT_RELATIONS = {
    "DIRECT_SUPPORT": "来源直接包含并确认该声明",
    "PREMISE_SUPPORT": "来源提供前提，由此可推出声明",
    "ATTRIBUTION": "来源是归属声明的出处",
    "CONTRADICTS": "来源与声明矛盾",
    "BACKGROUND": "来源提供相关背景但不直接支持",
}

# ── Claim status ──

CLAIM_SUPPORTED = "SUPPORTED"
CLAIM_PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
CLAIM_UNSUPPORTED = "UNSUPPORTED"


CLAIM_MAPPING_PROMPT = """你是技术情报分析专家。将以下AI回答分解为原子声明，并为每个声明匹配引用来源。

声明分类：
- MAJOR_FACT: 关键事实陈述（需要引用）
- NUMERIC_FACT: 数字/参数/统计数据（需要引用）
- COMPARISON: 比较（A优于/劣于B）（需要引用）
- CAUSAL: 因果关系（A导致B）（需要引用）
- ATTRIBUTED_CLAIM: 归属声明（某主体声称/公布）（需要引用）
- MINOR_EXPLANATION: 解释性文字（连接、过渡，不需要引用）

支持关系：
- DIRECT_SUPPORT: 来源直接包含并确认该声明
- PREMISE_SUPPORT: 来源提供前提，由此可推出声明
- ATTRIBUTION: 来源是归属声明的出处
- CONTRADICTS: 来源与声明矛盾
- BACKGROUND: 来源提供相关背景但不直接支持

规则：
1. 每个主要声明(MAJOR_*)必须至少有一个支持引用
2. 如果没有引用支持某主要声明，标注为 UNSUPPORTED
3. citation_id 对应来源列表中的序号
4. evidence_span 必须来自该来源的原文，不得编造

只输出JSON：
{{
  "claims": [
    {{
      "id": "claim_1",
      "text": "声明原文（从回答中提取的原文）",
      "type": "MAJOR_FACT",
      "support_status": "SUPPORTED",
      "supported_by": [
        {{
          "citation_id": 1,
          "relation": "DIRECT_SUPPORT",
          "evidence_span": "该来源中支持此声明的原文片段"
        }}
      ]
    }}
  ]
}}

用户问题：{query}

来源列表：
{source_list}

AI回答：
{answer}"""


async def map_claims_to_citations(
    query: str,
    answer: str,
    citations: list,
) -> dict:
    """Decompose answer into atomic claims and map each to supporting citations.

    Args:
        query: Original user question
        answer: Generated answer text
        citations: List of citation dicts (from build_context)

    Returns:
        {
            "claims": [
                {
                    "id": "claim_1",
                    "text": "...",
                    "type": "MAJOR_FACT",
                    "support_status": "SUPPORTED" | "UNSUPPORTED" | ...,
                    "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT", ...}],
                }
            ]
        }
    """
    if not answer or not answer.strip():
        return {"claims": []}

    if not citations:
        # No citations to map — all claims are unsupported
        return {"claims": []}

    # Build source list for prompt
    source_list = "\n".join(
        f"[{c['id']}] {c.get('title', '')} ({c.get('date', '')}, {c.get('source', '')})"
        for c in citations
    )

    prompt = CLAIM_MAPPING_PROMPT.format(
        query=query[:500],
        source_list=source_list,
        answer=answer[:4000],
    )

    try:
        result_text = await llm_model_func(
            prompt,
            system_prompt="你是技术情报分析专家。只输出JSON，不要输出其他内容。",
            temperature=0.0,
            max_tokens=4096,
        )

        parsed = _extract_json_safe(result_text)
        if parsed and "claims" in parsed:
            # Validate and clean claims
            claims = []
            for claim in parsed["claims"]:
                c = _validate_claim(claim, citations)
                if c:
                    claims.append(c)
            return {"claims": claims}

    except Exception as e:
        print(f"[claim_mapping] Error: {e}", flush=True)

    return {"claims": []}


def _extract_json_safe(text: str) -> Optional[dict]:
    """Robust JSON extraction with multiple fallback strategies."""
    if not text or not text.strip():
        return None

    try:
        return json.loads(text.strip())
    except Exception:
        pass

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        return json.loads(stripped)
    except Exception:
        pass

    m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            # Try repair
            candidate = m.group(0)
            candidate = re.sub(r",\s*}", "}", candidate)
            candidate = re.sub(r",\s*]", "]", candidate)
            candidate = re.sub(r"[\x00-\x1f]", "", candidate)
            try:
                return json.loads(candidate)
            except Exception:
                pass

    return None


def _validate_claim(claim: dict, citations: list) -> Optional[dict]:
    """Validate and normalize a claim dict from LLM output."""
    if not isinstance(claim, dict):
        return None

    text = claim.get("text", "").strip()
    if not text:
        return None

    claim_type = claim.get("type", "MINOR_EXPLANATION")
    if claim_type not in CLAIM_TYPES:
        claim_type = "MINOR_EXPLANATION"

    support_status = claim.get("support_status", CLAIM_UNSUPPORTED)
    supported_by_raw = claim.get("supported_by", [])

    # Validate citation references
    valid_citation_ids = {c["id"] for c in citations}
    supported_by = []
    for ref in supported_by_raw:
        if not isinstance(ref, dict):
            continue
        cid = ref.get("citation_id")
        if cid in valid_citation_ids:
            relation = ref.get("relation", "BACKGROUND")
            if relation not in SUPPORT_RELATIONS:
                relation = "BACKGROUND"
            supported_by.append({
                "citation_id": cid,
                "relation": relation,
                "evidence_span": ref.get("evidence_span", ""),
            })

    # Determine final support status
    is_major = claim_type in MAJOR_CLAIM_TYPES
    if not is_major:
        support_status = "MINOR"  # minor claims don't need support
    elif supported_by:
        has_direct = any(r["relation"] in ("DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION")
                         for r in supported_by)
        has_contradicts = any(r["relation"] == "CONTRADICTS" for r in supported_by)
        if has_direct and not has_contradicts:
            support_status = CLAIM_SUPPORTED
        elif has_direct and has_contradicts:
            support_status = CLAIM_PARTIALLY_SUPPORTED
        elif has_contradicts:
            support_status = CLAIM_UNSUPPORTED
        else:
            support_status = CLAIM_PARTIALLY_SUPPORTED
    else:
        support_status = CLAIM_UNSUPPORTED

    return {
        "id": claim.get("id", f"claim_{hash(text) % 10000}"),
        "text": text,
        "type": claim_type,
        "support_status": support_status,
        "supported_by": supported_by,
    }


def get_unsupported_major_claims(claims_mapping: dict) -> list:
    """Get all major claims that are UNSUPPORTED (must be deleted/weakened/re-retrieved).

    Returns list of claim dicts with support_status == CLAIM_UNSUPPORTED
    and type in MAJOR_CLAIM_TYPES.
    """
    claims = claims_mapping.get("claims", [])
    return [
        c for c in claims
        if c.get("support_status") == CLAIM_UNSUPPORTED
        and c.get("type") in MAJOR_CLAIM_TYPES
    ]


def get_claim_citation_map(claims_mapping: dict) -> dict:
    """Get a mapping from claim_id → list of citation_ids that support it.

    Returns:
        {"claim_1": [1, 3], "claim_2": [2], ...}
    """
    result = {}
    for claim in claims_mapping.get("claims", []):
        cid = claim.get("id", "")
        supported_by = claim.get("supported_by", [])
        citation_ids = [ref["citation_id"] for ref in supported_by
                        if ref.get("relation") != "CONTRADICTS"]
        if cid:
            result[cid] = citation_ids
    return result
