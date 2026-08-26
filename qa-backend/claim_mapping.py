"""
T004 — Atomic Claim + Claim-level Citation Mapping
===================================================
Splits the generated answer into atomic claims, classifies each claim's
type, and maps each claim to the citations that support it.

Claim Classification:
  MAJOR_FACT          — Key factual statement that requires citation
  NUMERIC_FACT        — Numbers, statistics, specifications
  COMPARISON          — A > B, A is better/worse than B
  CAUSAL              — A causes/enables/leads to B
  ATTRIBUTED_CLAIM    — Someone claims/states X
  MINOR_EXPLANATION   — Connective text, transitions, general context
                        (does NOT require citation)

Support Relations:
  DIRECT_SUPPORT      — Citation directly contains and confirms the claim
  PREMISE_SUPPORT     — Citation provides a premise from which claim follows
  ATTRIBUTION         — Citation is the source of an attributed claim
  CONTRADICTS         — Citation contradicts the claim
  BACKGROUND          — Citation provides relevant context but not direct support

Rules:
  - Major claims without support are UNSUPPORTED → must be deleted/weakened/re-retrieved
  - Every major claim must be mapped to at least one citation
"""
import json
import os
import re
from typing import Optional

from config import llm_model_func


# ── Claim types ──

CLAIM_TYPES = {
    "MAJOR_FACT": "关键事实 — 需要引用支持",
    "NUMERIC_FACT": "数字事实 — 参数、统计数据、规格",
    "COMPARISON": "比较 — A优于/劣于B",
    "CAUSAL": "因果 — A导致/促进/阻碍B",
    "ATTRIBUTED_CLAIM": "归属声明 — 某主体声称/公布的信息",
    "MINOR_EXPLANATION": "解释性文字 — 连接、过渡、背景（不要求引用）",
}

# Claims that REQUIRE citation support
MAJOR_CLAIM_TYPES = {"MAJOR_FACT", "NUMERIC_FACT", "COMPARISON", "CAUSAL", "ATTRIBUTED_CLAIM"}

# ── Support relations ──

SUPPORT_RELATIONS = {
    "DIRECT_SUPPORT": "来源直接包含并确认该声明",
    "PREMISE_SUPPORT": "来源提供前提，由此可推出声明",
    "ATTRIBUTION": "来源是归属声明的出处",
    "CONTRADICTS": "来源与声明矛盾",
    "BACKGROUND": "来源提供相关背景但不直接支持",
}

# ── Claim status ──

CLAIM_SUPPORTED = "SUPPORTED"
CLAIM_PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
CLAIM_UNSUPPORTED = "UNSUPPORTED"


CLAIM_MAPPING_PROMPT = """你是技术情报分析专家。将以下AI回答分解为原子声明，并为每个声明匹配引用来源。

声明分类：
- MAJOR_FACT: 关键事实陈述（需要引用）
- NUMERIC_FACT: 数字/参数/统计数据（需要引用）
- COMPARISON: 比较（A优于/劣于B）（需要引用）
- CAUSAL: 因果关系（A导致B）（需要引用）
- ATTRIBUTED_CLAIM: 归属声明（某主体声称/公布）（需要引用）
- MINOR_EXPLANATION: 解释性文字（连接、过渡，不需要引用）

支持关系：
- DIRECT_SUPPORT: 来源直接包含并确认该声明
- PREMISE_SUPPORT: 来源提供前提，由此可推出声明
- ATTRIBUTION: 来源是归属声明的出处
- CONTRADICTS: 来源与声明矛盾
- BACKGROUND: 来源提供相关背景但不直接支持

规则：
1. 每个主要声明(MAJOR_*)必须至少有一个支持引用
2. 如果没有引用支持某主要声明，标注为 UNSUPPORTED
3. citation_id 对应来源列表中的序号
4. evidence_span 必须来自该来源的原文，不得编造

只输出JSON：
{{
  "claims": [
    {{
      "id": "claim_1",
      "text": "声明原文（从回答中提取的原文）",
      "type": "MAJOR_FACT",
      "support_status": "SUPPORTED",
      "supported_by": [
        {{
          "citation_id": 1,
          "relation": "DIRECT_SUPPORT",
          "evidence_span": "该来源中支持此声明的原文片段"
        }}
      ]
    }}
  ]
}}

用户问题：{query}

来源列表：
{source_list}

AI回答：
{answer}"""


