# 技术边界数据库

这是一个全新的静态技术情报数据库仓库。它从三个 GitHub 源仓库拉取 CSV，生成统一 JSON，再由 GitHub Pages 展示。

## 目录
- `scripts/build_database.py`：拉取、规范化、缓存、分类、抽参、生成知识库
- `data/processed/intelligence.json`：主数据库
- `data/knowledge/*.json`：叶子行业知识库文档
- `taxonomy.json`：分类树
- `.github/workflows/*.yml`：自动更新与触发

## 运行
```bash
python3 scripts/build_database.py --output data
python3 -m http.server 8000
```
