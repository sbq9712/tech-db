"""
T005 + RT-025 — Fail-safe final verifier contract
==================================================
Phase 02 rewrite. The independent final verifier:

  * consumes ONLY question/scope, atomic claims, exact EvidenceRefs, and
    deterministic-check outputs (final spec §27, AR-55) — never Generator
    hidden reasoning, raw unselected retrieval text, or prior answer prose;
  * returns STRUCTURED FINDINGS only — it never authors/rewrites the final
    answer (final spec §26: "Verifier does not write final answers");
  * maps timeout / malformed response / empty response / parser error /
    429/5xx / exception to UNVERIFIED — a technical failure can never
    become PASSED (Q096).

States:
    PASSED       verification ran successfully, every claim verdict PASS
    FAILED       verification ran successfully, semantic findings exist
    UNVERIFIED   verification could not complete (technical failure)

MAX_VERIFY_RETRIES configurable (default 2); VERIFY_TIMEOUT seconds.
"""
import asyncio
import json
import os
import re

from config import llm_model_func

# TK-10/T005: bounded retries for transient transport failures only —
# semantic verdicts are never retried away.
MAX_VERIFY_RETRIES = int(os.environ.get("QA_MAX_VERIFY_RETRIES", "2"))

# Verify timeout (seconds) — enforced per LLM call (RT-025 failure matrix).
VERIFY_TIMEOUT = int(os.environ.get("QA_VERIFY_TIMEOUT", "60"))

VERIFY_PASSED = "PASSED"
VERIFY_FAILED = "FAILED"
VERIFY_UNVERIFIED = "UNVERIFIED"

VALID_CLAIM_VERDICTS = {"PASS", "FAIL", "UNKNOWN"}

# ── RT-025 exact EvidenceRef contract ──────────────────────────────────────
# The final verifier must receive COMPLETE exact refs — durable record
# identity (stable record_id, never a list position), the immutable snapshot
# binding, machine-checkable locators, the exact matched text, its snapshot
# hash, citation eligibility, and the source role used to cap attribution
# (RT-021). A ref missing or corrupting any of these is a technical failure:
# UNVERIFIED, never PASSED, never silently coerced.
REQUIRED_REF_FIELDS = (
    "evidence_id", "record_id", "source_snapshot_id", "locators",
    "exact_text", "evidence_text_sha256", "eligibility", "source_role",
)
_SHA256_HEX = set("0123456789abcdef")


def validate_evidence_ref(ref: dict) -> str:
    """Return '' when the ref satisfies the RT-025 EvidenceRef contract,
    else a machine-readable failure reason (fail-closed)."""
    if not isinstance(ref, dict):
        return "invalid_ref:not_a_dict"
    for field in REQUIRED_REF_FIELDS:
        if field not in ref:
            return f"missing_field:{field}"
    rid = ref.get("record_id")
    if not isinstance(rid, str) or not rid.strip():
        return "invalid_record_id:not_a_stable_string"
    if not isinstance(ref.get("source_snapshot_id"), str) or not ref["source_snapshot_id"].strip():
        return "invalid_source_snapshot_id"
    locators = ref.get("locators")
    if not isinstance(locators, list) or not locators:
        return "invalid_locators:empty"
    for loc in locators:
        if not isinstance(loc, dict):
            return "invalid_locators:not_a_dict"
        start, end = loc.get("start"), loc.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return "invalid_locators:non_integer_offsets"
        if start < 0 or end <= start:
            return "invalid_locators:non_positive_span"
        if not isinstance(loc.get("locator_type"), str) or not loc["locator_type"].strip():
            return "invalid_locators:missing_type"
    exact = ref.get("exact_text")
    if not isinstance(exact, str) or not exact.strip():
        return "invalid_exact_text:empty"
    sha = ref.get("evidence_text_sha256")
    if not isinstance(sha, str) or len(sha) != 64 or not set(sha.lower()) <= _SHA256_HEX:
        return "invalid_evidence_text_sha256:not_sha256_hex"
    if ref.get("eligibility") != "CITATION_ELIGIBLE":
        return "ineligible_evidence:" + str(ref.get("eligibility"))
    if not isinstance(ref.get("source_role"), str) or not ref["source_role"].strip():
        return "invalid_source_role"
    return ""


