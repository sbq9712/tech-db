#!/usr/bin/env python3
"""Reclassify records currently in '其它电池体系' using the updated prompt.

The updated prompt defines battery categories by CHARGE CARRIER (载流子):
- 锂电池: Li⁺ carrier (incl. solid-state Li, semi-solid Li, halide Li, Li-S, Li-metal, Li-air)
- 钠电池: Na⁺ carrier
- 其它电池体系: K⁺/Zn²⁺/Al³⁺/F⁻ carriers, flow batteries, metal-air (non-Li)

This script re-runs LLM classification on all '其它电池体系' records and moves
misclassified lithium/sodium batteries to their correct categories.
"""
from __future__ import annotations
import json, re, subprocess, sys, time, os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO = Path('/home/rhett/tech-db-fresh')
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
SKILL = Path.home() / '.hermes/skills/research/intelligence-classification/templates'
PROMPT_PATH = SKILL / 'unified_prompt.txt'

NEWS_TAGS = {'技术突破', '产业进展', '政策监管', '资本运作', '行业观察'}
LIT_TAGS = {'研究论文', '观点评论'}

def norm(v: str) -> str:
    return (v or '').replace('/', '-').replace('（', '').replace('）', '').replace('(', '').replace(')', '')

def call_glm(prompt: str) -> str:
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    for _ in range(3):
        try:
            r = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
            o = (r.stdout or '').strip()
            if r.returncode == 0 and o:
                return o
            time.sleep(3)
        except Exception:
            time.sleep(5)
    return ''

def parse(text: str):
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').replace('json\n', '', 1).strip()
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        text = m.group(0)
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else None
    except json.JSONDecodeError:
        return None

def save_all(data):
    """Save to lite JSON + regenerate lite-part shards."""
    LITE.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    CH = 3000
    chunks = [data[i:i+CH] for i in range(0, len(data), CH)]
    for ci, ch in enumerate(chunks):
        c = f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(ch, ensure_ascii=False, separators=(",", ":"))});\n'
        (REPO / 'data' / 'processed' / f'lite-part-{ci}.js').write_text(c, 'utf-8')
    log(f'  Saved lite JSON ({len(data)} records) + {len(chunks)} shards')

def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)

def main():
    data = json.loads(LITE.read_text('utf-8'))
    # Find all records in 其它电池体系
    todo = [(i, r) for i, r in enumerate(data) if '其它电池体系' in r.get('c', '')]
    log(f'其它电池体系 records to re-classify: {len(todo)}')
    if not todo:
        log('Nothing to do.')
        return

    prompt_tmpl = PROMPT_PATH.read_text('utf-8')
    BATCH = 10
    batches = [(todo[i:i+BATCH], prompt_tmpl) for i in range(0, len(todo), BATCH)]

    moved = {'锂电池': 0, '钠电池': 0, '其它电池体系': 0, '其它': 0}
    done = 0
    start = time.time()

    def process_batch(batch_data):
        chunk, prompt_template = batch_data
        items = []
        for _, r in chunk:
            items.append({
                'id': len(items),
                'type': r.get('i', 'n'),
                'title': (r.get('t', '') or '')[:120],
                'body': (r.get('fb', '') or r.get('b', '') or '')[:250],
            })
        fmt = ('\n\n只输出JSON数组，每个元素格式：\n'
               '{"id":0,"category":"完整路径或\\"不相关\\"","tag":"专属标签","topic":"核心主题"}\n'
               '重要：这些记录当前都在"其它电池体系"分类下，但可能有误。请根据载流子重新判断：\n'
               '- 以Li⁺为载流子的（包括全固态锂电池、半固态锂电池、卤化物固态锂电池、硫化物固态锂电池、锂硫电池、锂金属电池、锂空气电池）→ 锂电池\n'
               '- 以Na⁺为载流子的 → 钠电池\n'
               '- 以K⁺/Zn²⁺/Al³⁺/F⁻等为载流子，或液流电池/金属空气电池 → 其它电池体系')
        raw = call_glm(prompt_template + json.dumps(items, ensure_ascii=False) + fmt)
        results = parse(raw)
        if not results:
            return []
        tagged = []
        for res in results:
            lid = res.get('id', -1)
            if lid < 0 or lid >= len(chunk):
                continue
            gi = chunk[lid][0]
            cat = res.get('category', '').strip()
            tag = res.get('tag', '').strip()
            topic = res.get('topic', '').strip()
            tagged.append((gi, cat, tag, topic))
        return tagged

    WORKERS = 8
    log(f'Processing {len(batches)} batches × {BATCH} records, {WORKERS} workers')
    save_counter = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(process_batch, b): bi for bi, b in enumerate(batches)}
        for future in as_completed(futures):
            bi = futures[future]
            try:
                tagged = future.result()
            except Exception as e:
                log(f'  Batch {bi+1} FAILED: {e}')
                continue
            for gi, cat, tag, topic in tagged:
                r = data[gi]
                old_cat = r.get('c', '')
                # Normalize and validate
                if not cat or cat == '不相关':
                    continue
                # Determine target subcategory
                if '锂电池' in cat:
                    new_cat = '零碳产业/能量循环/能量存储/电化学储能/二次电池/锂电池'
                    moved['锂电池'] += 1
                elif '钠电池' in cat:
                    new_cat = '零碳产业/能量循环/能量存储/电化学储能/二次电池/钠电池'
                    moved['钠电池'] += 1
                elif '其它电池体系' in cat or '其他电池' in cat:
                    new_cat = old_cat  # keep
                    moved['其它电池体系'] += 1
                else:
                    # Moved to a completely different category
                    new_cat = cat
                    moved['其它'] += 1
                r['c'] = new_cat
                if tag:
                    rt = r.get('i', 'n')
                    vt = NEWS_TAGS if rt != 'l' else LIT_TAGS
                    if tag in vt:
                        r['tg'] = tag
                if topic and len(topic) <= 15:
                    r['tp'] = topic
                done += 1
            save_counter += 1
            if save_counter % 10 == 0:
                elapsed = time.time() - start
                pct = done / len(todo) * 100
                rate = done / max(elapsed, 1)
                eta = (len(todo) - done) / max(rate, 0.01)
                log(f'  [{save_counter}/{len(batches)}] {done}/{len(todo)} ({pct:.0f}%) '
                    f'moved: Li={moved["锂电池"]} Na={moved["钠电池"]} keep={moved["其它电池体系"]} '
                    f'ETA={eta:.0f}s')

    log(f'\n=== DONE ===')
    log(f'Re-classified: {done}/{len(todo)}')
    log(f'Moved to 锂电池: {moved["锂电池"]}')
    log(f'Moved to 钠电池: {moved["钠电池"]}')
    log(f'Kept in 其它电池体系: {moved["其它电池体系"]}')
    log(f'Moved elsewhere: {moved["其它"]}')
    log(f'Time: {time.time()-start:.0f}s')

    save_all(data)
    log('All data saved.')


if __name__ == '__main__':
    main()
