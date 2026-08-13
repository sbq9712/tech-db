#!/usr/bin/env python3
"""Restore source from backup, enrich ALL curated records missing kp, rebuild
all-records-lite.json byte-exactly, and rebuild the 27x3 shard files.

Phase A: stream-parse backup, verify reconstruction is byte-identical (safety proof)
Phase B: LLM-enrich curated records missing kp (report values reused where available)
Phase C: rewrite source + all shards; validate counts
"""
import json, sys, os, re, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from llm_client import call_glm  # noqa: E402

KP_PROMPT_TEMPLATE = """作为顶级情报分析专家，请基于深层语义理解提取输入文本中的关键技术情报。

【提取规则】
1. 有明确量化参数的，格式为：参数名[核心条件]: 参数值
2. 有明确属性但不可量化的，格式为：参数名[核心条件]: 定性特征
3. 无明确参数名但有关键技术状态/工艺特点/结论的，格式为：[核心条件]: 关键特征陈述
4. 如无任何关键技术参数可提取，返回空数组

只输出JSON数组：
[{{"key_params":["参数名[条件]: 值","..."]}}]

待处理情报：
标题：{title}
正文：{body}
分类：{category}"""

def parse_kp_response(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').replace('json\n', '', 1).strip()
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and arr:
                item = arr[0]
                if isinstance(item, dict) and 'key_params' in item:
                    return [str(p).strip() for p in item['key_params'] if str(p).strip()][:5]
                elif isinstance(item, str):
                    return [item]
        except json.JSONDecodeError:
            pass
    return []

S = (',', ':')
def dump(o): return json.dumps(o, ensure_ascii=False, separators=S)

BAK = REPO / 'data/processed/all-records-lite.pipeline-20260813-2015.json.bak'
OUT = REPO / 'data/processed/all-records-lite.json'
REPORT = REPO / 'runtime/kp_enrichment_full.json'

prev = {}
for rp in (REPO / 'runtime/kp_enrichment_full.json', REPO / 'runtime/delta_kp_enrichment.json'):
    if rp.exists():
        for x in json.loads(rp.read_text()):
            prev[x['index']] = x

src_str = BAK.read_text()
dec = json.JSONDecoder()
spans = []  # (start, end) per record
pos, n = 1, len(src_str)
while pos < n:
    while pos < n and src_str[pos] in ' \n\t\r,': pos += 1
    if pos >= n or src_str[pos] == ']': break
    obj, end = dec.raw_decode(src_str, pos)
    spans.append((pos, end))
    pos = end
print(f"[A] backup records: {len(spans)}", flush=True)
assert len(spans) == 52412

# Phase A: verify byte-identical reconstruction via join
parts = [src_str[s:e] for s, e in spans]
rebuilt = '[' + ','.join(parts) + ']'
assert rebuilt == src_str, "reconstruction mismatch!"
print("[A] byte-identical reconstruction verified", flush=True)
del parts, rebuilt

# find targets: curated (aip or lv>=1) and no kp
targets = {}
for i, (s, e) in enumerate(spans):
    head = src_str[s:e]
    if '"kp":' in head[:400] or '"kp":' in head[-400:]:
        # crude presence check; refine by parsing only curated candidates
        pass
# parse candidates: check aip/lv via lightweight heuristics then full parse
cand = []
for i, (s, e) in enumerate(spans):
    seg = src_str[s:e]
    if ('"aip":1' in seg or '"lv":' in seg) and '"kp":' not in seg:
        cand.append(i)
print(f"[B] candidate no-kp curated: {len(cand)}", flush=True)

# full-parse candidates to confirm curated & no-kp; apply previous report values
objs = {}
to_llm = []
for i in cand:
    o = json.loads(src_str[spans[i][0]:spans[i][1]])
    if not (o.get('aip') or o.get('lv', 0) >= 1):
        continue
    if o.get('kp'):
        continue
    objs[i] = o
    if i in prev:
        if prev[i].get('kp'):
            o['kp'] = prev[i]['kp']
    else:
        to_llm.append(i)
print(f"[B] confirmed targets: {len(objs)} | reuse-from-report: {len(objs)-len(to_llm)} | need-LLM: {len(to_llm)}", flush=True)

def llm_one(i):
    o = objs[i]
    prompt = KP_PROMPT_TEMPLATE.format(
        title=o.get('t', '')[:200],
        body=(o.get('b', '') or o.get('fb', '') or '')[:2000],
        category=o.get('c', '') or o.get('tg', ''))
    for attempt in range(3):
        try:
            kp = parse_kp_response(call_glm(prompt, timeout=90))
            return i, [p for p in kp if ':' in p and 3 <= len(p) <= 200]
        except Exception as ex:
            time.sleep(3)
    return i, None

if to_llm:
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, kp in ex.map(llm_one, to_llm):
            done += 1
            if kp:
                objs[i]['kp'] = kp
            if done % 20 == 0:
                print(f"  [B] {done}/{len(to_llm)} LLM calls done", flush=True)

report = []
for i in sorted(objs):
    o = objs[i]
    report.append({"index": i, "title": o.get('t', '')[:60], "kp": o.get('kp') or [], "status": "ok" if o.get('kp') else "empty"})
REPORT.parent.mkdir(exist_ok=True)
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
n_ok = sum(1 for r in report if r['kp'])
print(f"[B] enriched={n_ok}, empty={len(report)-n_ok}", flush=True)

# Phase C: rewrite source (string slices only — memory-safe)
out_parts = []
for i, (s, e) in enumerate(spans):
    if i in objs and objs[i].get('kp'):
        out_parts.append(dump(objs[i]))
    else:
        out_parts.append(src_str[s:e])
new_src = '[' + ','.join(out_parts) + ']'
del out_parts

# validity via streaming count (no full-object load)
_cnt = 0
_p = 1
while _p < len(new_src):
    while _p < len(new_src) and new_src[_p] in ' \n\t\r,': _p += 1
    if new_src[_p] == ']': break
    assert new_src[_p] == '{', f"non-dict element at {_cnt}"
    _, _p = dec.raw_decode(new_src, _p)
    _cnt += 1
assert _cnt == 52412, f"record count {_cnt} != 52412"
tmp = OUT.with_suffix('.json.tmp')
tmp.write_text(new_src, 'utf-8')
os.replace(tmp, OUT)
print(f"[C] source rewritten: {_cnt} records, {len(new_src)} chars", flush=True)
del new_src

# Phase C: rebuild ALL shards — stream one part at a time (memory-safe)
CH = 2000
SKIP = (None, '', [])
META_KEYS = {'t','d','u','c','a','i','source','sr','lv','cm','wr','tg','tp','sc','aip','kp','fb'}

def get_record(i):
    if i in objs:
        return objs[i]
    s, e = spans[i]
    return json.loads(src_str[s:e])

def build_meta(r):
    m = {}
    for k in r:
        if k in META_KEYS and r[k] not in SKIP:
            m[k] = r[k]
    m['hb'] = 1 if r.get('b') else 0
    return m

d = REPO / 'data/processed'
cnt_cur = cnt_kp = 0
for p in range(27):
    lo, hi = p*CH, min((p+1)*CH, 52412)
    chunk = [get_record(i) for i in range(lo, hi)]
    lite = "window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push(" + dump(chunk) + ");"
    meta = "window.__META_PARTS__=window.__META_PARTS__||[];window.__META_PARTS__.push(" + dump([build_meta(r) for r in chunk]) + ");"
    summ = "window.__SUMMARY_PARTS__=window.__SUMMARY_PARTS__||[];window.__SUMMARY_PARTS__.push(" + dump(
        [{'i': lo+j, 'as': (r.get('as') or ''), 'scd': r.get('scd'), 'kp': (r.get('kp') or [])}
         for j, r in enumerate(chunk)]) + ");"
    for r in chunk:
        if r.get('aip') or r.get('lv', 0) >= 1:
            cnt_cur += 1
            if r.get('kp'): cnt_kp += 1
    texts = {
        'lite-part-%d.js': lite,
        'meta-part-%d.js': meta,
        'summary-part-%d.js': summ,
    }
    for name_tpl, text in texts.items():
        f = d / (name_tpl % p)
        t = f.with_suffix('.js.tmp')
        t.write_text(text, 'utf-8')
        os.replace(t, f)
    del chunk, lite, meta, summ, texts
    print(f"  [C] part {p} rebuilt ({hi-lo} records)", flush=True)
print("[C] all 27x3 shards rebuilt", flush=True)
print(f"[V] curated={cnt_cur} curated-with-kp={cnt_kp}", flush=True)
