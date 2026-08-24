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

