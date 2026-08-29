# Agentic RAG Implementation Status

> TK-21 (Q20/R11) 纠偏版：本文件只描述**结构状态**与**证据入口**。
> 一切数字（测试计数、nightly 指标、延迟分布）以 artifact 为准，
> 本文不手写任何会漂移的数字；nightly 结论一律引用 artifact 路径。

## Overview
Upgrading from "Hybrid RAG" to "Evidence-Centric Adaptive Agentic RAG".
All 24 tickets (TK-01..TK-24) from the planning spec are implemented and
closed; see "Ticket Closure & Evidence Chain" below.

## Verified State (how to reproduce, don't trust prose)
| What | Command | Evidence artifact |
|------|---------|-------------------|
| Full push-tier suite | `python run_all_tests.py --tier push` | `qa-backend/test_summary.json` |
| Nightly tier (shadow + final acceptance) | `python run_all_tests.py --tier all` | `qa-backend/test_summary.json` |
| Spec/manifest validator | `python verify_spec_manifest.py` | exit 0 = PASS (7 checks) |
| Holdout lock + synthetic isolation | `python ../scripts/holdout_run.py --mode full --synthetic-isolation --retrieval` | `qa-backend/test_fixtures/holdout/synthetic_isolation.json`, `retrieval_full.json` |
| Nightly replay (gate-3 primary evidence) | `python ../scripts/nightly_replay.py --tag dayN --commit` | `qa-backend/test_fixtures/holdout/replay/<tag>.json` |
| Live smoke (SSE/search/graph/证据卡片/降级) | server on :8765 → `/api/health`, `/api/search`, `/api/graph`, POST `/api/chat/stream` | `runtime/traces/<date>.jsonl` (per-query stage trace) |

## CI (clickable evidence)
- Workflow: [`.github/workflows/qa-tests.yml`](.github/workflows/qa-tests.yml)
- Push tier runs: https://github.com/sbq9712/tech-db/actions/workflows/qa-tests.yml
  (every push/PR: push suites + validator + holdout smoke, artifact `push-test-summary`)
- Nightly runs: same workflow, `nightly` job (real GLM; artifacts
  `nightly-test-summary`), 23:00 UTC cron + manual dispatch.
- 注：CI 与本仓库远端同步受网络限制时，以本地 artifact 为准（见上表命令）。

## Implementation Structure (all complete, per-module tests in test_summary.json)
- **Phase A 证据链核心**: trace / citation_grounding / claim_mapping /
  verifier(fail-safe) / answer_status(four-state) / epistemic / provenance /
  source_suitability / temporal / entity_resolver / content_safety
- **Phase B 检索与循环**: retrieval/(vector,bm25,graph,rrf_fusion) /
  reranker / evidence_selector / router / decomposer / planner /
  evidence_ledger / evidence_grader / gap_analysis / orchestrator /
  stopping / knowledge_boundary / chunking / numeric_facts /
  conflict_detector / context_builder
- **Phase C 语义图**: semantic_graph / graph_aware / relation_ontology /
  graph_intent
- **Phase D 高级特性**: multi_document / query_integrity / answer_repair /
  entailment / req_fusion / reranker_stability / source_snapshot /
  prompt_injection_eval
- **Phase E 运维**: eval/replay / eval/human_review / degraded_mode +
  budget_guard / release_manifest / trace_retention
- **ER V2**: entity_resolver_v2
- **CI/评测基础设施**: run_all_tests (tier push/nightly/all) /
  verify_spec_manifest / holdout (lock+isolation) / shadow diff /
  nightly_replay / ttfb_guard / gate2+gate3 报告

## Test Summary
以 `qa-backend/test_summary.json`（每次 run_all_tests 生成）为准。
本文不重复具体计数 —— 校验器 V6 会核对 artifact 内部一致性。

