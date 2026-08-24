"""
T008 — Provenance Clustering
=============================
Probabilistic clustering of records by origin/provenance to identify
"likely same source" content and enable independent evidence counting.

Multiple media reposts of the same original news should cluster together.
Document count ≠ evidence independence.

Output per record:
  same_origin_probability: float  (0-1)
  provenance_root_id: str         (cluster identifier)
  independent_group_id: str      (group for counting)
  provenance_confidence: str     (high/medium/low)
  provenance_reason: str         (why clustered or not)

Implementation:
  - Deterministic features: canonical URL, domain, publish time
  - Similarity features: title similarity, body text similarity
  - Configurable thresholds (not hardcoded magic numbers)
"""
import hashlib
import re
import os
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime


# ── Configurable thresholds ──
THRESHOLD_HIGH = float(os.environ.get("QA_PROVENANCE_THRESHOLD_HIGH", "0.85"))
THRESHOLD_MEDIUM = float(os.environ.get("QA_PROVENANCE_THRESHOLD_MEDIUM", "0.65"))
THRESHOLD_LOW = float(os.environ.get("QA_PROVENANCE_THRESHOLD_LOW", "0.40"))

# Title similarity threshold for "likely same article"
TITLE_SIM_THRESHOLD = float(os.environ.get("QA_PROVENANCE_TITLE_SIM", "0.80"))


def normalize_url(url: str) -> str:
    """Normalize URL for provenance comparison."""
    if not url:
        return ""
    url = url.strip().lower()
    # Remove tracking parameters
    parsed = urlparse(url)
    # Keep only essential parts
    clean = f"{parsed.netloc}{parsed.path}"
    # Remove trailing slash
    clean = clean.rstrip("/")
    # Remove common tracking params
    return clean


