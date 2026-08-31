# Phase09 — Benchmarks, CI, release gates

This document is a human-readable view only. Machine authority remains
`spec/acceptance_matrix.json`, the named suites, generated benchmark/release
artifacts, and `phase09_release.evaluate_release`.

## Scope

- RT-100: locked retrieval/reranker/evidence benchmark
- RT-101: answer/citation/abstention hard gates
- RT-102: standard Research versus multi-document benchmark
- RT-103: deterministic ER benchmark with RT-075 dependency preserved
- RT-104: real `/api/chat/stream` and canonical orchestrator E2E
- RT-105: integrated failure injection
- RT-106: PR/nightly/release CI tiers and artifact provenance
- RT-107: single fail-closed release evaluator
- RT-108: ticket status derived from executable evidence

## Machine result

The deterministic Phase09 release gate reports:

```text
core_eligible = true
production_release_eligible = false
graph_activation_eligible = false
graph_state = OFF_NO_GAIN
external_blockers = [Q-336, RT-005, RT-075]
phase_status = PASS_WITH_EXTERNAL_BLOCKER
```

This is intentional. Code-local Phase09 gates pass, while production release
remains blocked by repository administration (RT-005), missing
production-representative ER shadow evidence (RT-075), and the current GitHub
public-repository artifact policy (Q-336). CI replay is not treated as
production evidence. Graph-V2 remains off because the sealed conclusion is
`NO_GAIN`.

## Gatekeeper repair evidence

```text
Phase09 suites: 97 passed, 0 failed
  benchmark_phase09: 35/35
  e2e_phase09: 21/21
  failure_injection_phase09: 20/20
  release_phase09: 21/21

Push deterministic tier: 1384 passed, 0 failed across 46 suites
Phase08: 78/78
Phase07: 118/118
Phase07 benchmark: 8/8, gain_conclusion=NO_GAIN
Phase06: 92/92
Phase06 benchmark: 15/15
ER V2: 32/32
Phase03: 162/162
Spec lint: PASS
Spec lint self-test: PASS
Acceptance matrix validate-only: PASS
Spec/runtime verifier: 7/7
Project checks: 20/20
```

The local optional RT-029 visual suite reports `0/0`; this is not used as real
browser evidence. The authoritative Chromium result must come from the GitHub
Actions `rt029-visual-regression` job on the pull request merge candidate.

## Benchmark provenance

`qa-backend/benchmark_phase09_result.json` records the execution git SHA,
normative spec and Decision Register hashes, locked dataset hash, manifest and
identity IDs, deterministic model adapter, prompt/config hashes, and schema
version. CI regenerates the artifact at its exact checkout SHA.

RT-100 now reconstructs Vector/BM25/Chunk routes from the hashed committed
mini-runtime and executes the canonical pool and content reranker. It does not
consume fixture-supplied route outputs. The release-eval fixture is separate
from the blinded holdout and records the holdout lock hash only as an isolation
proof. RT-103 reports pre-registered gates for every canonical entity class;
RT-075 remains external and cannot be replaced by CI replay.

## CI contract

The required Phase09 job executes `scripts/run_phase09_release_gate.py`, which
runs all four required suites, emits per-suite JSON artifacts, evaluates hard
invariants and provenance, and generates evidence-derived ticket status. The
workflow requests 180-day retention for these artifacts. On PR #10 GitHub
clamped the artifact to its current public-repository maximum of 90 days
(`expires_at=2026-11-28T16:29:10Z`). Q-336 therefore remains an explicit
external blocker until durable retention of at least 180 days is configured.

A skipped, missing, failed, cancelled, stale, or wrong-provenance required
suite cannot produce a green core decision. An infrastructure-flake label
cannot erase a semantic regression.

The publication-path audit found one GitHub side-effect path:
`.github/workflows/publish-runtime.yml` (`gh release create/upload`). It now
runs a fresh Phase09 release evaluation and an exact-checkout-SHA authorization
before any package construction or Release mutation. The authorization requires
`production_release_eligible == true` and no external blockers, so the current
Q-336/RT-005/RT-075 state deliberately denies publication. Local manifest
construction and `ReleaseCatalog.activate` are atomic runtime/test seams, not
GitHub publication paths; they do not publish external assets.

External blocker state is loaded from `spec/phase09_external_state.json`.
Changing a row to satisfied without a named artifact and SHA-256 proof is a
schema error, so a caller cannot manufacture an unblocked decision.

## Remaining external review

Independently verify the GitHub synthetic merge candidate, its parents/tree,
exact checkout SHA in important job logs, generated artifacts, and required CI
results. Do not merge or start Phase10 before that review.
