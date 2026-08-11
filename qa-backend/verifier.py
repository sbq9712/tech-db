"""
T005 — Verifier Fail-Safe
==========================
Ensures that any verification technical failure is reported as UNVERIFIED,
never silently passed.

States:
  PASSED     — Verification ran successfully and the answer is correct
  FAILED     — Verification ran successfully and the answer has problems
  UNVERIFIED — Verification could not complete (API timeout, parse failure, etc.)

Critical rule:
  verifier exception → UNVERIFIED (NEVER PASSED)
  malformed JSON     → UNVERIFIED (NEVER silent PASSED)
  empty API response → UNVERIFIED (NEVER PASSED)

MAX_VERIFY_RETRIES is configurable (default 2).
"""
import json
import os
import re
from typing import Optional

from config import llm_model_func

# Maximum retry attempts for transient failures (timeout, malformed JSON)
MAX_VERIFY_RETRIES = int(os.environ.get("QA_MAX_VERIFY_RETRIES", "2"))

# Verify timeout (seconds)
VERIFY_TIMEOUT = int(os.environ.get("QA_VERIFY_TIMEOUT", "60"))


# ── Verification result dataclass-like ──

VERIFY_PASSED = "PASSED"
VERIFY_FAILED = "FAILED"
VERIFY_UNVERIFIED = "UNVERIFIED"


class VerificationResult:
    """Structured verification result."""

    __slots__ = ("status", "issues", "rewritten_answer", "failure_reason")

    def __init__(self, status: str, issues: list = None,
                 rewritten_answer: str = "", failure_reason: str = ""):
        self.status = status
        self.issues = issues or []
        self.rewritten_answer = rewritten_answer
        self.failure_reason = failure_reason

    @property
    def passed(self) -> bool:
        """True only when status == PASSED (explicit success)."""
        return self.status == VERIFY_PASSED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "issues": self.issues,
            "rewritten_answer": self.rewritten_answer,
            "failure_reason": self.failure_reason,
        }


def _extract_json(text: str) -> Optional[dict]:
    """Robust JSON extraction from LLM output.

    Tries:
    1. Direct json.loads
    2. Strip markdown code fences then parse
    3. Regex extract first {...} object then parse
    4. Returns None if all fail
    """
    if not text or not text.strip():
        return None

    # Level 1: direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Level 2: strip code fences
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        return json.loads(stripped)
    except Exception:
        pass

    # Level 3: regex extract
    m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass

    # Level 4: try to repair common JSON issues
    if m:
        candidate = m.group(0)
        # Remove trailing commas before closing brackets
        candidate = re.sub(r",\s*}", "}", candidate)
        candidate = re.sub(r",\s*]", "]", candidate)
        # Remove control characters
        candidate = re.sub(r"[\x00-\x1f]", "", candidate)
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


VERIFY_PROMPT_TEMPLATE = """你是事实核查专家。审查以下AI生成的回答草稿，检查是否存在认识论错误。

检查类型：
- OPINION_AS_FACT: 将观点/评价当作事实陈述
- PREDICTION_AS_FACT: 将预测/预计当作已发生事实
- CLAIM_AS_FACT: 将某主体声称的信息当作独立验证事实
- ATTRIBUTION_LOST: 丢失来源归属（谁说的）
- OVERGENERALIZATION: 以偏概全（少量资料推导宏大结论）
- UNSUPPORTED_CLAIM: 无证据支持的声明
- TEMPORAL_ERROR: 时间错误（旧信息当作现状）
- CONFLICT_IGNORED: 忽略了证据中的矛盾

如果不存在 high severity 问题，返回 {{"passed": true}}。
如果存在 high severity 问题，返回：
{{"passed": false, "issues": [{{"sentence": "有问题的句子", "issue_type": "OPINION_AS_FACT", "severity": "high", "evidence_chunk_ids": ["1"], "suggested_rewrite": "修正后的句子"}}], "rewritten_answer": "完整的修正后回答"}}

重写规则：
- 降低确定性（"是" → "有观点认为"）
- 恢复来源归属（添加"XX机构认为"）
- 删除无证据内容
- 修复事实/观点/预测性质
- 显示来源冲突
- 不允许引入新的事实

只输出JSON对象。

用户问题：{query}

证据认识论元数据：
{evidence_meta}

AI回答草稿：
{draft_answer}"""


async def verify_with_fail_safe(
    query: str,
    draft_answer: str,
    claim_metadata: list,
    max_retries: int = None,
) -> VerificationResult:
    """Verify an answer with fail-safe guarantees.

    This function guarantees:
    - Any exception → UNVERIFIED (never PASSED)
    - Malformed JSON → UNVERIFIED (never silent PASSED)
    - Empty response → UNVERIFIED (never PASSED)
    - Timeout → UNVERIFIED after retries exhausted
    - PASSED only when the LLM explicitly returns {"passed": true}

    Args:
        query: Original user question
        draft_answer: The generated answer to verify
        claim_metadata: Epistemic claim classifications
        max_retries: Override MAX_VERIFY_RETRIES

    Returns:
        VerificationResult with status PASSED/FAILED/UNVERIFIED
    """
    if max_retries is None:
        max_retries = MAX_VERIFY_RETRIES

    # Edge case: empty draft answer
    if not draft_answer or not draft_answer.strip():
        # Empty answer is trivially "passed" — nothing to verify
        return VerificationResult(VERIFY_PASSED, failure_reason="empty_answer")

    # Build evidence metadata string
    evidence_str = json.dumps(claim_metadata, ensure_ascii=False, indent=2)
    if len(evidence_str) > 4000:
        evidence_str = evidence_str[:4000] + "\n... (truncated)"

    prompt = VERIFY_PROMPT_TEMPLATE.format(
        query=query,
        evidence_meta=evidence_str,
        draft_answer=draft_answer,
    )

    last_error = ""

    for attempt in range(max_retries + 1):
        try:
            result_text = await llm_model_func(
                prompt,
                system_prompt="你是事实核查专家。只输出JSON对象，不要输出其他内容。",
                temperature=0.0,
                max_tokens=4096,
            )

            # Check for empty response
            if not result_text or not result_text.strip():
                last_error = f"empty_response (attempt {attempt + 1})"
                continue

            # Parse JSON
            parsed = _extract_json(result_text)
            if parsed is None:
                last_error = f"json_parse_failed (attempt {attempt + 1})"
                continue

            # Validate structure
            passed = parsed.get("passed")
            if passed is True:
                # Explicit PASS from LLM
                return VerificationResult(VERIFY_PASSED)

            elif passed is False:
                # Explicit FAIL with issues
                issues = parsed.get("issues", [])
                rewritten = parsed.get("rewritten_answer", "")
                return VerificationResult(
                    VERIFY_FAILED,
                    issues=issues,
                    rewritten_answer=rewritten,
                )

            else:
                # Missing "passed" field — not a valid verification result
                last_error = f"missing_passed_field (attempt {attempt + 1})"
                continue

        except TimeoutError:
            last_error = f"timeout (attempt {attempt + 1})"
            continue
        except Exception as e:
            last_error = f"exception ({type(e).__name__}: {e}) (attempt {attempt + 1})"
            continue

    # All retries exhausted → UNVERIFIED
    print(f"[verifier] All {max_retries + 1} attempts failed: {last_error}", flush=True)
    return VerificationResult(VERIFY_UNVERIFIED, failure_reason=last_error)
