#!/usr/bin/env python3
"""Reclassify 71 unrelated excel-import records + generate summaries for all 682."""
import json, sys, os
sys.path.insert(0, '/home/rhett/tech-db-fresh')
os.chdir('/home/rhett/tech-db-fresh')
from auto_pipeline import classify_and_score, gen_summaries, log

D = json.load(open('data/processed/all-records-lite.json'))

# Step 1: Reclassify the 71 unrelated excel-import records
unrelated_indices = [i for i, r in enumerate(D) 
                     if r.get('source','') == 'excel-import' and r.get('c','') == '不相关']
print(f'Reclassifying {len(unrelated_indices)} unrelated records...', flush=True)
if unrelated_indices:
    unrelated_records = [D[i] for i in unrelated_indices]
    # Reset classification
    for r in unrelated_records:
        r['c'] = '未分类'
        r['tg'] = ''
    unrelated_records = classify_and_score(unrelated_records)
    for idx_in_list, global_idx in enumerate(unrelated_indices):
        D[global_idx] = unrelated_records[idx_in_list]
    # Check results
    still_unrelated = sum(1 for i in unrelated_indices if D[i].get('c','') == '不相关')
    print(f'After reclassify: {still_unrelated} still unrelated', flush=True)

# Step 2: Generate summaries for all excel-import records that don't have one
need_summary = [(i, r) for i, r in enumerate(D) 
                if r.get('source','') == 'excel-import' and not r.get('as','').strip()
                and r.get('c','') != '不相关']
print(f'Generating summaries for {len(need_summary)} records...', flush=True)
if need_summary:
    indices = [i for i, _ in need_summary]
    records = [D[i] for i in indices]
    records = gen_summaries(records)
    for idx_in_list, global_idx in enumerate(indices):
        D[global_idx] = records[idx_in_list]

# Save
with open('data/processed/all-records-lite.json', 'w', encoding='utf-8') as f:
    json.dump(D, f, ensure_ascii=False)

# Report
excel = [r for r in D if r.get('source','') == 'excel-import']
has_summary = sum(1 for r in excel if r.get('as','').strip())
unrelated = sum(1 for r in excel if r.get('c','') == '不相关')
print(f'\nFinal: {len(excel)} excel-import records', flush=True)
print(f'  has summary: {has_summary}', flush=True)
print(f'  unrelated: {unrelated}', flush=True)
print('DONE', flush=True)
