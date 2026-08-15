"""
Epistemic protection module — prevents the QA system from presenting
opinions, predictions, or unverified claims as established facts.

Pipeline insertion points:
  Retriever → Reranker → [Claim Classifier] → Answer Generator → [Answer Verifier] → Final Answer

The Claim Classifier analyzes retrieved chunks and tags their claims with epistemic types.
The Answer Verifier audits the draft answer against the classified evidence.
"""
import asyncio
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

# ── Source type taxonomy ──

SOURCE_TYPES = {
    "government":            "政府/监管机构发布",
    "academic_paper":        "学术论文/期刊",
    "company_announcement":  "公司公告/新闻稿",
    "company_financial_report": "公司财报",
    "broker_report":         "券商研报",
    "industry_report":       "行业报告/白皮书",
    "mainstream_media":      "主流媒体报道",
    "expert_interview":      "专家访谈/观点",
    "social_media":          "社交媒体/自媒体",
    "unknown":               "来源类型未知",
}

# ── Watchdog words: these default to OPINION/ANALYSIS unless source provides
#    a clear verifiable definition ──

WATCHDOG_WORDS = [
    "元年", "爆发期", "拐点", "成熟期", "全面商业化",
    "领先", "颠覆", "革命性", "新时代", "行业共识",
    "即将爆发", "蓝海", "风口", "里程碑", "突破性",
    "全球首个", "世界首创", "国内首个", "行业第一",
    "独一无二", "史无前例", "跨时代", "重塑",
]

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


def _parse_llm_json(text: str, expect: str = "any"):
    """Lenient JSON extraction for LLM output.

    Handles the common GLM failure modes observed live:
    - markdown fences / prose around the JSON
    - truncated arrays (retry by closing at the last complete element)
    - truncated objects (drop instead of crashing)

    expect: "array" | "object" | "any" — the requested top-level shape.
    Codex-review fix (P2): a verifier response like {"passed": false,
    "issues": [...]} must parse as the OBJECT first; array-first extraction
    silently returned the inner `issues` list and converted an explicit
    verification FAILURE into the default pass.
    Returns parsed value or None.
    """
    def _try_array():
        s = text.find("[")
        if s < 0:
            return None
        e = text.rfind("]")
        frag = text[s:e + 1] if e > s else text[s:]
        try:
            parsed = json.loads(frag)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            # truncated array: close at the last complete element
            last_obj = frag.rfind("}")
            if last_obj > 0:
                try:
                    repaired = json.loads(frag[:last_obj + 1] + "]")
                    if isinstance(repaired, list):
                        return repaired
                except Exception:
                    pass
            # numeric/primitive arrays: close at the last comma
            last_comma = frag.rfind(",")
            if last_comma > 0:
                try:
                    repaired = json.loads(frag[:last_comma] + "]")
                    if isinstance(repaired, list):
                        return repaired
                except Exception:
                    pass
        return None

    def _try_object():
        s = text.find("{")
        if s < 0:
            return None
        e = text.rfind("}")
        if e > s:
            try:
                parsed = json.loads(text[s:e + 1])
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                # truncated object: close at last complete "key": value
                for closer in ('"}', ']', '}', 'null', 'true', 'false'):
                    cut = text[s:].rfind(closer)
                    if cut > 0:
                        try:
                            repaired = json.loads(text[s:s + cut + len(closer)] + "}")
                            if isinstance(repaired, dict):
                                return repaired
                        except Exception:
                            continue
        return None

    if expect == "object":
        obj = _try_object()
        return obj if obj is not None else _try_array()
    arr = _try_array()
    return arr if arr is not None else _try_object()


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
[{{"chunk_id":"1","claims":[{{"claim":"声明内容","type":"OPINION","speaker":"XX证券","must_attribute":true,"confidence":0.92,"evidence_span":"原文片段"}}]}}]

用户问题：{query}

