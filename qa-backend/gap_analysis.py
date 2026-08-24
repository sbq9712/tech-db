"""
T023 — Gap Analysis + Targeted Query Generation
================================================
From Ledger + Grader, generate targeted queries for unfulfilled requirements.

"Search the original question again" usually just repeats results.
This module identifies what's specifically missing and generates
targeted queries to fill those gaps.

Gap types:
  MISSING_FACT, MISSING_ENTITY_COVERAGE, MISSING_TIME_PERIOD,
  MISSING_INDEPENDENT_SOURCE, CONFLICT_NEEDS_RESOLUTION,
  MISSING_NUMERIC_CONDITION, AMBIGUOUS_SCOPE
"""
import json
import os
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import List, Tuple
from config import llm_model_func


GAP_TYPES = {
    "MISSING_FACT", "MISSING_ENTITY_COVERAGE",
    "MISSING_OBJECT_DIMENSION", "MISSING_TIME_PERIOD",
    "MISSING_CURRENT_EVIDENCE", "MISSING_INDEPENDENT_SOURCE",
    "CONFLICT_NEEDS_RESOLUTION", "MISSING_NUMERIC_CONDITION",
    "MISSING_RELATION_METHOD", "AMBIGUOUS_SCOPE",
}


@dataclass(frozen=True)
class ResearchGap:
    gap_id: str
    gap_type: str
    requirement_id: str
    description: str
    resolvable: bool = True

    def __post_init__(self):
        if self.gap_type not in GAP_TYPES:
            raise ValueError(f"unknown canonical gap type {self.gap_type}")
        if not self.requirement_id:
            raise ValueError("gap must bind a requirement")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TargetedQuery:
    query_id: str
    query: str
    requirement_id: str
    gap_id: str
    gap_type: str
    round_number: int
    purpose: str
    normalized_query: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_query(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.findall(r"[\w\u3400-\u9fff]+", text))


def derive_gaps(ledger_status: dict) -> List[ResearchGap]:
    """Deterministically derive typed gaps from the per-requirement Ledger."""
    gaps = []
    for req in ledger_status.get("requirements", []):
        rid = str(req.get("id") or "")
        status = req.get("status")
        if status == "SUPPORTED":
            continue
        if status == "CONFLICTED":
            kind, desc = "CONFLICT_NEEDS_RESOLUTION", "unresolved evidence conflict"
        elif req.get("comparison_object") and req.get("comparison_dimension"):
            kind, desc = "MISSING_OBJECT_DIMENSION", (
                f"missing {req['comparison_object']} × {req['comparison_dimension']}")
        elif str(req.get("temporal_intent") or "") == "current" \
                or not req.get("temporal_coverage") and "current" in str(req.get("description", "")).lower():
            kind, desc = "MISSING_CURRENT_EVIDENCE", "missing current non-superseded evidence"
        elif req.get("relation_need") not in (None, "", "none"):
            kind, desc = "MISSING_RELATION_METHOD", "missing typed relation evidence"
        elif req.get("numeric_conditions"):
            kind, desc = "MISSING_NUMERIC_CONDITION", "missing scoped numeric evidence"
        elif req.get("independent_groups", 0) < 2 and \
                any("independent" in str(v).lower()
                    for v in req.get("missing_reasons", [])):
            kind, desc = "MISSING_INDEPENDENT_SOURCE", "missing independent provenance group"
        else:
            kind, desc = "MISSING_FACT", str(req.get("description") or "missing fact")
        digest = hashlib.sha256(f"{rid}\x1f{kind}\x1f{desc}".encode("utf-8")).hexdigest()[:12]
        gaps.append(ResearchGap(f"gap-{digest}", kind, rid, desc,
                                resolvable=not bool(req.get("impossible"))))
    return gaps


def targeted_queries(gaps: List[ResearchGap], requirements: dict, *,
                     original_query: str, round_number: int,
                     previous_queries: list) -> Tuple[List[TargetedQuery], List[str]]:
    """Build requirement-bound, anti-drift, semantically deduplicated queries."""
    previous = [normalize_query(q.query if isinstance(q, TargetedQuery)
                                else q.get("query", "") if isinstance(q, dict)
                                else str(q)) for q in previous_queries]
    out, rejected = [], []
    for gap in gaps:
        if not gap.resolvable:
            rejected.append(f"{gap.gap_id}:impossible")
            continue
        req = requirements.get(gap.requirement_id) or {}
        desc = str(req.get("description") or gap.description)
        suffix = {
            "MISSING_CURRENT_EVIDENCE": "latest current official evidence",
            "MISSING_INDEPENDENT_SOURCE": "independent verification",
            "CONFLICT_NEEDS_RESOLUTION": "conflicting values primary sources",
            "MISSING_NUMERIC_CONDITION": "exact value unit scope condition",
            "MISSING_RELATION_METHOD": "official typed relationship provenance",
            "AMBIGUOUS_SCOPE": "scope definition",
        }.get(gap.gap_type, gap.description)
        query = " ".join(f"{desc} {suffix}".split())
        normalized = normalize_query(query)
        # Anti-drift: generated query must retain at least one meaningful
        # token from its requirement or the original query.
        anchors = set(normalize_query(desc).split()) | set(
            normalize_query(original_query).split())
        if anchors and not anchors.intersection(normalized.split()):
            rejected.append(f"{gap.gap_id}:drift")
            continue
        duplicate = normalized in previous or any(
            __import__("difflib").SequenceMatcher(None, normalized, p).ratio() >= .86
            for p in previous if p)
        if duplicate:
            rejected.append(f"{gap.gap_id}:duplicate")
            continue
        qid = "query-" + hashlib.sha256(
            f"{gap.gap_id}\x1f{round_number}\x1f{normalized}".encode("utf-8")
        ).hexdigest()[:12]
        tq = TargetedQuery(qid, query, gap.requirement_id, gap.gap_id,
                           gap.gap_type, int(round_number),
                           "close_requirement_gap", normalized)
        out.append(tq)
        previous.append(normalized)
    return out, rejected


