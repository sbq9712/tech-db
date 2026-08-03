# 技术边界数据库

静态技术情报数据库，部署在 GitHub Pages。

## 在线访问

https://sbq9712.github.io/tech-db/

## 仅离线浏览数据库

1. 下载整个仓库（Code → Download ZIP）
2. 解压
3. 双击 `index-local.html`

所有数据和代码都在这一个文件里，无需安装任何软件，无需联网。

AI 问答需要联网。完整的一键安装、离线迁移包和 Docker 用法见 [MIGRATION.md](MIGRATION.md)。

## 一键启动数据库与 AI 问答

- Windows：双击 `start.cmd`
- Linux/macOS：运行 `./start.sh`
- Docker：复制 `.env.example` 为 `.env` 后运行 `docker compose up --build`

首次运行会从本仓库 Releases 下载固定版本的 `bge-m3` 模型和搜索索引，逐文件校验 SHA-256，并保存到不进入 Git 的 `runtime/`。GLM API Key 必须由每位部署者自行配置，绝不包含在仓库或迁移包中。

## 自动检查

- Windows：双击 `check.cmd`
- Linux/macOS：运行 `./check.sh`
- GitHub：每次 push 后自动运行同一套质量检查

## 本地开发/服务器模式

```bash
git clone https://github.com/sbq9712/tech-db.git
cd tech-db
python3 -m http.server 8000
# 打开 http://localhost:8000
```

## 文件结构

```
tech-db/
├── index.html              在线版入口（fetch 加载数据）
├── index-local.html        离线版入口（内联数据，双击即用）
├── app.js                  前端逻辑
├── styles.css              样式
├── data/
│   ├── category-order.json 分类树排序
│   └── processed/
│       ├── all-records-lite.json   精简数据（在线版）
│       ├── lite-part-0~5.js        分片数据（旧版离线）
│       ├── manifest.json           数据清单
│       └── manifest-data.js
├── scripts/
│   ├── build_database.py         拉取+规范化+去重
│   ├── classify_and_tag.py       领域分类+情报类型+标签
│   ├── extract_params.py         参数提取
│   ├── process_unclassified.py   处理未分类记录
│   ├── type_judge.py             情报类型判断
│   ├── import_excel.py           从Excel导入新情报
│   └── build_local.py            生成index-local.html
└── prompts/
    └── pipeline_guide.md         处理流程提示词
```

## 数据处理流程

新情报进入后，按以下顺序处理：

1. **领域分类** — 判断属于零碳产业/AI与智能科技/通用技术/不相关
2. **情报类型分类** — 新闻（技术突破/产业进展/政策监管/资本运作/行业观察）或文献（研究论文/观点评论）
3. **参数提取** — 从正文中提取关键参数

如果领域分类判定为"不相关"，后续步骤全部跳过。

### 处理未分类记录

```bash
python3 scripts/process_unclassified.py
python3 scripts/build_local.py  # 重建离线版
```

### 从 Excel 导入新情报

```bash
python3 scripts/import_excel.py /path/to/file.xlsx
python3 scripts/process_unclassified.py
python3 scripts/build_local.py
```

## 等级体系

| 等级 | 字段 | 颜色 | 说明 |
|------|------|------|------|
| 信息爬取 | — | — | 全部记录 |
| 信息筛选 | — | — | 去掉LLM判定为"不相关"的 |
| 精选 | lv=1 | 绿色 | 人工标注的高质量情报 |
| 重点 | lv=2 | 橙色 | 人工标注的重点情报 |
| 预警 | lv=3 | 红色 | 人工标注的预警情报 |

预警同时具有重点和精选属性，重点同时具有精选属性。
新导入未经LLM分类的情报标记为"未分类"（非"不相关"）。

## 技术栈

- 纯静态：HTML + CSS + JS，无框架依赖
- 分类引擎：GLM 5.2（语义推理，非关键词匹配）
- 字体：Outfit（数字清晰，0≠8）+ IBM Plex Mono
- 页面：蓝白视觉基调，支持主流现代桌面和移动浏览器