class VerificationResult:
    """Structured verification result — findings only, no answer text."""

    __slots__ = ("status", "issues", "findings", "failure_reason", "failure_class")

    def __init__(self, status: str, issues: list = None, findings: list = None,
                 failure_reason: str = "", failure_class: str = ""):
        self.status = status
        self.issues = issues if issues is not None else []
        # Per-claim structured findings: {claim_id, verdict, reason, conflict}
        self.findings = findings if findings is not None else []
        self.failure_reason = failure_reason
        # Transport/technical failure class: timeout | empty_response |
        # json_parse_failed | missing_fields | invalid_verdict |
        # http_429 | http_5xx | exception
        self.failure_class = failure_class

    @property
    def passed(self) -> bool:
        """True only for status == PASSED (explicit success)."""
        return self.status == VERIFY_PASSED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "issues": self.issues,
            "findings": self.findings,
            "failure_reason": self.failure_reason,
            "failure_class": self.failure_class,
        }


def _classify_exception(exc: Exception) -> str:
    """Map transport-level exception text to a failure class (RT-025)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in text or "timed out" in text:
        return "timeout"
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return "http_429"
    for code in ("500", "502", "503", "504"):
        if code in text:
            return "http_5xx"
    return "exception"


def _extract_json(text: str):
    """Robust JSON extraction (fenced/prose-wrapped/truncated)."""
    if not text or not text.strip():
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        return json.loads(stripped)
    except Exception:
        pass
    m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if m:
        candidate = m.group(0)
        candidate = re.sub(r",\s*}", "}", candidate)
        candidate = re.sub(r",\s*]", "]", candidate)
        candidate = re.sub(r"[\x00-\x1f]", "", candidate)
        try:
            return json.loads(candidate)
        except Exception:
            pass
    s, e = stripped.find("{"), stripped.rfind("}")
    if s >= 0 and e > s:
        try:
            parsed = json.loads(stripped[s:e + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


# ── RT-025 restricted-input verification prompt ────────────────────────────
# The verifier sees ONLY: question/scope, atomic claims, exact evidence
# excerpts (from grounded EvidenceRefs) and deterministic check outputs.
# No generator reasoning, no unselected retrieval text, no prior prose.
VERIFY_FINAL_PROMPT = """你是独立事实核查员。核验以下原子声明是否被给定证据支持。

规则：
1. 只允许使用下方提供的证据摘录与确定性检查结果，不得使用外部知识。
2. 对每个声明给出判定：PASS（证据明确支持）、FAIL（证据不支持或矛盾）、UNKNOWN（证据不足以判断）。
3. 证据中未出现的数字/实体按 FAIL 或 UNKNOWN 处理，不得猜测。

用户问题：{query}

原子声明：
{claims_block}

证据摘录（精确引用，来自不可变原文快照）：
{evidence_block}

确定性检查结果：
{deterministic_block}

