#!/usr/bin/env python3
"""一次跑完所有欠债：分类 → 评分 → 摘要"""
import json, subprocess, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed

LITE_PATH = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
SAVE_INTERVAL = 30

def save(data):
    with open(LITE_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",",":"))
    for i in range(18):
        s=i*3000; e=min(s+3000,len(data))
        chunk=data[s:e] if s<len(data) else []
        c='window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push('+json.dumps(chunk,ensure_ascii=False,separators=(",",":"))+');'
        with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js","w") as f:
            f.write(c)

def call_glm(prompt, timeout=180):
    try:
        r = subprocess.run(["hermes","-z",prompt,"--provider","zai","-m","glm-5.2","--cli"],
                          capture_output=True, text=True, timeout=timeout, cwd="/home/rhett")
        out = r.stdout.strip()
        if out.startswith('```'):
            lines = out.split('\n')
            out = '\n'.join(lines[1:])
            if out.endswith('```'): out = out[:-3].strip()
            if out.startswith('json'): out = out[4:].strip()
        import re
        m = re.search(r'[\[{].*[\]}]', out, re.S)
        if m: return m.group(0)
    except: pass
    return None

with open(LITE_PATH) as f:
    lite = json.load(f)

# ===== 1. 分类未分类 =====
unclassified = [(i, r) for i, r in enumerate(lite) if r.get("c","") == "未分类"]
print(f"Step 1: 分类 {len(unclassified)} 条未分类", flush=True)

