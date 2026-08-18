# Phase 00 current-state gap report

Baseline: `qa-backend/test_fixtures/remediation/baseline_phase00.json`  
Reviewed start SHA: `3439c27bcbd0087b9ee46d86aac4384fa9fcc74b`  
Environment: committed mini runtime plus locked historical artifacts; **no fresh production traffic was claimed**.

## Confirmed gaps at the reviewed HEAD

| Area | Evidence at start SHA | Phase-00 disposition |
|---|---|---|
| Normative authority | Final spec and Decision Register were absent from the repository and the manifest did not bind their hashes. | Fixed by RT-001 artifacts and lint. |
| Final Acceptance | `tests_final_acceptance.py` contained `or True`, import-only checks, hardcoded `True`, and a manually fabricated E2E Trace. | Replaced under RT-002 by matrix validation and real suite execution. |
| Runtime fixture | The mini index had Vector/BM25 files but no stable record IDs, immutable SourceSnapshots, identity snapshot, chunks, evidence metadata, or complete pinned manifest. | Added under RT-003. |
| Baseline provenance | Existing parity/nightly artifacts were fragmented and did not bind spec, decision, dataset, identity, model, config, and git SHA in one schema. | Aggregated honestly under RT-004. |
| Main protection | Repository metadata showed no verifiable required-check protection, and auto-sync pushed directly to `main`. | Workflow/policy remediation is implemented; enabling the GitHub ruleset remains an external action. |

## Known non-Phase-00 remediation risks

The audit starting points in the frozen prompt remain open until their later RT tickets pass: stable production identity, synthetic-summary isolation in primary indexes, atomic release pinning, exact normalization mapping, fail-safe FAST_RAG behavior, Evidence Package-only generation, verification default semantics, exact citation filtering, deterministic AnswerStateMachine ownership, and Graph-V2 gain-gated activation.

## Reproducibility

Run `python scripts/capture_phase00_baseline.py --source-sha 3439c27bcbd0087b9ee46d86aac4384fa9fcc74b`. The JSON output is deterministic for the locked input artifacts and source SHA.
