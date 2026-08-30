# Phase08 Completion Report — API, UI, Trace, Replay

Scope: RT-090..RT-094. Base: sealed Phase07 main
`c4c3e00f51ff932d9840d1e8203b476a5fa1243f` (tree
`760c3683c4e1ba15da94b8956d6b0b7af7787e03`).

## Implemented production behavior

- RT-090 extends the existing `AnswerStateMachine` with a versioned terminal
  response builder. Phase02 supplies its real state snapshot; legacy early
  exits first pass through a compatibility adapter which records facts in the
  same machine. Every non-cancellation chat terminal path carries stable
  `answer_status`, `verification_status`, evidence/degradation summaries and
  trace/profile diagnostics. Terminal status, verification state and stop
  reason are derived from that snapshot; compatibility arguments only assert
  equality. The legacy `status` alias is bound to the same canonical value.
  Request cancellation remains control flow and emits no fabricated normal
  `done`.
- RT-091 projects ReferenceCards from already-verified EvidenceRefs. Only
  exact locator-authorized spans render. Missing/invalid locators, scope
  denial, snapshot drift and `gs-*`/`gvs-*` Graph identifiers fail closed.
  Claim support/contradiction/background IDs and source role remain visible;
  all frontend strings are escaped. The normal chat API has no privileged
  evidence capability, so its effective scope is resolved server-side as
  public; neither request JSON nor the quota-bypass admin header can elevate
  retrieval, Generator input, citations or rendered ReferenceCards.
- RT-092 extends the canonical Trace replay command with exactly four fidelity
  modes: `HISTORICAL_EXACT`, `HISTORICAL_ARTIFACTS_CURRENT_MODEL`,
  `CURRENT_COMPARISON`, and `PARTIAL_REPLAY`. A bounded case-group command
  executes each case through the current committed mini-runtime and canonical
  Phase03 policy/selection/package path by default. Caller-supplied current
  output is not execution authority. Historical modes require verified
  artifact hashes and complete pins; exact mode additionally requires a
  trusted historical model runtime. The bounded command emits machine-readable
  per-case version/output diffs and rejects malformed inputs with non-zero
  status. Precomputed comparison remains an explicit compare-only mode.
- RT-093 keeps Human Review promotion in `DEVELOPMENT_REGRESSION`. Unconfirmed
  feedback is rejected, promotion provenance is durable, and any Human Review
  attempt to target the locked holdout fails. The existing holdout and lock
  remain the only release-holdout authority.
- RT-094 adds an operator-key-authenticated server endpoint over the existing
  retained Trace store. It re-applies the production allowlist, retention and
  configured snapshot scope, never returns raw traces, and exposes the honest
  RT-092 replay fidelity label to operator UI clients. Fidelity is derived
  from trusted artifact/model availability resolvers, never from manifest-id
  presence or a caller boolean.

## Reused canonical seams

`answer_status.py`, Phase02 terminal renderer/state snapshot,
`EvidencePackage`/EvidenceRefs and citation grounding, `trace.py` plus
`trace_retention.py`, `eval/replay.py`, `eval/human_review.py`, the existing
locked holdout, `GuardrailSettings.admin_key`, and `qa.js` reference rendering.
No parallel state machine, evidence authority, trace store, holdout or access
control system was introduced.

## Activation and external boundaries

Phase08 does not activate Graph-V2. The sealed result remains
`gain_conclusion=NO_GAIN`, `graph_v2_activation_claim=false`,
`activation_gate_satisfied=false`, 3 `CI_REPLAY` events over 0.0 days,
`canary=false`, and no equivalent-replay approval. RT-005 remains an external
repository ruleset action. RT-075 remains an external activation/shadow
evidence requirement. Phase09 is not started.

## Behavioral evidence

`qa-backend/tests_remediation_phase08.py` contains named top-level behavioral
tests for every Phase08 DoD, including real FastAPI/SSE production composition
and server-boundary authorization. The suite is registered in the push tier
and in the required `phase08-api-ui-trace-replay` remediation CI job. The real
RT029 Chromium visual gate remains required. Repair Round 1 adds the canonical
terminal invariants, trusted chat-scope composition and executed replay/fidelity
regressions. Local behavioral evidence is Phase08 78/78 and push tier 1287/1287
across 42 suites; fresh GitHub CI remains the merge-ref authority.
