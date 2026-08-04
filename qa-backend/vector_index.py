"""
Vector index builder — v2 (canonical set + refined embedding text).

Builds a numpy cosine-similarity index from the canonical record set:
  valid category AND dp != 1 (non-duplicate).

Embedding text = leaf category + title + tags + type + key params + AI summary.
No body text (BM25 handles full-text keyword matching). No truncation.
"""
import json, sys, time, asyncio, pickle, os
import numpy as np
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import embedding_func, EMBEDDING_DIM, WORKING_DIR

LITE = REPO / "data" / "processed" / "all-records-lite.json"
INDEX_DIR = WORKING_DIR
INDEX_FILE = INDEX_DIR / "vector_index_v2.pkl"

BATCH_SIZE = 64  # Reduced from 128 to avoid memory pressure

# Categories excluded from the canonical set
IRRELEVANT_CATS = {"不相关", "未分类", "手动导入", ""}


def format_record_text(rec: dict) -> str:
    """Format a record into concise, high-signal text for dense embedding.

    Composition (no truncation, no body):
      [leaf_category] title [tags] [type] key_params ai_summary
    """
    parts = []

    cat = rec.get("c", "") or ""
    if cat and cat not in IRRELEVANT_CATS:
        leaf = cat.split("/")[-1]
        if leaf:
            parts.append(f"[{leaf}]")

    title = rec.get("t", "") or ""
    if title:
        parts.append(title)

    tg = rec.get("tg", "") or ""
    if tg:
        parts.append(f"[{tg}]")

    tp = rec.get("tp", "") or ""
    if tp:
        parts.append(f"[{tp}]")

    kp = rec.get("kp", []) or []
    if isinstance(kp, list) and kp:
        parts.append("; ".join(str(k) for k in kp))

    as_text = rec.get("as", "") or ""
    if as_text:
        parts.append(as_text)

    return " ".join(parts)