async def map_claims_to_citations(
    query: str,
    answer: str,
    citations: list,
    *,
    retry_owner: str = "mapper",
    attempt_number: int = 1,
) -> dict:
    """Decompose answer into atomic claims and map each to supporting citations.

    Args:
        query: Original user question
        answer: Generated answer text
        citations: List of citation dicts (from build_context)

    Returns:
        {
            "claims": [
                {
                    "id": "claim_1",
                    "text": "...",
                    "type": "MAJOR_FACT",
                    "support_status": "SUPPORTED" | "UNSUPPORTED" | ...,
                    "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT", ...}],
                }
            ]
        }
    """
    if not answer or not answer.strip():
        return {"claims": []}

    if not citations:
        # No citations to map — all claims are unsupported
        return {"claims": []}

    # Legacy callers retain the historical internal two-attempt behavior.
    # Request-scoped production callers set retry_owner=request_context: one
    # LLM call is made and any failure is raised to RequestExecutionContext,
    # which exclusively owns retry, cancellation, deadlines, and backoff.
    payloads = ((12, 4000), (8, 1500))
    context_owned = retry_owner == "request_context"
    selected = [payloads[min(max(int(attempt_number), 1), 2) - 1]] \
        if context_owned else list(payloads)
    last_error = None
    for attempt, (n_src, ans_cap) in enumerate(selected, start=1):
        source_list = "\n".join(
            f"[{c['id']}] {c.get('title', '')} ({c.get('date', '')}, {c.get('source', '')})"
            for c in citations[:n_src]
        )
        prompt = CLAIM_MAPPING_PROMPT.format(
            query=query[:500], source_list=source_list, answer=answer[:ans_cap],
        )
        try:
            result_text = await llm_model_func(
                prompt,
                system_prompt="你是技术情报分析专家。只输出JSON，不要输出其他内容。",
                temperature=0.0,
                max_tokens=8192,  # GLM-5.2 reasoning headroom
                allow_reasoning_fallback=True,  # JSON caller: lenient parser downstream
            )
            parsed = _extract_json_safe(result_text)
            if parsed and "claims" in parsed:
                claims = []
                for claim in parsed["claims"]:
                    c = _validate_claim(claim, citations)
                    if c:
                        claims.append(c)
                return {"claims": claims}
            last_error = ValueError("invalid schema rejection: claim mapping")
        except Exception as e:
            last_error = e
            print(f"[claim_mapping] Error (attempt {attempt}/{len(selected)}): {e}",
                  flush=True)
            if not context_owned and attempt == 1:
                import asyncio as _aio
                await _aio.sleep(2)

    if context_owned:
        raise last_error or RuntimeError("claim mapping failed")

    # TK-24 deterministic fallback: LLM mapping unavailable → map the
    # answer's own [n] anchors (strictly grounded, no hallucinated mapping)
    fb = _anchor_fallback_claims(answer, citations)
    if fb["claims"]:
        print(f"[claim_mapping] LLM mapping failed → anchor fallback "
              f"({len(fb['claims'])} claims)", flush=True)
        return fb
    return {"claims": []}


