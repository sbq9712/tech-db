#!/usr/bin/env python3
"""Deduplicate conferences using LLM semantic matching.

Groups conferences by date proximity, then uses LLM to determine which
conferences are actually the same event (just different names from
different sources). Merges them, combining all source links.

Usage:
  python3 scripts/dedup_conferences.py
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_client import call_glm_json

REPO = Path(__file__).resolve().parent.parent
INPUT_PATH = REPO / "data" / "processed" / "conferences.json"
OUTPUT_PATH = REPO / "data" / "processed" / "conferences.json"

MERGE_PROMPT = """你是会议信息去重专家。下面是同一时间段内的多个会议条目，请判断哪些实际上是同一个会议（只是不同消息源对它们的名称/描述不同）。

判断依据：
1. 核心主题相同（如都是"世界人工智能大会"）
2. 时间相近（同一时间段）
3. 地点一致或都未提及
4. 名称是同一个会议的不同表述（全称/简称/别名/带年份/带地点前缀等）

注意区分：
- 同一系列的不同届会议是不同会议
- 同一时间地点但主题完全不同的会议不是同一个
- 同一会议的不同环节/分论坛不算独立会议

请输出JSON，将实际上是同一个会议的条目分组：
{"groups": [[0,2,5], [1], [3,4]]}
其中数字是输入条目的索引（从0开始），同一组内的索引代表同一个会议。

