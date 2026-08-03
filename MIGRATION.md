# Tech-DB 安装与迁移

Tech-DB 的网页数据库和问答服务彼此独立：`index-local.html` 可离线浏览数据库；AI 问答需要本地或云端后端、`bge-m3` 模型、搜索索引和使用者自己的 GLM API Key。

## 联网一键安装

### Windows

1. 安装 Python 3.11 或更高版本，并勾选“Add Python to PATH”。
2. 双击 `start.cmd`。
3. 首次运行会创建环境，从本仓库 `runtime-v1` Release 下载模型和索引，并核对 SHA-256。
4. 按提示填写自己的 `ZAI_API_KEY`。密钥只保存在本机 `.env`，不会进入 Git。

需要只做安装时，在 PowerShell 运行 `./setup.ps1`。双击 `check.cmd` 可执行与 GitHub Actions 相同的检查。

### Linux

```bash
chmod +x setup.sh start.sh check.sh
./setup.sh
./start.sh
```

网页入口为 `http://localhost:8097`，后端健康检查为 `http://localhost:8765/api/health`。

### Docker

先复制配置并填写自己的密钥：

```bash
cp .env.example .env
docker compose up --build
```

容器第一次启动时自动下载并校验 Runtime；模型和索引保存在名为 `tech-db-runtime` 的持久化卷中，重启不会重复下载。

## 完整离线迁移包

Releases 同时提供分卷的 `tech-db-offline.tar.gz.part-*`。下载全部分卷和 `SHA256SUMS` 后，按 Release 中 `README-OFFLINE.txt` 的命令合并并解压。包内已包含代码、模型和现成索引，不需要访问模型网站；GLM 在线回答仍需要联网和使用者自己的 Key。

## 文件位置

```text
runtime/
├── indexes/              # 向量、BM25、知识图谱与词典
├── models/bge-m3/        # 固定版本嵌入模型
├── state/                # 费用熔断等本机运行状态
└── install-state.json    # 已安装 Release 版本
```

整个 `runtime/` 都被 `.gitignore` 排除。删除它只会删除这台电脑的运行资产，下次启动可重新下载。

## 更新或重建索引

下载当前发布索引：

```bash
python scripts/runtime_assets.py install --components indexes --force
```

从仓库数据重新生成：

```bash
.venv/bin/python qa-backend/vector_index.py
.venv/bin/python qa-backend/bm25_index.py
```

生成结果写入 `runtime/indexes/`，不会意外进入 Git 历史。发布新索引时，由仓库管理员运行 GitHub Actions 中的 **Publish runtime assets** 工作流。

## 公共服务保护

默认限制为单个客户端每分钟 3 次、每天 30 次，全站每天 300 次，同时最多 3 个问答请求。`.env.example` 中的 `QA_*` 变量可修改阈值、设置管理员绕过密钥，并启用每日预估费用熔断。
