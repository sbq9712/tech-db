#!/usr/bin/env python3
"""重新生成本地数据文件（源 JSON + 三类分片 + manifest）+ index-local.html 引用。

直接调用 auto_pipeline.py 所用的规范构建器 scripts/build_snapshot.py，
保证输出与流水线字节一致（分片格式、manifest、data_version）。

用法：
    python3 scripts/build_local.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_snapshot import build_snapshot  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LITE_JSON = REPO / 'data' / 'processed' / 'all-records-lite.json'
LEGACY_MANIFEST = REPO / 'data' / 'processed' / 'manifest.json'


def main():
    data = json.loads(LITE_JSON.read_text('utf-8'))
    print(f"读取 {len(data)} 条记录")

    # 规范构建（原子）：源 JSON + lite/meta/summary 分片 + manifest-data.js + data_version
    shard_count = build_snapshot(data)
    print(f"规范重建 {shard_count} 个分片/类（build_snapshot）")

    # 兼容旧 manifest.json（部分脚本仍读取）
    if LEGACY_MANIFEST.exists():
        m = json.loads(LEGACY_MANIFEST.read_text('utf-8'))
        m.setdefault('meta', {})['records_total'] = len(data)
        m['meta']['total_shards'] = shard_count
        LEGACY_MANIFEST.write_text(json.dumps(m, ensure_ascii=False), 'utf-8')

    # 更新 index-local.html 引用
    index_local = REPO / 'index-local.html'
    if index_local.exists():
        html = index_local.read_text('utf-8')
        for kind in ('lite', 'meta', 'summary'):
            pat = re.compile(rf'([ \t]*)<script src="data/processed/{kind}-part-\d+\.js"></script>\n')
            matches = list(pat.finditer(html))
            if not matches:
                print(f"警告：index-local.html 中未找到 {kind}-part 引用")
                continue
            indent = matches[0].group(1)
            block = ''.join(
                f'{indent}<script src="data/processed/{kind}-part-{i}.js"></script>\n'
                for i in range(shard_count))
            html = html[:matches[0].start()] + block + html[matches[-1].end():]
        index_local.write_text(html, 'utf-8')
        print(f"index-local.html 引用已更新（{shard_count} 个分片/类）")

    print("完成！双击 index-local.html 即可查看最新数据")


if __name__ == '__main__':
    main()
