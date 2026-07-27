#!/usr/bin/env python3
"""Analyze scoring system: AI精选 (aip=1) vs manual selections (lv>0)."""
import json
import statistics
from collections import Counter, defaultdict

with open('data/processed/all-records-lite.json') as f:
    data = json.load(f)

total = len(data)
print(f"=" * 70)
print(f"TOTAL RECORDS: {total}")
print(f"=" * 70)

# Helper: safely get nested score dimensions
def get_dims(r):
    scd = r.get('scd', {})
    return {
        'breakthrough': scd.get('b', None),
        'industry': scd.get('i', None),
        'rarity': scd.get('r', None),
        'data': scd.get('d', None),
        'timeliness': scd.get('t', None),
    }

def get_score(r):
    return r.get('sc', None)

def get_cat(r):
    return r.get('c', '未分类')

def get_topic(r):
    return r.get('tg', None)

# =========================================================================
# 1. BASIC COUNTS
# =========================================================================
lv_records = [r for r in data if r.get('lv', 0) > 0]
aip_records = [r for r in data if r.get('aip') == 1]
no_lv_no_aip = [r for r in data if r.get('lv', 0) == 0 and r.get('aip', 0) != 1]

print(f"\n--- BASIC COUNTS ---")
print(f"Records with lv>0 (manual selection): {len(lv_records)}")
print(f"Records with aip=1 (AI精选):          {len(aip_records)}")
print(f"Records with neither:                  {len(no_lv_no_aip)}")

# lv distribution
lv_dist = Counter(r.get('lv', 0) for r in data if r.get('lv', 0) > 0)
print(f"\nlv distribution:")
for lv in sorted(lv_dist.keys()):
    labels = {1: '精选', 2: '重点', 3: '预警'}
    print(f"  lv={lv} ({labels.get(lv, '?')}): {lv_dist[lv]}")

# =========================================================================
# 2. OVERLAP BETWEEN lv AND aip
# =========================================================================
lv_and_aip = [r for r in data if r.get('lv', 0) > 0 and r.get('aip') == 1]
lv_not_aip = [r for r in data if r.get('lv', 0) > 0 and r.get('aip', 0) != 1]
aip_not_lv = [r for r in data if r.get('lv', 0) == 0 and r.get('aip') == 1]

print(f"\n--- OVERLAP ANALYSIS ---")
print(f"lv>0 AND aip=1 (both manual + AI selected): {len(lv_and_aip)}")
print(f"lv>0 but NOT aip=1 (manual missed by AI):    {len(lv_not_aip)}")
print(f"aip=1 but NOT lv>0 (AI-only picks):          {len(aip_not_lv)}")

if len(lv_records) > 0:
    recall = len(lv_and_aip) / len(lv_records) * 100
    print(f"\nRecall (AI catches manual picks): {len(lv_and_aip)}/{len(lv_records)} = {recall:.1f}%")
if len(aip_records) > 0:
    precision_vs_lv = len(lv_and_aip) / len(aip_records) * 100
    print(f"Precision vs manual (AI picks that are also manual): {len(lv_and_aip)}/{len(aip_records)} = {precision_vs_lv:.1f}%")

# =========================================================================
# 3. SCORE DISTRIBUTION BY lv LEVEL
# =========================================================================
print(f"\n--- SCORE DISTRIBUTION BY lv LEVEL ---")
for lv_val in [1, 2, 3]:
    subset = [r for r in data if r.get('lv', 0) == lv_val]
    scores = [get_score(r) for r in subset if get_score(r) is not None]
    if scores:
        labels = {1: '精选', 2: '重点', 3: '预警'}
        print(f"\nlv={lv_val} ({labels[lv_val]}, n={len(subset)}, scored={len(scores)}):")
        print(f"  Mean:   {statistics.mean(scores):.2f}")
        print(f"  Median: {statistics.median(scores):.2f}")
        print(f"  Stdev:  {statistics.stdev(scores):.2f}" if len(scores) > 1 else "")
        print(f"  Min:    {min(scores):.1f}")
        print(f"  Max:    {max(scores):.1f}")
        # Quartiles
        sorted_s = sorted(scores)
        n = len(sorted_s)
        q25 = sorted_s[int(n * 0.25)]
        q75 = sorted_s[int(n * 0.75)]
        print(f"  Q25:    {q25:.1f}")
        print(f"  Q75:    {q75:.1f}")

# =========================================================================
# 4. SCORE DISTRIBUTION: aip vs non-aip
# =========================================================================
print(f"\n--- SCORE DISTRIBUTION: aip vs non-aip ---")
for label, subset in [("aip=1", aip_records), ("aip=0/none", [r for r in data if r.get('aip', 0) != 1])]:
    scores = [get_score(r) for r in subset if get_score(r) is not None]
    if scores:
        print(f"\n{label} (n={len(subset)}, scored={len(scores)}):")
        print(f"  Mean:   {statistics.mean(scores):.2f}")
        print(f"  Median: {statistics.median(scores):.2f}")
        print(f"  Min:    {min(scores):.1f}")
        print(f"  Max:    {max(scores):.1f}")

