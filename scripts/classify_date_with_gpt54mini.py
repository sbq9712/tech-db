#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import textwrap
import re
from pathlib import Path

MODEL = 'gpt-5.4-mini'
PROVIDER = 'openai-api'


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_category(path: str) -> str:
    return path.replace('/', '-').replace('（', '').replace('）', '')


def classify_one(record: dict, categories: list[str]) -> dict:
    category_block = '\n'.join(f'- {c}' for c in categories)
    prompt = f'''
你是“技术边界数据库”的分类器。只能从给定分类列表中选择一个分类，必须使用 gpt-5.4-mini 直接判断，禁止使用向量或关键词捷径。

分类优先级：
1. 如果可归入“零碳产业”或“AI与智能科技”，必须优先归入这两个大类下最具体的叶子类目。
2. 只有当不属于上述两个大类，但确实属于底层共性技术时，才归入“通用技术”。
3. 与技术边界无关的民生、医疗服务、政务、文娱、体育等，归入“不相关”。出现 AI/5G/机器人等词不等于相关，必须看技术突破主体。
4. 示例：“一张膜分出航空煤油级馏段...”归入“零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/石油化工”。
5. 示例：“北京依托5G+AI为汕头肿瘤患者隔空手术”归入“不相关”。

分类列表（必须选一个）：
{category_block}

待分类情报：
标题：{record.get('title','')}
正文/摘要：{str(record.get('body',''))[:900]}
作者：{record.get('authors','')}
URL：{record.get('url','')}
类型：{record.get('intelligence_type','')}

只输出 JSON，不要 Markdown：
{{"category":"分类列表中的一个原文分类", "confidence":0.0, "reason":"一句话理由"}}
'''.strip()
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    last_text = ''
    for attempt in range(2):
        result = subprocess.run(cmd, cwd=Path.cwd(), text=True, capture_output=True, timeout=90)
        if result.returncode != 0:
            last_text = (result.stderr or result.stdout).strip()
            continue
        last_text = result.stdout.strip()
        text = last_text
        if text.startswith('```'):
            text = text.strip('`')
            text = text.replace('json\n', '', 1).strip()
        match = re.search(r'\{.*\}', text, flags=re.S)
        if match:
            text = match.group(0)
        try:
            obj = json.loads(text)
            break
        except json.JSONDecodeError:
            obj = None
    if obj is None:
        return {
            'category': '不相关',
            'classification_confidence': 0.0,
            'classification_reason': 'LLM output parse failed: ' + last_text[:180],
            'classifier_model': MODEL,
            'classifier_provider': PROVIDER,
        }
    category = obj.get('category') or '不相关'
    if category not in categories:
        category = '不相关'
    return {
        'category': normalize_category(category),
        'classification_confidence': float(obj.get('confidence') or 0.0),
        'classification_reason': obj.get('reason') or '',
        'classifier_model': MODEL,
        'classifier_provider': PROVIDER,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    categories = load_json(root / 'data/category-order.json')['categories']
    manifest = load_json(root / 'data/processed/manifest.json')
    target_count = 0
    classified = 0
    for shard in manifest['shards']:
        path = root / shard['path']
        payload = load_json(path)
        changed = False
        for record in payload.get('records', []):
            if record.get('date') != args.date:
                continue
            target_count += 1
            if args.resume and record.get('classifier_model') == MODEL and record.get('category') != '未分类':
                continue
            if args.limit and classified >= args.limit:
                continue
            result = classify_one(record, categories)
            record.update(result)
            record['key_parameters'] = []
            record['cache_hit'] = False
            changed = True
            classified += 1
            dump_json(path, payload)
            print(json.dumps({'classified': classified, 'title': record.get('title'), **result}, ensure_ascii=False), flush=True)
        if changed:
            dump_json(path, payload)
    print(json.dumps({'date': args.date, 'target_count': target_count, 'classified': classified}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