async def build_index():
    print(f"[1/3] Loading records from {LITE.name}...", flush=True)
    data = json.loads(LITE.read_text("utf-8"))
    print(f"  Total records in file: {len(data)}", flush=True)

    # Build canonical set: valid category AND dp != 1 (non-duplicate)
    records = []
    for i, rec in enumerate(data):
        cat = rec.get("c", "")
        dp = rec.get("dp", 0)
        if cat in IRRELEVANT_CATS or dp == 1:
            continue
        records.append((i, rec))

    print(f"  Canonical set (valid & non-dup): {len(records)}", flush=True)

    # ── Incremental mode: load existing index, only embed NEW records ──
    existing_embeddings = None
    existing_meta = []
    if INDEX_FILE.exists():
        print("  Index already exists. Checking...", flush=True)
        try:
            with open(INDEX_FILE, "rb") as f:
                saved = pickle.load(f)
            saved_count = len(saved["embeddings"])
            indexed_ids = {m["idx"] for m in saved["meta"]}
            # Find records not yet in the index
            new_records = [(i, r) for i, r in records if i not in indexed_ids]

            if not new_records:
                if saved_count == len(records):
                    print("  Index is complete!", flush=True)
                    return
                else:
                    # Meta count mismatch but all idx present — shouldn't happen, rebuild
                    print(f"  [WARN] Meta inconsistency ({saved_count} embs vs {len(saved['meta'])} meta). Full rebuild.", flush=True)
                    records = records  # proceed with full build below
            else:
                print(f"  Index has {saved_count}/{len(records)} records. "
                      f"Incremental: {len(new_records)} new to embed.", flush=True)
                existing_embeddings = saved["embeddings"]
                existing_meta = saved["meta"]
                records = new_records  # only embed the new ones
        except Exception as e:
            print(f"  Index corrupted ({e}). Rebuilding from scratch...", flush=True)

    print(f"\n[2/3] Embedding {len(records)} records (batch_size={BATCH_SIZE})...", flush=True)

    all_embeddings = []
    all_meta = []
    start_time = time.time()

    # Process in batches
    total_batches = (len(records) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(records))
        batch = records[start:end]

        texts = [format_record_text(rec) for _, rec in batch]

        try:
            embeddings = await embedding_func(texts)
            embeddings = np.array(embeddings, dtype=np.float32)
            all_embeddings.append(embeddings)

            for orig_idx, rec in batch:
                all_meta.append({
                    "idx": orig_idx,
                    "id": rec.get("id", ""),
                    "t": rec.get("t", ""),
                    "d": rec.get("d", ""),
                    "s": rec.get("s", ""),
                    "c": rec.get("c", ""),
                    "tg": rec.get("tg", []),
                    "sc": rec.get("sc", 0),
                    "u": rec.get("u", ""),
                })
        except Exception as e:
            print(f"  ERROR batch {batch_idx}: {e}", flush=True)
            # Add zero embeddings for failed batch
            all_embeddings.append(np.zeros((len(batch), EMBEDDING_DIM), dtype=np.float32))
            for orig_idx, rec in batch:
                all_meta.append({"idx": orig_idx, "t": rec.get("t", ""), "c": rec.get("c", "")})

        done = end
        elapsed = time.time() - start_time
        rate = done / max(elapsed, 1)
        eta = (len(records) - done) / max(rate, 0.1)

        if batch_idx % 10 == 0 or batch_idx == total_batches - 1:
            print(f"  [{done}/{len(records)}] Batch {batch_idx+1}/{total_batches} "
                  f"| {rate:.1f}/s ETA={eta/60:.0f}min", flush=True)

        # Save incrementally every 50 batches so server can use partial index
        if (batch_idx + 1) % 50 == 0 or batch_idx == total_batches - 1:
            try:
                # Merge with existing embeddings if in incremental mode
                new_embs = np.vstack(all_embeddings)
                if existing_embeddings is not None:
                    combined_embs = np.vstack([existing_embeddings, new_embs])
                    combined_meta = existing_meta + all_meta.copy()
                else:
                    combined_embs = new_embs
                    combined_meta = all_meta.copy()

                norms = np.linalg.norm(combined_embs, axis=1, keepdims=True)
                norms[norms == 0] = 1
                combined_embs = combined_embs / norms

                partial_data = {
                    "embeddings": combined_embs,
                    "meta": combined_meta,
                    "dim": EMBEDDING_DIM,
                }
                # Atomic save: write to temp file then rename
                tmp_file = str(INDEX_FILE) + ".tmp"
                with open(tmp_file, "wb") as f:
                    pickle.dump(partial_data, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.rename(tmp_file, str(INDEX_FILE))
                total_now = len(combined_meta)
                print(f"  💾 Saved index: {total_now} records "
                      f"({len(all_meta)} new + {len(existing_meta) if existing_embeddings is not None else 0} existing)", flush=True)
            except Exception as e:
                print(f"  ⚠️ Partial save failed: {e}", flush=True)
    
    print(f"\n[3/3] Saving final index...", flush=True)
    new_embeddings = np.vstack(all_embeddings)

    # Merge with existing if incremental
    if existing_embeddings is not None:
        final_embeddings = np.vstack([existing_embeddings, new_embeddings])
        final_meta = existing_meta + all_meta
    else:
        final_embeddings = new_embeddings
        final_meta = all_meta

    # Normalize embeddings for cosine similarity
    norms = np.linalg.norm(final_embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    final_embeddings = final_embeddings / norms

    index_data = {
        "embeddings": final_embeddings,
        "meta": final_meta,
        "dim": EMBEDDING_DIM,
    }

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic save: write to temp file then rename
    tmp_file = str(INDEX_FILE) + ".tmp"
    with open(tmp_file, "wb") as f:
        pickle.dump(index_data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.rename(tmp_file, str(INDEX_FILE))

    elapsed = time.time() - start_time
    size_mb = INDEX_FILE.stat().st_size / 1024 / 1024
    print(f"  Saved {len(final_meta)} embeddings ({size_mb:.0f}MB) in {elapsed/60:.1f}min", flush=True)
    print(f"  Index file: {INDEX_FILE}", flush=True)
    print("\n✅ Vector index build complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(build_index())
