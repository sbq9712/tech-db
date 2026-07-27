#!/usr/bin/env python3
"""
tech-db 全量评分脚本 v2
- 对 lite JSON 中所有相关记录（非"不相关"）进行五维质量评分
- 直接读写 all-records-lite.json
- 支持断点续跑（跳过已有 sc 的记录）
- 定期保存 + 重新生成分片

用法:
  python3 scripts/score_all.py                    # 全量评分
  python3 scripts/score_all.py --workers 6 --batch 20
"""
import json, subprocess, sys, time, threading, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO = Path(__file__).resolve().parent.parent
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000

BATCH_SIZE = 20
MAX_WORKERS = 6
TIMEOUT = 120

DOMAIN_THRESHOLDS = {
    "零碳产业": 6.5,
    "AI与智能科技": 6.5,
    "通用技术": 7.0,
}
SCORE_WEIGHTS = {
    "breakthrough": 0.30,
    "industry": 0.25,
    "rarity": 0.15,
    "data": 0.15,
    "timeliness": 0.15,
}

SCORING_PROMPT = """你是技术情报质量评估专家。对以下{count}条技术情报，每条打5个维度的分数（0-10分，可保留1位小数）。

维度定义：
1. breakthrough（技术突破性）：纯政策/市场=0；渐进改进=5；新机理/新材料/新架构=10
2. industry（产业影响力）：实验室概念=1；小规模验证=5；量产落地/产业链级=10
3. rarity（信息稀缺性）：转载旧闻=0；常规跟踪=5；独家首发/首次披露=10
4. data（数据充实度）：纯定性描述=0；有定性+部分参数=5；多个硬数据/量化指标=10
5. timeliness（时效性）：趋势分析/综述=2；近期进展=6；突发事件/最新发布=10

待评估情报：
{items_json}

只输出JSON数组，不要markdown代码块：
[{{"id":0,"breakthrough":7.5,"industry":6.0,"rarity":5.0,"data":8.0,"timeliness":7.0}}]"""

lock = threading.Lock()

def call_glm(prompt):
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt]
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=TIMEOUT)
        output = (r.stdout or '').strip()
        if output.startswith('```'):
            lines = output.split('\n')
            output = '\n'.join(lines[1:])
            if output.endswith('```'):
                output = output[:-3]
            output = output.strip()
            if output.startswith('json'):
                output = output[4:].strip()
        return output
    except subprocess.TimeoutExpired:
        return ''
    except Exception:
        return ''

def parse_json(text):
    import re
    if not text:
        return []
    text = text.strip()
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        text = m.group(0)
    try:
        r = json.loads(text)
        return r if isinstance(r, list) else []
    except:
        return []

def score_batch(batch_idx, batch_items):
    """Score a batch of records. batch_items = [(lite_idx, record_dict), ...]"""
    scoring_input = []
    for j, (idx, rec) in enumerate(batch_items):
        scoring_input.append({
            "id": j,
            "title": (rec.get('t', '') or '')[:120],
            "body": (rec.get('b', '') or rec.get('fb', '') or '')[:300],
            "category": rec.get('c', ''),
            "type": rec.get('i', ''),
        })
    prompt = SCORING_PROMPT.format(count=len(scoring_input), items_json=json.dumps(scoring_input, ensure_ascii=False))
    
    raw = call_glm(prompt)
    if not raw or len(raw) < 10:
        time.sleep(2)
        raw = call_glm(prompt)  # one retry
    
    results = parse_json(raw)
    scored = []
    for r in results:
        if not isinstance(r, dict):
            continue
        lid = r.get('id')
        try:
            lid = int(lid)
        except (TypeError, ValueError):
            continue
        if lid < 0 or lid >= len(batch_items):
            continue
        
        dims = {
            "b": round(float(r.get("breakthrough", 0)), 1),
            "i": round(float(r.get("industry", 0)), 1),
            "r": round(float(r.get("rarity", 0)), 1),
            "d": round(float(r.get("data", 0)), 1),
            "t": round(float(r.get("timeliness", 0)), 1),
        }
        final = round(
            dims["b"] * SCORE_WEIGHTS["breakthrough"] +
            dims["i"] * SCORE_WEIGHTS["industry"] +
            dims["r"] * SCORE_WEIGHTS["rarity"] +
            dims["d"] * SCORE_WEIGHTS["data"] +
            dims["t"] * SCORE_WEIGHTS["timeliness"],
            1
        )
        
        idx = batch_items[lid][0]
        scored.append((idx, final, dims))
    
    return batch_idx, scored

def should_ai_curate(final_score, category):
    for domain, threshold in DOMAIN_THRESHOLDS.items():
        if domain in category:
            return final_score >= threshold
    return False