# =========================================================================
# 5. DIMENSION ANALYSIS FOR lv RECORDS
# =========================================================================
print(f"\n--- DIMENSION SCORES FOR lv>0 RECORDS ---")
dims_all = ['breakthrough', 'industry', 'rarity', 'data', 'timeliness']
for lv_val in [1, 2, 3]:
    subset = [r for r in data if r.get('lv', 0) == lv_val]
    print(f"\nlv={lv_val} dimension means:")
    for dim in dims_all:
        vals = [get_dims(r)[dim] for r in subset if get_dims(r)[dim] is not None]
        if vals:
            print(f"  {dim:15s}: mean={statistics.mean(vals):.2f}, median={statistics.median(vals):.2f}, min={min(vals):.1f}, max={max(vals):.1f}")

# =========================================================================
# 6. MANUAL PICKS MISSED BY AI (lv>0 but aip!=1)
# =========================================================================
print(f"\n--- MANUAL PICKS MISSED BY AI (lv>0 but NOT aip=1): {len(lv_not_aip)} ---")
missed_scores = [get_score(r) for r in lv_not_aip if get_score(r) is not None]
if missed_scores:
    print(f"Score distribution of missed manual picks:")
    print(f"  Mean: {statistics.mean(missed_scores):.2f}, Median: {statistics.median(missed_scores):.2f}")
    print(f"  Min: {min(missed_scores):.1f}, Max: {max(missed_scores):.1f}")
    # Score buckets
    buckets = [(0, 5), (5, 5.5), (5.5, 6), (6, 6.5), (6.5, 7), (7, 7.5), (7.5, 8), (8, 10)]
    print(f"  Score bucket distribution:")
    for lo, hi in buckets:
        count = sum(1 for s in missed_scores if lo <= s < hi)
        pct = count / len(missed_scores) * 100
        bar = '#' * int(pct / 2)
        print(f"    [{lo:.1f}, {hi:.1f}): {count:4d} ({pct:5.1f}%) {bar}")

# By category for missed picks
print(f"\n  Missed picks by category:")
cat_missed = Counter(get_cat(r) for r in lv_not_aip)
for cat, cnt in cat_missed.most_common(15):
    print(f"    {cat}: {cnt}")

# By lv level for missed
print(f"\n  Missed picks by lv level:")
for lv_val in [1, 2, 3]:
    cnt = sum(1 for r in lv_not_aip if r.get('lv') == lv_val)
    labels = {1: '精选', 2: '重点', 3: '预警'}
    total_lv = len([r for r in data if r.get('lv') == lv_val])
    pct = cnt / total_lv * 100 if total_lv > 0 else 0
    print(f"    lv={lv_val} ({labels[lv_val]}): {cnt}/{total_lv} missed ({pct:.1f}%)")

# =========================================================================
# 7. CATEGORY ANALYSIS
# =========================================================================
print(f"\n--- CATEGORY ANALYSIS ---")
# Group categories at top level
def top_cat(c):
    if not c or c == '未分类':
        return '未分类/不相关'
    parts = c.split('-')
    return parts[0] if len(parts) > 0 else c

print(f"\nlv>0 records by top-level category:")
cat_lv = Counter(top_cat(get_cat(r)) for r in lv_records)
for cat, cnt in cat_lv.most_common():
    print(f"  {cat}: {cnt}")

print(f"\naip=1 records by top-level category:")
cat_aip = Counter(top_cat(get_cat(r)) for r in aip_records)
for cat, cnt in cat_aip.most_common():
    print(f"  {cat}: {cnt}")

# =========================================================================
# 8. LOW-SCORING MANUAL PICKS (lv=1 but score < threshold)
# =========================================================================
print(f"\n--- LOW-SCORING lv=1 PICKS (精选 with low scores) ---")
lv1_low = [r for r in data if r.get('lv') == 1 and get_score(r) is not None and get_score(r) < 6.5]
print(f"lv=1 (精选) with score < 6.5: {len(lv1_low)}")
if lv1_low:
    lv1_low_scores = [get_score(r) for r in lv1_low]
    print(f"  Score range: {min(lv1_low_scores):.1f} - {max(lv1_low_scores):.1f}")
    print(f"  Mean: {statistics.mean(lv1_low_scores):.2f}")
    # Show dimension breakdown
    print(f"  Dimension means for low-scoring lv=1:")
    for dim in dims_all:
        vals = [get_dims(r)[dim] for r in lv1_low if get_dims(r)[dim] is not None]
        if vals:
            print(f"    {dim:15s}: mean={statistics.mean(vals):.2f}")

# =========================================================================
# 9. AI PICKS NOT IN MANUAL (aip=1 but lv=0) - what scores do they get?
# =========================================================================
print(f"\n--- AI-ONLY PICKS (aip=1, lv=0): {len(aip_not_lv)} ---")
aip_only_scores = [get_score(r) for r in aip_not_lv if get_score(r) is not None]
if aip_only_scores:
    print(f"  Mean: {statistics.mean(aip_only_scores):.2f}, Median: {statistics.median(aip_only_scores):.2f}")
    print(f"  Min: {min(aip_only_scores):.1f}, Max: {max(aip_only_scores):.1f}")