## Feature Flags
| Flag | Default | Description |
|------|---------|-------------|
| QA_AGENTIC_ENABLED | true | Full agentic loop |
| QA_ROUTER_ENABLED | true | Adaptive router |
| QA_DECOMPOSITION_ENABLED | true | Query decomposition |
| QA_ITERATIVE_RETRIEVAL_ENABLED | true | Gap-driven retrieval |
| QA_RERANK_ENABLED | true | GLM reranking |
| QA_EVIDENCE_SELECTOR_ENABLED | true | Smart evidence selection |
| QA_EVIDENCE_GRADER_ENABLED | true | Evidence sufficiency grading |
| QA_CLAIM_MAPPING_ENABLED | true | Claim→citation mapping |
| QA_TRACE_ENABLED | true | QA tracing |
| QA_FAIL_SAFE_VERIFY_ENABLED | true | Fail-safe verification |
| QA_CITATION_GROUNDING_ENABLED | true | Citation span grounding |
| QA_ANSWER_STATUS_ENABLED | true | Four-state answer status |
| QA_CONTENT_SAFETY_ENABLED | true | Prompt injection defense |
| QA_PROVENANCE_ENABLED | true | Provenance metadata (TK-06 wave-1) |
| QA_TEMPORAL_ENABLED | true | Temporal reasoning (TK-06 wave-1) |
| QA_ENTITY_RESOLUTION_ENABLED | true | Entity resolution (TK-06 wave-1) |
| QA_SEMANTIC_GRAPH_ENABLED | true | Semantic graph retrieval (TK-06 wave-1) |
| QA_CONTEXTUAL_CHUNKS_ENABLED | true | Contextual chunks (TK-06 wave-1) |
| QA_NUMERIC_FACTS_ENABLED | true | Numeric fact extraction (TK-06 wave-1) |
| QA_CLAIM_GROUNDING_ENABLED | true | Claim-level grounding |
| QA_KNOWLEDGE_BOUNDARY_ENABLED | true | Knowledge boundary message (TK-06/R9) |
| QA_EXACT_GROUNDING_ENABLED | true | Exact-span citation grounding on SourceSnapshot (RT-020) |
| QA_TERMINAL_RENDERER_ENABLED | true | Buffered terminal rendering + post-verification citations (RT-027) |
| QA_EVIDENCE_PACKAGE_ENABLED | false | Phase03 typed EvidencePackage generation path (RT-030..039; on in agentic_full only) |
| QA_GRAPH_V2_ENABLED | false | Phase07 relation-aware Graph-V2 serving route (RT-080..087; on only via graph_v2_partial profile, high-confidence eligible subset) |

