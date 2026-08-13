#!/usr/bin/env python3
"""重建 meta/summary 分片 — 已合并入 build_local.py（规范构建器）。

本脚本保留为兼容入口，行为与 build_local.py 完全一致：
调用 auto_pipeline.py 的 build_snapshot.py，字节级一致输出。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_local import main  # noqa: E402

if __name__ == '__main__':
    main()
