#!/usr/bin/env python3
"""Unit tests for call_glm_batch resilience: backoff + circuit breaker + progress.
Monkeypatches llm_client.call_glm to simulate success/failure patterns.
No network. Run: python3 scripts/test_call_glm_batch.py
"""
import sys, json, itertools
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import llm_client

def _patch(monkey, fake):
    """Swap llm_client.call_glm with `fake`, restore after."""
    orig = llm_client.call_glm
    monkey["orig"] = orig
    llm_client.call_glm = fake

def _unpatch(monkey):
    llm_client.call_glm = monkey["orig"]

def _ok_json(batch):
    """Simulate a successful GLM response: one result dict per item, echoing id."""
    return json.dumps([{"id": it["id"], "v": 1} for it in batch])

def test_partial_then_fail():
    """(a) first N batches succeed, then constant failure -> returns first N, trips breaker."""
    m = {}
    state = {"calls": 0}
    items = [{"id": i} for i in range(400)]  # 40 batches @ size 10
    def fake(prompt, timeout=120):
        state["calls"] += 1
        # each "batch" = one call_glm; succeed for first 3 batches then fail forever
        if state["calls"] <= 3:
            # reconstruct batch from prompt is hard; instead return a small valid array
            return json.dumps([{"id": -1, "v": 1}])
        raise RuntimeError("simulated persistent API outage")
    _patch(m, fake)
    try:
        out = llm_client.call_glm_batch("PROMPT ", items, batch_size=10, max_workers=2,
                                        max_consecutive_failures=5, progress_every=100,
                                        backoff_delays=(0, 0, 0))  # no sleep in tests
    finally:
        _unpatch(m)
    assert isinstance(out, list), f"expected list, got {type(out)}"
    assert len(out) >= 3, f"expected >=3 partial results, got {len(out)}"
    print(f"  PASS (a) partial-then-fail: returned {len(out)} results, did not raise")

def test_all_fail():
    """(b) constant failure -> empty list, no raise, [ABORT] logged."""
    m = {}
    items = [{"id": i} for i in range(300)]  # 30 batches
    def fake(prompt, timeout=120):
        raise RuntimeError("API totally dead")
    _patch(m, fake)
    try:
        out = llm_client.call_glm_batch("PROMPT ", items, batch_size=10, max_workers=3,
                                        max_consecutive_failures=5, progress_every=100,
                                        backoff_delays=(0, 0, 0))
    finally:
        _unpatch(m)
    assert isinstance(out, list)
    assert len(out) == 0, f"expected empty list, got {len(out)}"
    print(f"  PASS (b) all-fail: returned empty list, no exception")

def test_intermittent_no_false_trip():
    """(c) intermittent failure (1 fail per 3) -> does NOT trip breaker, returns many."""
    m = {}
    cycler = itertools.cycle([False, False, True])  # 2 ok then 1 fail, repeating
    items = [{"id": i} for i in range(300)]  # 30 batches
    def fake(prompt, timeout=120):
        if next(cycler):
            raise RuntimeError("transient blip")
        return json.dumps([{"id": -1, "v": 1}])
    _patch(m, fake)
    try:
        out = llm_client.call_glm_batch("PROMPT ", items, batch_size=10, max_workers=3,
                                        max_consecutive_failures=5, progress_every=100,
                                        backoff_delays=(0, 0, 0))
    finally:
        _unpatch(m)
    assert isinstance(out, list)
    assert len(out) > 10, f"intermittent should still return many results, got {len(out)}"
    print(f"  PASS (c) intermittent: returned {len(out)} results, no false breaker trip")

def test_progress_logged():
    """(d) progress line printed at progress_every interval."""
    import io, contextlib
    m = {}
    items = [{"id": i} for i in range(250)]  # 25 batches
    def fake(prompt, timeout=120):
        return json.dumps([{"id": -1, "v": 1}])
    _patch(m, fake)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            llm_client.call_glm_batch("PROMPT ", items, batch_size=10, max_workers=3,
                                      max_consecutive_failures=999, progress_every=10,
                                      backoff_delays=(0, 0, 0))
    finally:
        _unpatch(m)
    txt = buf.getvalue()
    assert "进度:" in txt, f"expected progress log, got: {txt!r}"
    print(f"  PASS (d) progress-logged: saw '进度:' lines")

def main():
    print("Running call_glm_batch resilience tests (monkeypatched, no network)...")
    test_partial_then_fail()
    test_all_fail()
    test_intermittent_no_false_trip()
    test_progress_logged()
    print("\nAll tests passed.")

if __name__ == "__main__":
    main()
