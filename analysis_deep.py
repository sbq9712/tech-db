#!/usr/bin/env python3
"""Deep analysis: dimension-level patterns and formula optimization."""
import json
import statistics
from collections import Counter, defaultdict

with open('data/processed/all-records-lite.json') as f:
    data = json.load(f)

def get_dims(r):
    scd = r.get('scd', {})
    return (scd.get('b'), scd.get('i'), scd.get('r'), scd.get('d'), scd.get('t'))

def get_score(r):
    return r.get('sc')

lv_records = [r for r in data if r.get('lv', 0) > 0]
non_lv_scored = [r for r in data if r.get('lv', 0) == 0 and get_score(r) is not None]

print("=" * 70)
print("DEEP ANALYSIS: DIMENSION PATTERNS & FORMULA OPTIMIZATION")
print("=" * 70)

# =========================================================================
# 1. WEIGHT OPTIMIZATION: Find weights that best separate lv from non-lv
# =========================================================================
print("\n--- 1. WEIGHT OPTIMIZATION (Logistic-like separation) ---")

# Current weights: b=0.30, i=0.25, r=0.15, d=0.15, t=0.15
# Try different weight combinations and measure separation

def compute_weighted_score(r, weights):
    b, i, r_d, d, t = get_dims(r)
    if any(v is None for v in [b, i, r_d, d, t]):
        return None
    return (b * weights[0] + i * weights[1] + r_d * weights[2] + 
            d * weights[3] + t * weights[4])

def evaluate_weights(weights, threshold_options=None):
    """Measure how well these weights separate lv>0 from lv=0 records."""
    lv_scores = []
    non_scores = []
    for r in lv_records:
        s = compute_weighted_score(r, weights)
        if s is not None:
            lv_scores.append(s)
    for r in non_lv_scored:
        s = compute_weighted_score(r, weights)
        if s is not None:
            non_scores.append(s)
    
    if not lv_scores or not non_scores:
        return None
    
    lv_mean = statistics.mean(lv_scores)
    non_mean = statistics.mean(non_scores)
    separation = lv_mean - non_mean
    
    # Cohen's d effect size
    lv_std = statistics.stdev(lv_scores) if len(lv_scores) > 1 else 1
    non_std = statistics.stdev(non_scores) if len(non_scores) > 1 else 1
    pooled_std = ((lv_std**2 + non_std**2) / 2) ** 0.5
    cohens_d = separation / pooled_std if pooled_std > 0 else 0
    
    return {
        'lv_mean': lv_mean, 'non_mean': non_mean, 'separation': separation,
        'cohens_d': cohens_d, 'lv_scores': lv_scores, 'non_scores': non_scores
    }

weight_configs = [
    ("Current (b.30/i.25/r.15/d.15/t.15)", [0.30, 0.25, 0.15, 0.15, 0.15]),
    ("Boost timeliness (b.20/i.20/r.15/d.10/t.35)", [0.20, 0.20, 0.15, 0.10, 0.35]),
    ("Boost rarity+timeliness (b.20/i.20/r.25/d.10/t.25)", [0.20, 0.20, 0.25, 0.10, 0.25]),
    ("Boost industry+timeliness (b.15/i.30/r.15/d.10/t.30)", [0.15, 0.30, 0.15, 0.10, 0.30]),
    ("Balanced rarity+industry+timeliness (b.15/i.25/r.20/d.10/t.30)", [0.15, 0.25, 0.20, 0.10, 0.30]),
    ("Maximize separation (b.15/i.20/r.25/d.05/t.35)", [0.15, 0.20, 0.25, 0.05, 0.35]),
    ("De-emphasize breakthrough fully (b.10/i.25/r.20/d.10/t.35)", [0.10, 0.25, 0.20, 0.10, 0.35]),
]

print(f"\n{'Config':<55} {'lv_mean':>8} {'non_mean':>9} {'sep':>6} {'Cohen_d':>8}")
print("-" * 90)
for name, w in weight_configs:
    result = evaluate_weights(w)
    if result:
        print(f"{name:<55} {result['lv_mean']:8.2f} {result['non_mean']:9.2f} {result['separation']:6.2f} {result['cohens_d']:8.3f}")

# =========================================================================
# 2. RECALL AT DIFFERENT THRESHOLDS FOR BEST WEIGHT CONFIG
# =========================================================================
print("\n--- 2. RECALL/PRECISION FOR TOP WEIGHT CONFIGS ---")
best_weights = [0.15, 0.20, 0.25, 0.05, 0.35]  # Maximize separation
best_name = "MaxSep (b.15/i.20/r.25/d.05/t.35)"

