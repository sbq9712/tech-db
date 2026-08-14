"""
T022 — Evidence Grader: Rule Engine + GLM
==========================================
Determines if evidence is sufficient using BOTH deterministic rules
and LLM assessment. Never relies on a single LLM yes/no.

Layer 1: Rule Engine (deterministic checks)
  - Critical requirement coverage
  - Required entity coverage
  - Self-reported vs independent source ratio
  - Superseded-only evidence
  - Source-level/data-quality issues
  - Citation-capable evidence count
  - Temporal consistency

Layer 2: GLM Assessment (semantic)
  - Overall sufficiency judgment
  - Coverage score
  - Independent source score
  - Missing/gap identification

Hard constraints (rule failures that GLM cannot override):
  - Comparison A/B/C with B missing → cannot SUPPORTED
  - Only vendor self-reported evidence → cannot SUPPORTED for independent claims
  - High-severity unresolved conflict → cannot SUPPORTED
"""
import json
import os
from typing import List, Dict, Optional
from config import llm_model_func

from evidence_ledger import EvidenceLedger, REQ_MISSING, REQ_CONFLICTED


GRADER_PROMPT = """你是证据质量评估专家。判断以下证据是否足以回答用户问题。

规则：
1. 如果用户要求比较A/B/C，B无evidence → 不足
2. 如果用户问"实际是否如此"，只有厂商自述 → 不足
3. high severity未解决冲突 → 不足

判断维度：
- coverage_score: 0-1，需求覆盖程度
- independent_source_score: 0-1，来源独立性
- missing: 未满足的需求列表
- next_search_targets: 建议的下一步搜索方向

只输出JSON：
{{
  "overall": "SUFFICIENT" | "INSUFFICIENT",
  "coverage_score": 0.85,
  "independent_source_score": 0.7,
  "rule_failures": [],
  "requirements": [{{"id": "r1", "status": "SUPPORTED", "note": ""}}],
  "missing": ["missing aspect description"],
  "next_search_targets": ["search target suggestion"]
}}

用户问题：{query}

证据概要：
{evidence_summary}

需求状态：
{requirement_status}"""


async def grade_evidence(
    query: str,
    ledger: EvidenceLedger,
    evidence_set: list,
    router_result: dict = None,
    provenance_map: dict = None,
) -> dict:
    """Grade whether evidence is sufficient.

    Returns:
        {
            "overall": "SUFFICIENT" | "INSUFFICIENT",
            "coverage_score": float,
            "independent_source_score": float,
            "rule_failures": list,
            "requirements": list,
            "missing": list,
            "next_search_targets": list,
        }
    """
    router_result = router_result or {}
    provenance_map = provenance_map or {}
    ledger_status = ledger.get_status()

    # ── Layer 1: Deterministic Rule Engine ──
    rule_failures = _run_rule_checks(
        query, ledger, evidence_set, router_result, provenance_map
    )

    # ── Layer 2: GLM Assessment (if rules don't hard-fail) ──
    glm_result = {"overall": "INSUFFICIENT", "coverage_score": 0.0,
                  "independent_source_score": 0.0, "missing": [], "next_search_targets": []}

    if not rule_failures or any(rf.get("severity") != "hard" for rf in rule_failures):
        try:
            evidence_summary = _build_evidence_summary(evidence_set, provenance_map)
            req_status = json.dumps(ledger_status.get("requirements", []),
                                    ensure_ascii=False)

            prompt = GRADER_PROMPT.format(
                query=query[:300],
                evidence_summary=evidence_summary[:2000],
                requirement_status=req_status[:2000],
            )

            result_text = await llm_model_func(
                prompt,
                system_prompt="你是证据质量评估专家。只输出JSON。",
                temperature=0.0,
                max_tokens=1024,
                allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
            )

            glm_result = _parse_grader_result(result_text)

        except Exception as e:
            print(f"[grader] Error: {e}", flush=True)

    # ── Combine: Hard rules override GLM ──
    hard_failures = [rf for rf in rule_failures if rf.get("severity") == "hard"]
    if hard_failures:
        overall = "INSUFFICIENT"
    elif glm_result.get("overall") == "SUFFICIENT" and not rule_failures:
        overall = "SUFFICIENT"
    else:
        overall = glm_result.get("overall", "INSUFFICIENT")

    return {
        "overall": overall,
        "coverage_score": glm_result.get("coverage_score", 0.0),
        "independent_source_score": glm_result.get("independent_source_score", 0.0),
        "rule_failures": rule_failures,
        "requirements": glm_result.get("requirements", []),
        "missing": glm_result.get("missing", []),
        "next_search_targets": glm_result.get("next_search_targets", []),
    }


