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


# ══════════════════════════════════════════════════════════════════════════
# Phase 02 — RT-022: provenance-carrying numeric facts + claim verification
# ══════════════════════════════════════════════════════════════════════════
# Every numeric fact carries an evidence_ref (record_id / source_snapshot_id /
# locator / exact_text) pointing INTO the immutable SourceSnapshot text, plus
# normalized_value + transform_rule_version so the normalization itself is
# reproducible. verify_numeric_claim() checks a claim's numbers against
# grounded evidence with:
#   * unit-family matching — Gb/s (bits) and GB/s (bytes) are DIFFERENT
#     families and never normalize into each other (the classic 8× trap);
#   * scope matching — per-device vs system-aggregate figures don't compare;
#   * value matching — same family + same scope, ±5% tolerance.

NUMERIC_VERIFY_VERSION = "1.0.0"
TRANSFORM_RULE_VERSION = "2026-08-18.1"

_GENERIC_NUM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*("
    r"TB/s|GB/s|MB/s|KB/s|B/s|Tb/s|Gb/s|Mb/s|kb/s|Tbps|Gbps|Mbps|kbps|bps|"
    r"Wh/kg|Wh/L|mWh/cm²|"
    r"GWh|MWh|kWh|Wh|"
    r"MW|kW|mW|GW|"
    r"THz|GHz|MHz|kHz|Hz|"
    r"µm|nm|mm|cm|km|"
    r"MPa|GPa|kPa|"
    r"mAh|Ah|mA|"
    r"kg|mg|g|"
    r"°C|℃|%|"
    r"亿美元|万美元|亿|万|million|billion|"
    r"小时|分钟|秒|天|年|月|次|个|条|台|片|倍|dB"
    r")"
)

# Unit families. Bits and bytes NEVER share a family.
_UNIT_FAMILIES = {
    "bytes_per_sec": ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"],
    "bits_per_sec": ["bps", "kbps", "Mbps", "Gbps", "Tbps", "b/s", "kb/s", "Mb/s", "Gb/s", "Tb/s"],
    "energy_density": ["Wh/kg", "Wh/L", "mWh/cm²"],
    "energy": ["Wh", "kWh", "MWh", "GWh"],
    "power": ["mW", "W", "kW", "MW", "GW"],
    "frequency": ["Hz", "kHz", "MHz", "GHz", "THz"],
    "length": ["nm", "µm", "mm", "cm", "m", "km"],
    "pressure": ["kPa", "MPa", "GPa"],
    "charge": ["mAh", "Ah"],
    "current": ["mA", "A"],
    "mass": ["mg", "g", "kg"],
    "percent": ["%"],
    "temperature": ["°C", "℃"],
    "count": ["亿", "万", "million", "billion", "个", "次", "条", "台", "片", "倍", "亿美元", "万美元"],
    "time": ["秒", "分钟", "小时", "天", "月", "年"],
    "ratio_db": ["dB"],
}

_FAMILY_STEPS = {  # unit → (family, exponent step to family base)
    "bytes_per_sec": ("GB/s", 1000.0),
    "bits_per_sec": ("Gbps", 1000.0),
    "energy": ("kWh", 1000.0),
    "power": ("W", 1000.0),
    "frequency": ("GHz", 1000.0),
    "length": ("m", 10.0),
    "pressure": ("MPa", 1000.0),
    "charge": ("mAh", 1000.0),
    "current": ("mA", 1000.0),
    "mass": ("g", 1000.0),
    "time": ("秒", 60.0),   # 分钟/小时 are 60-steps; 天/月/年 handled below
    "count": ("", 1.0),
}

_COUNT_SCALES = {"万": 1e4, "亿美元": 1e8, "万美元": 1e4, "亿": 1e8,
                 "million": 1e6, "billion": 1e9}
_TIME_SCALES = {"秒": 1.0, "分钟": 60.0, "小时": 3600.0, "天": 86400.0,
                "月": 2.628e6, "年": 3.154e7}

_FAMILY_BASE = {
    "bytes_per_sec": "GB/s", "bits_per_sec": "Gbps", "energy_density": None,
    "energy": "kWh", "power": "W", "frequency": "GHz", "length": "m",
    "pressure": "MPa", "charge": "mAh", "current": "mA", "mass": "g",
    "percent": "%", "temperature": "°C", "count": "#", "time": "s",
    "ratio_db": "dB",
}