## Ticket Closure & Evidence Chain
| Ticket | Scope | Evidence |
|--------|-------|----------|
| TK-01..05 | planning spec → trace/metrics → T003 grounding → T004 claim map → T005 fail-safe verify | `tests_grounding/claim/verify` entries in test_summary.json |
| TK-06..07 | flags wave-1 + knowledge boundary; router heuristic-first | tests_flags_tk06, tests_router_tk07 |
| TK-08 | loop-control hard cap (≤12) + round reservation | tests_budget_tk08; `test_fixtures/gate2_report.json` R1a/R1b/R3a |
| TK-09 | TTFB guard (baseline+Δ, degrade on timeout) | tests_ttfb_tk09; `test_fixtures/ttfb/baseline_legacy.json` |
| TK-10 | GLM failure → UNVERIFIED + user warning | tests_degraded_tk10; gate2 R3c/R3d |
| TK-11 | Gate 2 verification report | `test_fixtures/gate2_report.json` (VERDICT: GATE2_PASS) |
| TK-12 | citation schema (source_label/spans/supports_claim_ids) | tests_citation_tk12 |
| TK-13 | frontend evidence card + warning banner | tests_frontend_tk13 (v=162) |
| TK-14 | spec manifest validator (7 checks) | tests_validator_tk14; `verify_spec_manifest.py` |
| TK-15 | CI tiers (push/nightly) + nightly_eval | tests_ci_tk15; `.github/workflows/qa-tests.yml` |
| TK-16 | holdout set (lock, 100 entries) | tests_holdout_tk16; `test_fixtures/holdout/` |
| TK-17 | shadow retrieval dual-path | tests_shadow_tk17 (nightly tier); `/api/shadow/report` |
| TK-18 | nightly replay (gate-3 evidence) | `test_fixtures/holdout/replay/day1.json` |
| TK-19 | gate 3 PASS → flags on + FAST_RAG fast-path contract | tests_gate3_tk19; `test_fixtures/gate3_report.json` |
| TK-20 | eval-side synthetic isolation (Q19) | tests_synthetic_tk20; `test_fixtures/holdout/synthetic_isolation.json` |
| TK-21 | this document (真实状态纠偏) | `verify_spec_manifest.py` exit 0 |
| TK-22 | sync_local.sh 运维衔接 | `scripts/sync_local.sh` + tests_ci_tk15 |
| TK-23 | legacy retrieval path deletion (post gate 3) | tests_parity (field-level) — new path is the only path |
| TK-24 | final acceptance | `tests_final_acceptance.py` + 完成报告 (见 commit) |
| TK-25 | canonical spec manifest lint（T040 补票：112 票注册表、依赖环/缺票/未知依赖/Phase 冲突/重复 schema/profile 9 类故障检测、spec_hash 防篡改、生产仅允许命名 profile） | tests_spec_lint_tk25; `spec/spec_manifest.json` + `scripts/lint_spec_manifest.py`（CI merge gate） |
| TK-26 | claim-type sufficiency policy registry（T043 补票：10 类 versioned policy，Grader 经 policy_id 判定；厂商自述永不满足 performance、negative claim 只能输出 KB 边界） | tests_sufficiency_tk26; `sufficiency_policies.py` → `evidence_grader.py` Rule 6 |
| TK-27 | span-level source lineage（T048 补票：quote-of-press-release 不算独立验证；同一媒体的独立实测是独立角色；uncertainty 保留；orchestrator 在线建 provenance map） | tests_span_lineage_tk27; `provenance.span_lineage` + `claim_mapping.attach_span_lineage` |

## Nightly Replay 与索引体积豁免（TK-18 / gate-3 证据）
- 真实索引（vector+bm25 约 1.2G）仅存在于本机（gitignored）；CI replay
  使用入库的 MINI 索引 fixture（`qa-backend/test_fixtures/mini_index`）。
- Codex 审查闭环（fa02a5b..e70d0d1 全量分段 A/B1/B2/C1/C2/C3）：44 项发现
  全部修复，rescue 回归（codex 对抗式复核）**44/44 VERIFIED**；全量测试
  409 passed / 0 failed（25 套件）；2026-08-18 T040/T043/T048 补票后 push 级
  353 passed / 0 failed（26 套件，新增 spec_lint_tk25 / sufficiency_tk26 /
  span_lineage_tk27）。
- fixture 重建配方（codex-review C2）：`python scripts/build_mini_index.py
  --from-records` — 只用已入库的 `all-records-mini.json`（manifest
  `records_sha256_16` 摘要校验）重建 vector+BM25 索引，可复现（已验证
  与入库索引字节一致）；默认模式（从 gitignored lite 重新抽样）已对齐
  索引构建器的规范过滤（排除 不相关/未分类/手动导入 与 dp==1）。
- 本地 nightly replay 入口：`.venv/bin/python scripts/nightly_replay.py
  --tag dayN --commit`（artifact: `qa-backend/test_fixtures/holdout/replay/dayN.json`，
  数字一律以该 artifact 为准，本文不引用具体数值）。
- gate-3 时间要求（连续 7 天 nightly replay）按所有者裁决压缩：一次全量
  确定性 replay 作为主证据，检索 shadow（QA_SHADOW_RETRIEVAL=1）在随后
  一周自然流量上继续累积补充证据（`/api/shadow/report`）。
