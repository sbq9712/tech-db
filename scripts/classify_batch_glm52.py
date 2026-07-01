#!/usr/bin/env python3
"""Parallel batch classifier using GLM 5.2 via hermes CLI.

Reads the classification prompt from the intelligence-classification skill template.
Classifies all unclassified records using multiple concurrent workers.
Pure semantic judgment via GLM 5.2 — no keyword or vector shortcuts.
Supports --resume for incremental runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

MODEL = 'glm-5.2'
PROVIDER = 'zai'

# Locate the skill prompt template
SKILL_PROMPT_PATHS = [
    Path.home() / '.hermes/skills/research/intelligence-classification/templates/classification_prompt.txt',
    Path('/home/rhett/.hermes/skills/research/intelligence-classification/templates/classification_prompt.txt'),
]


def load_prompt_template() -> str:
    for p in SKILL_PROMPT_PATHS:
        if p.exists():
            return p.read_text(encoding='utf-8').strip()
    raise FileNotFoundError(f'Classification prompt template not found in any of: {SKILL_PROMPT_PATHS}')


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')


def normalize_category(path: str) -> str:
    return path.replace('/', '-').replace('（', '').replace('）', '')


def parse_json_array(text: str):
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        text = text.replace('json\n', '', 1).replace('\n', '', 1).strip()
    match = re.search(r'\[.*\]', text, flags=re.S)
    if match:
        text = match.group(0)
    return json.loads(text)


def classify_batch(batch_id: int, batch: list[tuple[int, dict]], prompt_template: str, categories: list[str]) -> tuple[int, list[dict]]:
    """Classify a single batch. Returns (batch_id, results)."""
    items = []
    for idx, record in batch:
        items.append({
            'id': idx,
            'title': record.get('title', ''),
            'body': str(record.get('body', ''))[:800],
            'url': record.get('url', ''),
            'type': record.get('intelligence_type', ''),
        })

    prompt = f'''{prompt_template}

待分类 JSON：
{json.dumps(items, ensure_ascii=False)}

只输出 JSON 数组，不要 Markdown。数组元素格式：
{{"id":数字,"category":"候选路径库中的一条完整路径","confidence":0.0,"reason":"一句话理由"}}'''.strip()

    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    last = ''
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
            last = (result.stdout or result.stderr or '').strip()
            if result.returncode != 0:
                time.sleep(1 + attempt)
                continue
            output = parse_json_array(last)
            by_id = {int(x.get('id')): x for x in output if 'id' in x}
            results = []
            for idx, _ in batch:
                r = by_id.get(idx, {'id': idx, 'category': '不相关', 'confidence': 0.0, 'reason': 'missing from model output'})
                category = r.get('category') or '不相关'
                if category not in categories:
                    category = '不相关'
                results.append({
                    'idx': idx,
                    'category': normalize_category(category),
                    'confidence': float(r.get('confidence') or 0.0),
                    'reason': r.get('reason') or '',
                })
            return (batch_id, results)
        except Exception:
            time.sleep(1 + attempt)
            continue
    # All attempts failed
    fallback = []
    for idx, _ in batch:
        fallback.append({'idx': idx, 'category': '不相关', 'confidence': 0.0, 'reason': 'LLM batch parse failed: ' + last[:120]})
    return (batch_id, fallback)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default='', help='Only classify records matching this date. Empty = all.')
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--workers', type=int, default=6, help='Number of parallel LLM workers')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--shard', default='', help='Only process this month shard (YYYY-MM)')
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    categories = load_json(root / 'data/category-order.json')['categories']
    prompt_template = load_prompt_template()
    manifest = load_json(root / 'data/processed/manifest.json')

    # Collect all pending work per shard
    shard_work = {}  # shard_path -> [payload, list of (idx, record)]
    total_pending = 0
    for shard in manifest['shards']:
        if args.shard and shard['month'] != args.shard:
            continue
        path = root / shard['path']
        payload = load_json(path)
        records = payload.get('records', [])
        pending = []
        for idx, record in enumerate(records):
            if args.date and record.get('date') != args.date:
                continue
            if args.resume and record.get('classifier_model') == MODEL and record.get('category') != '未分类':
                continue
            pending.append((idx, record))
        if pending:
            shard_work[str(path)] = [payload, pending]
            total_pending += len(pending)

    if not total_pending:
        print(json.dumps({'message': 'nothing to classify', 'total_pending': 0}, ensure_ascii=False))
        return 0

    # Split into batches
    all_batches = []  # (shard_path, batch_id, [(idx, record), ...])
    for shard_path, (_, pending) in shard_work.items():
        for start in range(0, len(pending), args.batch_size):
            batch = pending[start:start + args.batch_size]
            if args.limit and len(all_batches) * args.batch_size >= args.limit:
                break
            all_batches.append((shard_path, len(all_batches), batch))

    print(f'Total pending: {total_pending}, batches: {len(all_batches)}, workers: {args.workers}, batch_size: {args.batch_size}', flush=True)

    # Thread-safe write-back per shard
    shard_locks = {sp: Lock() for sp in shard_work}
    classified_count = 0
    start_time = time.time()
    flush_interval = 50  # write shard file every N completed batches

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for shard_path, batch_id, batch in all_batches:
            future = executor.submit(classify_batch, batch_id, batch, prompt_template, categories)
            futures[future] = shard_path

        batch_counter = 0
        for future in as_completed(futures):
            shard_path = futures[future]
            batch_id, results = future.result()

            with shard_locks[shard_path]:
                payload, _ = shard_work[shard_path]
                records = payload['records']
                for r in results:
                    idx = r['idx']
                    record = records[idx]
                    record['category'] = r['category']
                    record['classification_confidence'] = r['confidence']
                    record['classification_reason'] = r['reason']
                    record['classifier_model'] = MODEL
                    record['classifier_provider'] = PROVIDER
                    record['key_parameters'] = []
                    record['cache_hit'] = False
                classified_count += len(results)

            batch_counter += 1
            if batch_counter % 10 == 0:
                elapsed = time.time() - start_time
                rate = classified_count / elapsed if elapsed > 0 else 0
                remaining = total_pending - classified_count
                eta_min = remaining / rate / 60 if rate > 0 else 0
                print(f'  progress: {classified_count}/{total_pending} ({rate:.1f}/s, ETA {eta_min:.0f}min)', flush=True)

            # Periodic flush to disk
            if batch_counter % flush_interval == 0:
                with shard_locks[shard_path]:
                    dump_json(Path(shard_path), shard_work[shard_path][0])

    # Final flush all shards
    for shard_path, (payload, _) in shard_work.items():
        dump_json(Path(shard_path), payload)

    elapsed = time.time() - start_time
    rate = classified_count / elapsed if elapsed > 0 else 0
    print(json.dumps({
        'classified': classified_count,
        'total_pending': total_pending,
        'elapsed_seconds': round(elapsed, 1),
        'rate_per_sec': round(rate, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
