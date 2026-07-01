#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

MODEL = 'gpt-5.4-mini'
PROVIDER = 'openai-api'


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_category(path: str) -> str:
    return path.replace('/', '-').replace('（', '').replace('）', '')


def parse_json_array(text: str):
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').replace('json\n', '', 1).strip()
    match = re.search(r'\[.*\]', text, flags=re.S)
    if match:
        text = match.group(0)
    return json.loads(text)


def classify_batch(batch: list[dict], categories: list[str]) -> list[dict]:
    category_block = '\n'.join(f'- {c}' for c in categories)
    items = []
    for idx, record in batch:
        items.append({
            'id': idx,
            'title': record.get('title', ''),
            'body': str(record.get('body', ''))[:800],
            'url': record.get('url', ''),
            'type': record.get('intelligence_type', ''),
        })
    prompt = f'''
你是“技术边界数据库”的分类器。必须使用你的语义判断，不要使用关键词或向量捷径。只能从给定分类列表中选择分类。

优先级：
1. 可归入“零碳产业”或“AI与智能科技”时，必须优先归入这两个大类下最具体叶子类目。
2. 只有不属于上述两个大类，但属于底层共性技术时，才归入“通用技术”。
3. 民生、医疗服务、政务、文娱、体育等不属于技术边界的，归入“不相关”。出现 AI/5G/机器人不代表相关。
4. “一张膜分出航空煤油级馏段...”应归入“零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/石油化工”。
5. “北京依托5G+AI为汕头肿瘤患者隔空手术”应归入“不相关”。

分类列表：
{category_block}

待分类 JSON：
{json.dumps(items, ensure_ascii=False)}

只输出 JSON 数组，不要 Markdown。数组元素格式：
{{"id":数字,"category":"分类列表中的一个原文分类","confidence":0.0,"reason":"一句话理由"}}
'''.strip()
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    last = ''
    for _ in range(2):
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
        last = (result.stdout or result.stderr or '').strip()
        if result.returncode != 0:
            continue
        try:
            output = parse_json_array(last)
            by_id = {int(x.get('id')): x for x in output if 'id' in x}
            return [by_id.get(idx, {'id': idx, 'category': '不相关', 'confidence': 0.0, 'reason': 'missing from model output'}) for idx, _ in batch]
        except Exception:
            continue
    return [{'id': idx, 'category': '不相关', 'confidence': 0.0, 'reason': 'LLM batch output parse failed: ' + last[:160]} for idx, _ in batch]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    categories = load_json(root / 'data/category-order.json')['categories']
    manifest = load_json(root / 'data/processed/manifest.json')
    classified = 0
    target = 0
    for shard in manifest['shards']:
        path = root / shard['path']
        payload = load_json(path)
        records = payload.get('records', [])
        pending = []
        for idx, record in enumerate(records):
            if record.get('date') != args.date:
                continue
            target += 1
            if args.resume and record.get('classifier_model') == MODEL and record.get('category') != '未分类':
                continue
            pending.append((idx, record))
        for start in range(0, len(pending), args.batch_size):
            if args.limit and classified >= args.limit:
                break
            batch = pending[start:start + args.batch_size]
            if args.limit:
                batch = batch[: max(0, args.limit - classified)]
            results = classify_batch(batch, categories)
            for result in results:
                idx = int(result['id'])
                record = records[idx]
                category = result.get('category') or '不相关'
                if category not in categories:
                    category = '不相关'
                record['category'] = normalize_category(category)
                record['classification_confidence'] = float(result.get('confidence') or 0.0)
                record['classification_reason'] = result.get('reason') or ''
                record['classifier_model'] = MODEL
                record['classifier_provider'] = PROVIDER
                record['key_parameters'] = []
                record['cache_hit'] = False
                classified += 1
            dump_json(path, payload)
            print(json.dumps({'classified': classified, 'target': target, 'batch': len(batch)}, ensure_ascii=False), flush=True)
    print(json.dumps({'date': args.date, 'target_count': target, 'classified': classified}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
