"""
T029 — Numeric Facts Extraction and Management
================================================
Tech-DB answers contain many numeric parameters: bandwidth, capacity,
efficiency, temperature, power, density, etc.

This module extracts and manages numeric facts to:
  1. Normalize units (GB/s, per-device vs system-total)
  2. Attach measurement conditions
  3. Enable conflict detection on numeric values
  4. Prevent unit confusion in answers

NumericFact Schema:
{
    "record_id": int,
    "metric": str,          # e.g. "bandwidth", "efficiency", "energy_density"
    "value": float,         # normalized value
    "raw_value": str,       # original text representation
    "unit": str,            # e.g. "TB/s", "Wh/kg", "%"
    "scope": str,           # "per_device" | "system_total" | "per_area" | "unknown"
    "condition": str,       # e.g. "8-GPU system", "25°C", "AM1.5G"
    "date": str,            # publication date of source
    "source_role": str,     # "self_reported" | "independent" | "unknown"
}
"""
import re
from primary_evidence import source_evidence_text
from typing import List, Dict


# Known metric patterns with their normalized names
METRIC_PATTERNS = [
    # Bandwidth
    (r"(?:带宽|bandwidth).{0,10}?([\d.]+)\s*(TB/s|GB/s|MB/s|Gbps|Mbps)", "bandwidth"),
    # Energy density
    (r"(?:能量密度|energy density).{0,10}?([\d.]+)\s*(Wh/kg|Wh/L|mWh/cm)", "energy_density"),
    # Efficiency
    (r"(?:效率|efficiency|PCE).{0,10}?([\d.]+)\s*%", "efficiency"),
    # Power
    (r"(?:功率|power).{0,10}?([\d.]+)\s*(MW|GW|kW|W|mW)", "power"),
    # Capacity
    (r"(?:产能|capacity).{0,10}?([\d.]+)\s*(GWh|MWh|kWh)", "capacity"),
    # Temperature
    (r"([\d.]+)\s*°?C", "temperature"),
    # Process node
    (r"(\d+)\s*nm", "process_node"),
    # Frequency
    (r"(?:频率|frequency|clock).{0,10}?([\d.]+)\s*(GHz|MHz)", "frequency"),
]

# Scope patterns
SCOPE_PATTERNS = [
    (r"(?:单|per[- ])(GPU|卡|芯片|设备|器件)", "per_device"),
    (r"(?:整机|系统|system|total|8[- ]?GPU|4[- ]?GPU)", "system_total"),
    (r"(?:每平方米|per m|/cm)", "per_area"),
]

# Condition patterns
CONDITION_PATTERNS = [
    r"(AM\s?1\.5\s?G|STC|标准测试条件|standard test)",
    r"(室温|room temperature|25\s*°?C)",
    r"(商用|commercial|mass prod|量产)",
    r"(实验|lab|experimental|中试|pilot)",
]


def extract_numeric_facts(record: dict) -> List[dict]:
    """Extract numeric facts from a record.

    Args:
        record: Record dict with at least 'b' or 'fb' for text

    Returns:
        List of numeric fact dicts
    """
    text = source_evidence_text(record)
    if not text:
        return []

    rid = record.get("record_id") or record.get("_idx", -1)
    date = record.get("d", "")
    facts = []

    for pattern, metric_name in METRIC_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                value = float(m.group(1))
                unit = m.group(2) if m.lastindex >= 2 else ""

                # Extract surrounding context for scope/condition
                start = max(0, m.start() - 50)
                end = min(len(text), m.end() + 50)
                context = text[start:end]

                scope = _detect_scope(context)
                condition = _detect_condition(context)

                facts.append({
                    "record_id": rid,
                    "metric": metric_name,
                    "value": value,
                    "raw_value": m.group(),
                    "unit": unit,
                    "scope": scope,
                    "condition": condition,
                    "date": date,
                    "source_role": "unknown",
                })
            except (ValueError, IndexError):
                continue

    return facts


def _detect_scope(context: str) -> str:
    """Detect measurement scope from context."""
    for pattern, scope in SCOPE_PATTERNS:
        if re.search(pattern, context, re.IGNORECASE):
            return scope
    return "unknown"


def _detect_condition(context: str) -> str:
    """Detect measurement conditions from context."""
    conditions = []
    for pattern in CONDITION_PATTERNS:
        m = re.search(pattern, context, re.IGNORECASE)
        if m:
            conditions.append(m.group())
    return "; ".join(conditions) if conditions else "unknown"


def normalize_unit(value: float, unit: str) -> tuple:
    """Normalize values to a standard unit within each metric.

    Returns (normalized_value, normalized_unit).
    """
    unit_lower = unit.lower()

    # Bandwidth: normalize to GB/s
    if "tb/s" in unit_lower:
        return (value * 1000, "GB/s")
    elif "mb/s" in unit_lower:
        return (value / 1000, "GB/s")
    elif "gbps" in unit_lower:
        return (value / 8, "GB/s")  # bits to bytes
    elif "mbps" in unit_lower:
        return (value / 8000, "GB/s")

    # Power: normalize to W
    if unit_lower in ("mw",):
        return (value / 1000, "W")
    elif unit_lower in ("kw",):
        return (value * 1000, "W")
    elif unit_lower in ("mw",) and "Wh" not in unit:
        return (value / 1000, "W")

    return (value, unit)


def compare_numeric_facts(f1: dict, f2: dict) -> str:
    """Compare two numeric facts.

    Returns:
        "AGREE" | "CONTRADICT" | "DIFFERENT_SCOPE" | "DIFFERENT_CONDITION" | "UNKNOWN"
    """
    if f1.get("metric") != f2.get("metric"):
        return "UNKNOWN"

    # Different scope
    if f1.get("scope") != f2.get("scope") and \
       f1.get("scope") != "unknown" and f2.get("scope") != "unknown":
        return "DIFFERENT_SCOPE"

    # Different condition
    if f1.get("condition") != f2.get("condition") and \
       f1.get("condition") != "unknown" and f2.get("condition") != "unknown":
        return "DIFFERENT_CONDITION"

    # Same unit, compare values
    v1 = f1.get("value", 0)
    v2 = f2.get("value", 0)
    if v1 == v2:
        return "AGREE"

    diff_ratio = abs(v1 - v2) / max(abs(v1), abs(v2), 0.001)
    if diff_ratio < 0.05:  # Within 5%
        return "AGREE"
    elif diff_ratio > 0.15:  # More than 15% different
        return "CONTRADICT"
    else:
        return "AGREE"  # Close enough
