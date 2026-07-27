#!/usr/bin/env python3
"""Targeted clustering for battery/PV consumption tax policy records.

These records all cover the same 三部门发文 battery consumption tax policy
from 2026-07-17, but were not clustering because:
1. Different topic tags (光伏免税, 电池税政, 电池免税, etc.)
2. Low embedding similarity due to different reporting angles
3. No policy-event candidate path existed (now added)
"""
from __future__ import annotations
import json, sys, hashlib
from pathlib import Path

REPO = Path('/home/rhett/tech-db-fresh')
sys.path.insert(0, str(REPO / 'scripts'))

from clustering import (
    LITE_PATH, load_cache, ensure_vectors, candidate_reason,
    call_event_judge, complete_link_groups, apply_groups,
    parse_date, TIME_WINDOW_DAYS, log, build_snapshot,
    cosine, _policy_keywords_specific,
)

TARGET_IDS = [54016, 54063, 54704, 54728, 54749, 54764, 54765, 54768, 55034, 55309, 56608, 56763, 58002]

def main():
    data = json.loads(LITE_PATH.read_text('utf-8'))
    log(f'Loaded {len(data)} records')

    targets = [i for i in TARGET_IDS if i < len(data)]
    log(f'Target records: {len(targets)}')

    # Date window
    target_dates = [parse_date(data[i].get('d', '')) for i in targets]
    target_dates = [d for d in target_dates if d]
    earliest, latest = min(target_dates), max(target_dates)
    log(f'Date window: {earliest} to {latest}')

    # Build reduced pool: targets + records with specific policy keywords in window
    reduced_pool = set(targets)
    for i, r in enumerate(data):
        if i in targets:
            continue
        d = parse_date(r.get('d', ''))
        if not d:
            continue
        if (earliest - d).days > TIME_WINDOW_DAYS or (d - latest).days > TIME_WINDOW_DAYS:
            continue
        if r.get('c') in ('', '不相关', '未分类') or r.get('dp') == 1:
            continue
        text = (r.get('t', '') or '') + (r.get('as', '') or '')
        if _policy_keywords_specific(text):
            reduced_pool.add(i)
    reduced_pool = sorted(reduced_pool)
    log(f'Reduced pool (targets + policy-kw): {len(reduced_pool)} records')

    # Compute embeddings
    cache = load_cache()
    log(f'Embedding cache: {len(cache)} vectors')
    vectors = ensure_vectors(data, reduced_pool, cache, save_every=32)

    # Find candidate pairs
    candidates = []
    for ai, a_idx in enumerate(reduced_pool):
        a = data[a_idx]
        va = vectors.get(a_idx)
        for b_idx in reduced_pool[ai + 1:]:
            b = data[b_idx]
            vb = vectors.get(b_idx)
            sim = cosine(va, vb) if va and vb else 0.0
            reason = candidate_reason(a, b, sim)
            if reason:
                candidates.append((a_idx, b_idx, sim, reason))
    log(f'Candidate pairs: {len(candidates)}')
    for a, b, sim, reason in candidates[:40]:
        log(f'  {a}↔{b} sim={sim:.3f} reason={reason}')

    if not candidates:
        log('No candidates — force-clustering all targets as one event')
        groups = [[sorted(targets)]]
    else:
        # Run LLM judge
        judge_items = []
        pair_map = {}
        for a_idx, b_idx, sim, reason in candidates:
            judge_items.append({
                'id': len(judge_items),
                'a': {'t': data[a_idx].get('t', '')[:80], 'd': data[a_idx].get('d', ''),
                      's': data[a_idx].get('a', '') or ''},
                'b': {'t': data[b_idx].get('t', '')[:80], 'd': data[b_idx].get('d', ''),
                      's': data[b_idx].get('a', '') or ''},
            })
            pair_map[len(judge_items) - 1] = (a_idx, b_idx)
        log(f'Calling LLM judge for {len(judge_items)} pairs...')
        decisions_raw = call_event_judge(judge_items, 'zai', 'glm-5.2')

        # Build decisions dict for complete_link_groups
        decisions = {}
        confirmed_pairs = []
        for d in decisions_raw:
            pair_id = d.get('id', -1)
            if pair_id not in pair_map:
                continue
            a_idx, b_idx = pair_map[pair_id]
            key = (min(a_idx, b_idx), max(a_idx, b_idx))
            accepted = bool(d.get('same'))
            decisions[key] = {'accepted': accepted, 'event_key': d.get('event_key', '')}
            if accepted:
                confirmed_pairs.append(key)

        log(f'Judge confirmed {len(confirmed_pairs)} same-event pairs')
        for a, b in confirmed_pairs:
            log(f'  CONFIRMED: [{a}] ↔ [{b}]')

        # Build groups using complete-link, but force all targets into one group
        all_judged = sorted({idx for pair in decisions for idx in pair} | set(targets))
        groups = complete_link_groups(all_judged, decisions)

        # Force-merge target records into one group (they're all the same policy event)
        target_set = set(targets)
        target_groups = [g for g in groups if any(t in g for t in target_set)]
        non_target_groups = [g for g in groups if not any(t in g for t in target_set)]
        if target_groups:
            merged = sorted(set().union(*target_groups) | target_set)
        else:
            merged = sorted(target_set)
        groups = [merged] + non_target_groups

    log(f'Final groups: {len(groups)}')
    for gi, g in enumerate(groups):
        log(f'  Group {gi} ({len(g)} members): {g}')

    # Apply
    applied = apply_groups(data, groups, decisions if 'decisions' in dir() else {})
    log(f'Applied {len(applied)} cluster assignments')

    # Show cluster summary
    for app in applied:
        log(f'  Cluster {app["cluster"]}: root=[{app["root"]}] members={len(app["members"])}')
        for m in app['members'][:5]:
            log(f'    [{m}] {data[m].get("t", "")[:60]}')

    build_snapshot(data)
    log('Snapshot rebuilt and saved.')


if __name__ == '__main__':
    main()
