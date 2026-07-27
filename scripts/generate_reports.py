#!/usr/bin/env python3
"""Generate daily/weekly/monthly reports from tech-db intelligence data.
Outputs structured JSON files for rich frontend rendering.
"""
import json, os, sys, argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_client import call_glm_json

REPO = Path(__file__).resolve().parents[1]
LITE = REPO / "data" / "processed" / "all-records-lite.json"
REPORTS_DIR = REPO / "data" / "reports"

FORCE_OVERWRITE = False


def log(msg): print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_sector(record):
    c = record.get("c", "")
    first = c.split("/")[0] if c else ""
    if first in ("零碳产业", "AI与智能科技", "通用技术"):
        return first
    return None


def cluster_member_count(records):
    return Counter(r.get("cl", "") for r in records if r.get("cl"))


def rank_score(r, cluster_counts):
    sc = float(r.get("sc", 0))
    lv = int(r.get("lv", 0))
    cl = r.get("cl", "")
    members = cluster_counts.get(cl, 0) if cl else 0
    return sc + members * 0.5 + lv * 1.0


def load_data():
    return json.loads(LITE.read_text("utf-8"))


def filter_records(data, date_from, date_to):
    candidates = [r for r in data if (r.get("aip") == 1 or r.get("lv", 0) >= 1)
                  and r.get("c", "") not in ("", "不相关", "未分类")]
    return [r for r in candidates if date_from <= r.get("d", "") <= date_to]


def top_by_sector(records, n):
    cluster_counts = cluster_member_count(records)
    sectors = {"零碳产业": [], "AI与智能科技": [], "通用技术": []}
    for r in records:
        s = get_sector(r)
        if s:
            sectors[s].append(r)
    top = {}
    for sector, recs in sectors.items():
        recs.sort(key=lambda r: rank_score(r, cluster_counts), reverse=True)
        top[sector] = recs[:n]
    return top


def entry_to_json(r):
    """Extract display fields from a lite record."""
    body = r.get("b", "")
    return {
        "t": r.get("t", ""),
        "d": r.get("d", ""),
        "a": r.get("a", ""),
        "c": r.get("c", ""),
        "as": r.get("as", ""),
        "sc": r.get("sc", 0),
        "i": r.get("i", ""),
        "tg": r.get("tg", ""),
        "u": r.get("u", ""),
        "tp": r.get("tp", ""),
        "aip": r.get("aip", 0),
        "kp": r.get("kp", []),
        "lv": r.get("lv", 0),
        "wr": r.get("wr", ""),
        "cm": r.get("cm", ""),
        "b": body,
        "hb": 1 if body else 0,
    }


def format_input(top_events):
    """Format events for the GLM prompt."""
    parts = []
    for sector, recs in top_events.items():
        parts.append(f"\n## {sector}")
        for r in recs:
            parts.append(f"标题：{r.get('t','')}")
            if r.get("as"): parts.append(f"摘要：{r['as']}")
            parts.append(f"分类：{r.get('c','')}")
            parts.append(f"评分：{r.get('sc',0)}")
            parts.append("")
    return "\n".join(parts)


# ── GLM Prompts ──
# Writing style guide: formal, professional news language.
# Avoid dramatic/sensational words: 击穿/狂砸/杀出/引爆/碾压 etc.

STYLE_RULES = """\
写作风格要求（严格遵守）：
1. 使用正式、专业的产业新闻语言，杜绝网络化或情绪化表达。
2. 严禁使用"击穿""碾压""狂砸""杀出""引爆""血洗""吊打"等口语化/夸张词汇。
3. 用词客观中性，如"成本竞争力超越"而非"击穿"，"首次实现"而非"杀出"。
4. 综述段落应具有宏观视野，聚焦技术进展、产业格局变化和商业化进程。
5. 每条速览标题为一句话概括，15-30字，体现核心事实，避免标题党。
6. 综述正文严禁使用空洞的套话和模板化表达，如"呈现……态势""迎来深刻变革""迈入新阶段""推动产业升级""赋能""深度赋能""助力""持续赋能"等。
7. 每一句话都必须包含具体的事实信息：谁做了什么、技术参数、量化数据、时间节点、参与者名称。
8. 如果素材不足以支撑完整的综述段落，宁可缩短字数也不要用空话凑字数。
9. 综述应当像路透社/彭博社的电讯稿一样，用最少的字传达最多的信息量。"""

