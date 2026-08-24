# Phase 03 完成报告 — retrieval → EvidencePackage 链路（RT-030..RT-039）

分支：`remediation/phase-03-retrieval-evidence-package`（Draft PR #4）
评审基线（round 1 HEAD）：286974225283cc7b02c4f47a64ccb1f3a80a9645

## 概览

Phase 03 交付"高召回检索 → 内容重排 → 硬规则 policy → 证据选择 →
不可变 EvidencePackage → hash 绑定生成视图"的完整生成前证据链，
并以 production-path E2E 锁定每一步的真实 server 语义。

- 测试：`tests_remediation_phase03.py` 112/112（含 21 个生产路径 E2E）；
  `tests_benchmark_phase03.py` 3/3 + production benchmark 3/3 指标全过。
- push tier：757/757（33 suites，0 fail）；parity gate-1 基线 0 drift。
- 规格工件：acceptance_matrix 登记全部新用例（RT-030..039 DOD 级），
  spec_manifest sha/spec_hash 重算，lint + verifier 通过。

## 逐 ticket 证据（第一轮交付 + round 1 整改）

### RT-030 检索层抽取与 parity
- injectable wrapper 恢复（删除 server.py 覆盖行）；patched
  `server.embedding_func` 生效（CI 无 torch 可跑）。
- Phase02 相关性语义恢复：`strong_vector OR strong_graph`（BM25_STRONG
  仅诊断面）。回归：bm25-only 不翻转、strong vector 翻转、seam 探针。
- 生产 E2E：`prod.raw_routes_cover_full_corpus` / `prod.legacy_top25_drops_target`。

### RT-031 高召回池（无全局 Top25 截断）
- `retrieval.runtime.run_routes`：per-route TRUE rank（1-based）、
  exclude_ids、`route_fetch_caps`（默认 ROUTE_TOP_K=50，env 可覆盖）。
- 生产 E2E（34 文档语料，目标融合排名 34）：legacy Top25 丢弃目标 ↔
  生产路径 selection+citations 命中目标，pool_size_routes==34。
- benchmark：candidate_survival 1.0（vs baseline 0.0）。

### RT-032 内容感知重排
- `rerank_local`：lexical F1+短语奖励，逐候选独立打分（batch 稳定）。
- synthetic-only 隔离（round 1 blocker 6）：分恒 0.0、
  `content_basis=synthetic_hint_only`、`counts_as_evidence=False`；
  GLM listwise 输入剔除 synthetic 候选，回填 0 分——任何截断下都排
  source-grounded 之后。
- 测试：`synthetic_only_gets_zero_and_flagged`、`synthetic_cannot_win_rerank`、
  `glm_success_still_quarantines_synthetic`。

### RT-033 需求/路由配额保留
- 输入全部真实：comparison objects/dimensions（显式句式）、
  provenance（Phase-02 cluster_provenance → record_id 重映射）、
  independent groups、route outlier（单路强信号 + rrf_rank>25）。
- 查询关键词确定性派生（≥3 CJK/拉丁内容词）；单关键需求回退是
  Phase-04（RT-040+ 分解）边界，如实文档化、不虚构。
- 测试：配额四触发 + 生产抽取 + pipeline 接线共 6 用例。

### RT-034 确定性硬规则引擎
- 键统一 `evidence_eligibility`；GATE A（选择前）+ GATE B（选择后）；
  blocked 记录 → CONFLICT/INVALID 关系、claim 级 → 清空 support；
  `no_evidence` 显式降级并携带 reason codes（两条路径均机器可读）。
- 生产 E2E 负例：QUARANTINED、RETRIEVAL_ONLY、ACCESS_SCOPE（含匹配
  scope 放行对照）、self-report-only、superseded-only、HIGH 冲突双降级、
  数值自矛盾、DEPRECATED 关系断言 —— 8/8 通过真实 server 路径。

### RT-035 证据选择
- 选择集只含 policy-cleared 候选；选择后 GATE B 再校验；
  contamination E2E：未选择 sentinel 不入渲染上下文、选中证据 DATA 边界包裹。

### RT-036 chunk 路由（父定位）
- 精确 EvidenceLocator（chunk id/snapshot id/offsets/sha）；
  篡改 sha fail-closed；mini_runtime fixture 一致性。

### RT-037 不可变 EvidencePackage（schema 3.1.0）
- 确定性 package_hash；policy_reasons/block 记录入包；
- trusted 模式权限（round 1 blocker 7）：`Phase03AuthorityError`
  fail-closed —— 无 pinned snapshot / 空 catalog 拒绝生成；
  进程内 + SSE 端点双 E2E（`phase03_missing_pinned_authority`）。

