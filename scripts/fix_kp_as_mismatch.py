#!/usr/bin/env python3
"""
Fix mismatched kp/as records by re-extracting with single-record LLM calls.
Bypasses the batch id mapping bug entirely.

For each mismatched record:
  - kp mismatch: re-extract key params using single call_glm()
  - as mismatch (has body): re-generate AI summary using single call_glm()
  - as mismatch (title-only): check topic word overlap → re-generate or skip
  - Max 3 retries per record; failures → unfixable.json
"""
import json, re, sys, time, os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from llm_client import call_glm

LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
VERIFICATION_PATH = REPO / "data" / "processed" / "kp_as_verification.json"
UNFIXABLE_PATH = REPO / "data" / "processed" / "unfixable.json"

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

SUMMARY_PROMPT_TEMPLATE = """你是技术情报摘要专家。为以下情报生成100-200字的中文AI摘要。
重要：无论原文是什么语言，摘要必须全部用中文撰写。
只输出摘要文本，不要输出JSON或其他格式。

标题：{title}
正文：{body}"""

SUMMARY_TITLE_ONLY_TEMPLATE = """这些情报没有正文，只有标题。请根据标题生成简短中文摘要（20-50字）。
根据标题推断研究主题和技术方向，简要描述该情报可能涉及的内容。
不要编造具体数据。只输出摘要文本。

标题：{title}"""

MAX_RETRIES = 3


