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

import llm_json  # Phase09 shared bounded LLM-JSON normalizer (Class C repair)

from config import llm_model_func
from runtime_safety import RequestCancelled  # cancellation ≠ verifier verdict

# TK-10/T005: bounded retries for transient transport failures only —
# semantic verdicts are never retried away.
MAX_VERIFY_RETRIES = int(os.environ.get("QA_MAX_VERIFY_RETRIES", "2"))

# Verify timeout (seconds) — enforced per LLM call (RT-025 failure matrix).
VERIFY_TIMEOUT = int(os.environ.get("QA_VERIFY_TIMEOUT", "60"))

# RT101-V12 post-mortem (R4): claims per verification LLM call. A 7-9 claim
# single call's JSON envelope truncated twice in the formal window (bounded
# retries exhausted → fail-closed UNVERIFIED). Batching bounds per-call
# output; semantics unchanged (merged findings, all-PASS aggregation).
# Codex V12-repair round P1-3: the env override is CLAMPED to [2, 16] and
# unparseable values fall back to the default — a bad env value must never
# abort import nor recreate oversized calls.
def _bounded_env_int(name: str, default: int, lo: int, hi: int,
                     env=None) -> int:
    raw = str((env if env is not None else os.environ).get(name, "") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, val))


VERIFY_CLAIM_BATCH_SIZE = _bounded_env_int("QA_VERIFY_CLAIM_BATCH_SIZE", 4, 2, 16)

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


