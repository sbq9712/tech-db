"""
T009 — Claim-aware Source Suitability
======================================
Judge how suitable a source is for a specific claim, not an absolute
"trustworthiness score" for the source domain.

Key principle: "nvidia.com = 0.95" creates bias and false precision.
The same source has different evidentiary value for different claims.

Example:
  Company official spec page claiming "bandwidth = 1.8TB/s":
    → High suitability for "company claims X bandwidth"
    → Low suitability for "X is actually faster than competitors"

Output:
  source_suitability: float (0-1, used as feature not truth)
  evidence_role: primary / secondary / unknown
  attribution_required: bool
  independence_penalty: float
  reason: str
"""
import os
from typing import Optional
from epistemic import infer_source_type


# Extended source types for T009 (aligned with T008 spec)
EXTENDED_SOURCE_TYPES = {
    "standards_body": "标准化组织",
    "regulator": "监管机构",
    "government": "政府机构",
    "academic_primary": "学术研究（一手）",
    "academic_secondary": "学术综述（二手）",
    "company_technical_doc": "公司技术文档",
    "company_press_release": "公司新闻稿",
    "company_financial_report": "公司财报",
    "research_institution": "研究机构",
    "major_media": "主流媒体",
    "industry_media": "行业媒体",
    "secondary_repost": "二手转载",
    "social_media": "社交媒体/自媒体",
    "unknown": "未知来源",
}

# Source type → default evidence role
SOURCE_EVIDENCE_ROLE = {
    "standards_body": "primary",
    "regulator": "primary",
    "government": "primary",
    "academic_primary": "primary",
    "academic_secondary": "secondary",
    "company_technical_doc": "primary",
    "company_press_release": "primary",  # self-reported primary
    "company_financial_report": "primary",  # self-reported primary
    "research_institution": "primary",
    "major_media": "secondary",
    "industry_media": "secondary",
    "secondary_repost": "secondary",
    "social_media": "secondary",
    "unknown": "unknown",
}

# Claim types (will be expanded in future)
CLAIM_TYPES = [
    "product_spec", "company_action", "performance_claim", "scientific_result",
    "market_size", "policy", "prediction", "opinion", "historical_event",
    "comparison", "causal", "definition", "numeric_fact",
]


def assess_source_suitability(
    record: dict,
    claim_type: str = "",
    claim_text: str = "",
    is_self_reported: bool = False,
) -> dict:
    """Assess how suitable a source is for a given claim.

    Returns:
        {
            "source_suitability": float (0-1),
            "evidence_role": str,
            "attribution_required": bool,
            "independence_penalty": float,
            "reason": str,
        }
    """
    source_type = infer_source_type(record)
    evidence_role = SOURCE_EVIDENCE_ROLE.get(source_type, "unknown")
    is_company_source = source_type in (
        "company_announcement", "company_technical_doc",
        "company_press_release", "company_financial_report"
    )

    # Default suitability
    suitability = 0.5
    attribution_required = False
    independence_penalty = 0.0
    reasons = []

    # ── Adjust based on claim type × source type ──

    if claim_type in ("product_spec", "numeric_fact"):
        if is_company_source:
            suitability = 0.8  # Company specs are good for their own products
            reasons.append("公司文档适合规格查询")
        elif source_type in ("academic_primary", "standards_body"):
            suitability = 0.9
            reasons.append("学术/标准来源适合规格验证")
        elif source_type in ("major_media", "industry_media"):
            suitability = 0.6
            reasons.append("媒体报道规格可能不够精确")

    elif claim_type in ("performance_claim", "scientific_result"):
        if source_type in ("academic_primary", "academic_paper", "research_institution"):
            suitability = 0.9
            reasons.append("学术来源适合性能/科学声明")
        elif is_company_source:
            suitability = 0.4  # Company performance claims need independent verification
            attribution_required = True
            independence_penalty = 0.3
            reasons.append("公司性能声明需要独立验证")
        elif source_type in ("major_media", "industry_media"):
            suitability = 0.6

    elif claim_type == "prediction":
        attribution_required = True
        independence_penalty = 0.2
        if is_company_source:
            suitability = 0.5
            reasons.append("公司预测需要归属")
        elif source_type in ("research_institution", "academic_primary"):
            suitability = 0.7
            reasons.append("研究机构预测较可信")
        else:
            suitability = 0.4

    elif claim_type == "opinion":
        attribution_required = True
        suitability = 0.3
        reasons.append("观点需要归属且低证据力")

    elif claim_type in ("company_action",):
        if is_company_source:
            suitability = 0.8
            reasons.append("公司自身行为适合公司来源")
        elif source_type in ("major_media", "industry_media"):
            suitability = 0.7
            reasons.append("媒体报道公司行为")
        attribution_required = not is_company_source

    elif claim_type == "policy":
        if source_type in ("government", "regulator"):
            suitability = 0.9
            reasons.append("政府/监管来源适合政策")
        elif source_type in ("major_media", "industry_media"):
            suitability = 0.6

    elif claim_type == "comparison":
        if source_type in ("academic_primary", "research_institution"):
            suitability = 0.8
        elif is_company_source:
            suitability = 0.3  # Company comparisons are biased
            independence_penalty = 0.4
            attribution_required = True
            reasons.append("公司比较声明有偏见")
        else:
            suitability = 0.5

    elif claim_type == "market_size":
        if source_type in ("research_institution", "industry_report"):
            suitability = 0.7
        elif is_company_source:
            suitability = 0.4
            attribution_required = True

    else:
        # Default: moderate suitability
        suitability = 0.5
        if source_type in ("academic_primary", "standards_body", "government"):
            suitability = 0.7
        elif source_type == "secondary_repost":
            suitability = 0.3
            independence_penalty = 0.2

    # Apply self-reported penalty
    if is_self_reported:
        independence_penalty = max(independence_penalty, 0.3)
        attribution_required = True
        if claim_type in ("performance_claim", "comparison", "market_size"):
            suitability *= 0.6  # Significant penalty for self-reported comparative claims
            reasons.append("自述来源降权")

    # Clamp
    suitability = max(0.1, min(1.0, suitability))

    return {
        "source_suitability": round(suitability, 2),
        "evidence_role": evidence_role,
        "attribution_required": attribution_required,
        "independence_penalty": round(independence_penalty, 2),
        "reason": "; ".join(reasons) if reasons else "default_assessment",
        "source_type": source_type,
    }


# ── Policy versioning (for audit) ──
SUITABILITY_POLICY_VERSION = "0.1.0"
SUITABILITY_POLICY_DESCRIPTION = """
Claim-aware source suitability assessment v0.1.
Rules:
  - Source type is inferred from record fields
  - Same source has different suitability for different claim types
  - Company sources: high for specs/actions, low for comparisons/performance
  - Self-reported claims receive independence penalty
  - All scores are features, not truth
  - No absolute trustworthiness scores shown to users
"""
