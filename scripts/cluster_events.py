#!/usr/bin/env python3
"""
tech-db 事件聚类脚本
- 对 lite JSON 中相关记录按技术主题语义聚类
- 用 GLM 5.2 对同一 topic + 同一分组进行事件聚类判断
- 识别同一事件的多个报道，选主条（信源权重高→分数高→日期最新）
- 直接读写 all-records-lite.json

聚类的策略：
1. 先按 topic 字段预分组
2. 同一 topic 内用 GLM 判断是否同一事件
3. 添加 cluster_id 字段 (cl) 和 is_primary 字段 (cp)
4. 主条选取：score最高 → date最新

用法:
  python3 scripts/cluster_events.py
  python3 scripts/cluster_events.py --workers 6 --batch 20
"""
import json, subprocess, sys, time, threading, argparse, hashlib
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO = Path(__file__).resolve().parent.parent
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000
BATCH_SIZE = 15
MAX_WORKERS = 6
TIMEOUT = 120

# Source tier weights for primary selection
SOURCE_TIERS = {
    'T1': 1.0,   # 顶刊/顶会 (Nature/Science/arXiv)
    'T1.5': 0.9, # 专业媒体
    'T2': 0.7,   # 一般科技媒体
    'T3': 0.5,   # 自媒体/聚合
}

CLUSTERING_PROMPT = """你是技术情报事件聚类专家。判断以下情报是否属于同一技术事件。

同一事件 = 围绕同一个具体技术进展/产品发布/项目签约的多篇报道。
不同事件 = 虽然同领域，但讲的是不同的具体进展。

判断标准：
- 相同事件：同一产品发布、同一研究成果发表、同一项目签约、同一政策文件
- 不同事件：同领域但不同产品、不同团队的不同研究、不同项目

待分组情报（均属同一技术主题"{topic}"）：
{items_json}

只输出JSON数组，每组为同一事件的情报id列表：
[{{"event_ids": [0, 2, 5], "event_label": "简短事件名(10字内)"}}]

注意：每条情报必须出现在且仅出现在一个事件组中。如果某条情报是独立事件，就单独一组。"""

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
    except:
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

def get_source_tier(authors):
    """Infer source tier from author/source field."""
    if not authors:
        return 'T3'
    authors_lower = authors.lower()
    if any(k in authors_lower for k in ['nature', 'science', 'arxiv', 'ieee', 'cell', 'pnas']):
        return 'T1'
    if any(k in authors_lower for k in ['pv magazine', 'batteries international', 'sciencealert', 'interesting engineering']):
        return 'T1.5'
    if any(k in authors_lower for k in ['新华网', '央视', '中国能源报', '中国能源新闻网', '人民网', '科技日报']):
        return 'T1.5'
    if any(k in authors_lower for k in ['IT之家', '36氪', '虎嗅', '雷锋网']):
        return 'T2'
    return 'T2'

def cluster_topic_batch(batch_idx, topic, batch_items):
    """Cluster records within a single topic batch."""
    items_input = []
    for j, (idx, rec) in enumerate(batch_items):
        items_input.append({
            "id": j,
            "title": (rec.get('t', '') or '')[:100],
            "body": (rec.get('b', '') or '')[:150],
            "date": rec.get('d', ''),
            "source": (rec.get('a', '') or '')[:50],
        })
    
    prompt = CLUSTERING_PROMPT.format(
        topic=topic,
        items_json=json.dumps(items_input, ensure_ascii=False)
    )
    
    raw = call_glm(prompt)
    if not raw or len(raw) < 10:
        time.sleep(2)
        raw = call_glm(prompt)
    
    events = parse_json(raw)
    clusters = []  # list of (event_label, [global_indices])
    
    if not events:
        # Fallback: each record is its own cluster
        for j, (idx, _) in enumerate(batch_items):
            clusters.append((f"{topic}_{j}", [idx]))
        return batch_idx, topic, clusters
    
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ids = ev.get('event_ids', [])
        if not isinstance(ids, list):
            ids = [ids] if isinstance(ids, int) else []
        label = ev.get('event_label', topic)[:15] if isinstance(ev.get('event_label'), str) else topic[:15]
        global_indices = []
        for lid in ids:
            try:
                lid = int(lid)
            except (TypeError, ValueError):
                continue
            if 0 <= lid < len(batch_items):
                global_indices.append(batch_items[lid][0])
        if global_indices:
            clusters.append((label, global_indices))
    
    # Ensure all items are covered
    covered = set()
    for _, indices in clusters:
        covered.update(indices)
    for j, (idx, _) in enumerate(batch_items):
        if idx not in covered:
            clusters.append((f"{topic}_orphan", [idx]))
    
    return batch_idx, topic, clusters

def select_primary(indices, data):
    """Select primary record for a cluster."""
    best = None
    best_score = -1
    for idx in indices:
        rec = data[idx]
        tier = SOURCE_TIERS.get(get_source_tier(rec.get('a', '')), 0.5)
        score = rec.get('sc', 0) or 0
        date = rec.get('d', '')
        # Composite: tier_weight * 2 + score + date_rank
        composite = tier * 2 + score
        if composite > best_score:
            best_score = composite
            best = idx
    return best