def verify_evidence_ref_consistency(ref: dict, snapshot: dict) -> str:
    """RT-025 EvidenceRef ↔ pinned immutable snapshot consistency check.

    Format validation (`validate_evidence_ref`) only proves the ref is
    well-formed. This deterministic (non-LLM) check proves its VALUES match
    the pinned snapshot authority:

        * record_id / source_snapshot_id bind to the pinned snapshot
        * evidence_text_sha256 equals the pinned snapshot's text hash
        * every locator offset is in range of the pinned evidence text
        * snapshot.evidence_text[start:end] equals the locator's exact text
          (concatenated locators rebuild exact_text — tamper-proof)
        * eligibility matches the pinned snapshot

    `snapshot` is the pinned authority dict: {record_id, source_snapshot_id,
    evidence_text (the raw text locators index into), evidence_text_sha256,
    eligibility}. Returns '' when consistent, else a machine-readable
    reason. Fail closed — never coerce a mismatch into PASS.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return "snapshot_not_available"
    if str(ref.get("record_id", "")) != str(snapshot.get("record_id", "")):
        return "record_id_mismatch"
    if str(ref.get("source_snapshot_id", "")) != str(snapshot.get("source_snapshot_id", "")):
        return "source_snapshot_id_mismatch"
    text = snapshot.get("evidence_text")
    if not isinstance(text, str):
        return "snapshot_evidence_text_missing"
    sha = snapshot.get("evidence_text_sha256")
    if isinstance(sha, str) and sha.strip():
        if str(ref.get("evidence_text_sha256", "")).lower() != sha.strip().lower():
            return "evidence_text_sha256_mismatch"
    rebuilt = ""
    for loc in ref.get("locators", []) or []:
        start, end = loc.get("start"), loc.get("end")
        if not isinstance(start, int) or not isinstance(end, int) \
                or isinstance(start, bool) or isinstance(end, bool):
            return "locator_offsets_not_integers"
        if start < 0 or end <= start or end > len(text):
            return "locator_out_of_range"
        rebuilt += text[start:end]
    if not rebuilt:
        return "locator_out_of_range"
    # Tamper check: locators and exact_text must describe the SAME slice of
    # the pinned immutable text (multi-span refs concatenate, matching the
    # grounding contract). A locator that points at different text, or a
    # tampered exact_text, cannot reconstruct from the pinned snapshot.
    if ref.get("exact_text") != rebuilt:
        return "exact_text_mismatch"
    elig = snapshot.get("eligibility") or "CITATION_ELIGIBLE"
    if str(ref.get("eligibility", "")) != str(elig):
        return "eligibility_mismatch"
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
    """Robust JSON extraction (fenced/prose-wrapped/truncated).

    Phase09 general-reliability repair (Class C): delegates FIRST to the
    shared bounded normalizer llm_json.parse_json — adds <think> block
    stripping, provider-envelope unwrapping, full-width punctuation
    folding, and honest truncated-stream closing (drop-not-guess). The
    legacy chain below is preserved verbatim as a final fallback.
    Fail-closed: returns None when nothing parses safely — a response
    that cannot safely be parsed stays a TECHNICAL FAILURE → UNVERIFIED.
    """
    if not text or not text.strip():
        return None
    try:
        val = llm_json.parse_json(text)
    except Exception:
        val = None
    if val is not None:
        return val
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
    # RT101-V12 post-mortem (R1d, generalized): evidence windows now carry
    # real grounded spans (up to the evidence-excerpt budget), so 400 chars
    # truncated away the fact-bearing tail of a genuine span and forced the
    # verifier to judge claims against cut-off text. Bounded 900-char view
    # per ref with an overall block cap keeps the prompt bounded while the
    # verdicts are computed over substantive evidence.
    evidence_lines = []
    for r in (evidence_refs or []):
        ev_text = str(r.get('exact_text') or r.get('text') or '')
        if len(ev_text) > 900:
            ev_text = ev_text[:900] + "…"
        evidence_lines.append(
            f"- [{r.get('evidence_id') or r.get('record_id')}] "
            f"({str(r.get('source_role') or 'unknown')}) "
            f"<record {str(r.get('record_id') or '?')} "
            f"snapshot {str(r.get('source_snapshot_id') or '?')} "
            f"loc {str(r.get('locators') or '?')}> "
            f"{ev_text}")
    # Codex V12-repair round P2-8: truncate at LINE boundaries with an
    # explicit marker — a mid-line cut could otherwise silently drop later
    # refs while the model sees a syntactically complete-looking block.
    evidence_block = ""
    for line in evidence_lines:
        candidate = f"{evidence_block}\n{line}" if evidence_block else line
        if len(candidate) > 24000:
            evidence_block += ("\n… (evidence block truncated at cap; "
                               "remaining refs omitted — do NOT treat "
                               "omitted refs as verified or unverified)")
            break
        evidence_block = candidate
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
                       max_retries: int = None,
                       snapshot_lookup=None, *,
                       retry_owner: str = "verifier",
                       attempt_number: int = 1) -> VerificationResult:
    """RT-025 fail-safe final verifier.

    PASSED requires a well-formed response whose every claim verdict is
    PASS. Any technical failure (timeout/empty/malformed/missing fields/
    invalid verdicts/429/5xx/exception) is UNVERIFIED — never PASSED.
    Semantic findings (FAIL/UNKNOWN verdicts) yield FAILED with structured
    findings; the AnswerStateMachine decides the terminal status.

    `snapshot_lookup` (optional, deterministic — never an LLM) maps an
    EvidenceRef to its pinned immutable snapshot authority dict. When
    provided, refs are additionally checked for VALUE consistency with the
    pinned snapshot (hash / offsets / exact text / eligibility); any
    mismatch is a fail-closed UNVERIFIED, never PASSED.
    """
    if max_retries is None:
        max_retries = MAX_VERIFY_RETRIES
    context_owned = retry_owner == "request_context"
    if context_owned:
        max_retries = 0

    # Empty claim set: nothing to verify ⇒ cannot claim verification PASSED
    # (RT-025 failure matrix: empty input is a technical failure class).
    if not atomic_claims:
        return VerificationResult(
            VERIFY_UNVERIFIED, failure_reason="empty_input:no_atomic_claims",
            failure_class="empty_response")

    # RT-025: non-empty claims with NO evidence at all can never verify to
    # PASSED — there is nothing to check the claims against. Fail closed.
    if not (evidence_refs or []):
        return VerificationResult(
            VERIFY_UNVERIFIED,
            failure_reason="invalid_evidence_ref:no_evidence_refs_for_nonempty_claims",
            failure_class="invalid_evidence_ref")

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

    # RT-025 consistency contract: when a pinned-snapshot authority is
    # injected, a well-formed ref whose VALUES do not match the pinned
    # immutable snapshot (wrong hash, locator pointing at different text,
    # tampered exact_text, foreign-generation snapshot id, mismatched
    # record_id/eligibility) is a fail-closed technical failure.
    if snapshot_lookup is not None:
        for ref in (evidence_refs or []):
            snap = None
            try:
                snap = snapshot_lookup(ref)
            except Exception:
                snap = None
            tag = str(ref.get('evidence_id') or ref.get('record_id') or '?')[:64]
            if not isinstance(snap, dict) or not snap:
                return VerificationResult(
                    VERIFY_UNVERIFIED,
                    failure_reason=f"invalid_evidence_ref:snapshot_not_in_pinned_catalog:{tag}",
                    failure_class="invalid_evidence_ref")
            reason = verify_evidence_ref_consistency(ref, snap)
            if reason:
                return VerificationResult(
                    VERIFY_UNVERIFIED,
                    failure_reason=f"invalid_evidence_ref:{reason}:{tag}",
                    failure_class="invalid_evidence_ref")

    prompt = build_verifier_input(query, atomic_claims, evidence_refs,
                                  deterministic_results)
    last_error, last_class = "", ""

    # ── RT101-V12 post-mortem (R4, generalized): bounded claim batching ──
    # The provider JSON envelope for a 7-9 claim × multi-line evidence
    # verification call at temperature 0 exceeded the bounded retry envelope
    # twice in the formal window (json_parse_failed class → fail-closed
    # UNVERIFIED rows, auto-failing the verifier_technical_pass_max=0
    # threshold). Batching bounds the per-call OUTPUT (the truncation
    # class) without touching verdict semantics: every batch runs the same
    # restricted prompt over the SAME evidence set; findings are merged;
    # overall PASSED only when every batch's claims are all-PASS; a
    # technical failure in ANY batch is a failure of the whole call
    # (fail-closed unchanged). Batch size is env-configurable and bounded.
    batch_size = VERIFY_CLAIM_BATCH_SIZE
    if isinstance(atomic_claims, list) and len(atomic_claims) > batch_size:
        # Codex V12-repair round P1-4: the batch loop is fail-closed
        # CALLER-INDEPENDENTLY — an unexpected exception escaping any
        # nested batch resolves to ONE whole-call UNVERIFIED result here
        # (never a raise), matching the documented "technical failure in
        # any batch is a failure of the whole call". Request cancellation
        # still propagates: a cancelled request is not a verifier verdict.
        try:
            merged_findings: list = []
            merged_issues: list = []
            all_pass = True
            any_failed = False
            for start in range(0, len(atomic_claims), batch_size):
                chunk = atomic_claims[start:start + batch_size]
                result = await verify_final(
                    query, chunk, evidence_refs, deterministic_results,
                    max_retries=max_retries, snapshot_lookup=snapshot_lookup,
                    retry_owner=retry_owner, attempt_number=attempt_number)
                if result.status == VERIFY_UNVERIFIED:
                    return result
                merged_findings.extend(result.findings or [])
                merged_issues.extend(result.issues or [])
                if result.status == VERIFY_FAILED:
                    any_failed = True
                if any(f.get("verdict") != "PASS"
                       for f in result.findings or []):
                    all_pass = False
        except RequestCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-closed, never PASSED
            return VerificationResult(
                VERIFY_UNVERIFIED,
                failure_reason=f"verify_batch_error:{type(exc).__name__}",
                failure_class="exception")
        if any_failed or not all_pass:
            return VerificationResult(VERIFY_FAILED, issues=merged_issues,
                                      findings=merged_findings)
        return VerificationResult(VERIFY_PASSED, findings=merged_findings)


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
            if context_owned:
                raise
            last_error = f"timeout (attempt {attempt + 1})"
            last_class = "timeout"
        except Exception as exc:  # noqa: BLE001 — fail-safe catch-all
            if context_owned:
                raise
            cls = _classify_exception(exc)
            last_error = f"{cls} ({type(exc).__name__}: {str(exc)[:120]} attempt {attempt + 1})"
            last_class = cls

    if context_owned:
        raise ValueError(
            f"invalid schema rejection: final verifier: {last_class or last_error}")
    return VerificationResult(
        VERIFY_UNVERIFIED, failure_reason=last_error, failure_class=last_class)


# ── Legacy shim (legacy_hybrid profile path) ───────────────────────────────
# The legacy single-pass path keeps its historical call signature. Two
# Phase-02 correctness fixes apply even here (final spec outranks history):
#   * empty draft is UNVERIFIED, not PASSED (Q096: empty response never PASS)
#   * the verifier no longer returns rewritten_answer — verifier-authored
#     final answers are removed (RT-025).
VERIFY_ATOMIC_CLAIMS_PROMPT = """你是独立事实核查员。核验以下AI回答中的原子声明是否被给定证据元数据支持。

