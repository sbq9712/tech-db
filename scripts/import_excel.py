#!/usr/bin/env python3
"""Import intelligence records from an Excel file.

Excel columns (auto-detected):
  - 重要程度/分类/等级  → lv (精选=1, 重点=2, 预警=3, 重要=2)
  - 文章标题/标题       → t
  - 文章内容/正文/内容  → b (full body) + fb (full body)
  - Comment/评论        → cm
  - 日期                → d (YYYY-MM-DD)
  - 链接地址/链接/URL   → u
  - 来源                → a
  - 作者                → appended to a

Usage:
  python3 scripts/import_excel.py /path/to/file.xlsx
"""
import json, sys, re
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'openpyxl', '--break-system-packages'])
    import openpyxl

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / 'data' / 'processed' / 'all-records-lite.json'

LEVEL_MAP = {'精选': 1, '重点': 2, '预警': 3, '重要': 2}

def clean_text(text, maxlen=None):
    if not text: return ''
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    if maxlen:
        text = text[:maxlen]
    return text.strip()

def find_col(headers, candidates):
    for i, h in enumerate(headers):
        h_str = str(h or '').strip()
        for c in candidates:
            if c in h_str:
                return i
    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/import_excel.py <file.xlsx>")
        sys.exit(1)
    
    xlsx_path = Path(sys.argv[1])
    if not xlsx_path.exists():
        print(f"Error: {xlsx_path} not found")
        sys.exit(1)

    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    headers = rows[0]
    
    print(f"Columns: {headers}")
    
    col_level = find_col(headers, ['重要程度', '分类', '等级', '级别', 'level'])
    col_title = find_col(headers, ['文章标题', '标题', 'title'])
    col_body = find_col(headers, ['文章内容', '正文', '内容', 'body', 'content'])
    col_comment = find_col(headers, ['Comment', 'comment', '评论', '评注'])
    col_date = find_col(headers, ['日期', 'date'])
    col_url = find_col(headers, ['链接地址', '链接', 'URL', 'url', 'link'])
    col_source = find_col(headers, ['来源', 'source'])
    col_author = find_col(headers, ['作者', 'author'])
    
    print(f"Detected: level={col_level}, title={col_title}, body={col_body}, comment={col_comment}, date={col_date}")

    data = json.loads(LITE_PATH.read_text('utf-8'))
    print(f"Existing records: {len(data)}")

    new_count = 0
    for row in rows[1:]:
        level_str = str(row[col_level] or '').strip() if col_level is not None else ''
        lv = LEVEL_MAP.get(level_str, 0)
        if lv == 0:
            continue
        
        title = clean_text(row[col_title]) if col_title is not None else ''
        full_body = clean_text(row[col_body]) if col_body is not None else ''
        comment = clean_text(row[col_comment]) if col_comment is not None else ''
        
        date_val = row[col_date] if col_date is not None else ''
        if isinstance(date_val, datetime):
            d = date_val.strftime('%Y-%m-%d')
        else:
            d = str(date_val)[:10] if date_val else ''
        
        url = str(row[col_url] or '').strip() if col_url is not None else ''
        source = str(row[col_source] or '').strip() if col_source is not None else ''
        author = str(row[col_author] or '').strip() if col_author is not None else ''
        
        if author and author != '无':
            source = f"{source} / {author}" if source else author
        
        rec = {
            't': title,
            'b': full_body[:120],
            'fb': full_body,
            'd': d,
            'i': 'n',
            'u': url,
            'c': '未分类',
            'a': source,
            'lv': lv,
        }
        if comment:
            rec['cm'] = comment
        
        data.append(rec)
        new_count += 1

    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    print(f"\nImported: {new_count} records")
    print(f"Total: {len(data)} records")
    print(f"With comment: {sum(1 for r in data[-new_count:] if r.get('cm'))}")
    print(f"\nNext: run process_unclassified.py to classify them")

if __name__ == '__main__':
    main()
