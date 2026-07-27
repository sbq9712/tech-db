#!/usr/bin/env python3
"""
tech-db 事件聚类脚本 v3（两两配对确认版）

核心逻辑：
1. n-gram预筛：同一topic内，标题3-gram重叠率>=15%的配对作为候选（免费，机械）
2. GLM确认：候选对发给GLM，看标题+正文前200字，语义判断是否同一事件
3. 并查集合并：所有确认的配对用并查集合并成聚类组

优势：
- 不分批次，无跨批次遗漏
- GLM看标题+正文，真正发挥语义理解价值
- 只对候选对调GLM，token可控
- 来源不作为判断标准

用法：
  python3 scripts/cluster_events_v3.py
  python3 scripts/cluster_events_v3.py --workers 6 --threshold 0.15
"""
import json, subprocess, sys, time, threading, hashlib, re
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = 'glm-5.2'
PROVIDER = 'zai'
REPO = Path(__file__).resolve().parent.parent
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000
MAX_WORKERS = 6
TIMEOUT = 60
NGRAM_THRESHOLD = 0.15  # 标题3-gram重叠率阈值

PAIR_PROMPT = """判断以下两条技术情报是否属于同一事件（同一技术进展/产品发布/政策出台/研究成果的不同报道）。

情报A：
标题：{title_a}
正文：{body_a}

情报B：
标题：{title_b}
正文：{body_b}

同一事件的例子：
- 同一论文的英文原文 + 中文解读
- 同一产品发布的中文报道 + 英文报道
- 同一政策的多个来源报道

不同事件的例子：
- 同领域但不同团队、不同产品
- 标题相似但内容讲的是不同的具体进展
- 会议征稿/广告/导航栏噪声混入

只回答JSON：{{"same": true/false}}"""

def title_ngrams(s, n=3):
    s = s.lower().strip()
    return set(s[i:i+n] for i in range(max(len(s)-n+1, 0))) if len(s) >= n else {s}

def ngram_overlap(t1, t2):
    g1, g2 = title_ngrams(t1), title_ngrams(t2)
    if not g1 or not g2:
        return 0
    return len(g1 & g2) / min(len(g1), len(g2))

def call_glm_pair(title_a, body_a, title_b, body_b):
    prompt = PAIR_PROMPT.format(
        title_a=title_a[:100], body_a=body_a[:200],
        title_b=title_b[:100], body_b=body_b[:200],
    )
    try:
        r = subprocess.run(
            ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt],
            text=True, capture_output=True, timeout=TIMEOUT
        )
        output = (r.stdout or '').strip()
        m = re.search(r'\{.*\}', output, re.S)
        if m:
            result = json.loads(m.group(0))
            return result.get('same', False)
    except:
        pass
    return False

# Union-Find for merging pairs into clusters
class UnionFind:
    def __init__(self):
        self.parent = {}
    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry

def select_parent(indices, data):
    """选父条(cp=0)：lv>0优先 > 分数最高"""
    for idx in indices:
        if (data[idx].get('lv', 0) or 0) > 0:
            return idx
    return max(indices, key=lambda i: data[i].get('sc', 0) or 0)

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
    import argparse
    parser = argparse.ArgumentParser(description='Event clustering v3 - pairwise confirmation')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS)
    parser.add_argument('--threshold', type=float, default=NGRAM_THRESHOLD)
    args = parser.parse_args()

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
    
    # Step 1: Group by topic
    topic_groups = defaultdict(list)
    for i in relevant:
        topic = data[i].get('tp', '') or data[i].get('tg', '') or '未知'
        topic_groups[topic].append(i)
    
    print(f"不同主题: {len(topic_groups)}", flush=True)
    
    # Step 2: n-gram prefilter within each topic to find candidate pairs
    candidates = []  # list of (idx_a, idx_b)
    for topic, indices in topic_groups.items():
        if len(indices) <= 1:
            continue
        for a in range(len(indices)):
            for b in range(a+1, len(indices)):
                ia, ib = indices[a], indices[b]
                overlap = ngram_overlap(data[ia].get('t', ''), data[ib].get('t', ''))
                if overlap >= args.threshold:
                    candidates.append((ia, ib))
    
    print(f"Step 1 完成: {len(candidates)} 候选对 (阈值={args.threshold})", flush=True)
    
    if not candidates:
        print("无候选对，聚类结束", flush=True)
        rebuild_chunks(data)
        return
    
    # Step 3: GLM pairwise confirmation
    uf = UnionFind()
    confirmed = 0
    done = 0
    lock = threading.Lock()
    start_time = time.time()
    
    def confirm_pair(ia, ib):
        return ia, ib, call_glm_pair(
            data[ia].get('t', ''), data[ia].get('b', ''),
            data[ib].get('t', ''), data[ib].get('b', ''),
        )
    
    WAVE = args.workers * 4
    for wave_start in range(0, len(candidates), WAVE):
        wave = candidates[wave_start:wave_start+WAVE]
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(confirm_pair, a, b): (a, b) for a, b in wave}
            for future in as_completed(futures):
                ia, ib, same = future.result()
                with lock:
                    if same:
                        uf.union(ia, ib)
                        confirmed += 1
                    done += 1
                    
                    if done % 50 == 0:
                        elapsed = time.time() - start_time
                        spd = done / max(elapsed, 1)
                        remain = len(candidates) - done
                        eta_m = remain / spd / 60 if spd > 0 else 999
                        print(f"  [{done}/{len(candidates)}] 确认={confirmed} ETA={eta_m:.0f}m", flush=True)
    
    print(f"Step 2 完成: {confirmed}/{len(candidates)} 配对确认", flush=True)
    
    # Step 4: Build clusters from union-find
    cluster_map = defaultdict(list)
    for ia, ib in candidates:
        root = uf.find(ia)
        if uf.find(ib) == root:
            # Same cluster - collect all members
            pass
    
    # Collect all members by root
    root_members = defaultdict(set)
    for i in relevant:
        if i in uf.parent:
            root = uf.find(i)
            root_members[root].add(i)
    
    # Write cluster data
    multi_count = 0
    for root, members in root_members.items():
        if len(members) < 2:
            continue
        member_list = sorted(members)
        multi_count += 1
        cid = hashlib.md5(f"{root}_{multi_count}".encode()).hexdigest()[:8]
        
        # Event label from first member's topic
        label = (data[member_list[0]].get('tp', '') or data[member_list[0]].get('tg', ''))[:15]
        
        parent_idx = select_parent(member_list, data)
        for idx in member_list:
            data[idx]['cl'] = cid
            data[idx]['cln'] = label
            data[idx]['cp'] = 0 if idx == parent_idx else 1
    
    # Stats
    clustered = sum(1 for r in data if r.get('cl'))
    cp1 = sum(1 for r in data if r.get('cp') == 1)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*50}", flush=True)
    print(f"聚类完成！耗时: {elapsed/60:.1f}m", flush=True)
    print(f"候选对: {len(candidates)}", flush=True)
    print(f"确认配对: {confirmed}", flush=True)
    print(f"多成员聚类: {multi_count} 个", flush=True)
    print(f"有聚类记录: {clustered} 条", flush=True)
    print(f"子条(cp=1): {cp1} 条", flush=True)
    
    rebuild_chunks(data)

if __name__ == '__main__':
    main()
