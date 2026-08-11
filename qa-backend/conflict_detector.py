"""
T030 — Conflict Detection
==========================
Detects and classifies conflicts between evidence items.

Conflict types:
  AGREE             — Evidence agrees
  CONTRADICT        — Same time, same conditions, different values
  DIFFERENT_SCOPE   — Different scope/condition (e.g., per-device vs system-total)
  VERSION_UPDATE    — Old plan vs new result (time-based evolution)
  UNKNOWN           — Cannot determine

Rules:
  1. Deterministic numeric/time comparison first
  2. GLM only handles scope/semantic ambiguity
  3. High-severity contradiction → Gap Analysis generates resolution query
  4. After targeted search, still conflicting → Ledger stays CONFLICTED
  5. Generator must explicitly present unresolved conflicts
  6. Self-report vs independent measurement differences need evidence_role
  7. Graph semantic relations aligned by subject/predicate/object/direction/time
"""
import re
from typing import List, Dict, Tuple


CONFLICT_AGREE = "AGREE"
CONFLICT_CONTRADICT = "CONTRADICT"
CONFLICT_DIFFERENT_SCOPE = "DIFFERENT_SCOPE"
CONFLICT_VERSION_UPDATE = "VERSION_UPDATE"
CONFLICT_UNKNOWN = "UNKNOWN"


def detect_conflicts(evidence_items: list, query: str = "") -> dict:
    """Detect conflicts among evidence items.

    Args:
        evidence_items: List of dicts with at least:
            - record_id
            - text or excerpt
            - date (optional)
            - source_role (optional: self_reported / independent)
        query: Original query for context

    Returns:
        {
            "has_conflicts": bool,
            "conflicts": [
                {
                    "type": "CONTRADICT",
                    "severity": "high" | "medium" | "low",
                    "items": [record_id, record_id],
                    "description": "...",
                    "conflicting_values": {...},
                }
            ],
        }
    """
    conflicts = []

    # Extract numeric facts from each evidence item
    all_facts = []
    for item in evidence_items:
        facts = _extract_numeric_facts(item)
        all_facts.extend(facts)

    # Group facts by metric name
    fact_groups: Dict[str, list] = {}
    for fact in all_facts:
        key = fact.get("metric", "").lower()
        if key:
            fact_groups.setdefault(key, []).append(fact)

    # Check for conflicts within each metric group
    for metric, facts in fact_groups.items():
        if len(facts) < 2:
            continue

        # Compare values
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                f1, f2 = facts[i], facts[j]
                conflict = _compare_facts(f1, f2)
                if conflict != CONFLICT_AGREE:
                    conflicts.append({
                        "type": conflict,
                        "severity": _determine_severity(conflict, f1, f2),
                        "items": [f1["record_id"], f2["record_id"]],
                        "metric": metric,
                        "description": f"{metric}: {f1.get('value')} vs {f2.get('value')}",
                        "conflicting_values": {
                            "item_1": {"value": f1.get("value"), "unit": f1.get("unit"),
                                       "date": f1.get("date"), "source_role": f1.get("source_role")},
                            "item_2": {"value": f2.get("value"), "unit": f2.get("unit"),
                                       "date": f2.get("date"), "source_role": f2.get("source_role")},
                        },
                    })

    return {
        "has_conflicts": len(conflicts) > 0,
        "conflicts": conflicts,
    }


def _extract_numeric_facts(item: dict) -> list:
    """Extract numeric facts from an evidence item."""
    facts = []
    text = item.get("text", item.get("excerpt", ""))
    if not text:
        return facts

    rid = item.get("record_id", -1)
    date = item.get("date", "")
    source_role = item.get("source_role", "unknown")

    # Pattern: number + unit (Chinese and English)
    patterns = [
        # English: 1.8 TB/s, 500 Wh/kg, 30%, etc.
        (r"([\d.]+)\s*(TB/s|GB/s|MB/s|Wh/kg|MW|GW|kW|W|nm|%\s*|mg|kg|g)", "metric_from_unit"),
        # Chinese: 1.8TB/s, 500瓦时/千克
        (r"([\d.]+)\s*(太字节|吉字节|瓦时|纳米|兆瓦|吉瓦)", "metric_from_cn_unit"),
    ]

    for pattern, extraction_type in patterns:
        for m in re.finditer(pattern, text):
            try:
                value = float(m.group(1))
                unit = m.group(2).strip()
                # Determine metric name from surrounding context
                start = max(0, m.start() - 20)
                context = text[start:m.start()]
                metric = _extract_metric_name(context)

                facts.append({
                    "record_id": rid,
                    "metric": metric or unit,
                    "value": value,
                    "unit": unit,
                    "date": date,
                    "source_role": source_role,
                    "raw_match": m.group(),
                })
            except (ValueError, IndexError):
                continue

    return facts


def _extract_metric_name(context: str) -> str:
    """Try to extract the metric name from context before the number."""
    # Look for common metric patterns
    metric_patterns = [
        r"(带宽|效率|能量密度|功率|容量|温度|频率|面积|速度)",
        r"(bandwidth|efficiency|density|power|capacity|temperature|frequency)",
    ]
    for pattern in metric_patterns:
        m = re.search(pattern, context, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _compare_facts(f1: dict, f2: dict) -> str:
    """Compare two numeric facts and determine relationship."""
    # Different units → can't compare
    if f1.get("unit", "").lower() != f2.get("unit", "").lower():
        return CONFLICT_DIFFERENT_SCOPE

    # Same unit, compare values
    v1, v2 = f1.get("value", 0), f2.get("value", 0)
    if v1 == v2:
        return CONFLICT_AGREE

    # Check relative difference
    diff_ratio = abs(v1 - v2) / max(abs(v1), abs(v2), 0.001)

    # Check if different time periods (version update)
    d1, d2 = f1.get("date", ""), f2.get("date", "")
    if d1 and d2 and d1 != d2:
        # Different dates with different values → likely version update
        if diff_ratio < 0.5:  # Not wildly different
            return CONFLICT_VERSION_UPDATE
        else:
            return CONFLICT_VERSION_UPDATE  # Still version update even if very different

    # Different source roles
    r1 = f1.get("source_role", "")
    r2 = f2.get("source_role", "")
    if r1 != r2 and CONFLICT_DIFFERENT_SCOPE:
        # Self-report vs independent → different scope
        if {r1, r2} == {"self_reported", "independent"}:
            return CONFLICT_DIFFERENT_SCOPE

    # Same time, same conditions, different values → CONTRADICT
    if diff_ratio > 0.1:  # More than 10% different
        return CONFLICT_CONTRADICT

    return CONFLICT_AGREE


def _determine_severity(conflict_type: str, f1: dict, f2: dict) -> str:
    """Determine severity of a conflict."""
    if conflict_type == CONFLICT_CONTRADICT:
        return "high"
    elif conflict_type == CONFLICT_DIFFERENT_SCOPE:
        return "low"  # Expected, not a real conflict
    elif conflict_type == CONFLICT_VERSION_UPDATE:
        return "medium"
    return "low"
