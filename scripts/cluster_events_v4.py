#!/usr/bin/env python3
"""
tech-db 事件聚类 v4 — GLM事件签名法

核心思路：
1. 对每条情报调一次GLM，生成标准化的"事件签名"（如"三星HBM4量产"）
2. 相同签名的情报自动归为同一聚类
3. 在聚类组内选最权威的一条当主条(cp=0)

优势：
- 每条记录只调1次GLM，不分批次、不两两配对
- 跨语言：中文"钙钛矿叠层"和英文"perovskite tandem"会生成相同签名
- 无需embedding API
- 签名可复用于搜索/去重

成本：约54K条×1次GLM调用，与分类/评分同级
用法：python3 scripts/cluster_events_v4.py
"""
import json, subprocess, sys, time, hashlib, re, threading
from pathlib import Path
from collections import defaultdict
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

SIGNATURE_PROMPT = """为以下每条技术情报生成一个标准化的"事件签名"。
事件签名 = 用10-20个字概括这条情报报道的核心事件，格式为"[主体]+[动作]+[对象]"。

规则：
- 用中文描述，即使是英文情报也要翻译成中文签名
- 签名要足够具体，区分同领域不同事件
- 同一事件的不同报道（中英文、不同媒体）应生成相同或高度相似的签名
- 如果情报是广告/征稿/导航栏等噪声，签名标为"噪声"

示例：
- "三星HBM4内存销售额率先突破10亿美元" → 签名："三星HBM4内存量产销售"
- "Samsung HBM4 memory sales exceed $1B" → 签名："三星HBM4内存量产销售"
- "钙钛矿叠层电池效率破30%" → 签名："钙钛矿叠层电池效率突破"
- "Perovskite tandem cell hits 30%" → 签名："钙钛矿叠层电池效率突破"
- "OpenAI发布首款定制芯片" → 签名："OpenAI定制芯片发布"
- "OpenAI unveils first custom chip" → 签名："OpenAI定制芯片发布"
- "2026焦耳时代钙钛矿会议征稿" → 签名："噪声"

只输出JSON数组：[{"id":0,"sig":"事件签名"}]
待处理情报：
"""

def call_glm(batch_items):
    prompt = SIGNATURE_PROMPT + json.dumps(batch_items, ensure_ascii=False)
    try:
        r = subprocess.run(
            ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt],
            text=True, capture_output=True, timeout=TIMEOUT
        )
        output = (r.stdout or '').strip()
        if output.startswith('```'):
            lines = output.split('\n')
            output = '\n'.join(lines[1:])
            if output.endswith('```'):
                output = output[:-3].strip()
            if output.startswith('json'):
                output = output[4:].strip()
        m = re.search(r'\[.*\]', output, re.S)
        if m:
            return json.loads(m.group(0))
    except:
        pass
    return None

def normalize_sig(sig):
    """Normalize signature for matching"""
    s = sig.lower().strip()
    # Remove common suffixes/particles
    for w in ['的', '了', '最新', '突破', '进展', '研究', '报道', '新闻']:
        s = s.replace(w, '')
    return s[:20]

def select_parent(indices, data):
    """选主条：lv>0 > score最高 > 日期最新"""
    for idx in indices:
        if (data[idx].get('lv', 0) or 0) > 0:
            return idx
    return max(indices, key=lambda i: (data[i].get('sc', 0) or 0, data[i].get('d', '')))

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
    data = json.loads(LITE.read_text('utf-8'))
    
    # Only cluster relevant, non-duplicate records
    relevant = [i for i, r in enumerate(data)
                if r.get('c', '') and r.get('c') not in ('不相关', '未分类', '')
                and r.get('dp', 0) != 1]
    
    print(f"总记录: {len(data)}", flush=True)
    print(f"可聚类记录: {len(relevant)}", flush=True)
    
    # Clear existing cluster fields
    for i in range(len(data)):
        data[i].pop('cl', None)
        data[i].pop('cln', None)
        data[i].pop('cp', None)
    
    # Build batches
    items = []
    for bid, idx in enumerate(relevant):
        r = data[idx]
        items.append({"id": bid, "title": (r.get('t','') or '')[:100], "body": (r.get('b','') or '')[:200]})
    
    batches = [items[i:i+BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    print(f"GLM批次: {len(batches)}", flush=True)
    
    # Generate signatures
    signatures = {}  # batch_id → signature
    done = 0
    start_time = time.time()
    lock = threading.Lock()
    
    def process_batch(batch):
        return call_glm(batch)
    
    WAVE = MAX_WORKERS * 3
    for wave_start in range(0, len(batches), WAVE):
        wave = batches[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_batch, b): b for b in wave}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    result = future.result()
                    if result:
                        for item in result:
                            signatures[item['id']] = item.get('sig', '')
                except:
                    pass
                with lock:
                    done += 1
                    if done % 20 == 0:
                        elapsed = time.time() - start_time
                        spd = done / max(elapsed, 1)
                        eta_m = (len(batches) - done) / spd / 60 if spd > 0 else 999
                        print(f"  [{done}/{len(batches)}] sigs={len(signatures)} ETA={eta_m:.0f}m", flush=True)
        
        # Save intermediate signatures
        with open('/tmp/cluster_signatures.json', 'w') as f:
            json.dump(signatures, f, ensure_ascii=False)
    
    print(f"签名生成: {len(signatures)}/{len(relevant)}", flush=True)
    
    # Group by normalized signature
    sig_groups = defaultdict(list)
    for bid, sig in signatures.items():
        if sig and sig != '噪声':
            norm = normalize_sig(sig)
            idx = relevant[bid]
            sig_groups[norm].append(idx)
    
    # Build clusters (only multi-member groups)
    cluster_count = 0
    for norm_sig, indices in sig_groups.items():
        if len(indices) < 2:
            continue
        cluster_count += 1
        cid = hashlib.md5(f"{norm_sig}_{cluster_count}".encode()).hexdigest()[:8]
        parent_idx = select_parent(indices, data)
        for idx in indices:
            data[idx]['cl'] = cid
            data[idx]['cln'] = norm_sig[:15]
            data[idx]['cp'] = 0 if idx == parent_idx else 1
    
    # Stats
    clustered = sum(1 for r in data if r.get('cl'))
    cp1 = sum(1 for r in data if r.get('cp') == 1)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*50}", flush=True)
    print(f"聚类完成！耗时: {elapsed/60:.1f}m", flush=True)
    print(f"签名生成: {len(signatures)} 条", flush=True)
    print(f"多成员聚类: {cluster_count} 个", flush=True)
    print(f"有聚类记录: {clustered} 条", flush=True)
    print(f"子条(cp=1): {cp1} 条", flush=True)
    
    # Show sample clusters
    cl_groups = defaultdict(list)
    for i, r in enumerate(data):
        if r.get('cl'):
            cl_groups[r['cl']].append((i, r))
    
    multi = [(cid, members) for cid, members in cl_groups.items() if len(members) > 1]
    print(f"\n聚类示例（前10个）:", flush=True)
    for cid, members in multi[:10]:
        parent = next((r for i, r in members if r.get('cp') == 0), members[0][1])
        print(f"  [{cid}] {parent.get('cln','')} ({len(members)}条)", flush=True)
        for idx, r in members:
            tag = '父' if r.get('cp') == 0 else '子'
            print(f"    [{tag}] {r.get('t','')[:40]}", flush=True)
    
    rebuild_chunks(data)

if __name__ == '__main__':
    main()
