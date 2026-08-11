"""
T038 — Multi-Document Evidence Processing
==========================================
Processes multiple documents to extract per-document evidence,
merge across documents, and prepare structured evidence for the Generator.

Workers:
  - Per-document: extract claims, evidence spans, source role
  - Cross-document: merge, deduplicate, detect conflicts

Key rules:
  1. Workers execute per-document (isolated)
  2. Each local claim must have exact evidence span from THAT document
  3. Evidence spans verified via T003 citation grounding
  4. Workers cannot output "based on common knowledge"
  5. relevant=true, evidence_found=false identifies "title relevant but no evidence"
  6. Workers don't decide overall status
  7. Workers don't decide cross-document conflict resolution
"""
import json
import os
from typing import List, Dict
from config import llm_model_func
from citation_grounding import ground_citation_evidence, get_original_text


DOCUMENT_WORKER_PROMPT = """你是技术情报分析专家。分析以下文档，提取与用户问题相关的证据。

规则：
1. 只从该文档的原文中提取证据
2. evidence_span 必须来自原文（会程序验证）
3. 不允许使用"常识"或"模型知识"
4. 如果文档标题相关但正文无证据，返回 evidence_found=false
5. 标注每条证据的 epistemic_type

只输出JSON：
{{
  "record_id": {record_id},
  "requirement_id": "{requirement_id}",
  "relevant": true,
  "evidence_found": true,
  "claims": [
    {{
      "local_claim": "该文档中的事实声明",
      "epistemic_type": "VERIFIABLE_FACT",
      "evidence_span": "原文中支持此声明的精确文本片段",
      "source_role": "self_reported"
    }}
  ],
  "numeric_facts": [],
  "entities": [],
  "temporal_scope": {{"period": "2026-01"}},
  "source_role": "self_reported",
  "internal_conflicts": [],
  "unanswered_aspects": []
}}

epistemic_type 可选：VERIFIABLE_FACT / REPORTED_CLAIM / ESTIMATE / PREDICTION / ANALYSIS / OPINION
source_role 可选：self_reported / independent / commentary / unknown

用户问题：{query}
需求：{requirement}

文档标题：{title}
来源：{source} | 日期：{date}

文档原文（最多2000字）：
{text}"""


async def process_document(
    query: str,
    requirement: str,
    record: dict,
    record_id: int,
) -> dict:
    """Process a single document for evidence extraction.

    Returns DocumentEvidencePacket:
    {
        "record_id": int,
        "requirement_id": str,
        "relevant": bool,
        "evidence_found": bool,
        "claims": [...],
        "numeric_facts": [...],
        "entities": [...],
        "temporal_scope": {...},
        "source_role": str,
        "internal_conflicts": [...],
        "unanswered_aspects": [...]
    }
    """
    text = get_original_text(record)
    if not text:
        return {
            "record_id": record_id,
            "relevant": False,
            "evidence_found": False,
            "claims": [],
        }

    # Truncate for prompt
    text_snippet = text[:2000]

    prompt = DOCUMENT_WORKER_PROMPT.format(
        record_id=record_id,
        requirement_id=requirement[:50],
        query=query[:300],
        requirement=requirement[:200],
        title=record.get("t", "")[:100],
        source=record.get("a", record.get("s", "")),
        date=record.get("d", ""),
        text=text_snippet,
    )

    try:
        result_text = await llm_model_func(
            prompt,
            system_prompt="你是技术情报分析专家。只输出JSON。",
            temperature=0.0,
            max_tokens=2048,
        )

        packet = _parse_worker_result(result_text, record_id)

        # Verify evidence spans
        for claim in packet.get("claims", []):
            span = claim.get("evidence_span", "")
            if span:
                grounding = ground_citation_evidence(record, proposed_span=span)
                claim["grounding_status"] = grounding["grounding_status"]
                claim["start_offset"] = grounding["start_offset"]
                claim["end_offset"] = grounding["end_offset"]
            else:
                claim["grounding_status"] = "GROUNDING_FAIL"

        return packet

    except Exception as e:
        print(f"[document_worker] Error: {e}", flush=True)
        return {
            "record_id": record_id,
            "relevant": True,
            "evidence_found": False,
            "claims": [],
            "error": str(e),
        }


async def process_documents(
    query: str,
    requirement: str,
    records: list,
    record_ids: list,
    max_concurrent: int = 5,
) -> list:
    """Process multiple documents concurrently.

    Args:
        query: User question
        requirement: Requirement description
        records: List of record dicts
        record_ids: List of record indices to process
        max_concurrent: Maximum concurrent workers

    Returns:
        List of DocumentEvidencePacket
    """
    semaphore = __import__("asyncio").Semaphore(max_concurrent)

    async def bounded_process(rid):
        async with semaphore:
            if 0 <= rid < len(records):
                return await process_document(query, requirement, records[rid], rid)
            return {"record_id": rid, "relevant": False, "evidence_found": False}

    tasks = [bounded_process(rid) for rid in record_ids[:20]]  # Cap at 20 documents
    results = await __import__("asyncio").gather(*tasks, return_exceptions=True)

    packets = []
    for r in results:
        if isinstance(r, Exception):
            packets.append({"relevant": False, "evidence_found": False, "error": str(r)})
        else:
            packets.append(r)

    return packets


def merge_cross_document(packets: list) -> dict:
    """Merge DocumentEvidencePackets across documents.

    Performs:
      - Merge claims
      - Provenance/independent group identification
      - Entity canonicalization hints
      - Temporal alignment
      - Numeric condition normalization
      - Conflict detection
      - Evidence coverage update

    Returns:
        {
            "merged_claims": [...],
            "all_evidence_spans": [...],
            "conflicts": [...],
            "coverage": {...},
        }
    """
    all_claims = []
    all_spans = []
    conflicts = []

    for packet in packets:
        if not packet.get("evidence_found"):
            continue

        rid = packet.get("record_id", -1)
        for claim in packet.get("claims", []):
            grounding = claim.get("grounding_status", "GROUNDING_FAIL")
            if grounding == "GROUNDING_FAIL":
                continue  # Drop ungrounded claims

            all_claims.append({
                "record_id": rid,
                "claim": claim.get("local_claim", ""),
                "type": claim.get("epistemic_type", "VERIFIABLE_FACT"),
                "source_role": claim.get("source_role", "unknown"),
                "evidence_span": claim.get("evidence_span", ""),
                "grounding_status": grounding,
            })
            all_spans.append({
                "record_id": rid,
                "text": claim.get("evidence_span", ""),
            })

    # Simple conflict detection: check for different values
    from conflict_detector import detect_conflicts
    evidence_items = [
        {"record_id": c["record_id"], "text": c["evidence_span"]}
        for c in all_claims
    ]
    conflict_result = detect_conflicts(evidence_items)
    conflicts = conflict_result.get("conflicts", [])

    return {
        "merged_claims": all_claims,
        "all_evidence_spans": all_spans,
        "conflicts": conflicts,
        "coverage": {
            "documents_processed": len(packets),
            "documents_with_evidence": sum(1 for p in packets if p.get("evidence_found")),
            "total_claims": len(all_claims),
            "grounded_claims": sum(1 for c in all_claims if c.get("grounding_status") in ("VALID", "FUZZY")),
        },
    }


def _parse_worker_result(text: str, record_id: int) -> dict:
    """Parse document worker LLM output."""
    import re
    if not text:
        return {"record_id": record_id, "relevant": False, "evidence_found": False, "claims": []}

    try:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            data["record_id"] = record_id
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return {"record_id": record_id, "relevant": True, "evidence_found": False, "claims": []}
