# §51-53 — Test isolation fix notes (prep-only, NOT applied yet)

## Observed contamination (RT101 session, verified twice via git stash)
`tests_e2e_phase09.py` line ~252 does a raw assignment:

```python
server.hybrid_search = ...   # module-global clobber, never restored
```

Any suite imported/ran AFTER e2e in the same pytest session inherits the
clobbered global. Confirmed victims:
- tests_parity::test_hybrid_parity_new_vs_legacy_baseline
- test_weak_query_admission
- test_rt030_lazy_endpoints_do_not_nameerror
- RT101 R4 integration test

All four PASS in isolation and FAIL after e2e → ordering contamination,
pre-existing on a clean 43801a7 tree (not introduced by RT101 work).

## Why not fixed in this pass
A correct fix edits the shared e2e harness (monkeypatch.setattr with
teardown, or an autouse fixture snapshotting/restoring the module dict).
That is test-infra-only and legal per directive §51, but it touches a suite
that is part of the Phase09 release gate evidence chain; changing that file
changes the evidence hashes referenced by release artifacts. That belongs in
its own reviewed commit — preferably after the owner (or RT101 completion)
unfreezes the Phase09 branch, so the evidence chain stays byte-stable.

## Prepared fix shape (apply later)
```python
# tests_e2e_phase09.py — replace raw assignments with:
@pytest.fixture(autouse=True)
def _isolate_server_globals(monkeypatch):
    monkeypatch.setattr(server, "hybrid_search", fake_hybrid, raising=True)
    ...
```
monkeypatch restores automatically; no other suite sees the fake.
