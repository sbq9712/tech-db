#!/usr/bin/env python3
"""Generate AI summaries - robust version. 4 workers, batch=5, auto-resume."""
import json, re, subprocess, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

MODEL='glm-5.2'; PROVIDER='zai'
REPO=Path('/home/rhett/tech-db-fresh')
LITE=REPO/'data/processed/all-records-lite.json'
NUM_WORKERS=4
BATCH=5

lock = threading.Lock()
done_count = 0
batch_done = 0
start_time = time.time()

def call_glm(prompt):
    cmd=['hermes','--provider',PROVIDER,'-m',MODEL,'-z',prompt]
    try:
        r=subprocess.run(cmd,text=True,capture_output=True,timeout=60)
        return (r.stdout or '').strip()
    except:
        return ''

def parse_json(text):
    text=text.strip()
    if text.startswith('```'):
        text=text.strip('`')
        if text.startswith('json'): text=text[4:]
        text=text.strip()
    m=re.search(r'\[.*\]',text,re.S)
    if m: text=m.group(0)
    try:
        r=json.loads(text)
        return r if isinstance(r,list) else []
    except:
        return []

def process_batch(args):
    batch_id, items = args
    items_text = '\n\n'.join([
        f'[{j}] 标题：{(it[1] or "")[:80]}\n正文：{(it[2] or "无正文")[:300]}'
        for j, it in enumerate(items)
    ])
    prompt = f'请为以下每篇技术情报生成一段100-200字的中文摘要。\n\n{items_text}\n\n只输出JSON数组：[{{"id":0,"summary":"摘要"}}]'

    raw = call_glm(prompt)
    if not raw or len(raw) < 10:
        return batch_id, []

    results = parse_json(raw)
    summaries = []
    for r in results:
        if not isinstance(r, dict): continue
        lid = r.get('id')
        try:
            lid = int(lid)
        except (TypeError, ValueError):
            continue
        if lid < 0 or lid >= len(items): continue
        s = r.get('summary','').strip() if isinstance(r.get('summary',''), str) else ''
        if s and len(s) > 20:
            summaries.append((items[lid][0], s))
    return batch_id, summaries

def rebuild_chunks(data):
    LITE.write_text(json.dumps(data, ensure_ascii=False, separators=(',',':')), 'utf-8')
    CH=3000
    chunks=[data[i:i+CH] for i in range(0,len(data),CH)]
    for f in (REPO/'data/processed').glob('lite-part-*.js'): f.unlink()
    for ci,ch in enumerate(chunks):
        c=f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(ch,ensure_ascii=False,separators=(",",":"))});\n'
        (REPO/'data/processed'/f'lite-part-{ci}.js').write_text(c,'utf-8')

def main():
    global done_count, batch_done, start_time

    data = json.loads(LITE.read_text('utf-8'))
    todo = [(i, r.get('t',''), r.get('fb','') or r.get('b','')) for i, r in enumerate(data) if not r.get('as')]
    print(f'Need summary: {len(todo)}/{len(data)}', flush=True)
    if not todo:
        print('All done!')
        return

    batches = []
    for bi in range(0, len(todo), BATCH):
        batches.append((bi//BATCH, todo[bi:bi+BATCH]))

    total_batches = len(batches)
    print(f'Workers={NUM_WORKERS} Batch={BATCH} Batches={total_batches}', flush=True)

    WAVE = NUM_WORKERS * 2  # 8 batches per wave

    for wave_start in range(0, len(batches), WAVE):
        wave = batches[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {executor.submit(process_batch, b): b for b in wave}
            for future in as_completed(futures):
                batch_id, summaries = future.result()
                with lock:
                    for idx, s in summaries:
                        data[idx]['as'] = s
                        done_count += 1
                    batch_done += 1

                    if batch_done % 50 == 0:
                        rebuild_chunks(data)
                        no_as = sum(1 for r in data if not r.get('as'))
                        el = time.time() - start_time
                        spd = done_count / max(el, 1)
                        eta_h = (no_as / spd / 3600) if spd > 0 else 999
                        print(f'[{done_count}/{len(todo)}] Remain={no_as} ({spd:.1f}/s ETA={eta_h:.1f}h)', flush=True)

        # Save after each wave
        rebuild_chunks(data)

    rebuild_chunks(data)
    no_as = sum(1 for r in data if not r.get('as'))
    print(f'\nDone! Generated={done_count} Remaining={no_as} Time={(time.time()-start_time)/3600:.1f}h', flush=True)

if __name__=='__main__':
    main()
