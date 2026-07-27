#!/usr/bin/env python3
"""Judge intelligence_type for news-spider/wechat records using GLM 5.2.

These sources may contain literature content (reporting on research papers).
LLM judges: is this about literature content (论文/研究) or news content?
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / 'data' / 'processed'

PROMPT = '''请判断以下情报属于"文献"还是"新闻"。

判断标准：
- 文献：内容是在介绍或讨论某篇学术论文、研究成果、实验方法、科学发现。即使发表在新闻渠道，核心内容是学术研究本身的，也算文献。
- 新闻：内容是在报道产业事件、产品发布、政策法规、市场动态、行业观点等非学术研究内容。

只输出JSON数组：[{"id":0,"type":"文献"}或{"id":0,"type":"新闻"}]

待判断情报：
'''


def load_shards():
    manifest = json.loads((DATA_DIR / 'manifest.json').read_text('utf-8'))
    shards = []
    for s in manifest['shards']:
        path = REPO_ROOT / s['path']
        data = json.loads(path.read_text('utf-8'))
        shards.append({'path': path, 'records': data['records']})
    return shards


def save_shards(shards):
    for shard in shards:
        shard['path'].write_text(
            json.dumps({'records': shard['records']}, ensure_ascii=False),
            'utf-8'
        )


def get_todo(shards):
    """Get news-spider/wechat records that need type judgment."""
    todo = []
    for si, shard in enumerate(shards):
        for ri, r in enumerate(shard['records']):
            src = r.get('source', '')
            if 'news-spider' in src or 'wechat' in src:
                todo.append((si, ri, r))
    return todo


def call_glm(prompt, retries=2):
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
            output = (result.stdout or '').strip()
            if not output:
                time.sleep(2)
                continue
            text = output
            if text.startswith('```'):
                text = text.strip('`').replace('json\n', '', 1).strip()
            match = re.search(r'\[.*\]', text, flags=re.S)
            if match:
                text = match.group(0)
            return json.loads(text)
        except Exception:
            time.sleep(2)
    return []


def process_batch(args_tuple):
    batch_idx, batch_items = args_tuple

    llm_items = []
    for global_idx, record in batch_items:
        llm_items.append({
            'id': len(llm_items),
            'title': (record.get('title', '') or '')[:150],
            'body': (record.get('body', '') or '')[:200],
        })

    prompt = PROMPT + json.dumps(llm_items, ensure_ascii=False)
    results = call_glm(prompt)

    judged = []
    for r in results:
        local_id = r.get('id')
        if local_id is None or local_id < 0 or local_id >= len(batch_items):
            continue
        global_idx = batch_items[local_id][0]
        rtype = r.get('type', '').strip()
        if rtype == '文献':
            judged.append((global_idx, 'literature'))
        elif rtype == '新闻':
            judged.append((global_idx, 'news'))

    return batch_idx, judged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--batch-size', type=int, default=50)
    args = parser.parse_args()

    shards = load_shards()
    todo = get_todo(shards)

    print(f"待判断类型: {len(todo)} 条", flush=True)
    if not todo:
        print("无需处理", flush=True)
        return

    # Build batches
    batches = []
    for i in range(0, len(todo), args.batch_size):
        chunk = todo[i:i + args.batch_size]
        batch_items = [(i + j, chunk[j][2]) for j in range(len(chunk))]
        batches.append((len(batches), batch_items))

    idx_map = {}
    for global_idx, (si, ri, _) in enumerate(todo):
        idx_map[global_idx] = (si, ri)

    done_count = 0
    save_counter = 0
    start_time = time.time()
    print(f"\n开始判断：{len(batches)} 批，{args.workers} workers\n", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_batch, b): b[0] for b in batches}
        for future in as_completed(futures):
            batch_idx, judged = future.result()
            for global_idx, new_type in judged:
                si, ri = idx_map[global_idx]
                shards[si]['records'][ri]['intelligence_type'] = new_type
                done_count += 1

            save_counter += 1
            if save_counter % 5 == 0:
                save_shards(shards)

            elapsed = time.time() - start_time
            pct = done_count / len(todo) * 100
            print(f"  Batch {batch_idx+1}/{len(batches)} | "
                  f"已判断 {done_count}/{len(todo)} ({pct:.1f}%) | {elapsed:.0f}s", flush=True)

    save_shards(shards)

    # Stats
    changed = sum(1 for s in shards for r in s['records']
                  if ('news-spider' in r.get('source', '') or 'wechat' in r.get('source', ''))
                  and r.get('intelligence_type') == 'literature')

    elapsed = time.time() - start_time
    print(f"\n完成！判断 {done_count} 条，其中改为文献: {changed} 条，耗时 {elapsed:.0f}s", flush=True)


if __name__ == '__main__':
    main()
