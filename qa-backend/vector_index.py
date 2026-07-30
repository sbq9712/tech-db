"""
Fast vector index builder for all records.
Creates a numpy-based cosine similarity index of all 58K records.
This runs in ~30-60 minutes (embedding time only, no LLM calls).
"""
import json, sys, time, asyncio, pickle, os
import numpy as np
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import embedding_func, EMBEDDING_DIM

LITE = REPO / "data" / "processed" / "all-records-lite.json"
INDEX_DIR = REPO / "data" / "lightrag"
INDEX_FILE = INDEX_DIR / "vector_index.pkl"

BATCH_SIZE = 128  # Embedding batch size (larger = faster on CPU)


def format_record_text(rec: dict) -> str:
    """Format a record into text for embedding. Keep it SHORT for speed."""
    title = rec.get("t", "") or ""
    ai_summary = rec.get("as", "") or ""
    body = rec.get("b", "") or rec.get("fb", "") or ""
    cat = rec.get("c", "") or ""

    # Use title + AI summary (preferred) or truncated body
    # Keep text short for fast embedding (~100-200 chars)
    if ai_summary:
        text = f"{title} {ai_summary[:200]}"
    elif body:
        text = f"{title} {body[:200]}"
    else:
        text = title

    if cat:
        text = f"[{cat}] {text}"

    return text[:300]  # Hard cap at 300 chars


async def build_index():
    print(f"[1/3] Loading records from {LITE.name}...", flush=True)
    data = json.loads(LITE.read_text("utf-8"))
    print(f"  Total records: {len(data)}", flush=True)
    
    # Filter relevant records
    records = []
    for i, rec in enumerate(data):
        cat = rec.get("c", "")
        if not cat or cat in ("不相关", "未分类", ""):
            continue
        records.append((i, rec))
    
    print(f"  Relevant records: {len(records)}", flush=True)
    
    # Check for existing progress
    texts_file = INDEX_DIR / "index_texts.json"
    if INDEX_FILE.exists():
        print("  Index already exists. Checking...", flush=True)
        try:
            with open(INDEX_FILE, "rb") as f:
                saved = pickle.load(f)
            if len(saved["embeddings"]) == len(records):
                print("  Index is complete!", flush=True)
                return
            print(f"  Index has {len(saved['embeddings'])}/{len(records)} records. Rebuilding...", flush=True)
        except Exception:
            print("  Index corrupted. Rebuilding...", flush=True)
    
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
                partial_embs = np.vstack(all_embeddings)
                norms = np.linalg.norm(partial_embs, axis=1, keepdims=True)
                norms[norms == 0] = 1
                partial_embs = partial_embs / norms

                partial_data = {
                    "embeddings": partial_embs,
                    "meta": all_meta.copy(),
                    "dim": EMBEDDING_DIM,
                }
                # Atomic save: write to temp file then rename
                tmp_file = str(INDEX_FILE) + ".tmp"
                with open(tmp_file, "wb") as f:
                    pickle.dump(partial_data, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.rename(tmp_file, str(INDEX_FILE))
                print(f"  💾 Saved partial index: {len(all_meta)} records", flush=True)
            except Exception as e:
                print(f"  ⚠️ Partial save failed: {e}", flush=True)
            print(f"  [{done}/{len(records)}] Batch {batch_idx+1}/{total_batches} "
                  f"| {rate:.1f}/s ETA={eta/60:.0f}min", flush=True)
    
    print(f"\n[3/3] Saving index...", flush=True)
    all_embeddings = np.vstack(all_embeddings)
    
    # Normalize embeddings for cosine similarity
    norms = np.linalg.norm(all_embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    all_embeddings = all_embeddings / norms
    
    index_data = {
        "embeddings": all_embeddings,
        "meta": all_meta,
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
    print(f"  Saved {len(all_meta)} embeddings ({size_mb:.0f}MB) in {elapsed/60:.1f}min", flush=True)
    print(f"  Index file: {INDEX_FILE}", flush=True)
    print("\n✅ Vector index build complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(build_index())