if unclassified:
    CLASS_PROMPT = """你是技术情报语义分类与标签标注专家。对以下每条情报同时完成分类和打标签。
只输出JSON数组：[{"id":0,"category":"完整路径或'不相关'","tag":"标签","topic":"5字主题"}]
候选路径：
零碳产业/物质循环/资源处理/勘探技术, 零碳产业/物质循环/资源处理/开采技术, 零碳产业/物质循环/资源处理/尾矿处理, 零碳产业/物质循环/资源处理/资源回收, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/石油化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/煤化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/天然气化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/电化学过程/CO2RR, 零碳产业/物质循环/有机物（碳循环）/有机工业/电化学过程/有机电合成, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/农林, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/传统生物化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/合成生物学, 零碳产业/物质循环/有机物（碳循环）/CCUS, 零碳产业/物质循环/无机物/黑色金属/钢铁, 零碳产业/物质循环/无机物/黑色金属/其它黑色金属产业, 零碳产业/物质循环/无机物/有色金属/铝业, 零碳产业/物质循环/无机物/有色金属/铜业, 零碳产业/物质循环/无机物/有色金属/其它有色金属产业, 零碳产业/物质循环/无机物/非金属/水泥, 零碳产业/物质循环/无机物/非金属/玻璃, 零碳产业/物质循环/无机物/非金属/陶瓷, 零碳产业/物质循环/无机物/非金属/其它无机非金属, 零碳产业/能量循环/能源测/发电技术/火电, 零碳产业/能量循环/能源测/发电技术/水电, 零碳产业/能量循环/能源测/发电技术/光伏, 零碳产业/能量循环/能源测/发电技术/光热, 零碳产业/能量循环/能源测/发电技术/风电, 零碳产业/能量循环/能源测/发电技术/核电, 零碳产业/能量循环/能源测/发电技术/燃料电池, 零碳产业/能量循环/能源测/发电技术/其他发电技术, 零碳产业/能量循环/能源测/供热技术/热电联产, 零碳产业/能量循环/能源测/供热技术/热泵, 零碳产业/能量循环/能量存储/电化学储能/二次电池/锂电池, 零碳产业/能量循环/能量存储/电化学储能/二次电池/钠电池, 零碳产业/能量循环/能量存储/电化学储能/二次电池/其它电池体系, 零碳产业/能量循环/能量存储/电化学储能/一次电池, 零碳产业/能量循环/能量存储/电化学储能/超级电容器, 零碳产业/能量循环/能量存储/储热/熔盐储热, 零碳产业/能量循环/能量存储/储热/固态储热, 零碳产业/能量循环/能量存储/储热/压缩空气, 零碳产业/能量循环/能量存储/储热/水储热, 零碳产业/能量循环/能量存储/储热/其他储热技术, 零碳产业/能量循环/能量存储/化学能/氢基能源, 零碳产业/能量循环/能量存储/化学能/可再生燃料, 零碳产业/能量循环/能量存储/机械能/重力储能, 零碳产业/能量循环/能量存储/机械能/飞轮储能, 零碳产业/能量循环/能量存储/其它储能技术, AI与智能科技/AI软件层/底座大模型/文本模型, AI与智能科技/AI软件层/底座大模型/多模态模型, AI与智能科技/AI软件层/工程改进/工作流, AI与智能科技/AI软件层/工程改进/AGENT, AI与智能科技/AI硬件层/半导体, AI与智能科技/AI硬件层/芯片, AI与智能科技/AI硬件层/计算集群, AI与智能科技/AI硬件层/数据中心, AI与智能科技/其它智能科技/脑机接口, AI与智能科技/其它智能科技/量子信息, AI与智能科技/具身智能/模型和具身操作系统, AI与智能科技/具身智能/硬件和控制, AI与智能科技/具身智能/供能和换电生态, 通用技术/检测和表征/先进科学仪器, 通用技术/通信和运输/电网技术/孤网园区, 通用技术/通信和运输/电网技术/配电和电力交易, 通用技术/通信和运输/电网技术/其它电网技术, 通用技术/通信和运输/管网技术, 通用技术/通信和运输/航天, 通用技术/通信和运输/航空, 通用技术/通信和运输/陆路运输, 通用技术/通信和运输/水路运输, 通用技术/催化剂, 通用技术/材料工程/耐热材料, 通用技术/材料工程/强度材料, 通用技术/材料工程/密封材料, 通用技术/材料工程/耐腐蚀材料, 通用技术/材料工程/其它先进材料
待处理情报：
"""
    items = [{"id":i, "type":"literature" if lite[idx].get("i")=="l" else "news", "title":lite[idx].get("t","")[:200], "body":lite[idx].get("b","")[:500]} for i, (idx, _) in enumerate(unclassified)]
    BATCH = 10
    batches = [items[i:i+BATCH] for i in range(0, len(items), BATCH)]
    
    def run_batch(batch):
        p = CLASS_PROMPT + json.dumps(batch, ensure_ascii=False)
        raw = call_glm(p)
        if raw:
            import json as j
            try: return j.loads(raw)
            except: pass
        return []
    
    done = 0
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(run_batch, b): b for b in batches}
        for f in as_completed(futures):
            try:
                for item in f.result():
                    results[item["id"]] = item
            except: pass
            done += 1
            if done % 5 == 0: print(f"  分类 {done}/{len(batches)}", flush=True)
    
    applied = 0
    for bid, (idx, _) in enumerate(unclassified):
        if bid in results:
            r = results[bid]
            lite[idx]["c"] = r.get("category","未分类")
            if r.get("tag"): lite[idx]["tg"] = r["tag"]
            if r.get("topic"): lite[idx]["tp"] = r["topic"]
            applied += 1
    print(f"  分类完成: {applied}/{len(unclassified)}", flush=True)
    save(lite)

# ===== 2. 评分（缺评分的非不相关记录） =====
need_score = [(i, r) for i, r in enumerate(lite) if r.get("sc",0) == 0 and r.get("c","") not in ("不相关","未分类","")]
print(f"\nStep 2: 评分 {len(need_score)} 条", flush=True)

