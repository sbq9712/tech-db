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
- 主 pipeline：`auto_pipeline.py`（本地由 systemd `techdb-pipeline.timer` 每 2 小时触发；SKIP_INDEX_BUILD=1 由 data-sync/vector/graph 专职服务建索引；main 受 GH006 分支保护，推送失败时 ALLOW_LOCAL_STATE_ADVANCE=1 本地推进 state 并推 data-auto-sync/<ts> 备份分支）
- 分支策略（2026-09-20，owner 指令"只保留一个 main 分支"）：远端仅 `main` 一个分支；分支保护已放宽（enforce_admins=false、免审，但外部 PR 仍要求 11 项 Phase09 检查）。Phase10 准备工作快照在 tag `phase10-prep-snapshot` + 本地 worktree `/home/rhett/tech-db-phase10-prep`（分支 `prep/phase10-rt110-116` 仅存本地）。data-auto-sync 备份分支已确认全部为 main 祖先并清理；现在 main 可直推，该备份路径自然休眠。
- 站点访问（2026-09-20）：首选 `http://localhost:8765`（server.py 现直接服务前端：白名单根文件 + /data/{processed,reports,knowledge} + data/ 根 JSON，同源无 CORS；docs/redoc/openapi 已关闭防隧道泄漏）。8097 http.server 门户仍并存。隧道 keepalive 改为健康探测驱动（连续 3 次/10min 失败才重启，替代原 5h 强制轮换 URL），URL 变更后 tunnel_url_sync 自动推 main + Pages ~1 分钟内生效（push 已不被拒）。qa.js qaFetch（v221+）：Pages 页隧道失效时自动回退 http://localhost:8765（需 PNA 头，已在 OPTIONS 中间件加 Access-Control-Allow-Private-Network: true），改前端 js 后必须 bump index.html 的 qa.js?v=N。
- 模型：GLM-5.3-flash；QA 管线默认 thinking disabled（`QA_LLM_THINKING` 可开）
- 标题翻译：GLM 批量翻译（`translate_non_chinese_titles`，Google Translate 已 429 死亡）
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
- 聚类增量：`python3 scripts/clustering.py --ids <id1>,<id2> --dry-run --provider zai --model glm-5.3-flash`
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