def rebuild_chunks(data):
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
    parser = argparse.ArgumentParser(description='Event clustering for tech-db')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS)
    parser.add_argument('--batch', type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    data = json.loads(LITE.read_text('utf-8'))
    
    # Only cluster relevant records
    relevant_indices = [i for i, r in enumerate(data)
                        if r.get('c', '') and r.get('c') not in ('不相关', '未分类', '')]
    
    print(f"总记录: {len(data)}", flush=True)
    print(f"相关记录: {len(relevant_indices)}", flush=True)
    
    # Group by topic
    topic_groups = defaultdict(list)
    for i in relevant_indices:
        topic = data[i].get('tg', '') or data[i].get('tp', '') or '未知主题'
        topic_groups[topic].append(i)
    
    print(f"不同主题数: {len(topic_groups)}", flush=True)
    
    # Build batches: each batch = records from same topic
    batches = []
    for topic, indices in topic_groups.items():
        batch_records = [(idx, data[idx]) for idx in indices]
        if len(batch_records) <= 1:
            # Single record = its own cluster
            batches.append((len(batches), topic, batch_records, True))  # skip_api=True
        else:
            # Split large topics into sub-batches
            for i in range(0, len(batch_records), args.batch):
                chunk = batch_records[i:i+args.batch]
                batches.append((len(batches), topic, chunk, len(chunk) <= 1))
    
    api_batches = [b for b in batches if not b[3]]
    skip_batches = [b for b in batches if b[3]]
    
    print(f"总批次: {len(batches)} (API: {api_batches}, 自动: {skip_batches})", flush=True)
    print(f"预估API调用: {len(api_batches)} 次", flush=True)
    
    # Reset existing cluster info
    for i in relevant_indices:
        data[i].pop('cl', None)
        data[i].pop('cln', None)
        data[i].pop('cp', None)
    
    lock = threading.Lock()
    cluster_counter = 0
    batch_counter = 0
    multi_clusters = 0
    total_records_clustered = 0
    start_time = time.time()
    
    WAVE = args.workers * 3
    
    for wave_start in range(0, len(api_batches), WAVE):
        wave = api_batches[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {}
            for b in wave:
                futures[executor.submit(cluster_topic_batch, b[0], b[1], b[2])] = b
            
            for future in as_completed(futures):
                batch_idx, topic, clusters = future.result()
                with lock:
                    for event_label, global_indices in clusters:
                        cluster_counter += 1
                        cluster_id = hashlib.md5(f"{topic}_{cluster_counter}".encode()).hexdigest()[:8]
                        
                        if len(global_indices) > 1:
                            multi_clusters += 1
                            primary_idx = select_primary(global_indices, data)
                            for idx in global_indices:
                                data[idx]['cl'] = cluster_id
                                data[idx]['cln'] = event_label
                                data[idx]['cp'] = 1 if idx == primary_idx else 0
                        else:
                            idx = global_indices[0]
                            data[idx]['cl'] = cluster_id
                            data[idx]['cln'] = event_label
                            data[idx]['cp'] = 1
                        
                        total_records_clustered += len(global_indices)
                    
                    batch_counter += 1
                    elapsed = time.time() - start_time
                    spd = batch_counter / max(elapsed, 1)
                    remain = len(api_batches) - batch_counter
                    eta_m = remain / spd / 60 if spd > 0 else 999
                    
                    print(f"  [{batch_counter}/{len(api_batches)}] 主题={topic[:15]} "
                          f"多事件聚类={multi_clusters} 总记录={total_records_clustered} "
                          f"ETA={eta_m:.0f}m", flush=True)
        
        # Save after each wave
        with lock:
            rebuild_chunks(data)
    
    # Handle skip batches (single records)
    for batch_idx, topic, batch_items, skip in skip_batches:
        cluster_counter += 1
        cluster_id = hashlib.md5(f"{topic}_{cluster_counter}".encode()).hexdigest()[:8]
        for idx, _ in batch_items:
            data[idx]['cl'] = cluster_id
            data[idx]['cln'] = topic[:15]
            data[idx]['cp'] = 1
    
    # Final save
    rebuild_chunks(data)
    
    # Stats
    clustered = sum(1 for r in data if r.get('cl'))
    multi_member = sum(1 for r in data if r.get('cl') and not r.get('cp'))
    primaries = sum(1 for r in data if r.get('cp') == 1)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*50}", flush=True)
    print(f"聚类完成！", flush=True)
    print(f"耗时: {elapsed/60:.1f}m", flush=True)
    print(f"已聚类记录: {clustered} 条", flush=True)
    print(f"主条记录(cp=1): {primaries} 条", flush=True)
    print(f"多成员集群: {multi_clusters} 个", flush=True)
    print(f"非主条(cp=0): {multi_member} 条", flush=True)
    
    # Show top clusters by size
    cluster_sizes = defaultdict(list)
    for i, r in enumerate(data):
        if r.get('cl'):
            cluster_sizes[r['cl']].append((i, r))
    
    top_clusters = sorted(cluster_sizes.items(), key=lambda x: -len(x[1]))[:10]
    print(f"\nTop 10 最大聚类:", flush=True)
    for cid, members in top_clusters:
        if len(members) <= 1:
            continue
        primary = next((m for i, m in members if m.get('cp') == 1), members[0][1])
        label = primary.get('cln', '')
        title = primary.get('t', '')[:40]
        print(f"  [{cid}] {label} ({len(members)}条) → {title}", flush=True)

if __name__ == '__main__':
    main()