for w_config, w_name in [(weight_configs[0][1], "Current"), (best_weights, best_name)]:
    print(f"\n  Weights: {w_name}")
    lv_s = [(compute_weighted_score(r, w_config) or 0) for r in lv_records]
    all_s = []
    for r in data:
        s = compute_weighted_score(r, w_config)
        if s is not None:
            all_s.append((s, r.get('lv', 0) > 0))
    
    for thresh in [5.0, 5.5, 6.0, 6.5, 7.0]:
        selected = [(s, is_lv) for s, is_lv in all_s if s >= thresh]
        caught = sum(1 for s, is_lv in selected if is_lv)
        recall = caught / len(lv_records) * 100
        precision = caught / len(selected) * 100 if selected else 0
        print(f"    thresh>={thresh}: select {len(selected):6d}, recall {recall:5.1f}%, precision {precision:.1f}%")

# =========================================================================
# 3. DIMENSION-LEVEL ANALYSIS: Which dimensions individually predict lv?
# =========================================================================
print("\n--- 3. SINGLE-DIMENSION PREDICTIVE POWER ---")
dim_names = ['breakthrough', 'industry', 'rarity', 'data', 'timeliness']
for idx, dim_name in enumerate(dim_names):
    lv_vals = [get_dims(r)[idx] for r in lv_records if get_dims(r)[idx] is not None]
    non_vals = [get_dims(r)[idx] for r in non_lv_scored if get_dims(r)[idx] is not None]
    
    lv_mean = statistics.mean(lv_vals)
    non_mean = statistics.mean(non_vals)
    
    # How many lv records have this dim >= 7?
    lv_high = sum(1 for v in lv_vals if v >= 7)
    non_high = sum(1 for v in non_vals if v >= 7)
    
    # If we used ONLY this dim >= 7 as threshold
    total_high = lv_high + non_high
    recall_single = lv_high / len(lv_records) * 100
    precision_single = lv_high / total_high * 100 if total_high > 0 else 0
    
    print(f"  {dim_name:15s}: lv_mean={lv_mean:.2f} non_mean={non_mean:.2f} diff={lv_mean-non_mean:+.2f} | "
          f"if dim>=7: recall={recall_single:4.1f}% prec={precision_single:4.1f}% (catch {lv_high}/{len(lv_records)})")

# =========================================================================
# 4. COMPOSITE SIGNALS: timeliness >= 7 AND rarity >= 6
# =========================================================================
print("\n--- 4. COMPOSITE SIGNAL ANALYSIS ---")
# Test composite rules
composite_rules = [
    ("timeliness >= 7", lambda r: get_dims(r)[4] is not None and get_dims(r)[4] >= 7),
    ("timeliness >= 7 AND rarity >= 5", lambda r: get_dims(r)[4] is not None and get_dims(r)[4] >= 7 and get_dims(r)[2] >= 5),
    ("timeliness >= 7 AND industry >= 5", lambda r: get_dims(r)[4] is not None and get_dims(r)[4] >= 7 and get_dims(r)[1] >= 5),
    ("rarity >= 7", lambda r: get_dims(r)[2] is not None and get_dims(r)[2] >= 7),
    ("timeliness >= 8", lambda r: get_dims(r)[4] is not None and get_dims(r)[4] >= 8),
    ("(timeliness >= 7 AND rarity >= 6) OR score >= 6.5", 
     lambda r: get_dims(r)[4] is not None and ((get_dims(r)[4] >= 7 and get_dims(r)[2] is not None and get_dims(r)[2] >= 6) or (get_score(r) is not None and get_score(r) >= 6.5))),
    ("(timeliness >= 7 AND industry >= 5) OR score >= 6.5",
     lambda r: get_dims(r)[4] is not None and ((get_dims(r)[4] >= 7 and get_dims(r)[1] is not None and get_dims(r)[1] >= 5) or (get_score(r) is not None and get_score(r) >= 6.5))),
]

for rule_name, rule_fn in composite_rules:
    selected_lv = sum(1 for r in lv_records if rule_fn(r))
    selected_non = sum(1 for r in non_lv_scored if rule_fn(r))
    total_sel = selected_lv + selected_non
    recall = selected_lv / len(lv_records) * 100
    precision = selected_lv / total_sel * 100 if total_sel > 0 else 0
    print(f"  {rule_name:<60} select={total_sel:6d} recall={recall:5.1f}% prec={precision:5.1f}%")

# =========================================================================
# 5. ANALYZE SPECIFIC TYPES OF MISSED RECORDS
# =========================================================================
print("\n--- 5. MISSED RECORD TYPE BREAKDOWN ---")
lv_not_aip = [r for r in data if r.get('lv', 0) > 0 and r.get('aip', 0) != 1]

# Classify missed records by type
review_count = sum(1 for r in lv_not_aip if '综述' in r.get('t', '') or 'review' in r.get('t', '').lower())
policy_count = sum(1 for r in lv_not_aip if '政策' in r.get('tg', '') or r.get('tg') == '政策监管')
breakthrough_count = sum(1 for r in lv_not_aip if r.get('tg') == '技术突破')
industry_count = sum(1 for r in lv_not_aip if r.get('tg') == '产业进展')

