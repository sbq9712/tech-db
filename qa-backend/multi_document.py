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
import copy
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, List, Dict, Optional, Tuple
from config import llm_model_func
from citation_grounding import ground_citation_evidence, get_original_text


@dataclass(frozen=True)
class DocumentWorkerInput:
    """One invocation is structurally scoped to one immutable document."""
    query: str
    requirement_ids: Tuple[str, ...]
    requirement_descriptions: Tuple[str, ...]
    record_id: str
    source_snapshot_id: str
    evidence_text: str
    content_sha256: str
    entity_metadata: dict = field(default_factory=dict)
    source_metadata: dict = field(default_factory=dict)
    provenance_metadata: dict = field(default_factory=dict)
    temporal_metadata: dict = field(default_factory=dict)
    synthetic_navigation_hint: str = ""

    def __post_init__(self):
        if not self.record_id or not self.source_snapshot_id:
            raise ValueError("worker input requires stable record/snapshot identity")
        actual = hashlib.sha256(self.evidence_text.encode("utf-8")).hexdigest()
        if self.content_sha256 and actual != self.content_sha256:
            raise ValueError("worker immutable evidence_text hash mismatch")


@dataclass(frozen=True)
class WorkerEvidenceRef:
    record_id: str
    source_snapshot_id: str
    start_offset: int
    end_offset: int
    exact_text: str
    text_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DocumentLocalClaim:
    claim: str
    requirement_id: str
    evidence_refs: Tuple[WorkerEvidenceRef, ...]
    epistemic_type: str = "VERIFIABLE_FACT"

    def to_dict(self) -> dict:
        return {"claim": self.claim, "requirement_id": self.requirement_id,
                "epistemic_type": self.epistemic_type,
                "evidence_refs": [r.to_dict() for r in self.evidence_refs]}


@dataclass(frozen=True)
class DocumentEvidencePacket:
    record_id: str
    source_snapshot_id: str
    requirement_results: Tuple[dict, ...]
    local_claims: Tuple[DocumentLocalClaim, ...]
    numeric_facts: Tuple[dict, ...] = field(default_factory=tuple)
    relation_checks: Tuple[dict, ...] = field(default_factory=tuple)
    temporal_scope: dict = field(default_factory=dict)
    source_role: str = "unknown"
    independent_group_id: str = ""
    internal_conflicts: Tuple[dict, ...] = field(default_factory=tuple)
    unanswered_aspects: Tuple[str, ...] = field(default_factory=tuple)
    relevant: bool = False
    evidence_found: bool = False
    degraded: Tuple[str, ...] = field(default_factory=tuple)
    worker_version: str = "phase04-worker-1.0"

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "source_snapshot_id": self.source_snapshot_id,
            "requirement_results": [dict(v) for v in self.requirement_results],
            "local_claims": [v.to_dict() for v in self.local_claims],
            "numeric_facts": [dict(v) for v in self.numeric_facts],
            "relation_checks": [dict(v) for v in self.relation_checks],
            "temporal_scope": dict(self.temporal_scope),
            "source_role": self.source_role,
            "independent_group_id": self.independent_group_id,
            "internal_conflicts": [dict(v) for v in self.internal_conflicts],
            "unanswered_aspects": list(self.unanswered_aspects),
            "relevant": self.relevant,
            "evidence_found": self.evidence_found,
            "degraded": list(self.degraded),
            "worker_version": self.worker_version,
        }


