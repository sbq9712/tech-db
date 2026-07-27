#!/usr/bin/env python3
"""逐条翻译顽固非中文标题"""
import json, subprocess, re
from concurrent.futures import ThreadPoolExecutor, as_completed

LITE_PATH = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"

def is_chinese(text):
    # Only translate if title has ZERO Chinese characters
    return bool(re.search(r'[\u4e00-\u9fff]', text))

with open(LITE_PATH) as f:
    lite = json.load(f)

targets = [(i, r) for i, r in enumerate(lite) if not is_chinese(r.get('t', '')) and r.get('c', '') != '不相关']
print(f'顽固记录: {len(targets)}', flush=True)

def translate_one(idx_title):
    idx, title = idx_title
    try:
        prompt = f'将以下英文技术标题翻译成简洁中文。只输出中文标题，不要输出任何其他内容。\n{title[:120]}'
        r = subprocess.run(['hermes','-z',prompt,'--provider','zai','-m','glm-5.2','--cli'],
                          capture_output=True, text=True, timeout=45, cwd='/home/rhett')
        out = r.stdout.strip()
        for prefix in ['以下是', '翻译']:
            if out.startswith(prefix): out = out[len(prefix):]
        out = out.strip('"\' ')
        lines = [l.strip() for l in out.split('\n') if l.strip() and not l.strip().startswith('```')]
        if lines:
            return (idx, lines[0][:100])
    except: pass
    return (idx, None)

results = {}
done = 0
with ThreadPoolExecutor(max_workers=6) as ex:
    futures = {ex.submit(translate_one, (idx, r.get('t',''))): idx for idx, r in targets}
    for f in as_completed(futures):
        idx, trans = f.result()
        if trans and is_chinese(trans):
            results[idx] = trans
        done += 1
        if done % 30 == 0: print(f'  [{done}/{len(targets)}] got={len(results)}', flush=True)

applied = 0
for idx, trans in results.items():
    lite[idx]['ot'] = lite[idx].get('t', '')
    lite[idx]['t'] = trans
    applied += 1

print(f'翻译: {applied}/{len(targets)}', flush=True)

with open(LITE_PATH, 'w') as f:
    json.dump(lite, f, ensure_ascii=False, separators=(',', ':'))

for r in lite:
    if r.get('as', '').strip(): r.pop('b', None)
    if r.get('lv', 0) == 0 and r.get('fb', '').strip(): r.pop('fb', None)
for i in range(18):
    s=i*3000; e=min(s+3000,len(lite)); chunk=lite[s:e] if s<len(lite) else []
    c='window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push('+json.dumps(chunk,ensure_ascii=False,separators=(',',':'))+');'
    with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js","w") as f: f.write(c)

remaining = sum(1 for r in lite if not is_chinese(r.get('t', '')) and r.get('c', '') != '不相关')
print(f'最终剩余: {remaining}', flush=True)
