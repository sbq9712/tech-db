#!/usr/bin/env python3
"""从完整 shard JSON 生成 all-records-lite.json（短键格式）。

运行 build_database.py + classify_and_tag.py + extract_params.py 之后，
需要运行此脚本来生成前端用的 lite 数据，然后运行 build_local.py 生成分片。

用法:
    python3 scripts/shard_to_lite.py
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARD_DIR = REPO / 'data' / 'processed'
LITE_PATH = SHARD_DIR / 'all-records-lite.json'

# body 截断长度（lite 不含全文）
BODY_TRUNCATE = 120

def truncate(s, n):
    if not s:
        return ''
    return s[:n] + ('...' if len(s) > n else '')

def make_lite_record(r):
    """把完整记录转为短键 lite 格式，省略空值。"""
    item = {'t': r.get('title', '')}
    
    body = r.get('body', '')
    item['b'] = truncate(body, BODY_TRUNCATE)
    
    d = r.get('date', '')
    if d:
        item['d'] = d
    
    i = r.get('intelligence_type', '')
    if i:
        item['i'] = i
    
    u = r.get('url', '')
    if u:
        item['u'] = u
    
    c = r.get('category', '')
    item['c'] = c or '未分类'
    
    a = r.get('authors', '')
    if a:
        item['a'] = a
    
    tg = r.get('tag', '')
    if tg:
        item['tg'] = tg
    
    tp = r.get('topic', '')
    if tp:
        item['tp'] = tp
    
    kp = r.get('key_params', [])
    if kp:
        item['kp'] = kp
    
    fb = r.get('full_body', '') or body
    if fb and len(fb) > BODY_TRUNCATE:
        item['fb'] = fb
    
    cm = r.get('comment', '')
    if cm:
        item['cm'] = cm
    
    lv = r.get('lv', 0)
    if lv:
        item['lv'] = lv
    
    dp = r.get('dp', 0)
    if dp:
        item['dp'] = dp
    
    sc = r.get('score')
    if sc is not None:
        item['sc'] = sc
    
    scd = r.get('score_detail')
    if scd:
        item['scd'] = scd
    
    aip = r.get('aip', 0)
    if aip:
        item['aip'] = aip
    
    ai_summary = r.get('ai_summary', '') or r.get('as', '')
    if ai_summary:
        item['as'] = ai_summary
    
    return item

def main():
    shards = sorted(SHARD_DIR.glob('records-*.json'))
    if not shards:
        print(f"错误：找不到 records-*.json 文件")
        return 1
    
    all_lite = []
    for shard_path in shards:
        data = json.loads(shard_path.read_text('utf-8'))
        for r in data.get('records', []):
            all_lite.append(make_lite_record(r))
    
    # 按日期降序排列
    all_lite.sort(key=lambda x: x.get('d', ''), reverse=True)
    
    LITE_PATH.write_text(
        json.dumps(all_lite, ensure_ascii=False, separators=(',', ':')),
        'utf-8'
    )
    
    size_mb = LITE_PATH.stat().st_size / 1024 / 1024
    print(f"生成 {len(all_lite)} 条记录 → {LITE_PATH.name}")
    print(f"文件大小: {size_mb:.1f} MB")
    
    # 统计
    classified = sum(1 for r in all_lite if r.get('c') not in ['未分类', '不相关', ''])
    unrelated = sum(1 for r in all_lite if r.get('c') == '不相关')
    unclassified = sum(1 for r in all_lite if r.get('c') in ['未分类', ''])
    has_lv = sum(1 for r in all_lite if r.get('lv', 0) > 0)
    has_as = sum(1 for r in all_lite if r.get('as'))
    print(f"已分类(相关): {classified} | 不相关: {unrelated} | 未分类: {unclassified}")
    print(f"有lv: {has_lv} | 有AI摘要: {has_as}")
    
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