- shadow 期成本（R6/R14）：检索级 shadow 0 额外 LLM 调用；答案级 shadow
  使每 query LLM 成本 ×2（replay artifact 的 `shadow_cost` 字段记录）。

## Gate 3 Decision (TK-19, 2026-08-14)
- **决策：PASS** — 7 个 LLM 依赖 flag（AGENTIC/ROUTER/DECOMPOSITION/
  ITERATIVE_RETRIEVAL/RERANKER/EVIDENCE_GRADER/CLAIM_MAPPING）翻为
  default-on；每个 flag 保留环境变量 kill switch（QA_*_ENABLED=0）。
- 证据链：`qa-backend/test_fixtures/gate3_report.json`（主证据为 replay
  artifact，本文不重复其数字）。
- **FAST_RAG 快路径契约**（rulings Q3 / user story 2）：简单 query 在
  agentic 开启时零 LLM 循环控制成本 —— `planner.create_plan` 对 FAST_RAG
  路由固定 `max_iterations=1`，orchestrator 在 FAST_RAG 模式下跳过 rerank
  与 grader（SUFFICIENT 默认）。契约由 tests_gate3_tk19 固化。
- **epistemic 解析加固**：`_parse_llm_json` 对截断 JSON 在最后一个完整
  元素处截断重闭合，避免 classify→verify 链路静默失效。


## 运维架构（2026-08-18）：systemd 自愈栈

此前进程以 nohup/setsid 从交互 shell 启动，活在 logind session scope 里；
WSL 最后一个会话关闭时 systemd-logind 杀掉整个 scope（journal 证据：
session c2 在每次进程团灭时刻被 Removed）—— 与 VM 是否存活无关。
现全部改为 systemd **user units**（`ops/systemd/`，`loginctl enable-linger`）：

| Unit | 作用 | 节奏 |
|------|------|------|
| techdb-data-sync | git pull + lite 重建 + BM25 | timer 30min |
| techdb-vector | 向量索引增量 embedding（完成后自动重启 server 加载） | timer |
| techdb-graph | 精选记录知识图谱增量入库（完成后自动重启 server） | timer 45min |
| techdb-server | Q&A 后端 :8765 + 门户 :8097 | Restart=always |
| techdb-tunnel | cloudflared 快速隧道 + qa.js URL 自动同步推送（含 rebase 重试） | Restart=always |

CI 侧（auto-sync）与本地侧（data-sync/vector/graph）形成闭环：
CI 每 4h 推数据 → 本地 30min 内自动跟进索引与图谱 → 隧道 URL 变更自动推送。
验证：进程 cgroup 在 `user@1000.service/techdb-*.service`（非 session scope），
linger 已启用（VM 启动即拉起全部服务）。

## 后续 Ticket（spec 明确范围外）
- LightRAG ingest 恢复（剩余 curated 候选，用户原话"Q5之后再说"）——
  本次按裁决作为"AI精选+精选情报"图谱构建后续工作单执行。
- **图谱构建完成（2026-08-16）**：8202/8202 条 AI精选∪精选情报 记录全部
  入库（主跑 5342 + 两轮重试 185→19→4 + 4 条收尾）；最终图
  **74987 nodes / 96702 edges / 74991 entity→record 映射**。
  export 拓扑修复：`GraphBuilder.export()` 增加悬垂边过滤（节点名 >50 字符
  被拒的实体，其边在导出时丢弃，272/96974=0.28%）——
  final_verification **19/19 PASS**（含 Edge connectivity 0 broken）。

## TK-23 Contract Phase (2026-08-14) — legacy retrieval deleted
- **已删除**：`server.py` 旧内联检索实现与 `QA_RETRIEVAL_LEGACY` 逃生口。
  `_search_with_quality_legacy` 仅保留为 raising stub（防静默回退）。