def get_domain(url: str) -> str:
    """Extract domain from URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower()
    except Exception:
        return ""


def normalize_title(title: str) -> str:
    """Normalize title for comparison (remove punctuation, lowercase)."""
    if not title:
        return ""
    # Remove common prefixes/suffixes
    t = re.sub(r"[【\[].*?[】\]]", "", title)  # Remove 【】[] brackets
    t = re.sub(r"[^\w\s]", "", t.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_similarity(t1: str, t2: str) -> float:
    """Calculate title similarity using character-level overlap."""
    import difflib
    n1 = normalize_title(t1)
    n2 = normalize_title(t2)
    if not n1 or not n2:
        return 0.0
    return difflib.SequenceMatcher(None, n1, n2).ratio()


def compute_provenance_similarity(r1: dict, r2: dict) -> tuple:
    """Compute provenance similarity between two records.

    Returns (same_origin_probability, reason).
    """
    reasons = []
    score_components = []

    # Feature 1: Same canonical URL (strongest signal)
    url1 = normalize_url(r1.get("u", ""))
    url2 = normalize_url(r2.get("u", ""))
    if url1 and url2 and url1 == url2:
        return (0.99, "exact_same_url")

    # Feature 2: Same domain + similar title
    domain1 = get_domain(r1.get("u", ""))
    domain2 = get_domain(r2.get("u", ""))
    title_sim = title_similarity(r1.get("t", ""), r2.get("t", ""))

    if domain1 and domain2 and domain1 == domain2:
        if title_sim >= TITLE_SIM_THRESHOLD:
            return (0.95, "same_domain_same_title")
        reasons.append(f"same_domain (title_sim={title_sim:.2f})")
        score_components.append(("same_domain", 0.3))

    if title_sim >= TITLE_SIM_THRESHOLD:
        reasons.append(f"very_similar_title ({title_sim:.2f})")
        score_components.append(("title_match", 0.4))

    # Feature 3: Publish date proximity
    d1 = r1.get("d", "")
    d2 = r2.get("d", "")
    if d1 and d2:
        try:
            dt1 = datetime.strptime(d1[:10], "%Y-%m-%d")
            dt2 = datetime.strptime(d2[:10], "%Y-%m-%d")
            day_diff = abs((dt1 - dt2).days)
            if day_diff == 0:
                reasons.append("same_date")
                score_components.append(("same_date", 0.2))
            elif day_diff <= 3:
                reasons.append(f"close_date ({day_diff}d)")
                score_components.append(("close_date", 0.1))
        except (ValueError, TypeError):
            pass

    # Feature 4: Body text similarity (if available)
    b1 = (r1.get("b", "") or r1.get("as", ""))[:500]
    b2 = (r2.get("b", "") or r2.get("as", ""))[:500]
    if b1 and b2:
        import difflib
        body_sim = difflib.SequenceMatcher(None, b1, b2).ratio()
        if body_sim >= 0.7:
            reasons.append(f"similar_body ({body_sim:.2f})")
            score_components.append(("body_match", 0.3))

    # Combine scores
    total_score = sum(w for _, w in score_components)
    total_score = min(1.0, total_score)

    if not reasons:
        return (0.0, "no_match")

    return (total_score, "; ".join(reasons))


def cluster_provenance(records: list, existing_clusters: dict = None) -> dict:
    """Cluster records by provenance.

    Args:
        records: List of record dicts
        existing_clusters: Existing provenance map {idx: cluster_id}

    Returns:
        {
            idx: {
                "provenance_root_id": str,
                "independent_group_id": str,
                "same_origin_probability": float,
                "provenance_confidence": str,
                "provenance_reason": str,
            }
        }
    """
    result = {}
    clusters = existing_clusters or {}
    next_cluster_id = max((int(v.split("-")[-1]) for v in clusters.values() if v.startswith("prov-")), default=0) + 1

    # Simple O(n²) clustering (fine for offline batch processing)
    # For production, would use blocking + approximate matching
    for i, rec in enumerate(records):
        if i in clusters:
            root = clusters[i]
            result[i] = {
                "provenance_root_id": root,
                "independent_group_id": root,
                "same_origin_probability": 1.0,
                "provenance_confidence": "high",
                "provenance_reason": "existing_cluster",
            }
            continue

        # Check against all previously assigned records
        best_match_idx = -1
        best_score = 0.0
        best_reason = ""

        for j in range(i):
            if j not in result:
                continue
            score, reason = compute_provenance_similarity(rec, records[j])
            if score > best_score:
                best_score = score
                best_match_idx = j
                best_reason = reason

        if best_score >= THRESHOLD_MEDIUM and best_match_idx >= 0:
            # Join existing cluster
            root = result[best_match_idx]["provenance_root_id"]
            confidence = "high" if best_score >= THRESHOLD_HIGH else "medium"
            result[i] = {
                "provenance_root_id": root,
                "independent_group_id": root,
                "same_origin_probability": round(best_score, 3),
                "provenance_confidence": confidence,
                "provenance_reason": best_reason,
            }
        else:
            # New cluster
            root = f"prov-{next_cluster_id}"
            next_cluster_id += 1
            result[i] = {
                "provenance_root_id": root,
                "independent_group_id": root,
                "same_origin_probability": 1.0,
                "provenance_confidence": "high",
                "provenance_reason": "unique_source",
            }

    return result


def count_independent_sources(record_indices: list, provenance_map: dict) -> int:
    """Count the number of independent source groups in a set of records."""
    groups = set()
    for idx in record_indices:
        info = provenance_map.get(idx)
        if info:
            groups.add(info.get("independent_group_id", f"unknown-{idx}"))
        else:
            groups.add(f"unique-{idx}")
    return len(groups)


# ── T048: Claim-level / span-level source lineage ─────────────────────────
# Document-level provenance says "this RECORD is a repost of X". Span-level
# lineage says "THIS evidence span quotes an official statement" vs "this
# span is the outlet's own reporting/testing" — the same article can contain
# both. Uncertainty is preserved (provenance_confidence), never flattened to
# a binary verdict.

# Attribution markers: a span whose text explicitly attributes its content
# to a primary source is quoted_primary, NOT the publisher's own reporting.
_ATTRIBUTION_MARKERS = (
    "新闻稿", "官方声明", "公告称", "在声明中表示", "根据官方", "公司表示",
    "新闻中心", "press release", "according to", "in a statement",
    "told reporters", "发布会上表示", "受访者表示",
)
# Own-reporting markers: outlet's own testing/interview/verification.
_OWN_REPORTING_MARKERS = (
    "本报记者", "本报测试", "实测", "我们的测试", "独立测试", "试驾", "评测",
    "our testing", "we tested", "our benchmark", "exclusive interview",
    "采访中透露", "独家采访",
)


def span_lineage(record: dict, provenance_entry: dict,
                 span_text: str = "") -> dict:
    """Compute span-level source role (T048).

    record: the lite record (t/source/domain fields) or its metadata
    provenance_entry: output of T008 clustering for this record
    span_text: the exact evidence span (markers inside the span win over
               document-level defaults)

    Returns lineage dict — attached to each claim support entry by
    claim_mapping so independence is counted per claim/span, and no
    media-quoting-official span is ever miscounted as independent
    verification.
    """
    span = span_text or ""
    publisher = (record.get("source") or record.get("source_domain")
                 or record.get("org") or "unknown")
    original = (provenance_entry.get("original_source_org")
                or provenance_entry.get("original_url")
                or None)
    doc_role = provenance_entry.get("evidence_role") or "unknown"

    quoted_primary = False
    own_reporting = False
    marker = None
    for m in _ATTRIBUTION_MARKERS:
        if m in span:
            quoted_primary, marker = True, m
            break
    if not quoted_primary:
        for m in _OWN_REPORTING_MARKERS:
            if m in span:
                own_reporting, marker = True, m
                break

    if quoted_primary:
        span_role = "quoted_primary_source"
    elif own_reporting:
        span_role = "independent_reporting"
    elif doc_role == "self_reported":
        span_role = "self_reported"
    elif doc_role == "independent":
        span_role = "independent_reporting"
    elif doc_role == "primary":
        span_role = "primary_statement"
    else:
        span_role = "unknown"

    confidence = provenance_entry.get("provenance_confidence", "low")
    if marker and confidence in ("high", "medium"):
        # explicit in-span marker beats document-level inference
        confidence = "high"

    return {
        "span_source_role": span_role,
        "quoted_primary_source": quoted_primary,
        "independent_reporting": own_reporting or span_role == "independent_reporting",
        "document_publisher": publisher,
        "document_role": doc_role,
        "provenance_root_id": provenance_entry.get("provenance_root_id"),
        "independent_group_id": provenance_entry.get("independent_group_id"),
        "same_origin_probability": provenance_entry.get("same_origin_probability", 0.0),
        "provenance_confidence": confidence,   # uncertainty preserved, not binary
        "lineage_marker": marker,
    }


def claim_independence_report(claims: list,
                              records_by_id: Optional[dict] = None,
                              provenance_map: Optional[dict] = None) -> dict:
    """T048: per-claim independence accounting for a claim mapping.

    For each claim, group its supporting evidence by independent_group_id,
    but quotes of a primary source do NOT add independence beyond the
    primary's own group (5 outlets quoting one press release = 1
    independent group, not 5). Returns a per-claim report plus a global
    summary the grader/verifier can consume.
    """
    records_by_id = records_by_id or {}
    provenance_map = provenance_map or {}
    per_claim = []
    for c in claims or []:
        cid = c.get("claim_id") or c.get("id")
        groups, roles = {}, []
        # claim_mapping emits "supported_by"; spec examples use "support"
        support = c.get("supported_by") or c.get("support") or []
        for s in support:
            rid = s.get("record_id")
            rec = records_by_id.get(rid, {})
            pm = provenance_map.get(rid, {})
            lin = s.get("span_lineage") or span_lineage(rec, pm,
                                                        s.get("evidence_span", ""))
            raw_gid = lin.get("independent_group_id")
            if raw_gid:
                gid = raw_gid
            elif rid in provenance_map:
                # An explicit pinned provenance entry with no known group is
                # UNKNOWN, not evidence that this record is independently
                # sourced. Collapse unknowns honestly rather than fabricating
                # one unique group per record. The record fallback remains
                # only for callers that supplied no provenance entry at all.
                gid = "__PROVENANCE_UNKNOWN__"
            else:
                gid = f"record:{rid}"
            entry = groups.setdefault(gid, {
                "group": gid,
                "roles": set(),
                "quoted_primary": False,
                "independent": False,
                "records": [],
            })
            entry["records"].append(rid)
            entry["roles"].add(lin.get("span_source_role", "unknown"))
            if lin.get("quoted_primary_source"):
                entry["quoted_primary"] = True
            if lin.get("independent_reporting") or \
                    lin.get("span_source_role") == "primary_statement":
                entry["independent"] = True
            roles.append(lin.get("span_source_role"))
        # a group counts as independent validation only if it contains
        # non-quoted, non-self-reported reporting (its own journalism/
        # testing) or is itself the primary source
        independent_groups = [g for g in groups.values() if g["independent"]]
        per_claim.append({
            "claim_id": cid,
            "support_count": len(support),
            "groups_total": len(groups),
            "independent_groups": len(independent_groups),
            "roles": roles,
            "groups": [{"group": g["group"], "roles": sorted(g["roles"]),
                        "quoted_primary": g["quoted_primary"],
                        "independent": g["independent"],
                        "records": g["records"]} for g in groups.values()],
            "independence_sufficient": len(independent_groups) >= 1,
        })
    return {
        "claims_total": len(per_claim),
        "claims_with_independent_support": sum(
            1 for p in per_claim if p["independence_sufficient"]),
        "per_claim": per_claim,
    }