### RT-038 容量适配与最终对象 hash
- `PackedGenerationView`：不可变视图 + view_hash 绑定压缩/裁剪后内容；
  mandatory 永不静默截断（abstain 显式化）；压缩文本非证据；
  4 用例锁定（view_hash 绑定、压缩不留陈旧 hash、丢弃不悬空、
  冲突关键证据保留）。

### RT-039 生成输入 allowlist
- GeneratorInput 类型门（EvidencePackage | PackedGenerationView）；
  渲染只读 allowlist 字段；渲染含 view_hash（blocker 8 语义）；
  数据边界包裹全部证据 payload。

## Production benchmark（blocker 9）

`tests_benchmark_phase03.py::test_phase03_production_benchmark`
（产物 `qa-backend/benchmark_phase03_production_result.json`）：
- 6 探针 × 34 文档真实 pinned release（load_release_resources →
  RuntimeSnapshot → server._run_phase03_context）。
- 场景前置成立 1.0；legacy Top25 丢弃 1.0；prod rank26 生存 1.0；
  selector 覆盖 1.0；延迟 legacy 1.25ms vs phase03 19.57ms
  （阈值 ≤ max(50ms, 4×legacy)，实测满足，比值如实上报）。

## 已知边界（如实声明）

1. 需求分解（多 requirement/深实体）是 Phase-04（RT-040+）范围；
   Phase03 单关键需求回退不虚构任何需求。
2. GLM listwise rerank 在 CI（无网络/无 key）由 reranker 内部降级为
   pool-order 打分；生产 E2E/benchmark 用 FAST_RAG（本地确定性引擎）
   锁定内容感知契约。
3. 访问 scope 规则沿用引擎评审语义：请求 scope 非 public 时才比对；
   public 请求不触发（引擎行为保持与 Phase-02 评审一致）。
4. nightly `final_acceptance`（DoD 总账）仍为 NOT READY —— Phase04+
   tickets 未完成所致，与 Phase03 无关（push tier 全绿）。


## 第二轮验收整改（2026-08-24 — blockers A/B/C/D）

### A. RT-031 全端点预门控（server.py）

- Phase03 块移动到 legacy `if not search_results or not is_relevant`
  弱查询门控之前；`_phase03_active` 时门控整体旁路。
- 四态答案状态：`determine_answer_status` 的 `has_results`/`is_relevant`
  在 Phase03 激活时以证据决策为准（legacy 判定不再污染状态）。
- flag 关闭：路径与重构前等价（legacy 配置/Top-K/相关性配置零变化）。
- FULL HTTP/SSE E2E（`RT031.round2_*`，6 用例）：TestClient +
  `RuntimePinMiddleware` 真实路径（FakePinManager 提供与生产一致的
  request-pinned snapshot），fixture 全向量 cos<0.55（legacy 必拒）+
  目标融合 rank34（legacy Top25 之外）：
  - `RT031.round2_fixture_trips_legacy_gate`：前置成立（run_hybrid 实测
    is_relevant=False 且结果非空）；
  - 端点不再在 Phase03 前以 weak_query 退出（status/stop_reason 均非
    UNSUPPORTED/weak_query，无 error 事件）；
  - rank34 目标进入 citations/searched ids；
  - 生成 system_prompt 只含选中包证据（record= 头 ⊆ citations，
    未选择池成员 sentinel 不出现）；
  - 同 fixture 关 flag → 原 weak_query 拒答（legacy 字节兼容实证）。
- 直连生产 benchmark（`tests_benchmark_phase03.py`）保持不变并复跑
  通过（rank26 生存 1.0 / selector 覆盖 1.0 / 延迟如实）。

### B. RT-033 对象×维度配对预留（retrieval/reserve.py）

- 新增 `RESERVE_COMPARISON_OBJECT_DIMENSION`（key=`{obj}|{dim}`）：
  候选必须内容同时命中两轴 **且** 带真实路由信号
  （`_eligible(requirement_matched=False)`——纯 token 命中不豁免
  eligibility floor，垃圾不可存活）。
- 失衡 fixture（`RT033.round2_pair_reserve_*`，4 用例）：
  alpha 占满 30 容量头，beta/gamma 单轴预留槽位被无维度 token 的
  头部填充消耗，B/C 四个配对候选位列 31..34；配对预留 + 容量交换
  后全部 6 对 ≥1 幸存者（`pool_with_reserves` 确定性交换）；垃圾
  （beta+latency token、零信号）被 `REJECT_BELOW_ELIGIBILITY_FLOOR`
  拒绝；关闭配对预留（`_PAIR_RESERVE_ENABLED`/`QA_RESERVE_PAIR_ENABLED`）
  后同 fixture 4 个 B/C 对全部归零（必需的 ablation 失败）。
