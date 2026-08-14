# Agentic RAG Implementation Status

## Overview
Upgrading from "Hybrid RAG" to "Evidence-Centric Adaptive Agentic RAG"
All tickets from the Master Spec have been implemented.

## Implementation Summary

### Phase A: Foundation (T001–T013) ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| trace.py | ✅ Complete | 8 tests | T001 |
| eval/metrics.py | ✅ Complete | Framework ready | T002 |
| citation_grounding.py | ✅ Complete | 8 tests | T003 |
| claim_mapping.py | ✅ Complete | 5 tests | T004 |
| verifier.py | ✅ Complete | 9 tests | T005 |
| answer_status.py | ✅ Complete | 6 tests | T006 |
| epistemic.py | ✅ Complete | Enrichment done | T007 |
| provenance.py | ✅ Complete | 4 tests | T008 |
| source_suitability.py | ✅ Complete | 3 tests | T009 |
| temporal.py | ✅ Complete | 6 tests | T010 |
| entity_resolver.py | ✅ Complete | 4 tests | T011 |
| check_data_quality.py | ✅ Complete | Report generated | T012 |
| content_safety.py | ✅ Complete | 7 tests | T013 |

### Phase B: Evidence Infrastructure (T008–T031) ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| retrieval/ (vector, bm25, graph, fusion) | ✅ Complete | 8 tests | T014-T015 |
| reranker.py | ✅ Complete | 3 tests | T016 |
| evidence_selector.py | ✅ Complete | 3 tests | T017 |
| router.py | ✅ Complete | 3 tests | T018 |
| decomposer.py | ✅ Complete | 3 tests | T019 |
| planner.py | ✅ Complete | 2 tests | T020 |
| evidence_ledger.py | ✅ Complete | 5 tests | T021 |
| evidence_grader.py | ✅ Complete | 3 tests | T022 |
| gap_analysis.py | ✅ Complete | 3 tests | T023 |
| orchestrator.py | ✅ Complete | 3 tests | T024 |
| stopping.py | ✅ Complete | 2 tests | T025 |
| knowledge_boundary.py | ✅ Complete | 3 tests | T026 |
| chunking.py | ✅ Complete | 3 tests | T028 |
| numeric_facts.py | ✅ Complete | 3 tests | T029 |
| conflict_detector.py | ✅ Complete | 3 tests | T030 |
| context_builder.py | ✅ Complete | 3 tests | T031 |

### Phase C: Semantic Graph (T027, T039, T044–T045) ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| semantic_graph.py | ✅ Complete | Pipeline ready | T027 |
| retrieval/graph_aware.py | ✅ Complete | Relation-aware | T039 |
| relation_ontology.py | ✅ Complete | 15 predicates | T044 |
| graph_intent.py | ✅ Complete | Multi-hop validation | T045 |

### Phase D: Advanced Features ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| multi_document.py | ✅ Complete | 3 tests | T038 |
| query_integrity.py | ✅ Complete | 5 tests | T042 |
| answer_repair.py | ✅ Complete | 6 tests | T052 |
| entailment.py | ✅ Complete | 4 tests | T046 |
| req_fusion.py | ✅ Complete | 4 tests | T050 |
| reranker_stability.py | ✅ Complete | 3 tests | T051 |
| source_snapshot.py | ✅ Complete | 2 tests | T047 |
| prompt_injection_eval.py | ✅ Complete | 19/19 adversarial | T053 |

### Phase E: Operations ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| eval/replay.py | ✅ Complete | CLI ready | T035 |
| eval/human_review.py | ✅ Complete | CLI ready | T036 |
| degraded_mode.py + budget_guard.py | ✅ Complete | 33 tests | T037 |
| release_manifest.py | ✅ Complete | 2 tests | T041 |
| trace_retention.py | ✅ Complete | 3 tests | T056 |

### Entity Resolution V2 (ER-001..ER-124) ✅ Complete
| Module | Status | Tests | Ticket |
|--------|--------|-------|--------|
| entity_resolver_v2.py | ✅ Complete | 32 tests | ER-001..ER-124 |

### Integration ✅ Complete
| Feature | Status |
|---------|--------|
| Orchestrator → SSE pipeline | ✅ Wired (feature flag) |
| Frontend answer status UI | ✅ T033 Complete |
| Budget guard on verification | ✅ Active |
| Feature flag progressive rollout | ✅ All flags defined |

## Test Summary
- **Phase A tests**: 42 passed
- **Phase B+C tests**: 45 passed
- **Phase D tests**: 37 passed
- **Phase Final tests**: 29 passed
- **Phase Ops tests**: 33 passed
- **Integration tests**: 15 passed
- **ER V2 tests**: 32 passed
- **Final Acceptance tests**: 72 passed
- **Total**: 305 tests, all passing


## Nightly Replay 与索引体积豁免（TK-18 / gate-3 证据）
- 真实索引（vector+bm25 约 1.2G）仅存在于本机（gitignored，见
  check_project 的 FORBIDDEN_TRACKED_INDEXES）；CI replay 使用入库的 MINI
  索引 fixture（qa-backend/test_fixtures/mini_index）。
- 本地 nightly replay 入口：`.venv/bin/python scripts/nightly_replay.py
  --tag dayN --commit`（一键跑完并产出 artifact commit，报告存
  qa-backend/test_fixtures/holdout/replay/dayN.json）。
- gate-3 时间要求（连续 7 天 nightly replay）按所有者裁决压缩：一次全量
  确定性 replay 作为主证据（day1：id_overlap mean=1.0、top1=1.0、
  relevance/grounding 完全一致、new TTFB ≤ legacy），检索 shadow
  （QA_SHADOW_RETRIEVAL=1）在随后一周自然流量上继续累积补充证据。
- shadow 期成本（R6/R14）：检索级 shadow 0 额外 LLM 调用；答案级 shadow
  使每 query LLM 成本 ×2（报告 shadow_cost 字段记录）。

## Feature Flags
| Flag | Default | Description |
|------|---------|-------------|
| QA_AGENTIC_ENABLED | false | Full agentic loop |
| QA_ROUTER_ENABLED | false | Adaptive router |
| QA_DECOMPOSITION_ENABLED | false | Query decomposition |
| QA_ITERATIVE_RETRIEVAL_ENABLED | false | Gap-driven retrieval |
| QA_RERANK_ENABLED | false | GLM reranking |
| QA_EVIDENCE_SELECTOR_ENABLED | true | Smart evidence selection |
| QA_EVIDENCE_GRADER_ENABLED | false | Evidence sufficiency grading |
| QA_CLAIM_MAPPING_ENABLED | false | Claim→citation mapping |
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

## Background Tasks (Still Running)
- **Vector index rebuild** (PID 963): Batch ~171/339, ETA ~226 min
- **LightRAG ingest** (PID 1061): Stage 18/20, nearly complete