# =========================================================================
# 10. CONFUSION MATRIX STYLE ANALYSIS
# =========================================================================
print(f"\n--- THRESHOLD SIMULATION ---")
print(f"If threshold for aip were different, how would recall change?")
# Simulate different thresholds
for threshold in [5.0, 5.5, 6.0, 6.5, 7.0, 7.5]:
    would_be_aip = [r for r in data if get_score(r) is not None and get_score(r) >= threshold]
    lv_caught = [r for r in would_be_aip if r.get('lv', 0) > 0]
    if len(lv_records) > 0:
        recall = len(lv_caught) / len(lv_records) * 100
    else:
        recall = 0
    total_picked = len(would_be_aip)
    precision = len(lv_caught) / total_picked * 100 if total_picked > 0 else 0
    print(f"  Threshold >= {threshold:.1f}: would select {total_picked:6d} records, "
          f"catch {len(lv_caught):4d}/{len(lv_records)} manual ({recall:5.1f}%), "
          f"precision {precision:.1f}%")

# =========================================================================
# 11. DIMENSION ANALYSIS: What distinguishes manual picks from the rest?
# =========================================================================
print(f"\n--- DIMENSION ANALYSIS: manual picks vs non-manual ---")
for dim in dims_all:
    lv_vals = [get_dims(r)[dim] for r in lv_records if get_dims(r)[dim] is not None]
    non_lv = [r for r in data if r.get('lv', 0) == 0 and get_score(r) is not None]
    non_vals = [get_dims(r)[dim] for r in non_lv if get_dims(r)[dim] is not None]
    if lv_vals and non_vals:
        print(f"  {dim:15s}: lv mean={statistics.mean(lv_vals):.2f} vs non-lv mean={statistics.mean(non_vals):.2f} "
              f"(diff={statistics.mean(lv_vals) - statistics.mean(non_vals):+.2f})")

# =========================================================================
# 12. TOPIC/TYPE PATTERNS IN MISSED PICKS
# =========================================================================
print(f"\n--- TOPIC PATTERNS IN MISSED MANUAL PICKS ---")
topic_missed = Counter(r.get('tg', '无') for r in lv_not_aip)
print(f"Top topics in missed manual picks:")
for topic, cnt in topic_missed.most_common(15):
    print(f"  {topic}: {cnt}")

# =========================================================================
# 13. SAMPLE LOW-SCORING lv=1 MISSED RECORDS
# =========================================================================
print(f"\n--- SAMPLE: lv=1 (精选) missed by AI, lowest scores ---")
lv1_missed = [r for r in lv_not_aip if r.get('lv') == 1 and get_score(r) is not None]
lv1_missed.sort(key=lambda r: get_score(r))
for r in lv1_missed[:10]:
    sc = get_score(r)
    dims = get_dims(r)
    cat = get_cat(r)
    print(f"  sc={sc:.1f} [{dims['breakthrough']:.0f},{dims['industry']:.0f},{dims['rarity']:.0f},{dims['data']:.0f},{dims['timeliness']:.0f}] "
          f"cat={cat[:30]} | {r.get('t', '')[:60]}")

# =========================================================================
# 14. CATEGORY-SPECIFIC THRESHOLD ANALYSIS
# =========================================================================
print(f"\n--- CATEGORY-SPECIFIC SCORE ANALYSIS ---")
# Zero carbon, AI, general tech categories
def cat_group(c):
    if not c:
        return 'other'
    if '零碳' in c or '碳' in c:
        return 'zero_carbon'
    if c.startswith('AI') or '人工智能' in c or 'AI' in c:
        return 'ai'
    if c == '不相关':
        return 'irrelevant'
    return 'general_tech'

for group_name in ['zero_carbon', 'ai', 'general_tech', 'irrelevant']:
    group_records = [r for r in data if cat_group(get_cat(r)) == group_name]
    group_lv = [r for r in group_records if r.get('lv', 0) > 0]
    group_aip = [r for r in group_records if r.get('aip') == 1]
    group_scores = [get_score(r) for r in group_records if get_score(r) is not None]
    group_lv_scores = [get_score(r) for r in group_lv if get_score(r) is not None]
    
    print(f"\n  {group_name}: {len(group_records)} total, {len(group_lv)} lv>0, {len(group_aip)} aip=1")
    if group_scores:
        print(f"    All scores: mean={statistics.mean(group_scores):.2f}")
    if group_lv_scores:
        print(f"    lv scores:  mean={statistics.mean(group_lv_scores):.2f}, min={min(group_lv_scores):.1f}, max={max(group_lv_scores):.1f}")
        # 10th percentile
        sorted_s = sorted(group_lv_scores)
        p10 = sorted_s[max(0, int(len(sorted_s) * 0.10))]
        p25 = sorted_s[int(len(sorted_s) * 0.25)]
        print(f"    lv p10={p10:.1f}, p25={p25:.1f}")

print(f"\n{'=' * 70}")
print(f"ANALYSIS COMPLETE")
print(f"{'=' * 70}")
