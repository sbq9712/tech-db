#!/usr/bin/env python3
"""
为缺AI摘要的记录生成摘要。
分批调用GLM，每批20条，6线程并行。
每条基于title+body（截断），输出100-200字中文摘要。
"""
import json, subprocess, sys, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BATCH_SIZE = 20
MAX_WORKERS = 6
TIMEOUT = 180
SAVE_INTERVAL = 30  # seconds

SUMMARY_PROMPT = """你是技术情报摘要专家。为以下每条情报生成100-200字的中文AI摘要。
摘要要求：提炼核心技术内容、关键数据指标、主要结论。不要翻译标题，要基于正文内容生成实质性摘要。

待处理情报（JSON数组）：
"""

def call_glm(batch_items, timeout=TIMEOUT):
    prompt = SUMMARY_PROMPT + json.dumps(batch_items, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["hermes", "-z", prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
            capture_output=True, text=True, timeout=timeout,
            cwd="/home/rhett"
        )
        out = result.stdout.strip()
        start = out.find('[')
        end = out.rfind(']')
        if start >= 0 and end > start:
            return json.loads(out[start:end+1])
    except Exception as e:
        print(f"    [ERROR] {e}")
    return None

def main():
    lite_path = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
    with open(lite_path) as f:
        lite = json.load(f)
    
    # Find records without AI summary
    missing = []
    for i, r in enumerate(lite):
        if not r.get("as", "").strip():
            missing.append(i)
    
    print(f"Total records: {len(lite)}")
    print(f"Missing summary: {len(missing)}")
    
    # Prepare batches
    batches = []
    for start in range(0, len(missing), BATCH_SIZE):
        batch_indices = missing[start:start+BATCH_SIZE]
        batch_items = []
        for idx in batch_indices:
            r = lite[idx]
            batch_items.append({
                "id": idx,
                "title": r.get("t", "")[:200],
                "body": r.get("b", "")[:500],
            })
        batches.append(batch_items)
    
    print(f"Batches: {len(batches)}")
    
    # Save file for incremental writes
    out_path = "/tmp/ai_summaries_new.json"
    summaries = {}
    if os.path.exists(out_path):
        with open(out_path) as f:
            summaries = json.load(f)
    print(f"Already have: {len(summaries)}")
    
    # Filter out already done
    batches = [b for b in batches if b[0]["id"] not in summaries and b[-1]["id"] not in summaries]
    print(f"Remaining batches: {len(batches)}")
    
    done = 0
    last_save = time.time()
    
    def process_batch(batch):
        result = call_glm(batch)
        return (batch, result)
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_batch, b): b for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                _, result = future.result()
                if result:
                    for item in result:
                        summaries[item["id"]] = item.get("summary", "")
                    done += 1
                else:
                    print(f"  [FAIL] batch starting at {batch[0]['id']}")
            except Exception as e:
                print(f"  [FAIL] batch starting at {batch[0]['id']}: {e}")
            
            if done % 10 == 0:
                print(f"  {done}/{len(batches)} batches done ({len(summaries)} summaries)")
            
            # Periodic save
            if time.time() - last_save > SAVE_INTERVAL:
                with open(out_path, "w") as f:
                    json.dump(summaries, f, ensure_ascii=False)
                last_save = time.time()
    
    # Final save
    with open(out_path, "w") as f:
        json.dump(summaries, f, ensure_ascii=False)
    
    print(f"\nDone: {len(summaries)} total summaries")
    print(f"Saved to {out_path}")

if __name__ == "__main__":
    main()
