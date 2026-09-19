#!/usr/bin/env python3
"""Backfill: translate the stock of existing non-Chinese titles in the lite
dataset via GLM-5.3-flash (Google Translate path 429-dead since 2026-09).

Mutates data/processed/all-records-lite.json titles in place, republishes the
shard snapshot, commits locally and pushes a data-backup branch. main is
GH006-protected (PR-only), so the commit rides data-auto-sync/<ts> like the
pipeline's own fallback.

Usage: .venv/bin/python scripts/backfill_translate_titles.py [--dry-run]
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from llm_client import call_glm  # noqa: E402
from build_snapshot import build_snapshot  # noqa: E402

REPO = "."
LITE_PATH = "data/processed/all-records-lite.json"
BATCH = 25
MAX_WORKERS = 5

PROMPT = (
    "你是技术情报标题翻译引擎。将下列 JSON 数组中的每条外语标题翻译成简体中文。\n"
    "要求：\n"
    "1. 保留专业术语的准确性与行业通用译法\n"
    "2. 专有名词（公司名/产品名/模型名/人名）保留英文\n"
    "3. 只输出 JSON 数组，格式：[{\"id\":0,\"zh\":\"翻译\"},...]，"
    "id 与输入一致，不要输出其他任何内容\n\n输入："
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    dry = "--dry-run" in sys.argv
    with open(LITE_PATH, encoding="utf-8") as f:
        lite = json.load(f)
    has_cn = lambda s: bool(re.search(r"[一-鿿]", s or ""))
    targets = [(i, r["t"]) for i, r in enumerate(lite) if not has_cn(r.get("t", ""))]
    log(f"non-Chinese titles: {len(targets)} / {len(lite)}")
    if not targets or dry:
        log("nothing to do" if not targets else "dry-run: exiting")
        return

    batches = []
    for bstart in range(0, len(targets), BATCH):
        chunk = targets[bstart:bstart + BATCH]
        batches.append((bstart, [{"id": k, "title": t} for k, (_, t) in enumerate(chunk)]))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def do_batch(item):
        bstart, batch = item
        full = PROMPT + json.dumps(batch, ensure_ascii=False)
        for delay in (0, 5, 15, 45):
            if delay:
                import time
                time.sleep(delay)
            try:
                out = call_glm(full, timeout=120)
                s, e = out.find("["), out.rfind("]")
                if s >= 0 and e > s:
                    arr = json.loads(out[s:e + 1])
                    if isinstance(arr, list):
                        return bstart, batch, arr
            except Exception:
                pass
        return bstart, batch, None

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(do_batch, item) for item in batches]
        for n, f in enumerate(as_completed(futures), 1):
            bstart, batch, arr = f.result()
            if arr:
                for row in arr:
                    try:
                        rid = int(row.get("id"))
                        zh = (row.get("zh") or "").strip()
                    except Exception:
                        continue
                    if 0 <= rid < len(batch) and zh and has_cn(zh):
                        lite[targets[bstart + rid][0]]["t"] = zh
                        done += 1
            if n % 20 == 0:
                log(f"progress: {n}/{len(batches)} batches, {done} translated")

    log(f"translated {done}/{len(targets)}")
    if done == 0:
        log("no changes — skip snapshot/push")
        return

    shard_count = build_snapshot(lite)
    log(f"snapshot rebuilt ({shard_count} shards)")
    subprocess.run(["git", "add", "data/processed/"], cwd=REPO)
    c = subprocess.run(["git", "commit", "-m", "chore: backfill zh titles via GLM (2637 stock)"],
                       cwd=REPO, capture_output=True, text=True)
    log("commit: " + (c.stdout.strip()[:80] or c.stderr.strip()[:80]))
    import os
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        from pathlib import Path
        for cand in (Path.home() / ".gh_env", Path(".gh_env")):
            if cand.exists():
                for line in cand.read_text().split("\n"):
                    if line.startswith(("GH_TOKEN=", "export GH_TOKEN=")):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                if token:
                    break
    if token:
        ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        url = f"https://sbq9712:{token}@github.com/sbq9712/tech-db.git"
        p = subprocess.run(["git", "push", url, f"HEAD:refs/heads/data-auto-sync/{ts_tag}"],
                           cwd=REPO, capture_output=True, text=True, timeout=180)
        log("backup branch push: " + ("OK data-auto-sync/%s" % ts_tag if p.returncode == 0
                                      else p.stderr.strip()[:160]))
    log("done")


if __name__ == "__main__":
    main()
