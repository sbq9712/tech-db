#!/usr/bin/env python3
"""Incremental event clustering: embedding/entity recall + strict structured adjudication.

Safety properties:
- Candidate recall may be broad; only high-confidence same-event judgments merge.
- Same product/entity is candidate evidence, never merge evidence.
- A proposed group must be a complete graph of accepted pair judgments.
- Existing clusters are preserved; confirmed groups are merged incrementally.
- Cache keys are content fingerprints, not mutable list indexes.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_snapshot import build_snapshot
from data_contract import is_relevant
from llm_client import call_glm

REPO = Path(__file__).resolve().parents[1]
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
EMB_PATH = REPO / "data" / "processed" / "embeddings-v2.json"
TIME_WINDOW_DAYS = 20
COSINE_THRESHOLD = 0.82
ENTITY_COSINE_FLOOR = 0.70
TOPIC_COSINE_FLOOR = 0.60
MIN_CONFIDENCE = 0.85
BATCH_SIZE = 12

_MODEL = None


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def record_text(record: dict) -> str:
    return f"{record.get('t', '')[:200]} {record.get('as', '')[:300]}".strip()


def fingerprint(record: dict) -> str:
    raw = "\0".join((record.get("u", ""), record.get("t", ""), record.get("d", ""), record_text(record)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_model():
    global _MODEL
    if _MODEL is None:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("BAAI/bge-m3", device="cpu")
    return _MODEL


def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors = get_model().encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosine_matrix_rows(matrix, target_vectors):
    """Vectorized cosine similarity between each row of matrix and each target vector.

    All inputs are L2-normalized, so cosine = dot product.
    """
    if np is None:
        return None
    mat = np.asarray(matrix, dtype=np.float32)
    tgt = np.asarray(target_vectors, dtype=np.float32)
    return mat @ tgt.T


def cosine_vec_to_many(target, matrix):
    if np is None:
        return None
    t = np.asarray(target, dtype=np.float32)
    m = np.asarray(matrix, dtype=np.float32)
    return m @ t


# Only specific named models/projects/products. Generic domain words are deliberately excluded.
ENTITY_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])(?:GPT|Claude|Gemini|Llama|Qwen|DeepSeek|GLM|Grok|Mythos|Fable)[-\s]?[A-Za-z]*\d+(?:\.\d+)*(?:[-\s]?(?:Pro|Max|Mini|Turbo|Flash|Ultra))?(?![A-Za-z0-9])", re.I),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]+[-_][A-Za-z0-9][A-Za-z0-9._-]*\b"),
    re.compile(r"[A-Za-z]{2,}\s?[-–—]?\s?\d+(?:\.\d+){1,3}\b"),
)


def specific_entities(record: dict) -> set[str]:
    text = record_text(record)
    entities = set()
    for pattern in ENTITY_PATTERNS:
        for match in pattern.findall(text):
            normalized = re.sub(r"[\s_–—]+", "-", match).upper()
            entities.add(normalized)
    return entities


def candidate_reason(a: dict, b: dict, similarity: float) -> str | None:
    da, db = parse_date(a.get("d", "")), parse_date(b.get("d", ""))
    if not da or not db or abs((da - db).days) > TIME_WINDOW_DAYS:
        return None
    shared = specific_entities(a) & specific_entities(b)
    if similarity >= COSINE_THRESHOLD:
        return "embedding+entity" if shared else "embedding"
    if shared and similarity >= ENTITY_COSINE_FLOOR:
        return "specific-entity"
    # Topic match: same topic tag + moderate similarity
    topic_a = (a.get("tp") or "").strip()
    topic_b = (b.get("tp") or "").strip()
    if topic_a and topic_a == topic_b and similarity >= TOPIC_COSINE_FLOOR:
        return "topic-match"
    # Topic-similar match: topics not identical but highly overlapping (bigram Jaccard)
    # e.g. "光伏免税" vs "电池免税" — same policy event, different angle labels
    if topic_a and topic_b:
        topic_overlap = _title_keyword_overlap(topic_a, topic_b)
        if topic_overlap >= 0.4 and similarity >= TOPIC_COSINE_FLOOR:
            return "topic-similar"
    # Title-keyword match: same topic + high title overlap (for short articles
    # where embedding may be noisy due to boilerplate/ads in body text)
    if topic_a and topic_a == topic_b:
        title_overlap = _title_keyword_overlap(a.get("t", ""), b.get("t", ""))
        if title_overlap >= 0.5:
            return "title-keyword"
    # Policy-event match: both records reference the same policy/regulatory event
    # The defining signal is shared SPECIFIC policy keywords (e.g. "消费税", "免税").
    # Title overlap is naturally low for policy news because outlets report
    # different angles of the same policy. So we rely on: shared specific policy kw +
    # moderate embedding similarity + date proximity (already checked above).
    if similarity >= 0.35:
        text_a = (a.get("t", "") or "") + (a.get("as", "") or "")
        text_b = (b.get("t", "") or "") + (b.get("as", "") or "")
        spec_kw = _policy_keywords_specific(text_a) & _policy_keywords_specific(text_b)
        if spec_kw:
            return "policy-event"
    return None


def _title_keyword_overlap(ta: str, tb: str) -> float:
    """Jaccard similarity of character bigrams in two titles (handles Chinese text)."""
    def char_bigrams(text: str) -> set:
        text = re.sub(r"[^一-鿿A-Za-z0-9]", "", text)
        if len(text) < 2:
            return set()
        return set(text[i:i+2] for i in range(len(text) - 1))

    ba, bb = char_bigrams(ta), char_bigrams(tb)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


# Policy/regulatory keywords for detecting same-policy-event pairs.
# Tier 1 (specific): identify a concrete policy action — strong event signal.
# Tier 2 (generic): broad policy context — weak signal, needs additional evidence.
_POLICY_KW_SPECIFIC = {
    "消费税", "免税", "征税", "出口退税",
    "补贴退坡", "补贴取消", "双反", "反倾销", "反补贴",
    "出口管制", "禁令", "制裁", "碳关税", "碳税",
    "联合发文", "三部门", "多部门",
}
_POLICY_KW_GENERIC = {
    "财政", "财政部", "税务总局", "工信部", "发改委", "能源局",
    "税收", "新规", "新政策", "政策", "法规", "立法",
    "监管", "准入", "标准", "目录", "白名单", "碳市场",
    "关税", "配额", "指标", "十四五", "十五五", "规划", "纲要",
}


def _policy_keywords(text: str) -> set:
    """Extract policy/regulatory keywords from text (exact substring match)."""
    if not text:
        return set()
    return {kw for kw in (_POLICY_KW_SPECIFIC | _POLICY_KW_GENERIC) if kw in text}


def _policy_keywords_specific(text: str) -> set:
    """Extract only specific (event-identifying) policy keywords from text."""
    if not text:
        return set()
    return {kw for kw in _POLICY_KW_SPECIFIC if kw in text}
RELEASE_NEGATIVE = re.compile(r"(?:泄露|曝光|传闻|实测|评测|作弊|叫停|延期|研究员|能力评价|基准测试)")


def release_lifecycle_pair(a: dict, b: dict) -> tuple[bool, str]:
    """Controlled same-product release-cycle exception within the hard time window."""
    da, db = parse_date(a.get("d", "")), parse_date(b.get("d", ""))
    if not da or not db or abs((da - db).days) > TIME_WINDOW_DAYS:
        return False, ""
    shared = specific_entities(a) & specific_entities(b)
    if not shared:
        return False, ""
    title_a, title_b = a.get("t", ""), b.get("t", "")
    text_a, text_b = record_text(a), record_text(b)
    # Negative lifecycle actions must describe the article's main event (title), not
    # incidental summary text such as "stronger than the rumored Mythos model".
    if RELEASE_NEGATIVE.search(title_a) or RELEASE_NEGATIVE.search(title_b):
        return False, ""
    if not RELEASE_POSITIVE.search(text_a) or not RELEASE_POSITIVE.search(text_b):
        return False, ""
    entity = sorted(shared, key=len, reverse=True)[0]
    return True, f"发布生命周期|{entity}|{min(da, db):%Y-%m-%d}"


EVENT_PROMPT = """你是严格的技术情报事件消歧器。判断每一对报道是否属于同一个具体事件，而不是同一主题。