只输出JSON对象（不要输出其他内容）：
{{"claims": [{{"claim_id": "...", "verdict": "PASS|FAIL|UNKNOWN", "reason": "一句话理由"}}], "overall_passed": true}}"""


def build_verifier_input(query: str, atomic_claims: list,
                         evidence_refs: list,
                         deterministic_results: dict = None) -> str:
    """Build the RESTRICTED verifier prompt (RT-025 input allowlist).

    Input-leak defense: anything not passed here cannot reach the verifier.
    Integration tests assert canary strings (generator reasoning / unselected
    text) never appear in the returned prompt.
    """
    claims_block = "\n".join(
        f"- [{c.get('id')}] {str(c.get('text', ''))[:300]}"
        for c in (atomic_claims or []))
    evidence_block = "\n".join(
        f"- [{r.get('evidence_id') or r.get('record_id')}] "
        f"({str(r.get('source_role') or 'unknown')}) "
        f"<record {str(r.get('record_id') or '?')} "
        f"snapshot {str(r.get('source_snapshot_id') or '?')} "
        f"loc {str(r.get('locators') or '?')}> "
        f"{str(r.get('exact_text') or r.get('text') or '')[:400]}"
        for r in (evidence_refs or []))
    deterministic_block = json.dumps(
        deterministic_results or {}, ensure_ascii=False, default=str)[:2000]
    return VERIFY_FINAL_PROMPT.format(
        query=str(query or "")[:500],
        claims_block=claims_block or "（无）",
        evidence_block=evidence_block or "（无）",
        deterministic_block=deterministic_block,
    )


async def verify_final(query: str, atomic_claims: list, evidence_refs: list,
                       deterministic_results: dict = None,
                       max_retries: int = None) -> VerificationResult:
    """RT-025 fail-safe final verifier.

    PASSED requires a well-formed response whose every claim verdict is
    PASS. Any technical failure (timeout/empty/malformed/missing fields/
    invalid verdicts/429/5xx/exception) is UNVERIFIED — never PASSED.
    Semantic findings (FAIL/UNKNOWN verdicts) yield FAILED with structured
    findings; the AnswerStateMachine decides the terminal status.
    """
    if max_retries is None:
        max_retries = MAX_VERIFY_RETRIES

    # Empty claim set: nothing to verify ⇒ cannot claim verification PASSED
    # (RT-025 failure matrix: empty input is a technical failure class).
    if not atomic_claims:
        return VerificationResult(
            VERIFY_UNVERIFIED, failure_reason="empty_input:no_atomic_claims",
            failure_class="empty_response")

    # RT-025 exact EvidenceRef contract: incomplete / non-eligible /
    # structurally invalid refs are a technical failure — UNVERIFIED —
    # never coerced into a well-formed verifier prompt.
    for ref in (evidence_refs or []):
        reason = validate_evidence_ref(ref)
        if reason:
            return VerificationResult(
                VERIFY_UNVERIFIED,
                failure_reason=(
                    f"invalid_evidence_ref:{reason}:"
                    f"{str(ref.get('evidence_id') or ref.get('record_id') or '?')[:64]}"),
                failure_class="invalid_evidence_ref")

    prompt = build_verifier_input(query, atomic_claims, evidence_refs,
                                  deterministic_results)
    last_error, last_class = "", ""

    for attempt in range(max_retries + 1):
        try:
            result_text = await asyncio.wait_for(
                llm_model_func(
                    prompt,
                    system_prompt="你是独立事实核查员。只输出JSON对象，不要输出其他内容。",
                    temperature=0.0,
                    max_tokens=4096,
                    allow_reasoning_fallback=True,  # JSON caller: lenient parser
                ),
                timeout=VERIFY_TIMEOUT,
            )

            if not result_text or not result_text.strip():
                last_error = f"empty_response (attempt {attempt + 1})"
                last_class = "empty_response"
                continue

            parsed = _extract_json(result_text)
            if parsed is None:
                last_error = f"json_parse_failed (attempt {attempt + 1})"
                last_class = "json_parse_failed"
                continue

            raw_claims = parsed.get("claims")
            overall = parsed.get("overall_passed")
            if not isinstance(raw_claims, list) or not isinstance(overall, bool):
                last_error = f"missing_fields (attempt {attempt + 1})"
                last_class = "missing_fields"
                continue

            findings, invalid = [], False
            claim_ids = {str(c.get("id")) for c in atomic_claims}
            for item in raw_claims:
                if not isinstance(item, dict):
                    invalid = True
                    break
                verdict = str(item.get("verdict", "")).strip().upper()
                if verdict not in VALID_CLAIM_VERDICTS:
                    invalid = True
                    break
                findings.append({
                    "claim_id": str(item.get("claim_id", "")),
                    "verdict": verdict,
                    "reason": str(item.get("reason", ""))[:300],
                })
            if invalid:
                # Malformed verdicts are technical failures — never PASS.
                last_error = f"invalid_verdict (attempt {attempt + 1})"
                last_class = "invalid_verdict"
                continue
            if claim_ids and not claim_ids.issubset({f["claim_id"] for f in findings}):
                # Every atomic claim must receive a verdict; omissions are
                # malformed output, not implicit passes.
                last_error = f"incomplete_claim_coverage (attempt {attempt + 1})"
                last_class = "missing_fields"
                continue

            all_pass = all(f["verdict"] == "PASS" for f in findings)
            if overall is True and all_pass:
                return VerificationResult(VERIFY_PASSED, findings=findings)
            # Semantic findings (verifier ran fine, evidence lacks support).
            issues = [f for f in findings if f["verdict"] != "PASS"]
            return VerificationResult(
                VERIFY_FAILED, issues=[{
                    "claim_id": f["claim_id"], "verdict": f["verdict"],
                    "reason": f["reason"],
                } for f in issues], findings=findings)

        except asyncio.TimeoutError:
            last_error = f"timeout (attempt {attempt + 1})"
            last_class = "timeout"
        except Exception as exc:  # noqa: BLE001 — fail-safe catch-all
            cls = _classify_exception(exc)
            last_error = f"{cls} ({type(exc).__name__}: {str(exc)[:120]} attempt {attempt + 1})"
            last_class = cls

    return VerificationResult(
        VERIFY_UNVERIFIED, failure_reason=last_error, failure_class=last_class)


# ── Legacy shim (legacy_hybrid profile path) ───────────────────────────────
# The legacy single-pass path keeps its historical call signature. Two
# Phase-02 correctness fixes apply even here (final spec outranks history):
#   * empty draft is UNVERIFIED, not PASSED (Q096: empty response never PASS)
#   * the verifier no longer returns rewritten_answer — verifier-authored
#     final answers are removed (RT-025).
VERIFY_LEGACY_PROMPT = """你是事实核查专家。审查以下AI生成的回答草稿，检查是否存在认识论错误。