def rebuild_chunks(data):
    """Save lite JSON + regenerate chunks."""
    LITE.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    for f in CHUNK_DIR.glob('lite-part-*.js'):
        f.unlink()
    chunks = [data[i:i+CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
    for ci, ch in enumerate(chunks):
        content = (f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];'
                   f'window.__LITE_PARTS__.push({json.dumps(ch, ensure_ascii=False, separators=(",", ":"))});\n')
        (CHUNK_DIR / f'lite-part-{ci}.js').write_text(content, 'utf-8')
    manifest_path = CHUNK_DIR / 'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text('utf-8'))
        manifest['meta']['records_total'] = len(data)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), 'utf-8')
        (CHUNK_DIR / 'manifest-data.js').write_text(
            f'window.__MANIFEST__={json.dumps(manifest, ensure_ascii=False)};', 'utf-8')

def main():
    parser = argparse.ArgumentParser(description='Full scoring for tech-db')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS)
    parser.add_argument('--batch', type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    data = json.loads(LITE.read_text('utf-8'))
    
    # Find relevant records that need scoring
    todo = []
    for i, r in enumerate(data):
        cat = r.get('c', '')
        if not cat or cat in ('不相关', '未分类', ''):
            continue
        if r.get('sc') is not None and r.get('sc') > 0:
            continue  # already scored
        todo.append((i, r))
    
    total = len(data)
    relevant = sum(1 for r in data if r.get('c', '') and r.get('c') not in ('不相关', '未分类', ''))
    already = relevant - len(todo)
    
    print(f"总记录: {total}", flush=True)
    print(f"相关记录: {relevant}", flush=True)
    print(f"已评分: {already}", flush=True)
    print(f"待评分: {len(todo)}", flush=True)
    
    if not todo:
        print("全部完成！", flush=True)
        return
    
    # Build batches
    batches = []
    for i in range(0, len(todo), args.batch):
        batches.append((len(batches), todo[i:i+args.batch]))
    
    total_batches = len(batches)
    print(f"批次: {total_batches} × {args.batch} 条/批, {args.workers} workers", flush=True)
    print(f"预估时间: ~{total_batches / args.workers * 25 / 3600:.1f}h", flush=True)
    
    done_count = 0
    fail_count = 0
    ai_count = 0
    start_time = time.time()
    batch_counter = 0
    WAVE = args.workers * 3  # process in waves to control memory
    
    for wave_start in range(0, len(batches), WAVE):
        wave = batches[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(score_batch, b[0], b[1]): b for b in wave}
            for future in as_completed(futures):
                batch_idx, scored = future.result()
                with lock:
                    for idx, final, dims in scored:
                        cat = data[idx].get('c', '')
                        aip = should_ai_curate(final, cat)
                        data[idx]['sc'] = final
                        data[idx]['scd'] = dims
                        data[idx]['aip'] = 1 if aip else 0
                        done_count += 1
                        if aip:
                            ai_count += 1
                    
                    batch_counter += 1
                    elapsed = time.time() - start_time
                    spd = done_count / max(elapsed, 1)
                    remain = len(todo) - done_count
                    eta_h = remain / spd / 3600 if spd > 0 else 999
                    
                    print(f"  [{done_count}/{len(todo)}] Batch {batch_counter}/{total_batches} "
                          f"| AI精选={ai_count} | {spd:.1f}/s ETA={eta_h:.1f}h", flush=True)
        
        # Save after each wave
        with lock:
            rebuild_chunks(data)
    
    # Final save
    rebuild_chunks(data)
    
    # Stats
    all_scores = [r.get('sc', 0) for r in data if r.get('sc')]
    elapsed = time.time() - start_time
    print(f"\n{'='*50}", flush=True)
    print(f"完成！已评分: {len(all_scores)} 条", flush=True)
    print(f"耗时: {elapsed/3600:.1f}h", flush=True)
    if all_scores:
        print(f"分数范围: {min(all_scores):.1f} - {max(all_scores):.1f}", flush=True)
        print(f"平均分: {sum(all_scores)/len(all_scores):.2f}", flush=True)
        
        # Distribution
        ranges = {"0-3": 0, "3-5": 0, "5-6": 0, "6-6.5": 0, "6.5-7": 0, "7-8": 0, "8-10": 0}
        for s in all_scores:
            if s < 3: ranges["0-3"] += 1
            elif s < 5: ranges["3-5"] += 1
            elif s < 6: ranges["5-6"] += 1
            elif s < 6.5: ranges["6-6.5"] += 1
            elif s < 7: ranges["6.5-7"] += 1
            elif s < 8: ranges["7-8"] += 1
            else: ranges["8-10"] += 1
        print(f"\n分布:", flush=True)
        for r, c in ranges.items():
            bar = '█' * (c // 50)
            print(f"  {r:>8s}: {c:5d}  {bar}", flush=True)
    
    total_ai = sum(1 for r in data if r.get('aip'))
    print(f"\nAI精选(aip=1): {total_ai} 条 ({total_ai/max(len(all_scores),1)*100:.1f}%)", flush=True)

if __name__ == '__main__':
    main()
