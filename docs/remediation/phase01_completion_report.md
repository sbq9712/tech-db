# Phase 01 completion evidence

Scope: RT-010 through RT-018 on top of accepted Phase-00 commit
`2c21b658b7a31ddfde151764e2be299626706c59`.

## Behavioral evidence

- `python qa-backend/tests_remediation_phase01.py`: 55 passed, 0 failed.
- `python qa-backend/run_all_tests.py --tier push`: 415 passed, 0 failed across 28 suites.
- `python scripts/check_project.py`: PASS.
- canonical spec lint and negative-fixture self-test: PASS.
- acceptance-matrix source/count validation: PASS (563 DoDs; 518 explicitly unmet/blocked).
- deterministic mini runtime rebuild/health: PASS (8 fully synthetic records).

The Phase-01 suite now includes production-path evidence for durable stable IDs
across Vector/BM25/Graph/Fusion/Trace, cross-code-point NFKC contraction,
pre-store artifact schema rejection, a real ASGI request pinned through a
concurrent manifest reload, strict server startup failure, and a bootable DR
restore with corruption rejection.

## Retrieval benchmark

`python scripts/run_phase01_benchmark.py` runs a deterministic fixture benchmark,
not production traffic.  Recall@1 changed from 0.6667 to 1.0; the synthetic-only
sentinel was present in the historical primary-text formulation and absent from
the rebuilt source-grounded formulation.  The machine-readable result is
`qa-backend/test_fixtures/remediation/phase01_retrieval_benchmark.json`.

## Honesty and external state

- The committed mini runtime is the reproducible rebuild evidence. No production
  canary or real-traffic benchmark is claimed.
- Graph-V2 remains `NOT_ACTIVATED_BY_GAIN_GATE` in the fixture profile.
- RT-005 remains `BLOCKED_EXTERNAL_ACTION`; this phase does not claim that main
  branch protection or required status checks were enabled.
- Current runtime-v1 deployment remains explicitly on `legacy_hybrid`.
  Manifest mode is strict fail-closed and is not claimed production-active;
  its future shadow/canary activation conditions are recorded in `MIGRATION.md`.