检查类型：OPINION_AS_FACT、PREDICTION_AS_FACT、CLAIM_AS_FACT、ATTRIBUTION_LOST、OVERGENERALIZATION、UNSUPPORTED_CLAIM、TEMPORAL_ERROR、CONFLICT_IGNORED

只输出JSON对象：{{"passed": true|false, "issues": [{{"type": "...", "claim": "..."}}]}}（不要输出其他内容，不得改写回答。）

用户问题：{query}

证据元数据：
{evidence_meta}

AI回答草稿：
{draft_answer}"""


async def verify_with_fail_safe(
    query: str,
    draft_answer: str,
    claim_metadata: list,
    max_retries: int = None,
) -> VerificationResult:
    """Legacy fail-safe verifier (same failure contract as verify_final).

    Guarantees: exception/malformed/empty/timeout → UNVERIFIED, never
    PASSED; PASSED only on an explicit, well-formed {"passed": true}.
    """
    if max_retries is None:
        max_retries = MAX_VERIFY_RETRIES

    if not draft_answer or not draft_answer.strip():
        # Phase-02 fix (Q096): nothing verifiable — UNVERIFIED, never PASS.
        return VerificationResult(
            VERIFY_UNVERIFIED, failure_reason="empty_answer",
            failure_class="empty_response")

    evidence_str = json.dumps(claim_metadata, ensure_ascii=False, indent=2)
    if len(evidence_str) > 4000:
        evidence_str = evidence_str[:4000] + "\n... (truncated)"

    prompt = VERIFY_LEGACY_PROMPT.format(
        query=query, evidence_meta=evidence_str, draft_answer=draft_answer)
    last_error, last_class = "", ""

    for attempt in range(max_retries + 1):
        try:
            result_text = await asyncio.wait_for(
                llm_model_func(
                    prompt,
                    system_prompt="你是事实核查专家。只输出JSON对象，不要输出其他内容。",
                    temperature=0.0,
                    max_tokens=4096,
                    allow_reasoning_fallback=True,
                ),
                timeout=VERIFY_TIMEOUT,
            )
            if not result_text or not result_text.strip():
                last_error = f"empty_response (attempt {attempt + 1})"
                last_class = "empty_response"
                continue
            parsed = _extract_json(result_text)
            if parsed is None:
                last_error = f"json_parse_failed (attempt {attempt + 1})"
                last_class = "json_parse_failed"
                continue
            passed = parsed.get("passed")
            if passed is True:
                return VerificationResult(VERIFY_PASSED)
            if passed is False:
                return VerificationResult(
                    VERIFY_FAILED, issues=parsed.get("issues", []))
            last_error = f"missing_passed_field (attempt {attempt + 1})"
            last_class = "missing_fields"
        except asyncio.TimeoutError:
            last_error = f"timeout (attempt {attempt + 1})"
            last_class = "timeout"
        except Exception as exc:  # noqa: BLE001
            cls = _classify_exception(exc)
            last_error = f"{cls} ({type(exc).__name__}: {str(exc)[:120]} attempt {attempt + 1})"
            last_class = cls

    return VerificationResult(
        VERIFY_UNVERIFIED, failure_reason=last_error, failure_class=last_class)
