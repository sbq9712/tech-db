#!/usr/bin/env python3
"""增量合并：从 shard JSON 更新 lite JSON。

保留旧 lite 中已有的 lv/as/fb/cm/sc/scd/aip/dp 等字段，
从 shard JSON 合并新的 category/tag/topic/key_params，
为新增记录生成 lite 格式条目。

用法:
    python3 scripts/merge_shard_to_lite.py
"""
import json, glob
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARD_DIR = REPO / 'data' / 'processed'
LITE_PATH = SHARD_DIR / 'all-records-lite.json'

BODY_TRUNCATE = 120

def truncate(s, n):
    if not s:
        return ''
    return s[:n] + ('...' if len(s) > n else '')

def make_lite_record(r):
    """从 shard 完整记录生成 lite 格式。"""
    item = {'t': r.get('title', '')}
    body = r.get('body', '') or ''
    item['b'] = truncate(body, BODY_TRUNCATE)
    
    if r.get('date'): item['d'] = r['date']
    if r.get('intelligence_type'): item['i'] = r['intelligence_type']
    if r.get('url'): item['u'] = r['url']
    item['c'] = r.get('category', '') or '未分类'
    if r.get('authors'): item['a'] = r['authors']
    if r.get('tag'): item['tg'] = r['tag']
    if r.get('topic'): item['tp'] = r['topic']
    if r.get('key_params'): item['kp'] = r['key_params']
    if body and len(body) > BODY_TRUNCATE: item['fb'] = body
    if r.get('comment'): item['cm'] = r['comment']
    lv = r.get('lv', 0)
    if lv: item['lv'] = lv
    dp = r.get('dp', 0)
    if dp: item['dp'] = dp
    sc = r.get('score')
    if sc is not None: item['sc'] = sc
    scd = r.get('score_detail')
    if scd: item['scd'] = scd
    aip = r.get('aip', 0)
    if aip: item['aip'] = aip
    ai_summary = r.get('ai_summary', '') or r.get('as', '')
    if ai_summary: item['as'] = ai_summary
    
    return item

def update_lite_from_shard(lite_rec, shard_rec):
    """用 shard 的分类/标签信息更新 lite 记录，保留 lite 特有字段。"""
    # 更新分类相关字段
    lite_rec['c'] = shard_rec.get('category', '') or '未分类'
    
    if shard_rec.get('tag'):
        lite_rec['tg'] = shard_rec['tag']
    if shard_rec.get('topic'):
        lite_rec['tp'] = shard_rec['topic']
    if shard_rec.get('key_params'):
        lite_rec['kp'] = shard_rec['key_params']
    
    # 更新类型判断
    if shard_rec.get('intelligence_type'):
        lite_rec['i'] = shard_rec['intelligence_type']
    
    return lite_rec

def main():
    # 加载旧 lite
    old_lite = json.loads(LITE_PATH.read_text('utf-8'))
    print(f"旧 lite: {len(old_lite)} 条")
    
    # url+title+date 做匹配 key（唯一）
    old_map = {}
    for r in old_lite:
        key = (r.get('u', ''), r.get('t', ''), r.get('d', ''))
        old_map[key] = r
    
    # 加载 shard
    all_shard = []
    for f in sorted(SHARD_DIR.glob('records-*.json')):
        d = json.loads(f.read_text('utf-8'))
        all_shard.extend(d.get('records', []))
    print(f"Shard: {len(all_shard)} 条")
    
    # 合并：遍历 shard 更新匹配的旧记录，同时标记 shard 中已有的
    shard_keys_seen = set()
    matched = 0
    new_count = 0
    updated = 0
    
    for sr in all_shard:
        key = (sr.get('url', ''), sr.get('title', ''), sr.get('date', ''))
        shard_keys_seen.add(key)
        if key in old_map:
            old_r = old_map[key]
            # 用 shard 更新分类/标签
            old_cat = old_r.get('c', '')
            new_cat = sr.get('category', '')
            if (new_cat and new_cat != '未分类' and old_cat != new_cat) or \
               (not old_r.get('tg') and sr.get('tag')):
                old_r = update_lite_from_shard(old_r, sr)
                updated += 1
            matched += 1
        else:
            old_map[key] = make_lite_record(sr)
            new_count += 1
    
    # 收集所有记录：旧 lite 全部保留 + 新增 shard 记录
    merged = list(old_map.values())
    
    # 按日期降序排列
    merged.sort(key=lambda x: x.get('d', ''), reverse=True)
    
    print(f"匹配: {matched} (更新: {updated})")
    print(f"新增: {new_count}")
    print(f"合并后: {len(merged)} 条")
    
    # 写入
    LITE_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, separators=(',', ':')),
        'utf-8'
    )
    size_mb = LITE_PATH.stat().st_size / 1024 / 1024
    print(f"文件大小: {size_mb:.1f} MB")
    
    # 统计
    classified = sum(1 for r in merged if r.get('c') not in ['未分类', '不相关', ''])
    unrelated = sum(1 for r in merged if r.get('c') == '不相关')
    unclassified = sum(1 for r in merged if r.get('c') in ['未分类', ''])
    has_lv = sum(1 for r in merged if r.get('lv', 0) > 0)
    has_as = sum(1 for r in merged if r.get('as'))
    has_sc = sum(1 for r in merged if r.get('sc') is not None)
    print(f"已分类(相关): {classified} | 不相关: {unrelated} | 未分类: {unclassified}")
    print(f"有lv: {has_lv} | 有AI摘要: {has_as} | 有评分: {has_sc}")
    
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
