"""
Vector index builder — v2 (canonical set + refined embedding text).

Builds a numpy cosine-similarity index from the canonical record set:
  valid category AND dp != 1 (non-duplicate).

Embedding text = source-grounded category/title/tags/type/key params/body.
Generated summaries are excluded from primary factual retrieval.
"""
import json, sys, time, asyncio, pickle, os, hashlib
import numpy as np
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import embedding_func, EMBEDDING_DIM, WORKING_DIR
from primary_evidence import source_evidence_text
from index_build_view import MigrationError, ensure_build_view
import index_build_view as _ibv

LITE = Path(os.environ.get("TECH_DB_LITE_DATASET",
                           str(_ibv.DEFAULT_DATASET)))
INDEX_DIR = WORKING_DIR
INDEX_FILE = INDEX_DIR / "vector_index_v2.pkl"

# Embedding batch size. bge-m3 accepts up to 8192 tokens and
# sentence-transformers pads every mini-batch to its longest member, so a
# batch of 64 long evidence texts materializes a ~64x8192x1024 fp32 hidden
# state (~2.1GB) on top of the ~2.3GB model — the observed OOM kill of the
# production rebuild on a memory-constrained host (8GB RAM with the server
# co-resident). Records are processed length-sorted, so the tail batches are
# precisely the longest texts. Per-row embeddings are independent and
# normalized per text, so batch size does not change any vector value; only
# the transient memory/speed tradeoff moves. 16 keeps the worst-case
# activation ~5x smaller with negligible CPU overhead. Env-overridable for
# constrained hosts.
BATCH_SIZE = int(os.environ.get("TECH_DB_VECTOR_BATCH_SIZE", "16"))

# Categories excluded from the canonical set
IRRELEVANT_CATS = {"不相关", "未分类", "手动导入", ""}


def format_record_text(rec: dict) -> str:
    """Format a record into concise, high-signal text for dense embedding.

    Composition: [leaf_category] title [tags] [type] key_params evidence_text.
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

    evidence_text = source_evidence_text(rec)
    if evidence_text:
        parts.append(str(evidence_text))

    return " ".join(parts)


def _text_hash(text: str) -> str:
    """Short hash of embedding text for staleness detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


