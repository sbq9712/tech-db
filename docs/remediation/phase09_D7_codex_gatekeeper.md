# D7 — Codex Final Gatekeeper (final round)

- Invoked: 2026-09-09, `codex exec -s read-only -o <verdict-file>`,
  head d24ed1e (tested ab399f5 = 1689 passed / 0 failed / 49 suites,
  chain 31/31 strict).
- Rounds this decision: design round → adversarial code review (round 2,
  11 findings, all fixed in 8d4e121) → gatekeeper attempt 1 (found the
  publicly-known-test-key production-acceptance issue; fixed in ab399f5;
  attempt's verdict block lost to its own context compaction, but its
  validator run showed 31/31 and its policy/chain tamper matrices' only
  two "BYPASS" labels were its own read-only-sandbox monkeypatch artifacts —
  the real validator detects the reason-erasure case, reproduced directly:
  C4 fails with exit 1) → FINAL gatekeeper round below (fresh session,
  1.37M tokens of probing, verdict captured via --output-last-message).
- The final round independently executed: strict validator (31/31),
  authorize-path denial (PUBLISH_DENIED with full RT-101 reason), policy
  tamper matrix (legacy SATISFIED / satisfaction key / trust classes),
  proof tamper matrix (wrong SHA bindings, swapped bindings, wrong
  manifest/snapshot, post-signing field edit, weak key, publicly-known
  test key against BOTH production providers), external blocker
  self-hash attack via temp copy (ValueError), and holdout-shape scan
  of the locked fixture (answer_fields = []).
- Gatekeeper attempt 1's substantive finding (test key accepted by
  production providers) is closed by PUBLICLY_KNOWN_TEST_KEYS in
  qa-backend/phase09_authority.py, verified by the gatekeeper itself.

## Final verdict (captured verbatim via --output-last-message, head d24ed1e)

```
AUTHORITY_BYPASS_CLOSED: YES
EVIDENCE_CHAIN_CONSISTENT: YES
HOLDOUT_ISOLATION_PRESERVED: YES
RELEASE_FAIL_CLOSED_WITHOUT_GENUINE_RT101: YES
SAFE_FOR_FRESH_V5: YES
FINDINGS: none
```

### Re-validation at the shipped head 21cac63

After the verdict, the ONLY code change was a test-infrastructure fix
(qa-backend/tests_release_phase09.py: shallow-clone-safe non-HEAD shas —
`HEAD~1` is unresolvable under CI `fetch-depth: 1` and had crashed the
release_phase09 suite in CI; replaced by `HEAD^{tree}`). No authority,
gate, validator, publish, or evidence-chain code path changed.
A fresh gatekeeper round at 21cac63 confirmed before its session budget
exhausted (3.2M tokens): strict validator 31/31, release suite 79/79
under /dev/shm isolation, copied-chain tamper detection (count/report/
hash failures all caught), PUBLISH_DENIED on the publish path with the
new key-rejection reason, replay-binding semantics reconciled
(parent-signed proof accepted only at the parent checkout, replay at
HEAD rejected), and holdout fixture answer_fields=[]. Its final verdict
block could not be captured within the local codex reliability budget
(two stalled sessions killed at 0 CPU, one budget exhaustion — three
retries spent); the d24ed1e verdict above therefore stands as the final
gatekeeper verdict for the D7 code, which is byte-equivalent on every
security-relevant path at 21cac63.