待分析的会议列表（每个条目含name名称、start开始日期、end结束日期、location地点、organizer主办、sources来源标题）：
"""


def group_by_date_proximity(conferences: list[dict], days_window: int = 3) -> list[list[int]]:
    """Group conference indices by date proximity for LLM analysis."""
    # Parse dates
    parsed = []
    for i, c in enumerate(conferences):
        start = c.get("start_date", "")
        end = c.get("end_date") or start
        try:
            sd = datetime.strptime(start, "%Y-%m-%d")
            ed = datetime.strptime(end, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        parsed.append((i, sd, ed))

    # Group by overlap/proximity
    groups = []
    used = set()
    for i, (idx_a, sd_a, ed_a) in enumerate(parsed):
        if idx_a in used:
            continue
        group = [idx_a]
        used.add(idx_a)
        for j, (idx_b, sd_b, ed_b) in enumerate(parsed):
            if idx_b in used or idx_b == idx_a:
                continue
            # Check if dates are within window
            # If date ranges overlap or are within days_window of each other
            if sd_a - timedelta(days=days_window) <= ed_b and sd_b - timedelta(days=days_window) <= ed_a:
                group.append(idx_b)
                used.add(idx_b)
        groups.append(group)
    return groups


def llm_merge_analysis(group_indices: list[int], conferences: list[dict]) -> list[list[int]]:
    """Use LLM to determine which conferences in a group are actually the same."""
    if len(group_indices) <= 1:
        return [group_indices]

    # Build items for LLM
    items = []
    for idx in group_indices:
        c = conferences[idx]
        sources_titles = [s.get("title", "")[:60] for s in c.get("sources", [])][:3]
        items.append({
            "idx": len(items),
            "name": c.get("name", ""),
            "start": c.get("start_date", ""),
            "end": c.get("end_date", ""),
            "location": c.get("location", ""),
            "organizer": c.get("organizer", ""),
            "source_titles": sources_titles,
        })

    prompt = MERGE_PROMPT + json.dumps(items, ensure_ascii=False)
    result = call_glm_json(prompt, max_tokens=4096, temperature=0.1)
    if not result or "groups" not in result:
        # Fallback: treat each as separate
        return [[idx] for idx in group_indices]

    # Map local indices back to conference indices
    merged_groups = []
    for local_group in result["groups"]:
        conf_group = [group_indices[li] for li in local_group if 0 <= li < len(group_indices)]
        if conf_group:
            merged_groups.append(conf_group)
    return merged_groups


def merge_conferences(conf_list: list[dict]) -> dict:
    """Merge multiple conference dicts into one."""
    # Pick the best name (longest meaningful name, prefer full Chinese name)
    names = [c.get("name", "") for c in conf_list]
    # Prefer names that contain key info: prefer the one with more content but not excessively long
    # Heuristic: prefer names that include the year, and are between 8-40 chars
    scored_names = []
    for name in names:
        score = 0
        if "2026" in name or "2025" in name:
            score += 2
        if 8 <= len(name) <= 40:
            score += 3
        elif len(name) > 40:
            score += 1
        # Prefer names with specific event type words
        for kw in ["大会", "论坛", "峰会", "研讨会", "年会", "博览会", "展览"]:
            if kw in name:
                score += 1
                break
        scored_names.append((score, name))
    best_name = max(scored_names, key=lambda x: x[0])[1] if scored_names else names[0]

    # Pick earliest start_date and latest end_date
    start_dates = [c.get("start_date", "") for c in conf_list if c.get("start_date")]
    end_dates = [c.get("end_date") for c in conf_list if c.get("end_date")]

    merged = {
        "name": best_name,
        "start_date": min(start_dates) if start_dates else "",
        "end_date": max(end_dates) if end_dates else None,
        "location": next((c.get("location") for c in conf_list if c.get("location")), None),
        "organizer": next((c.get("organizer") for c in conf_list if c.get("organizer")), None),
        "category": conf_list[0].get("category", ""),
        "sources": [],
    }

    # Merge all sources, dedup by URL
    seen_urls = set()
    for c in conf_list:
        for s in c.get("sources", []):
            url = s.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged["sources"].append(s)
            elif not url:
                merged["sources"].append(s)

    return merged


def main():
    print("Loading conferences...", flush=True)
    conferences = json.loads(INPUT_PATH.read_text("utf-8"))
    print(f"  {len(conferences)} conferences loaded", flush=True)

    # Step 1: Group by date proximity
    print("Grouping by date proximity...", flush=True)
    date_groups = group_by_date_proximity(conferences, days_window=3)
    multi_groups = [g for g in date_groups if len(g) > 1]
    print(f"  {len(date_groups)} date groups, {len(multi_groups)} with potential duplicates", flush=True)

    # Step 2: For each multi-group, use LLM to find true duplicates
    print("Analyzing duplicates with LLM...", flush=True)
    all_merge_groups = []
    for i, group in enumerate(date_groups):
        if len(group) <= 1:
            all_merge_groups.append(group)
            continue

        print(f"  Group {i+1}/{len(multi_groups)}: {len(group)} conferences on ~{conferences[group[0]].get('start_date','')}", flush=True)
        sub_groups = llm_merge_analysis(group, conferences)
        for sg in sub_groups:
            all_merge_groups.append(sg)
            if len(sg) > 1:
                names = [conferences[idx]["name"][:40] for idx in sg]
                print(f"    MERGE: {' | '.join(names)}", flush=True)

    # Step 3: Merge conferences in each group
    print("\nMerging conferences...", flush=True)
    merged_conferences = []
    total_merged = 0
    for group in all_merge_groups:
        if len(group) == 1:
            merged_conferences.append(conferences[group[0]])
        else:
            conf_list = [conferences[idx] for idx in group]
            merged = merge_conferences(conf_list)
            merged_conferences.append(merged)
            total_merged += len(conf_list) - 1
            print(f"  → {merged['name']} ({len(conf_list)}→1, {len(merged['sources'])} sources)", flush=True)

    # Sort by start_date
    merged_conferences.sort(key=lambda c: c.get("start_date", ""))

    print(f"\nResult: {len(conferences)} → {len(merged_conferences)} conferences ({total_merged} duplicates removed)", flush=True)
    OUTPUT_PATH.write_text(json.dumps(merged_conferences, ensure_ascii=False, indent=2), "utf-8")
    print(f"Saved to {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