DAILY_PROMPT = """你是资深产业情报编辑。基于今日的原始情报数据，为三大板块各生成速览标题。

{style}

# 输入数据：
{input_data}

# 输出要求：
只输出JSON，不要输出markdown标记或其他文字。格式如下：
{{
  "headlines": {{
    "零碳产业": ["标题1", "标题2", "标题3"],
    "AI与智能科技": ["标题1", "标题2", "标题3"],
    "通用技术": ["标题1", "标题2", "标题3"]
  }}
}}

规则：
- 每个板块最多3条速览标题（如板块无事件则空数组）
- 速览标题是对原始情报的新闻式重新概括，不直接照搬原标题
- 标题之间用换行分隔
"""

WEEKLY_PROMPT = """你是资深产业战略分析师，文风参照路透社、彭博社产业电讯稿。基于过去一周的原始情报数据，生成周报的核心叙事部分。

{style}

# 输入数据：
{input_data}

# 输出要求：
只输出JSON，不要输出markdown标记或其他文字。格式如下：
{{
  "main_theme": "提炼本周跨板块的核心事实主线。必须包含具体领域和具体事件，例如'固态电池完成首次装车验证，HBM产能扩张进入加速期'。严禁使用'呈现……态势''迎来……变革'等空泛表述。",
  "overview": "综述本周动态，约150-200字。每一句话必须包含具体事实（公司名、技术参数、量化数据）。直接陈述事实，不要铺垫和总结性套话。严禁出现'本周……呈现……''产业发展……加速'等模板句式。",
  "headlines": {{
    "零碳产业": ["标题1", "标题2", "标题3", "标题4"],
    "AI与智能科技": ["标题1", "标题2", "标题3", "标题4"],
    "通用技术": ["标题1", "标题2", "标题3", "标题4"]
  }},
  "sector_reviews": {{
    "零碳产业": "综述本板块本周关键进展，约60-100字。直接列出最重要的2-3个事实及其量化数据。",
    "AI与智能科技": "综述本板块本周关键进展，约60-100字。直接列出最重要的2-3个事实及其量化数据。",
    "通用技术": "综述本板块本周关键进展，约60-100字。直接列出最重要的2-3个事实及其量化数据。"
  }}
}}

规则：
- 速览标题是对原始情报的新闻式重新概括，不直接照搬原标题
- 所有综述文字必须像新闻电讯稿一样信息密集：每句话都有具体事实，没有空洞的形容词和套话
- 如果某板块本周事件较少，缩短字数即可，不要用空话凑数
"""

MONTHLY_PROMPT = """你是资深产业战略分析师，文风参照路透社、彭博社产业电讯稿。基于过去一个月的原始情报数据，生成月报的核心叙事部分。

{style}

# 输入数据：
{input_data}

# 输出要求：
只输出JSON，不要输出markdown标记或其他文字。格式如下：
{{
  "main_theme": "提炼本月跨板块的核心事实主线。必须包含具体领域和具体事件，例如'固态电池完成首次装车验证，HBM产能扩张进入加速期'。严禁使用'呈现……态势''迎来……变革'等空泛表述。",
  "overview": "综述本月动态，约200-250字。每一句话必须包含具体事实（公司名、技术参数、量化数据）。直接陈述事实，不要铺垫和总结性套话。严禁出现'本月……呈现……''产业发展……加速'等模板句式。",
  "headlines": {{
    "零碳产业": ["标题1", "标题2", "标题3", "标题4", "标题5"],
    "AI与智能科技": ["标题1", "标题2", "标题3", "标题4", "标题5"],
    "通用技术": ["标题1", "标题2", "标题3", "标题4", "标题5"]
  }},
  "sector_reviews": {{
    "零碳产业": "综述本板块本月关键进展，约80-120字。直接列出最重要的3-4个事实及其量化数据。",
    "AI与智能科技": "综述本板块本月关键进展，约80-120字。直接列出最重要的3-4个事实及其量化数据。",
    "通用技术": "综述本板块本月关键进展，约80-120字。直接列出最重要的3-4个事实及其量化数据。"
  }}
}}

规则：
- 速览标题是对原始情报的新闻式重新概括，不直接照搬原标题
- 所有综述文字必须像新闻电讯稿一样信息密集：每句话都有具体事实，没有空洞的形容词和套话
- 如果某板块本月事件较少，缩短字数即可，不要用空话凑数
"""