待分析片段：
{chunks}
"""



def _get_chunk_text(search_result: dict, records: list) -> str:
    """Extract text from a search result by looking up the full record."""
    meta = search_result.get("meta", {})
    orig_idx = meta.get("idx", -1)
    record = records[orig_idx] if 0 <= orig_idx < len(records) else None
    if record:
        return record.get("as", "") or record.get("b", "") or ""
    return ""


async def classify_claims(query: str, search_results: list, top_k: int = 5) -> list:
    """Classify epistemic claims for the top-k search results.

    Returns list of {"chunk_id", "claims": [...]} dicts.
    """
    if not search_results:
        return []

    # Only classify top-k chunks (cost control)
    to_classify = search_results[:top_k]

    # Access meta dict with abbreviated keys (search_results from rrf_fuse)
    from server import load_records
    _records = load_records()
    chunks_json = json.dumps([
        {
            "chunk_id": str(i + 1),
            "title": r.get("meta", {}).get("t", ""),
            "source": r.get("meta", {}).get("s", ""),
            "date": r.get("meta", {}).get("d", ""),
            "text": _get_chunk_text(r, _records)[:500],
        }
        for i, r in enumerate(to_classify)
    ], ensure_ascii=False)

    prompt = CLASSIFY_PROMPT.format(query=query, chunks=chunks_json)

    try:
        result = await llm_model_func(
            prompt,
            system_prompt="你是认识论信息分类专家。只输出JSON数组，不要输出其他内容。",
            max_tokens=8192,  # GLM-5.2 reasoning: low caps leave content empty
            allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
        )
        # Parse JSON array from result (lenient — handles truncation)
        parsed = _parse_llm_json(result, expect="array")
        if isinstance(parsed, list):
            return parsed
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

如果不存在 high severity 问题，返回 {{"passed": true}}。
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
            max_tokens=8192,
            allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
        )
        # Parse JSON object from result (lenient) — object-first so a
        # {"passed": false, "issues": [...]} reply isn't swallowed as the
        # inner issues[] array (codex review P2)
        parsed = _parse_llm_json(result, expect="object")
        if isinstance(parsed, dict):
            return parsed
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
- 一个来源的观点不能升级为"行业普遍认为"
- 不同来源冲突时需要显式呈现冲突
- 证据不足时降低确定性，不允许为了完整回答而补足不存在的事实
- 如果结论是模型基于多条材料综合推导的，使用"综合现有信息来看""基于上述信息判断"等措辞

【高警惕词 — 除非来源提供明确可验证定义，否则默认视为评价/分析性语言】
"元年""爆发期""拐点""成熟期""全面商业化""领先""颠覆""革命性"
"新时代""行业共识""即将爆发""蓝海""风口""里程碑""突破性"
"全球首个""世界首创""国内首个""行业第一""独一无二""史无前例""跨时代""重塑"
遇到这些词时，必须：
1. 追溯到具体来源（谁说的）
2. 使用归属性措辞（"XX机构将当前阶段描述为..."）
3. 不得直接陈述为客观事实
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


# ── 5. Source type inference ──

def infer_source_type(record: dict) -> str:
    """Infer source_type from record fields (tag, source, category, etc.).

    Mapping:
      研究论文/Doi/Science/Nature → academic_paper
      产业进展/公司名 → company_announcement
      观点评论/行业观察 → industry_report
      政策监管 → government
      资本运作 → broker_report
    """
    _tg = record.get("tg") or ""
    tag = (_tg[0] if isinstance(_tg, list) and _tg else _tg).strip() if isinstance(_tg, (str, list)) else ""
    source = (record.get("a") or record.get("s") or "").strip()
    category = (record.get("c") or "").strip()

    # Academic papers
    if tag == "研究论文":
        return "academic_paper"
    source_lower = source.lower()
    if any(k in source_lower for k in ["doi", "science", "nature", "wiley", "cell", "arxiv", "springer", "elsevier"]):
        return "academic_paper"

    # Government / policy
    if tag == "政策监管" or "政府" in source or "ministry" in source_lower or "政策" in category:
        return "government"

    # Capital / financial
    if tag == "资本运作":
        return "broker_report"

    # Company announcements (industry progress from companies)
    if tag == "产业进展" or tag == "技术突破":
        return "company_announcement"

    # Industry reports (observations, analysis)
    if tag in ("观点评论", "行业观察"):
        return "industry_report"

    # Mainstream media
    if any(k in source_lower for k in ["bbc", "reuters", "bloomberg", "新华", "央视", "人民", "ithome", "澎湃"]):
        return "mainstream_media"

    return "unknown"


def build_source_metadata(record: dict) -> dict:
    """Build source metadata for a record (for citation enrichment)."""
    return {
        "title": record.get("t", ""),
        "source": record.get("a", record.get("s", "")),
        "source_type": infer_source_type(record),
        "author": "",
        "published_at": record.get("d", ""),
        "url": record.get("u", ""),
    }


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

    # Find the best cluster of matches (densest region) — O(n log n) with bisect
    import bisect
    positions.sort()
    best_start = positions[0]
    best_density = 0
    for pos in positions:
        # Count matches within window using binary search
        lo = bisect.bisect_left(positions, pos - window)
        hi = bisect.bisect_right(positions, pos + window)
        nearby = hi - lo
        if nearby > best_density:
            best_density = nearby
            best_start = pos

    # Extract window around best cluster — account for ellipsis length
    prefix = "..." if best_start > 20 else ""
    suffix_len = 3 if best_start + max_length < len(body) else 0
    effective_max = max_length - len(prefix) - suffix_len
    excerpt_start = max(0, best_start - 20)
    excerpt_end = min(len(body), excerpt_start + max(1, effective_max))

    excerpt = body[excerpt_start:excerpt_end]
    if prefix:
        excerpt = prefix + excerpt
    if suffix_len > 0:
        excerpt = excerpt + "..."

    return excerpt