- **kill switch 语义变化（Q2，contract 后）**：
  - contract 前：`QA_RETRIEVAL_LEGACY=1` 可切回旧路径（expand 阶段逃生口）。
  - contract 后：逃生口不复存在。回滚 = `git revert` TK-23 提交。
  - 功能级回滚继续使用 flag 语义（`QA_*_ENABLED=0`，21 个 flag 均在）。
- **shadow 演化**：QA_SHADOW_RETRIEVAL 从"双路径对比"转为"漂移监视"——
  live 检索层 vs 冻结的 gate-3 参考 artifact
  （`qa-backend/test_fixtures/holdout/shadow_diff_full.json`，最后一次在
  legacy 存在时记录的 per-query top-25 ids）。不在参考集的 query 只记录
  延迟/相关性。报告字段 `reference` 标明来源。
- **nightly_replay 演化**：legacy 腿同样改为冻结参考（overlap = live vs
  frozen reference）。历史 day1.json 保留 dual-path 形态（gate-3 证据）。
- **parity 保证不变**：tests_parity 对冻结 gate-1 基线（0 drift）+
  字段级 route-score 校验继续守护 seam。
- 回归：push 324/324（23 suites）+ nightly 80/80。

## Phase 02 (2026-08-18) — citation/claim/state verifier chain (RT-020..RT-029)

分支 `remediation/phase-02-citation-claim-state-verifier`，基线 cdc5896。
完成证据见 `docs/remediation/phase02_completion_report.md`：
- RT-020 精确 grounding（EXACT/INVALID 二值、多 span、NFKC/fuzzy 定位后回落精确 locator）
- RT-021 类型化关系（DIRECT/PREMISE/ATTRIBUTION 支持类；BACKGROUND/CONTRADICTS 永不支持；vendor/self-report 上限 ATTRIBUTION）
- RT-022 数值事实溯源（evidence_ref + transform_rule_version；Gb/s vs GB/s、per-device vs 整机不混淆）
- RT-023 claim coverage gate（未映射事实句阻断 SUPPORTED；hedged/attributed 仍算 claim-bearing）
- RT-024 AnswerStateMachine v2.0.0（唯一状态权威；NOT_RUN 初值；技术失败→UNVERIFIED）
- RT-025 fail-safe verifier（timeout/429/5xx/malformed/exception→UNVERIFIED；结构化 findings，无 rewritten_answer）
- RT-026 有界修复环（≤2 轮；core claim 永不删除；确定性终止）
- RT-027 终端渲染器 + 后验证 SSE（事实草稿缓冲；citations 事件仅验证后；ttfs/ttfa 入 trace）
- RT-028 引用 schema 2.0.0（snapshot_id/locators/support_relations/degraded/diagnostics；旧字段兼容）
- RT-029 前端证据态渲染（schema 版本失效、关系 chip、UNVERIFIED 横幅、locator chip）
验收矩阵：36 个 legacy DoD 依 Phase-02 证据升级 SATISFIED；6 个诚实保持 NOT_SATISFIED；T037 硬门禁未动。

## Phase 03 (2026-08-18) — retrieval→EvidencePackage chain (RT-030..RT-039)

分支 `remediation/phase-03-retrieval-evidence-package`（Draft PR #4），评审基线 2869742。
完成报告见 `docs/remediation/phase03_completion_report.md`。

### 评审 round 1 整改（10 blockers，全部闭环）

1. **RT-030 权限与 parity**：删除 server.py L197 处对 injectable 检索 wrapper 的
   覆盖（`vector_search = _rt.vector_search` 等三行）——patched
   `server.embedding_func` 重新生效，CI（无 torch）不再 ModuleNotFoundError；
   恢复 Phase02 语义 `is_relevant = strong_vector OR strong_graph`
   （移除未授权 `or bm25` 分支）；回归测试锁定（bm25-only 不翻转相关性、
   strong vector 仍翻转、embedding seam 探针）。
