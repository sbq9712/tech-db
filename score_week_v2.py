#!/usr/bin/env python3
"""
tech-db 五维评分 v2（修正版）
- 质量分 = 纯五维加权，不乘信源权重
- 信源等级仅用于聚类选主条
- 只跑非"不相关"记录
"""

import json, subprocess, sys, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BATCH_SIZE = 10
MAX_WORKERS = 6
TIMEOUT = 120

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
    try:
        result = subprocess.run(
            ["hermes", "-z", prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        output = result.stdout.strip()
        if output.startswith("```"):
            lines = output.split("\n")
            output = "\n".join(lines[1:])
            if output.endswith("```"):
                output = output[:-3]
            output = output.strip()
            if output.startswith("json"):
                output = output[4:].strip()
        return json.loads(output)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT]", flush=True)
        return None
    except Exception as e:
        print(f"  [ERROR] {e}", flush=True)
        return None

def score_batch(batch_idx, batch_data):
    items_json = json.dumps(batch_data, ensure_ascii=False)
    prompt = SCORING_PROMPT.format(count=len(batch_data), items_json=items_json)
    result = call_glm(prompt)
    if result is None:
        print(f"  [RETRY] batch {batch_idx}", flush=True)
        time.sleep(2)
        result = call_glm(prompt)
    return batch_idx, result or []

def compute_final_score(scores):
    """Pure 5-dimension weighted score, no source weight."""
    weighted = sum(
        scores.get(dim, 0) * weight
        for dim, weight in SCORE_WEIGHTS.items()
    )
    return round(weighted, 1)

def should_ai_curate(final_score, category):
    for domain, threshold in DOMAIN_THRESHOLDS.items():
        if domain in category:
            return final_score >= threshold
    return False

def main():
    with open("/tmp/week_records.json", "r", encoding="utf-8") as f:
        records = json.load(f)
    
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
    
    print(f"Records: {len(records)}, Batches: {len(batches)}", flush=True)
    
    all_scores = {}
    
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
                print(f"  {completed}/{len(batches)} batches, {len(all_scores)}/{len(records)} scored", flush=True)
    
    print(f"\nScored: {len(all_scores)}/{len(records)}", flush=True)
    
    # Compute final scores
    output = []
    for i, r in enumerate(records):
        if i in all_scores:
            scores = all_scores[i]
            final = compute_final_score(scores)
            cat = r.get("category", "")
            ai = should_ai_curate(final, cat)
            output.append({
                "id": i,
                "url": r.get("url", ""),
                "score": final,
                "sc": {  # dimension scores for tooltip
                    "b": scores["breakthrough"],
                    "i": scores["industry"],
                    "r": scores["rarity"],
                    "d": scores["data"],
                    "t": scores["timeliness"],
                },
                "ai_curate": ai,
            })
    
    outpath = "/tmp/week_scores_v2.json"
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)
    
    scores_list = [o["score"] for o in output]
    ai_count = sum(1 for o in output if o["ai_curate"])
    
    print(f"\n=== Summary ===", flush=True)
    print(f"Scored: {len(output)}", flush=True)
    print(f"Range: {min(scores_list):.1f} - {max(scores_list):.1f}", flush=True)
    print(f"Average: {sum(scores_list)/len(scores_list):.2f}", flush=True)
    print(f"AI精选: {ai_count}/{len(output)} ({ai_count/len(output)*100:.1f}%)", flush=True)
    
    # Distribution
    ranges = {"0-3":0,"3-5":0,"5-6":0,"6-6.5":0,"6.5-7":0,"7-8":0,"8-10":0}
    for s in scores_list:
        if s<3: ranges["0-3"]+=1
        elif s<5: ranges["3-5"]+=1
        elif s<6: ranges["5-6"]+=1
        elif s<6.5: ranges["6-6.5"]+=1
        elif s<7: ranges["6.5-7"]+=1
        elif s<8: ranges["7-8"]+=1
        else: ranges["8-10"]+=1
    print(f"\nDistribution:", flush=True)
    for r, c in ranges.items():
        bar = "█"*(c//3)
        print(f"  {r:>8s}: {c:4d}  {bar}", flush=True)
    
    top20 = sorted(output, key=lambda x: -x["score"])[:20]
    print(f"\nTop 20:", flush=True)
    rec_map = {i: r for i, r in enumerate(records)}
    for o in top20:
        r = rec_map[o["id"]]
        ai = "★" if o["ai_curate"] else ""
        src = r.get("authors","")[:25]
        print(f"{o['score']:5.1f} {ai:>2} {src:<25s} {r.get('title','')[:45]}", flush=True)

if __name__ == "__main__":
    main()
