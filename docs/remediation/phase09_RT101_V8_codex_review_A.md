# RT-101 V8 prep — Codex Review Cluster A (verbatim final verdict)

- Reviewer: codex exec (--sandbox read-only), serial run, stdin: prompt file
- Date: 2026-09-15 (session)
- Reviewed HEAD: 4dd3f30 (fixes landed afterwards at f7bf016, d677302)
- Scope: Cluster A — claim-map/JSON provider contract, evidence authority
  (legacy citation resolution), transparent-dev repairs:
  904abd2 (thinking-disabled JSON contract + _legacy_record_id_map),
  7b8daee (retrieval-class deadline seam), 4dd3f30 (T8 regression).
- Blinding: reviewer confirmed — no gold/holdout/owner-secret material opened.
- Verdict: **APPROVE** — REMAINING P0: 0, P1: 1, P2: 4.
- Disposition (serial review discipline: P1 must reach 0 before Cluster B):
  - P1-1 (server.py `_legacy_record_id_map` accepts any nonempty mappings
    list; first-match resolution can misbind stable ids) → FIXED at
    d677302: loader now validates via
    `index_build_view.validate_record_id_map` against THIS install's
    dataset bytes (snapshot id = sha256 of the lite dataset file; the live
    install map 30391 mappings validates with zero issues — deployment
    behavior unchanged). Locked by new T9 (valid map accepted verbatim;
    stale/duplicate-id/duplicate-idx/tombstoned/quarantined-with-id/
    partial/empty/wrong-schema all rejected → None; rejected load cached;
    real install map validates against real dataset bytes).
  - P2-3 (T8 does not lock the production caller-class boundary) → FIXED
    at d677302: new T10 — `allow_reasoning_fallback=True` appears exactly
    in the 10 enumerated JSON-parser modules (claim_mapping 1, decomposer 1,
    epistemic 2, evidence_grader 1, gap_analysis 1, multi_document 1,
    reranker 1, router 1, server 1, verifier 2) and nowhere else; server.py
    keeps unflagged prose calls.
  - P2-2 (thinking parameter is ZAI/GLM-specific) → ACCEPTED with rationale:
    the parameter is a provider-specific deployment contract; for providers
    that reject unknown parameters the JSON calls fail closed (existing
    bounded fallbacks unchanged); no release threshold or authority path
    affected.
  - P2-4 (T6 does not cover partial/duplicate/stale maps) → COVERED by T9's
    loader-level cases (the loader is the enforcement point; the resolver
    contract remains documented in T6).
  - P2-5 (deadline-override provenance not recorded in evidence) → ACCEPTED
    with rationale: reviewer verified CI and the formal runner define no
    values for the new seam variables (canonical defaults govern all formal
    runs); provenance emission touches the release-evidence schema and is
    deferred to dedicated evidence-schema work, not the candidate freeze.
- Post-fix suite: tests_repair_v7_postmortem.py 183/183 (182/182 without a
  TECH_DB_RUNTIME_DIR-provided install map — the install-map check skips).
- CI note: hermeticity fix f7bf016 was required for the all-mock push tier
  (T8 originally built the Authorization header via load_api_key before the
  mocked transport); release_phase09 fails identically at baseline 22156ca
  (Q-336 external blocker) — not a regression.

---

VERDICT: APPROVE
P0 COUNT: 0
P1 COUNT: 1
P2 COUNT: 4

1. qa-backend/server.py:364 — P1 — `_legacy_record_id_map()` accepts any dict with a nonempty `mappings` list, while resolution accepts the first matching entry. A corrupt-but-parseable, partial, duplicate-index, duplicate-ID, tombstoned, quarantined, or dataset-mismatched map can therefore assign a wrong-but-plausible stable `record_id`; exact grounding may subsequently validate against that wrong record. Fix: validate this legacy map through `index_build_view.validate_record_id_map` plus the corresponding dataset snapshot before caching; reject and cache `None` on any issue, and explicitly reject tombstoned/quarantined/duplicate markers. [FIXED d677302]
2. qa-backend/config.py:128 — P2 — Sending the ZAI/GLM-specific `thinking` object to arbitrary OpenAI-compatible providers may cause strict unknown-parameter rejection. The behavior is fail-closed and this client is provider-specific, but compatibility is implicit. Fix: gate the key on an explicit provider contract or accepted-parameter configuration and keep missing/unconfirmed support fail-closed. [ACCEPTED — rationale above]
3. qa-backend/tests_repair_v7_postmortem.py:555 — P2 — T8 validates the wire payload directly but not the production caller-class boundary. Fix: add a source/call-site contract test enumerating every production `llm_model_func` call, requiring `allow_reasoning_fallback=True` exactly for JSON parsers and absence on generation/repair/prose calls. [FIXED d677302 — T10]
4. qa-backend/tests_repair_v7_postmortem.py:441 — P2 — T6 locks tombstoned mappings as resolvable and omits partial, duplicate, stale, and wrong-dataset map cases. Fix: add loader and `_record_for_citation` tests proving every malformed or identity-ambiguous map produces `None`/empty stable IDs and no citation. [COVERED — T9 loader-level cases]
5. qa-backend/runtime_safety.py:348 — P2 — The seam itself preserves canonical defaults, and formal CI does not set these variables, but override provenance is not recorded in benchmark or release evidence. Fix: emit an operator-configuration provenance field identifying active deadline overrides so formal results cannot be mistaken for canonical-default results. [ACCEPTED — rationale above]

Blinding confirmation: I did not access any forbidden material.