def _unit_family(unit: str):
    """Map a raw unit string to (family, canonical_unit) — None if unknown.

    Bit/byte case is decided on the RAW string ('GB/s' bytes vs 'Gb/s' bits)
    BEFORE any case folding, so lowercasing can never conflate the two.
    """
    if not unit:
        return None, None
    fam, canon = None, unit
    for family, units in _UNIT_FAMILIES.items():
        for u in units:
            if u == unit:
                fam, canon = family, u
                break
        if fam:
            break
    if fam:  # exact (case-sensitive) match won
        return fam, canon
    # Fuzzy: match ignoring case EXCEPT the bits/bytes distinction.
    for family, units in _UNIT_FAMILIES.items():
        for u in units:
            if u.lower() == unit.strip().lower():
                # Preserve bit/byte split for the bps families.
                raw_bits = unit.strip().endswith("bps") or unit.strip().endswith("b/s")
                u_bits = u.endswith("bps") or u.endswith("b/s")
                if raw_bits == u_bits:
                    return family, u
                if family not in ("bytes_per_sec", "bits_per_sec"):
                    return family, u
    return None, unit


def _normalize_to_family_base(value: float, unit: str):
    """(normalized_value, normalized_unit, transform_rule_version) or None."""
    fam, canon = _unit_family(unit)
    if fam is None:
        return None
    base = _FAMILY_BASE.get(fam)
    if base is None:  # family without cross-unit normalization (e.g. Wh/kg)
        return (value, canon, TRANSFORM_RULE_VERSION)
    if fam == "count":
        scale = _COUNT_SCALES.get(canon, 1.0)
        return (value * scale, base, TRANSFORM_RULE_VERSION)
    if fam == "time":
        scale = _TIME_SCALES.get(canon, 1.0)
        return (value * scale, "s", TRANSFORM_RULE_VERSION)
    # Step-based families: position of canon relative to the BASE unit.
    units = _UNIT_FAMILIES[fam]
    try:
        idx = units.index(canon)
        base_idx = units.index(base)
    except ValueError:
        return (value, canon, TRANSFORM_RULE_VERSION)
    step = _FAMILY_STEPS.get(fam, (None, 1000.0))[1]
    return (value * (step ** (idx - base_idx)), base, TRANSFORM_RULE_VERSION)


def _iter_number_units(text: str):
    for m in _GENERIC_NUM_RE.finditer(text or ""):
        try:
            yield float(m.group(1)), m.group(2), m.group(0), m.start(), m.end()
        except ValueError:
            continue


_SCOPE_MARKERS = {
    "per_device": re.compile(r"单(?:GPU|卡|芯片|设备|器件|芯片组)|per[- ](?:GPU|device|chip)", re.IGNORECASE),
    "system_total": re.compile(r"整机|系统级?|aggregate|system[- ](?:total|level)|8[- ]?GPU|4[- ]?GPU|总带宽", re.IGNORECASE),
}


def _detect_scope_markers(text: str) -> set:
    found = set()
    for scope, pat in _SCOPE_MARKERS.items():
        if pat.search(text or ""):
            found.add(scope)
    return found


def extract_numeric_facts_with_source(text: str,
                                      record_id=None,
                                      source_snapshot_id=None,
                                      locator=None) -> List[dict]:
    """RT-022: numeric facts carrying evidence_ref into the immutable snapshot.

    Each fact: {record_id, metric, value, raw_value, unit, unit_family,
                normalized_value, normalized_unit, transform_rule_version,
                scope, evidence_ref: {record_id, source_snapshot_id, locator,
                                       exact_text}}
    """
    if not text:
        return []
    facts = []
    for value, unit, full, start, end in _iter_number_units(text):
        fam, canon = _unit_family(unit)
        norm = _normalize_to_family_base(value, unit)
        facts.append({
            "record_id": record_id,
            "metric": fam or "unknown",
            "value": value,
            "raw_value": full,
            "unit": canon,
            "unit_family": fam,
            "normalized_value": norm[0] if norm else None,
            "normalized_unit": norm[1] if norm else canon,
            "transform_rule_version": norm[2] if norm else None,
            "scope": next(iter(_detect_scope_markers(text[max(0, start - 40):end + 40]))) if _detect_scope_markers(text[max(0, start - 40):end + 40]) else "unknown",
            "evidence_ref": {
                "record_id": record_id,
                "source_snapshot_id": source_snapshot_id,
                "locator": locator,
                "exact_text": text[max(0, start - 30):min(len(text), end + 30)].strip(),
            },
        })
    return facts