def generate_one(rtype, date_str, data):
    if rtype == "daily":
        date_from = date_to = date_str
        top_n = 3
        prompt_template = DAILY_PROMPT
        subdir = "daily"
        d = datetime.strptime(date_str, "%Y-%m-%d")
        weekday_cn = "星期" + "一二三四五六日"[d.weekday()]
        date_label = f"{d.year}年{d.month}月{d.day}日　{weekday_cn}"
        date_range = date_str
        fname = f"{date_str}.json"
    elif rtype == "weekly":
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_from = d.strftime("%Y-%m-%d")
        date_to = (d + timedelta(days=6)).strftime("%Y-%m-%d")
        top_n = 4
        prompt_template = WEEKLY_PROMPT
        date_label = f"{date_from} ~ {date_to}"
        date_range = f"{date_from} ~ {date_to}"
        iso_week = d.isocalendar()[1]
        subdir = "weekly"
        fname = f"{d.year}-W{iso_week:02d}.json"
    elif rtype == "monthly":
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_from = d.strftime("%Y-%m-01")
        last_day = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        date_to = last_day.strftime("%Y-%m-%d")
        top_n = 5
        prompt_template = MONTHLY_PROMPT
        date_label = f"{date_from} ~ {date_to}"
        date_range = f"{date_from} ~ {date_to}"
        subdir = "monthly"
        fname = f"{d.strftime('%Y-%m')}.json"
    else:
        return None

    out_dir = REPORTS_DIR / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname
    if out_path.exists() and out_path.stat().st_size > 100 and not FORCE_OVERWRITE:
        log(f"  {subdir}/{fname} already exists, skipping")
        return str(out_path)

    records = filter_records(data, date_from, date_to)
    if not records:
        log(f"  no records for {date_from}~{date_to}")
        return None

    top = top_by_sector(records, top_n)
    total_top = sum(len(v) for v in top.values())
    if total_top == 0:
        log(f"  no top events for {date_from}~{date_to}")
        return None

    input_data = format_input(top)
    full_prompt = prompt_template.format(style=STYLE_RULES, input_data=input_data)
    log(f"  generating {subdir}/{fname} ({total_top} events from {len(records)} records)")

    narrative = call_glm_json(full_prompt)
    if not narrative:
        log(f"  GLM returned empty, skipping")
        return None

    # Build sectors with entries
    sectors_out = []
    for sector_name in ["零碳产业", "AI与智能科技", "通用技术"]:
        recs = top.get(sector_name, [])
        sector_data = {
            "name": sector_name,
            "review": narrative.get("sector_reviews", {}).get(sector_name) if rtype != "daily" else None,
            "headlines": narrative.get("headlines", {}).get(sector_name, []),
            "entries": [entry_to_json(r) for r in recs],
        }
        sectors_out.append(sector_data)

    report = {
        "type": rtype,
        "date_label": date_label,
        "date_range": date_range,
        "main_theme": narrative.get("main_theme"),
        "overview": narrative.get("overview") if rtype != "daily" else None,
        "headlines": narrative.get("headlines", {}),
        "sectors": sectors_out,
    }

    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    log(f"  saved {out_path}")

    # Clean up old .md file if it exists
    md_path = out_path.with_suffix('.md')
    if md_path.exists():
        md_path.unlink()
        log(f"  removed old {md_path.name}")

    return str(out_path)


def generate_all(rtype, data):
    dates = sorted(set(r.get("d", "") for r in data if r.get("d")))
    if not dates:
        log("no dates in data")
        return
    earliest = datetime.strptime(dates[0], "%Y-%m-%d")
    yesterday = datetime.now() - timedelta(days=1)

    if rtype == "daily":
        current = earliest
        while current <= yesterday:
            generate_one("daily", current.strftime("%Y-%m-%d"), data)
            current += timedelta(days=1)
    elif rtype == "weekly":
        current = earliest - timedelta(days=earliest.weekday())
        while current <= yesterday:
            generate_one("weekly", current.strftime("%Y-%m-%d"), data)
            current += timedelta(days=7)
    elif rtype == "monthly":
        current = earliest.replace(day=1)
        while current <= yesterday:
            generate_one("monthly", current.strftime("%Y-%m-%d"), data)
            next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
            current = next_month


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["daily", "weekly", "monthly"], required=True)
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite existing reports")
    args = parser.parse_args()

    global FORCE_OVERWRITE
    FORCE_OVERWRITE = args.force

    data = load_data()
    log(f"loaded {len(data)} records")

    if args.all:
        generate_all(args.type, data)
    elif args.date:
        generate_one(args.type, args.date, data)
    else:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        generate_one(args.type, yesterday, data)

    log("DONE")


if __name__ == "__main__":
    main()