规则：
1. 只允许使用下方提供的证据元数据，不得使用外部知识。
2. 对每个声明给出判定：PASS（证据明确支持）、FAIL（证据不支持、矛盾或属认识论错误：观点当事实/预测当事实/归属丢失/过度概括/时间错误）、UNKNOWN（证据不足以判断）。
3. 证据中未出现的数字/实体按 FAIL 或 UNKNOWN 处理，不得猜测。

用户问题：{query}

原子声明：
{claims_block}

证据元数据：
{evidence_meta}

AI回答草稿：
{draft_answer}

只输出JSON对象（不要输出其他内容）：
{{"claims": [{{"claim_id": "<方括号中的声明ID>", "verdict": "PASS|FAIL|UNKNOWN", "reason": "一句话理由"}}], "overall_passed": true}}"""


VERIFY_LEGACY_PROMPT = """你是事实核查专家。审查以下AI生成的回答草稿，检查是否存在认识论错误。

检查类型：OPINION_AS_FACT、PREDICTION_AS_FACT、CLAIM_AS_FACT、ATTRIBUTION_LOST、OVERGENERALIZATION、UNSUPPORTED_CLAIM、TEMPORAL_ERROR、CONFLICT_IGNORED

规则：
1. 对草稿中的每一条事实性声明逐条给出判定：PASS（证据元数据明确支持）、FAIL（证据不支持、矛盾或属认识论错误）、UNKNOWN（证据不足以判断）。
2. 证据元数据中未出现的数字/实体按 FAIL 或 UNKNOWN 处理，不得猜测。
3. 不得改写回答；每条声明都必须出现在 claims 数组中（遗漏即技术失败）。

