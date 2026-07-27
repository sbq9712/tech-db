#!/usr/bin/env python3
"""重新生成本地数据文件。

改了数据（导入Excel、分类等）后运行此脚本，
会重新生成 lite-part-*.js 分片文件 + 更新 manifest。

用法（Windows 命令行）：
    cd 仓库目录
    python scripts/build_local.py

用法（Mac/Linux）：
    cd 仓库目录
    python3 scripts/build_local.py
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LITE_JSON = REPO / 'data' / 'processed' / 'all-records-lite.json'
MANIFEST_JSON = REPO / 'data' / 'processed' / 'manifest.json'
CHUNK_DIR = REPO / 'data' / 'processed'

CHUNK_SIZE = 3000  # 每个文件约1.2MB，浏览器不会拦截

def main():
    # 读取数据
    data = json.loads(LITE_JSON.read_text('utf-8'))
    print(f"读取 {len(data)} 条记录")

    # 更新 manifest
    manifest = json.loads(MANIFEST_JSON.read_text('utf-8'))
    manifest['meta']['records_total'] = len(data)
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False), 'utf-8')
    (CHUNK_DIR / 'manifest-data.js').write_text(
        f'window.__MANIFEST__={json.dumps(manifest, ensure_ascii=False)};', 'utf-8')

    # 删除旧分片
    for f in CHUNK_DIR.glob('lite-part-*.js'):
        f.unlink()

    # 生成新分片
    chunks = [data[i:i+CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
    for i, chunk in enumerate(chunks):
        content = (f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];'
                   f'window.__LITE_PARTS__.push('
                   f'{json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))});\n')
        (CHUNK_DIR / f'lite-part-{i}.js').write_text(content, 'utf-8')

    # 更新 index-local.html 的 script 引用数量
    index_local = REPO / 'index-local.html'
    if index_local.exists():
        html = index_local.read_text('utf-8')
        import re
        # 删除旧的 lite-part 引用
        html = re.sub(r'\s*<script src="data/processed/lite-part-\d+\.js"></script>', '', html)
        # 在 manifest-data.js 引用前插入新的
        new_scripts = '\n'.join(
            f'  <script src="data/processed/lite-part-{i}.js"></script>'
            for i in range(len(chunks))
        )
        html = html.replace(
            '<script src="data/processed/manifest-data.js"></script>',
            f'{new_scripts}\n  <script src="data/processed/manifest-data.js"></script>'
        )
        index_local.write_text(html, 'utf-8')

    print(f"生成 {len(chunks)} 个分片文件（每个 ≤{CHUNK_SIZE} 条记录）")
    print(f"manifest 更新：{len(data)} 条记录")
    print("完成！双击 index-local.html 即可查看最新数据")

if __name__ == '__main__':
    main()
