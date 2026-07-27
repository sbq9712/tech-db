#!/usr/bin/env python3
"""Build body index from news + literature CSVs with auth, then merge into lite data."""
import json, subprocess, csv, io, os

# Load GH_TOKEN
gh_env = open('/home/rhett/.gh_env').read()
GH_TOKEN = ''
for line in gh_env.split('\n'):
    if 'GH_TOKEN' in line and '=' in line:
        GH_TOKEN = line.split('=',1)[1].strip().strip('"').strip("'")

REPOS = {
    'news': {'repo': 'sbq9712/news-spider', 'path': 'data'},
    'literature': {'repo': 'sbq9712/literature-rss-spider', 'path': 'output'},
}

body_by_url = {}

for name, info in REPOS.items():
    api_url = f"https://api.github.com/repos/{info['repo']}/contents/{info['path']}"
    result = subprocess.run(['curl', '-4', '-sS', '-m', '15',
        '-H', f'Authorization: token {GH_TOKEN}', api_url],
        capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            print(f'{name}: API error: {data.get("message","")}', flush=True)
            continue
        files = [f['name'] for f in data if f['name'].endswith('.csv')]
    except Exception:
        print(f'{name}: failed to parse API response', flush=True)
        continue
    
    print(f'{name}: {len(files)} CSVs', flush=True)
    for i, fname in enumerate(files):
        raw_url = f"https://raw.githubusercontent.com/{info['repo']}/main/{info['path']}/{fname}"
        result = subprocess.run(['curl', '-4', '-sS', '-m', '30', raw_url],
                               capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip() or '404' in result.stdout[:20]:
            continue
        try:
            reader = csv.DictReader(io.StringIO(result.stdout))
            for row in reader:
                body = (row.get('content','') or row.get('abstract','') or row.get('body','') or '').strip()
                if not body: continue
                u = (row.get('url','') or row.get('link','') or '').strip()
                if u and u not in body_by_url:
                    body_by_url[u] = body
        except Exception:
            pass
        if (i+1) % 50 == 0:
            print(f'  {name}: {i+1}/{len(files)}, index: {len(body_by_url)}', flush=True)
    print(f'{name} done: {len(body_by_url)} urls', flush=True)

# Save index
with open('/tmp/body-index.json', 'w', encoding='utf-8') as f:
    json.dump(body_by_url, f, ensure_ascii=False)
print(f'Index saved: {len(body_by_url)} urls', flush=True)

# Merge into lite data
lite = json.load(open('/home/rhett/tech-db-fresh/data/processed/all-records-lite.json'))
restored_new = 0
untruncated = 0
for r in lite:
    u = (r.get('u','') or '').strip()
    if not u or u not in body_by_url: continue
    full = body_by_url[u]
    current = r.get('b','').strip()
    if not current:
        r['b'] = full
        restored_new += 1
    elif len(current) < len(full):
        r['b'] = full
        untruncated += 1

print(f'Restored: {restored_new} new, {untruncated} untruncated', flush=True)
has_b = sum(1 for r in lite if r.get('b','').strip())
print(f'Total with body: {has_b}/{len(lite)} ({has_b*100//len(lite)}%)', flush=True)

with open('/home/rhett/tech-db-fresh/data/processed/all-records-lite.json', 'w', encoding='utf-8') as f:
    json.dump(lite, f, ensure_ascii=False)
print('Lite data saved', flush=True)
