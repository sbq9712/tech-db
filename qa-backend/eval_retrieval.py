"""Evaluation script for hybrid retrieval.

Runs the golden set against vector-only, BM25-only, and hybrid (RRF) retrieval.
Reports recall@25 and per-question hit/miss.
"""
import json, sys, pickle, time, os
import numpy as np
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

INDEX_DIR = REPO / "data" / "lightrag"
VECTOR_FILE = INDEX_DIR / "vector_index_v2.pkl"
BM25_FILE = INDEX_DIR / "bm25_index.pkl"
DICT_FILE = INDEX_DIR / "jieba_custom_dict.txt"

from eval_golden import GOLDEN_SET

IRRELEVANT_CATS = {"不相关", "未分类", "手动导入", ""}
TOP_K = 25
RRF_K = 60


def load_vector_index():
    if not VECTOR_FILE.exists():
        print(f"  Vector index not found: {VECTOR_FILE}")
        return None, None
    with open(VECTOR_FILE, "rb") as f:
        data = pickle.load(f)
    return data["embeddings"], data["meta"]


def load_bm25_index():
    if not BM25_FILE.exists():
        print(f"  BM25 index not found: {BM25_FILE}")
        return None, None, None
    with open(BM25_FILE, "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["meta"], data["corpus_tokens"]


def vector_search(query_vec, embeddings, meta, top_k=TOP_K):
    """Return list of (record_idx, score) sorted by score desc."""
    scores = embeddings @ query_vec
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [(meta[i]["idx"], float(scores[i])) for i in top_indices]


def bm25_search(query, bm25, meta, top_k=TOP_K):
    """Return list of (record_idx, score) sorted by score desc."""
    tokens = list(jieba.cut_for_search(query))
    scores = bm25.get_scores(tokens)
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [(meta[i]["idx"], float(scores[i])) for i in top_indices]


def rrf_fuse(results_list, k=RRF_K, top_k=TOP_K):
    """Reciprocal Rank Fusion of multiple result lists.

    Each result_list is [(record_idx, score), ...].
    Returns fused [(record_idx, rrf_score), ...] sorted desc.
    """
    rrf_scores = {}
    for results in results_list:
        for rank, (rec_idx, _) in enumerate(results):
            if rec_idx not in rrf_scores:
                rrf_scores[rec_idx] = 0.0
            rrf_scores[rec_idx] += 1.0 / (rank + k)

    fused = sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_k]
    return fused


def compute_recall(retrieved_ids, correct_ids, k=TOP_K):
    """Recall@k: fraction of correct records found in top-k."""
    retrieved_set = set(retrieved_ids[:k])
    correct_set = set(correct_ids)
    if not correct_set:
        return 0.0
    return len(retrieved_set & correct_set) / len(correct_set)


def run_evaluation(embeddings=None, vec_meta=None, bm25=None, bm25_meta=None,
                   query_embed_func=None):
    """Run all golden questions and report results."""

    print(f"\n{'='*70}")
    print(f"  Retrieval Evaluation — {len(GOLDEN_SET)} questions, recall@{TOP_K}")
    print(f"{'='*70}\n")

    methods = []

    # Prepare methods
    if embeddings is not None:
        methods.append(("vector", "向量"))
    if bm25 is not None:
        methods.append(("bm25", "BM25"))
    if embeddings is not None and bm25 is not None:
        methods.append(("hybrid", "混合(RRF)"))

    results_by_method = {}

    for method_id, method_name in methods:
        recalls = []
        colloquial_recalls = []
        direct_recalls = []
        hits = 0

        print(f"--- {method_name} ---")

        for entry in GOLDEN_SET:
            q = entry["q"]
            correct = entry["correct"]
            q_type = entry["type"]

            if method_id == "vector":
                query_vec = query_embed_func(q)
                results = vector_search(query_vec, embeddings, vec_meta)
            elif method_id == "bm25":
                results = bm25_search(q, bm25, bm25_meta)
            elif method_id == "hybrid":
                query_vec = query_embed_func(q)
                vec_results = vector_search(query_vec, embeddings, vec_meta)
                bm25_results = bm25_search(q, bm25, bm25_meta)
                fused = rrf_fuse([vec_results, bm25_results])
                results = [(idx, score) for idx, score in fused]

            retrieved_ids = [r[0] for r in results]
            recall = compute_recall(retrieved_ids, correct)

            recalls.append(recall)
            if q_type == "colloquial":
                colloquial_recalls.append(recall)
            else:
                direct_recalls.append(recall)

            hit = "✅" if recall > 0 else "❌"
            if recall > 0:
                hits += 1

            print(f"  {hit} [{q_type:10s}] recall={recall:.2f} | {q[:40]}")

        avg_recall = np.mean(recalls)
        avg_colloq = np.mean(colloquial_recalls) if colloquial_recalls else 0
        avg_direct = np.mean(direct_recalls) if direct_recalls else 0

        results_by_method[method_id] = {
            "avg_recall": avg_recall,
            "hit_rate": hits / len(GOLDEN_SET),
            "direct_recall": avg_direct,
            "colloquial_recall": avg_colloq,
        }

        print(f"\n  📊 {method_name} 结果:")
        print(f"     平均 recall@{TOP_K}: {avg_recall:.2%}")
        print(f"     命中率 (至少找到1条): {hits}/{len(GOLDEN_SET)} ({hits/len(GOLDEN_SET):.0%})")
        print(f"     直接提问 recall: {avg_direct:.2%}")
        print(f"     口语化提问 recall: {avg_colloq:.2%}\n")

    # Summary comparison
    if len(methods) > 1:
        print(f"{'='*70}")
        print(f"  对比总结")
        print(f"{'='*70}")
        for method_id, method_name in methods:
            r = results_by_method[method_id]
            print(f"  {method_name:15s}: recall={r['avg_recall']:.2%}, 命中={r['hit_rate']:.0%}, "
                  f"直接={r['direct_recall']:.2%}, 口语={r['colloquial_recall']:.2%}")


if __name__ == "__main__":
    import asyncio
    from config import embedding_func

    # Load jieba dict
    if DICT_FILE.exists():
        jieba.load_userdict(str(DICT_FILE))

    # Load indices
    print("Loading indices...")
    embeddings, vec_meta = load_vector_index()
    bm25, bm25_meta, _ = load_bm25_index()

    if embeddings is None:
        print("Cannot run evaluation without vector index.")
        sys.exit(1)

    # Embedding function for queries
    async def get_query_embedding(query):
        emb = await embedding_func([query])
        return np.array(emb[0], dtype=np.float32)

    def query_embed_func(query):
        return asyncio.run(get_query_embedding(query))

    run_evaluation(
        embeddings=embeddings,
        vec_meta=vec_meta,
        bm25=bm25,
        bm25_meta=bm25_meta,
        query_embed_func=query_embed_func,
    )
