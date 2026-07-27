#!/usr/bin/env python3
"""Unified classify + tag script using GLM 5.2 via hermes CLI.

For NEW records (no category yet):
  - One LLM call outputs category + tag + topic simultaneously.

For EXISTING records (already classified, just need tags):
  - Use --tag-only mode: skip records that already have tag+topic.
  - Uses the tagging_prompt.txt (lighter, no classification paths).

Both modes skip records where category == '不相关'.

Usage:
  # Full pipeline for new records (classify + tag in one pass)
  python3 scripts/classify_and_tag.py --workers 8 --batch-size 20

  # Tag-only for records already classified but missing tag/topic
  python3 scripts/classify_and_tag.py --tag-only --workers 8 --batch-size 50

  # Resume (skip records that already have category AND tag+topic)
  python3 scripts/classify_and_tag.py --resume
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
SKILL_DIR = Path.home() / '.hermes/skills/research/intelligence-classification/templates'
UNIFIED_PROMPT_PATH = SKILL_DIR / 'unified_prompt.txt'
TAGGING_PROMPT_PATH = SKILL_DIR / 'tagging_prompt.txt'

NEWS_TAGS = {'技术突破', '产业进展', '政策监管', '资本运作', '行业观察'}
LIT_TAGS = {'研究论文', '观点评论'}
VALID_CATEGORIES = None  # populated at runtime from category-order.json


def load_categories():
    """Load valid category paths from category-order.json, normalized."""
    global VALID_CATEGORIES
    order_path = REPO_ROOT / 'data' / 'category-order.json'
    if order_path.exists():
        data = json.loads(order_path.read_text('utf-8'))
        cats = set()
        for path in data.get('categories', []):
            norm = normalize_category(path)
            cats.add(norm)
        cats.add('不相关')
        VALID_CATEGORIES = cats
    else:
        VALID_CATEGORIES = None  # don't validate


def normalize_category(value: str) -> str:
    return (value or '').replace('/', '-').replace('（', '').replace('）', '').replace('(', '').replace(')', '')


def load_prompt(tag_only: bool) -> str:
    path = TAGGING_PROMPT_PATH if tag_only else UNIFIED_PROMPT_PATH
    if not path.exists():
        print(f"ERROR: Prompt template not found at {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text('utf-8')


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


def get_todo(shards: list[dict], tag_only: bool, resume: bool) -> list[tuple]:
    """Get list of (shard_idx, record_idx, record) needing processing."""
    todo = []
    for si, shard in enumerate(shards):
        for ri, r in enumerate(shard['records']):
            # Always skip 不相关
            if r.get('category') == '不相关':
                continue

            if tag_only:
                # Only process records that already have a category but lack tag/topic
                if not r.get('category') or r.get('category') == '未分类':
                    continue
                if r.get('tag') and r.get('topic'):
                    continue
                todo.append((si, ri, r))
            else:
                # Full mode: process records without a valid category
                if resume and r.get('category') and r.get('category') != '未分类':
                    if r.get('tag') and r.get('topic'):
                        continue
                if r.get('category') and r.get('category') != '未分类':
                    # Already classified — skip in full mode unless --resume wants tags
                    if not resume:
                        continue
                    if r.get('tag') and r.get('topic'):
                        continue
                todo.append((si, ri, r))
    return todo


def build_batch_prompt(prompt_template: str, items: list[dict], tag_only: bool) -> str:
    if tag_only:
        output_fmt = '\n\n只输出JSON数组，每个元素格式：\n{"id":0,"专属标签":"","核心主题":""}'
    else:
        output_fmt = '\n\n只输出JSON数组，每个元素格式：\n{"id":0,"category":"完整路径或\"不相关\"","tag":"专属标签或空字符串","topic":"核心主题或空字符串"}'
    batch_json = json.dumps(items, ensure_ascii=False)
    return prompt_template + batch_json + output_fmt


def call_glm(prompt: str, retries: int = 3) -> list[dict]:
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
            output = (result.stdout or '').strip()
            if result.returncode != 0 or not output:
                time.sleep(3)
                continue
            text = output
            if text.startswith('```'):
                text = text.strip('`').replace('json\n', '', 1).strip()
            match = re.search(r'\[.*\]', text, flags=re.S)
            if match:
                text = match.group(0)
            return json.loads(text)
        except Exception:
            time.sleep(3)
    return []


def validate_tag(record_type: str, tag: str) -> bool:
    if record_type == 'news':
        return tag in NEWS_TAGS
    elif record_type == 'literature':
        return tag in LIT_TAGS
    return False


def process_batch(args_tuple):
    batch_idx, prompt_template, batch_items, tag_only = args_tuple

    llm_items = []
    for global_idx, record in batch_items:
        llm_items.append({
            'id': len(llm_items),
            'type': record.get('intelligence_type', ''),
            'category': (record.get('category', '') or '')[:60],
            'title': (record.get('title', '') or '')[:120],
            'body': (record.get('body', '') or '')[:250],
        })

    prompt = build_batch_prompt(prompt_template, llm_items, tag_only)
    results = call_glm(prompt)

    tagged = []
    for r in results:
        local_id = r.get('id')
        if local_id is None or local_id < 0 or local_id >= len(batch_items):
            continue
        global_idx = batch_items[local_id][0]
        record = batch_items[local_id][1]
        rec_type = record.get('intelligence_type', '')

        if tag_only:
            tag = r.get('专属标签', '').strip()
            topic = r.get('核心主题', '').strip()
            if not validate_tag(rec_type, tag):
                tag = ''
            if not topic or len(topic) > 15:
                topic = ''
            if tag and topic:
                tagged.append((global_idx, None, None, tag, topic))
        else:
            category = r.get('category', '').strip()
            tag = r.get('tag', '').strip()
            topic = r.get('topic', '').strip()

            # Validate category
            if category == '不相关':
                tagged.append((global_idx, '不相关', '', '', ''))
            else:
                norm_cat = normalize_category(category)
                if VALID_CATEGORIES and norm_cat not in VALID_CATEGORIES:
                    # Try fuzzy: find closest match
                    pass  # accept anyway, LLM is the authority
                if not validate_tag(rec_type, tag):
                    tag = ''
                if not topic or len(topic) > 15:
                    topic = ''
                tagged.append((global_idx, norm_cat or category, None, tag, topic))

    return batch_idx, tagged


def main():
    parser = argparse.ArgumentParser(description='Unified classify + tag with GLM 5.2')
    parser.add_argument('--tag-only', action='store_true',
                        help='Only tag records that already have a category (skip classification)')
    parser.add_argument('--resume', action='store_true',
                        help='Skip records that already have category AND tag+topic')
    parser.add_argument('--workers', type=int, default=8, help='Parallel workers')
    parser.add_argument('--batch-size', type=int, default=50, help='Records per batch')
    args = parser.parse_args()

    load_categories()
    prompt_template = load_prompt(args.tag_only)
    shards = load_shards()
    todo = get_todo(shards, args.tag_only, args.resume)

    total = sum(len(s['records']) for s in shards)
    skip_unc = sum(1 for s in shards for r in s['records'] if r.get('category') == '不相关')

    mode = "TAG-ONLY" if args.tag_only else "UNIFIED (classify+tag)"
    print(f"模式: {mode}", flush=True)
    print(f"总计: {total} 条", flush=True)
    print(f"不相关(跳过): {skip_unc} 条", flush=True)
    print(f"待处理: {len(todo)} 条", flush=True)
    if not todo:
        print("无需处理，全部完成。", flush=True)
        return

    # Build batches
    batches = []
    for i in range(0, len(todo), args.batch_size):
        chunk = todo[i:i + args.batch_size]
        batch_items = [(i + j, chunk[j][2]) for j in range(len(chunk))]
        batches.append((i // args.batch_size, prompt_template, batch_items, args.tag_only))

    idx_map = {}
    for global_idx, (si, ri, _) in enumerate(todo):
        idx_map[global_idx] = (si, ri)

    done_count = 0
    fail_count = 0
    start_time = time.time()
    print(f"\n开始处理：{len(batches)} 批 × {args.batch_size} 条/批，{args.workers} workers\n", flush=True)

    checkpoint_counter = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_batch, b): b[0] for b in batches}
        for future in as_completed(futures):
            batch_idx, tagged = future.result()
            for result in tagged:
                global_idx = result[0]
                si, ri = idx_map[global_idx]
                category, _, tag, topic = result[1], result[2], result[3], result[4]

                if category is not None:
                    shards[si]['records'][ri]['category'] = category
                if tag is not None:
                    shards[si]['records'][ri]['tag'] = tag
                if topic is not None:
                    shards[si]['records'][ri]['topic'] = topic

                if tag and topic:
                    done_count += 1
                elif category == '不相关':
                    shards[si]['records'][ri]['tag'] = ''
                    shards[si]['records'][ri]['topic'] = ''
                    done_count += 1
                else:
                    fail_count += 1

            elapsed = time.time() - start_time
            pct = done_count / len(todo) * 100
            print(f"  Batch {batch_idx+1}/{len(batches)} | "
                  f"已处理 {done_count}/{len(todo)} ({pct:.1f}%) | "
                  f"失败 {fail_count} | {elapsed:.0f}s", flush=True)

            checkpoint_counter += 1
            if checkpoint_counter % 20 == 0:
                save_shards(shards)
                print(f"  [checkpoint] saved to disk after {checkpoint_counter} batches ({done_count} records)", flush=True)

    # Save first round results
    save_shards(shards)

    # Retry: just re-run with --resume logic (pick up only missing records)
    still_missing = []
    for global_idx, (si, ri, _) in enumerate(todo):
        r = shards[si]['records'][ri]
        if r.get('category') == '不相关':
            continue
        if not r.get('tag') or not r.get('topic'):
            still_missing.append((si, ri, r))

    retry_round = 0
    while still_missing and retry_round < 5:
        retry_round += 1
        print(f"\n重试第{retry_round}轮：{len(still_missing)} 条，4 workers...", flush=True)

        # Build retry batches with correct global indices into still_missing
        retry_batches = []
        for i in range(0, len(still_missing), args.batch_size):
            chunk = still_missing[i:i + args.batch_size]
            batch_items = [(j, chunk[j][2]) for j in range(len(chunk))]
            retry_batches.append((len(retry_batches), prompt_template, batch_items, args.tag_only))

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(process_batch, b): b[0] for b in retry_batches}
            for future in as_completed(futures):
                batch_idx, tagged = future.result()
                for result in tagged:
                    local_idx = result[0]
                    if local_idx < 0 or local_idx >= len(retry_batches[batch_idx][2]):
                        continue
                    # Map local_idx back to (si, ri) via the batch's own items
                    record_dict = retry_batches[batch_idx][2][local_idx][1]
                    category, _, tag, topic = result[1], result[2], result[3], result[4]
                    if tag and topic:
                        # Find si, ri by scanning still_missing for this record
                        for si, ri, r in still_missing:
                            if r is record_dict:
                                shards[si]['records'][ri]['tag'] = tag
                                shards[si]['records'][ri]['topic'] = topic
                                done_count += 1
                                break

        save_shards(shards)
        print(f"  保存进度...", flush=True)

        still_missing = []
        for global_idx, (si, ri, _) in enumerate(todo):
            r = shards[si]['records'][ri]
            if r.get('category') == '不相关':
                continue
            if not r.get('tag') or not r.get('topic'):
                still_missing.append((si, ri, r))

    if still_missing:
        print(f"\n剩余 {len(still_missing)} 条无法完成，跳过", flush=True)

    print(f"\n保存到磁盘...", flush=True)
    save_shards(shards)

    elapsed = time.time() - start_time
    final_missing = len(still_missing) if still_missing else 0
    print(f"\n完成！处理 {done_count} 条，剩余 {final_missing} 条未标注，耗时 {elapsed:.0f}s", flush=True)


if __name__ == '__main__':
    main()
