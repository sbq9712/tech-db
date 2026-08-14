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

## Nightly Replay 与索引体积豁免（TK-18 / gate-3 证据）
- 真实索引（vector+bm25 约 1.2G）仅存在于本机（gitignored）；CI replay
  使用入库的 MINI 索引 fixture（`qa-backend/test_fixtures/mini_index`）。
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

## 后续 Ticket（spec 明确范围外）
- LightRAG ingest 恢复（剩余 curated 候选，用户原话"Q5之后再说"）——
  本次按裁决作为"AI精选+精选情报"图谱构建后续工作单执行（进行中）。
