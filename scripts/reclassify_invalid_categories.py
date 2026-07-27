#!/usr/bin/env python3
"""Reclassify records outside the immutable taxonomy leaf whitelist using GLM 5.2."""
import json, os, subprocess, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = "/home/rhett/tech-db-fresh"
DATA = f"{REPO}/data/processed/all-records-lite.json"
TAX = f"{REPO}/data/category-taxonomy.json"
CHECKPOINT = f"{REPO}/.reclassify_checkpoint.json"

with open(TAX, encoding="utf-8") as f:
    leaves = json.load(f)["categories"]
allowed = set(leaves) | {"不相关", "未分类"}
leaf_text = "\n".join(leaves)

PROMPT = f'''你是技术情报语义分类专家。把每条情报严格分类到以下固定叶子白名单中的一个，或者“不相关”。
必须综合理解技术活动、处理对象和最终目的；严禁仅凭关键词匹配。
禁止输出中间节点，禁止创造、改写、缩短分类路径。
固定叶子白名单：
{leaf_text}
只输出JSON数组：[{{"id":0,"category":"白名单完整路径或不相关"}}]
待分类情报：
'''

def normalize(c):
    c = str(c or "").strip()
    for sep in ['>', '—', '→', '·']:
        c = c.replace(sep, '/')
    return re.sub(r'\s*/\s*', '/', c)

def call(batch):
    items = [{"id": j, "title": r.get("t", "")[:180],
              "summary": (r.get("as") or r.get("b") or "")[:450],
              "current_invalid_category": r.get("c", "")}
             for j, (_idx, r) in enumerate(batch)]
    cmd = ["hermes", "-z", PROMPT + json.dumps(items, ensure_ascii=False),
           "--provider", "openai-api", "-m", "gpt-5.6-sol", "--cli"]
    for attempt in range(3):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd="/home/rhett")
            out = p.stdout or ""
            s, e = out.find('['), out.rfind(']')
            if s >= 0 and e > s:
                rows = json.loads(out[s:e+1])
                result = {}
                for x in rows:
                    lid = x.get("id")
                    cat = normalize(x.get("category"))
                    if isinstance(lid, int) and 0 <= lid < len(batch) and cat in allowed and cat != "未分类":
                        result[lid] = cat
                return result
        except Exception:
            pass
        time.sleep(3 * (attempt + 1))
    return {}

def save(data, done):
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, DATA)
    with open(CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)

def main():
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    done = set()
    if os.path.exists(CHECKPOINT):
        try:
            done = set(json.load(open(CHECKPOINT)))
        except Exception:
            pass
    invalid = [(i, r) for i, r in enumerate(data)
               if normalize(r.get("c")) not in allowed and i not in done]
    print(f"invalid_to_process={len(invalid)}", flush=True)
    batch_size = 8
    batches = [invalid[i:i+batch_size] for i in range(0, len(invalid), batch_size)]
    completed = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(call, b): b for b in batches}
        for fut in as_completed(futs):
            batch = futs[fut]
            result = fut.result()
            for lid, cat in result.items():
                idx = batch[lid][0]
                data[idx]["c"] = cat
                done.add(idx)
                completed += 1
            if completed % 80 < batch_size:
                save(data, done)
                print(f"classified={completed}/{len(invalid)}", flush=True)
    save(data, done)
    remaining = sum(1 for r in data if normalize(r.get("c")) not in allowed)
    print(f"complete classified={completed} remaining_invalid={remaining}", flush=True)
    if remaining:
        raise SystemExit(2)

if __name__ == "__main__":
    main()
