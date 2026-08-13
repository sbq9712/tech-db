#!/usr/bin/env python3
"""
Verify kp (key_params) and as (AI summary) fields against title+body.
Identifies records where kp/as content is mismatched (from other records).

Detection strategy (multi-signal):
  1. For kp: extract bracket terms, numbers-with-units, and key technical terms
     Check if they appear in the record's title+body+as
  2. For as: extract numbers and named entities
     Check overlap with title+body
  3. A record is "mismatched" if the overlap score is below threshold

Output: JSON file listing all mismatched record indices + stats
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
OUTPUT_PATH = REPO / "data" / "processed" / "kp_as_verification.json"


def extract_numbers(text):
    """Extract numeric values (with optional units) from text.
    Normalizes comma/space-separated digits: 6,000 → 6000, 16 000 → 16000.
    """
    # Remove commas/spaces between digits (iteratively for multiple separators)
    normalized = text
    for _ in range(5):  # max 5 iterations for numbers like 1,000,000
        new = re.sub(r'(\d)[,\s](\d{3}\b)', r'\1\2', normalized)
        if new == normalized:
            break
        normalized = new
    return set(re.findall(r'\d+\.?\d*', normalized))


def extract_english_terms(text):
    """Extract English acronyms, proper nouns, and chemical formulas."""
    terms = set()
    for m in re.findall(r'[A-Z][A-Za-z]{2,}', text):
        terms.add(m.lower())
    for m in re.findall(r'(?:Li|Na|Cu|Fe|Zn|Ni|Co|Mn|Si|Ca|Ti|Sn|Mo|Al|Ga|Ge|Se|Zr|Nb|Ta|Re|Ir|Os|Pt|Ru|Pd|Cr|V|W|Sb|Bi|In|Mg|Ba|Sr|Be|B|C|N|O|F|P|S|Cl|Br|I|H|He|Ne|Ar|Kr|Xe|Rn|Cs|Rb|Fr|Ra|Ac|Th|Pa|U|Np|Pu|Am|Cm|Bk|Cf|Es|Fm|Md|No|Lr|Rf|Db|Sg|Bh|Hs|Mt|Ds|Rg|Cn|Nh|Fl|Mc|Lv|Ts|Og|La|Ce|Pr|Nd|Pm|Sm|Eu|Gd|Tb|Dy|Ho|Er|Tm|Yb|Lu|Hf|Rh|Ag|Cd|Te|Xe|Tl|Pb|Po|At|Y)\w{1,}', text):
        if len(m) >= 3:
            terms.add(m)
    return terms


def extract_kp_terms(kp_list):
    """Extract meaningful terms from key_params list."""
    terms = set()
    numbers = set()
    for param in kp_list:
        param_str = str(param)
        # Extract bracket terms [xxx]
        for bracket in re.findall(r'\[([^\]]+)\]', param_str):
            # Split by common separators and take meaningful parts
            for part in re.split(r'[/、,，；;]', bracket):
                part = part.strip()
                if len(part) >= 2:
                    terms.add(part)
        # Extract prefix before bracket
        prefix = param_str.split('[')[0].strip()
        if prefix and len(prefix) >= 2:
            terms.add(prefix)
        # Extract numbers
        numbers.update(extract_numbers(param_str))
        # Extract English/chemical terms
        terms.update(extract_english_terms(param_str))
    return terms, numbers


def check_kp_match(record):
    """Check if kp content matches the record. Returns (is_match, score, detail)."""
    kp = record.get('kp', [])
    if not kp:
        return True, 1.0, "no kp"
    
    title = record.get('t', '')
    body = record.get('b', '') or record.get('fb', '') or ''
    # Note: deliberately exclude 'as' from combined since it may be contaminated
    combined = f"{title} {body}"
    combined_lower = combined.lower()
    
    kp_terms, kp_numbers = extract_kp_terms(kp)
    
    if not kp_terms and not kp_numbers:
        return True, 1.0, "empty kp terms"
    
    # Check term overlap
    body_terms = set()
    for cn in re.findall(r'[一-鿿]{2,}', combined):
        body_terms.add(cn)
    body_terms.update(extract_english_terms(combined))
    
    body_numbers = extract_numbers(combined)
    
    # Term matching: check if kp terms appear in body
    term_hits = 0
    term_total = 0
    for term in kp_terms:
        # For Chinese terms: check if any 2+ char substring appears
        if re.match(r'^[一-鿿]+$', term):
            # Check 2-char substrings
            substrings = [term[i:i+2] for i in range(len(term)-1)] if len(term) > 2 else [term]
            if any(sub in combined for sub in substrings):
                term_hits += 1
            term_total += 1
        else:
            # English/chemical term
            if term.lower() in combined_lower:
                term_hits += 1
            term_total += 1
    
    term_score = term_hits / term_total if term_total > 0 else 1.0
    
    # Number matching: numbers in kp should appear in body
    if kp_numbers:
        # Filter out very common numbers (years, 0, 1)
        current_year = datetime.now().year
        year_filter = {str(y) for y in range(current_year - 2, current_year + 3)}
        significant_nums = {n for n in kp_numbers if n not in ('0', '1') and n not in year_filter}
        if significant_nums:
            num_hits = len(significant_nums & body_numbers)
            num_score = num_hits / len(significant_nums)
        else:
            num_score = 1.0
    else:
        num_score = 1.0
    
    # Combined score: term_score is primary, num_score is secondary
    score = 0.6 * term_score + 0.4 * num_score
    
    is_match = score >= 0.4  # Threshold: at least 40% overlap
    
    detail = f"term={term_score:.2f}({term_hits}/{term_total}) num={num_score:.2f} score={score:.2f}"
    return is_match, score, detail


def extract_cn_bigrams(text):
    """Extract Chinese 2-char sliding window bigrams from contiguous CJK segments."""
    bigrams = set()
    for seg in re.findall(r'[一-鿿]+', text):
        if len(seg) >= 2:
            for i in range(len(seg) - 1):
                bigrams.add(seg[i:i+2])
    return bigrams


def check_as_match(record):
    """Check if AI summary matches the record. Returns (is_match, score, detail).

    Strategy: Use bigram PRECISION — what fraction of the title's Chinese bigrams
    appear in the AS? A correct AS should contain many title bigrams. A contaminated
    AS (from a different record) will have near-zero title bigram overlap.

    For English titles where bigram matching isn't applicable, fall back to
    English entity overlap and number overlap checks.

    Thresholds:
    - Chinese titles with ≥6 bigrams: precision < 0.10 → mismatch
    - English entity check: if title has ≥2 English entities and 0 appear in AS → mismatch
    - Number check: supplementary signal
    """
    summary = record.get('as', '').strip()
    if not summary:
        return True, 1.0, "no summary"

    title = record.get('t', '')
    body = record.get('b', '') or record.get('fb', '') or ''

    if not body:
        # Title-only records: skip strict check (Phase 1 generates from title)
        return True, 1.0, "title-only"

    # Signal 1: Chinese bigram precision
    title_bigrams = extract_cn_bigrams(title)
    sum_bigrams = extract_cn_bigrams(summary)

    cn_precision = None
    if len(title_bigrams) >= 6:
        hits = len(title_bigrams & sum_bigrams)
        cn_precision = hits / len(title_bigrams)

    # Signal 2: English entity overlap (title entities in AS)
    title_ents = extract_english_terms(title)
    sum_lower = summary.lower()
    en_precision = None
    if title_ents:
        en_hits = sum(1 for e in title_ents if e in sum_lower)
        en_precision = en_hits / len(title_ents)

    # Signal 3: Number overlap (body numbers in AS)
    body_combined = f"{title} {body}"
    body_nums = extract_numbers(body_combined)
    sum_nums = extract_numbers(summary)
    sig_body_nums = {n for n in body_nums if n not in ('0', '1')}
    sig_sum_nums = {n for n in sum_nums if n not in ('0', '1')}
    num_precision = None
    if sig_sum_nums:
        num_precision = len(sig_sum_nums & sig_body_nums) / len(sig_sum_nums)

    # Decision logic: only flag as mismatch when ALL available signals are low.
    # This reduces false positives from cases where CN bigrams don't match
    # (e.g., formal name vs abbreviation: 国际应用系统分析研究所 vs IIASA)
    # but other signals (entities, numbers) confirm the AS is from the same record.
    is_mismatch = False
    reasons = []

    mismatch_signals = 0
    total_signals = 0

    if cn_precision is not None:
        total_signals += 1
        if cn_precision < 0.10:
            mismatch_signals += 1
            reasons.append(f"cn_prec={cn_precision:.2f}")

    if en_precision is not None and len(title_ents) >= 2:
        total_signals += 1
        if en_precision < 0.10:
            mismatch_signals += 1
            reasons.append(f"en_prec={en_precision:.2f}")

    if num_precision is not None and sig_sum_nums:
        total_signals += 1
        if num_precision < 0.10:
            mismatch_signals += 1
            reasons.append(f"num_prec={num_precision:.2f}")

    # Flag as mismatch only if ALL available signals are low
    # (requires at least 2 signals to avoid single-signal false positives)
    if total_signals >= 2 and mismatch_signals == total_signals:
        is_mismatch = True
    elif total_signals == 1 and mismatch_signals == 1:
        # Only one signal — be cautious
        if cn_precision is not None and len(title_bigrams) >= 10:
            # Strong CN signal: title has many bigrams, none in AS
            is_mismatch = True

    # Overall score for reporting
    scores = []
    for name, val in [('cn_prec', cn_precision), ('en_prec', en_precision), ('num_prec', num_precision)]:
        if val is not None:
            scores.append(val)
    overall = sum(scores) / len(scores) if scores else 1.0

    detail_parts = []
    if cn_precision is not None:
        detail_parts.append(f"cn={cn_precision:.2f}")
    if en_precision is not None:
        detail_parts.append(f"en={en_precision:.2f}")
    if num_precision is not None:
        detail_parts.append(f"num={num_precision:.2f}")
    detail_parts.append(f"score={overall:.2f}")

    detail = ' '.join(detail_parts)
    if reasons:
        detail += f" [{'+ '.join(reasons)}]"

    return not is_mismatch, overall, detail


def main():
    print("=" * 60)
    print("  KP/AS Verification Scan")
    print("=" * 60)
    
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"Total records: {len(data)}")
    
    kp_stats = {"total": 0, "match": 0, "mismatch": 0, "no_kp": 0}
    as_stats = {"total": 0, "match": 0, "mismatch": 0, "no_as": 0, "title_only": 0}
    
    kp_mismatched = []
    as_mismatched = []
    
    for i, r in enumerate(data):
        # Check kp
        kp = r.get('kp', [])
        if not kp:
            kp_stats["no_kp"] += 1
        else:
            kp_stats["total"] += 1
            is_match, score, detail = check_kp_match(r)
            if is_match:
                kp_stats["match"] += 1
            else:
                kp_stats["mismatch"] += 1
                kp_mismatched.append({
                    "idx": i,
                    "title": r.get('t', '')[:80],
                    "score": round(score, 3),
                    "detail": detail,
                    "kp_sample": [str(p)[:80] for p in kp[:2]],
                })
        
        # Check as
        summary = r.get('as', '').strip()
        body = r.get('b', '') or r.get('fb', '') or ''
        if not summary:
            as_stats["no_as"] += 1
        else:
            as_stats["total"] += 1
            if not body:
                as_stats["title_only"] += 1
                # Title-only records use a more lenient check
            
            is_match, score, detail = check_as_match(r)
            if is_match:
                as_stats["match"] += 1
            else:
                as_stats["mismatch"] += 1
                as_mismatched.append({
                    "idx": i,
                    "title": r.get('t', '')[:80],
                    "score": round(score, 3),
                    "detail": detail,
                    "as_sample": summary[:120],
                })
    
    # Report
    print(f"\n--- KP (key_params) Results ---")
    print(f"  No kp:        {kp_stats['no_kp']}")
    print(f"  Has kp:       {kp_stats['total']}")
    print(f"  Match:        {kp_stats['match']} ({100*kp_stats['match']/max(kp_stats['total'],1):.1f}%)")
    print(f"  Mismatched:   {kp_stats['mismatch']} ({100*kp_stats['mismatch']/max(kp_stats['total'],1):.1f}%)")
    
    print(f"\n--- AS (AI summary) Results ---")
    print(f"  No as:        {as_stats['no_as']}")
    print(f"  Has as:       {as_stats['total']} (incl {as_stats['title_only']} title-only)")
    print(f"  Match:        {as_stats['match']} ({100*as_stats['match']/max(as_stats['total'],1):.1f}%)")
    print(f"  Mismatched:   {as_stats['mismatch']} ({100*as_stats['mismatch']/max(as_stats['total'],1):.1f}%)")
    
    # Save results
    output = {
        "kp": {
            "total_with_kp": kp_stats["total"],
            "match": kp_stats["match"],
            "mismatch": kp_stats["mismatch"],
            "mismatch_rate": round(kp_stats["mismatch"] / max(kp_stats["total"], 1), 4),
        },
        "as": {
            "total_with_as": as_stats["total"],
            "match": as_stats["match"],
            "mismatch": as_stats["mismatch"],
            "mismatch_rate": round(as_stats["mismatch"] / max(as_stats["total"], 1), 4),
        },
        "kp_mismatch_indices": [m["idx"] for m in kp_mismatched],
        "as_mismatch_indices": [m["idx"] for m in as_mismatched],
        "kp_mismatch_details": kp_mismatched[:50],  # Sample for review
        "as_mismatch_details": as_mismatched[:50],
    }
    
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")
    print(f"\nResults saved to: {OUTPUT_PATH}")
    print(f"KP mismatch indices: {len(kp_mismatched)} records need re-extraction")
    print(f"AS mismatch indices: {len(as_mismatched)} records need re-extraction")
    
    # Show some samples
    print(f"\n--- Sample KP mismatches ---")
    for m in kp_mismatched[:5]:
        print(f"  [{m['idx']}] score={m['score']} {m['detail']}")
        print(f"    Title: {m['title']}")
        print(f"    KP:    {m['kp_sample'][0] if m['kp_sample'] else '?'}")
    
    print(f"\n--- Sample AS mismatches ---")
    for m in as_mismatched[:5]:
        print(f"  [{m['idx']}] score={m['score']} {m['detail']}")
        print(f"    Title: {m['title']}")
        print(f"    AS:    {m['as_sample'][:80]}")


if __name__ == "__main__":
    main()
