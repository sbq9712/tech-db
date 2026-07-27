#!/usr/bin/env python3
"""Retry classify+score for failed records (batch=5, longer timeout)"""
import json, subprocess, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BATCH_SIZE = 5
MAX_WORKERS = 4
TIMEOUT = 240

CLASSIFY_PROMPT = """你是技术情报语义分类专家。根据定义对每条情报分类。
分类优先级：零碳产业 > AI与智能科技 > 通用技术 > 不相关
不相关 = 纯财务/政策/市场/人事/地缘政治，无技术研发或工程实践。
零碳产业子类：物质循环(资源处理/有机物碳循环/无机物)、能量循环(发电/储能/氢基能源)
AI子类：AI软件层(底座大模型/工程改进)、AI硬件层(半导体/芯片/计算集群/数据中心)、具身智能、量子信息
通用技术：检测表征、通信运输(电网/航天/航空/陆路/水路)、催化剂、材料工程

只输出JSON数组：[{"id":0,"category":"完整路径或不相关","tag":"news专属标签或空","topic":"5字名词性短语"}]
news标签5选1：技术突破/产业进展/政策监管/资本运作/行业观察
literature标签2选1：研究论文/观点评论

待分类情报：
"""

SCORE_PROMPT = """你是技术情报质量评估专家。对以下每条情报打5个维度分数（0-10分）。
1. breakthrough（技术突破性）：纯政策/市场=0；渐进改进=5；新机理/新材料/新架构=10
2. industry（产业影响力）：实验室概念=1；小规模验证=5；量产落地/产业链级=10
3. rarity（信息稀缺性）：转载旧闻=0；常规跟踪=5；独家首发/首次披露=10
4. data（数据充实度）：纯定性=0；定性+部分参数=5；多个硬数据=10
5. timeliness（时效性）：趋势分析/综述=2；近期进展=6；突发事件/最新发布=10

只输出JSON数组：[{"id":0,"b":7.5,"i":6.0,"r":5.0,"d":8.0,"t":7.0}]

待评估情报：
"""

def call_glm(prompt, batch_json, timeout=TIMEOUT):
    full_prompt = prompt + json.dumps(batch_json, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["hermes", "-z", full_prompt, "--provider", "zai", "-m", "glm-5.2", "--cli"],
            capture_output=True, text=True, timeout=timeout, cwd="/home/rhett"
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
    with open("/tmp/retry_records.json") as f:
        records = json.load(f)
    print(f"Retry records: {len(records)}")

    batches = []
    for i in range(0, len(records), BATCH_SIZE):
        batch_items = []
        for j, r in enumerate(records[i:i+BATCH_SIZE]):
            batch_items.append({
                "id": i + j,
                "type": "news" if r.get("i") == "n" else "literature",
                "title": r.get("t", "")[:200],
                "body": r.get("b", "")[:300],
                "category": "未分类",
            })
        batches.append(batch_items)

    print(f"Batches: {len(batches)} (batch_size={BATCH_SIZE})")

    # Phase 1: Classify
    print("\n=== CLASSIFY ===")
    classify_results = {}
    def do_classify(batch):
        return (batch[0]["id"], call_glm(CLASSIFY_PROMPT, batch))
    
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(do_classify, b): b for b in batches}
        for f in as_completed(futures):
            try:
                bid, result = f.result()
                if result:
                    for item in result:
                        classify_results[item["id"]] = item
            except: pass
            done += 1
            if done % 10 == 0: print(f"  {done}/{len(batches)} classified")
    print(f"Classified: {len(classify_results)}/{len(records)}")

    # Phase 2: Score
    print("\n=== SCORE ===")
    score_results = {}
    def do_score(batch):
        return (batch[0]["id"], call_glm(SCORE_PROMPT, batch))
    
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(do_score, b): b for b in batches}
        for f in as_completed(futures):
            try:
                bid, result = f.result()
                if result:
                    for item in result:
                        score_results[item["id"]] = item
            except: pass
            done += 1
            if done % 10 == 0: print(f"  {done}/{len(batches)} scored")
    print(f"Scored: {len(score_results)}/{len(records)}")

    # Merge
    WEIGHTS = {"b": 0.30, "i": 0.25, "r": 0.15, "d": 0.15, "t": 0.15}
    THRESHOLDS = {"零碳产业": 6.5, "AI与智能科技": 6.5, "通用技术": 7.0}

    merged = []
    for i, r in enumerate(records):
        cat_item = classify_results.get(i, {})
        score_item = score_results.get(i, {})
        category = cat_item.get("category", "不相关")
        score = 0
        scd = None
        aip = 0
        if category != "不相关" and score_item:
            b = score_item.get("b", 0)
            ind = score_item.get("i", 0)
            rar = score_item.get("r", 0)
            dat = score_item.get("d", 0)
            tim = score_item.get("t", 0)
            score = round(b*0.3 + ind*0.25 + rar*0.15 + dat*0.15 + tim*0.15, 1)
            scd = {"b": b, "i": ind, "r": rar, "d": dat, "t": tim}
            domain = category.split("-")[0] if "-" in category else category.split("/")[0]
            if score >= THRESHOLDS.get(domain, 7.0):
                aip = 1
        merged.append({
            "orig_idx": r["orig_idx"],
            "url": r.get("u", ""),
            "category": category,
            "tag": cat_item.get("tag", ""),
            "topic": cat_item.get("topic", ""),
            "score": score,
            "scd": scd,
            "aip": aip,
        })

    with open("/tmp/retry_results.json", "w") as f:
        json.dump(merged, f, ensure_ascii=False)

    scored_count = sum(1 for m in merged if m["score"] > 0)
    aip_count = sum(1 for m in merged if m["aip"])
    print(f"\n=== SUMMARY ===")
    print(f"Total: {len(merged)}")
    print(f"Relevant: {sum(1 for m in merged if m['category'] != '不相关')}")
    print(f"Scored: {scored_count}")
    print(f"AI精选: {aip_count}")

if __name__ == "__main__":
    main()
