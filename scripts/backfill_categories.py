#!/usr/bin/env python3
"""Backfill 未分类 (uncategorized) records — user-directed repair 2026-08-16.

Root cause: auto-sync classification batches can fail under API rate-limit
pressure (shared ZAI quota); failed batches left records with c="未分类"
even though they carry scores/summaries (AI精选 included). The canonical
index builders EXCLUDE 未分类, so these records were invisible to search.

This script:
  1. collects lite records with c == "未分类"
  2. re-classifies them via the SAME CLASSIFY_PROMPT / whitelist as
     auto_pipeline.classify_and_score (tag + topic included)
  3. preserves all existing enrichment (aip/sc/scd/as/kp/...)
  4. republishes the snapshot atomically (build_snapshot)

Scoring is NOT redone — scores already exist for 精选 records and
non-精选 records don't need scores for indexing (indexing needs category
+ dp only).

Usage:
  python scripts/backfill_categories.py [--max N] [--dry-run]
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
TAXONOMY_PATH = REPO / "data" / "category-taxonomy.json"

VALID_NEWS_TAGS = {"技术突破", "产业进展", "政策监管", "资本运作", "行业观察"}
VALID_LIT_TAGS = {"研究论文", "观点评论"}

CLASSIFY_PROMPT = """你是技术情报语义分类与标签标注专家。对以下每条情报同时完成分类和打标签。

分类必须严格从下方叶子白名单中选择一个完整路径，或选择"不相关"。禁止输出中间节点，禁止创造新分类，禁止改写路径。
重要：谨慎使用"不相关"标签。以下情况绝对不能标为"不相关"：
- 能源政策、政府规划、行业标准（如能源局、工信部等政策文件）
- 天气预测、气象技术相关
- 技术约束分析（如关键金属供应链约束影响技术发展）
- 技术发展评论、趋势分析、行业观点
- 技术伦理、安全事件、监管讨论
- 技术与社会经济交叉议题（如电动化潜力、脱碳路径）
- 商业新闻中的技术创新要素（如公司估值反映技术竞争格局）
只要情报与技术、能源、材料、AI、零碳产业有任何关联，就应归入对应分类，而不是"不相关"。

白名单叶子分类：
{leaves}

只输出JSON数组：[{{"id":0,"category":"白名单中的完整叶子路径或不相关","tag":"标签","topic":"5字主题"}}]

情报列表：
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="limit records (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--max-workers", type=int, default=5)
    args = ap.parse_args()

    from llm_client import call_glm_batch
    from build_snapshot import build_snapshot

    with TAXONOMY_PATH.open(encoding="utf-8") as f:
        valid_leaves = set(json.load(f)["categories"])
    valid_all = valid_leaves | {"不相关"}

    lite = json.loads(LITE_PATH.read_text(encoding="utf-8"))
    targets = [i for i, r in enumerate(lite) if r.get("c") == "未分类"]
    if args.max:
        targets = targets[:args.max]
    print(f"未分类 records: {len(targets)}")
    if not targets or args.dry_run:
        return

    items = [{"id": k,
              "type": "literature" if lite[i].get("i") == "l" else "news",
              "title": lite[i].get("t", "")[:200],
              "body": (lite[i].get("b") or "")[:500]}
             for k, i in enumerate(targets)]

    cp_file = REPO / ".backfill_checkpoint.json"
    done = {}
    if cp_file.exists():
        done = json.loads(cp_file.read_text(encoding="utf-8"))

    def _cp(results_so_far):
        m = {r.get("id"): r for r in results_so_far if isinstance(r, dict)}
        done.update({k: v for k, v in m.items() if v.get("id") is not None})
        cp_file.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")

    pending = [it for it in items if str(it["id"]) not in done and it["id"] not in done]
    print(f"pending after checkpoint: {len(pending)}")
    results = []
    if pending:
        results = call_glm_batch(
            CLASSIFY_PROMPT.format(leaves="\n".join(sorted(valid_leaves))),
            pending, batch_size=args.batch_size,
            max_workers=args.max_workers, checkpoint_fn=_cp,
        )
        _cp(results)

    idmap = {}
    for k, v in done.items():
        try:
            idmap[int(k)] = v
        except (TypeError, ValueError):
            continue
    for r in results or []:
        if isinstance(r, dict) and r.get("id") is not None:
            idmap[int(r["id"])] = r

    fixed = skipped = 0
    for k, i in enumerate(targets):
        r = idmap.get(k)
        if not r:
            skipped += 1
            continue
        cat = str(r.get("category", "")).strip()
        cat = re.sub(r"\s*/\s*", "/", cat)
        if cat not in valid_all:
            skipped += 1
            continue
        rec = lite[i]
        rec["c"] = cat
        is_lit = rec.get("i") == "l"
        tag = str(r.get("tag", "")).strip()
        valid_tags = VALID_LIT_TAGS if is_lit else VALID_NEWS_TAGS
        rec["tg"] = tag if tag in valid_tags else ("研究论文" if is_lit else "行业观察")
        if cat != "不相关":
            rec["tp"] = str(r.get("topic", ""))[:10]
        else:
            for f in ("aip", "sc", "scd", "as", "kp", "tp", "cl", "cp", "cln"):
                rec.pop(f, None)
        fixed += 1

    print(f"classified: {fixed} | left 未分类 (API failed/invalid): {skipped}")
    still = sum(1 for r in lite if r.get("c") == "未分类")
    print(f"remaining 未分类 in lite: {still}")
    if fixed == 0:
        return

    n = build_snapshot(lite)
    print(f"snapshot rebuilt: {n} shards")
    cp_file.unlink(missing_ok=True)
    print("done")


if __name__ == "__main__":
    main()