print(f"  Total missed: {len(lv_not_aip)}")
print(f"  技术突破 topic: {breakthrough_count} ({breakthrough_count/len(lv_not_aip)*100:.1f}%)")
print(f"  产业进展 topic: {industry_count} ({industry_count/len(lv_not_aip)*100:.1f}%)")
print(f"  政策监管 topic: {policy_count} ({policy_count/len(lv_not_aip)*100:.1f}%)")
print(f"  Title contains 综述/review: {review_count}")

# Score distribution of missed 技术突破 vs 产业进展
for topic in ['技术突破', '产业进展', '政策监管', '行业观察']:
    subset = [r for r in lv_not_aip if r.get('tg') == topic]
    scores = [get_score(r) for r in subset if get_score(r) is not None]
    if scores:
        print(f"\n  Missed {topic} (n={len(subset)}): mean={statistics.mean(scores):.2f}, median={statistics.median(scores):.2f}")

# =========================================================================
# 6. DIMENSION PROFILE: lv=1 vs lv=2 vs non-lv (radar chart data)
# =========================================================================
print("\n--- 6. DIMENSION PROFILES ---")
print(f"\n{'Group':<15} {'breakthrough':>13} {'industry':>10} {'rarity':>8} {'data':>8} {'timeliness':>11}")
print("-" * 70)
for label, subset in [("lv=1精选", [r for r in data if r.get('lv') == 1]),
                       ("lv=2重点", [r for r in data if r.get('lv') == 2]),
                       ("lv=3预警", [r for r in data if r.get('lv') == 3]),
                       ("non-lv", non_lv_scored)]:
    means = []
    for idx in range(5):
        vals = [get_dims(r)[idx] for r in subset if get_dims(r)[idx] is not None]
        means.append(statistics.mean(vals) if vals else 0)
    print(f"{label:<15} {means[0]:13.2f} {means[1]:10.2f} {means[2]:8.2f} {means[3]:8.2f} {means[4]:11.2f}")

# =========================================================================
# 7. PROPOSED HYBRID SCORING FORMULA
# =========================================================================
print("\n--- 7. PROPOSED HYBRID FORMULA EVALUATION ---")

# Proposed: reweighted score + bonus for high timeliness/rarity
def hybrid_score(r):
    b, i, r_d, d, t = get_dims(r)
    if any(v is None for v in [b, i, r_d, d, t]):
        return None
    # New base weights favoring timeliness and rarity
    base = b * 0.15 + i * 0.20 + r_d * 0.25 + d * 0.10 + t * 0.30
    # Bonus: if timeliness >= 8, add bonus
    timeliness_bonus = max(0, (t - 7)) * 0.15
    # Bonus: if rarity >= 7, add bonus  
    rarity_bonus = max(0, (r_d - 6)) * 0.10
    return base + timeliness_bonus + rarity_bonus

# Evaluate hybrid
print(f"\nHybrid formula: base(0.15b+0.20i+0.25r+0.10d+0.30t) + timeliness_bonus + rarity_bonus")
lv_hybrid = [(hybrid_score(r) or 0) for r in lv_records]
all_hybrid = []
for r in data:
    s = hybrid_score(r)
    if s is not None:
        all_hybrid.append((s, r.get('lv', 0) > 0))

lv_h = [s for s, _ in [(hybrid_score(r) or 0, r) for r in lv_records]]
non_h = [s for s, is_lv in all_hybrid if not is_lv]
print(f"  lv mean: {statistics.mean(lv_h):.2f}, non-lv mean: {statistics.mean(non_h):.2f}, separation: {statistics.mean(lv_h)-statistics.mean(non_h):+.2f}")

for thresh in [5.5, 6.0, 6.5, 7.0, 7.5]:
    selected = [(s, is_lv) for s, is_lv in all_hybrid if s >= thresh]
    caught = sum(1 for s, is_lv in selected if is_lv)
    recall = caught / len(lv_records) * 100
    precision = caught / len(selected) * 100 if selected else 0
    print(f"    thresh>={thresh}: select {len(selected):6d}, recall {recall:5.1f}%, precision {precision:.1f}%")

# =========================================================================
# 8. KEY INSIGHT: What fraction of lv records have timeliness >= 7?
# =========================================================================
print("\n--- 8. KEY SIGNAL: TIMELINESS DISTRIBUTION ---")
for label, subset in [("lv=1", [r for r in data if r.get('lv') == 1]),
                       ("lv=2", [r for r in data if r.get('lv') == 2]),
                       ("non-lv", non_lv_scored)]:
    t_vals = [get_dims(r)[4] for r in subset if get_dims(r)[4] is not None]
    ge7 = sum(1 for v in t_vals if v >= 7)
    ge8 = sum(1 for v in t_vals if v >= 8)
    print(f"  {label}: timeliness >= 7: {ge7}/{len(t_vals)} ({ge7/len(t_vals)*100:.1f}%), >= 8: {ge8}/{len(t_vals)} ({ge8/len(t_vals)*100:.1f}%)")

print("\n" + "=" * 70)
print("DEEP ANALYSIS COMPLETE")
print("=" * 70)
