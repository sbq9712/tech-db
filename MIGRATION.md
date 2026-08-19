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

## Runtime profile migration

The published `runtime-v1` release contains the current pickle indexes and
`bge-m3` model, but it does not contain the Phase-01 immutable manifest
catalog/current pointer and complete versioned JSON artifact set. Therefore all
current Docker, shell, Windows and systemd launchers explicitly configure:

```text
TECH_DB_RUNTIME_MODE=legacy_hybrid
QA_PIPELINE_PROFILE=legacy_hybrid
```

This is a named deployment profile, not an exception fallback. The server
rejects an unset/unknown mode. Selecting `manifest` enables strict validation;
a missing current pointer, incompatible schema or damaged artifact fails cold
startup and never silently falls back to `legacy_hybrid` or `previous`.

`QA_PIPELINE_PROFILE` is applied at `feature_flags` import — before any flag
consumer — and an explicitly-set `QA_*` env var that deviates from the
declared profile is a fail-closed startup error. `legacy_hybrid` pins the
pre-Phase-02 deployed activation state: shipped agentic/correctness flags
keep their gate-3 defaults (on) and only the Phase-02 flags
(`QA_EXACT_GROUNDING_ENABLED`, `QA_TERMINAL_RENDERER_ENABLED`) are off.
Applying the profile therefore changes nothing the deployment already ran
except disabling the two new Phase-02 capabilities.

### Activation gate: `legacy_hybrid` → `manifest`

Do not switch production merely because Phase-01 unit/integration tests pass.
Activation requires all of the following:

1. publish a complete immutable manifest generation containing the dataset,
   RecordIdMap, source/identity/metadata catalogs, primary indexes, prompts and
   model/config declarations with accepted schemas and SHA-256 hashes;
2. pass strict cold-start, request-generation pinning, rollback and restore
   rehearsal against that exact generation;
3. run shadow comparison against `legacy_hybrid`, document parity/approved
   deltas, invalid-citation count and operational error/latency results;
4. complete an explicitly approved production canary with monitoring and a
   complete-manifest rollback target;
5. record the activation decision and set `TECH_DB_RUNTIME_MODE=manifest`,
   `TECH_DB_RELEASE_ROOT`, and `TECH_DB_RELEASE_CATALOG_DIR` in deployment
   configuration before restart.

No production shadow/canary is claimed by the current PR. Per decision Q015,
retain `legacy_hybrid` through canary and at least two stable production
releases after full manifest activation; removal requires a separate approved
deprecation change.

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

从仓库数据重新生成（**必须先做稳定身份迁移**）：

仓库数据 `data/processed/all-records-lite.json` 是历史遗留的位置列表，记录本身不带稳定 `record_id`。向量/BM25 构建器拒绝输出没有稳定 ID 的元数据，因此直接运行会失败。正确的路径是显式迁移（`techdb-vector.timer` 触发的 `qa-backend/vector_index.py` 与 `scripts/boot_sync.py` 中的 `qa-backend/bm25_index.py` 都走同一条链）：

```text
legacy 数据集（保持原格式，不重写）
  → 显式稳定身份迁移（SourceIdentityKey / RecordRegistry：
    上游 ID > URL > legacy_source_key；绝不使用列表位置或正文相似度）
  → 数据集字节钉扎(sha256)的 RecordIdMap sidecar
    （runtime/state/record_id_map.json）
  → 稳定 ID 装饰的 BUILD VIEW（副本；legacy 文件不修改）
  → vector / BM25 构建器
  → 输出元数据携带真实稳定 record_id
```

```bash
# 1) 迁移（幂等：同一数据集重跑完全复用既有 ID；换序不改变任何
#    记录的 ID；同文不同源不会被合并；无审计身份的记录 fail closed）
.venv/bin/python qa-backend/index_build_view.py \
  --registry runtime/state/record_registry.sqlite \
  --output  runtime/state/record_id_map.json

# 2) 重建索引（消费经过验证、与数据集字节钉扎一致的 map）
.venv/bin/python qa-backend/bm25_index.py
.venv/bin/python qa-backend/vector_index.py
```

失败即闭（fail closed）规则：

- map 缺失 / 损坏 / 与当前数据集 sha 不一致 / 覆盖不全 / 一对多解析
  → 构建器直接失败并提示上面的迁移命令，绝不退回旧索引构建器，
  也绝不生成 `legacy-idx:<n>` 之类的临时 ID；
- 每条 canonical 记录必须唯一解析到一个稳定 `record_id`；
- 无可审计源身份（无上游 ID / URL / legacy key）的记录默认失败；
  如需放行必须显式隔离（`--quarantine`）并写入可审计清单，
  绝不偷偷生成不可重放的随机 ID；
- **同一 URL 下多条不同标题的记录**（聚合页/DOI 归属错误）不会自动
  合并也不会自动拆分：必须有提交在仓库里的人工消歧清单
  `data/processed/identity_disambiguation.json`（为每条记录指定唯一
  的 `legacy_source_key`），否则迁移失败并列出待整理条目；
  **同一 URL 且同一标题**的记录视为同一逻辑记录的重复导入：
  首次出现为准，后续作为显式 `duplicate_of` 排除（审计在 map 中）；
- 数据集变更后（sha 变化）必须重跑迁移：身份注册表会为新增来源
  分配新 ID、复用既有来源的旧 ID。

行为级回归见 `qa-backend/tests_index_migration.py`（CI gate
`legacy-index-migration`）。生成结果写入 `runtime/indexes/`，
不会意外进入 Git 历史。发布新索引时，由仓库管理员运行 GitHub
Actions 中的 **Publish runtime assets** 工作流。

## 公共服务保护

默认限制为单个客户端每分钟 3 次、每天 30 次，全站每天 300 次，同时最多 3 个问答请求。`.env.example` 中的 `QA_*` 变量可修改阈值、设置管理员绕过密钥，并启用每日预估费用熔断。
