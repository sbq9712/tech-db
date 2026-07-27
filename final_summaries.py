#!/usr/bin/env python3
"""Final retry for missing AI summaries - batch=5, timeout=240"""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BATCH_SIZE = 5
MAX_WORKERS = 4
TIMEOUT = 240

PROMPT = """你是技术情报摘要专家。为以下每条情报生成100-200字的中文AI摘要。
摘要要求：提炼核心技术内容、关键数据指标、主要结论。不要翻译标题，要基于正文内容生成实质性摘要。
如果正文为空，请基于标题和领域知识生成简要摘要。
只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""

def call_glm(batch):
    prompt = PROMPT + json.dumps(batch, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["hermes", "-z", prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
            capture_output=True, text=True, timeout=TIMEOUT, cwd="/home/rhett"
        )
        out = result.stdout.strip()
        s, e = out.find('['), out.rfind(']')
        if s >= 0 and e > s:
            return json.loads(out[s:e+1])
    except Exception as ex:
        print(f"    [ERROR] {ex}")
    return None

def main():
    lite_path = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
    with open(lite_path) as f:
        lite = json.load(f)
    
    missing = [i for i, r in enumerate(lite) if not r.get("as", "").strip()]
    print(f"Missing: {len(missing)}")
    
    batches = []
    for start in range(0, len(missing), BATCH_SIZE):
        batch_indices = missing[start:start+BATCH_SIZE]
        batch_items = [{"id": idx, "title": lite[idx].get("t","")[:200], "body": lite[idx].get("b","")[:500]} for idx in batch_indices]
        batches.append(batch_items)
    
    print(f"Batches: {len(batches)} (size={BATCH_SIZE})")
    
    results = {}
    done = 0
    last_save = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(call_glm, b): b for b in batches}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    for item in result:
                        results[item["id"]] = item.get("summary", "")
                done += 1
            except: pass
            if done % 20 == 0:
                print(f"  {done}/{len(batches)} done ({len(results)} summaries)")
            if time.time() - last_save > 30:
                # Merge into lite directly
                for idx_str, summary in results.items():
                    idx = int(idx_str)
                    if idx < len(lite) and summary.strip():
                        lite[idx]["as"] = summary
                with open(lite_path, "w") as f:
                    json.dump(lite, f, ensure_ascii=False, separators=(",",":"))
                last_save = time.time()
    
    # Final merge
    merged = 0
    for idx_str, summary in results.items():
        idx = int(idx_str)
        if idx < len(lite) and summary.strip():
            if not lite[idx].get("as", "").strip():
                lite[idx]["as"] = summary
                merged += 1
    
    with open(lite_path, "w") as f:
        json.dump(lite, f, ensure_ascii=False, separators=(",",":"))
    
    total_as = sum(1 for r in lite if r.get("as","").strip())
    print(f"\nMerged: {merged}")
    print(f"Total AI摘要: {total_as}/{len(lite)} ({total_as*100//len(lite)}%)")
    
    # Rebuild chunks
    for i in range(18):
        start = i * 3000
        end = min(start + 3000, len(lite))
        chunk = lite[start:end] if start < len(lite) else []
        content = 'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push(' + json.dumps(chunk, ensure_ascii=False, separators=(",",":")) + ');'
        with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js", "w") as f:
            f.write(content)
    
    print("Done")

if __name__ == "__main__":
    main()
