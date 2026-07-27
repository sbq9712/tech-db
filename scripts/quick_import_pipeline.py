#!/usr/bin/env python3
"""Quick pipeline for imported records: classify → score → summarize → rebuild → push."""
import json, subprocess, time, re, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

REPO = "/home/rhett/tech-db-fresh"
LITE_PATH = f"{REPO}/data/processed/all-records-lite.json"
DATA_DIR = f"{REPO}/data/processed"
MODEL = 'glm-5.2'
PROVIDER = 'zai'
BATCH = 10
WORKERS = 6

def log(msg):
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def call_glm(prompt, timeout=90):
    cmd = ['hermes', '--provider', PROVIDER, '-m', MODEL, '-z', prompt, '--cli']
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        output = (r.stdout or '').strip()
        s = output.find('[')
        e = output.rfind(']')
        if s >= 0 and e > s:
            return json.loads(output[s:e+1])
    except:
        pass
    return None

CLASSIFY_PROMPT = """你是技术情报语义分类与标签标注专家。对以下每条情报同时完成分类和打标签。
分类优先级：零碳产业 > AI与智能科技 > 通用技术 > 不相关。
只输出JSON数组：[{"id":0,"category":"完整路径或'不相关'","tag":"标签","topic":"5字主题"}]
标签规则：新闻→技术突破/产业进展/政策监管/资本运作/行业观察；文献→研究论文/观点评论
待处理情报：
"""

SCORE_PROMPT = """对以下每条情报打5个维度分数（0-10分）。
1. breakthrough: 纯政策/市场=0；渐进改进=5；新机理/新材料=10
2. industry: 实验室概念=1；小规模验证=5；量产落地=10
3. rarity: 转载旧闻=0；常规跟踪=5；独家首发=10
4. data: 纯定性=0；定性+参数=5；多硬数据=10
5. timeliness: 趋势综述=2；近期进展=6；突发=10
只输出JSON数组：[{"id":0,"b":7.5,"i":6.0,"r":5.0,"d":8.0,"t":7.0}]
待评估情报（跳过不相关）：
"""

SUMMARY_PROMPT = """你是技术情报摘要专家。为以下每条情报生成100-200字的中文AI摘要。
只输出JSON数组：[{"id":0,"summary":"..."}]
待处理情报：
"""

NEWS_TAGS = {'技术突破', '产业进展', '政策监管', '资本运作', '行业观察'}
LIT_TAGS = {'研究论文', '观点评论'}
THRESHOLDS = {"零碳产业": 6.3, "AI与智能科技": 6.5, "通用技术": 6.8}
BOOST_TAGS = {"政策监管", "行业观察"}

def norm_cat(c):
    return (c or '').replace('/', '-').replace('（', '').replace('）', '').replace('(', '').replace(')', '')

def save_and_rebuild(data):
    with open(LITE_PATH, 'w') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    for i in range(20):
        start = i * 3000
        end = min(start + 3000, len(data))
        chunk = data[start:end] if start < len(data) else []
        content = f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(chunk, ensure_ascii=False, separators=(",",":"))});'
        with open(os.path.join(DATA_DIR, f"lite-part-{i}.js"), "w") as f:
            f.write(content)
    # Update manifest
    manifest_path = os.path.join(DATA_DIR, "manifest-data.js")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            mc = f.read()
        idx = mc.find(';')
        ms = mc[len("window.__MANIFEST__="):idx]
        manifest = json.loads(ms)
        manifest['meta']['records_total'] = len(data)
        with open(manifest_path, 'w') as f:
            f.write("window.__MANIFEST__=" + json.dumps(manifest, ensure_ascii=False, separators=(',', ':')) + ";")
    log(f"  Saved + rebuilt: {len(data)} records")

