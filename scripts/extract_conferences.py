#!/usr/bin/env python3
"""Extract conference/event information from intelligence records.

Scans all records for conference announcements and extracts:
- Conference name
- Start/end dates
- Location
- Organizer
- Source record(s)

Outputs to data/processed/conferences.json

Usage:
  python3 scripts/extract_conferences.py
"""
from __future__ import annotations
import json, os, sys, re
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import call_glm_batch

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
OUTPUT_PATH = REPO / "data" / "processed" / "conferences.json"

CONFERENCE_KEYWORDS = [
    "会议", "大会", "论坛", "峰会", "研讨会", "年会", "博览会",
    "展览会", "学术", "summit", "conference", "symposium",
    "workshop", "expo", "exhibition", "通知", "征稿", "call for",
    "将于", "即将召开", "即将举行", "会议通知"
]

EXTRACT_PROMPT = """你是一个会议信息提取专家。从以下情报中提取会议/活动信息。

规则：
1. 只提取明确的会议/活动预告（有会议名称和日期信息）
2. 如果情报不是关于会议预告的，返回 {"id": N, "is_conference": false}
3. 如果情报日期与会议日期差距超过1年（前后），返回 {"id": N, "is_conference": false}
4. 会议日期必须明确（至少有开始日期）

对于有效会议，返回：
{"id": N, "is_conference": true, "name": "会议名称", "start_date": "YYYY-MM-DD", 
 "end_date": "YYYY-MM-DD或null", "location": "地点或null", "organizer": "主办单位或null"}

注意：
- 日期格式必须是 YYYY-MM-DD
- 如果只有月份没有具体日期，日期部分用01填充
- 会议名称用全称，不要缩写
- 如果情报中说的是已结束的会议（如"XX会议回顾"），不算会议预告

待分析的情报：
"""


def find_conference_candidates(data: list[dict]) -> list[tuple[int, dict]]:
    """Pre-filter records that might contain conference info."""
    candidates = []
    for i, r in enumerate(data):
        text = (r.get("t", "") + " " + r.get("as", "")).lower()
        if any(kw.lower() in text for kw in CONFERENCE_KEYWORDS):
            if r.get("c", "") != "不相关" and r.get("c", "") != "未分类":
                candidates.append((i, r))
    return candidates


def extract_batch(items: list[dict]) -> list[dict]:
    """Send batch to LLM for conference extraction."""
    llm_items = []
    for item in items:
        gi, r = item
        record_date = r.get("d", "")
        title = r.get("t", "")[:200]
        summary = r.get("as", "")[:300]
        body = r.get("b", "")[:500]
        llm_items.append({
            "id": len(llm_items),
            "date": record_date,
            "title": title,
            "summary": summary,
            "body": body,
        })

    prompt = EXTRACT_PROMPT + json.dumps(llm_items, ensure_ascii=False)
    results = call_glm_batch(prompt, llm_items, batch_size=20)
    return results


def main():
    print("Loading data...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  {len(data)} total records", flush=True)

    candidates = find_conference_candidates(data)
    print(f"  {len(candidates)} conference candidates (pre-filter)", flush=True)

    if not candidates:
        print("No candidates found.", flush=True)
        OUTPUT_PATH.write_text("[]", "utf-8")
        return

    # Process in batches
    all_conferences = []
    batch_size = 20
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        print(f"  Processing batch {start//batch_size + 1}/{(len(candidates)-1)//batch_size + 1}...", flush=True)
        results = extract_batch(batch)

        for r in results:
            local_id = r.get("id")
            if local_id is None or local_id < 0 or local_id >= len(batch):
                continue
            if not r.get("is_conference"):
                continue

            gi, record = batch[local_id]
            conf = {
                "name": r.get("name", "").strip(),
                "start_date": r.get("start_date", ""),
                "end_date": r.get("end_date"),
                "location": r.get("location"),
                "organizer": r.get("organizer"),
                "category": record.get("c", "").split("/")[0] if record.get("c") else "",
                "sources": [{
                    "title": record.get("t", ""),
                    "url": record.get("u", ""),
                    "date": record.get("d", ""),
                }],
            }

            # Validate dates
            try:
                record_date = datetime.strptime(record.get("d", "2026-01-01"), "%Y-%m-%d")
                conf_start = datetime.strptime(conf["start_date"], "%Y-%m-%d")
                # Skip if conference is more than 1 year after or 6 months before record date
                diff = (conf_start - record_date).days
                if diff > 365 or diff < -180:
                    continue
            except (ValueError, TypeError):
                continue

            if conf["name"] and conf["start_date"]:
                all_conferences.append(conf)

        print(f"    Found {len([1 for r in results if r.get('is_conference')])} conferences in batch", flush=True)

    # Deduplicate by conference name (case-insensitive)
    seen_names = {}
    deduped = []
    for conf in all_conferences:
        name_key = conf["name"].lower().strip()
        if name_key in seen_names:
            # Merge sources
            existing = seen_names[name_key]
            existing["sources"].extend(conf["sources"])
            # Keep earliest start date
            if conf["start_date"] < existing["start_date"]:
                existing["start_date"] = conf["start_date"]
        else:
            seen_names[name_key] = conf
            deduped.append(conf)

    # Sort by start_date
    deduped.sort(key=lambda c: c.get("start_date", ""))

    print(f"\nTotal unique conferences: {len(deduped)}", flush=True)
    OUTPUT_PATH.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), "utf-8")
    print(f"Saved to {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
