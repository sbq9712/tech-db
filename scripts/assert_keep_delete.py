#!/usr/bin/env python3
"""断言脚本：验证保留/删除范围的正确性。
每次涉及删除范围的改动前必须运行此脚本，确认数字一致。

保留规则（已验证）：
  精选情报(683) = source=='excel-import' OR lv==3
  重点情报(101) = lv>=2 （是精选的子集）
  预警情报(10)  = lv==3 （是重点的子集）
  嵌套关系：预警 ⊂ 重点 ⊂ 精选

删除规则：
  非精选情报 = NOT (source=='excel-import' OR lv==3)
"""
import json, sys
from pathlib import Path

LITE = Path(__file__).resolve().parents[1] / "data" / "processed" / "all-records-lite.json"

def main():
    data = json.loads(LITE.read_text("utf-8"))
    total = len(data)

    keep = [r for r in data if r.get("source") == "excel-import" or r.get("lv") == 3]
    delete = [r for r in data if not (r.get("source") == "excel-import" or r.get("lv") == 3)]
    focus = [r for r in data if r.get("lv", 0) >= 2]
    alerts = [r for r in data if r.get("lv") == 3]

    errors = []

    def check(name, actual, expected):
        if actual != expected:
            errors.append(f"FAIL {name}: expected {expected}, got {actual}")
        else:
            print(f"OK   {name}: {actual}")

    check("总记录", total, 60669)
    check("精选情报 (source==excel-import OR lv==3)", len(keep), 683)
    check("重点情报 (lv>=2)", len(focus), 101)
    check("预警情报 (lv==3)", len(alerts), 10)
    check("删除 (非精选)", len(delete), 59986)

    # 嵌套关系断言
    keep_ids = set(id(r) for r in keep)
    focus_ids = set(id(r) for r in focus)
    alert_ids = set(id(r) for r in alerts)

    if not alert_ids.issubset(focus_ids):
        errors.append("FAIL nesting: alerts not subset of focus")
    else:
        print("OK   nesting: alerts (10) subset-of focus (101)")

    if not focus_ids.issubset(keep_ids):
        errors.append("FAIL nesting: focus not subset of keep")
    else:
        print("OK   nesting: focus (101) subset-of keep (683)")

    # 预警字段完整性
    for r in alerts:
        if not r.get("wr"):
            errors.append(f"FAIL alert missing wr: {r.get('t','')[:30]}")
    if not any("alert missing wr" in e for e in errors):
        print("OK   all alert records have wr field")

    print(f"\nkeep: {len(keep)} | delete: {len(delete)} | total: {total}")
    if errors:
        print(f"\nFAILED {len(errors)} assertions:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\nAll assertions passed.")

if __name__ == "__main__":
    main()
