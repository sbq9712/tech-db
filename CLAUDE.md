# tech-db 项目上下文（Claude Code）

## 项目概述
技术情报数据库，追踪零碳产业、AI与智能科技、通用技术三大领域的硬核前沿情报。
数据源是三个 GitHub 仓库的 CSV，经过去重、分类、评分、聚类后展示在 GitHub Pages 前端。

## 关键路径
- 工作目录：`/home/rhett/tech-db-fresh/`
- 主数据：`data/processed/all-records-lite.json`（唯一真相源）
- 前端分片：`data/processed/lite-part-0~N.js`（每片 2000 条）
- manifest：`data/processed/manifest-data.js`
- 固定分类树：`data/category-taxonomy.json`（77 个最小叶子，未经用户明确命令不得修改）
- 主 pipeline：`auto_pipeline.py`
- 聚类引擎：`scripts/clustering.py`
- 数据契约：`scripts/data_contract.py`、`scripts/validate_data_contract.py`、`scripts/build_snapshot.py`
- 前端：`index.html`、`app.js`、`styles.css`（修改后必须 bump `?v=N`）
- 线上：https://sbq9712.github.io/tech-db/

## 强制规则
1. 分类树固定 77 个叶子；任何分类只能精确命中叶子或为 `不相关`/`未分类`。
2. `不相关` 是终止状态：分类为不相关后不得有 `sc/scd/aip/as/kp/tp/cl/cp/cln`。
3. 分类分隔符必须用 `/`，不得用 `-`、`>` 等。
4. 新闻标签白名单：`技术突破/产业进展/政策监管/资本运作/行业观察`。
5. 文献标签白名单：`研究论文/观点评论`。
6. 修改 records 后必须通过 `build_snapshot()` 重建，然后运行 `validate_data_contract.py`。
7. push 前必须验证数据契约通过。
8. git 提交只暂存生成数据，禁止 `git add -A`。
9. 聚类 `cp=0` 是可见父条目，`cp=1` 是隐藏子条目，每个聚类恰好一个父项。
10. AI 精选必须双重判断：`aip=1 && category!='不相关'`。
11. 预警渲染不依赖 `full_body`；只要有 `lv/cm/wr` 就渲染人工信息分支。
12. 前端缓存：修改 `app.js` 或 `styles.css` 后必须同步 bump `index.html` 中的 `?v=N`。

## 常用命令
- 验证数据契约：`python3 scripts/validate_data_contract.py`
- 运行测试：`python3 -m unittest discover -s tests -v`
- 语法检查：`python3 -m py_compile <file>` 和 `node --check app.js`
- 推送（不硬编码 token）：`source /home/rhett/.gh_env && git push "https://sbq9712:${GH_TOKEN}@github.com/sbq9712/tech-db.git" main`
- 聚类增量：`python3 scripts/clustering.py --ids <id1>,<id2> --dry-run --provider zai --model glm-5.2`
- 推送后验证线上：sleep 70 然后 curl `https://sbq9712.github.io/tech-db/index.html` 确认 `?v=N` 已更新

## 技术栈
- 后端：Python 3（stdlib + sentence-transformers + numpy）
- 前端：原生 HTML/CSS/JS（无构建工具，无框架）
- 数据存储：JSON 文件 + GitHub Pages
- 模型：GLM-5.2（分类/评分/摘要/聚类）、BGE-m3（embedding）
- 字体：Outfit + IBM Plex Mono + Noto Sans SC（强制浅色主题不适用 tooltip）

## 安全
- GitHub token 只从 `/home/rhett/.gh_env` 环境变量加载，不硬编码。
- embedding 缓存、pipeline state、聚类 checkpoint 已 gitignore，不上 GitHub。