2. **RT-031 生产路径高召回**：新增 `retrieval.runtime.run_routes`
   （per-route 真排名 1-based、exclude 过滤、ROUTE_TOP_K=50 上限）；
   `_run_phase03_context` 以 raw route 结果为 pool 源（不再消费
   run_hybrid 已截断的 Top25）。生产 E2E：融合排名 34（>25）的目标记录
   在 legacy 表面被丢弃，却经真实 server 路径进入 rerank→selection→citations。
3. **RT-033 真实输入**：comparison objects/dimensions 来自显式对比句式
   （vs/对比/和…哪个 + 维度模式），provenance 来自 Phase-02
   `cluster_provenance`（idx→record_id 重映射），independent groups、
   route outlier（raw route 单路强信号 + rrf_rank>25）全部接入；
   关键词由查询内容词确定性派生（Phase-04 分解边界如实文档化）。
4. **RT-034 硬规则全量接线**：pipeline 产出 `evidence_eligibility` 统一键；
   GATE A（选择前：QUARANTINED/RETRIEVAL_ONLY/ACCESS_SCOPE/synthetic-only/
   无 pinned 权限）+ GATE B（选择后：HIGH 冲突/数值自矛盾/关系断言对
   temporal intent 无效/self-report-only/superseded-only）→ blocked 记录降级
   CONFLICT/INVALID、claim 级阻断清空 support → `no_evidence` 显式降级。
   生产 E2E 负例 8 连（隔离/仅检索/越权访问 scope+匹配放行对照/自述/
   被取代/高冲突双降级/数值无效/关系无效）全部通过真实 server 路径验证。
5. **RT-035 选择前后双 policy**：选择集只从 policy-cleared 候选产生；
   注入/未选择内容 E2E：未选择 sentinel 不进入渲染上下文，选中证据
   一律 DATA 边界包裹。
6. **RT-032 合成内容隔离**：synthetic-only（仅 meta["as"] 可解析内容）
   候选 rerank 分恒 0、`counts_as_evidence=False`；GLM 路径将其隔离出
   listwise 输入并在回填时强制 0 分——任何 top_k 截断下都无法挤掉
   source-grounded 证据。
7. **trusted 模式 fail-closed**：`Phase03AuthorityError`；无 pinned
   snapshot / pinned catalog 为空 → 拒绝生成（不再制造 `rec:<id>` 快照 id）。
   进程内 + 真实 SSE 端点双重 E2E（stop_reason=
   `phase03_missing_pinned_authority`）。
8. **RT-038 hash 绑定最终对象**：`PackedGenerationView`（不可变视图，
   view_hash 绑定压缩/裁剪后的精确内容；canonical package 不可变）；
   压缩/丢弃/abstain 全路径 hash 一致性 4 用例锁定。
9. **benchmark 生产路径化**：`tests_benchmark_phase03.py` 新增
   production-path benchmark（真实 pinned release →
   `server._run_phase03_context`）：rank26 生存率 1.0、selector 覆盖 1.0、
   延迟差值如实上报（legacy 1.25ms vs phase03 19.6ms，阈值 ≤max(50ms,4×)）。
10. **CI 全绿**：push tier 757/757（33 suites），phase03 112/112，
    benchmark 3/3，parity 5/5，lint/verify spec 通过；
    acceptance_matrix 新增 40 个生产路径用例登记（sha 重算）。

### Phase 03 交付面（第一轮 + 整改合并）
- RT-030/031/032/033/034/035/036/037/038/039 全绿（详见完成报告）。
- EvidencePackage schema 3.1.0（policy_reasons、NON_SUPPORT_RELATIONS、
  view 绑定）；pipeline 3.1.0；Citations 携带 evidence_id/source_snapshot_id。
