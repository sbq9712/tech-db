#!/usr/bin/env python3
"""T002 — Retrieval evaluation runner.

Runs golden cases through the retrieval pipeline and reports metrics.

Usage:
    cd qa-backend
    python eval/run_retrieval_eval.py [--vector-only] [--bm25-only] [--hybrid]

Metrics:
    Recall@25, MRR, nDCG@25
"""
import asyncio
import json
import sys
import pickle
import time
import os
import numpy as np
from pathlib import Path

# Setup paths
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO))

from config import WORKING_DIR, embedding_func
from metrics import recall_at_k, mrr, ndcg_at_k
from report import generate_report, save_report, print_report

# Import golden cases
from eval_golden import GOLDEN_SET

INDEX_DIR = WORKING_DIR
VECTOR_FILE = INDEX_DIR / "vector_index_v2.pkl"
BM25_FILE = INDEX_DIR / "bm25_index.pkl"
DICT_FILE = INDEX_DIR / "jieba_custom_dict.txt"

TOP_K = 25
RRF_K = 60


def load_vector_index():
    if not VECTOR_FILE.exists():
        print(f"❌ Vector index not found: {VECTOR_FILE}")
        return None, None
    with open(VECTOR_FILE, "rb") as f:
        data = pickle.load(f)
    return data["embeddings"], data["meta"]


def load_bm25_index():
    if not BM25_FILE.exists():
        print(f"❌ BM25 index not found: {BM25_FILE}")
        return None, None, None
    with open(BM25_FILE, "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["meta"], data.get("corpus_tokens")


def vector_search(query_vec, embeddings, meta, top_k=TOP_K):
    scores = embeddings @ query_vec
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [(meta[i]["idx"], float(scores[i])) for i in top_indices]


def bm25_search(query, bm25, meta, top_k=TOP_K):
    import jieba
    tokens = list(jieba.cut_for_search(query))
    if not tokens:
        return []
    scores = bm25.get_scores(tokens)
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [(meta[i]["idx"], float(scores[i])) for i in top_indices if scores[i] > 0]


def rrf_fuse(vec_results, bm25_results, k=RRF_K, top_k=TOP_K):
    rrf_scores = {}
    for rank, (idx, score) in enumerate(vec_results):
        rrf_scores.setdefault(idx, 0.0)
        rrf_scores[idx] += 1.0 / (rank + k)
    for rank, (idx, score) in enumerate(bm25_results):
        rrf_scores.setdefault(idx, 0.0)
        rrf_scores[idx] += 1.0 / (rank + k)
    return sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_k]


async def run_evaluation(method: str = "all"):
    """Run retrieval evaluation.

    Args:
        method: "vector" | "bm25" | "hybrid" | "all"
    """
    print(f"\n{'='*70}")
    print(f"  Retrieval Evaluation — {len(GOLDEN_SET)} golden cases")
    print(f"{'='*70}\n")

    # Load jieba dict
    if DICT_FILE.exists():
        import jieba
        jieba.load_userdict(str(DICT_FILE))

    # Load indices
    print("Loading indices...")
    embeddings, vec_meta = load_vector_index()
    bm25, bm25_meta, _ = load_bm25_index()

    if embeddings is None and bm25 is None:
        print("❌ No indices available!")
        return

    # Embedding function for queries
    async def get_query_embedding(query):
        emb = await embedding_func([query])
        return np.array(emb[0], dtype=np.float32)

    # Determine methods to run
    methods = []
    if method in ("all", "vector") and embeddings is not None:
        methods.append(("vector", "向量"))
    if method in ("all", "bm25") and bm25 is not None:
        methods.append(("bm25", "BM25"))
    if method in ("all", "hybrid") and embeddings is not None and bm25 is not None:
        methods.append(("hybrid", "混合(RRF)"))

    all_reports = {}

    for method_id, method_name in methods:
        print(f"\n--- {method_name} ---")
        results = []

        for entry in GOLDEN_SET:
            q = entry["q"]
            correct = entry["correct"]

            if method_id == "vector":
                query_vec = await get_query_embedding(q)
                retrieved = vector_search(query_vec, embeddings, vec_meta)
            elif method_id == "bm25":
                retrieved = bm25_search(q, bm25, bm25_meta)
            elif method_id == "hybrid":
                query_vec = await get_query_embedding(q)
                vec_r = vector_search(query_vec, embeddings, vec_meta)
                bm25_r = bm25_search(q, bm25, bm25_meta)
                retrieved = rrf_fuse(vec_r, bm25_r)

            retrieved_ids = [r[0] for r in retrieved]

            r = recall_at_k(retrieved_ids, correct, k=TOP_K)
            m = mrr(retrieved_ids, correct)
            nd = ndcg_at_k(retrieved_ids, correct, k=TOP_K)

            hit = "✅" if r > 0 else "❌"
            results.append({
                "q": q[:60],
                "type": entry.get("type", "?"),
                "correct": correct,
                "retrieved_top5": retrieved_ids[:5],
                "recall@25": r,
                "mrr": m,
                "ndcg@25": nd,
            })

            print(f"  {hit} [{entry.get('type', '?'):8s}] recall={r:.0%} mrr={m:.3f} | {q[:50]}")

        report = generate_report(results, mode="retrieval")
        report["method"] = method_id
        all_reports[method_id] = report
        print_report(report)

    # Save combined report
    output_path = str(REPO / "evaluation_report.json")
    combined = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "methods": {k: v["metrics"] for k, v in all_reports.items()},
        "detailed": all_reports,
    }
    Path(output_path).write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 Report saved to: {output_path}")

    return all_reports


if __name__ == "__main__":
    method = "all"
    if len(sys.argv) > 1:
        method = sys.argv[1].strip("--")
    asyncio.run(run_evaluation(method))
