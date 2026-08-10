#!/usr/bin/env python3
"""Backfill key parameters (kp) and AI summaries (as) for existing records.

Targets:
1. Curated records with body but no kp → extract kp
2. Records with AI summary but no body → extract kp from summary
3. Records with no body and no summary → generate short summary from title, then extract kp

Usage:
  python3 scripts/backfill_kp_summaries.py
"""
import json, sys, os, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from auto_pipeline import VALID_CATEGORY_LEAVES, log
from llm_client import call_glm_batch as _call_glm

LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"

KP_PROMPT = """作为顶级情报分析专家，请基于深层语义理解提取输入文本中的关键技术情报。

【禁止项】
严禁降级为关键词或向量匹配。所有提取必须基于对全文技术内涵的理解。

【提取规则】
1. 有明确量化参数的，格式为：参数名[核心条件]: 参数值
   例：能量密度[软包电池]: 350 Wh/kg
   转换效率[钙钛矿叠层]: 30.4%
2. 有明确属性但不可量化的，格式为：参数名[核心条件]: 定性特征
   例：催化剂[碱性海水]: 镍铁层状双氢氧化物
3. 无明确参数名但有关键技术状态/工艺特点/结论的，格式为：[核心条件]: 关键特征陈述
4. 如无任何关键技术参数可提取，返回空数组

只输出JSON数组：
[{"id":0,"key_params":["参数名[条件]: 值","..."]}]
待处理情报：
"""

SUMMARY_TITLE_ONLY = """这些情报没有正文，只有标题。请根据标题生成简短中文摘要（20-50字）。
根据标题推断研究主题和技术方向，简要描述该情报可能涉及的内容。
不要编造具体数据。只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""

def main():
    data = json.loads(LITE_PATH.read_text("utf-8"))
    log(f"总记录: {len(data)}")

    # ── Phase 1: Generate summaries for records with no body and no summary ──
    no_summary_ids = [i for i, r in enumerate(data)
                      if r.get("c", "") in VALID_CATEGORY_LEAVES
                      and not r.get("b", "").strip()
                      and not r.get("as", "").strip()]
    log(f"\n=== Phase 1: 无正文无摘要记录 → 生成标题摘要 ({len(no_summary_ids)} 条) ===")

    BATCH = 20
    all_items = [{"id": i, "title": data[idx]["t"][:200]}
                 for i, idx in enumerate(no_summary_ids)]
    results = _call_glm(SUMMARY_TITLE_ONLY, all_items, batch_size=10,
                        max_workers=5, progress_every=50)
    for r in results:
        local_id = r.get("id")
        if local_id is not None and local_id < len(no_summary_ids):
            summary = r.get("summary", "").strip()
            if summary:
                data[no_summary_ids[local_id]]["as"] = summary

    has_as = sum(1 for r in data if r.get("as", "").strip())
    log(f"  摘要覆盖: {has_as}/{len(data)} ({has_as/len(data)*100:.1f}%)")

    # Save intermediate
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    log("  中间结果已保存")

    # ── Phase 2: Extract key parameters for all eligible records missing kp ──
    # Now eligible = valid category AND (body>=10 OR summary>=10)
    kp_todo = [i for i, r in enumerate(data)
               if r.get("c", "") in VALID_CATEGORY_LEAVES
               and (len(r.get("b", "").strip()) >= 10 or len(r.get("as", "").strip()) >= 10)
               and not r.get("kp")]

    log(f"\n=== Phase 2: 关键参数提取 ({len(kp_todo)} 条) ===")

    all_kp_items = [{"id": local_id,
                     "title": data[idx]["t"][:200],
                     "body": (data[idx].get("b", "") or data[idx].get("as", ""))[:500],
                     "category": data[idx].get("c", "")}
                    for local_id, idx in enumerate(kp_todo)]

    results = _call_glm(KP_PROMPT, all_kp_items, batch_size=10,
                        max_workers=5, progress_every=50)
    total_extracted = 0
    for r in results:
        local_id = r.get("id")
        if local_id is not None and local_id < len(kp_todo):
            params = r.get("key_params", [])
            if params:
                data[kp_todo[local_id]]["kp"] = params
                total_extracted += 1

    # Final save
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    has_kp = sum(1 for r in data if r.get("kp"))
    has_as = sum(1 for r in data if r.get("as", "").strip())
    log(f"\n=== 最终结果 ===")
    log(f"总记录: {len(data)}")
    log(f"AI摘要: {has_as}/{len(data)} ({has_as/len(data)*100:.1f}%)")
    log(f"关键参数: {has_kp}/{len(data)} ({has_kp/len(data)*100:.1f}%)")

    # Per-category breakdown
    curated = [r for r in data if r.get("source") == "excel-import" or r.get("lv") == 3]
    curated_kp = sum(1 for r in curated if r.get("kp"))
    log(f"精选记录kp: {curated_kp}/{len(curated)}")

if __name__ == "__main__":
    main()
