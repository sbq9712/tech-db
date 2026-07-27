#!/usr/bin/env python3
"""Translate all non-Chinese titles to Chinese using GLM"""
import json, subprocess, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed

LITE_PATH = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
BATCH = 30
WORKERS = 6

def is_chinese(text):
    # Only translate if title has ZERO Chinese characters (pure English/numbers/symbols)
    # Titles with even one Chinese char (e.g. "Cursor正开发Claude竞品") are left as-is
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def call_glm(prompt, timeout=120):
    try:
        r = subprocess.run(["hermes", "-z", prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
                          capture_output=True, text=True, timeout=timeout, cwd="/home/rhett")
        out = r.stdout.strip()
        if out.startswith('```'):
            lines = out.split('\n')
            out = '\n'.join(lines[1:])
            if out.endswith('```'): out = out[:-3].strip()
            if out.startswith('json'): out = out[4:].strip()
        m = re.search(r'[\[{].*[\]}]', out, re.S)
        if m: return m.group(0)
    except: pass
    return None

with open(LITE_PATH) as f:
    lite = json.load(f)

# Find non-Chinese titles (excluding 不相关)
targets = [(i, r) for i, r in enumerate(lite) if not is_chinese(r.get('t', '')) and r.get('c', '') != '不相关']
print(f"非中文标题需翻译: {len(targets)}", flush=True)

TRANSLATE_PROMPT = """将以下英文技术情报标题翻译成中文。保持技术术语准确，简洁专业。
只输出JSON数组：[{"id":0,"title":"中文标题"}]
待翻译：
"""

items = [{"id": i, "title": r.get('t', '')[:200]} for i, (idx, r) in enumerate(targets)]
batches = [items[i:i+BATCH] for i in range(0, len(items), BATCH)]
print(f"批次: {len(batches)}", flush=True)

results = {}
done = 0
start = time.time()
lock = None

def process_batch(batch):
    prompt = TRANSLATE_PROMPT + json.dumps(batch, ensure_ascii=False)
    raw = call_glm(prompt)
    if raw:
        try:
            arr = json.loads(raw)
            return {item['id']: item['title'] for item in arr if 'title' in item}
        except: pass
    return {}

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futures = {ex.submit(process_batch, b): b for b in batches}
    for f in as_completed(futures):
        try:
            results.update(f.result())
        except: pass
        done += 1
        if done % 20 == 0:
            elapsed = time.time() - start
            spd = done / max(elapsed, 1)
            eta = (len(batches) - done) / spd / 60 if spd > 0 else 999
            print(f"  [{done}/{len(batches)}] translated={len(results)} ETA={eta:.0f}m", flush=True)

# Apply translations - store original in 'ot' field
applied = 0
for bid, (idx, r) in enumerate(targets):
    if bid in results and results[bid].strip():
        lite[idx]['ot'] = r.get('t', '')  # keep original
        lite[idx]['t'] = results[bid]
        applied += 1

print(f"\n翻译完成: {applied}/{len(targets)}", flush=True)

with open(LITE_PATH, 'w') as f:
    json.dump(lite, f, ensure_ascii=False, separators=(',', ':'))

# Rebuild chunks
for r in lite:
    if r.get("as", "").strip(): r.pop("b", None)
    if r.get("lv", 0) == 0 and r.get("fb", "").strip(): r.pop("fb", None)
for i in range(18):
    s=i*3000; e=min(s+3000,len(lite)); chunk=lite[s:e] if s<len(lite) else []
    c='window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push('+json.dumps(chunk,ensure_ascii=False,separators=(",",":"))+');'
    with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js","w") as f:
        f.write(c)

# Stats
still_non_cn = sum(1 for r in lite if not is_chinese(r.get('t', '')) and r.get('c', '') != '不相关')
print(f"剩余非中文标题: {still_non_cn}", flush=True)
print("Done", flush=True)
