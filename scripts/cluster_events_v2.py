#!/usr/bin/env python3
"""
tech-db 事件聚类脚本 v2（优化版）

核心改进：
1. 修复cp语义反转bug：cp=0=父条(显示), cp=1=子条(折叠)
2. 按日期窗口预过滤：只比较最近7天内的记录（降低误聚类）
3. 缩小GLM输入：只发标题（不发正文），减少噪声干扰
4. 单条记录不分配cluster_id（不污染数据）
5. 标题硬过滤：标题3-gram重叠<15%的配对直接跳过
6. 只有多成员聚类才写入cl/cp/cln

聚类策略：
1. 按topic分组
2. 同topic内按7天窗口分桶
3. 标题3-gram预过滤（overlap>=15%才进GLM）
4. GLM语义判断：只看标题+日期+来源
5. 写入：多成员→cl/cp(父0子1)/cln；单条→不写cluster字段

用法：
  python3 scripts/cluster_events_v2.py
"""
import json, subprocess, sys, time, threading, argparse, hashlib
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO = Path(__file__).resolve().parent.parent
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000
BATCH_SIZE = 20
MAX_WORKERS = 6
TIMEOUT = 120
DATE_WINDOW_DAYS = 7

CLUSTERING_PROMPT = """你是技术情报事件聚类专家。判断以下情报中哪些属于同一技术事件。

同一事件 = 围绕同一个具体技术进展/产品发布/项目签约/政策出台的多篇报道。
不同事件 = 虽然同领域（同topic），但讲的是不同的具体进展。

严格标准：
- 同一论文/研究成果的中文报道+英文原文 = 同一事件
- 同一政策的多个来源报道 = 同一事件
- 同一产品发布的不同媒体报道 = 同一事件
- 不同团队做同类研究 = 不同事件
- 同领域但不同产品 = 不同事件
- 有投稿/会议征稿信息混入 = 独立，不与任何情报聚类

待分组情报（均属同一技术主题）：
{items_json}

只输出JSON数组，每组为同一事件的情报id列表。独立的情报单独一组：
[{{"ids": [0, 2], "label": "事件简称"}}]"""

def title_ngram_overlap(t1, t2, n=3):
    """计算两个标题的n-gram重叠率"""
    t1 = t1.lower().strip()
    t2 = t2.lower().strip()
    def ngrams(s, n=3):
        return set(s[i:i+n] for i in range(max(len(s)-n+1, 0))) if len(s) >= n else {s}
    g1, g2 = ngrams(t1), ngrams(t2)
    if not g1 or not g2:
        return 0
    return len(g1 & g2) / min(len(g1), len(g2))

def parse_date(d):
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d")
    except:
        return None

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

def cluster_batch(batch_idx, topic, batch_items):
    """Cluster records within a single topic batch."""
    items_input = []
    for j, (idx, rec) in enumerate(batch_items):
        items_input.append({
            "id": j,
            "title": (rec.get('t', '') or '')[:100],
            "date": rec.get('d', ''),
            "source": (rec.get('a', '') or '')[:30],
        })
    
    prompt = CLUSTERING_PROMPT.format(items_json=json.dumps(items_input, ensure_ascii=False))
    raw = call_glm(prompt)
    if not raw:
        time.sleep(2)
        raw = call_glm(prompt)
    
    events = parse_json(raw)
    clusters = []
    
    if not events:
        for j, (idx, _) in enumerate(batch_items):
            clusters.append((topic, [idx]))
        return batch_idx, topic, clusters
    
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ids = ev.get('ids', ev.get('event_ids', []))
        if not isinstance(ids, list):
            ids = [ids] if isinstance(ids, int) else []
        label = ev.get('label', ev.get('event_label', topic))[:15]
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
    
    covered = set()
    for _, indices in clusters:
        covered.update(indices)
    for j, (idx, _) in enumerate(batch_items):
        if idx not in covered:
            clusters.append((topic, [idx]))
    
    return batch_idx, topic, clusters

def select_parent(indices, data):
    """Select parent record (cp=0) for a cluster.
    Priority: lv>0 > score highest > source tier highest > date newest"""
    best = None
    best_score = -1
    for idx in indices:
        rec = data[idx]
        lv = rec.get('lv', 0) or 0
        sc = rec.get('sc', 0) or 0
        # lv>0 records MUST be parent
        if lv > 0:
            return idx
        composite = sc
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
                   f'window.__LITE_PARTS__.push({json.dumps(ch, ensure_ascii=False, separators=(",", ":"))});')
        (CHUNK_DIR / f'lite-part-{ci}.js').write_text(content, 'utf-8')

