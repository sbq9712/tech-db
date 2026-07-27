#!/usr/bin/env python3
"""
tech-db 数据完整性校验脚本
在每次数据导入或重建后运行，确保：
1. 所有有正文的记录都有AI摘要
2. 所有非"不相关"记录都有评分
3. 统计缺失项并报告
"""
import json, os, sys

def check():
    lite_path = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "all-records-lite.json")
    lite_path = os.path.abspath(lite_path)
    
    if not os.path.exists(lite_path):
        print(f"ERROR: {lite_path} not found")
        sys.exit(1)
    
    with open(lite_path, "r", encoding="utf-8") as f:
        lite = json.load(f)
    
    total = len(lite)
    
    # Check 1: AI summaries
    has_summary = sum(1 for r in lite if r.get("as", "").strip())
    pending = sum(1 for r in lite if not r.get("as", "").strip() and r.get("b", "").strip())
    no_content = sum(1 for r in lite if not r.get("as", "").strip() and not r.get("b", "").strip())
    
    # Check 2: Scores (only for non-不相关)
    relevant = [r for r in lite if r.get("c", "") != "不相关" and not r.get("dp")]
    has_score = sum(1 for r in relevant if r.get("sc", 0) > 0)
    missing_score = len(relevant) - has_score
    
    # Check 3: Classification
    unclassified = sum(1 for r in lite if r.get("c", "") in ("", "未分类"))
    
    # Check 4: Dedup status
    dp_count = sum(1 for r in lite if r.get("dp"))
    
    print("=" * 60)
    print("tech-db 数据完整性报告")
    print("=" * 60)
    print(f"总记录: {total}")
    print(f"重复标记(dp=1): {dp_count}")
    print(f"未分类: {unclassified}")
    print()
    print(f"AI摘要:")
    print(f"  已生成: {has_summary} ({has_summary*100//total}%)")
    print(f"  待生成(有正文无摘要): {pending}")
    print(f"  无正文: {no_content}")
    print()
    print(f"评分(非不相关·非重复):")
    print(f"  相关记录: {len(relevant)}")
    print(f"  已评分: {has_score} ({has_score*100//max(len(relevant),1)}%)")
    print(f"  缺评分: {missing_score}")
    print()
    
    issues = []
    if pending > 0:
        issues.append(f"⚠ {pending} 条记录有正文但缺AI摘要")
    if missing_score > 0:
        issues.append(f"⚠ {missing_score} 条相关记录缺评分")
    if unclassified > 0:
        issues.append(f"⚠ {unclassified} 条记录未分类")
    
    if issues:
        print("⚠ 需要处理的问题:")
        for issue in issues:
            print(f"  {issue}")
        sys.exit(1)
    else:
        print("✓ 全部检查通过")
        sys.exit(0)

if __name__ == "__main__":
    check()
