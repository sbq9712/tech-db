#!/usr/bin/env python3
"""One-off migration: 77-leaf → 125-leaf taxonomy (2026-09 行业图景 2026-09-20 版).

Reclassifies every record whose category falls outside the new 125-leaf
whitelist, then re-derives pipeline AI fields for affected records:

  stage 1  classify  — GLM batch classification against the new whitelist
                       (verbatim prompt/validation logic from auto_pipeline)
  stage 2  score     — records whose category changed (or lacked sc), using the
                       pipeline's exact tag-aware weight profile + THRESHOLDS
  stage 3  summaries — newly-relevant records lacking `as` (reuses
                       auto_pipeline.gen_summaries on a reference subset)
  stage 4  key params— newly-relevant eligible records lacking `kp` (reuses
                       auto_pipeline.extract_key_params on a reference subset)
  stage 5  snapshot  — atomic republish of lite/shards/manifest via
                       build_snapshot (strips AI fields from 不相关 records)

Resumable: checkpoints in data/reclass-checkpoint/{classify,score}.json.
Records are never added/removed, so lite indices are stable across resumes.

Usage:
  python3 scripts/reclassify_taxonomy_v2.py [--limit N] [--no-snapshot]
                                             [--skip-classify] [--skip-score]
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

from data_contract import LITE_PATH, VALID_LIT_TAGS, VALID_NEWS_TAGS  # noqa: E402
from llm_client import call_glm_batch  # noqa: E402

# NOTE: importing auto_pipeline loads VALID_CATEGORY_LEAVES from the NEW
# data/category-taxonomy.json (125 leaves) and gives us gen_summaries /
# extract_key_params with pipeline-identical behavior.
import auto_pipeline as ap  # noqa: E402
from build_snapshot import build_snapshot  # noqa: E402
import validate_data_contract as vdc  # noqa: E402

CHECKPOINT_DIR = os.path.join(REPO, "data", "reclass-checkpoint")
PIPELINE_LOCK = os.path.join(REPO, ".pipeline.lock")

TAXONOMY_PATH = os.path.join(REPO, "data", "category-taxonomy.json")
with open(TAXONOMY_PATH, encoding="utf-8") as _f:
    NEW_LEAVES = json.load(_f)["categories"]
LEAVES_SET = set(NEW_LEAVES)
VALID_CLASSIFICATIONS_NEW = LEAVES_SET | {"不相关", "未分类"}

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
""" + "\n".join(sorted(NEW_LEAVES)) + """
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

THRESHOLDS = {"零碳产业": 6.3, "AI与智能科技": 6.5, "通用技术": 6.8}

_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_fingerprint(path: str):
    fp_file = path + ".fp"
    if os.path.exists(fp_file):
        with open(fp_file, encoding="utf-8") as f:
            return json.load(f)
    return None


class JsonCheckpoint:
    """Thread-safe {str(id): result} checkpoint persisted as one JSON file.

    Guarded by a run fingerprint (taxonomy sha + prompts sha): a checkpoint
    whose recorded fingerprint differs from the current run context is
    rejected outright instead of being replayed (Codex review fix #1).
    """

    def __init__(self, path: str, fingerprint: str):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            recorded = self.data.pop("__fingerprint__", None) if "__fingerprint__" in self.data else _read_fingerprint(path)
            if recorded is not None and recorded != fingerprint:
                raise SystemExit(
                    f"[FATAL] {os.path.basename(path)} fingerprint mismatch: "
                    f"recorded={recorded} current={fingerprint} — refusing to replay stale results"
                )
            log(f"  断点续跑: {os.path.basename(path)} 已有 {len(self.data)} 条结果")
        else:
            with open(path + ".fp", "w", encoding="utf-8") as f:
                json.dump(fingerprint, f)

    def merge_and_save(self, results_so_far: list) -> None:
        """callback signature compatible with llm_client.call_glm_batch(checkpoint_fn=...)"""
        with self.lock:
            for r in results_so_far:
                rid = r.get("id")
                if isinstance(rid, int):
                    self.data[str(rid)] = r
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def restored_results(self) -> list[dict]:
        out = []
        for k, v in self.data.items():
            r = dict(v)
            r["id"] = int(k)
            out.append(r)
        return out


_LEAF_BY_TAIL: dict[str, list[str]] = {}
for _leaf in NEW_LEAVES:
    _LEAF_BY_TAIL.setdefault(_leaf.rsplit("/", 1)[-1], []).append(_leaf)


def repair_category(cat: str) -> str:
    """Recover near-miss model output to an exact whitelist leaf.

    Models sometimes drop an intermediate level (e.g. .../传统蓄电池/锂电 for
    .../传统蓄电池/有机体系/锂电). If the final segment matches exactly one
    whitelist leaf's final segment, repair to it. Otherwise 未分类.
    Always returns something inside VALID_CLASSIFICATIONS_NEW.
    """
    tail = cat.rsplit("/", 1)[-1].strip()
    cands = _LEAF_BY_TAIL.get(tail, [])
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        # prefer the candidate sharing the longest path prefix with the model output
        def prefix_len(cand: str) -> int:
            a, b = cat.split("/"), cand.split("/")
            n = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                n += 1
            return n
        return max(cands, key=prefix_len)
    return "未分类"


def apply_classify(records: list[dict], results: list[dict]) -> None:
    """Per-record logic from auto_pipeline.classify_and_score + tail repair."""
    for r in results:
        idx = r.get("id")
        if not isinstance(idx, int) or idx < 0 or idx >= len(records):
            continue
        cat = r.get("category", "未分类").strip()
        for sep in ['>', '—', '→', '·']:
            cat = cat.replace(sep, '/')
        cat = re.sub(r'\s*/\s*', '/', cat)
        if cat not in VALID_CLASSIFICATIONS_NEW:
            fixed = repair_category(cat)
            if fixed != cat:
                log(f"  [REPAIR] {cat} → {fixed}")
            cat = fixed
        records[idx]["c"] = cat
        if cat == "不相关":
            for field in ("aip", "sc", "scd", "as", "kp", "tp", "cl", "cp", "cln"):
                records[idx].pop(field, None)
        tag = r.get("tag", "").strip()
        is_lit = records[idx].get("i") == "l"
        valid_tags = VALID_LIT_TAGS if is_lit else VALID_NEWS_TAGS
        if tag not in valid_tags:
            tag = "研究论文" if is_lit else "行业观察"
        records[idx]["tg"] = tag
        if cat != "不相关":
            records[idx]["tp"] = r.get("topic", "")


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]


def run_fingerprint() -> str:
    """Binds checkpoints to (taxonomy, prompts, lite identity)."""
    tax = _sha(TAXONOMY_PATH)
    prompts = hashlib.md5((CLASSIFY_PROMPT + SCORE_PROMPT).encode("utf-8")).hexdigest()[:12]
    with open(LITE_PATH, encoding="utf-8") as f:
        n = len(json.load(f))
    return f"tax:{tax};prompt:{prompts};lite_count:{n}"


def acquire_pipeline_lock() -> IO:
    """Non-blocking exclusive lock shared with auto_pipeline (Codex fix #3)."""
    fh = open(PIPELINE_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("[FATAL] .pipeline.lock 被占用（nightly pipeline 运行中？）——拒绝并发")
    return fh


def fix_cluster_integrity(records: list[dict]) -> int:
    """Codex fix #4: cluster families must stay intact (exactly 1 cp=0 parent
    + >=1 cp=1 children, uniform names). If any member of a family lost its
    leaf category (不相关/未分类), dissolve the ENTIRE family — the most
    conservative, validator-safe option. Returns dissolved member count."""
    families: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        cl = r.get("cl")
        if cl not in (None, ""):
            families.setdefault(str(cl), []).append(i)
    dissolved = set()
    for cl, members in families.items():
        if any(records[i].get("c", "") not in LEAVES_SET for i in members):
            dissolved.update(members)
    for i in dissolved:
        records[i].pop("cl", None)
        records[i].pop("cp", None)
        records[i].pop("cln", None)
    return len(dissolved)


def preflight_check(records: list[dict]) -> list[str]:
    """In-memory pre-publish gate (Codex fix #2): never snapshot broken state."""
    errors = []
    for i, r in enumerate(records):
        c = r.get("c", "")
        if c not in VALID_CLASSIFICATIONS_NEW:
            errors.append(f"record {i}: category {c!r} outside whitelist")
        if c == "不相关":
            for f in ("aip", "sc", "scd", "as", "kp", "tp", "cl", "cp", "cln"):
                if r.get(f) not in (None, "", [], {}, 0):
                    errors.append(f"record {i}: AI field {f} on 不相关 record")
        if not r.get("tg"):
            errors.append(f"record {i}: missing tg")
    # cluster families still present must satisfy the validator shape
    fams: dict[str, list[dict]] = {}
    for r in records:
        cl = r.get("cl")
        if cl not in (None, ""):
            fams.setdefault(str(cl), []).append(r)
    for cl, members in fams.items():
        parents = [m for m in members if m.get("cp") == 0]
        children = [m for m in members if m.get("cp") == 1]
        if len(parents) != 1 or not children:
            errors.append(f"cluster {cl}: broken family shape")
    return errors


def apply_scores(records: list[dict], idx_list: list[int], score_map: dict) -> None:
    """Scoring logic from auto_pipeline (tag-aware weights + boosts)."""
    for idx in idx_list:
        r = records[idx]
        sc = score_map.get(idx)
        if not sc:
            continue
        b, i, rr, d, t = sc.get("b", 0), sc.get("i", 0), sc.get("r", 0), sc.get("d", 0), sc.get("t", 0)
        tag = r.get("tg", "")
        is_lit = r.get("i") == "l"
        if is_lit:
            w = {"b": 0.28, "i": 0.15, "r": 0.20, "d": 0.22, "t": 0.15}
        elif tag in ("技术突破", "产业进展"):
            w = {"b": 0.25, "i": 0.25, "r": 0.15, "d": 0.10, "t": 0.25}
        elif tag == "政策监管":
            w = {"b": 0.15, "i": 0.20, "r": 0.25, "d": 0.15, "t": 0.25}
        else:
            w = {"b": 0.20, "i": 0.20, "r": 0.20, "d": 0.15, "t": 0.25}

        score = b * w["b"] + i * w["i"] + rr * w["r"] + d * w["d"] + t * w["t"]

        if t >= 8: score += 0.3
        elif t >= 7: score += 0.15
        if b >= 7: score += 0.4
        if rr >= 7: score += 0.3
        if i >= 7 and not is_lit: score += 0.3

        cat_path = r.get("c", "")
        cross_domain = any(kw in cat_path for kw in ["电网技术", "配电", "储能", "氢能", "碳捕集"])
        if cross_domain and b >= 4:
            score += 0.3
        if tag == "政策监管" and b >= 5:
            score += 0.3

        score = round(score, 1)

        r["sc"] = score
        r["scd"] = {"b": b, "i": i, "r": rr, "d": d, "t": t}

        domain = r["c"].split("/")[0]
        threshold = THRESHOLDS.get(domain, 6.8)
        # Migration note: unlike the nightly pipeline (fresh records only), a
        # record rescored here may carry a stale aip from its old category —
        # clear it when the new score no longer qualifies.
        if score >= threshold:
            r["aip"] = 1
        else:
            r.pop("aip", None)


def main() -> None:
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    no_snapshot = "--no-snapshot" in args or limit is not None
    skip_classify = "--skip-classify" in args
    skip_score = "--skip-score" in args

    lock_fh = acquire_pipeline_lock()  # Codex fix #3
    fingerprint = run_fingerprint()
    log(f"run fingerprint: {fingerprint}")

    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    log("加载 lite ...")
    with open(LITE_PATH, encoding="utf-8") as f:
        records = json.load(f)
    total = len(records)
    old_c = [r.get("c", "") for r in records]
    out_before = sum(1 for c in old_c if c not in LEAVES_SET)
    log(f"共 {total} 条记录; 白名单外 {out_before} 条 (77叶旧目录/不相关/未分类)")

    # ── stage 1: classify ────────────────────────────────────────────────
    if not skip_classify:
        targets = [i for i, c in enumerate(old_c) if c not in LEAVES_SET]
        if limit is not None:
            targets = targets[:limit]
        items = [{"id": i, "type": "literature" if records[i].get("i") == "l" else "news",
                  "title": records[i]["t"][:200], "body": records[i].get("b", "")[:500]}
                 for i in targets]
        cp = JsonCheckpoint(os.path.join(CHECKPOINT_DIR, "classify.json"), fingerprint)
        pending = [it for it in items if str(it["id"]) not in cp.data]
        log(f"分类: 目标 {len(items)}, 待跑 {len(pending)}")
        results = cp.restored_results()
        if pending:
            t0 = time.time()
            new_results = call_glm_batch(CLASSIFY_PROMPT, pending, batch_size=10,
                                         checkpoint_fn=cp.merge_and_save)
            results.extend(new_results)
            dt = time.time() - t0
            log(f"分类批次完成: {len(pending)} 条 / {dt:.0f}s ({dt/max(1,len(pending)):.2f}s/条)")
        apply_classify(records, results)
    else:
        log("跳过分类 (--skip-classify)")

    remaining = sum(1 for r in records if r.get("c", "") not in LEAVES_SET)
    log(f"分类后白名单外剩余: {remaining} (允许: 未分类待自愈)")

    # ── stage 2: score changed/newly-relevant ────────────────────────────
    if not skip_score:
        score_targets = [i for i, r in enumerate(records)
                         if r.get("c", "") in LEAVES_SET
                         and (r.get("c", "") != old_c[i] or not r.get("sc"))]
        items = [{"id": i, "title": r["t"][:200], "body": r.get("b", "")[:500],
                  "category": r.get("c", "")} for i, r in
                 ((i, records[i]) for i in score_targets)]
        cp = JsonCheckpoint(os.path.join(CHECKPOINT_DIR, "score.json"), fingerprint)
        pending = [it for it in items if str(it["id"]) not in cp.data]
        log(f"评分: 目标 {len(items)}, 待跑 {len(pending)}")
        score_map = {r["id"]: r for r in cp.restored_results()}
        if pending:
            new_results = call_glm_batch(SCORE_PROMPT, pending, batch_size=10,
                                         checkpoint_fn=cp.merge_and_save)
            score_map.update({r["id"]: r for r in new_results})
        apply_scores(records, score_targets, score_map)
        log(f"评分完成: 本轮 {len(score_targets)} 条")
    else:
        log("跳过评分 (--skip-score)")

    # ── stage 3: summaries for newly-relevant records lacking `as` ───────
    sub_idx = [i for i, r in enumerate(records)
               if r.get("c", "") in LEAVES_SET and not r.get("as", "").strip()]
    log(f"摘要: {len(sub_idx)} 条缺 AI 摘要")
    if sub_idx:
        ap.gen_summaries([records[i] for i in sub_idx])

    # ── stage 4: key params for newly-relevant eligible records ─────────
    kp_idx = [i for i, r in enumerate(records)
              if r.get("c", "") in LEAVES_SET
              and (len(r.get("b", "").strip()) >= 10 or len(r.get("as", "").strip()) >= 10)
              and not r.get("kp")]
    log(f"关键参数: {len(kp_idx)} 条待提取")
    if kp_idx:
        try:
            ap.extract_key_params([records[i] for i in kp_idx])
        except Exception as e:  # non-fatal, matches pipeline tolerance
            log(f"  [WARN] 关键参数提取失败 (non-fatal): {e}")

    # ── stage 5: report + snapshot ───────────────────────────────────────
    dissolved = fix_cluster_integrity(records)  # Codex fix #4
    log(f"cluster 完整性: 解除 {dissolved} 条成员的聚类归属（家族内有非叶子记录）")

    errors = preflight_check(records)  # Codex fix #2
    if errors:
        log(f"[FATAL] 发布前预检失败 {len(errors)} 项，前 10 条:")
        for e in errors[:10]:
            log(f"  - {e}")
        raise SystemExit(2)

    leaf_cnt = sum(1 for r in records if r.get("c", "") in LEAVES_SET)
    irr_cnt = sum(1 for r in records if r.get("c") == "不相关")
    unc_cnt = sum(1 for r in records if r.get("c") == "未分类")
    changed = sum(1 for i, r in enumerate(records) if r.get("c", "") != old_c[i])
    report = {"total": total, "leaves": leaf_cnt, "不相关": irr_cnt, "未分类": unc_cnt,
              "changed": changed, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    log(f"结果: {json.dumps(report, ensure_ascii=False)}")
    with open(os.path.join(CHECKPOINT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if no_snapshot:
        log("--limit/--no-snapshot: 不重建分片，仅校验内存结果")
        return
    log("重建 lite + 分片 + manifest (build_snapshot) ...")
    shard_count = build_snapshot(records)
    log(f"快照重建完成: {shard_count} 个分片")

    # Post-publish hard gate: validator on the published artifacts. On
    # failure, restore previous data files from git (safe rollback).
    import subprocess
    val = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "validate_data_contract.py")],
                         capture_output=True, text=True, cwd=REPO, timeout=120)
    if val.returncode != 0:
        log("[FATAL] 发布后 validator 失败——回滚 data/processed 至 git HEAD")
        log(val.stdout[-800:] or val.stderr[-800:])
        subprocess.run(["git", "checkout", "--", "data/"], cwd=REPO, timeout=60)
        raise SystemExit(3)
    log("validator 通过 ✓")


if __name__ == "__main__":
    main()
