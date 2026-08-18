# Audited emergency bypass policy

Direct pushes to `main` are prohibited for humans and automation. An emergency bypass is allowed only for an active security or production-correctness incident when waiting for the normal reviewed merge would materially increase harm.

The operator must open an incident record before bypass, identify the exact commit and protected checks that cannot complete, obtain approval from a repository administrator other than the author when a second administrator is available, and use the narrowest time-bounded ruleset bypass. The bypass must never suppress secret scanning or allow known-failing correctness checks.

Within 24 hours, the operator must restore protection, open a retrospective issue, attach the incident timeline and bypass audit log, run every normally required check against the shipped commit, and either merge a reviewed follow-up or revert the emergency change. Bot and data-sync identities have no standing bypass permission.

Required `main` checks are documented by `.github/workflows/remediation-gates.yml`: canonical spec lint, acceptance-matrix completeness, unit suites, deterministic mini-runtime integration/E2E, critical failure injection, synthetic isolation, fast regression, and security/safety.