- 生产语义：FAST_RAG 池上限 80 / RESEARCH·DEEP 180、ROUTE_TOP_K 50、
  RERANK_CAPACITY 40、MAX_EVIDENCE_SLOTS 15。

### Phase 03 — 第二轮验收整改（2026-08-24，blockers A/B/C/D）

1. **Blocker A（RT-031 全端点预门控顺序）**：`/api/chat/stream` 中
   Phase03 块移到 legacy weak-query 门控之前——`EVIDENCE_PACKAGE_ENABLED`
   开启时，legacy Top25/`is_relevant` 只是 legacy 决策面，绝不再作为
   权威预门控终止请求；Phase03 的 `no_evidence`/policy/容量结果就是
   证据决策。四态答案状态同步修正（`has_results`/`is_relevant` 以
   Phase03 激活为准），legacy weak_query 不再污染成功回答的状态。
   关闭 flag 时路径字节兼容（legacy 配置零削弱、FINAL_TOP_K 不变）。
   新增 FULL HTTP/SSE E2E（真实 TestClient + RuntimePinMiddleware 路径）：
   全向量 cos<0.55 的 fixture（legacy 必拒）下 Phase03 照常产出
   EvidencePackage，rank34 目标进入 selection/citations/generation 上下文，
   上下文只含选中证据；同一 fixture 关 flag 后仍走原样 weak_query 拒答。
2. **Blocker B（RT-033 对象×维度配对预留）**：`apply_reserve` 新增
   PAIR 级预留（`RESERVE_COMPARISON_OBJECT_DIMENSION`，key=`obj|dim`
   双轴稳定键）；候选必须同时内容命中两轴且带真实路由信号
   （`requirement_matched=False`，纯 token 匹配的垃圾不可存活）。
   A/B/C 失衡 fixture（alpha 独占 30 容量头 + 单轴槽位被消耗 + B/C
   落在 31..34 位）下，预留+容量交换后 6 对全部有幸存者；关闭配对
   预留（`QA_RESERVE_PAIR_ENABLED=0`/ablation seam）同 fixture 必败。
3. **Blocker C（RT-034 provenance + 实体/维度硬规则）**：同一个
   `EvidencePolicyEngine.evaluate()`（FAST/RESEARCH/DEEP 共用，无并行
   引擎）新增 `check_provenance`（同一 `independent_group_id` 的转发/
   重复稿只算一个独立来源；`POLICY_PROVENANCE_INSUFFICIENT`）与
   `check_entity_coverage`（`POLICY_ENTITY_MISSING`/
   `POLICY_DIMENSION_MISSING`，含 obj×dim 配对覆盖）。结构化输入
   真正可用时确定性检查；不可用（Phase04 未产出）时在
   `rule_applicability` 如实记 `NOT_APPLICABLE`——不伪造通过、不静默
   跳过。生产路径 E2E：同一 wire URL 的转发簇在独立性查询下
   no_evidence，不同 wire 的正控通过。
4. **Blocker D（RT-038 packed-view 证据语义）**：`support_evidence_ids`
   只解析到 `counts_as_evidence=True` 且 relation ∈ SUPPORT_RELATIONS
   的条目；压缩导航卡不再是可信支持——非关键需求因打包失去唯一支持
   时 coverage 如实降为 MISSING/GAP；mandatory 保持原文或显式
   `context_capacity_exceeded` abstain。`validate()` 强化：拒绝
   非证据支持引用、非支持关系、COVERED 但零证据支持、mandatory 被
   压缩（无显式 abstain）、悬空引用、陈旧 hash。
   `binding_payload` 绑定全部 Generator 可见字段（schema、query、
   gaps、degraded_capabilities、selection_floor、完整 capacity、
   dropped_ids 等）。渲染端导航卡只出现在独立非证据区块，
   需求区只渲染证据条目（缺失即明示"缺失证据"）。3 组 mutation
   用例（degraded 变更→stale hash；压缩支持不可信；导航卡不作为
   证据渲染）全部锁定。
