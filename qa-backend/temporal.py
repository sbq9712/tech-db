"""
T010 — Temporal Metadata
=========================
Manage temporal context of records:
  - published_at: when the article was published
  - event_at: when the event described actually happened
  - target_future_time: for predictions/roadmaps
  - temporal_status: current / historical / superseded / future / unknown

Rules:
  - "current/latest" queries should not prioritize superseded info
  - "historical" queries should keep old evidence
  - Version updates (old plan → new result) must be distinguishable
  - Unknown dates are marked unknown, never guessed
"""
import os
import re
from typing import Optional
from datetime import datetime


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string in common formats."""
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()

    # Try common formats
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日",
        "%Y-%m",
        "%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str[:len(datetime.now().strftime(fmt))], fmt)
        except (ValueError, TypeError):
            continue

    # Try extracting date from string
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", date_str)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    return None


def determine_temporal_status(
    record: dict,
    query_temporal_intent: str = "unspecified",
    now: datetime = None,
) -> dict:
    """Determine the temporal status of a record.

    Args:
        record: Record dict
        query_temporal_intent: "current" | "latest" | "as_of" | "historical" | "trend" | "unspecified"
        now: Current datetime (for testing)

    Returns:
        {
            "temporal_status": str,  # current / historical / superseded / future / unknown
            "published_at": str,
            "event_at": str,
            "target_future_time": str,
            "temporal_relevance": str,  # high / medium / low / unknown
        }
    """
    if now is None:
        now = datetime.now()

    published = parse_date(record.get("d", ""))
    published_str = record.get("d", "")

    # Determine temporal status
    status = "unknown"
    if published:
        days_ago = (now - published).days
        if days_ago < 0:
            status = "future"  # Published in the future (data error?)
        elif days_ago <= 30:
            status = "current"
        elif days_ago <= 180:
            status = "recent"
        elif days_ago <= 730:
            status = "historical"
        else:
            status = "superseded"

    # Adjust based on query temporal intent
    relevance = "medium"
    if query_temporal_intent in ("current", "latest"):
        if status in ("current", "recent"):
            relevance = "high"
        elif status == "historical":
            relevance = "low"
        elif status == "superseded":
            relevance = "low"  # Superseded info for current queries
        else:
            relevance = "unknown"
    elif query_temporal_intent == "historical":
        if status in ("historical", "superseded"):
            relevance = "high"  # Old info for historical queries
        elif status == "current":
            relevance = "medium"
        else:
            relevance = "medium"
    elif query_temporal_intent == "trend":
        relevance = "high"  # All time periods relevant for trends
    else:  # unspecified
        if status == "current":
            relevance = "high"
        elif status == "recent":
            relevance = "medium"
        else:
            relevance = "medium"

    return {
        "temporal_status": status,
        "published_at": published_str,
        "event_at": "",  # Would need event extraction
        "target_future_time": "",  # Would need prediction extraction
        "temporal_relevance": relevance,
    }


def extract_temporal_hints(text: str) -> list:
    """Extract temporal hints (future predictions, dates mentioned) from text.

    Returns list of {"type": "prediction"/"event"/"plan", "time": "...", "text": "..."}.
    """
    hints = []

    if not text:
        return hints

    # Prediction patterns (Chinese)
    pred_patterns = [
        (r"(?:预计|计划|规划|目标|有望|将于)(\d{4})年", "prediction"),
        (r"(?:到|至|截至)(\d{4})年", "plan"),
        (r"(?: roadmap|路线图).{0,20}?(\d{4})", "plan"),
    ]

    for pattern, ptype in pred_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            year = int(m.group(1))
            if 2020 <= year <= 2050:
                # Get surrounding context
                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 20)
                hints.append({
                    "type": ptype,
                    "time": f"{year}",
                    "text": text[start:end],
                })

    return hints
