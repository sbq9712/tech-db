"""BM25 index builder for hybrid retrieval.

Builds a BM25 index from the canonical record set (same as vector index).
Uses jieba search engine mode + custom technical dictionary.
Persists to disk for fast cold-start.
"""
import json, sys, pickle, time, os
import numpy as np
from pathlib import Path
from collections import Counter

import jieba
import jieba.posseg as pseg
from rank_bm25 import BM25Okapi

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import WORKING_DIR
from primary_evidence import primary_bm25_text
from index_build_view import MigrationError, ensure_build_view
import index_build_view as _ibv

REPO = Path(__file__).resolve().parent.parent
LITE = Path(os.environ.get("TECH_DB_LITE_DATASET",
                           str(_ibv.DEFAULT_DATASET)))
INDEX_DIR = WORKING_DIR
BM25_FILE = INDEX_DIR / "bm25_index.pkl"
DICT_FILE = INDEX_DIR / "jieba_custom_dict.txt"

IRRELEVANT_CATS = {"不相关", "未分类", "手动导入", ""}


def build_custom_dict(records: list) -> set:
    """Extract technical terms from structured fields for jieba custom dictionary."""
    terms = set()

    # 1. Leaf category names (e.g. "钙钛矿", "锂电池")
    for r in records:
        cat = r.get("c", "")
        if cat and cat not in IRRELEVANT_CATS:
            leaf = cat.split("/")[-1]
            if len(leaf) >= 2:
                terms.add(leaf)

    # 2. Key parameter field names (e.g. "装机容量", "换电站数量")
    for r in records:
        for kp in (r.get("kp", []) or []):
            kp = str(kp)
            if "[" in kp:
                term = kp.split("[")[0].strip()
            elif ":" in kp:
                term = kp.split(":")[0].strip()
            else:
                term = kp.strip()
            if term and len(term) >= 2:
                terms.add(term)

    # 3. Type field (tp) with frequency >= 2 (filter noise)
    tp_counts = Counter(r.get("tp", "") for r in records if r.get("tp", "").strip())
    for tp, count in tp_counts.items():
        if count >= 2 and len(tp) >= 2:
            terms.add(tp)

    # 4. Tags (tg)
    for r in records:
        tg = r.get("tg", "").strip()
        if tg:
            terms.add(tg)

    return terms


def save_jieba_dict(terms: set, path: Path):
    """Save custom dictionary in jieba format."""
    with open(path, "w", encoding="utf-8") as f:
        for term in sorted(terms):
            f.write(f"{term} 999 nr\n")  # high weight, mixed POS
    print(f"  Custom dict saved: {len(terms)} terms → {path.name}", flush=True)


def format_bm25_text(rec: dict) -> str:
    """Source-grounded full text for the primary BM25 index.

    BM25 thrives on text volume — include everything available.
    """
    return primary_bm25_text(rec)


def tokenize(text: str) -> list:
    """Tokenize using jieba search engine mode (better recall)."""
    return list(jieba.cut_for_search(text))


def build_bm25_index():
    print("=" * 60, flush=True)
    print("  BM25 Index Builder (jieba + rank_bm25)", flush=True)
    print("=" * 60, flush=True)

    # 1. Load records — through the stable-ID migration adapter (Phase-02
    # review, legacy_hybrid compatibility): a legacy dataset without inline
    # record_id is decorated from a validated, dataset-pinned RecordIdMap
    # build view; missing/invalid map fails closed with the migration
    # command. Never a legacy-idx pseudo-ID, never a silent fallback.
    print(f"\n[1/4] Loading records...", flush=True)
    try:
        data, view_info = ensure_build_view(LITE, _ibv.DEFAULT_MAP)
    except MigrationError as exc:
        raise RuntimeError(str(exc)) from exc
    print(f"  Build view: {view_info['source']} — {view_info['records']} "
          f"canonical inputs (dataset {view_info['dataset_snapshot_id'][:19]}…)",
          flush=True)
    if view_info.get("quarantined") or view_info.get("duplicates"):
        print(f"  ⚠ build view exclusions: {view_info.get('quarantined', 0)} "
              "quarantined (no auditable identity), "
              f"{view_info.get('duplicates', 0)} logical duplicates — "
              "both audited in the RecordIdMap, never indexed blind",
              flush=True)

    # Build canonical set (same as vector index); the migration build view
    # injects each record's explicit legacy dataset idx
    canonical = []
    for i, rec in enumerate(data):
        cat = rec.get("c", "")
        dp = rec.get("dp", 0)
        if cat not in IRRELEVANT_CATS and dp != 1:
            canonical.append((int(rec.get("idx", i)), rec))
    print(f"  Canonical set: {len(canonical)} records", flush=True)

    # 2. Build custom dictionary
    print(f"\n[2/4] Building custom jieba dictionary...", flush=True)
    all_records = [r for _, r in canonical]
    terms = build_custom_dict(all_records)
    save_jieba_dict(terms, DICT_FILE)

    # Load dict into jieba
    jieba.load_userdict(str(DICT_FILE))
    print(f"  Loaded {len(terms)} custom terms into jieba", flush=True)

    # 3. Tokenize all documents
    print(f"\n[3/4] Tokenizing {len(canonical)} documents...", flush=True)
    start_time = time.time()
    corpus = []
    meta = []
    for idx, (orig_idx, rec) in enumerate(canonical):
        text = format_bm25_text(rec)
        tokens = tokenize(text)
        corpus.append(tokens)
        meta.append({
            "idx": orig_idx,
            "record_id": rec.get("record_id", ""),
            "t": rec.get("t", ""),
            "c": rec.get("c", ""),
            "d": rec.get("d", ""),
        })
        if (idx + 1) % 5000 == 0:
            elapsed = time.time() - start_time
            print(f"  [{idx+1}/{len(canonical)}] {elapsed:.0f}s elapsed", flush=True)

    elapsed = time.time() - start_time
    print(f"  Tokenized {len(corpus)} docs in {elapsed:.0f}s", flush=True)

    # 4. Build BM25 index
    print(f"\n[4/4] Building BM25 index...", flush=True)
    bm25 = BM25Okapi(corpus)

    # Persist
    index_data = {
        "bm25": bm25,
        "meta": meta,
        "corpus_tokens": corpus,  # needed for BM25Okapi.get_scores()
    }

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = str(BM25_FILE) + ".tmp"
    with open(tmp_file, "wb") as f:
        pickle.dump(index_data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.rename(tmp_file, str(BM25_FILE))

    size_mb = BM25_FILE.stat().st_size / 1024 / 1024
    total_elapsed = time.time() - start_time
    print(f"\n✅ BM25 index saved: {len(meta)} docs, {size_mb:.0f}MB, {total_elapsed:.0f}s", flush=True)
    print(f"  File: {BM25_FILE}", flush=True)

    # Quick sanity check
    print(f"\n--- Sanity check ---", flush=True)
    test_queries = ["钙钛矿太阳能电池", "固态电池", "人工智能"]
    for q in test_queries:
        tokens = tokenize(q)
        scores = bm25.get_scores(tokens)
        top5 = np.argsort(scores)[::-1][:5]
        print(f"  '{q}' → top: ", end="")
        for ti in top5:
            print(f"[{meta[ti]['t'][:25]}={scores[ti]:.1f}] ", end="")
        print()


if __name__ == "__main__":
    build_bm25_index()