GAP_ANALYSIS_PROMPT = """你是研究分析专家。基于当前证据不足之处，生成针对性的补搜查询。

规则：
1. 每个新查询必须绑定一个未满足的gap
2. 避免重复已有查询
3. 如果gap在当前数据库中不可满足，建议停止
4. 查询类型包括：找事实、找独立验证、解决冲突、找特定时间段

只输出JSON：
{{
  "gaps": [
    {{
      "type": "MISSING_INDEPENDENT_SOURCE",
      "requirement_id": "r2",
      "description": "缺少第三方验证"
    }}
  ],
  "queries": [
    {{
      "query": "补搜查询",
      "requirement_id": "r2",
      "purpose": "find_independent_validation",
      "preferred_routes": ["vector", "bm25"],
      "source_need": "independent"
    }}
  ],
  "should_stop": false
}}

用户问题：{question}

当前证据状态：
{ledger_status}

缺失项：
{missing}

已有查询（避免重复）：
{previous_queries}"""


async def analyze_gaps(
    question: str,
    ledger_status: dict,
    grader_result: dict,
    previous_queries: list,
    previous_results: list = None,
) -> dict:
    """Analyze gaps and generate targeted queries.

    Returns:
        {
            "gaps": list,
            "queries": list,
            "should_stop": bool,
        }
    """
    missing = grader_result.get("missing", [])
    next_targets = grader_result.get("next_search_targets", [])

    # Quick check: if nothing missing, stop
    if not missing and not next_targets:
        return {"gaps": [], "queries": [], "should_stop": True}

    # Quick check: if we've been searching too long with no progress
    if len(previous_queries) >= 10:
        return {"gaps": [{"type": "MAX_QUERIES", "description": "query_limit_reached"}],
                "queries": [], "should_stop": True}

    try:
        prompt = GAP_ANALYSIS_PROMPT.format(
            question=question[:300],
            ledger_status=json.dumps(ledger_status.get("requirements", []),
                                     ensure_ascii=False)[:1000],
            missing=json.dumps(missing, ensure_ascii=False)[:500],
            previous_queries=json.dumps(previous_queries[-5:], ensure_ascii=False)[:500],
        )

        result_text = await llm_model_func(
            prompt,
            system_prompt="你是研究分析专家。只输出JSON。",
            temperature=0.0,
            max_tokens=1024,
            allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
        )

        return _parse_gap_result(result_text)

    except Exception as e:
        print(f"[gap_analysis] Error: {e}", flush=True)
        # Fallback: use next_search_targets from grader
        fallback_queries = []
        for target in next_targets[:3]:
            # Deduplicate against previous queries
            if target not in previous_queries:
                fallback_queries.append({
                    "query": target,
                    "requirement_id": "fallback",
                    "purpose": "fill_gap",
                    "preferred_routes": ["vector", "bm25"],
                    "source_need": "any",
                })

        return {
            "gaps": [{"type": "FALLBACK", "description": "gap_analysis_error"}],
            "queries": fallback_queries,
            "should_stop": len(fallback_queries) == 0,
        }


def _parse_gap_result(text: str) -> dict:
    """Parse gap analysis LLM output."""
    import re
    if not text:
        return {"gaps": [], "queries": [], "should_stop": True}

    try:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return {
                "gaps": data.get("gaps", []),
                "queries": data.get("queries", []),
                "should_stop": bool(data.get("should_stop", False)),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    return {"gaps": [], "queries": [], "should_stop": True}


def deduplicate_queries(new_queries: list, previous_queries: list,
                        similarity_threshold: float = 0.85) -> list:
    """Remove queries that are too similar to previous ones."""
    import difflib
    result = []
    for q in new_queries:
        query_text = q.get("query", "") if isinstance(q, dict) else q
        is_dup = False
        for prev in previous_queries:
            prev_text = prev.get("query", prev) if isinstance(prev, dict) else prev
            ratio = difflib.SequenceMatcher(None, query_text.lower(), prev_text.lower()).ratio()
            if ratio >= similarity_threshold:
                is_dup = True
                break
        if not is_dup:
            result.append(q)
    return result