async def build_index():
    print(f"[1/3] Loading records from {LITE.name}...", flush=True)
    # Stable-ID migration adapter (Phase-02 review, legacy_hybrid
    # compatibility): the legacy dataset may carry no inline record_id —
    # resolve the stable-ID-decorated BUILD VIEW through the validated
    # RecordIdMap path instead of hard-failing / never a legacy-idx pseudo-ID.
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
    print(f"  Total records in file: {len(data)}", flush=True)

    # Build canonical set: valid category AND dp != 1 (non-duplicate)
    records = []
    for i, rec in enumerate(data):
        cat = rec.get("c", "")
        dp = rec.get("dp", 0)
        if cat in IRRELEVANT_CATS or dp == 1:
            continue
        # legacy dataset idx (injected by the migration build view when the
        # dataset has no inline idx field) — durable meta identity anchor
        records.append((int(rec.get("idx", i)), rec))

    print(f"  Canonical set (valid & non-dup): {len(records)}", flush=True)

    # ── Incremental mode: load existing index, detect new/changed/stale ──
    existing_embeddings = None
    existing_meta = []

    # Compute text hashes for current canonical set
    canonical_hashes = {}
    for i, rec in records:
        text = format_record_text(rec)
        canonical_hashes[i] = _text_hash(text)

    if INDEX_FILE.exists():
        print("  Index already exists. Checking...", flush=True)
        try:
            with open(INDEX_FILE, "rb") as f:
                saved = pickle.load(f)

            # Build lookup: idx → (position_in_array, stored_hash)
            saved_by_idx = {}
            for pos, m in enumerate(saved["meta"]):
                saved_by_idx[m["idx"]] = (pos, m.get("_th", ""))

            canonical_ids = set(canonical_hashes.keys())
            saved_ids = set(saved_by_idx.keys())

            # Three sets:
            # 1. new: in canonical but not in saved → need embedding
            # 2. changed: in both but hash differs → need re-embedding
            # 3. stale: in saved but not in canonical → drop (dp=1, reclassified, etc.)
            new_ids = canonical_ids - saved_ids
            stale_ids = saved_ids - canonical_ids
            changed_ids = set()
            for idx in canonical_ids & saved_ids:
                _, stored_hash = saved_by_idx[idx]
                # Skip change detection for records without stored hash (pre-migration index)
                if stored_hash and stored_hash != canonical_hashes[idx]:
                    changed_ids.add(idx)

            need_embed_ids = new_ids | changed_ids
            keep_ids = canonical_ids - need_embed_ids  # unchanged, keep as-is

            # Build "existing" arrays from kept records only (drop stale + changed)
            if keep_ids:
                keep_positions = sorted(saved_by_idx[i][0] for i in keep_ids)
                existing_embeddings = saved["embeddings"][keep_positions]
                existing_meta = [saved["meta"][p] for p in keep_positions]

            # Stable-ID migration of a pre-migration index (Phase-02
            # review): kept entries may carry an empty record_id because the
            # index was built before the RecordIdMap existed. Their durable
            # metadata must be REBOUND to the build view's stable IDs — the
            # stored idx still identifies the canonical record; text-hash
            # change detection above guards against misaligned positions
            # (a shifted/reordered dataset flags the moved records as
            # changed and re-embeds them rather than mislabeling).
            view_rid_by_idx = {i: str(rec.get("record_id") or "")
                               for i, rec in records}
            needs_id_migration = any(
                not str(m.get("record_id") or "").strip()
                for m in (existing_meta or saved["meta"]))
            if existing_meta and needs_id_migration:
                rebound = sum(1 for m in existing_meta
                              if not str(m.get("record_id") or "").strip())
                for m in existing_meta:
                    rid = view_rid_by_idx.get(m["idx"], "")
                    if rid:
                        m["record_id"] = rid
                print(f"  Stable-ID migration: rebound {rebound} kept "
                      "entries to build-view record_ids.", flush=True)

            if not need_embed_ids:
                # Nothing to embed — either fully up-to-date, or only stale
                # to prune / metadata to migrate
                needs_th_migration = any("_th" not in m for m in (existing_meta or saved["meta"]))
                if (not stale_ids and not needs_th_migration
                        and not needs_id_migration):
                    print("  Index is complete and up-to-date!", flush=True)
                    return

                # Write pruned index (stale removed, and/or _th hashes added)
                if existing_embeddings is not None:
                    print(f"  Writing pruned index ({len(existing_meta)} records, "
                          f"dropped {len(stale_ids)} stale)...", flush=True)
                    # Ensure all kept records have _th
                    for m in existing_meta:
                        idx = m["idx"]
                        if idx in canonical_hashes and "_th" not in m:
                            m["_th"] = canonical_hashes[idx]
                    norms = np.linalg.norm(existing_embeddings, axis=1, keepdims=True)
                    norms[norms == 0] = 1
                    existing_embeddings = existing_embeddings / norms
                    index_data = {
                        "embeddings": existing_embeddings,
                        "meta": existing_meta,
                        "dim": EMBEDDING_DIM,
                    }
                    tmp_file = str(INDEX_FILE) + ".tmp"
                    with open(tmp_file, "wb") as f:
                        pickle.dump(index_data, f, protocol=pickle.HIGHEST_PROTOCOL)
                    os.rename(tmp_file, str(INDEX_FILE))
                    print(f"  Saved {len(existing_meta)} records.", flush=True)
                return

            # Separate keep vs need-embed from the canonical records list
            records_to_embed = [(i, r) for i, r in records if i in need_embed_ids]
            # Sort by text length so batches pad less (short texts batch together,
            # long texts batch together). Pure CPU-time optimization; embeddings
            # are identical because meta carries the original record index.
            records_to_embed.sort(key=lambda p: len(format_record_text(p[1])))

            parts = []
            if new_ids: parts.append(f"{len(new_ids)} new")
            if changed_ids: parts.append(f"{len(changed_ids)} changed")
            if stale_ids: parts.append(f"{len(stale_ids)} stale (dropped)")
            print(f"  Incremental: {', '.join(parts)}. "
                  f"Keeping {len(keep_ids)}, embedding {len(records_to_embed)}.", flush=True)

            records = records_to_embed  # only embed new + changed

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
                    "record_id": rec.get("record_id", ""),
                    "id": rec.get("id", ""),
                    "t": rec.get("t", ""),
                    "d": rec.get("d", ""),
                    "s": rec.get("s", ""),
                    "c": rec.get("c", ""),
                    "tg": rec.get("tg", []),
                    "sc": rec.get("sc", 0),
                    "u": rec.get("u", ""),
                    "_th": canonical_hashes[orig_idx],
                })
        except Exception as e:
            print(f"  ERROR batch {batch_idx}: {e}", flush=True)
            # Add zero embeddings for failed batch
            all_embeddings.append(np.zeros((len(batch), EMBEDDING_DIM), dtype=np.float32))
            for orig_idx, rec in batch:
                all_meta.append({"idx": orig_idx, "record_id": rec.get("record_id", ""), "t": rec.get("t", ""), "c": rec.get("c", ""),
                                 "_th": canonical_hashes[orig_idx]})

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
