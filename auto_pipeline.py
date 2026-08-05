#!/usr/bin/env python3
"""
tech-db 全自动Pipeline：检测新数据 → 去重 → 分类 → 评分 → AI摘要 → 聚类合并 → 重建lite → push GitHub
设计为cron每4小时运行。只有检测到新数据时才执行后续步骤（省token）。

设计原则（第一性原理）：
  1. state 是"已成功推送到 GitHub 的文件"记录，不是"已看到的文件"
  2. 任何中间步骤失败 → state 不更新 → 下次 cron 自动重试
  3. push 失败 = 整个 pipeline 失败，state 保留
  4. 下载失败的文件不进入 state
  5. 无日期文件（如 articles.csv）每次都重新处理（因为内容会变）
  6. 分片数量由数据量动态决定，前端也动态检测
  7. 单实例锁防止并发
"""
import json, os, sys, subprocess, time, hashlib, glob, re, fcntl
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from build_snapshot import build_snapshot
from llm_client import call_glm_batch

REPO = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO, "data", "processed")
LITE_PATH = os.path.join(DATA_DIR, "all-records-lite.json")
# state 放在持久化目录，不放 /tmp（tmpfs 重启即丢）
STATE_FILE = os.path.join(REPO, ".pipeline_state.json")
LOCK_FILE = os.path.join(REPO, ".pipeline.lock")
TOKEN = os.environ.get("GH_TOKEN", "")
# Self-heal: cron jobs don't inherit the user shell env.
if not TOKEN:
    for _env in (os.path.join(os.path.expanduser("~"), ".gh_env"), os.path.join(REPO, ".gh_env")):
        try:
            with open(_env) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line.startswith("export GH_TOKEN=") or _line.startswith("GH_TOKEN="):
                        TOKEN = _line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if TOKEN:
                break
        except (FileNotFoundError, PermissionError):
            continue
GH_PUSH_URL = f"https://sbq9712:{TOKEN}@github.com/sbq9712/tech-db.git" if TOKEN else None

# 无日期前缀的文件，内容会滚动更新，每次都必须重新处理
ROLLING_FILES = {"articles.csv"}
CHUNK_SIZE = 2000

# 合法标签集合（GLM 返回的其他标签一律纠正为默认值）
VALID_NEWS_TAGS = {"技术突破", "产业进展", "政策监管", "资本运作", "行业观察"}
VALID_LIT_TAGS = {"研究论文", "观点评论"}

# 分类树唯一权威来源：77 个固定叶子。没有用户明确命令不得增删。
TAXONOMY_PATH = os.path.join(REPO, "data", "category-taxonomy.json")
with open(TAXONOMY_PATH, encoding="utf-8") as _f:
    VALID_CATEGORY_LEAVES = set(json.load(_f)["categories"])
VALID_CLASSIFICATIONS = VALID_CATEGORY_LEAVES | {"不相关", "未分类"}

# Source repos
SOURCES = {
    "news": {"repo": "sbq9712/news-spider", "path": "data", "prefix": "articles-"},
    "literature": {"repo": "sbq9712/literature-rss-spider", "path": "output", "prefix": "news_with_abstract_"},
    "wechat": {"repo": "wodewoping-png/wechat-daily-news-csv", "path": "csv", "prefix": ""},
}

def log(msg):
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── 单实例锁 ──
def acquire_lock():
    """防止两个 cron 同时运行。"""
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (BlockingIOError, OSError):
        log("[SKIP] Another pipeline instance is running. Exiting.")
        os.close(lock_fd)
        sys.exit(0)

# ── GitHub API（正确认证）──
def gh_api(url):
    """GitHub API call with proper authentication."""
    try:
        cmd = ["curl", "-4", "-sL", f"https://api.github.com/repos/{url}"]
        if TOKEN:
            cmd += ["-H", f"Authorization: Bearer {TOKEN}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
        return None

def get_source_files():
    """Get latest file lists from all 3 source repos"""
    new_files = {}
    for name, info in SOURCES.items():
        url = f"{info['repo']}/contents/{info['path']}"
        data = gh_api(url)
        if not isinstance(data, list):
            log(f"  [WARN] {name}: API failed")
            continue
        csvs = sorted([d["name"] for d in data if d["name"].endswith(".csv")])
        new_files[name] = csvs
    return new_files

# ── State 管理 ──
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"known_files": {}, "file_hashes": {}}

def save_state(state):
    """Atomic write: write to temp then rename."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False)
    os.rename(tmp, STATE_FILE)

def file_content_hash(path):
    """SHA256 of file content — used to detect rolling file changes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def check_new_files():
    """检测新文件或内容变化的滚动文件。
    不修改 state。返回 (has_new, new_per_source, current, n_failed)。"""
    state = load_state()
    current = get_source_files()

    # CI mode: only process files from the last N days to avoid backlog
    max_days = os.environ.get("PIPELINE_MAX_DAYS")
    if max_days:
        max_days = int(max_days)
        cutoff = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=max_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        log(f"  CI mode: only processing files dated >= {cutoff_str}")

    n_failed = len(SOURCES) - len(current)
    has_new = False
    new_per_source = {}
    for name, files in current.items():
        known = set(state["known_files"].get(name, []))
        current_set = set(files)
        new = current_set - known
        # CI filter: only keep recent files
        if max_days:
            new = {f for f in new if _extract_date_from_filename(f, name) >= cutoff_str}
        # 滚动文件：即使文件名已知，如果内容 hash 变了也要重新处理
        for fname in list(files):
            if fname in ROLLING_FILES and fname in known:
                new.add(fname)
        if new:
            has_new = True
            new_per_source[name] = sorted(new)
            log(f"  {name}: {len(new)} new/changed files: {', '.join(sorted(new)[:5])}")
        else:
            log(f"  {name}: up to date ({len(files)} files)")

    return has_new, new_per_source, current, n_failed


