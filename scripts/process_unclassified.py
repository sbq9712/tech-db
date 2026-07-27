#!/usr/bin/env python3
"""Process unclassified records (category='未分类') in lite data.
Stage 1: Domain classification (零碳产业/AI/通用技术/不相关)
Stage 2: Intelligence type + tag + topic (skip if 不相关)
Stage 3: Parameter extraction (skip if 不相关)

Usage: python3 scripts/process_unclassified.py
"""
from __future__ import annotations
import json, re, subprocess, sys, time
from pathlib import Path

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO_ROOT = Path(__file__).resolve().parent.parent
LITE_PATH = REPO_ROOT / 'data' / 'processed' / 'all-records-lite.json'
SKILL_DIR = Path.home() / '.hermes/skills/research/intelligence-classification/templates'

NEWS_TAGS = {'技术突破', '产业进展', '政策监管', '资本运作', '行业观察'}
LIT_TAGS = {'研究论文', '观点评论'}

def normalize_category(value: str) -> str:
    return (value or '').replace('/', '-').replace('（', '').replace('）', '').replace('(', '').replace(')', '')

def call_glm(prompt: str, retries: int = 3) -> str:
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
            output = (result.stdout or '').strip()
            if result.returncode != 0 or not output:
                print(f"  Retry {attempt+1}: empty output", flush=True)
                time.sleep(3)
                continue
            return output
        except subprocess.TimeoutExpired:
            print(f"  Retry {attempt+1}: timeout", flush=True)
            time.sleep(5)
        except Exception as e:
            print(f"  Retry {attempt+1}: {e}", flush=True)
            time.sleep(3)
    return ''

def parse_json_response(text: str) -> list:
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').replace('json\n', '', 1).strip()
    match = re.search(r'\[.*\]', text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except:
        return []

def load_prompt(name: str) -> str:
    path = SKILL_DIR / name
    if not path.exists():
        print(f"ERROR: Prompt not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text('utf-8')

def main():
    # Load lite data
    data = json.loads(LITE_PATH.read_text('utf-8'))
    todo = [(i, r) for i, r in enumerate(data) if r.get('c') == '未分类']
    print(f"Total: {len(data)}, Unclassified: {len(todo)}", flush=True)
    if not todo:
        print("Nothing to process.", flush=True)
        return

    # Load prompts
    unified_prompt = load_prompt('unified_prompt.txt')
    param_prompt = load_prompt('param_extraction_prompt.txt')

    BATCH_SIZE = 10
    batches = [todo[i:i+BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    print(f"Batches: {len(batches)} x {BATCH_SIZE}", flush=True)

    done = 0
    failed = 0
    start = time.time()

    for bi, batch in enumerate(batches):
        # Build LLM items
        llm_items = []
        for idx, r in batch:
            llm_items.append({
                'id': len(llm_items),
                'type': r.get('i', 'n'),
                'title': (r.get('t', '') or '')[:120],
                'body': (r.get('b', '') or '')[:250],
            })

        # Stage 1+2: Unified classification + tag + topic
        output_fmt = '\n\n只输出JSON数组，每个元素格式：\n{"id":0,"category":"完整路径或\"不相关\"","tag":"专属标签或空字符串","topic":"核心主题或空字符串"}'
        prompt = unified_prompt + json.dumps(llm_items, ensure_ascii=False) + output_fmt
        raw = call_glm(prompt)
        results = parse_json_response(raw)

        # Map results back
        updates = {}
        for r in results:
            lid = r.get('id')
            if lid is None or lid < 0 or lid >= len(batch):
                continue
            global_idx = batch[lid][0]
            cat = r.get('category', '').strip()
            tag = r.get('tag', '').strip()
            topic = r.get('topic', '').strip()

            if cat == '不相关':
                updates[global_idx] = {'c': '不相关', 'tg': '', 'tp': ''}
            else:
                norm_cat = normalize_category(cat)
                rec_type = data[global_idx].get('i', 'n')
                valid_tags = NEWS_TAGS if rec_type == 'n' else LIT_TAGS
                if tag not in valid_tags:
                    tag = ''
                if not topic or len(topic) > 15:
                    topic = ''
                updates[global_idx] = {'c': norm_cat or cat, 'tg': tag, 'tp': topic}

        # Apply updates
        for idx, fields in updates.items():
            data[idx].update(fields)
            done += 1

        # Stage 3: Param extraction for non-不相关 records
        param_items = []
        param_map = {}
        for idx, fields in updates.items():
            if fields.get('c') and fields['c'] != '不相关':
                r = data[idx]
                param_map[len(param_items)] = idx
                param_items.append({
                    'id': len(param_items),
                    'type': r.get('i', 'n'),
                    'category': (r.get('c', '') or '')[:60],
                    'title': (r.get('t', '') or '')[:120],
                    'body': (r.get('b', '') or '')[:250],
                })

        if param_items:
            param_output_fmt = '\n\n只输出JSON数组，每个元素格式：\n{"id":0,"关键参数":["参数1","参数2"]}'
            param_full = param_prompt + json.dumps(param_items, ensure_ascii=False) + param_output_fmt
            param_raw = call_glm(param_full)
            param_results = parse_json_response(param_raw)
            for pr in param_results:
                pid = pr.get('id')
                if pid is not None and pid in param_map:
                    kp = pr.get('关键参数', [])
                    if isinstance(kp, list) and kp:
                        data[param_map[pid]]['kp'] = kp[:5]

        # Incremental save every 5 batches
        if (bi + 1) % 5 == 0 or bi == len(batches) - 1:
            LITE_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
            # Rebuild chunks
            CHUNK = 8000
            chunks = [data[i:i+CHUNK] for i in range(0, len(data), CHUNK)]
            for ci, chunk in enumerate(chunks):
                content = f'window.__LITE_PARTS__ = window.__LITE_PARTS__ || [];\nwindow.__LITE_PARTS__.push({json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))});\n'
                (REPO_ROOT / 'data' / 'processed' / f'lite-part-{ci}.js').write_text(content, 'utf-8')
            elapsed = time.time() - start
            rate = done / max(elapsed, 1)
            print(f"[{bi+1}/{len(batches)}] Done={done} Failed={failed} ({rate:.1f}/s) Saved.", flush=True)

    # Final save
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    CHUNK = 8000
    chunks = [data[i:i+CHUNK] for i in range(0, len(data), CHUNK)]
    for ci, chunk in enumerate(chunks):
        content = f'window.__LITE_PARTS__ = window.__LITE_PARTS__ || [];\nwindow.__LITE_PARTS__.push({json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))});\n'
        (REPO_ROOT / 'data' / 'processed' / f'lite-part-{ci}.js').write_text(content, 'utf-8')

    elapsed = time.time() - start
    still_unclassified = sum(1 for r in data if r.get('c') == '未分类')
    print(f"\nComplete! Done={done} Failed={failed} Time={elapsed:.0f}s", flush=True)
    print(f"Still unclassified: {still_unclassified}", flush=True)

if __name__ == '__main__':
    main()
