#!/usr/bin/env python3
"""
tech-db 事件聚类 — BGE-m3 embedding方案

流程：
1. 对所有可聚类记录(dp≠1, 非不相关/未分类)的标题+AI摘要生成embedding向量
2. 同一topic内，用余弦相似度>0.85配对
3. 并查集合并成聚类组
4. 每组选最权威的当父条(cp=0)，其余子条(cp=1)

优势：
- 零LLM调用、零token消耗
- 跨语言（中英文自动对齐）
- 速度快：54K条向量化约10分钟，相似度矩阵秒级
"""
import json, time, hashlib, sys
from pathlib import Path
from collections import defaultdict
import numpy as np

REPO = Path(__file__).resolve().parent.parent
LITE = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000
SIM_THRESHOLD = 0.82      # raised from 0.75 — was too loose, grouped same-domain-different-event
MAX_DATE_SPREAD = 14      # cluster members must be within 14 days of each other

def load_model():
    from sentence_transformers import SentenceTransformer
    print("Loading BGE-m3...", flush=True)
    model = SentenceTransformer('BAAI/bge-m3')
    print(f"Model loaded. Dim={model.get_sentence_embedding_dimension()}", flush=True)
    return model

def select_parent(indices, data):
    """选父条(cp=0)：lv>0优先 > score最高"""
    for idx in indices:
        if (data[idx].get('lv', 0) or 0) > 0:
            return idx
    return max(indices, key=lambda i: data[i].get('sc', 0) or 0)

def rebuild_chunks(data):
    LITE.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    # Build lite-part without b/fb fields for frontend
    for r in data:
        if r.get("as", "").strip():
            r.pop("b", None)
        if r.get("lv", 0) == 0 and r.get("fb", "").strip():
            r.pop("fb", None)
    
    for f in CHUNK_DIR.glob('lite-part-*.js'):
        f.unlink()
    chunks = [data[i:i+CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
    for ci, ch in enumerate(chunks):
        content = (f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];'
                   f'window.__LITE_PARTS__.push({json.dumps(ch, ensure_ascii=False, separators=(",", ":"))});')
        (CHUNK_DIR / f'lite-part-{ci}.js').write_text(content, 'utf-8')

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

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--threshold', type=float, default=SIM_THRESHOLD)
    args = parser.parse_args()

    model = load_model()
    
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
    
    # Group by topic
    topic_groups = defaultdict(list)
    for i in relevant:
        topic = data[i].get('tp', '') or data[i].get('tg', '') or '未知'
        topic_groups[topic].append(i)
    
    print(f"不同主题: {len(topic_groups)}", flush=True)
    
    # Step 1: Batch-encode all multi-record-topic records at once, then group
    print("\nStep 1: 生成embedding向量...", flush=True)
    start = time.time()
    
    # Only process topics with >1 record
    multi_topics = {t: v for t, v in topic_groups.items() if len(v) > 1}
    multi_indices = sorted(set(i for v in multi_topics.values() for i in v))
    print(f"  多记录主题: {len(multi_topics)}, 需编码记录: {len(multi_indices)}", flush=True)
    
    # Build texts for all multi-topic records at once
    texts = []
    for idx in multi_indices:
        title = (data[idx].get('t', '') or '')[:200]
        summary = (data[idx].get('as', '') or '')[:100]
        text = f"{title}。{summary}" if summary else title
        texts.append(text)
    
    # Batch encode (single call — much faster than per-topic)
    all_embeddings = model.encode(texts, show_progress_bar=True, batch_size=64)
    # Normalize once
    norms = np.linalg.norm(all_embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    all_embeddings_norm = all_embeddings / norms
    
    # Create index mapping: original data index → position in multi_indices
    idx2pos = {idx: pos for pos, idx in enumerate(multi_indices)}
    
    elapsed = time.time() - start
    print(f"Embedding完成，耗时: {elapsed/60:.1f}m", flush=True)
    
    uf = UnionFind()
    from datetime import datetime
    
    # Step 2: Compute pairwise similarity within each topic group
    topic_count = 0
    for topic, indices in multi_topics.items():
        topic_count += 1
        if topic_count % 200 == 0:
            print(f"  相似度计算... {topic_count}/{len(multi_topics)} 主题", flush=True)
        
        # Get positions in the batch embedding
        positions = [idx2pos[i] for i in indices]
        
        # Extract sub-embeddings and compute similarity matrix
        sub_emb = all_embeddings_norm[positions]
        sim_matrix = sub_emb @ sub_emb.T
        
        for i in range(len(positions)):
            for j in range(i+1, len(positions)):
                if sim_matrix[i, j] < args.threshold:
                    continue
                # Date check: skip if records are >MAX_DATE_SPREAD days apart
                di = data[indices[i]].get('d', '')[:10]
                dj = data[indices[j]].get('d', '')[:10]
                if di and dj:
                    try:
                        dt_i = datetime.strptime(di, '%Y-%m-%d')
                        dt_j = datetime.strptime(dj, '%Y-%m-%d')
                        if abs((dt_i - dt_j).days) > MAX_DATE_SPREAD:
                            continue
                    except:
                        pass
                uf.union(indices[i], indices[j])
    
    # Step 2: Build clusters from union-find
    root_members = defaultdict(list)
    for i in relevant:
        if i in uf.parent:
            root = uf.find(i)
            root_members[root].append(i)
    
    # Write cluster data (only multi-member)
    cluster_count = 0
    for root, members in root_members.items():
        if len(members) < 2:
            continue
        cluster_count += 1
        cid = hashlib.md5(f"{root}_{cluster_count}".encode()).hexdigest()[:8]
        label = (data[members[0]].get('tp', '') or data[members[0]].get('tg', ''))[:15]
        parent_idx = select_parent(members, data)
        for idx in members:
            data[idx]['cl'] = cid
            data[idx]['cln'] = label
            data[idx]['cp'] = 0 if idx == parent_idx else 1
    
    # Stats
    clustered = sum(1 for r in data if r.get('cl'))
    cp1 = sum(1 for r in data if r.get('cp') == 1)
    
    print(f"\n{'='*50}", flush=True)
    print(f"聚类完成！", flush=True)
    print(f"多成员聚类: {cluster_count} 个", flush=True)
    print(f"有聚类记录: {clustered} 条", flush=True)
    print(f"子条(cp=1): {cp1} 条", flush=True)
    
    # Show sample clusters
    cl_groups = defaultdict(list)
    for i, r in enumerate(data):
        if r.get('cl'):
            cl_groups[r['cl']].append((i, r))
    multi = [(cid, m) for cid, m in cl_groups.items() if len(m) > 1]
    print(f"\n聚类示例（前15个）:", flush=True)
    for cid, members in multi[:15]:
        parent = next((r for i, r in members if r.get('cp') == 0), members[0][1])
        print(f"  [{cid}] {parent.get('cln','')} ({len(members)}条)", flush=True)
        for idx, r in members[:3]:
            tag = '父' if r.get('cp') == 0 else '子'
            print(f"    [{tag}] {r.get('t','')[:50]}", flush=True)
    
    # Save
    print("\n保存数据...", flush=True)
    rebuild_chunks(data)
    print("完成!", flush=True)

if __name__ == '__main__':
    main()