def _extract_date_from_filename(fname, source_name):
    """Extract a YYYY-MM-DD date from a filename, return '' if not parseable."""
    import re
    # articles-2026-07-21.csv → 2026-07-21
    # news_with_abstract_2026-07-21.csv → 2026-07-21
    # 2026-06-15.csv → 2026-06-15
    m = re.search(r'(\d{4}-\d{2}-\d{2})', fname)
    if m:
        return m.group(1)
    # Also try YYYYMMDD format
    m = re.search(r'(\d{4})(\d{2})(\d{2})', fname)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""

def commit_state(current, processed_files):
    """只把成功处理并推送的文件标记为已处理。
    processed_files: {source_name: [(fname, content_hash), ...]}"""
    state = load_state()
    # Ensure keys exist (handle legacy state files)
    if "known_files" not in state:
        state["known_files"] = {}
    if "file_hashes" not in state:
        state["file_hashes"] = {}
    for name in current:
        existing = set(state["known_files"].get(name, []))
        for fname, _hash in processed_files.get(name, []):
            existing.add(fname)
            state["file_hashes"][f"{name}/{fname}"] = _hash
        state["known_files"][name] = sorted(existing)
    save_state(state)
    log(f"  State committed ({sum(len(v) for v in processed_files.values())} files marked)")

