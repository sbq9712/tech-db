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
