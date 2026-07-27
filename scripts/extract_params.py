#!/usr/bin/env python3
"""Extract key parameters from intelligence records using GLM 5.2.

Stage 2 of the pipeline. Processes all non-不相关 records that already have
tag+topic from Stage 1. Extracts 0-5 key technical parameters per record.

Usage:
  python3 scripts/extract_params.py --workers 6 --batch-size 50
  python3 scripts/extract_params.py --resume
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
PROMPT_PATH = Path.home() / '.hermes/skills/research/intelligence-classification/templates/param_extraction_prompt.txt'


def load_prompt() -> str:
    if not PROMPT_PATH.exists():
        print(f"ERROR: Prompt not found at {PROMPT_PATH}", file=sys.stderr)
        sys.exit(1)
    return PROMPT_PATH.read_text('utf-8')


def load_shards() -> list[dict]:
    manifest = json.loads((DATA_DIR / 'manifest.json').read_text('utf-8'))
    shards = []
    for s in manifest['shards']:
        path = REPO_ROOT / s['path']
        data = json.loads(path.read_text('utf-8'))
        shards.append({'path': path, 'records': data['records']})
    return shards


def save_shards(shards: list[dict]):
    for shard in shards:
        shard['path'].write_text(
            json.dumps({'records': shard['records']}, ensure_ascii=False),
            'utf-8'
        )


def get_todo(shards: list[dict], resume: bool) -> list[tuple]:
    """Get records needing param extraction.
    Skip: 不相关, records without tag+topic, records that already have key_params (if resume)."""
    todo = []
    for si, shard in enumerate(shards):
        for ri, r in enumerate(shard['records']):
            if r.get('category') == '不相关':
                continue
            if not r.get('tag') or not r.get('topic'):
                continue
            if resume and 'key_params' in r:
                continue
            todo.append((si, ri, r))
    return todo


def build_batch_prompt(prompt_template: str, items: list[dict]) -> str:
    batch_json = json.dumps(items, ensure_ascii=False)
    return prompt_template + batch_json


def call_glm(prompt: str, retries: int = 2) -> list[dict]:
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
            output = (result.stdout or '').strip()
            if result.returncode != 0 or not output:
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
    batch_idx, prompt_template, batch_items = args_tuple

    llm_items = []
    for global_idx, record in batch_items:
        llm_items.append({
            'id': len(llm_items),
            'type': record.get('intelligence_type', ''),
            'title': (record.get('title', '') or '')[:150],
            'body': (record.get('body', '') or '')[:400],
        })

    prompt = build_batch_prompt(prompt_template, llm_items)
    results = call_glm(prompt)

    extracted = []
    for r in results:
        local_id = r.get('id')
        if local_id is None or local_id < 0 or local_id >= len(batch_items):
            continue
        global_idx = batch_items[local_id][0]
        key_params = r.get('key_params', [])
        if isinstance(key_params, list):
            key_params = [str(p).strip() for p in key_params if str(p).strip()][:5]
        else:
            key_params = []
        extracted.append((global_idx, key_params))

    return batch_idx, extracted


def main():
    parser = argparse.ArgumentParser(description='Extract key params with GLM 5.2')
    parser.add_argument('--resume', action='store_true', help='Skip records that already have key_params')
    parser.add_argument('--workers', type=int, default=6, help='Parallel workers')
    parser.add_argument('--batch-size', type=int, default=50, help='Records per batch')
    args = parser.parse_args()

    prompt_template = load_prompt()
    shards = load_shards()
    todo = get_todo(shards, args.resume)

    total = sum(len(s['records']) for s in shards)
    skip_unc = sum(1 for s in shards for r in s['records'] if r.get('category') == '不相关')
    skip_tagged = sum(1 for s in shards for r in s['records'] if r.get('key_params'))

    print(f"总计: {total} 条", flush=True)
    print(f"不相关(跳过): {skip_unc} 条", flush=True)
    print(f"已有参数(跳过): {skip_tagged} 条", flush=True)
    print(f"待提取: {len(todo)} 条", flush=True)
    if not todo:
        print("无需处理，全部完成。", flush=True)
        return

    # Build batches
    batches = []
    for i in range(0, len(todo), args.batch_size):
        chunk = todo[i:i + args.batch_size]
        batch_items = [(i + j, chunk[j][2]) for j in range(len(chunk))]
        batches.append((i // args.batch_size, prompt_template, batch_items))

    idx_map = {}
    for global_idx, (si, ri, _) in enumerate(todo):
        idx_map[global_idx] = (si, ri)

    done_count = 0
    fail_count = 0
    save_counter = 0
    start_time = time.time()
    print(f"\n开始提取：{len(batches)} 批 × {args.batch_size} 条/批，{args.workers} workers\n", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_batch, b): b[0] for b in batches}
        for future in as_completed(futures):
            batch_idx, extracted = future.result()
            for global_idx, key_params in extracted:
                si, ri = idx_map[global_idx]
                shards[si]['records'][ri]['key_params'] = key_params
                done_count += 1

            elapsed = time.time() - start_time
            pct = done_count / len(todo) * 100
            print(f"  Batch {batch_idx+1}/{len(batches)} | "
                  f"已提取 {done_count}/{len(todo)} ({pct:.1f}%) | {elapsed:.0f}s", flush=True)

            # Incremental save every 20 batches
            save_counter += 1
            if save_counter % 20 == 0:
                save_shards(shards)
                print(f"  [checkpoint] saved to disk ({done_count} records)", flush=True)

    # Save after first round
    save_shards(shards)

    # Retry with parallel workers
    retry_round = 0
    while True:
        still_missing = []
        for global_idx, (si, ri, _) in enumerate(todo):
            if 'key_params' not in shards[si]['records'][ri]:
                still_missing.append((si, ri, shards[si]['records'][ri]))

        if not still_missing:
            break

        retry_round += 1
        if retry_round > 5:
            print(f"\n已重试{retry_round-1}轮，剩余{len(still_missing)}条无法完成", flush=True)
            for si, ri, _ in still_missing:
                shards[si]['records'][ri]['key_params'] = []
            break

        print(f"\n重试第{retry_round}轮：{len(still_missing)} 条，4 workers...", flush=True)
        retry_batches = []
        for i in range(0, len(still_missing), args.batch_size):
            chunk = still_missing[i:i + args.batch_size]
            batch_items = [(j, chunk[j][2]) for j in range(len(chunk))]
            retry_batches.append((len(retry_batches), prompt_template, batch_items))

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(process_batch, b): b[0] for b in retry_batches}
            for future in as_completed(futures):
                batch_idx, extracted = future.result()
                for local_idx, key_params in extracted:
                    if local_idx < 0 or local_idx >= len(retry_batches[batch_idx][2]):
                        continue
                    record_dict = retry_batches[batch_idx][2][local_idx][1]
                    for si, ri, r in still_missing:
                        if r is record_dict:
                            shards[si]['records'][ri]['key_params'] = key_params
                            done_count += 1
                            break

        save_shards(shards)
        print(f"  保存进度...", flush=True)

    print(f"\n保存到磁盘...", flush=True)
    save_shards(shards)

    # Set empty key_params for 不相关 records
    for shard in shards:
        for r in shard['records']:
            if r.get('category') == '不相关':
                r.setdefault('key_params', [])

    save_shards(shards)

    elapsed = time.time() - start_time
    print(f"\n完成！提取 {done_count} 条，耗时 {elapsed:.0f}s", flush=True)


if __name__ == '__main__':
    main()