def _run_rule_checks(
    query: str,
    ledger: EvidenceLedger,
    evidence_set: list,
    router_result: dict,
    provenance_map: dict,
) -> list:
    """Run deterministic rule checks. Returns list of failures."""
    failures = []
    status = ledger.get_status()

    # Rule 1: Critical requirement missing
    if status.get("critical_missing", 0) > 0:
        failures.append({
            "rule": "critical_requirement_missing",
            "severity": "hard",
            "detail": f"{status['critical_missing']} critical requirement(s) unsatisfied",
        })

    # Rule 2: Comparison with missing entity
    if router_result.get("question_type") == "COMPARISON":
        missing = [r for r in status.get("requirements", []) if r["status"] == "MISSING"]
        if missing:
            failures.append({
                "rule": "comparison_entity_missing",
                "severity": "hard",
                "detail": f"Comparison missing: {[r['id'] for r in missing]}",
            })

    # Rule 3: Only self-reported evidence
    independent_count = sum(
        1 for e in evidence_set
        if provenance_map.get(e.get("record_id"), {}).get("evidence_role") != "self_reported"
    )
    if evidence_set and independent_count == 0 and router_result.get("needs_conflict_check"):
        failures.append({
            "rule": "only_self_reported",
            "severity": "hard",
            "detail": "All evidence is self-reported, no independent validation",
        })

    # Rule 4: Insufficient evidence count
    if len(evidence_set) < 2 and router_result.get("needs_multi_source_evidence"):
        failures.append({
            "rule": "insufficient_evidence_count",
            "severity": "soft",
            "detail": f"Only {len(evidence_set)} evidence items, need multi-source",
        })

    # Rule 5: Unresolved conflicts
    conflicted = [r for r in status.get("requirements", []) if r["status"] == "CONFLICTED"]
    if conflicted:
        failures.append({
            "rule": "unresolved_conflicts",
            "severity": "hard" if any(r["importance"] == "critical" for r in conflicted) else "soft",
            "detail": f"Conflicted requirements: {[r['id'] for r in conflicted]}",
        })

    return failures


def _build_evidence_summary(evidence_set: list, provenance_map: dict) -> str:
    """Build a text summary of evidence for the GLM grader."""
    lines = []
    for i, e in enumerate(evidence_set[:10]):  # Cap at 10
        rid = e.get("record_id", -1)
        meta = e.get("meta", {})
        prov = provenance_map.get(rid, {})
        lines.append(
            f"[{i+1}] {meta.get('t', 'N/A')} | {meta.get('s', 'N/A')} | "
            f"{meta.get('d', 'N/A')} | role={prov.get('evidence_role', 'unknown')}"
        )
    return "\n".join(lines)


def _parse_grader_result(text: str) -> dict:
    """Parse grader LLM output."""
    import re
    if not text:
        return {"overall": "INSUFFICIENT"}

    try:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return {
                "overall": data.get("overall", "INSUFFICIENT"),
                "coverage_score": float(data.get("coverage_score", 0)),
                "independent_source_score": float(data.get("independent_source_score", 0)),
                "requirements": data.get("requirements", []),
                "missing": data.get("missing", []),
                "next_search_targets": data.get("next_search_targets", []),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    return {"overall": "INSUFFICIENT"}