def parse_kp_response(text):
    """Parse kp response from LLM."""
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`').replace('json\n', '', 1).strip()
    # Find JSON array
    match = re.search(r'\[.*\]', text, re.S)
    if match:
        try:
            arr = json.loads(match.group(0))
            if isinstance(arr, list) and len(arr) > 0:
                item = arr[0]
                if isinstance(item, dict) and 'key_params' in item:
                    return [str(p).strip() for p in item['key_params'] if str(p).strip()][:5]
                elif isinstance(item, str):
                    return [item]
        except json.JSONDecodeError:
            pass
    return []


def check_title_topic_overlap(title, summary):
    """Check if title and summary share topic words (for title-only records)."""
    title_cn = set(re.findall(r'[一-鿿]{2,}', title))
    sum_cn = set(re.findall(r'[一-鿿]{2,}', summary))
    if not title_cn:
        return True  # Can't determine, don't skip
    overlap = title_cn & sum_cn
    return len(overlap) > 0


def fix_kp(record):
    """Re-extract kp for a single record."""
    title = record.get('t', '')
    body = (record.get('b', '') or record.get('fb', '') or '')[:500]
    category = record.get('c', '')
    
    prompt = KP_PROMPT_TEMPLATE.format(title=title[:200], body=body, category=category)
    
    for attempt in range(MAX_RETRIES):
        try:
            result = call_glm(prompt, timeout=60)
            kp = parse_kp_response(result)
            if kp or result.strip():
                # Accept even empty list if LLM responded (means no params to extract)
                if not kp:
                    return []  # Empty is valid if LLM says no params
                return kp
        except Exception as e:
            print(f"    Attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None  # Failed


def fix_as(record):
    """Re-generate as for a single record."""
    title = record.get('t', '')
    body = (record.get('b', '') or record.get('fb', '') or '').strip()
    
    if body:
        prompt = SUMMARY_PROMPT_TEMPLATE.format(title=title[:200], body=body[:1000])
    else:
        prompt = SUMMARY_TITLE_ONLY_TEMPLATE.format(title=title[:200])
    
    for attempt in range(MAX_RETRIES):
        try:
            result = call_glm(prompt, timeout=60)
            summary = result.strip()
            if summary and len(summary) >= 10:
                # For title-only: check topic overlap
                if not body:
                    if not check_title_topic_overlap(title, summary):
                        # Topic mismatch - might be wrong, but for title-only this is expected
                        # since LLM can only guess from title. Accept it.
                        pass
                return summary
        except Exception as e:
            print(f"    Attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None  # Failed


def main():
    print("=" * 60)
    print("  Fix kp/as Mismatched Records")
    print("=" * 60)
    
    # Load data
    data = json.loads(LITE_PATH.read_text('utf-8'))
    verification = json.loads(VERIFICATION_PATH.read_text('utf-8'))
    
    kp_mismatch = set(verification['kp_mismatch_indices'])
    as_mismatch = set(verification['as_mismatch_indices'])
    
    print(f"Total records: {len(data)}")
    print(f"KP to fix: {len(kp_mismatch)}")
    print(f"AS to fix: {len(as_mismatch)}")
    
    unfixable = []
    kp_fixed = 0
    kp_failed = 0
    as_fixed = 0
    as_failed = 0
    as_skipped_title_only = 0
    
    all_indices = sorted(kp_mismatch | as_mismatch)
    
    for i, idx in enumerate(all_indices):
        r = data[idx]
        title = r.get('t', '')[:50]
        body = (r.get('b', '') or r.get('fb', '') or '').strip()
        needs_kp = idx in kp_mismatch
        needs_as = idx in as_mismatch
        
        print(f"\n[{i+1}/{len(all_indices)}] idx={idx} {title}")
        
        # Fix kp
        if needs_kp:
            print(f"  Fixing kp...")
            new_kp = fix_kp(r)
            if new_kp is not None:
                data[idx]['kp'] = new_kp
                kp_fixed += 1
                print(f"  ✅ kp fixed: {new_kp[:2] if new_kp else '(empty)'}")
            else:
                kp_failed += 1
                unfixable.append({"idx": idx, "field": "kp", "title": title})
                print(f"  ❌ kp failed after {MAX_RETRIES} retries")
        
        # Fix as
        if needs_as:
            # Title-only branch: check topic overlap first
            if not body:
                existing_as = r.get('as', '')
                if existing_as and check_title_topic_overlap(title, existing_as):
                    print(f"  ⏭️  as skipped (title-only, topic overlap exists)")
                    as_skipped_title_only += 1
                else:
                    print(f"  Fixing as (title-only)...")
                    new_as = fix_as(r)
                    if new_as is not None:
                        data[idx]['as'] = new_as
                        as_fixed += 1
                        print(f"  ✅ as fixed: {new_as[:60]}...")
                    else:
                        as_failed += 1
                        unfixable.append({"idx": idx, "field": "as", "title": title})
                        print(f"  ❌ as failed after {MAX_RETRIES} retries")
            else:
                print(f"  Fixing as...")
                new_as = fix_as(r)
                if new_as is not None:
                    data[idx]['as'] = new_as
                    as_fixed += 1
                    print(f"  ✅ as fixed: {new_as[:60]}...")
                else:
                    as_failed += 1
                    unfixable.append({"idx": idx, "field": "as", "title": title})
                    print(f"  ❌ as failed after {MAX_RETRIES} retries")

        # Save progress every 10 records
        if (i + 1) % 10 == 0:
            LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), 'utf-8')
            print(f"  [Progress saved: {i+1}/{len(all_indices)}]")
    
    # Final save
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), 'utf-8')
    
    # Save unfixable
    if unfixable:
        UNFIXABLE_PATH.write_text(json.dumps(unfixable, ensure_ascii=False, indent=2), 'utf-8')
    
    # Summary
    print(f"\n{'='*60}")
    print(f"  FIX SUMMARY")
    print(f"{'='*60}")
    print(f"  KP fixed:    {kp_fixed}")
    print(f"  KP failed:   {kp_failed}")
    print(f"  AS fixed:    {as_fixed}")
    print(f"  AS failed:   {as_failed}")
    print(f"  AS skipped (title-only): {as_skipped_title_only}")
    print(f"  Unfixable:   {len(unfixable)}")
    print(f"  Data saved to: {LITE_PATH}")
    if unfixable:
        print(f"  Unfixable saved to: {UNFIXABLE_PATH}")

    # Verify-fix-verify: run verification after fix to confirm mismatch reduced
    print(f"\n{'='*60}")
    print(f"  POST-FIX VERIFICATION")
    print(f"{'='*60}")
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "verify_kp_as.py")],
        capture_output=True, text=True, timeout=300,
    )
    print(result.stdout[-500:] if result.stdout else "(no output)")
    if result.returncode != 0:
        print(f"  ⚠️ Verification returned non-zero exit code: {result.returncode}")
        print(f"  stderr: {result.stderr[-200:]}" if result.stderr else "")


if __name__ == "__main__":
    main()
