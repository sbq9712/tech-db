# Phase 07 完成报告 — Graph serving + relation-aware retrieval (RT-080..RT-087)

评审基线（PHASE07_REVIEW_BASE_SHA）：`e5500c9cb7169c248e4f940af7b8824308b6a3de`
（Phase06 squash merge 后的 sealed main；ACTUAL_START_SHA 与其一致，
工作分支从最新 `origin/main` 新建干净 worktree）。

范围严格限定 RT-080..RT-087，不启动 Phase08，不翻转 Ready，不合并。
本阶段实现**复用**既有 seam，未另建并行存储：类型词汇/校验机制沿用
T044 `relation_ontology`；降级语义落在既有 `runtime_safety`/`route_degraded`
链路；graph 工件随 global manifest 原子发布，请求 pinning 与整 manifest
回滚继承 Phase06 `ReleaseCatalog`/`RuntimeSnapshot`，无第二套权威源。

## 图权威边界（GRAPH AUTHORITY BOUNDARY）

- **图命中本身不是可引用证据**。GraphPath（statement_id `gs-*`、
  snapshot_id `gvs-*`）只作为检索/解释信号；记录级聚合只经由边上的
  EvidenceRefs；测试 `wiring.graph_paths_are_not_citations` 锁定 citations
  中不出现 `gs-`/`gvs-` 前缀。
- 图工件 `graph-snapshot-v2` 内容 hash 封存；篡改在
  `verify_graph_artifact`/`GraphSnapshotView` fail-closed 拒绝。
- 图随 manifest 整体回滚：回滚后 pinned generation 的 graph 视图与
  identity 世代保持绑定（`rt082.manifest_binding_...` 系列用例）。

## 各 Ticket 实现面

- **RT-080** `graph_v2_ontology.py`：版本化 ontology（主版本兼容校验，
  不兼容世代 fail-closed）；`normalize_statement` 产出不可变
  GraphStatement（direction/predicate/evidence_refs/scope/时间性全保留，
  statement_id 由内容派生）；confidence 分层——未 grounding/无 evidence
  refs 封顶 low-tier，WEAK 组（如 RELATED_CO_OCCURRENCE）封顶更低；
  否定/计划性/共现与肯定断言分离，不得进入高置信层。
- **RT-081** `graph_extraction.py`：确定性抽取基线（可审计、gold 可复现；
  LLM 抽取需另过独立验证门）。方向/domain-range 校验失败以
  `DIRECTION_INVALID` 等机器可读 reason code 拒绝，绝不静默翻转；
  未知谓词 `PREDICATE_REJECTED`；候选 span 必须**精确 grounding** 进
  不可变 SourceSnapshot 的 evidence_text（`GROUNDING_MISMATCH` 拒绝）；
  同一 S-P-O 跨记录合并为单 statement 多 EvidenceRefs；任何注入故障
  整体 fail-closed 中止，不发布半成品图。
- **RT-082** `graph_serving.py`：Graph Query Intent 校验（捏造谓词、
  未知关系组、方向、hop 界全部拒绝）；遍历硬上限 2 hop；
  A→B+B→C 不自动推导 A→C（transitive=False 的谓词发现的二跳路径标记
  discovery-only）；不可变 GraphSnapshotView 绑定 manifest。
- **RT-083** `RelationAwareGraphRetriever`：每条路径带特征分解的
  解释性评分（assertion status/polarity/grounding 加成/谓词意图匹配/
  hop 衰减/**hub 惩罚**），彻底取代 Phase06 graph 路由的均匀 +0.35；
  grounded 边只经 EvidenceRefs 聚合记录；时间性过滤（DEPRECATED/
  PLANNED 默认不进 current 意图）；hub trap fixture 证明 hub 节点
  无法靠重复路径刷分（duplicate paths ≠ independence）。
- **RT-084** `evidence_policy.check_relation_method_evidence`：独立
  relation-critical policy gate——Router 声称 SUPPORTED 而无独立图/文本
  支持时硬阻断（`POLICY_RELATION_METHOD_MISSING` +
  router_misclassification_guarded），router 谎报不可信；接入**同一个**
  `EvidencePolicyEngine.evaluate()`（无并行引擎）。
- **RT-085** 生产接线 + benchmark：`server._graph_v2_route` 三重门
  （named profile flag → pinned generation 真实载入图工件 →
  高置信子集资格判定）；flag 开而图未接线 → 显式降级行
  `RUNTIME_GRAPH_V2_NOT_WIRED`（**NOT WIRED = PARTIAL**，绝不静默跳过）；
  `tests_benchmark_phase07.py` 独立锁定 gold fixture（sha256 封存，
  无自验证回路）跑 legacy vs Graph-V2 对比，机器可写
  `benchmark_phase07_result.json`。
- **RT-086** `graph_v2_partial` 命名 profile（`profile_registry_version`
  1.1.0 → 1.2.0）：`QA_GRAPH_V2_ENABLED` 仅经该 profile 开启；
  `partial_activation_decision` 只放行强路由信号 + 高置信实体 seed 的
  查询；legacy graph 路由原样保留为回滚目标。
- **RT-087** `graph_activation.py`：shadow 观测零 serving 变异、事件
  机器可写；激活门规范阈值 **≥1000 events AND ≥7 days**（等价 replay
  需带外审批 token `QA_GRAPH_V2_ACTIVATION_APPROVAL`，本阶段无权自批，
  token 不存在即恒为 `locked_replay_only`）；未达门槛/无增益 →
  `NOT_ACTIVATED_BY_GATE`；即使评估全绿也停在
  `ACTIVATION_ALLOWED_PENDING_RELEASE`。

## 激活声明（ACTIVATION）

- `graph_v2_activation_claim = false`
- `activation_gate_satisfied = false`
- `locked_replay_only = true`
- CI mini-fixture 上的诚实结论为 **NO_GAIN**（legacy ≈ Graph-V2），
  原样写入 benchmark artifact，不粉饰。生产 shadow/canary 未启动；
  RT-087 激活门在获得真实 ≥1000 事件 × ≥7 天 shadow 证据与显式
  带外批准前保持关闭。

## 行为证据

- `qa-backend/tests_remediation_phase07.py`：65 用例全绿（RT-080..087
  单元/集成 + 生产 wiring E2E + server 层诚实降级三态）。
- `qa-backend/tests_benchmark_phase07.py`：8 用例全绿；锁定 fixture、
  hub trap、多跳增益、确定性重放、激活声明恒 false。
- `qa-backend/benchmark_phase07_result.json`：机器可读 artifact。
- `verify_spec_manifest.py` 7/7；`lint_spec_manifest.py` 0 错误组
  （含 selftest 全故障类检出）；acceptance_matrix RT-080..087 全部
  34 个 DoD 映射到真实存在的顶层测试函数（sha 已重算）。
- 必需 CI job：`phase07-graph-serving-relation-aware-retrieval`
  （`.github/workflows/remediation-gates.yml`，既有 gate 全保留）。

## 外部阻塞（未动）

- RT-005（分支保护）仍是 `BLOCKED_EXTERNAL_ACTION`，本报告不声称
  已配置仓库保护策略。
- RT-075 激活阻塞器未翻转。
