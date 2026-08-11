"""
T031 — Evidence-aware Context Assembly
=======================================
Organizes the final Generator input as an "Evidence Package",
not a "Search Results Dump".

Structure:
  Question
  Research assumptions/scope
  Requirement R1
    Supporting Evidence
    Conflicting Evidence
    Provenance/Source Role
    Temporal metadata
    Exact/Relevant Spans
  Requirement R2
  ...

Rules:
  1. Group by requirement
  2. Compress same-source duplicates
  3. Conflicts must be preserved
  4. Source role clear (self_reported / independent)
  5. Prefer exact/relevant original spans
  6. Token budget by evidence value, not rank
  7. Synthetic summary not as evidence
  8. Untrusted text in data boundary
  9. MISSING requirements clearly marked
"""
import os
import json
from typing import List, Dict, Optional

from content_safety import wrap_retrieved_content, augment_system_prompt
from citation_grounding import get_original_text, get_text_source


MAX_CONTEXT_TOKENS = int(os.environ.get("QA_MAX_CONTEXT_TOKENS", "8000"))


def build_evidence_package(
    query: str,
    requirements: list,
    evidence_set: list,
    ledger_status: dict,
    records: list,
    provenance_map: dict = None,
    temporal_map: dict = None,
    conflict_result: dict = None,
    get_record_fn=None,
) -> str:
    """Build the evidence-aware context package for the Generator.

    Args:
        query: User question
        requirements: From decomposer
        evidence_set: Selected evidence items
        ledger_status: Current ledger state
        records: Full record list
        provenance_map: {record_id: provenance_info}
        temporal_map: {record_id: temporal_info}
        conflict_result: From conflict detector
        get_record_fn: Function to get record by id

    Returns:
        Formatted context string for LLM prompt
    """
    provenance_map = provenance_map or {}
    temporal_map = temporal_map or {}
    conflict_result = conflict_result or {}
    conflicts = conflict_result.get("conflicts", [])

    sections = []

    # ── Section 1: Question & Scope ──
    sections.append(f"【用户问题】\n{query}\n")

    # Add research scope
    req_descriptions = [r.get("description", r.get("id", "")) for r in requirements]
    if req_descriptions:
        sections.append("【研究范围】")
        for desc in req_descriptions:
            sections.append(f"  • {desc}")
        sections.append("")

    # ── Section 2: Evidence by Requirement ──
    sections.append("【证据资料】")

    token_budget = MAX_CONTEXT_TOKENS
    used_tokens = 0

    for req in requirements:
        rid = req.get("id", "r1")
        desc = req.get("description", "")
        status = "MISSING"
        for ls in ledger_status.get("requirements", []):
            if ls.get("id") == rid:
                status = ls.get("status", "MISSING")
                break

        if status == "MISSING":
            sections.append(f"\n--- 需求 [{rid}]: {desc} ---")
            sections.append("  ⚠️ 缺失证据。请明确告知用户此部分信息不足。")
            sections.append("  不要用模型预训练知识补全。")
            continue

        # Find evidence for this requirement
        req_evidence = [e for e in evidence_set
                        if e.get("requirement_id") == rid or rid == "r1"]

        if not req_evidence:
            req_evidence = evidence_set[:5]  # Fallback: use top evidence

        sections.append(f"\n--- 需求 [{rid}]: {desc} ---")

        for i, evidence in enumerate(req_evidence[:5]):  # Max 5 per requirement
            if used_tokens >= token_budget:
                sections.append("  ...(token预算已用完)...")
                break

            record_id = evidence.get("record_id", -1)
            record = None
            if get_record_fn:
                try:
                    record = get_record_fn(record_id)
                except Exception:
                    pass
            if not record and 0 <= record_id < len(records):
                record = records[record_id]

            if not record:
                continue

            title = record.get("t", "N/A")
            source = record.get("a", record.get("s", "N/A"))
            date = record.get("d", "N/A")

            # Get provenance info
            prov = provenance_map.get(record_id, {})
            source_role = prov.get("evidence_role", "unknown")
            role_label = {"self_reported": "自述", "independent": "独立验证",
                         "commentary": "评论", "unknown": "未知"}.get(source_role, source_role)

            # Get temporal info
            temporal = temporal_map.get(record_id, {})
            temporal_status = temporal.get("temporal_status", "unknown")

            # Get original text (with data boundary for safety)
            original_text = get_original_text(record)
            if original_text:
                # Limit text length for token budget
                remaining = max(100, token_budget - used_tokens - 500)
                text_snippet = original_text[:remaining]
                wrapped = wrap_retrieved_content(text_snippet)
            else:
                wrapped = "[无原文文本]"

            source_type = record.get("tg", "")
            sections.append(
                f"  [{i+1}] {title}\n"
                f"      来源: {source} | 日期: {date} | 角色: {role_label} | 时效: {temporal_status}\n"
                f"      原文:\n      {wrapped}"
            )

            # Estimate tokens used (rough: 1 Chinese char ≈ 2 tokens)
            used_tokens += len(title) + len(wrapped) // 2 + 50

    # ── Section 3: Conflicts ──
    if conflicts:
        sections.append("\n【⚠️ 证据冲突】")
        for c in conflicts[:3]:  # Max 3 conflicts shown
            vals = c.get("conflicting_values", {})
            sections.append(
                f"  • {c.get('metric', '?')}: "
                f"来源{c.get('items', ['?'])[0]}={vals.get('item_1', {}).get('value', '?')} "
                f"vs 来源{c.get('items', ['?', '?'])[1]}={vals.get('item_2', {}).get('value', '?')} "
                f"({c.get('type', 'unknown')})"
            )
        sections.append("  请在回答中明确说明此冲突。")

    # ── Section 4: Missing aspects ──
    missing = [r for r in ledger_status.get("requirements", [])
               if r.get("status") in ("MISSING", "PARTIAL", "CONFLICTED")]
    if missing:
        sections.append("\n【⚠️ 证据不足的方面】")
        for m in missing:
            sections.append(f"  • {m.get('description', m.get('id', '?'))}: {m.get('status', '?')}")
        sections.append("  请在回答中诚实说明哪些方面证据不足。")

    return "\n".join(sections)


def build_generator_system_prompt(
    base_prompt: str,
    evidence_package: str,
    has_conflicts: bool = False,
    has_missing: bool = False,
) -> str:
    """Build the full system prompt for the Generator."""
    safety_prompt = augment_system_prompt(base_prompt)

    rules = """
【生成规则】
1. 只基于提供的证据资料回答，不要用模型预训练知识补全缺失事实
2. 用 [1][2] 等标注引用来源
3. 如果某需求标记为"缺失证据"，诚实说明
4. 如果有证据冲突，明确呈现冲突，不要选择一个值假装唯一
5. 自述来源的数据必须标注归属主体
6. 预测/估算使用不确定性语言
"""

    if has_conflicts:
        rules += "7. 本问题存在证据冲突，必须在回答中明确说明\n"
    if has_missing:
        rules += "8. 本问题部分需求证据不足，必须诚实说明\n"

    return f"{safety_prompt}\n{rules}\n\n{evidence_package}"
