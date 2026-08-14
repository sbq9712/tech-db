"""TK-10 — GLM API failure → legacy result UNVERIFIED + user warning (Q11).

Invariants:
  * verify_with_fail_safe: API exception → UNVERIFIED, never PASSED
  * UNVERIFIED answer → done event carries a user-visible warning
  * API-failure signature detected → warning annotated with the service note
  * non-UNVERIFIED statuses → no warning (silent only when fine)
"""
import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="tk10-"))

from degraded_mode import build_user_warning, looks_like_api_failure
from verifier import verify_with_fail_safe, VERIFY_PASSED, VERIFY_UNVERIFIED, VERIFY_FAILED

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


META = [{"claim": "X is Y", "record_idx": 1, "source": "s"}]


def t_api_failure_signatures():
    assert looks_like_api_failure("urlopen error timed out")
    assert looks_like_api_failure("HTTP Error 503: Service Unavailable")
    assert looks_like_api_failure("Connection refused")
    assert not looks_like_api_failure("malformed JSON from model")
    assert not looks_like_api_failure("")


def t_verify_api_exception_unverified():
    """LLM layer raising (GLM down) → UNVERIFIED, never PASSED."""
    import verifier
    real = verifier.llm_model_func

    async def dead_llm(*a, **kw):
        raise ConnectionError("urlopen error connection refused")

    verifier.llm_model_func = dead_llm
    try:
        vr = asyncio.run(verify_with_fail_safe("q", "some answer text here", META))
        assert vr.status == VERIFY_UNVERIFIED, vr.status
        assert vr.status != VERIFY_PASSED
    finally:
        verifier.llm_model_func = real


def t_verify_empty_llm_output_unverified():
    """LLM returning empty/garbage → UNVERIFIED, never PASSED."""
    import verifier
    real = verifier.llm_model_func

    async def empty_llm(*a, **kw):
        return ""

    verifier.llm_model_func = empty_llm
    try:
        vr = asyncio.run(verify_with_fail_safe("q", "some answer text here", META))
        assert vr.status == VERIFY_UNVERIFIED, vr.status
    finally:
        verifier.llm_model_func = real


def t_warning_contract():
    w_api = build_user_warning("UNVERIFIED", "UNVERIFIED", "urlopen error timed out")
    assert w_api.startswith("⚠️") and "模型服务暂时不可用" in w_api
    w_plain = build_user_warning("UNVERIFIED", "UNVERIFIED", "malformed json")
    assert w_plain.startswith("⚠️") and "注意" in w_plain and "模型服务暂时不可用" not in w_plain
    assert build_user_warning("SUPPORTED") == ""
    assert build_user_warning("PARTIALLY_SUPPORTED") == ""
    assert build_user_warning("UNSUPPORTED") == ""  # boundary_message covers it


def t_status_chain():
    """UNVERIFIED verification propagates: answer_status = UNVERIFIED."""
    from answer_status import determine_answer_status
    st, _ = determine_answer_status(
        has_results=True, is_relevant=True,
        verification_status=VERIFY_UNVERIFIED, claim_mapping={"claims": []})
    assert st.value == "UNVERIFIED", st.value
    # NOTE: VERIFY_FAILED (issues found) maps to PARTIALLY_SUPPORTED in the
    # repo's four-state model — UNVERIFIED (cannot verify) is the API-failure
    # contract; FAILED is a different, expected degradation.


def t_verified_path_no_warning():
    """PASSED verification → SUPPORTED → no warning."""
    from answer_status import determine_answer_status
    st, _ = determine_answer_status(
        has_results=True, is_relevant=True,
        verification_status=VERIFY_PASSED, claim_mapping={"claims": []})
    assert build_user_warning(st.value, VERIFY_PASSED) == ""


if __name__ == "__main__":
    print("TK-10 — GLM failure → legacy UNVERIFIED + user warning")
    for name, fn in [
        ("API-failure signature detection", t_api_failure_signatures),
        ("verify: API exception → UNVERIFIED never PASSED", t_verify_api_exception_unverified),
        ("verify: empty LLM output → UNVERIFIED", t_verify_empty_llm_output_unverified),
        ("user warning contract (api vs plain vs none)", t_warning_contract),
        ("status chain: UNVERIFIED propagates to answer_status", t_status_chain),
        ("verified path → no warning", t_verified_path_no_warning),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-10 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