def _extract_json_safe(text: str) -> Optional[dict]:
    """Robust JSON extraction with multiple fallback strategies."""
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
        try:
            return json.loads(m.group(0))
        except Exception:
            # Try repair
            candidate = m.group(0)
            candidate = re.sub(r",\s*}", "}", candidate)
            candidate = re.sub(r",\s*]", "]", candidate)
            candidate = re.sub(r"[\x00-\x1f]", "", candidate)
            try:
                return json.loads(candidate)
            except Exception:
                pass

    # GLM-5.2 with long inputs returns prose-wrapped JSON: take the widest
    # balanced-looking object span as a last resort.
    s, e = stripped.find("{"), stripped.rfind("}")
    if s >= 0 and e > s:
        try:
            parsed = json.loads(stripped[s:e + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _validate_claim(claim: dict, citations: list) -> Optional[dict]:
    """Validate and normalize a claim dict from LLM output."""
    if not isinstance(claim, dict):
        return None

    text = claim.get("text", "").strip()
    if not text:
        return None

    claim_type = claim.get("type", "MINOR_EXPLANATION")
    if claim_type not in CLAIM_TYPES:
        claim_type = "MINOR_EXPLANATION"

    support_status = claim.get("support_status", CLAIM_UNSUPPORTED)
    supported_by_raw = claim.get("supported_by", [])

    # Validate citation references
    valid_citation_ids = {c["id"] for c in citations}
    supported_by = []
    for ref in supported_by_raw:
        if not isinstance(ref, dict):
            continue
        cid = ref.get("citation_id")
        if cid in valid_citation_ids:
            relation = ref.get("relation", "BACKGROUND")
            if relation not in SUPPORT_RELATIONS:
                relation = "BACKGROUND"
            supported_by.append({
                "citation_id": cid,
                "relation": relation,
                "evidence_span": ref.get("evidence_span", ""),
            })

    # Determine final support status
    is_major = claim_type in MAJOR_CLAIM_TYPES
    if not is_major:
        support_status = "MINOR"  # minor claims don't need support
    elif supported_by:
        has_direct = any(r["relation"] in ("DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION")
                         for r in supported_by)
        has_contradicts = any(r["relation"] == "CONTRADICTS" for r in supported_by)
        if has_direct and not has_contradicts:
            support_status = CLAIM_SUPPORTED
        elif has_direct and has_contradicts:
            support_status = CLAIM_PARTIALLY_SUPPORTED
        elif has_contradicts:
            support_status = CLAIM_UNSUPPORTED
        else:
            support_status = CLAIM_PARTIALLY_SUPPORTED
    else:
        support_status = CLAIM_UNSUPPORTED

    return {
        "id": claim.get("id", f"claim_{hash(text) % 10000}"),
        "text": text,
        "type": claim_type,
        "support_status": support_status,
        "supported_by": supported_by,
    }


def get_unsupported_major_claims(claims_mapping: dict) -> list:
    """Get all major claims that are UNSUPPORTED (must be deleted/weakened/re-retrieved).

    Returns list of claim dicts with support_status == CLAIM_UNSUPPORTED
    and type in MAJOR_CLAIM_TYPES.
    """
    claims = claims_mapping.get("claims", [])
    return [
        c for c in claims
        if c.get("support_status") == CLAIM_UNSUPPORTED
        and c.get("type") in MAJOR_CLAIM_TYPES
    ]


def get_claim_citation_map(claims_mapping: dict) -> dict:
    """Get a mapping from claim_id → list of citation_ids that support it.

    Returns:
        {"claim_1": [1, 3], "claim_2": [2], ...}
    """
    result = {}
    for claim in claims_mapping.get("claims", []):
        cid = claim.get("id", "")
        supported_by = claim.get("supported_by", [])
        citation_ids = [ref["citation_id"] for ref in supported_by
                        if ref.get("relation") != "CONTRADICTS"]
        if cid:
            result[cid] = citation_ids
    return result


def _anchor_fallback_claims(answer: str, citations: list) -> dict:
    """Deterministic no-LLM fallback (TK-24): map [n]-anchored sentences.

    When the GLM claim-mapping call fails (transport flakes observed live:
    "Remote end closed connection"), build the claim map from the answer's
    own citation anchors — every sentence containing [n] becomes a claim
    supported by citation n. This is strictly grounded (no hallucinated
    mapping) and keeps the evidence card alive under API instability.
    """
    import re as _re
    valid = {c["id"] for c in citations}
    claims = []
    # split into sentences (。！？!?) and strip markdown table pipes for spans
    sentences = [s.strip() for s in _re.split(r"(?<=[。！？!?])\s*", answer) if s.strip()]
    n = 0
    for s in sentences:
        anchors = [int(m) for m in _re.findall(r"\[(\d+)\]", s)
                   if int(m) in valid]
        if not anchors or len(s) < 8:
            continue
        n += 1
        text = s if len(s) <= 120 else s[:117] + "..."
        # Honesty contract (codex review P1): the [n] anchor was placed by
        # the ANSWER GENERATOR, not verified against the citation body. Keep
        # the citation association as BACKGROUND context + PARTIALLY_SUPPORTED
        # — never upgrade unverified answer content to DIRECT/SUPPORTED.
        claims.append({
            "id": f"claim_{n}",
            "text": text,
            "type": "ATTRIBUTED_CLAIM",
            "support_status": "PARTIALLY_SUPPORTED",
            "supported_by": [
                {"citation_id": cid, "relation": "BACKGROUND",
                 "evidence_span": citations[[i for i, c in enumerate(citations)
                                             if c["id"] == cid][0]]
                 .get("body_snippet", "")[:80]}
                for cid in sorted(set(anchors))[:3]
            ],
        })
    return {"claims": claims, "fallback": "anchor_extraction (LLM mapping unavailable)"}


# ── T048: span-level source lineage attachment ────────────────────────────

def attach_span_lineage(claims_mapping: dict,
                        citations: list,
                        provenance_map: Optional[dict] = None,
                        records_by_id: Optional[dict] = None) -> dict:
    """Attach T048 span lineage to every claim support entry (in place).

    Deterministic (no LLM): each supported_by entry gains record_id (from
    its citation) and span_lineage (from provenance.span_lineage). After
    this, provenance.claim_independence_report() can count independence
    per claim/span instead of per document.

    Returns the same claims_mapping for chaining.
    """
    from provenance import span_lineage as _span_lineage
    provenance_map = provenance_map or {}
    records_by_id = records_by_id or {}
    cid_to_record = {}
    for c in citations or []:
        cid_to_record[c.get("id")] = c.get("record_id")

    for claim in claims_mapping.get("claims", []):
        for ref in claim.get("supported_by", []) or []:
            rid = ref.get("record_id") or cid_to_record.get(ref.get("citation_id"))
            if rid is None:
                continue
            ref["record_id"] = rid
            rec = records_by_id.get(rid, {})
            pm = provenance_map.get(rid, {})
            ref["span_lineage"] = _span_lineage(
                rec, pm, ref.get("evidence_span", ""))
    return claims_mapping


def claim_independence(claims_mapping: dict,
                       provenance_map: Optional[dict] = None,
                       records_by_id: Optional[dict] = None) -> dict:
    """Per-claim independence accounting (T048) — see
    provenance.claim_independence_report."""
    from provenance import claim_independence_report
    return claim_independence_report(
        claims_mapping.get("claims", []),
        records_by_id=records_by_id,
        provenance_map=provenance_map,
    )


# ══════════════════════════════════════════════════════════════════════════
# Phase 02 — RT-021: typed support relations + deterministic entailment
# ══════════════════════════════════════════════════════════════════════════
# The LLM mapping pass proposes relations; this pass ENFORCES them against
# the exactly-grounded evidence text (RT-020 output). Rules (final spec §5):
#   * only DIRECT_SUPPORT / PREMISE_SUPPORT / ATTRIBUTION carry support;
#     BACKGROUND and CONTRADICTS never count as support;
#   * deterministic entailment (entailment.py layer 1, use_llm=False) runs on
#     every DIRECT/PREMISE entry over the grounded span text:
#       REFUTES            → relation becomes CONTRADICTS
#       NEUTRAL (entity    → relation downgraded to BACKGROUND
#       absent, det.)      
#       ENTAILS            → kept, annotated
#       ambiguous (no hard rule fired) → kept, annotated as ambiguous
#   * a support relation whose citation has NO grounded exact text (RT-020
#     INVALID) is downgraded to BACKGROUND — an invalid citation cannot
#     support a claim;
#   * vendor/self-reported evidence (evidence_role self_reported/vendor/
#     press_release) caps performance claims (NUMERIC_FACT / COMPARISON /
#     CAUSAL) at ATTRIBUTION — never DIRECT_SUPPORT.

RELATION_CHECK_VERSION = "1.0.0"

SUPPORTING_RELATIONS = ("DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION")

PERFORMANCE_CLAIM_TYPES = {"NUMERIC_FACT", "COMPARISON", "CAUSAL"}
ATTRIBUTION_ONLY_EVIDENCE_ROLES = {"self_reported", "vendor", "press_release"}


def apply_relation_checks(claims_mapping: dict,
                          evidence_index: Optional[dict] = None) -> dict:
    """RT-021: enforce typed relations + deterministic entailment (in place).

    Args:
        claims_mapping: output of map_claims_to_citations ({"claims": [...]})
        evidence_index: {citation_id: {"text": <grounded exact evidence text>,
                                       "record_id": ...,
                                       "evidence_role": ...}}
            Entries absent from the index mean "no valid exact grounding for
            that citation" → its support relations downgrade to BACKGROUND.

    Returns the same mapping (for chaining) with mapping["relation_checks"]
    appended:
        {version, entries_checked, role_capped, entailment_verified,
         downgraded_to_background, contradicted, unsupported_after}
    """
    from entailment import check_entailment, EntailmentLabel

    evidence_index = evidence_index or {}
    stats = {
        "version": RELATION_CHECK_VERSION,
        "entries_checked": 0,
        "role_capped": 0,
        "entailment_verified": 0,
        "entailment_ambiguous": 0,
        "downgraded_to_background": 0,
        "contradicted": 0,
        "unsupported_after": 0,
    }

    for claim in claims_mapping.get("claims", []):
        claim_type = claim.get("type", "MINOR_EXPLANATION")
        for ref in claim.get("supported_by", []) or []:
            stats["entries_checked"] += 1
            relation = ref.get("relation", "BACKGROUND")
            ev = evidence_index.get(ref.get("citation_id")) or {}
            ev_text = (ev.get("text") or "").strip()

            if relation not in SUPPORTING_RELATIONS:
                # BACKGROUND / CONTRADICTS carry no support by definition.
                continue

            # (a) No valid exact grounding for this citation → no support.
            if not ev_text:
                ref["relation"] = "BACKGROUND"
                ref["relation_check"] = "no_grounded_evidence"
                stats["downgraded_to_background"] += 1
                continue

            # (b) Vendor/self-report role caps performance claims.
            role = (ev.get("evidence_role") or "").strip().lower()
            if (role in ATTRIBUTION_ONLY_EVIDENCE_ROLES
                    and claim_type in PERFORMANCE_CLAIM_TYPES
                    and relation in ("DIRECT_SUPPORT", "PREMISE_SUPPORT")):
                ref["relation"] = "ATTRIBUTION"
                ref["relation_check"] = f"role_cap:{role}"
                stats["role_capped"] += 1
                continue

            # (c) ATTRIBUTION needs no entailment (source IS the claim's
            # origin — the vendor doc saying X supports "vendor claims X").
            if relation == "ATTRIBUTION":
                ref["relation_check"] = "attribution_origin"
                continue

            # (d) Deterministic entailment over the grounded span text.
            result = check_entailment(claim.get("text", ""), ev_text,
                                      use_llm=False)
            label = result.label
            if label == EntailmentLabel.REFUTES:
                ref["relation"] = "CONTRADICTS"
                ref["relation_check"] = f"entailment_refutes:{result.reason[:80]}"
                stats["contradicted"] += 1
            elif label == EntailmentLabel.NEUTRAL and result.method == "deterministic":
                ref["relation"] = "BACKGROUND"
                ref["relation_check"] = f"entailment_neutral:{result.reason[:80]}"
                stats["downgraded_to_background"] += 1
            elif label == EntailmentLabel.ENTAILS:
                ref["relation_check"] = "entailment_verified"
                stats["entailment_verified"] += 1
            else:
                ref["relation_check"] = "entailment_ambiguous"
                stats["entailment_ambiguous"] += 1

        # Re-derive the claim's support status under the enforced relations.
        if claim_type in MAJOR_CLAIM_TYPES:
            rels = [r.get("relation") for r in (claim.get("supported_by") or [])]
            has_support = any(r in SUPPORTING_RELATIONS for r in rels)
            has_contra = any(r == "CONTRADICTS" for r in rels)
            prev = claim.get("support_status")
            if has_support and not has_contra:
                claim["support_status"] = CLAIM_SUPPORTED
            elif has_support and has_contra:
                claim["support_status"] = CLAIM_PARTIALLY_SUPPORTED
            else:
                # BACKGROUND-only or contradicted-only → no support
                # (AR: BACKGROUND/CONTRADICTS 不能作为支持).
                claim["support_status"] = CLAIM_UNSUPPORTED
                if prev != CLAIM_UNSUPPORTED:
                    stats["unsupported_after"] += 1

    claims_mapping["relation_checks"] = stats
    return claims_mapping


def has_supporting_relation(claim: dict) -> bool:
    """True iff the claim carries at least one enforced support relation."""
    return any(r.get("relation") in SUPPORTING_RELATIONS
               for r in claim.get("supported_by", []) or [])


# ══════════════════════════════════════════════════════════════════════════
# Phase 02 — RT-023: claim coverage gate
# ══════════════════════════════════════════════════════════════════════════
# Every claim-bearing sentence in the final answer must be mapped to a claim
# in the claim mapping. Unmapped claim-bearing content blocks SUPPORTED (the
# state machine consumes this gate via record_claim_coverage).
#
# AR-57: hedged/modal ("可能", "预计", ...) and attribution ("据…称") sentences
# are STILL claim-bearing — they must be mapped, never waved through.

COVERAGE_GATE_VERSION = "1.0.0"

_HEDGED_MARKERS = (
    "可能", "或许", "预计", "有望", "据称", "似乎", "大概", "推测", "或将", "或会",
    "倾向于", "潜在", "should", "may", "might", "could", "would likely",
)
_ATTRIBUTION_MARKERS = (
    "据报道", "据消息", "据称", "表示", "宣布", "披露", "声称", "公布", "指出",
    "according to", "reported", "says", "said", "claims",
)
_NON_CLAIM_PREFIXES = (
    "以下是", "下面是", "下表是", "总的来说", "总而言之", "综上所述", "简而言之",
    "总结一下", "希望", "注：", "注意：", "参考", "参见", "来源",
)


def _split_answer_sentences(answer: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[。！？!?\n])\s*", answer or "")
            if s.strip()]


def _normalize_for_match(text: str) -> str:
    """Strip citation anchors/punctuation/whitespace for coverage matching."""
    t = re.sub(r"\[\d+\]", "", text or "")
    t = re.sub(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]{}#*>|`~]+", "", t)
    return t


def _bigram_overlap(a: str, b: str) -> float:
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def _classify_sentence(sentence: str) -> tuple:
    """(is_claim_bearing: bool, reason: str)"""
    s = sentence.strip()
    if len(s) < 6:
        return False, "trivial"
    if re.match(r"^[#>*\-|=:~`\s]+$", s):
        return False, "markdown_furniture"
    if re.match(r"^\|", s) and set(s) <= set("|-: 0123456789.%/"):
        return False, "table_rule"
    if re.search(r"(希望|祝).{0,8}(有帮助|愉快|顺利)", s):
        return False, "greeting"
    if any(s.startswith(p) for p in _NON_CLAIM_PREFIXES) and not re.search(r"\d", s):
        return False, "meta"
    # Claim-bearing detectors (order matters: cite the strongest reason).
    if re.search(r"\d", s):
        return True, "numeric"
    if any(m in s for m in _HEDGED_MARKERS):
        return True, "hedged"
    if any(m in s for m in _ATTRIBUTION_MARKERS):
        return True, "attribution"
    if re.search(r"[A-Za-z]{3,}", s) or len(re.findall(r"[一-鿿]", s)) >= 8:
        return True, "substantive"
    return False, "fragment"


def check_claim_coverage(answer: str, claims_mapping: dict) -> dict:
    """RT-023 claim coverage gate.

    Returns:
        {
          "version": COVERAGE_GATE_VERSION,
          "gate": "PASS" | "FAIL",
          "coverage": float,            # covered / claim-bearing sentences
          "claim_bearing_sentences": int,
          "covered_sentences": int,
          "uncovered_sentences": [{"sentence": ..., "reason": ...}],
        }
    """
    claims = claims_mapping.get("claims", [])
    claim_norms = [_normalize_for_match(c.get("text", "")) for c in claims]
    claim_norms = [c for c in claim_norms if len(c) >= 4]

    total = covered = 0
    uncovered = []
    for sentence in _split_answer_sentences(answer):
        bearing, reason = _classify_sentence(sentence)
        if not bearing:
            continue
        total += 1
        sn = _normalize_for_match(sentence)
        matched = False
        for cn in claim_norms:
            if cn in sn or sn in cn:
                matched = True
                break
            if len(cn) >= 8 and len(sn) >= 8 and _bigram_overlap(cn, sn) >= 0.6:
                matched = True
                break
        if matched:
            covered += 1
        else:
            uncovered.append({"sentence": sentence[:120], "reason": reason})

    return {
        "version": COVERAGE_GATE_VERSION,
        "gate": "PASS" if total == covered else "FAIL",
        "coverage": round(covered / total, 4) if total else 1.0,
        "claim_bearing_sentences": total,
        "covered_sentences": covered,
        "uncovered_sentences": uncovered,
    }