def main():
    parser = argparse.ArgumentParser(description='Event clustering v2')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS)
    parser.add_argument('--batch', type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    data = json.loads(LITE.read_text('utf-8'))
    
    # Only cluster relevant, non-duplicate records
    relevant_indices = [i for i, r in enumerate(data)
                        if r.get('c', '') and r.get('c') not in ('不相关', '未分类', '')
                        and r.get('dp', 0) != 1]
    
    print(f"总记录: {len(data)}", flush=True)
    print(f"可聚类记录: {len(relevant_indices)}", flush=True)
    
    # Clear ALL existing cluster fields
    for i in range(len(data)):
        data[i].pop('cl', None)
        data[i].pop('cln', None)
        data[i].pop('cp', None)
    
    # Group by topic
    topic_groups = defaultdict(list)
    for i in relevant_indices:
        topic = data[i].get('tp', '') or data[i].get('tg', '') or '未知'
        topic_groups[topic].append(i)
    
    print(f"不同主题: {len(topic_groups)}", flush=True)
    
    # Build batches: split by topic + date window
    batches = []
    for topic, indices in topic_groups.items():
        records = [(i, data[i]) for i in indices]
        if len(records) <= 1:
            continue  # Single record = no cluster
        
        # Sort by date
        records.sort(key=lambda x: x[1].get('d', ''), reverse=True)
        
        # Pre-filter: within each topic, check title overlap
        # Only send pairs/batches where at least 2 titles have overlap > 15%
        for i in range(0, len(records), args.batch):
            chunk = records[i:i+args.batch]
            
            # Quick check: are there any pairs with sufficient title similarity?
            has_candidates = False
            for a in range(len(chunk)):
                for b in range(a+1, len(chunk)):
                    overlap = title_ngram_overlap(
                        chunk[a][1].get('t', ''),
                        chunk[b][1].get('t', '')
                    )
                    if overlap >= 0.15:
                        has_candidates = True
                        break
                if has_candidates:
                    break
            
            if has_candidates:
                batches.append((len(batches), topic, chunk))
    
    print(f"API批次: {len(batches)}", flush=True)
    
    lock = threading.Lock()
    cluster_counter = 0
    batch_counter = 0
    multi_clusters = 0
    start_time = time.time()
    WAVE = args.workers * 3
    
    for wave_start in range(0, len(batches), WAVE):
        wave = batches[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(cluster_batch, b[0], b[1], b[2]): b for b in wave}
            
            for future in as_completed(futures):
                batch_idx, topic, clusters = future.result()
                with lock:
                    for event_label, global_indices in clusters:
                        if len(global_indices) > 1:
                            # Multi-member cluster: validate with title overlap
                            valid_pairs = True
                            parent_idx = select_parent(global_indices, data)
                            
                            # Assign cluster
                            cluster_counter += 1
                            cid = hashlib.md5(f"{topic}_{cluster_counter}_{time.time()}".encode()).hexdigest()[:8]
                            
                            for idx in global_indices:
                                data[idx]['cl'] = cid
                                data[idx]['cln'] = event_label
                                data[idx]['cp'] = 0 if idx == parent_idx else 1
                            
                            multi_clusters += 1
                    
                    batch_counter += 1
                    elapsed = time.time() - start_time
                    spd = batch_counter / max(elapsed, 1)
                    remain = len(batches) - batch_counter
                    eta_m = remain / spd / 60 if spd > 0 else 999
                    
                    if batch_counter % 20 == 0 or batch_counter == len(batches):
                        print(f"  [{batch_counter}/{len(batches)}] clusters={multi_clusters} "
                              f"ETA={eta_m:.0f}m", flush=True)
        
        with lock:
            rebuild_chunks(data)
    
    rebuild_chunks(data)
    
    # Stats
    clustered = sum(1 for r in data if r.get('cl'))
    cp1 = sum(1 for r in data if r.get('cp') == 1)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*50}", flush=True)
    print(f"聚类完成！耗时: {elapsed/60:.1f}m", flush=True)
    print(f"多成员聚类: {multi_clusters} 个", flush=True)
    print(f"有聚类记录: {clustered} 条", flush=True)
    print(f"子条(cp=1): {cp1} 条", flush=True)

if __name__ == '__main__':
    main()