def main():
    with open(LITE_PATH) as f:
        data = json.load(f)
    
    # === Stage 1: Classify ===
    todo_cls = [(i, r) for i, r in enumerate(data) if r.get('c') == '未分类']
    log(f"=== Stage 1: Classify {len(todo_cls)} records ===")
    
    if todo_cls:
        batches = [todo_cls[i:i+BATCH] for i in range(0, len(todo_cls), BATCH)]
        
        def process_cls(batch_idx, batch_items):
            items = [{"id": j, "type": "literature" if r.get('i')=='l' else "news",
                      "title": (r.get('t','') or '')[:200], "body": (r.get('fb','') or r.get('b','') or '')[:500]}
                     for j, (idx, r) in enumerate(batch_items)]
            raw = call_glm(CLASSIFY_PROMPT + json.dumps(items, ensure_ascii=False))
            if not raw: return []
            results = []
            for r in raw:
                lid = r.get('id')
                if lid is None or lid < 0 or lid >= len(batch_items): continue
                gi = batch_items[lid][0]
                cat = norm_cat(r.get('category','').strip())
                tag = r.get('tag','').strip()
                topic = r.get('topic','').strip()
                rt = data[gi].get('i','n')
                vt = NEWS_TAGS if rt == 'n' else LIT_TAGS
                if tag not in vt: tag = ''
                results.append((gi, {'c': cat or '不相关', 'tg': tag, 'tp': topic[:15] if topic else ''}))
            return results
        
        done = 0
        WAVE = WORKERS
        for ws in range(0, len(batches), WAVE):
            wave = batches[ws:ws+WAVE]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(process_cls, bi, b): bi for bi, b in enumerate(wave)}
                for f in as_completed(futures):
                    for idx, fields in f.result():
                        data[idx].update(fields)
                        done += 1
            elapsed_total = time.time()
            if (ws // WAVE + 1) % 3 == 0:
                save_and_rebuild(data)
                log(f"  Classified: {done}/{len(todo_cls)}")
        
        save_and_rebuild(data)
        log(f"  Classification complete: {done}/{len(todo_cls)}")
    
    # === Stage 2: Score ===
    todo_sc = [(i, r) for i, r in enumerate(data) 
               if r.get('c') not in ('不相关', '未分类', '') and not r.get('sc')]
    log(f"=== Stage 2: Score {len(todo_sc)} records ===")
    
    if todo_sc:
        batches = [todo_sc[i:i+BATCH] for i in range(0, len(todo_sc), BATCH)]
        
        def process_sc(batch_idx, batch_items):
            items = [{"id": j, "title": (r.get('t','') or '')[:200], 
                      "body": (r.get('fb','') or r.get('b','') or '')[:500], "category": r.get('c','')}
                     for j, (idx, r) in enumerate(batch_items)]
            raw = call_glm(SCORE_PROMPT + json.dumps(items, ensure_ascii=False))
            if not raw: return []
            results = []
            for r in raw:
                lid = r.get('id')
                if lid is None or lid < 0 or lid >= len(batch_items): continue
                gi = batch_items[lid][0]
                b, i_, rr, d, t = r.get('b',0), r.get('i',0), r.get('r',0), r.get('d',0), r.get('t',0)
                tag = data[gi].get('tg','')
                is_lit = data[gi].get('i') == 'l'
                score = b*0.15 + i_*0.20 + rr*0.25 + d*0.10 + t*0.30
                if t >= 8: score += 0.3
                elif t >= 7: score += 0.15
                if tag in BOOST_TAGS: score += 0.5
                if b >= 7: score += 0.4
                if rr >= 7: score += 0.3
                if i_ >= 7: score += 0.3
                if is_lit: score -= 0.4
                score = round(score, 1)
                domain = data[gi].get('c','').split('-')[0]
                threshold = THRESHOLDS.get(domain, 6.8)
                aip = 1 if score >= threshold else 0
                if not aip:
                    max_dim = max(b,i_,rr,d,t)
                    if is_lit and max_dim >= 9.0: aip = 1
                    elif not is_lit and max_dim >= 8: aip = 1
                if not aip and not is_lit and tag == '技术突破' and b >= 6.5 and score >= 5.5: aip = 1
                results.append((gi, {'sc': score, 'scd': {'b':b,'i':i_,'r':rr,'d':d,'t':t}, 'aip': aip}))
            return results
        
        done = 0
        for ws in range(0, len(batches), WORKERS):
            wave = batches[ws:ws+WORKERS]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(process_sc, bi, b): bi for bi, b in enumerate(wave)}
                for f in as_completed(futures):
                    for idx, fields in f.result():
                        data[idx].update(fields)
                        done += 1
            if (ws // WORKERS + 1) % 3 == 0:
                save_and_rebuild(data)
                log(f"  Scored: {done}/{len(todo_sc)}")
        
        save_and_rebuild(data)
        log(f"  Scoring complete: {done}/{len(todo_sc)}")
    
    # === Stage 3: AI Summaries ===
    todo_sm = [(i, r) for i, r in enumerate(data) if not r.get('as') and (r.get('fb','') or r.get('b',''))]
    log(f"=== Stage 3: Summarize {len(todo_sm)} records ===")
    
    if todo_sm:
        SBatch = 20
        batches = [todo_sm[i:i+SBatch] for i in range(0, len(todo_sm), SBatch)]
        
        def process_sm(batch_idx, batch_items):
            items = [{"id": j, "title": (r.get('t','') or '')[:200], 
                      "body": (r.get('fb','') or r.get('b','') or '')[:500]}
                     for j, (idx, r) in enumerate(batch_items)]
            raw = call_glm(SUMMARY_PROMPT + json.dumps(items, ensure_ascii=False), timeout=120)
            if not raw: return []
            results = []
            for r in raw:
                lid = r.get('id')
                if lid is None or lid < 0 or lid >= len(batch_items): continue
                gi = batch_items[lid][0]
                s = r.get('summary','').strip()
                if s and len(s) > 20:
                    results.append((gi, {'as': s}))
            return results
        
        done = 0
        for ws in range(0, len(batches), WORKERS):
            wave = batches[ws:ws+WORKERS]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(process_sm, bi, b): bi for bi, b in enumerate(wave)}
                for f in as_completed(futures):
                    for idx, fields in f.result():
                        data[idx].update(fields)
                        done += 1
            if (ws // WORKERS + 1) % 2 == 0:
                save_and_rebuild(data)
                log(f"  Summarized: {done}/{len(todo_sm)}")
        
        save_and_rebuild(data)
        log(f"  Summary complete: {done}/{len(todo_sm)}")
    
    # === Final stats ===
    log(f"\n=== Final Stats ===")
    log(f"Total records: {len(data)}")
    log(f"Unclassified: {sum(1 for r in data if r.get('c') == '未分类')}")
    log(f"No score: {sum(1 for r in data if not r.get('sc'))}")
    log(f"No summary: {sum(1 for r in data if not r.get('as'))}")
    log(f"lv=1 (精选): {sum(1 for r in data if r.get('lv') == 1)}")
    log(f"lv=2 (重点): {sum(1 for r in data if r.get('lv') == 2)}")
    log(f"lv=3 (预警): {sum(1 for r in data if r.get('lv') == 3)}")
    log(f"Done!")

if __name__ == '__main__':
    main()
