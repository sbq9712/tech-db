#!/usr/bin/env python3
"""Classify + Score + Summarize the Excel-imported records."""
import json, sys, os
sys.path.insert(0, '/home/rhett/tech-db-fresh')
os.chdir('/home/rhett/tech-db-fresh')

from auto_pipeline import classify_and_score, gen_summaries, translate_non_chinese_titles

D = json.load(open('data/processed/all-records-lite.json'))
indices = json.load(open('/tmp/excel-needs-classify.json'))
records = [D[i] for i in indices]

print(f'Processing {len(records)} records...', flush=True)
print('Step 1: Translate titles...', flush=True)
records = translate_non_chinese_titles(records)
print('Step 2: Classify + Score...', flush=True)
records = classify_and_score(records)
print('Step 3: Summaries...', flush=True)
records = gen_summaries(records)

# Write back
for idx_in_list, global_idx in enumerate(indices):
    D[global_idx] = records[idx_in_list]

with open('data/processed/all-records-lite.json', 'w', encoding='utf-8') as f:
    json.dump(D, f, ensure_ascii=False)
print('DONE', flush=True)