# ── CSV 下载与解析 ──
def download_new_csvs(new_per_source):
    """下载 CSV 文件。返回 (records, successfully_downloaded, failed_files)。
    successfully_downloaded: {source: [(fname, hash), ...]}
    failed_files: [(source, fname), ...]
    """
    import csv as csv_mod

    new_records = []
    successfully_downloaded = {}
    failed_files = []

    for name, new_files in new_per_source.items():
        info = SOURCES[name]
        successfully_downloaded[name] = []

        for fname in new_files:
            url = f"https://raw.githubusercontent.com/{info['repo']}/main/{info['path']}/{fname}"
            local_path = f"/tmp/techdb_{name}_{fname}"
            # Never accept a stale file left by an earlier failed run.
            try:
                os.remove(local_path)
            except FileNotFoundError:
                pass
            ok = False
            for _attempt in range(3):
                try:
                    subprocess.run(
                        ["curl", "-4", "-sSL", "--retry", "3", "--retry-delay", "2",
                         "--connect-timeout", "15", "--max-time", "90",
                         url, "-o", local_path],
                        capture_output=True, timeout=120
                    )
                except subprocess.TimeoutExpired:
                    log(f"  [WARN] timeout dl {fname} (attempt {_attempt+1}/3)")
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    ok = True
                    break
                # raw.githubusercontent.com may be unreachable; fall back to API raw endpoint
                api_url = f"https://api.github.com/repos/{info['repo']}/contents/{info['path']}/{fname}"
                try:
                    r = subprocess.run(
                        ["curl", "-4", "-sSL", "-H", "Accept: application/vnd.github.v3.raw",
                         "--connect-timeout", "15", "--max-time", "90",
                         api_url, "-o", local_path],
                        capture_output=True, timeout=120
                    )
                    if r.returncode == 0 and os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                        log(f"  {fname}: raw endpoint failed, got via API fallback")
                        ok = True
                        break
                except subprocess.TimeoutExpired:
                    log(f"  [WARN] timeout API dl {fname} (attempt {_attempt+1}/3)")
                time.sleep(2)
            if not ok:
                log(f"  [ERROR] Failed to download {fname} — will NOT mark as processed")
                failed_files.append((name, fname))
                continue

            # 验证内容是否真的变化（针对滚动文件）
            chash = file_content_hash(local_path)
            state = load_state()
            old_hash = state.get("file_hashes", {}).get(
                f"{name}/{fname}",
                state.get("file_hashes", {}).get(fname),  # legacy state compatibility
            )
            if fname in ROLLING_FILES and old_hash == chash:
                log(f"  {fname}: content unchanged, skipping")
                successfully_downloaded[name].append((fname, chash))
                continue

            # Parse CSV
            try:
                with open(local_path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv_mod.DictReader(f)
                    for row in reader:
                        title = row.get("title", row.get("Title", ""))
                        # Body extraction: cover all 3 source formats
                        # news-spider: content | literature: abstract | wechat: clean_text
                        body = row.get("clean_text",
                                row.get("abstract",
                                row.get("content",
                                row.get("body",
                                row.get("Body",
                                row.get("content_preview",
                                row.get("digest",
                                row.get("summary", ""))))))))
                        # Date extraction: prefer publish date from row, fallback to filename
                        pub_date = row.get("publish_time",
                                   row.get("published_at",
                                   row.get("pub_date",
                                   row.get("published_str", ""))))
                        if pub_date:
                            m2 = re.search(r'(\d{4}-\d{2}-\d{2})', pub_date)
                            date = m2.group(1) if m2 else ""
                        else:
                            date = ""
                        if not date:
                            m = re.search(r'(\d{4}-\d{2}-\d{2})', fname)
                            date = m.group(1) if m else ""
                        url_val = row.get("url", row.get("link", row.get("URL", "")))
                        source_name = row.get("source", row.get("account_name",
                                   row.get("source_name",
                                   row.get("authors",
                                   row.get("author", row.get("Authors", ""))))))
                        if not title:
                            continue
                        new_records.append({
                            "t": title[:500],
                            "b": body if body else "",
                            "d": date,
                            "u": url_val,
                            "c": "未分类",
                            "a": source_name,
                            "i": "l" if "literature" in name else "n",
                            "source": fname,
                        })
                successfully_downloaded[name].append((fname, chash))
            except Exception as e:
                log(f"  [ERROR] Parse error {fname}: {e} — will NOT mark as processed")
                failed_files.append((name, fname))

    return new_records, successfully_downloaded, failed_files

# ── 去重 ──
def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    m = re.search(r'(10\.\d{4,}/[^\s?#]+)', url)
    if m:
        return f"DOI:{m.group(1).lower().rstrip('.')}"
    m = re.search(r'nature\.com/articles/(s\d+-\d+-\d+[-\w]*)', url)
    if m:
        return f"DOI:10.1038/{m.group(1).lower()}"
    parsed = urlparse(url.lower().rstrip('/'))
    return f"URL:{parsed.netloc}{parsed.path}"

def dedup_check(new_records, existing_lite):
    existing_titles = set()
    existing_body_prefix = set()
    existing_urls = set()
    for r in existing_lite:
        t = r.get("t", "").strip().lower()[:80]
        b = r.get("b", "")[:80]
        u = normalize_url(r.get("u", ""))
        if t: existing_titles.add(t)
        if b: existing_body_prefix.add(b)
        if u: existing_urls.add(u)

    unique = []
    dupes = 0
    # Add accepted records to these sets so duplicates inside the same new batch are removed too.
    for r in new_records:
        t = r.get("t", "").strip().lower()[:80]
        b = r.get("b", "")[:80]
        u = normalize_url(r.get("u", ""))
        if t in existing_titles or (b and b in existing_body_prefix) or (u and u in existing_urls):
            dupes += 1
        else:
            unique.append(r)
            if t: existing_titles.add(t)
            if b: existing_body_prefix.add(b)
            if u: existing_urls.add(u)
    return unique, dupes

# ── GLM 调用 ──
# call_glm_batch is imported from llm_client (no longer uses hermes CLI)

# ── 标题翻译 ──
def translate_non_chinese_titles(records):
    """Translate titles that contain no Chinese characters to zh-CN using Google Translate."""
    has_cn = lambda s: bool(re.search(r'[\u4e00-\u9fff]', s or ''))
    targets = [(i, r['t']) for i, r in enumerate(records) if not has_cn(r.get('t', ''))]
    if not targets:
        log(f"  标题翻译: 0 条需要翻译")
        return records

    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        log(f"  [WARN] deep-translator 未安装，跳过标题翻译")
        return records

    translator = GoogleTranslator(source='auto', target='zh-CN')
    done = 0; failed = 0
    for idx, title in targets:
        try:
            translated = translator.translate(title[:5000])
            if translated and has_cn(translated):
                records[idx]['t'] = translated
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    log(f"  标题翻译: {done}/{len(targets)} 成功 (失败 {failed})")
    return records


# ── 分类 + 评分 ──
def classify_and_score(records):
    items = [{"id": i, "type": "literature" if r.get("i")=="l" else "news",
              "title": r["t"][:200], "body": r.get("b","")[:500]} for i, r in enumerate(records)]

    CLASSIFY_PROMPT = """你是技术情报语义分类与标签标注专家。对以下每条情报同时完成分类和打标签。
分类必须严格从下方叶子白名单中选择一个完整路径，或选择“不相关”。禁止输出中间节点，禁止创造新分类，禁止改写路径。

重要：谨慎使用"不相关"标签。以下情况绝对不能标为"不相关"：
- 能源政策、政府规划、行业标准（如能源局、工信部等政策文件）
- 天气预测、气象技术相关
- 技术约束分析（如关键金属供应链约束影响技术发展）
- 技术发展评论、趋势分析、行业观点
- 技术伦理、安全事件、监管讨论
- 技术与社会经济交叉议题（如电动化潜力、脱碳路径）
- 商业新闻中的技术创新要素（如公司估值反映技术竞争格局）
只要情报与技术、能源、材料、AI、零碳产业有任何关联，就应归入对应分类，而不是"不相关"。
"不相关"仅用于：纯娱乐八卦、体育赛事、生活方式、无技术要素的纯政治新闻。

合法叶子白名单：
""" + "\n".join(sorted(VALID_CATEGORY_LEAVES)) + """
只输出JSON数组：[{"id":0,"category":"白名单中的完整叶子路径或不相关","tag":"标签","topic":"5字主题"}]
标签规则：新闻→技术突破/产业进展/政策监管/资本运作/行业观察；文献→研究论文/观点评论
待处理情报：
"""
    SCORE_PROMPT = """对以下每条情报打5个维度分数（0-10分）。

评分维度说明：
1. breakthrough(突破性): 纯政策/市场=0-2；渐进改进=3-5；显著技术进步=6-8；新机理/新材料/颠覆性=9-10
   - 注意：综合性前沿技术发布（如科协年度前沿问题、国家科技规划）应在6-8分
   - 跨领域整合性研究（如储能+电网规划、氢能+CCUS）应在5-7分
   - 技术经济性分析/可行性研究应在4-6分
2. industry(产业力): 实验室概念=1-2；小规模验证=3-5；中试/示范=6-7；量产落地/广泛应用=8-10
   - 会议/论坛预告不算产业进展，industry应偏低(2-4)
3. rarity(稀缺性): 转载旧闻=0-2；常规跟踪=3-5；深度分析=6-7；独家首发/罕见数据=8-10
   - 全面综述/系统性分析应在5-7分（信息整合本身有价值）
4. data(数据密度): 纯定性=0-2；定性+少量参数=3-5；有具体技术参数=6-8；多维度硬数据=9-10
   - 即使无正文，标题中包含技术方向和应用场景的也应给3-4分
5. timeliness(时效性): 趋势综述/历史回顾=2-3；近期进展=4-6；当周突发=7-8；最新独家=9-10
   - 重要会议/政策发布应在6-8分

只输出JSON数组：[{"id":0,"b":7.5,"i":6.0,"r":5.0,"d":8.0,"t":7.0}]
待评估情报（跳过不相关）：
"""

    log("  分类中...")
    classify_results = call_glm_batch(CLASSIFY_PROMPT, items, batch_size=10)

    for r in classify_results:
        idx = r.get("id")
        if not isinstance(idx, int) or idx < 0 or idx >= len(records):
            continue
        # Valid id: normalize category separator, then enforce immutable leaf whitelist.
        cat = r.get("category", "未分类").strip()
        for sep in ['>', '—', '→', '·']:
            cat = cat.replace(sep, '/')
        cat = re.sub(r'\s*/\s*', '/', cat)
        if cat not in VALID_CLASSIFICATIONS:
            # Never persist invented/intermediate categories. Leave for retry/repair.
            log(f"  [WARN] Invalid category rejected: {cat}")
            cat = "未分类"
        records[idx]["c"] = cat
        if cat == "不相关":
            for field in ("aip", "sc", "scd", "as", "kp", "tp", "cl", "cp", "cln"):
                records[idx].pop(field, None)
        # Validate tag: must be in the allowed set for this record type
        tag = r.get("tag", "").strip()
        is_lit = records[idx].get("i") == "l"
        valid_tags = VALID_LIT_TAGS if is_lit else VALID_NEWS_TAGS
        if tag not in valid_tags:
            tag = "研究论文" if is_lit else "行业观察"
        records[idx]["tg"] = tag
        if cat != "不相关":
            records[idx]["tp"] = r.get("topic", "")

    # GLM batch classification may omit some ids; ensure every record has c and tg.
    for idx in range(len(records)):
        is_lit = records[idx].get("i") == "l"
        default_tag = "研究论文" if is_lit else "行业观察"
        if "c" not in records[idx] or records[idx].get("c") == "":
            records[idx]["c"] = "未分类"
        if "tg" not in records[idx] or records[idx].get("tg") == "":
            records[idx]["tg"] = default_tag

    classified = sum(1 for r in records if r.get("c","") not in ("","未分类"))
    log(f"  分类完成: {classified}/{len(records)}")

    relevant_items = [{"id": i, "title": r["t"][:200], "body": r.get("b","")[:500], "category": r.get("c","")}
                      for i, r in enumerate(records) if r.get("c","") in VALID_CATEGORY_LEAVES]

    log(f"  评分中... ({len(relevant_items)} 条相关)")
    score_results = call_glm_batch(SCORE_PROMPT, relevant_items, batch_size=10)
    score_map = {r["id"]: r for r in score_results}

    THRESHOLDS = {"零碳产业": 6.3, "AI与智能科技": 6.5, "通用技术": 6.8}

    for idx, r in enumerate(records):
        if r.get("c","") not in VALID_CATEGORY_LEAVES: continue
        sc = score_map.get(idx)
        if not sc: continue

        b, i, rr, d, t = sc.get("b",0), sc.get("i",0), sc.get("r",0), sc.get("d",0), sc.get("t",0)
        tag = r.get("tg","")
        is_lit = r.get("i") == "l"

        # Tag-aware weight profiles
        # b=breakthrough, i=industry, r=rarity, d=data, t=timeliness
        if is_lit:
            # Literature: emphasis on breakthrough + data + rarity
            w = {"b": 0.28, "i": 0.15, "r": 0.20, "d": 0.22, "t": 0.15}
        elif tag in ("技术突破", "产业进展"):
            # High-impact news: boost breakthrough + industry + timeliness
            w = {"b": 0.25, "i": 0.25, "r": 0.15, "d": 0.10, "t": 0.25}
        elif tag == "政策监管":
            # Policy: relevance + timeliness matter most
            w = {"b": 0.15, "i": 0.20, "r": 0.25, "d": 0.15, "t": 0.25}
        else:
            # Default news (行业观察, 资本运作, etc.)
            w = {"b": 0.20, "i": 0.20, "r": 0.20, "d": 0.15, "t": 0.25}

        score = b*w["b"] + i*w["i"] + rr*w["r"] + d*w["d"] + t*w["t"]

        # Boosts for high individual dimension scores
        if t >= 8: score += 0.3
        elif t >= 7: score += 0.15
        if b >= 7: score += 0.4
        if rr >= 7: score += 0.3
        if i >= 7 and not is_lit: score += 0.3

        # Category-aware boost: cross-cutting and frontier topics
        cat_path = r.get("c", "")
        # Grid/storage intersection, hydrogen+CCUS, and similar cross-domain topics
        cross_domain = any(kw in cat_path for kw in ["电网技术", "配电", "储能", "氢能", "碳捕集"])
        if cross_domain and b >= 4:
            score += 0.3  # boost cross-domain integration research
        # Comprehensive frontier reports (科协, 国家级科技规划, etc.)
        if tag == "政策监管" and b >= 5:
            score += 0.3  # high-impact policy/frontier reports

        score = round(score, 1)

        r["sc"] = score
        r["scd"] = {"b": b, "i": i, "r": rr, "d": d, "t": t}

        domain = r["c"].split("/")[0]
        threshold = THRESHOLDS.get(domain, 6.8)
        aip = 1 if score >= threshold else 0
        # Removed: single-dimension high score → AI精选
        # Only total score above threshold qualifies for AI精选
        if aip:
            r["aip"] = 1

    scored = sum(1 for r in records if r.get("sc",0) > 0)
    aip_count = sum(1 for r in records if r.get("aip"))
    log(f"  评分完成: {scored} scored, {aip_count} AI精选")

    return records

# ── AI 摘要 ──
def gen_summaries(records):
    SUMMARY_PROMPT_FULL = """你是技术情报摘要专家。为以下每条情报生成100-200字的中文AI摘要。
重要规则：
1. 无论原文是什么语言，摘要必须全部用中文撰写
2. 严格基于提供的标题和正文生成摘要，绝对不要编造正文中没有的信息
3. 提炼核心技术要点、关键参数和结论
4. 不要使用"该研究/该技术可能..."等推测性语句，只总结已知事实
只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""

    SUMMARY_PROMPT_SHORT = """为以下每条情报生成简短中文摘要（30-80字）。
正文极短，请基于标题和有限正文生成简短摘要。
不要编造数据或结论。只总结已知信息。
只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""

    # Policy: NO summary for empty-body records.
    # Short summary (30-80 chars) for short-body records (<50 chars).
    # Full summary (100-200 chars) for records with body >=50 chars.
    eligible_full = [i for i, r in enumerate(records)
                     if r.get("c", "") in VALID_CATEGORY_LEAVES
                     and len(r.get("b", "").strip()) >= 50]
    eligible_short = [i for i, r in enumerate(records)
                      if r.get("c", "") in VALID_CATEGORY_LEAVES
                      and 0 < len(r.get("b", "").strip()) < 50]

    # Clear any existing summary for empty-body records
    cleared = 0
    for i, r in enumerate(records):
        if not r.get("b", "").strip() and r.get("as", "").strip():
            r["as"] = ""
            cleared += 1
    if cleared:
        log(f"  清空 {cleared} 条无正文记录的AI摘要")

    log(f"  摘要生成中... ({len(eligible_full)} 条正文 + {len(eligible_short)} 条短正文)")

    # Process full-body records
    for round_no in range(3):
        pending = [i for i in eligible_full if not records[i].get("as", "").strip()]
        if not pending:
            break
        if round_no > 0:
            log(f"  重试第 {round_no} 轮: {len(pending)} 条待生成")
        items = [{"id": i, "title": records[i]["t"][:200], "body": records[i].get("b","")[:800]}
                 for i in pending]
        results = call_glm_batch(SUMMARY_PROMPT_FULL, items, batch_size=20)
        for r in results:
            idx = r.get("id")
            if idx is not None and idx < len(records):
                summary = r.get("summary", "").strip()
                if summary:
                    records[idx]["as"] = summary

    # Process short-body records
    for round_no in range(3):
        pending = [i for i in eligible_short if not records[i].get("as", "").strip()]
        if not pending:
            break
        if round_no > 0:
            log(f"  短正文重试第 {round_no} 轮: {len(pending)} 条待生成")
        items = [{"id": i, "title": records[i]["t"][:200], "body": records[i].get("b","")[:200]}
                 for i in pending]
        results = call_glm_batch(SUMMARY_PROMPT_SHORT, items, batch_size=20)
        for r in results:
            idx = r.get("id")
            if idx is not None and idx < len(records):
                summary = r.get("summary", "").strip()
                if summary:
                    records[idx]["as"] = summary

    has_summary = sum(1 for r in records if r.get("as","").strip())
    has_body = sum(1 for r in records if r.get("b","").strip())
    log(f"  摘要完成: {has_summary}/{len(records)} (有正文记录: {has_body})")
    return records

# ── 合并 + 重建分片 ──
def merge_and_rebuild(new_records):
    """Merge new records into lite, rebuild chunks. Returns (count, start_index)."""
    with open(LITE_PATH) as f:
        lite = json.load(f)

    unique, dupes = dedup_check(new_records, lite)
    log(f"  去重: {len(new_records)} → {len(unique)} ({dupes} duplicates)")

    if not unique:
        log("  无新记录需要合并")
        return 0, len(lite)

    start_index = len(lite)
    for r in unique:
        lite.append(r)

    log(f"  合并后总记录: {len(lite)}")

    # Publish lite JSON, all contiguous shards and manifest from one snapshot.
    shard_count = build_snapshot(lite)
    log(f"  原子重建 {shard_count} 个分片")

    return len(unique), start_index


# ── 增量聚类 ──
def incremental_cluster(start_index, total_count):
    """Cluster only newly-merged records against the 20-day historical pool.

    Uses the same scheme-1 thresholds and LLM adjudication as the full run,
    but only processes records from start_index onward. Existing clusters
    are preserved; new records may join existing clusters or form new ones.
    """
    if start_index >= total_count:
        log("  无新记录需要聚类")
        return 0

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    import clustering

    data = json.load(open(LITE_PATH))
    # Compute pool once, then derive new_indices as a subset
    pool_indices = [i for i, r in enumerate(data)
                    if clustering.is_relevant(r) and r.get("dp") != 1]
    new_indices = [i for i in pool_indices if i >= start_index]

    if not new_indices:
        log("  新记录中无 eligible 条目，跳过聚类")
        return 0

    cache = clustering.load_cache()
    log(f"  增量聚类: {len(new_indices)} 新记录，embedding 缓存 {len(cache)}")

    # Ensure vectors for new records (historical ones are already cached)
    vectors = clustering.ensure_vectors(data, new_indices, cache, save_every=64)

    candidates = clustering.make_candidates(data, new_indices, pool_indices, vectors)
    log(f"  候选对: {len(candidates)}")

    if not candidates:
        log("  无候选对，跳过聚类")
        return 0

    decisions = clustering.adjudicate(data, candidates, "zai", "glm-5.2")
    accepted = [k for k, v in decisions.items() if v.get("accepted")]
    log(f"  裁决通过: {len(accepted)}/{len(decisions)}")

    if not accepted:
        log("  无裁决通过的对，跳过聚类写入")
        return 0

    judged_indices = sorted({idx for pair in decisions for idx in pair})
    groups = clustering.complete_link_groups(judged_indices, decisions)
    applied = clustering.apply_groups(data, groups, decisions) if groups else []
    log(f"  聚类应用: {len(applied)} 个")

    if applied:
        shard_count = build_snapshot(data)
        log(f"  重建分片: {shard_count}")

    return len(applied)

# ── 推送 GitHub ──
def git_push():
    """Commit generated data only, then push. Never stage unrelated source edits."""
    if not TOKEN:
        log("  [ERROR] No GH_TOKEN — cannot push")
        return False

    subprocess.run(["git", "update-index", "--refresh", "-q"],
                   capture_output=True, text=True, cwd=REPO, timeout=60)

    # Refuse to auto-commit when source edits are present. This prevents a cron run
    # from mixing an unfinished frontend/code change with generated data.
    # data/reports/ is excluded: it is generated by Step 11 (auto reports) or
    # GitHub Actions, and git_push() never stages or commits it,
    # so in-flight report files must not block a data-sync push.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal", "--", ":(exclude)data/processed/lite-part-*.js", ":(exclude)data/processed/meta-part-*.js", ":(exclude)data/processed/summary-part-*.js", ":(exclude)data/processed/manifest-data.js", ":(exclude)data/processed/conferences.json", ":(exclude)data/reports/"],
        capture_output=True, text=True, cwd=REPO, timeout=30,
    )
    if dirty.returncode != 0 or dirty.stdout.strip():
        log("  [ERROR] Source worktree is dirty; refusing automatic data commit")
        return False

    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    generated_paths = ["data/processed/manifest-data.js", "data/processed/lite-part-*.js", "data/processed/meta-part-*.js", "data/processed/summary-part-*.js"]
    result = subprocess.run(["git", "add", "-A", "--", *generated_paths], capture_output=True, text=True, cwd=REPO, timeout=30)
    if result.returncode != 0:
        log(f"  [ERROR] git add failed: {result.stderr[:200]}")
        return False

    # git commit only when generated data differs from HEAD.
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO, timeout=30)
    if staged.returncode == 1:
        result = subprocess.run(["git", "commit", "-m", f"chore: auto-sync {ts}"],
                               capture_output=True, text=True, cwd=REPO, timeout=30)
        if result.returncode != 0:
            log(f"  [ERROR] git commit failed: {result.stderr[:200]}")
            return False
    elif staged.returncode != 0:
        log("  [ERROR] git diff --cached failed")
        return False

    # Always push: a previous run may have committed locally but failed to push.
    if GH_PUSH_URL is None:
        log("  [ERROR] No push URL configured")
        return False
    result = subprocess.run(["git", "push", GH_PUSH_URL, "main"],
                           capture_output=True, text=True, cwd=REPO, timeout=120)
    if result.returncode != 0:
        log(f"  [ERROR] git push failed: {result.stderr[:200]}")
        return False

    log("  Pushed to GitHub")
    return True