if need_score:
    SCORE_PROMPT = """对以下每条情报打5个维度分数（0-10分）。
1.breakthrough:纯政策/市场=0;渐进改进=5;新机理/新材料=10
2.industry:实验室概念=1;小规模验证=5;量产落地=10
3.rarity:转载旧闻=0;常规跟踪=5;独家首发=10
4.data:纯定性=0;定性+参数=5;多硬数据=10
5.timeliness:趋势综述=2;近期进展=6;突发=10
只输出JSON数组：[{"id":0,"b":7.5,"i":6.0,"r":5.0,"d":8.0,"t":7.0}]
待评估情报：
"""
    items = [{"id":i, "title":lite[idx].get("t","")[:200], "body":lite[idx].get("b","")[:500]} for i, (idx, _) in enumerate(need_score)]
    BATCH = 10
    batches = [items[i:i+BATCH] for i in range(0, len(items), BATCH)]
    
    def run_score(batch):
        p = SCORE_PROMPT + json.dumps(batch, ensure_ascii=False)
        raw = call_glm(p)
        if raw:
            import json as j
            try: return j.loads(raw)
            except: pass
        return []
    
    done = 0
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(run_score, b): b for b in batches}
        for f in as_completed(futures):
            try:
                for item in f.result():
                    results[item["id"]] = item
            except: pass
            done += 1
            if done % 3 == 0: print(f"  评分 {done}/{len(batches)}", flush=True)
    
    THRESHOLDS = {"零碳产业": 6.3, "AI与智能科技": 6.5, "通用技术": 6.8}
    BOOST_TAGS = {"政策监管", "行业观察"}
    applied = 0
    for bid, (idx, r) in enumerate(need_score):
        if bid not in results: continue
        sc = results[bid]
        b,i,rr,d,t = sc.get("b",0),sc.get("i",0),sc.get("r",0),sc.get("d",0),sc.get("t",0)
        tag = r.get("tg","")
        is_lit = r.get("i") == "l"
        score = b*0.15 + i*0.20 + rr*0.25 + d*0.10 + t*0.30
        if t >= 8: score += 0.3
        elif t >= 7: score += 0.15
        if tag in BOOST_TAGS: score += 0.5
        if b >= 7: score += 0.4
        if rr >= 7: score += 0.3
        if i >= 7: score += 0.3
        if is_lit: score -= 0.4
        score = round(score, 1)
        lite[idx]["sc"] = score
        lite[idx]["scd"] = {"b":b,"i":i,"r":rr,"d":d,"t":t}
        domain = r.get("c","").split("/")[0]
        threshold = THRESHOLDS.get(domain, 6.8)
        aip = 1 if score >= threshold else 0
        if not aip:
            max_dim = max(b,i,rr,d,t)
            if is_lit and max_dim >= 9.0: aip = 1
            elif not is_lit and max_dim >= 8: aip = 1
        if not aip and not is_lit and tag == "技术突破" and b >= 6.5 and score >= 5.5: aip = 1
        if aip: lite[idx]["aip"] = 1
        applied += 1
    print(f"  评分完成: {applied}/{len(need_score)}", flush=True)
    save(lite)

# ===== 3. AI摘要（缺摘要的记录） =====
need_summary = [(i, r) for i, r in enumerate(lite) if not r.get("as","").strip()]
print(f"\nStep 3: 摘要 {len(need_summary)} 条", flush=True)

if need_summary:
    SUMMARY_PROMPT = """你是技术情报摘要专家。为以下每条情报生成100-200字的中文AI摘要。
提炼核心技术内容、关键数据指标、主要结论。正文为空时基于标题生成简要摘要。
只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""
    items = [{"id":i, "title":lite[idx].get("t","")[:200], "body":lite[idx].get("b","")[:500]} for i, (idx, _) in enumerate(need_summary)]
    BATCH = 20
    batches = [items[i:i+BATCH] for i in range(0, len(items), BATCH)]
    
    def run_summary(batch):
        p = SUMMARY_PROMPT + json.dumps(batch, ensure_ascii=False)
        raw = call_glm(p)
        if raw:
            import json as j
            try: return j.loads(raw)
            except: pass
        return []
    
    done = 0
    last_save = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(run_summary, b): b for b in batches}
        for f in as_completed(futures):
            try:
                for item in f.result():
                    results[item["id"]] = item.get("summary","")
            except: pass
            done += 1
            if done % 5 == 0:
                print(f"  摘要 {done}/{len(batches)} ({len(results)}条)", flush=True)
            if time.time() - last_save > SAVE_INTERVAL:
                for bid, s in results.items():
                    if bid < len(need_summary):
                        lite[need_summary[bid][0]]["as"] = s
                save(lite)
                last_save = time.time()
    
    for bid, s in results.items():
        if bid < len(need_summary):
            lite[need_summary[bid][0]]["as"] = s
    print(f"  摘要完成: {len(results)}/{len(need_summary)}", flush=True)
    save(lite)

# Final stats
no_s = sum(1 for r in lite if not r.get("as","").strip())
no_sc = sum(1 for r in lite if r.get("sc",0) == 0 and r.get("c","") not in ("不相关","未分类",""))
unc = sum(1 for r in lite if r.get("c","") == "未分类")
print(f"\n=== 完成 ===")
print(f"剩余缺摘要: {no_s}")
print(f"剩余缺评分: {no_sc}")
print(f"剩余未分类: {unc}")