5. **回归保持**：第一轮已接受项零重写；push tier 784/784
   （33 suites，+27 用例），phase03 139/139，benchmark 3/3，
   parity 5/5，verify_spec_manifest 7/7；acceptance_matrix 登记
   RT-031.DOD-04 / RT-033.DOD-03 / RT-034.DOD-03 / RT-038.DOD-04
   （acceptance_matrix_sha256 与 spec_hash 已重算）。

## Phase 04 (2026-08-24) — query integrity and agentic orchestration (RT-040..RT-049)

实现基于现有 server + Phase02 verifier + Phase03 EvidencePackage 管线：
claim 级可信会话前提、确定性 rewrite semantic diff、FAST 正确性路径、
typed ResearchState、严格 Planner、隔离 document workers 与 scoped cache、
Ledger/Grader 硬规则、gap-bound retrieval、bounded stopping/Knowledge
Boundary。行为证据与诚实的 rollout 边界见
`docs/remediation/phase04_completion_report.md`。本阶段不声明生产
shadow/canary 或 Graph-V2 激活；RT-005 仍是外部动作 blocker。

## Phase 07 (2026-08-27) — Graph serving + relation-aware retrieval (RT-080..RT-087)

评审基线 e5500c9c（Phase06 sealed main）。完成报告见
`docs/remediation/phase07_completion_report.md`。要点：
- 版本化 ontology + 不可变 GraphStatement（RT-080）；方向/谓词/精确
  grounding 校验的确定性抽取基线，故障注入整体 fail-closed（RT-081）。
- Graph Query Intent 校验、2-hop 硬上限、非传递 discovery-only（RT-082）。
- 关系感知检索器：解释性 per-path 评分 + hub 惩罚 + EvidenceRefs-only
  记录聚合，图命中本身不是可引用证据（RT-083）。
- 独立 relation policy gate 接入同一 EvidencePolicyEngine，router 谎报
  SUPPORTED 被硬阻断（RT-084）。
- 生产三重门接线（flag→pinned 图工件→高置信子集资格），未接线显式
  降级 RUNTIME_GRAPH_V2_NOT_WIRED；锁定 gold benchmark，CI mini-fixture
  诚实结论 NO_GAIN（RT-085）。
- `graph_v2_partial` 命名 profile（profile_registry_version 1.2.0，
  RT-086）；激活门 ≥1000 events × ≥7 days 规范阈值 + 带外审批 token，
  CI 无法自批，`graph_v2_activation_claim=false`、
  `activation_gate_satisfied=false`、`locked_replay_only=true`（RT-087）。
- 行为证据：tests_remediation_phase07 65/65、tests_benchmark_phase07
  8/8、`benchmark_phase07_result.json`；CI job
  `phase07-graph-serving-relation-aware-retrieval`。

## Phase 08 (2026-08-28) — API, UI, Trace, Replay (RT-090..RT-094)

从 sealed Phase07 main `c4c3e00f` 开始，复用现有 AnswerStateMachine、
EvidenceRefs、Trace/retention、replay、Human Review、holdout lock、operator
auth 与前端引用路径，完成统一 terminal contract、claim-aware 精确 span
ReferenceCards、四种诚实 replay fidelity、development regression 与 locked
holdout 隔离，以及 server-side operator audit/trace policy。完成报告见
`docs/remediation/phase08_completion_report.md`；行为套件为
`qa-backend/tests_remediation_phase08.py`，required CI job 为
`phase08-api-ui-trace-replay`。Repair Round 1 将 terminal 的 verification /
stop reason 统一绑定 canonical snapshot，关闭 chat JSON scope 自提升，并让
case-group 默认执行真实 current Phase03 pipeline；本地证据 78/78，push tier
1287/1287（42 suites）。Graph-V2 仍未激活，RT-005/RT-075 仍为外部事项，
Phase09 未开始。
