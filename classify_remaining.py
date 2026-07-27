#!/usr/bin/env python3
"""Classify remaining 未分类 records"""
import json, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

LITE_PATH = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"

with open(LITE_PATH) as f:
    lite = json.load(f)

unclassified = [(i, r) for i, r in enumerate(lite) if r.get("c", "未分类") == "未分类"]
print(f"未分类: {len(unclassified)}")
if not unclassified:
    print("Nothing to do")
    exit(0)

PROMPT = """你是技术情报语义分类与标签标注专家。对以下每条情报同时完成分类和打标签。
分类优先级：零碳产业 > AI与智能科技 > 通用技术 > 不相关。
只输出JSON数组：[{"id":0,"category":"完整路径或'不相关'","tag":"标签","topic":"5字主题"}]
候选路径必须从以下中选择：
零碳产业/物质循环/资源处理/勘探技术, 零碳产业/物质循环/资源处理/开采技术, 零碳产业/物质循环/资源处理/尾矿处理, 零碳产业/物质循环/资源处理/资源回收, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/石油化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/煤化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/热化学过程/天然气化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/电化学过程/CO2RR, 零碳产业/物质循环/有机物（碳循环）/有机工业/电化学过程/有机电合成, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/农林, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/传统生物化工, 零碳产业/物质循环/有机物（碳循环）/有机工业/生物过程/合成生物学, 零碳产业/物质循环/有机物（碳循环）/CCUS, 零碳产业/物质循环/无机物/黑色金属/钢铁, 零碳产业/物质循环/无机物/黑色金属/其它黑色金属产业, 零碳产业/物质循环/无机物/有色金属/铝业, 零碳产业/物质循环/无机物/有色金属/铜业, 零碳产业/物质循环/无机物/有色金属/其它有色金属产业, 零碳产业/物质循环/无机物/非金属/水泥, 零碳产业/物质循环/无机物/非金属/玻璃, 零碳产业/物质循环/无机物/非金属/陶瓷, 零碳产业/物质循环/无机物/非金属/其它无机非金属, 零碳产业/能量循环/能源测/发电技术/火电, 零碳产业/能量循环/能源测/发电技术/水电, 零碳产业/能量循环/能源测/发电技术/光伏, 零碳产业/能量循环/能源测/发电技术/光热, 零碳产业/能量循环/能源测/发电技术/风电, 零碳产业/能量循环/能源测/发电技术/核电, 零碳产业/能量循环/能源测/发电技术/燃料电池, 零碳产业/能量循环/能源测/发电技术/其他发电技术, 零碳产业/能量循环/能源测/供热技术/热电联产, 零碳产业/能量循环/能源测/供热技术/热泵, 零碳产业/能量循环/能量存储/电化学储能/二次电池/锂电池, 零碳产业/能量循环/能量存储/电化学储能/二次电池/钠电池, 零碳产业/能量循环/能量存储/电化学储能/二次电池/其它电池体系, 零碳产业/能量循环/能量存储/电化学储能/一次电池, 零碳产业/能量循环/能量存储/电化学储能/超级电容器, 零碳产业/能量循环/能量存储/储热/熔盐储热, 零碳产业/能量循环/能量存储/储热/固态储热, 零碳产业/能量循环/能量存储/储热/压缩空气, 零碳产业/能量循环/能量存储/储热/水储热, 零碳产业/能量循环/能量存储/储热/其他储热技术, 零碳产业/能量循环/能量存储/化学能/氢基能源, 零碳产业/能量循环/能量存储/化学能/可再生燃料, 零碳产业/能量循环/能量存储/机械能/重力储能, 零碳产业/能量循环/能量存储/机械能/飞轮储能, 零碳产业/能量循环/能量存储/其它储能技术, AI与智能科技/AI软件层/底座大模型/文本模型, AI与智能科技/AI软件层/底座大模型/多模态模型, AI与智能科技/AI软件层/工程改进/工作流, AI与智能科技/AI软件层/工程改进/AGENT, AI与智能科技/AI硬件层/半导体, AI与智能科技/AI硬件层/芯片, AI与智能科技/AI硬件层/计算集群, AI与智能科技/AI硬件层/数据中心, AI与智能科技/其它智能科技/脑机接口, AI与智能科技/其它智能科技/量子信息, AI与智能科技/具身智能/模型和具身操作系统, AI与智能科技/具身智能/硬件和控制, AI与智能科技/具身智能/供能和换电生态, 通用技术/检测和表征/先进科学仪器, 通用技术/通信和运输/电网技术/孤网园区, 通用技术/通信和运输/电网技术/配电和电力交易, 通用技术/通信和运输/电网技术/其它电网技术, 通用技术/通信和运输/管网技术, 通用技术/通信和运输/航天, 通用技术/通信和运输/航空, 通用技术/通信和运输/陆路运输, 通用技术/通信和运输/水路运输, 通用技术/催化剂, 通用技术/材料工程/耐热材料, 通用技术/材料工程/强度材料, 通用技术/材料工程/密封材料, 通用技术/材料工程/耐腐蚀材料, 通用技术/材料工程/其它先进材料
待处理情报：
"""

items = [{"id": i, "type": "literature" if r.get("i")=="l" else "news", "title": r.get("t","")[:200], "body": r.get("b","")[:500]} for i, (_, r) in enumerate(unclassified)]
BATCH = 10
batches = [items[i:i+BATCH] for i in range(0, len(items), BATCH)]

def call(batch):
    p = PROMPT + json.dumps(batch, ensure_ascii=False)
    try:
        r = subprocess.run(["hermes","-z",p,"--provider","zai","-m","glm-5.2","--cli"], capture_output=True, text=True, timeout=180, cwd="/home/rhett")
        o = r.stdout.strip()
        s,e = o.find("["),o.rfind("]")
        if s>=0 and e>s: return json.loads(o[s:e+1])
    except: pass
    return None

results = {}
done = 0
with ThreadPoolExecutor(max_workers=6) as ex:
    futures = {ex.submit(call,b):b for b in batches}
    for f in as_completed(futures):
        try:
            r = f.result()
            if r:
                for item in r: results[item["id"]] = item
            done += 1
        except: pass
        if done%5==0: print(f"  {done}/{len(batches)}")

applied = 0
for bid, (idx, r) in enumerate(unclassified):
    if bid in results:
        res = results[bid]
        lite[idx]["c"] = res.get("category","未分类")
        if res.get("tag"): lite[idx]["tg"] = res["tag"]
        if res.get("topic"): lite[idx]["tp"] = res["topic"]
        applied += 1

still = sum(1 for r in lite if r.get("c","")=="未分类")
print(f"Applied: {applied}, Still 未分类: {still}")

with open(LITE_PATH,"w") as f:
    json.dump(lite,f,ensure_ascii=False,separators=(",",":"))

for i in range(18):
    s=i*3000; e=min(s+3000,len(lite))
    chunk=lite[s:e] if s<len(lite) else []
    c='window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push('+json.dumps(chunk,ensure_ascii=False,separators=(",",":"))+');'
    with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js","w") as f:
        f.write(c)
print("Done")