@dataclass(frozen=True)
class PacketCacheKey:
    manifest_id: str
    profile: str
    source_snapshot_id: str
    requirement_fingerprint: str
    worker_model: str
    prompt_version: str
    schema_version: str
    access_scope_fingerprint: str

    def __post_init__(self):
        if not all(asdict(self).values()):
            raise ValueError("packet cache key requires every isolation field")

    @classmethod
    def build(cls, *, manifest_id: str, profile: str,
              source_snapshot_id: str, requirements: List[dict],
              worker_model: str, prompt_version: str,
              schema_version: str, access_scope: str) -> "PacketCacheKey":
        req_fp = hashlib.sha256(json.dumps(
            requirements, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        access_fp = hashlib.sha256(str(access_scope).encode("utf-8")).hexdigest()
        return cls(manifest_id, profile, source_snapshot_id, req_fp,
                   worker_model, prompt_version, schema_version, access_fp)


class PacketCache:
    """Optional immutable packet cache; misses/failures never change truth."""
    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._items: Dict[PacketCacheKey, DocumentEvidencePacket] = {}

    def get(self, key: PacketCacheKey) -> Optional[DocumentEvidencePacket]:
        if not self.enabled:
            return None
        return copy.deepcopy(self._items.get(key))

    def put(self, key: PacketCacheKey, packet: DocumentEvidencePacket):
        if self.enabled:
            self._items[key] = copy.deepcopy(packet)

    def clear(self):
        self._items.clear()


async def process_document_packet(
    worker_input: DocumentWorkerInput,
    extractor_fn: Optional[Callable[[DocumentWorkerInput], Awaitable[dict]]] = None,
) -> DocumentEvidencePacket:
    """Extract a typed packet and exact-ground every local claim.

    The extractor sees only ``worker_input`` (one snapshot). Natural-language
    output never leaves this function; only exact-grounded typed claims do.
    """
    if not isinstance(worker_input, DocumentWorkerInput):
        raise TypeError("process_document_packet requires DocumentWorkerInput")
    if extractor_fn is None:
        record = {
            "t": worker_input.source_metadata.get("title", ""),
            "a": worker_input.source_metadata.get("source", ""),
            "d": worker_input.temporal_metadata.get("date", ""),
            "fb": worker_input.evidence_text,
        }
        legacy = await process_document(
            worker_input.query,
            " | ".join(worker_input.requirement_descriptions),
            record, worker_input.record_id)
    else:
        legacy = await extractor_fn(worker_input)
    legacy = legacy if isinstance(legacy, dict) else {}
    claims: List[DocumentLocalClaim] = []
    requirement_results = []
    found_by_req = {rid: False for rid in worker_input.requirement_ids}
    for raw in legacy.get("claims") or legacy.get("local_claims") or []:
        if not isinstance(raw, dict):
            continue
        span = str(raw.get("evidence_span") or raw.get("exact_text") or "")
        start = worker_input.evidence_text.find(span) if span else -1
        if start < 0:
            continue  # exact-only; no fuzzy/approximate worker evidence
        end = start + len(span)
        req_id = str(raw.get("requirement_id") or
                     (worker_input.requirement_ids[0]
                      if worker_input.requirement_ids else ""))
        if req_id not in found_by_req:
            continue
        ref = WorkerEvidenceRef(
            record_id=worker_input.record_id,
            source_snapshot_id=worker_input.source_snapshot_id,
            start_offset=start, end_offset=end, exact_text=span,
            text_sha256=hashlib.sha256(span.encode("utf-8")).hexdigest())
        claims.append(DocumentLocalClaim(
            claim=str(raw.get("local_claim") or raw.get("claim") or span),
            requirement_id=req_id, evidence_refs=(ref,),
            epistemic_type=str(raw.get("epistemic_type") or "VERIFIABLE_FACT")))
        found_by_req[req_id] = True
    for rid in worker_input.requirement_ids:
        requirement_results.append({
            "requirement_id": rid,
            "relevant": bool(legacy.get("relevant", True)),
            "evidence_found": found_by_req[rid],
        })
    return DocumentEvidencePacket(
        record_id=worker_input.record_id,
        source_snapshot_id=worker_input.source_snapshot_id,
        requirement_results=tuple(requirement_results),
        local_claims=tuple(claims),
        numeric_facts=tuple(legacy.get("numeric_facts") or []),
        relation_checks=tuple(legacy.get("relation_checks") or []),
        temporal_scope=dict(legacy.get("temporal_scope") or {}),
        source_role=str(legacy.get("source_role") or
                        worker_input.provenance_metadata.get("source_role") or
                        "unknown"),
        # This is copied from request-pinned enrichment metadata, never
        # inferred from worker prose.  Empty means unknown; packet/ledger
        # code must not fabricate one group per record.
        independent_group_id=str(
            worker_input.provenance_metadata.get("independent_group_id") or ""),
        internal_conflicts=tuple(legacy.get("internal_conflicts") or []),
        unanswered_aspects=tuple(str(v) for v in
                                 legacy.get("unanswered_aspects") or []),
        relevant=bool(legacy.get("relevant", bool(claims))),
        evidence_found=bool(claims),
        degraded=(("worker_error",) if legacy.get("error") else tuple()),
    )


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
            allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
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
