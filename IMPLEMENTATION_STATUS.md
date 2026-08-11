# Tech-DB Agentic RAG Implementation Status

## Overview
Implementation of the Evidence-Centric Adaptive Agentic RAG specification.

## Phase A — Foundation & Correctness (T001-T006, T013)
All tickets implemented with tests passing.

| Ticket | Module | Status | Tests |
|--------|--------|--------|-------|
| T001 | trace.py | ✅ Complete | 8 tests |
| T002 | eval/ | ✅ Complete | Framework ready |
| T003 | citation_grounding.py | ✅ Complete | 8 tests |
| T004 | claim_mapping.py | ✅ Complete | 5 tests |
| T005 | verifier.py | ✅ Complete | 9 tests |
| T006 | answer_status.py | ✅ Complete | 6 tests |
| T013 | content_safety.py | ✅ Complete | 7 tests |

## Phase B — Knowledge Evidence Infrastructure (T007-T012)
Core modules implemented. Enrichment scripts pending full data pipeline integration.

| Ticket | Module | Status |
|--------|--------|--------|
| T007 | check_data_quality.py | ✅ Checks implemented |
| T008 | provenance.py | ✅ Complete |
| T009 | source_suitability.py | ✅ Complete |
| T010 | temporal.py | ✅ Complete |
| T011 | entity_resolver.py | ✅ Initial V1 (ER-001..124 epic pending) |
| T012 | check_data_quality.py | ✅ Complete |

## Phase C — Retrieval + Adaptive Agentic Core (T014-T027)

| Ticket | Module | Status |
|--------|--------|--------|
| T014 | retrieval/ | ✅ Wrapper layer complete |
| T015 | retrieval/fusion.py | ✅ Candidate Pool RRF |
| T016 | reranker.py | ✅ Complete |
| T017 | evidence_selector.py | ✅ Complete |
| T018 | router.py | ✅ Complete |
| T019 | decomposer.py | ✅ Complete |
| T020 | planner.py | ✅ Complete |
| T021 | evidence_ledger.py | ✅ Complete |
| T022 | evidence_grader.py | ✅ Complete |
| T023 | gap_analysis.py | ✅ Complete |
| T024 | orchestrator.py | ✅ Complete |
| T025 | stopping.py | ✅ Complete |
| T026 | knowledge_boundary.py | ✅ Complete |
| T027 | (T011 + T027 graph) | ⏳ Semantic graph pending graph pipeline build |

## Phase D — Advanced Quality (T028-T033)

| Ticket | Module | Status |
|--------|--------|--------|
| T028 | chunking.py | ⏳ Chunking pending |
| T029 | numeric_facts.py | ✅ Complete |
| T030 | conflict_detector.py | ✅ Complete |
| T031 | context_builder.py | ✅ Complete |
| T032 | citation_grounding.py | ✅ (uses T003) |
| T033 | Frontend | ⏳ Frontend updates pending |

## Phase E — Operational (T034-T037, T041, T056)

| Ticket | Module | Status |
|--------|--------|--------|
| T034 | eval/ | ✅ Benchmark framework ready |
| T035 | eval/replay.py | ⏳ Replay pending |
| T036 | eval/golden.py | ✅ Bad case schema ready |
| T037 | orchestrator.py | ✅ Integration skeleton complete |
| T041 | release_manifest.py | ✅ Complete |
| T056 | trace_retention.py | ✅ Complete |

## Entity Resolution V2 Epic (ER-001..ER-124)
Initial entity registry implemented (T011). Full ER-V2 epic (ER-001..ER-124)
is a major workstream that includes entity mention schema, resolution decisions,
merge/split operations, incremental snapshots, query-time resolver, shadow
ingestion, and graph-V2 activation. This is tracked separately.

## Integration into server.py
- ✅ Trace system (T001) integrated into SSE handler
- ✅ Citation grounding (T003) integrated into done event
- ✅ Fail-safe verifier (T005) replaces old verify_answer
- ✅ Four-state answer status (T006) in done event
- ✅ Content safety (T013) imports added
- ✅ Feature flags (all new features controllable via env vars)

## Test Results
- Phase A: 42/42 passed
- Phase B+C: 45/45 passed
- Total: 87/87 tests passing

## Feature Flags
All new features are controlled by environment variables:
- QA_AGENTIC_ENABLED: Master switch
- QA_TRACE_ENABLED: Trace system (default: true)
- QA_ROUTER_ENABLED: Adaptive router
- QA_RERANK_ENABLED: Content-aware reranker
- QA_EVIDENCE_SELECTOR_ENABLED: Evidence selector
- QA_EVIDENCE_GRADER_ENABLED: Evidence grader
- QA_ITERATIVE_RETRIEVAL_ENABLED: Iterative loop
- QA_CITATION_GROUNDING_ENABLED: Citation grounding (default: true)
- QA_FAIL_SAFE_VERIFY_ENABLED: Fail-safe verifier (default: true)
- QA_ANSWER_STATUS_ENABLED: Four-state status (default: true)
- QA_CLAIM_MAPPING_ENABLED: Claim mapping
- QA_CONTENT_SAFETY_ENABLED: Content safety (default: true)

## Files Created
Core modules: 27 Python files
Eval framework: 5 Python files
Retrieval layer: 5 Python files
Tests: 2 test files
Scripts: 1 script

Total: ~40 new files, ~5000 lines of code