必须提取并返回：
- actor：事件主体
- object：具体产品/项目/论文/政策
- action：发布/预告/上线/签约/融资/研究发现等
- status：预告、正式发生、后续报道或独立事件
- event_key：主体|具体对象|核心动作|事件时间（可归一化）
- same：是否同一具体事件
- confidence：0到1
- conflict_reason：若不同，指出对象、动作、时间、地点或指标冲突

裁决规则：
1. 预告即将发布、正式发布、上线及同一轮可用性报道，可以是同一事件的状态演进。
2. 仅共享公司、技术领域或关键词绝不是同一事件。
3. “钠/钠电池/AI/储能”等宽泛词不能作为同事件证据。
4. 同一公司不同产品、同一产品不同轮次发布、同领域不同论文/工厂/项目必须判否。
5. 不确定时判否；只有证据充分才 same=true。
6. 政策/监管事件例外：同一政策文件（如三部门联合发文、财政部/税务总局通知）被不同媒体从不同角度报道（如一篇侧重光伏免税、一篇侧重电池征税，但都指向同一份政策文件），应判为同一事件。判据：提及相同发文部门+相同政策文件/同一轮政策动作+相近日期。

只输出JSON数组：
[{"id":0,"actor":"...","object":"...","action":"...","status":"...","event_key":"...","same":true,"confidence":0.95,"conflict_reason":""}]
待判断：
"""


def _safe_json_loads(text: str) -> list | None:
    """Try to parse a JSON array from text, with progressive cleanup fallbacks."""
    # 1. Direct extraction of first JSON array span.
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    snippet = text[start:end + 1]
    try:
        parsed = json.loads(snippet)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # 2. Strip Markdown code fences if present.
    fenced = re.sub(r"^```(?:json)?|```$", "", snippet, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(fenced)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # 3. Tolerate unescaped control characters / smart quotes.
    cleaned = (fenced
               .replace("\u201c", "\"").replace("\u201d", "\"")
               .replace("\u2018", "'").replace("\u2019", "'")
               .replace("\n", " ").replace("\t", " "))
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def call_event_judge(items: list[dict], provider: str, model: str) -> list[dict]:
    """Call the LLM event judge with up to 3 retries; never raise on malformed output."""
    prompt = EVENT_PROMPT + json.dumps(items, ensure_ascii=False)
    last_error = ""
    for attempt in range(3):
        try:
            out = call_glm(prompt, system_msg="你是事件聚类裁决专家。直接输出JSON数组，不要输出思考过程和markdown标记。",
                           model=model, timeout=240)
        except Exception as e:
            last_error = f"API error: {e}"
            continue
        parsed = _safe_json_loads(out)
        if parsed is None:
            last_error = f"unparseable output: {out[:200]}"
            continue
        # Validate id set matches what we sent.
        expected_ids = {item["id"] for item in items}
        result_ids = {r.get("id") for r in parsed}
        if result_ids != expected_ids:
            last_error = f"id mismatch: expected {sorted(expected_ids)}, got {sorted(result_ids)}"
            continue
        return parsed
    # All retries failed: degrade gracefully — mark every pair as not-same with low confidence.
    log(f"  call_event_judge FAILED after 3 attempts: {last_error}")
    return [{
        "id": item["id"], "actor": "", "object": "", "action": "",
        "status": "解析失败", "event_key": "", "same": False,
        "confidence": 0.0, "conflict_reason": "LLM 输出无法解析",
    } for item in items]


def load_cache() -> dict[str, list[float]]:
    if not EMB_PATH.exists():
        return {}
    try:
        payload = json.loads(EMB_PATH.read_text(encoding="utf-8"))
        return payload.get("vectors", {}) if payload.get("version") == 2 else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save_cache(cache: dict[str, list[float]]) -> None:
    temp = EMB_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 2, "vectors": cache}, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, EMB_PATH)


def ensure_vectors(data: list[dict], indices: list[int], cache: dict, save_every: int = 256) -> dict[int, list[float]]:
    vectors = {}
    missing = []
    for idx in indices:
        key = fingerprint(data[idx])
        if key in cache:
            vectors[idx] = cache[key]
        else:
            missing.append(idx)
    saved = 0
    for start in range(0, len(missing), 32):
        batch = missing[start:start + 32]
        embedded = embed_texts([record_text(data[idx]) for idx in batch])
        for idx, vector in zip(batch, embedded):
            key = fingerprint(data[idx])
            cache[key] = vector
            vectors[idx] = vector
            saved += 1
            if saved and saved % save_every == 0:
                save_cache(cache)
                log(f"  cached {saved}/{len(missing)} new embeddings")
    if missing:
        save_cache(cache)
        log(f"  embeddings ready: {len(missing)} new, cache size {len(cache)}")
    return vectors


def build_date_buckets(data: list[dict], pool: list[int]) -> dict:
    buckets = defaultdict(list)
    for idx in pool:
        date = parse_date(data[idx].get("d", ""))
        if date:
            buckets[date].append(idx)
    return buckets


def pool_window_indices(buckets: dict, date_a: datetime, window_days: int) -> list[int]:
    pool = []
    for offset in range(-window_days, window_days + 1):
        day = date_a + timedelta(days=offset)
        if day in buckets:
            pool.extend(buckets[day])
    return pool


def make_candidates(
    data: list[dict],
    target_indices: list[int],
    pool_indices: list[int],
    vectors: dict[int, list[float]],
) -> list[dict]:
    """Compare targets to a date-window pool using vectorized cosine."""
    buckets = build_date_buckets(data, pool_indices)
    candidates = []
    seen = set()
    for a in sorted(target_indices):
        date_a = parse_date(data[a].get("d", ""))
        if not date_a:
            continue
        window = pool_window_indices(buckets, date_a, TIME_WINDOW_DAYS)
        if a in window:
            window = [b for b in window if b != a]
        if not window:
            continue
        # Vectorized cosine: window vectors vs target vector
        win_matrix = [vectors[b] for b in window]
        sims = cosine_vec_to_many(vectors[a], win_matrix)
        for pos, b in enumerate(window):
            pair_key = (min(a, b), max(a, b))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            similarity = float(sims[pos]) if sims is not None else cosine(vectors[a], vectors[b])
            reason = candidate_reason(data[a], data[b], similarity)
            if reason:
                candidates.append({"a": pair_key[0], "b": pair_key[1], "similarity": similarity, "reason": reason})
    return candidates


CHECKPOINT_PATH = REPO / "data" / "processed" / "clustering-decisions.jsonl"


def load_checkpoint() -> dict[tuple[int, int], dict]:
    decisions = {}
    if CHECKPOINT_PATH.exists():
        for line in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pair = (row["a"], row["b"])
            decisions[pair] = row
    return decisions


def append_checkpoint(decisions_batch: list[tuple[tuple[int, int], dict]]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
        for pair, value in decisions_batch:
            row = {"a": pair[0], "b": pair[1], **value}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()


def adjudicate(data: list[dict], candidates: list[dict], provider: str, model: str) -> dict[tuple[int, int], dict]:
    existing = load_checkpoint()
    decisions = {}
    todo = []
    for pair in candidates:
        key = (pair["a"], pair["b"])
        if key in existing:
            decisions[key] = existing[key]
        else:
            todo.append(pair)
    log(f"  adjudicate: {len(decisions)} cached, {len(todo)} new pairs")
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        items = []
        for local_id, pair in enumerate(batch):
            a, b = pair["a"], pair["b"]
            items.append({
                "id": local_id, "candidate_reason": pair["reason"],
                "similarity": round(pair["similarity"], 4),
                "a": {"title": data[a].get("t", ""), "summary": data[a].get("as", ""), "date": data[a].get("d", "")},
                "b": {"title": data[b].get("t", ""), "summary": data[b].get("as", ""), "date": data[b].get("d", "")},
            })
        results = call_event_judge(items, provider, model)
        by_id = {result.get("id"): result for result in results}
        if set(by_id) != set(range(len(batch))):
            raise RuntimeError("event judge omitted or duplicated pair ids")
        batch_decisions = []
        for local_id, pair in enumerate(batch):
            result = by_id[local_id]
            lifecycle, lifecycle_key = release_lifecycle_pair(data[pair["a"]], data[pair["b"]])
            if lifecycle:
                result.update({
                    "same": True,
                    "confidence": max(float(result.get("confidence", 0)), MIN_CONFIDENCE),
                    "status": "同一精确产品的发布生命周期",
                    "event_key": lifecycle_key,
                    "conflict_reason": "",
                    "rule": "release-lifecycle",
                })
            result["accepted"] = bool(result.get("same")) and float(result.get("confidence", 0)) >= MIN_CONFIDENCE
            key = (pair["a"], pair["b"])
            decisions[key] = result
            batch_decisions.append((key, result))
        append_checkpoint(batch_decisions)
        log(f"  batch {start // BATCH_SIZE + 1}/{(len(todo) + BATCH_SIZE - 1) // BATCH_SIZE} done")
    return decisions


def complete_link_groups(indices: list[int], decisions: dict[tuple[int, int], dict]) -> list[list[int]]:
    """Greedy complete-link groups: every pair in a group must be explicitly accepted."""
    groups = []
    for idx in sorted(indices):
        placed = False
        for group in groups:
            if all(decisions.get((min(idx, other), max(idx, other)), {}).get("accepted") for other in group):
                group.append(idx)
                placed = True
                break
        if not placed:
            groups.append([idx])
    return [group for group in groups if len(group) > 1]


def root_rank(record: dict) -> tuple:
    date = record.get("d", "9999-99-99")
    return (record.get("lv", 0) > 0, record.get("lv", 0), record.get("sc", 0), -int(date.replace("-", "")))


def apply_groups(data: list[dict], groups: list[list[int]], decisions: dict) -> list[dict]:
    applied = []
    for group in groups:
        # Merge any existing clusters touched by this confirmed complete-link group.
        existing_ids = {str(data[idx].get("cl")) for idx in group if data[idx].get("cl") not in (None, "")}
        members = set(group)
        if existing_ids:
            members.update(i for i, record in enumerate(data) if str(record.get("cl")) in existing_ids)
        root = max(members, key=lambda idx: root_rank(data[idx]))
        digest = hashlib.sha1("|".join(str(i) for i in sorted(members)).encode()).hexdigest()[:10]
        event_keys = [decisions[pair].get("event_key", "") for pair in decisions if pair[0] in group and pair[1] in group]
        cluster_name = max((key for key in event_keys if key), key=len, default=data[root].get("t", ""))
        for idx in members:
            data[idx]["cl"] = digest
            data[idx]["cp"] = 0 if idx == root else 1
            data[idx]["cln"] = cluster_name
        applied.append({"cluster": digest, "root": root, "members": sorted(members), "name": cluster_name})
    return applied


def run(indices: list[int] | None, dry_run: bool, provider: str, model: str, full: bool = False, estimate: bool = False) -> dict:
    data = json.loads(LITE_PATH.read_text(encoding="utf-8"))
    eligible = [i for i, record in enumerate(data) if is_relevant(record) and record.get("dp") != 1]
    eligible_set = set(eligible)
    if indices is None:
        if not full:
            raise RuntimeError("refusing implicit full clustering; pass --ids or explicit --full")
        selected = eligible
        pool = eligible
    else:
        selected = [i for i in indices if i in eligible_set]
        selected_dates = [parse_date(data[i].get("d", "")) for i in selected]
        selected_dates = [date for date in selected_dates if date]
        if not selected_dates:
            raise RuntimeError("selected records have no valid dates")
        earliest, latest = min(selected_dates), max(selected_dates)
        pool = [
            i for i in eligible
            if (date := parse_date(data[i].get("d", "")))
            and (earliest - date).days <= TIME_WINDOW_DAYS
            and (date - latest).days <= TIME_WINDOW_DAYS
        ]
    if len(selected) < 1 or len(pool) < 2:
        raise RuntimeError("need at least one target and two eligible pool records")
    cache = load_cache()
    vectors = ensure_vectors(data, sorted(set(selected) | set(pool)), cache)
    candidates = make_candidates(data, selected, pool, vectors)
    if estimate:
        # Reason distribution and batch cost, no model calls.
        reason_counts = defaultdict(int)
        for pair in candidates:
            reason_counts[pair["reason"]] += 1
        return {
            "selected": len(selected), "pool": len(pool),
            "candidate_pairs": len(candidates),
            "reason_counts": dict(reason_counts),
            "estimated_llm_batches": (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE,
        }
    if not candidates:
        return {"selected": selected, "candidates": [], "groups": [], "applied": []}
    decisions = adjudicate(data, candidates, provider, model)
    judged_indices = sorted({idx for pair in decisions for idx in pair})
    groups = complete_link_groups(judged_indices, decisions)
    applied = apply_groups(data, groups, decisions) if groups else []
    report = {"selected": selected, "candidates": candidates, "decisions": decisions, "groups": groups, "applied": applied}
    if applied and not dry_run:
        build_snapshot(data)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", help="comma-separated target record indexes")
    parser.add_argument("--full", action="store_true", help="explicitly allow a full eligible-set run")
    parser.add_argument("--estimate", action="store_true", help="only count candidate pairs and cost, no LLM")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", default="zai")
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument("--report", default="/tmp/clustering-report.json")
    args = parser.parse_args()
    indices = [int(value) for value in args.ids.split(",")] if args.ids else None
    report = run(indices, args.dry_run, args.provider, args.model, full=args.full, estimate=args.estimate)
    if args.estimate:
        print(json.dumps(report, ensure_ascii=False))
        return
    serializable = dict(report)
    serializable["decisions"] = {f"{a},{b}": value for (a, b), value in report.get("decisions", {}).items()}
    Path(args.report).write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"selected": len(report["selected"]), "candidates": len(report["candidates"]), "groups": report["groups"], "applied": report["applied"], "dry_run": args.dry_run}, ensure_ascii=False))


if __name__ == "__main__":
    main()