只输出JSON对象（不要输出其他内容）：
{{"claims": [{{"claim_id": "<声明原文的前20字>", "verdict": "PASS|FAIL|UNKNOWN", "reason": "一句话理由"}}], "overall_passed": true}}

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
    *,
    retry_owner: str = "verifier",
    attempt_number: int = 1,
    atomic_claims: list = None,
) -> VerificationResult:
    """Legacy fail-safe verifier (same failure contract as verify_final).

    Guarantees: exception/malformed/empty/timeout → UNVERIFIED, never
    PASSED; PASSED only on an explicit, well-formed {"passed": true}.

    RT101-V13 post-mortem (Repair A): when ``atomic_claims`` (the canonical
    claim-map rows: [{"id": "claim_1", "text": ...}, ...]) is provided, the
    verifier runs the STRUCTURED per-claim contract — every atomic claim
    must receive an explicit PASS/FAIL/UNKNOWN verdict, omissions and
    malformed verdicts stay technical failures (fail-closed UNVERIFIED),
    and semantic FAIL carries structured ``findings`` so the per-claim
    display-authorization seam can distinguish verified-supported claims
    from flagged ones. Overall FAILED semantics are unchanged; only the
    verdict TRANSPORT is added. ``atomic_claims=None`` preserves the
    historical whole-draft contract for any other callers.
    """
    if max_retries is None:
        max_retries = MAX_VERIFY_RETRIES
    context_owned = retry_owner == "request_context"
    if context_owned:
        max_retries = 0

    if not draft_answer or not draft_answer.strip():
        # Phase-02 fix (Q096): nothing verifiable — UNVERIFIED, never PASS.
        return VerificationResult(
            VERIFY_UNVERIFIED, failure_reason="empty_answer",
            failure_class="empty_response")

    evidence_str = json.dumps(claim_metadata, ensure_ascii=False, indent=2)
    if len(evidence_str) > 4000:
        evidence_str = evidence_str[:4000] + "\n... (truncated)"
    # RT101-V14 semantic qualification (generalized P0): text-bearing
    # evidence rows ({evidence_id, record_id, title, date, text}) must not
    # be truncated away by the label-metadata budget. Serialize them in a
    # DEDICATED budget ahead of the labels so the verifier prompt always
    # carries the actual evidence text (verdict semantics unchanged).
    _text_rows = []
    try:
        _text_rows = [row for row in (claim_metadata or [])
                      if isinstance(row, dict) and row.get("text")
                      and row.get("evidence_id")]
    except Exception:
        _text_rows = []
    if _text_rows:
        _plain = [row for row in (claim_metadata or [])
                  if not (isinstance(row, dict) and row.get("text")
                          and row.get("evidence_id"))]
        _lab = json.dumps(_plain, ensure_ascii=False, indent=2)
        # RT101-V14 semantic qualification repair (R2 companion): the
        # evidence rows now carry query-relevant windows (800 chars
        # context / 1200 cited rows — the same view the generator wrote
        # the draft from), so the dedicated text budget grows to fit
        # them; the label budget and verdict semantics are unchanged.
        _txt_budget = 16000
        _txt = json.dumps(_text_rows, ensure_ascii=False, indent=2)
        if len(_txt) > _txt_budget:
            _txt = _txt[:_txt_budget] + "\n... (truncated)"
        evidence_str = ("// 证据文本（核实声明依据）:\n" + _txt
                        + "\n// 认识论标签元数据:\n" + _lab)
        if len(evidence_str) > 20000:
            evidence_str = evidence_str[:20000] + "\n... (truncated)"

    _structured = isinstance(atomic_claims, list) and bool(atomic_claims)
    if _structured:
        claims_block = "\n".join(
            f"- [{str(c.get('id'))}] {str(c.get('text', ''))[:200]}"
            for c in atomic_claims if isinstance(c, dict) and c.get("id"))
        if not claims_block:
            _structured = False
        else:
            prompt = VERIFY_ATOMIC_CLAIMS_PROMPT.format(
                query=query, claims_block=claims_block,
                evidence_meta=evidence_str, draft_answer=draft_answer)
    if not _structured:
        prompt = VERIFY_LEGACY_PROMPT.format(
            query=query, evidence_meta=evidence_str,
            draft_answer=draft_answer)
    last_error, last_class = "", ""

    # Phase09 repair RD-3 (corpus adjudication): bounded transient retry
    # for request-scoped verification.  The V5 formal run recorded 3/15
    # verifier window exhaustions; request-scoped calls previously had
    # ZERO internal tolerance — a single transient empty/parse hiccup
    # converted directly into a technical failure.  This retry is
    # strictly bounded: at most ONE extra attempt for TRANSIENT classes
    # only (empty response / malformed JSON / missing field).  It can
    # never extend any deadline: the enclosing run_stage owns the hard
    # stage deadline and still cancels an overrunning retry mid-flight.
    # Timeouts and provider exceptions keep their fail-closed behavior
    # (raised to RequestExecutionContext) — a call that consumed its
    # whole window is a genuine technical failure, never retried into
    # a PASS.
    _transient_extra_attempts = 1 if context_owned else 0
    # RT101-V13 post-mortem (Repair E): truly transient TRANSPORT classes
    # join the bounded single extra attempt for request-scoped calls —
    # a 429/5xx/timeout hiccup on an otherwise healthy call previously
    # consumed the whole formal verification window as a technical
    # failure (V13 case_09 class). Semantic and malformed-content classes
    # are NOT retryable (never re-ask a verifier that already answered).
    # The enclosing run_stage still owns every deadline and can cancel an
    # overrunning retry mid-flight; a call that exhausts its window stays
    # a genuine technical failure — fail-closed is unchanged.
    _transient_classes = {"empty_response", "json_parse_failed",
                          "missing_fields", "http_429", "http_5xx",
                          "timeout"}
    _total_attempts = max_retries + 1 + _transient_extra_attempts
    _attempts_made = 0

    for attempt in range(_total_attempts):
        _attempts_made = attempt + 1
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
            else:
                parsed = _extract_json(result_text)
                if parsed is None:
                    last_error = f"json_parse_failed (attempt {attempt + 1})"
                    last_class = "json_parse_failed"
                else:
                    # RT101-V13 post-mortem (Repair A, generalized): the
                    # legacy verifier previously returned FAILED with
                    # `issues` only and NEVER populated `findings`, so the
                    # display-authorization seam (which requires per-claim
                    # verdicts) withheld EVERY citation whenever the overall
                    # verdict was FAILED — even for claims the verifier
                    # did not flag (V13 formal: authorized=0 in 14/15
                    # answer cases). The legacy verifier now uses the SAME
                    # structured per-claim verdict contract as verify_final:
                    #   * every fact-bearing claim in the draft MUST receive
                    #     a verdict; omissions/malformed verdicts stay
                    #     technical failures (fail-closed UNVERIFIED);
                    #   * PASSED only when every verdict is PASS;
                    #   * FAILED carries the structured `findings` so the
                    #     downstream per-claim authorization can keep
                    #     authority for claims the verifier explicitly
                    #     PASSED while still withholding flagged ones.
                    # Verifier semantics are unchanged: a technical failure
                    # can never PASS, and per-claim authority still requires
                    # SUPPORTED + explicit PASS at the authorization seam.
                    raw_claims = parsed.get("claims")
                    overall = parsed.get("overall_passed")
                    if overall is None:
                        # Historical legacy bare shape is
                        # {"passed": bool, "issues": [...]} — the
                        # pre-repair verifier prompt contract (and every
                        # historical caller/test) uses the "passed" key;
                        # the structured contract adds "overall_passed".
                        # Accept both; a structured response never carries
                        # "passed" alone, so this cannot blur the two
                        # contracts.
                        overall = parsed.get("passed")
                    legacy_issues = parsed.get("issues")
                    if raw_claims is None and isinstance(overall, bool):
                        # Backward-compatible shape: bare
                        # {"passed": bool, "issues": [...]} with no
                        # per-claim verdicts. A FAIL in this shape carries
                        # NO verdict evidence for any specific claim —
                        # mapping it onto per-claim authority would let an
                        # unattributed global verdict revoke (or grant)
                        # individual claim authority. Fail closed to the
                        # historical behavior: PASSED (no findings) or
                        # FAILED with issues only (no findings) — the
                        # authorization seam then treats all claims as
                        # NOT_PASSED exactly as before this repair.
                        if overall is True:
                            return VerificationResult(VERIFY_PASSED)
                        return VerificationResult(
                            VERIFY_FAILED,
                            issues=(legacy_issues if isinstance(
                                legacy_issues, list) else []))
                    if not isinstance(raw_claims, list) or not isinstance(
                            overall, bool):
                        last_error = (f"missing_fields (attempt "
                                      f"{attempt + 1})")
                        last_class = "missing_fields"
                        continue
                    findings, invalid = [], False
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
                        # Malformed verdicts are technical failures —
                        # never PASS.
                        last_error = (f"invalid_verdict (attempt "
                                      f"{attempt + 1})")
                        last_class = "invalid_verdict"
                        continue
                    if not findings:
                        # A draft with fact-bearing claims always yields at
                        # least one claim row; an empty claims array means
                        # the verifier did not answer the question.
                        last_error = (f"incomplete_claim_coverage "
                                      f"(attempt {attempt + 1})")
                        last_class = "missing_fields"
                        continue
                    if _structured:
                        # RT-025 coverage contract (same as verify_final):
                        # every atomic claim must receive a verdict;
                        # omissions are malformed output, not implicit
                        # passes. Fail closed — UNVERIFIED.
                        want_ids = {str(c.get("id")) for c in atomic_claims
                                    if isinstance(c, dict) and c.get("id")}
                        got_ids = {f["claim_id"] for f in findings}
                        if not want_ids.issubset(got_ids):
                            last_error = (
                                f"incomplete_claim_coverage "
                                f"(attempt {attempt + 1})")
                            last_class = "missing_fields"
                            continue
                    all_pass = all(f["verdict"] == "PASS" for f in findings)
                    if overall is True and all_pass:
                        return VerificationResult(VERIFY_PASSED,
                                                  findings=findings)
                    # Semantic findings (verifier ran fine, evidence lacks
                    # support for the flagged subset).
                    issues = [f for f in findings if f["verdict"] != "PASS"]
                    return VerificationResult(
                        VERIFY_FAILED, issues=[{
                            "claim_id": f["claim_id"],
                            "verdict": f["verdict"],
                            "reason": f["reason"],
                        } for f in issues], findings=findings)
            if (last_class in _transient_classes
                    and _attempts_made < _total_attempts):
                # Codex review A2 P1 fix: transient retry budget is
                # caller-agnostic (bounded by _total_attempts).  Legacy
                # callers keep their historical retry-on-transient
                # contract within max_retries; request-scoped callers
                # get exactly the single extra bounded attempt (RD-3).
                print(f"[verify] transient ({last_class}); bounded retry "
                      f"{_attempts_made}/{_total_attempts - 1}",
                      flush=True)
                continue
        except asyncio.TimeoutError:
            if context_owned:
                raise
            last_error = f"timeout (attempt {attempt + 1})"
            last_class = "timeout"
            # RT101-V13 Repair E: transport hiccups join the historical
            # legacy transient-retry contract (still bounded by
            # max_retries — never an unbounded wait). Unknown exception
            # classes do NOT retry.
            if (last_class in _transient_classes
                    and _attempts_made < _total_attempts):
                print(f"[verify] transient ({last_class}); bounded retry "
                      f"{_attempts_made}/{_total_attempts - 1}",
                      flush=True)
                continue
        except Exception as exc:  # noqa: BLE001
            if context_owned:
                raise
            cls = _classify_exception(exc)
            last_error = f"{cls} ({type(exc).__name__}: {str(exc)[:120]} attempt {attempt + 1})"
            last_class = cls
            if (cls in _transient_classes
                    and _attempts_made < _total_attempts):
                print(f"[verify] transient ({cls}); bounded retry "
                      f"{_attempts_made}/{_total_attempts - 1}",
                      flush=True)
                continue
        break

    if context_owned:
        raise ValueError(
            f"invalid schema rejection: final verifier: {last_class or last_error}")
    return VerificationResult(
        VERIFY_UNVERIFIED, failure_reason=last_error, failure_class=last_class)
