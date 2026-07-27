#!/usr/bin/env python3
"""
tech-db 五维评分脚本（本周测试版）
对 /tmp/week_records.json 中的831条记录逐批调用 GLM 5.2 打分。
"""

import json, subprocess, sys, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BATCH_SIZE = 10
MAX_WORKERS = 6
TIMEOUT = 120

TIER_WEIGHTS = {"T1": 1.0, "T1.5": 0.85, "T2": 0.70}
DOMAIN_THRESHOLDS = {
    "零碳产业": 6.5,
    "AI与智能科技": 6.5,
    "通用技术": 7.0,
}
SCORE_WEIGHTS = {
    "breakthrough": 0.30,
    "industry": 0.25,
    "rarity": 0.15,
    "data": 0.15,
    "timeliness": 0.15,
}

SCORING_PROMPT = """你是技术情报质量评估专家。对以下{count}条技术情报，每条打5个维度的分数（0-10分，可保留1位小数）。

维度定义：
1. breakthrough（技术突破性）：纯政策/市场=0；渐进改进=5；新机理/新材料/新架构=10
2. industry（产业影响力）：实验室概念=1；小规模验证=5；量产落地/产业链级=10
3. rarity（信息稀缺性）：转载旧闻=0；常规跟踪=5；独家首发/首次披露=10
4. data（数据充实度）：纯定性描述=0；有定性+部分参数=5；多个硬数据/量化指标=10
5. timeliness（时效性）：趋势分析/综述=2；近期进展=6；突发事件/最新发布=10

待评估情报：
{items_json}

只输出JSON数组，不要markdown代码块：
[{{"id":0,"breakthrough":7.5,"industry":6.0,"rarity":5.0,"data":8.0,"timeliness":7.0}}]"""

def call_glm(prompt):
    """Call GLM 5.2 via hermes CLI."""
    try:
        result = subprocess.run(
            ["hermes", "-z", prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        output = result.stdout.strip()
        # Strip markdown code fences if present
        if output.startswith("```"):
            output = output.split("\n", 1)[1] if "\n" in output else output[3:]
            if output.endswith("```"):
                output = output[:-3]
            output = output.strip()
            if output.startswith("json"):
                output = output[4:].strip()
        return json.loads(output)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] batch timed out")
        return None
    except json.JSONDecodeError as e:
        print(f"  [JSON ERROR] {e}")
        print(f"  Output: {result.stdout[:200]}")
        return None
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None

def score_batch(batch_idx, batch_data):
    """Score a single batch."""
    items_json = json.dumps(batch_data, ensure_ascii=False)
    prompt = SCORING_PROMPT.format(count=len(batch_data), items_json=items_json)
    
    result = call_glm(prompt)
    if result is None:
        # Retry once
        print(f"  [RETRY] batch {batch_idx}")
        time.sleep(2)
        result = call_glm(prompt)
    
    return batch_idx, result or []

def compute_final_score(scores, source_tier):
    """Compute final quality score from 5 dimensions + source weight."""
    weighted = sum(
        scores.get(dim, 0) * weight
        for dim, weight in SCORE_WEIGHTS.items()
    )
    tier_weight = TIER_WEIGHTS.get(source_tier, 0.70)
    return round(weighted * tier_weight, 1)

def should_ai_curate(final_score, category):
    """Check if score exceeds domain threshold for AI精选."""
    for domain, threshold in DOMAIN_THRESHOLDS.items():
        if domain in category:
            return final_score >= threshold
    return False

def main():
    # Load data
    with open("/tmp/week_records.json", "r", encoding="utf-8") as f:
        records = json.load(f)
    
    with open("/tmp/source_tiers.json", "r", encoding="utf-8") as f:
        tier_map = json.load(f)
    
    # Prepare batches
    scoring_input = []
    for i, r in enumerate(records):
        body = r.get("body", "")[:300]
        scoring_input.append({
            "id": i,
            "title": r.get("title", ""),
            "body": body,
            "category": r.get("category", ""),
            "type": r.get("intelligence_type", ""),
        })
    
    batches = [(i, scoring_input[i:i+BATCH_SIZE]) 
               for i in range(0, len(scoring_input), BATCH_SIZE)]
    
    print(f"Total records: {len(records)}")
    print(f"Batches: {len(batches)}, Workers: {MAX_WORKERS}")
    print(f"Starting scoring...\n")
    
    all_scores = {}  # id → {breakthrough, industry, ...}
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(score_batch, idx, batch): idx 
                   for idx, batch in batches}
        
        completed = 0
        for future in as_completed(futures):
            batch_idx, results = future.result()
            completed += 1
            
            for item in results:
                rid = item.get("id")
                if rid is not None:
                    all_scores[rid] = {
                        "breakthrough": item.get("breakthrough", 0),
                        "industry": item.get("industry", 0),
                        "rarity": item.get("rarity", 0),
                        "data": item.get("data", 0),
                        "timeliness": item.get("timeliness", 0),
                    }
            
            if completed % 10 == 0 or completed == len(batches):
                print(f"  Progress: {completed}/{len(batches)} batches, "
                      f"{len(all_scores)}/{len(records)} scored")
    
    print(f"\nScoring complete: {len(all_scores)}/{len(records)} records scored")
    
    # Compute final scores and AI精选
    output = []
    for i, r in enumerate(records):
        if i in all_scores:
            scores = all_scores[i]
            source = r.get("authors", "")
            tier = tier_map.get(source, "T2")
            final = compute_final_score(scores, tier)
            ai_curate = should_ai_curate(final, r.get("category", ""))
            
            output.append({
                "id": i,
                "title": r.get("title", "")[:60],
                "source": source[:40],
                "tier": tier,
                "breakthrough": scores["breakthrough"],
                "industry": scores["industry"],
                "rarity": scores["rarity"],
                "data": scores["data"],
                "timeliness": scores["timeliness"],
                "score": final,
                "ai_curate": ai_curate,
            })
    
    # Save results
    outpath = "/tmp/week_scores.json"
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    # Summary stats
    scores_list = [o["score"] for o in output]
    ai_curated = sum(1 for o in output if o["ai_curate"])
    
    print(f"\n=== Summary ===")
    print(f"Records scored: {len(output)}")
    print(f"Score range: {min(scores_list):.1f} - {max(scores_list):.1f}")
    print(f"Average score: {sum(scores_list)/len(scores_list):.1f}")
    print(f"AI精选 (score >= threshold): {ai_curated}/{len(output)} ({ai_curated/len(output)*100:.1f}%)")
    
    # Top 20
    top20 = sorted(output, key=lambda x: -x["score"])[:20]
    print(f"\nTop 20 by score:")
    print(f"{'Score':>5}  {'AI':>2}  {'Tier':>4}  {'Source':<30s}  Title")
    print("-" * 120)
    for o in top20:
        ai = "★" if o["ai_curate"] else ""
        print(f"{o['score']:5.1f}  {ai:>2}  {o['tier']:>4}  {o['source']:<30s}  {o['title']}")

if __name__ == "__main__":
    main()
