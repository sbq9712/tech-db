#!/usr/bin/env python3
"""RT-105 table-driven integrated failure injection."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from answer_status import AnswerStateMachine, build_terminal_response
from multi_document import PacketCache, PacketCacheKey
from phase09_release import validate_benchmark_artifact
from runtime_safety import (FailureClass, RequestExecutionContext,
                            StageExecutionError)

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


class InjectedFailure(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


async def inject_stage(capability, exc):
    context = RequestExecutionContext(mode="RESEARCH")

    async def fail():
        raise exc

    try:
        await context.run_stage(capability, fail,
                                requirement_critical=True,
                                safe_fallback_available=False)
    except StageExecutionError:
        pass
    return context.degraded_capabilities


def terminal_for(capability, reason):
    machine = AnswerStateMachine()
    machine.record_technical_failure(capability, reason)
    machine.finalize()
    return build_terminal_response(answer="", answer_status="UNVERIFIED",
                                   state_machine_snapshot=machine.snapshot())


def test_failure_matrix():
    rows = [
        ("retrieval", InjectedFailure("route unavailable")),
        ("evidence_grader", InjectedFailure("malformed grader JSON")),
        ("evidence_grader", asyncio.TimeoutError("grader timeout")),
        ("evidence_grader", InjectedFailure("rate limited", status_code=429)),
        ("evidence_grader", InjectedFailure("upstream 503", status_code=503)),
        ("grounding", InjectedFailure("exact span grounding failure")),
        ("entailment", InjectedFailure("entailment failure")),
        ("final_verifier", InjectedFailure("verifier malformed response")),
    ]
    for index, (capability, exc) in enumerate(rows):
        degraded = asyncio.run(inject_stage(capability, exc))
        terminal = terminal_for(capability, str(exc))
        check(f"RT105 {index} degradation recorded", bool(degraded)
              and degraded[-1]["capability"] == capability)
        check(f"RT105 {index} never semantic pass",
              terminal["answer_status"] != "SUPPORTED"
              and terminal["verification_status"] != "PASSED")


def test_cache_mismatch_and_manifest_corruption():
    base = dict(manifest_id="manifest-a", profile="core",
                source_snapshot_id="ss-1", requirements=[{"id": "r1"}],
                worker_model="deterministic", prompt_version="p1",
                schema_version="s1", access_scope="public")
    key_a = PacketCacheKey.build(**base)
    key_b = PacketCacheKey.build(**{**base, "manifest_id": "manifest-b"})
    cache = PacketCache()
    cache._items[key_a] = {"sentinel": True}
    check("RT105 cache manifest mismatch is a miss", cache.get(key_b) is None)

    # Standalone synthetic artifact: this suite must not depend on another
    # suite having already created a file in the worktree.
    expected_dataset = "1" * 64
    corrupted = {
        "schema_version": "phase09-benchmark-1.0", "verdict": "PASS",
        "metrics": {"sentinel": {"value": 1, "threshold": 1,
                                    "direction": "gte", "passed": True}},
        "provenance": {
            "git_sha": "2" * 40, "spec_sha256": "3" * 64,
            "decision_register_sha256": "4" * 64,
            "manifest_id": "manifest", "dataset_sha256": "0" * 64,
            "identity_snapshot_id": "identity", "model": "deterministic",
            "prompt_sha256": "5" * 64,
            "schema_version": "phase09-benchmark-1.0",
            "config_sha256": "6" * 64,
        },
    }
    expected = {"dataset_sha256": expected_dataset}
    issues = validate_benchmark_artifact(corrupted,
                                         expected_provenance=expected)
    check("RT105 manifest/provenance corruption fails closed",
          any("stale or wrong provenance" in issue for issue in issues))


def test_real_endpoint_failures():
    from tests_remediation_phase08 import _production_terminal_case
    events, payloads = asyncio.run(_production_terminal_case("generator_failure"))
    terminal = [row for row in payloads if row.get("terminal_schema_version")]
    check("RT105 real endpoint generator 5xx-like failure",
          events.count("done") == 1 and len(terminal) == 1
          and terminal[0]["answer_status"] == "UNVERIFIED")
    events, payloads = asyncio.run(_production_terminal_case("cancel"))
    check("RT105 real endpoint cancellation cleanup",
          "done" not in events and not any(
              row.get("terminal_schema_version") for row in payloads))


def main():
    test_failure_matrix()
    test_cache_mismatch_and_manifest_corruption()
    test_real_endpoint_failures()
    print("=" * 66)
    print(f"  Phase09 failure injection: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