def verify_numeric_claim(claim_text: str, evidence_text: str) -> dict:
    """RT-022: verify a claim's numbers against grounded evidence text.

    Returns:
        {"version", "status", "checked", "findings": [
            {"claim_value", "claim_unit", "evidence_value", "evidence_unit",
             "family", "result"}]}
    status ∈ MATCH | MISMATCH | SCOPE_MISMATCH | UNIT_FAMILY_MISMATCH |
              NO_EVIDENCE_NUMBER | NO_CLAIM_NUMBER
    Priority (worst wins): MISMATCH > SCOPE_MISMATCH > UNIT_FAMILY_MISMATCH >
    NO_EVIDENCE_NUMBER > MATCH.
    """
    claim_nums = [(v, u) for v, u, *_ in _iter_number_units(claim_text)]
    if not claim_nums:
        return {"version": NUMERIC_VERIFY_VERSION, "status": "NO_CLAIM_NUMBER",
                "checked": 0, "findings": []}

    ev_nums = [(v, u) for v, u, *_ in _iter_number_units(evidence_text)]
    if not ev_nums:
        return {"version": NUMERIC_VERIFY_VERSION, "status": "NO_EVIDENCE_NUMBER",
                "checked": len(claim_nums), "findings": []}

    claim_scopes = _detect_scope_markers(claim_text)
    ev_scopes = _detect_scope_markers(evidence_text)
    scope_conflict = bool(claim_scopes and ev_scopes
                          and claim_scopes != ev_scopes)

    findings = []
    worst = "MATCH"
    rank = {"MATCH": 0, "NO_EVIDENCE_NUMBER": 1, "UNIT_FAMILY_MISMATCH": 2,
            "SCOPE_MISMATCH": 3, "MISMATCH": 4}

    for cv, cu in claim_nums:
        cfam, ccanon = _unit_family(cu)
        cnorm = _normalize_to_family_base(cv, cu)
        # Prefer evidence numbers in the claim's family.
        candidates = []
        other_family = []
        for evv, evu in ev_nums:
            efam, ecanon = _unit_family(evu)
            if cfam is not None and efam == cfam:
                candidates.append((evv, ecanon, efam))
            else:
                other_family.append((evv, ecanon, efam))

        if not candidates:
            f = {"claim_value": cv, "claim_unit": ccanon,
                 "evidence_value": None, "evidence_unit": None,
                 "family": cfam,
                 "result": "UNIT_FAMILY_MISMATCH" if other_family
                           else "NO_EVIDENCE_NUMBER"}
            findings.append(f)
            if rank[f["result"]] > rank[worst]:
                worst = f["result"]
            continue

        best = None
        for evv, ecanon, efam in candidates:
            enorm = _normalize_to_family_base(evv, ecanon)
            if cnorm and enorm:
                diff = abs(cnorm[0] - enorm[0]) / max(abs(cnorm[0]), abs(enorm[0]), 1e-9)
                res = "MATCH" if diff <= 0.05 else "MISMATCH"
            else:
                res = "MATCH" if abs(cv - evv) <= 0.05 * max(abs(cv), abs(evv), 1e-9) else "MISMATCH"
            if best is None or (rank[res] < rank[best[2]]):
                best = (evv, ecanon, res, diff if cnorm and enorm else None)
        if best[2] == "MISMATCH" and scope_conflict:
            res = "SCOPE_MISMATCH"  # conflicting scope may explain the delta
        else:
            res = best[2]
        findings.append({"claim_value": cv, "claim_unit": ccanon,
                         "evidence_value": best[0], "evidence_unit": best[1],
                         "family": cfam, "result": res})
        if rank[res] > rank[worst]:
            worst = res

    return {"version": NUMERIC_VERIFY_VERSION, "status": worst,
            "checked": len(claim_nums), "findings": findings}
