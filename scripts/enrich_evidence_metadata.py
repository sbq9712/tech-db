#!/usr/bin/env python3
"""
T007 — Evidence Metadata Enrichment
====================================
Enriches each record with evidence metadata for the RAG pipeline.

Per record generates:
  record_id
  source_org_id
  source_domain
  source_type
  source_level: primary / secondary / unknown
  evidence_role: self_reported / independent / commentary / unknown
  original_source_org
  original_url
  provenance_root_id
  independent_group_id
  is_repost
  same_origin_probability
  published_at
  event_at
  temporal_status
  epistemic_hints
  content_risk_flags
  data_quality_flags
  metadata_version
  enriched_at

Principle: "Who published, where from, what nature, when valid, is it a repost?"
NOT: "How much can we trust it for all questions?"
"""
import json
import sys
import os
import re
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "qa-backend"))

LITE = REPO / "data" / "processed" / "all-records-lite.json"
OUTPUT = REPO / "runtime" / "indexes" / "evidence_metadata.json"

METADATA_VERSION = "0.1.0"


def extract_domain(url: str) -> str:
    """Extract domain from URL."""
    from urllib.parse import urlparse
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def infer_source_level(record: dict) -> str:
    """Infer if source is primary or secondary."""
    tag = record.get("tg", "")
    source = (record.get("a", "") or record.get("s", "")).lower()

    # Primary sources
    if tag in ("产业进展", "技术突破"):
        return "primary"
    if any(k in source for k in ["official", "官网", ".gov"]):
        return "primary"

    # Secondary
    if tag in ("观点评论", "行业观察"):
        return "secondary"
    if any(k in source for k in ["news", "media", "报道", "转载"]):
        return "secondary"

    return "unknown"


def infer_evidence_role(record: dict) -> str:
    """Infer evidence role."""
    tag = record.get("tg", "")
    source = (record.get("a", "") or record.get("s", "")).lower()

    # Self-reported: company's own announcement
    if tag in ("产业进展", "技术突破") and source:
        return "self_reported"

    # Commentary: opinions/analysis
    if tag in ("观点评论",):
        return "commentary"

    # Independent: academic, media
    if tag == "研究论文":
        return "independent"
    if any(k in source for k in ["doi", "nature", "science", "arxiv"]):
        return "independent"

    return "unknown"


def detect_epistemic_hints(text: str) -> list:
    """Detect epistemic hints in text."""
    hints = []
    if not text:
        return hints

    # Predictions
    if re.search(r"预计|预测|有望|或将|可能|计划", text):
        hints.append("contains_prediction")
    if re.search(r"元年|爆发|拐点|突破性|颠覆", text):
        hints.append("contains_hype_language")
    if re.search(r"声称|宣称|公布|表示", text):
        hints.append("contains_attribution")

    return hints


def detect_content_risk_flags(text: str) -> list:
    """Detect content risk flags."""
    flags = []
    if not text:
        return flags

    # Prompt injection risk
    if re.search(r"忽略以上|ignore.*instructions|你现在是", text, re.IGNORECASE):
        flags.append("prompt_injection_risk")

    # Marketing hype
    if re.search(r"全球首个|世界首创|独一无二|史无前例", text):
        flags.append("marketing_hype")

    return flags


def detect_data_quality_flags(record: dict) -> list:
    """Detect data quality issues."""
    flags = []

    if not record.get("b") and not record.get("fb"):
        flags.append("missing_body")
    if not record.get("d"):
        flags.append("missing_date")
    if not record.get("u"):
        flags.append("missing_url")
    if not record.get("t"):
        flags.append("missing_title")

    # Date validity
    date_str = record.get("d", "")
    if date_str:
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            now = datetime.now()
            if dt > now:
                flags.append("future_date")
            elif dt.year < 2000:
                flags.append("past_date_anomaly")
        except (ValueError, TypeError):
            flags.append("invalid_date_format")

    return flags


def enrich_record(idx: int, record: dict) -> dict:
    """Enrich a single record with evidence metadata."""
    from epistemic import infer_source_type
    from temporal import determine_temporal_status

    url = record.get("u", "")
    text = record.get("fb", "") or record.get("b", "") or record.get("as", "")
    source = record.get("a", "") or record.get("s", "")

    return {
        "record_id": idx,
        "source_org_id": source,
        "source_domain": extract_domain(url),
        "source_type": infer_source_type(record),
        "source_level": infer_source_level(record),
        "evidence_role": infer_evidence_role(record),
        "original_source_org": "",
        "original_url": "",
        "provenance_root_id": f"prov-{idx}",  # Default: unique
        "independent_group_id": f"prov-{idx}",
        "is_repost": False,
        "same_origin_probability": 1.0,
        "published_at": record.get("d", ""),
        "event_at": "",
        "temporal_status": determine_temporal_status(record).get("temporal_status", "unknown"),
        "epistemic_hints": detect_epistemic_hints(text),
        "content_risk_flags": detect_content_risk_flags(text),
        "data_quality_flags": detect_data_quality_flags(record),
        "metadata_version": METADATA_VERSION,
        "enriched_at": datetime.now().isoformat(),
    }


def run_enrichment():
    """Run evidence metadata enrichment on all records."""
    print(f"Loading records from {LITE.name}...")
    data = json.loads(LITE.read_text("utf-8"))
    print(f"  Total records: {len(data)}")

    print(f"\nEnriching {len(data)} records...")
    metadata = {}
    for i, record in enumerate(data):
        metadata[i] = enrich_record(i, record)
        if (i + 1) % 10000 == 0:
            print(f"  {i+1}/{len(data)} enriched")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ Saved {len(metadata)} records to {OUTPUT}")

    # Summary stats
    source_levels = {}
    evidence_roles = {}
    quality_issues = 0
    for m in metadata.values():
        sl = m.get("source_level", "unknown")
        source_levels[sl] = source_levels.get(sl, 0) + 1
        er = m.get("evidence_role", "unknown")
        evidence_roles[er] = evidence_roles.get(er, 0) + 1
        if m.get("data_quality_flags"):
            quality_issues += 1

    print(f"\nSummary:")
    print(f"  Source levels: {source_levels}")
    print(f"  Evidence roles: {evidence_roles}")
    print(f"  Records with quality issues: {quality_issues}")


if __name__ == "__main__":
    run_enrichment()