- 既有单轴/独立源/异常路由预留测试原样保留，全部通过。

### C. RT-034 provenance + 实体/维度硬规则（evidence_policy.py + pipeline）

- 同一个共享引擎 `evaluate()` 新增：
  - `check_provenance`：同一 `independent_group_id` 的转发/重复稿
    折叠为单一独立来源；独立性要求下 distinct groups <
    `min_independent_groups`（默认 2）→ `POLICY_PROVENANCE_INSUFFICIENT`
    （hard，claim 级，无法被任何模型翻转）。
  - `check_entity_coverage`：required entities/objects/dimensions +
    选中证据文本确定性覆盖检查；对象缺失 → `POLICY_ENTITY_MISSING`，
    维度/对象×维度配对缺失 → `POLICY_DIMENSION_MISSING`。
  - `PolicyReport.rule_applicability`：结构化输入不可用（Phase04 未
    产出）时记 `NOT_APPLICABLE: ...`——不伪造、不静默声明通过；
    `not_applicable_rules()` 机读。
- pipeline（唯一 engine 实例）接线：required_objects/dimensions 来自
  确定性 comparison 派生，selected texts 来自选中记录 content，
  provenance groups 来自 Phase-02 聚类重映射；三个新 code 进入
  `_CLAIM_LEVEL_CODES`（claim 级阻断所有派生需求的 support）。
- 用例（10 个）：同簇 3 转发 HARD_FAIL / 异簇 PASS / 不可用
  NOT_APPLICABLE 如实 / 实体缺失 / 配对缺失 / 无结构输入
  NOT_APPLICABLE / pipeline 派生接线（beta 缺失 → no_evidence +
  机读 code）/ pipeline 正控（覆盖齐全 → ok，双引用）/ 生产 pinned
  转发簇（同 wire URL）no_evidence / 生产正控（不同 wire ≥2 引用）。

### D. RT-038 packed-view 证据语义（evidence_package.py + generator_input.py）

- `fit_to_capacity._mk_view`：`support_evidence_ids` 只保留
  `counts_as_evidence=True` 且 relation ∈ SUPPORT_RELATIONS 的条目；
  覆盖从打包后实际证据支持重算（唯一支持被压缩/丢弃的非关键需求
  → MISSING/GAP）；导航卡保留在视图的非证据表示中。
- `validate()` 强化：非证据支持引用 / 非支持关系 / COVERED 零证据
  支持 / mandatory 压缩（无显式 context_capacity_exceeded）/ 悬空
  mandatory/conflict/condition 引用 / 陈旧 view_hash —— 全部拒绝。
- `binding_payload` 扩展绑定：schema_version、query、gaps、
  degraded_capabilities、selection_floor、完整 capacity dict、
  dropped_ids（原有：canonical hash、evidence 文本 hash、压缩/计数/
  relation/policy_reasons、requirements、mandatory、conflicts、
  conditions）。任何 Generator 可见字段变更 → stale hash。
- 渲染端（`render_generator_prompt`）：需求区只渲染
  counts_as_evidence 且未压缩条目（缺失即"⚠️ 缺失证据"）；压缩卡
  只出现在独立"【导航卡片（非证据，仅指针 — 不得引用为证据）】"区块。
- 用例（7 个）：压缩支持不可信（ID 剔除+validate 干净）/ 覆盖重算 /
  手工篡改（回填压缩 ID + COVERED + 重算 hash）双重拒绝 / 非支持
  relation 拒绝 / degraded 变更 stale hash mutation / mandatory 压缩
  拒绝 / 生成器永不把导航卡渲染为证据（区块边界断言）。

### 回归与登记

- push tier **784/784**（33 suites，+27 round-2 用例）；phase03
  **139/139**；benchmark 3/3；parity 5/5；verify_spec_manifest 7/7。
- 第一轮已接受区域零重写（RT-030/032/035/036/037/039、authority
  fail-closed、quarantine 接线、合成隔离、不可变 canonical 方向、
  757→784 基线只增不减）。
- acceptance_matrix 新增 `RT-031.DOD-04` / `RT-033.DOD-03` /
  `RT-034.DOD-03` / `RT-038.DOD-04`（27 个用例登记，
  `acceptance_matrix_sha256`、`spec_hash` 重算）。