# ── 主流程 ──
def main():
    lock_fd = acquire_lock()

    log("=" * 50)
    log("tech-db auto-sync pipeline starting")

    # Step 1: Check for new files (does NOT save state)
    log("Step 1: Checking source repos...")
    has_new, new_per_source, current, n_failed = check_new_files()

    if n_failed == len(SOURCES):
        log(f"[ERROR] All {n_failed} source API calls failed. Aborting (will retry next run).")
        return
    if n_failed:
        log(f"  [WARN] {n_failed}/{len(SOURCES)} source(s) API failed — proceeding with partial data.")
    if not has_new:
        log("No new files. Done.")
        return

    try:
        # Step 2: Download and parse new CSVs
        log("Step 2: Downloading new data...")
        new_records, successfully_downloaded, failed_files = download_new_csvs(new_per_source)
        log(f"  Parsed {len(new_records)} new records from CSVs")
        if failed_files:
            log(f"  [WARN] {len(failed_files)} files failed to download: {[f[1] for f in failed_files]}")

        # 即使没有新记录（全是重复或下载失败），也只 commit 成功下载的文件
        if not new_records:
            log("No new records to process. Ensuring pending commits are pushed before state commit.")
            if not git_push():
                log("[FATAL] Push failed — state NOT committed.")
                return
            commit_state(current, successfully_downloaded)
            return

        # Step 3: Dedup against existing
        log("Step 3: Dedup check...")
        with open(LITE_PATH) as f:
            existing = json.load(f)
        unique, dupes = dedup_check(new_records, existing)
        log(f"  {len(new_records)} → {len(unique)} unique ({dupes} dupes)")

        if not unique:
            log("All records are duplicates. Ensuring pending commits are pushed before state commit.")
            if not git_push():
                log("[FATAL] Push failed — state NOT committed.")
                return
            commit_state(current, successfully_downloaded)
            return

        # Step 4: Translate titles + Classify + Score
        log("Step 4: Translate titles + Classify + Score...")
        unique = translate_non_chinese_titles(unique)
        unique = classify_and_score(unique)

        # Step 5: AI Summaries
        log("Step 5: AI Summaries...")
        unique = gen_summaries(unique)

        # Step 6: Merge + Rebuild
        log("Step 6: Merge + Rebuild lite...")
        merged_count, start_index = merge_and_rebuild(unique)
        total_after_merge = start_index + merged_count

        # Step 6b: Incremental clustering (new records only)
        if merged_count > 0:
            log("Step 6b: Incremental clustering...")
            try:
                cluster_count = incremental_cluster(start_index, total_after_merge)
            except Exception as ce:
                log(f"  [WARN] 增量聚类失败，跳过: {ce}")
                cluster_count = 0

            # Step 6c: Extract conferences + dedup
            log("Step 6c: Extract conferences...")
            try:
                conf_result = subprocess.run(
                    [sys.executable, os.path.join(REPO, "scripts", "extract_conferences.py")],
                    capture_output=True, text=True, cwd=REPO, timeout=300
                )
                if conf_result.returncode == 0:
                    log("  " + conf_result.stdout.strip().split('\n')[-1])
                else:
                    log(f"  [WARN] 会议提取失败: {conf_result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                log("  [WARN] 会议提取超时，跳过")
            except Exception as ce:
                log(f"  [WARN] 会议提取异常: {ce}")

            log("Step 6c.2: Dedup conferences...")
            try:
                dedup_result = subprocess.run(
                    [sys.executable, os.path.join(REPO, "scripts", "dedup_conferences.py")],
                    capture_output=True, text=True, cwd=REPO, timeout=120
                )
                if dedup_result.returncode == 0:
                    log("  " + dedup_result.stdout.strip().split('\n')[-1])
                else:
                    log(f"  [WARN] 会议去重失败: {dedup_result.stderr[:200]}")
            except Exception as ce:
                log(f"  [WARN] 会议去重异常: {ce}")

        # Step 7: Validate all immutable data contracts, then push.
        log("Step 7: Validate data contracts...")
        validation = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "validate_data_contract.py")],
                                    capture_output=True, text=True, cwd=REPO, timeout=60)
        if validation.returncode != 0:
            raise RuntimeError(validation.stdout or validation.stderr or "data contract validation failed")
        log("  " + validation.stdout.strip())
        log("Step 8: Push to GitHub...")
        if not git_push():
            log("[FATAL] Push failed — state NOT committed. Files will be retried next run.")
            return  # 不 commit_state，下次 cron 自动重试

        # Step 9: Commit state ONLY after successful push
        log("Step 9: Commit state...")
        commit_state(current, successfully_downloaded)

        # Step 10: Rebuild search indexes (vector + BM25 + knowledge graph)
        log("Step 10: Rebuilding search indexes...")

        venv_python = os.path.join(REPO, ".venv", "bin", "python")
        qa_backend = os.path.join(REPO, "qa-backend")
        index_dir = os.path.join(REPO, "data", "lightrag")
        index_env = {**os.environ, "TECH_DB_INDEX_DIR": index_dir}

        # 10a: BM25 index (fast, ~5 min)
        log("  10a: Building BM25 index...")
        bm25_result = subprocess.run(
            [venv_python, os.path.join(qa_backend, "bm25_index.py")],
            capture_output=True, text=True, cwd=REPO, timeout=600, env=index_env
        )
        if bm25_result.returncode != 0:
            log(f"  [WARN] BM25 index build failed (non-fatal): {bm25_result.stderr[:200]}")
        else:
            log("  BM25 index built successfully.")

        # 10b: Vector index (incremental, only new records embedded)
        log("  10b: Building vector index...")
        vec_result = subprocess.run(
            [venv_python, os.path.join(qa_backend, "vector_index.py")],
            capture_output=False, cwd=REPO, timeout=14400, env=index_env
        )
        if vec_result.returncode != 0:
            log(f"  [WARN] Vector index build failed (non-fatal)")
        else:
            log("  Vector index built successfully.")

        # 10c: Knowledge graph (incremental, only new records)
        log("  10c: Updating knowledge graph (incremental)...")
        graph_result = subprocess.run(
            [venv_python, os.path.join(qa_backend, "concurrent_ingest.py"),
             "--concurrency", "5"],
            capture_output=False, cwd=REPO, timeout=7200, env=index_env
        )
        if graph_result.returncode != 0:
            log(f"  [WARN] Knowledge graph update failed (non-fatal)")
        else:
            log("  Knowledge graph updated successfully.")

        log("Step 10 complete: All indexes rebuilt.")

        # Step 11: Generate reports (daily always, weekly on Monday, monthly on 1st)
        log("Step 11: Generating reports...")
        try:
            cst_now = datetime.now(timezone(timedelta(hours=8)))
            yesterday_cst = (cst_now - timedelta(days=1)).strftime("%Y-%m-%d")
            weekday = cst_now.weekday()  # 0=Monday

            report_scripts = os.path.join(REPO, "scripts", "generate_reports.py")

            # 11a: Daily report (always)
            log(f"  11a: Daily report for {yesterday_cst}...")
            daily_result = subprocess.run(
                [venv_python, report_scripts, "--type", "daily", "--date", yesterday_cst],
                capture_output=True, text=True, cwd=REPO, timeout=120
            )
            if daily_result.returncode == 0:
                log("  " + daily_result.stdout.strip().split('\n')[-1])
            else:
                log(f"  [WARN] Daily report failed: {daily_result.stderr[:200]}")

            # 11b: Weekly report (Monday only)
            if weekday == 0:
                last_monday = (cst_now - timedelta(days=7)).strftime("%Y-%m-%d")
                log(f"  11b: Weekly report for week of {last_monday}...")
                weekly_result = subprocess.run(
                    [venv_python, report_scripts, "--type", "weekly", "--date", last_monday],
                    capture_output=True, text=True, cwd=REPO, timeout=180
                )
                if weekly_result.returncode == 0:
                    log("  " + weekly_result.stdout.strip().split('\n')[-1])
                else:
                    log(f"  [WARN] Weekly report failed: {weekly_result.stderr[:200]}")

            # 11c: Monthly report (1st of month only)
            if cst_now.day == 1:
                first_of_prev = (cst_now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
                log(f"  11c: Monthly report for {first_of_prev}...")
                monthly_result = subprocess.run(
                    [venv_python, report_scripts, "--type", "monthly", "--date", first_of_prev],
                    capture_output=True, text=True, cwd=REPO, timeout=180
                )
                if monthly_result.returncode == 0:
                    log("  " + monthly_result.stdout.strip().split('\n')[-1])
                else:
                    log(f"  [WARN] Monthly report failed: {monthly_result.stderr[:200]}")

            # 11d: Commit and push reports
            log("  11d: Committing reports...")
            subprocess.run(["git", "add", "data/reports/"], cwd=REPO, timeout=30, capture_output=True)
            staged_reports = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO, timeout=30)
            if staged_reports.returncode == 1:
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"report: auto {cst_now.strftime('%Y-%m-%d')}"],
                    cwd=REPO, timeout=30, capture_output=True, text=True)
                if commit_result.returncode == 0:
                    if GH_PUSH_URL:
                        push_result = subprocess.run(
                            ["git", "push", GH_PUSH_URL, "main"],
                            cwd=REPO, timeout=120, capture_output=True, text=True)
                        if push_result.returncode == 0:
                            log("  Reports committed and pushed.")
                        else:
                            log(f"  [WARN] Report push failed: {push_result.stderr[:200]}")
                    else:
                        log("  Reports committed locally (no GH_TOKEN for push).")
                else:
                    log(f"  [WARN] Report commit failed: {commit_result.stderr[:200]}")
            else:
                log("  No new reports to commit.")

            log("Step 11 complete.")
        except Exception as re:
            log(f"  [WARN] Report generation failed (non-fatal): {re}")

        log(f"Pipeline complete: +{merged_count} records")
    except Exception as e:
        log(f"[ERROR] Pipeline failed: {e}")
        log("State NOT committed — files will be retried next run.")
        import traceback
        traceback.print_exc()
        raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except:
            pass

if __name__ == "__main__":
    main()
