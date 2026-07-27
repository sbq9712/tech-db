#!/usr/bin/env python3
"""Atomically publish lite JSON, shards and manifest from one record snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from data_contract import CHUNK_SIZE, DATA_DIR, LITE_PATH, MANIFEST_PATH, enforce_terminal_categories, load_manifest


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _meta_only(record: dict) -> dict:
    """Ultra-minimal record for instant first paint.
    Excludes body, ai_summary, score_dims, other_topics.
    These are loaded in subsequent tiers."""
    EXCLUDE = {"b", "as", "scd", "ot"}
    result = {k: v for k, v in record.items() if k not in EXCLUDE}
    result["hb"] = 1 if record.get("b") else 0
    return result


def _summary_only(record: dict, idx: int) -> dict:
    """AI summary + score dimensions for tier 2 background load."""
    return {
        "i": idx,
        "as": record.get("as", ""),
        "scd": record.get("scd"),
        "kp": record.get("kp", []),
    }


def build_snapshot(records: list[dict]) -> int:
    """Write all generated artifacts to a staging dir, then replace live files."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    enforce_terminal_categories(records)
    shard_count = (len(records) + CHUNK_SIZE - 1) // CHUNK_SIZE
    stage = Path(tempfile.mkdtemp(prefix="techdb-build-", dir=str(DATA_DIR.parent)))
    backups = Path(tempfile.mkdtemp(prefix="techdb-backup-", dir=str(DATA_DIR.parent)))
    live_files = [LITE_PATH, MANIFEST_PATH, *DATA_DIR.glob("lite-part-*.js"), *DATA_DIR.glob("meta-part-*.js"), *DATA_DIR.glob("summary-part-*.js")]
    for live in live_files:
        if live.exists():
            shutil.copy2(live, backups / live.name)
    try:
        staged_lite = stage / LITE_PATH.name
        _write_json(staged_lite, records)
        data_version = hashlib.sha256(staged_lite.read_bytes()).hexdigest()[:12]

        for i in range(shard_count):
            chunk = records[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
            base_idx = i * CHUNK_SIZE
            # Full shard (with body text) — for on-demand [展开全文]
            payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
            (stage / f"lite-part-{i}.js").write_text(
                "window.__LITE_PARTS__=window.__LITE_PARTS__||[];"
                f"window.__LITE_PARTS__.push({payload});",
                encoding="utf-8",
            )
            # Meta-only shard (ultra-minimal, no body/summary/dims)
            meta_chunk = [_meta_only(r) for r in chunk]
            meta_payload = json.dumps(meta_chunk, ensure_ascii=False, separators=(",", ":"))
            (stage / f"meta-part-{i}.js").write_text(
                "window.__META_PARTS__=window.__META_PARTS__||[];"
                f"window.__META_PARTS__.push({meta_payload});",
                encoding="utf-8",
            )
            # Summary shard (ai_summary + score_dims + key_params)
            summary_chunk = [_summary_only(r, base_idx + j) for j, r in enumerate(chunk)]
            summary_payload = json.dumps(summary_chunk, ensure_ascii=False, separators=(",", ":"))
            (stage / f"summary-part-{i}.js").write_text(
                "window.__SUMMARY_PARTS__=window.__SUMMARY_PARTS__||[];"
                f"window.__SUMMARY_PARTS__.push({summary_payload});",
                encoding="utf-8",
            )

        manifest = load_manifest()
        manifest.setdefault("meta", {})["records_total"] = len(records)
        manifest["meta"]["total_shards"] = shard_count
        manifest["meta"]["data_version"] = data_version
        (stage / MANIFEST_PATH.name).write_text(
            "window.__MANIFEST__=" + json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + ";",
            encoding="utf-8",
        )

        # Publish complete replacements only. Old extra shards are removed last.
        os.replace(staged_lite, LITE_PATH)
        for i in range(shard_count):
            os.replace(stage / f"lite-part-{i}.js", DATA_DIR / f"lite-part-{i}.js")
            os.replace(stage / f"meta-part-{i}.js", DATA_DIR / f"meta-part-{i}.js")
            os.replace(stage / f"summary-part-{i}.js", DATA_DIR / f"summary-part-{i}.js")
        os.replace(stage / MANIFEST_PATH.name, MANIFEST_PATH)
        for pattern in ["lite-part-*.js", "meta-part-*.js", "summary-part-*.js"]:
            for old in DATA_DIR.glob(pattern):
                try:
                    idx = int(old.stem.rsplit("-", 1)[1])
                except (IndexError, ValueError):
                    continue
                if idx >= shard_count:
                    old.unlink()
        return shard_count
    except Exception:
        # Restore the complete previous generation on any publication failure.
        for live in [LITE_PATH, MANIFEST_PATH, *DATA_DIR.glob("lite-part-*.js"), *DATA_DIR.glob("meta-part-*.js"), *DATA_DIR.glob("summary-part-*.js")]:
            if live.exists():
                live.unlink()
        for backup in backups.iterdir():
            target = LITE_PATH if backup.name == LITE_PATH.name else MANIFEST_PATH if backup.name == MANIFEST_PATH.name else DATA_DIR / backup.name
            shutil.copy2(backup, target)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backups, ignore_errors=True)
