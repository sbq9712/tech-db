#!/usr/bin/env python3
"""Rebuild meta-part-*.js and summary-part-*.js shard files from all-records-lite.json."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LITE_JSON = REPO / 'data' / 'processed' / 'all-records-lite.json'
CHUNK_DIR = REPO / 'data' / 'processed'
CHUNK_SIZE = 3000

def main():
    data = json.loads(LITE_JSON.read_text('utf-8'))
    print(f"读取 {len(data)} 条记录")
    
    # Build meta records
    meta_records = []
    for r in data:
        meta = {
            "t": r.get("t", ""),
            "d": r.get("d", ""),
            "u": r.get("u", ""),
            "c": r.get("c", ""),
            "a": r.get("a", ""),
            "i": r.get("i", "n"),
            "source": r.get("source", ""),
            "lv": r.get("lv", 0),
            "cm": r.get("cm", ""),
            "wr": r.get("wr", ""),
            "tg": r.get("tg", ""),
            "tp": r.get("tp", ""),
            "sc": r.get("sc", 0),
            "aip": r.get("aip", 0),
            "kp": r.get("kp", []),
            "hb": r.get("hb", 0),
        }
        meta_records.append(meta)
    
    # Build summary records
    summary_records = []
    for i, r in enumerate(data):
        summary = {
            "i": i,
            "as": r.get("as", ""),
            "scd": r.get("scd", {}),
            "kp": r.get("kp", []),
        }
        summary_records.append(summary)
    
    # Delete old parts
    for f in CHUNK_DIR.glob('meta-part-*.js'):
        f.unlink()
    for f in CHUNK_DIR.glob('summary-part-*.js'):
        f.unlink()
    
    # Write new parts
    meta_chunks = [meta_records[i:i+CHUNK_SIZE] for i in range(0, len(meta_records), CHUNK_SIZE)]
    summary_chunks = [summary_records[i:i+CHUNK_SIZE] for i in range(0, len(summary_records), CHUNK_SIZE)]
    
    for i, chunk in enumerate(meta_chunks):
        content = (f'window.__META_PARTS__=window.__META_PARTS__||[];'
                   f'window.__META_PARTS__.push('
                   f'{json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))});\n')
        (CHUNK_DIR / f'meta-part-{i}.js').write_text(content, 'utf-8')
    
    for i, chunk in enumerate(summary_chunks):
        content = (f'window.__SUMMARY_PARTS__=window.__SUMMARY_PARTS__||[];'
                   f'window.__SUMMARY_PARTS__.push('
                   f'{json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))});\n')
        (CHUNK_DIR / f'summary-part-{i}.js').write_text(content, 'utf-8')
    
    # Update index-local.html references
    index_local = REPO / 'index-local.html'
    if index_local.exists():
        html = index_local.read_text('utf-8')
        import re
        # Update meta-part references
        html = re.sub(r'\s*<script src="data/processed/meta-part-\d+\.js"></script>', '', html)
        new_meta = '\n'.join(
            f'  <script src="data/processed/meta-part-{i}.js"></script>'
            for i in range(len(meta_chunks))
        )
        html = html.replace(
            '<script src="data/processed/manifest-data.js"></script>',
            f'{new_meta}\n  <script src="data/processed/manifest-data.js"></script>'
        )
        
        # Update summary-part references
        html = re.sub(r'\s*<script src="data/processed/summary-part-\d+\.js"></script>', '', html)
        new_summary = '\n'.join(
            f'  <script src="data/processed/summary-part-{i}.js"></script>'
            for i in range(len(summary_chunks))
        )
        html = html.replace(
            '<script src="data/processed/manifest-data.js"></script>',
            f'{new_summary}\n  <script src="data/processed/manifest-data.js"></script>'
        )
        index_local.write_text(html, 'utf-8')
    
    print(f"生成 {len(meta_chunks)} 个 meta-part + {len(summary_chunks)} 个 summary-part")
    print("完成！")


if __name__ == '__main__':
    main()
