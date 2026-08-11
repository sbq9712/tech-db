"""
T046 — Claim-Evidence Entailment Checking
==========================================
Verifies that evidence text actually entails (supports) a claim.

Two-layer approach:
  Layer 1: Deterministic checks (exact number match, entity overlap, negation)
  Layer 2: LLM-based NLI assessment (when deterministic is ambiguous)

Entailment labels:
  ENTAILS: evidence directly supports the claim
  REFUTES: evidence contradicts the claim
  NEUTRAL: evidence is not relevant or insufficient

Hard rules (deterministic):
  - If claim has specific number and evidence has different number → REFUTES
  - If claim entity not in evidence → NEUTRAL
  - If claim and evidence differ on negation → REFUTES
"""
import os
import re
import json
from typing import Optional, Dict, List
from dataclasses import dataclass
from enum import Enum


class EntailmentLabel(str, Enum):
    ENTAILS = "ENTAILS"
    REFUTES = "REFUTES"
    NEUTRAL = "NEUTRAL"


@dataclass
class EntailmentResult:
    label: EntailmentLabel
    confidence: float
    method: str  # "deterministic" | "llm" | "fallback"
    reason: str
    key_facts: dict


# ─── Deterministic Layer ───

NUMERIC_PATTERN = re.compile(
    r'(\d+\.?\d*)\s*(TB/s|GB/s|MB/s|Wh/kg|mAh/g|V|nm|nm²|W|mW|kW|MW|GW|'
    r'%|美元|元|亿|万|million|billion|吨|摄氏度|°C|K|Hz|kHz|MHz|GHz|THz|'
    r'次|个|条|年|月|天|小时|分钟|秒|km|m|cm|mm|kg|g|倍|分|dB|GPa|MPa)'
    r'|\d+\.?\d*\s*(?:TB/s|GB/s|Wh/kg|nm|%|亿|万|million|billion)',
    re.IGNORECASE
)


def _extract_numbers(text: str) -> List[Dict]:
    """Extract numeric values with their units and context."""
    results = []
    for m in NUMERIC_PATTERN.finditer(text):
        val_str = m.group(1) if m.group(1) else m.group(0).split()[0]
        try:
            val = float(val_str)
        except (ValueError, IndexError):
            continue
        unit = ""
        full_match = m.group(0)
        # Try to extract unit from match
        unit_match = re.search(r'[a-zA-Z/%°]+$', full_match)
        if unit_match:
            unit = unit_match.group(0)
        
        # Also check for Chinese units
        cn_match = re.search(r'[年月天次个条亿万吨倍分秒时]*$', full_match)
        
        results.append({
            "value": val,
            "unit": unit.lower(),
            "raw": full_match.strip(),
            "context": text[max(0, m.start()-20):m.end()+20],
        })
    return results


def _check_negation(text: str) -> bool:
    """Check if text has negation markers."""
    negation_markers = ["不", "没有", "无", "非", "未", "别", "莫", "勿",
                       "not", "no", "never", "none", "n't", "without", "cannot"]
    text_lower = text.lower()
    return any(m in text_lower for m in negation_markers)


def _extract_entities_simple(text: str) -> set:
    """Extract entity-like tokens (capitalized words and Chinese terms)."""
    entities = set()
    # Capitalized English words
    for m in re.finditer(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', text):
        entities.add(m.group(0).lower())
    # Chinese entity-like tokens (2+ chars)
    for m in re.finditer(r'[一-鿿]{2,}', text):
        entities.add(m.group(0))
    return entities


def _deterministic_entailment(claim: str, evidence: str) -> Optional[EntailmentResult]:
    """Run deterministic entailment checks.
    
    Returns None if no hard rule fires (ambiguous case → needs LLM).
    """
    claim_numbers = _extract_numbers(claim)
    evidence_numbers = _extract_numbers(evidence)
    
    # Rule 1: Numeric mismatch
    # If claim states a specific number and evidence has a different number for same unit
    if claim_numbers:
        for cn in claim_numbers:
            matching_ev = [en for en in evidence_numbers 
                          if en["unit"] == cn["unit"] and cn["unit"]]
            if matching_ev:
                # Check if any evidence number matches claim number
                matched = any(abs(en["value"] - cn["value"]) < 0.01 * max(abs(cn["value"]), 1)
                            for en in matching_ev)
                if not matched:
                    # All evidence numbers for this unit differ from claim
                    # Check if it's truly a contradiction vs different metric
                    if len(matching_ev) == 1:
                        return EntailmentResult(
                            label=EntailmentLabel.REFUTES,
                            confidence=0.85,
                            method="deterministic",
                            reason=f"Number mismatch: claim says {cn['raw']}, evidence says {matching_ev[0]['raw']}",
                            key_facts={"claim_number": cn["raw"], "evidence_number": matching_ev[0]["raw"]},
                        )
    
    # Rule 2: Negation flip
    claim_neg = _check_negation(claim)
    evidence_neg = _check_negation(evidence)
    if claim_neg != evidence_neg:
        # Check if the core assertion is the same (ignoring negation)
        claim_core = re.sub(r'不|没有|无|非|未|not\s|no\s|n\'t', '', claim).strip()
        evidence_core = re.sub(r'不|没有|无|非|未|not\s|no\s|n\'t', '', evidence).strip()
        # If cores are similar, this is a refutation
        if claim_core and evidence_core:
            claim_entities = _extract_entities_simple(claim_core)
            evidence_entities = _extract_entities_simple(evidence_core)
            overlap = claim_entities & evidence_entities
            if len(overlap) >= min(len(claim_entities), 1):
                return EntailmentResult(
                    label=EntailmentLabel.REFUTES,
                    confidence=0.80,
                    method="deterministic",
                    reason="Negation mismatch: claim and evidence differ on negation",
                    key_facts={"claim_negated": claim_neg, "evidence_negated": evidence_neg},
                )
    
    # Rule 3: Entity absence → neutral (not refutes, just insufficient)
    claim_entities = _extract_entities_simple(claim)
    if claim_entities:
        evidence_entities = _extract_entities_simple(evidence)
        overlap = claim_entities & evidence_entities
        if not overlap and len(claim_entities) >= 1:
            return EntailmentResult(
                label=EntailmentLabel.NEUTRAL,
                confidence=0.70,
                method="deterministic",
                reason="Claim entities not found in evidence",
                key_facts={"claim_entities": list(claim_entities)[:5],
                          "evidence_entities": list(evidence_entities)[:5]},
            )

    # Rule 4: Numeric match with entity overlap → soft ENTAILS
    # If all claim numbers match evidence numbers and entities overlap, it's likely supported
    if claim_numbers and claim_entities:
        evidence_entities_all = _extract_entities_simple(evidence)
        entity_overlap = claim_entities & evidence_entities_all
        if entity_overlap:
            all_nums_matched = True
            for cn in claim_numbers:
                matching_ev = [en for en in evidence_numbers
                              if en["unit"] == cn["unit"] and cn["unit"]]
                if matching_ev:
                    matched = any(abs(en["value"] - cn["value"]) < 0.01 * max(abs(cn["value"]), 1)
                                for en in matching_ev)
                    if not matched:
                        all_nums_matched = False
                        break
            if all_nums_matched:
                return EntailmentResult(
                    label=EntailmentLabel.ENTAILS,
                    confidence=0.75,
                    method="deterministic",
                    reason="All numeric facts match and entities overlap",
                    key_facts={"matched_numbers": len(claim_numbers)},
                )

    # No hard rule fired
    return None


# ─── LLM Layer ───

def _llm_entailment(claim: str, evidence: str, api_key: str = None) -> EntailmentResult:
    """Use LLM for NLI assessment."""
    api_key = api_key or os.environ.get("ZAI_API_KEY", "")
    base_url = os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
    
    if not api_key:
        return EntailmentResult(
            label=EntailmentLabel.NEUTRAL,
            confidence=0.3,
            method="fallback",
            reason="No API key available for LLM entailment check",
            key_facts={},
        )
    
    prompt = f"""判断以下证据文本是否支持给定声明。

声明：{claim}

证据：{evidence}

请只返回JSON，格式：
{{"label": "ENTAILS" | "REFUTES" | "NEUTRAL", "confidence": 0.0-1.0, "reason": "简要说明"}}

规则：
- ENTAILS: 证据直接支持声明中的事实
- REFUTES: 证据与声明矛盾
- NEUTRAL: 证据不相关或不足以判断
"""
    
    try:
        import urllib.request
        data = json.dumps({
            "model": "glm-4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
        }).encode("utf-8")
        
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        
        content = result["choices"][0]["message"]["content"]
        
        # Parse JSON
        # Try direct parse first
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # Strip markdown fences
            cleaned = re.sub(r'```json\s*', '', content)
            cleaned = re.sub(r'```\s*$', '', cleaned.strip())
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                # Regex extract
                m = re.search(r'\{[^}]+\}', content)
                if m:
                    parsed = json.loads(m.group(0))
                else:
                    raise
        
        label_str = parsed.get("label", "NEUTRAL").upper()
        label = EntailmentLabel(label_str) if label_str in ("ENTAILS", "REFUTES", "NEUTRAL") else EntailmentLabel.NEUTRAL
        
        return EntailmentResult(
            label=label,
            confidence=float(parsed.get("confidence", 0.5)),
            method="llm",
            reason=parsed.get("reason", ""),
            key_facts={},
        )
        
    except Exception as e:
        return EntailmentResult(
            label=EntailmentLabel.NEUTRAL,
            confidence=0.3,
            method="fallback",
            reason=f"LLM entailment failed: {e}",
            key_facts={},
        )


# ─── Main Entry Point ───

def check_entailment(claim: str, evidence: str, use_llm: bool = True) -> EntailmentResult:
    """Check if evidence entails the claim.
    
    Two-layer approach:
    1. Deterministic rules (hard rules, always run)
    2. LLM assessment (when deterministic is ambiguous)
    
    Hard rules override LLM: if deterministic returns REFUTES, LLM cannot override to ENTAILS.
    """
    if not claim or not evidence:
        return EntailmentResult(
            label=EntailmentLabel.NEUTRAL,
            confidence=0.5,
            method="deterministic",
            reason="Empty claim or evidence",
            key_facts={},
        )
    
    # Layer 1: Deterministic
    det_result = _deterministic_entailment(claim, evidence)
    
    if det_result and det_result.confidence >= 0.7:
        # Hard rule fired with high confidence — return without LLM
        return det_result
    
    # Layer 2: LLM (if enabled and deterministic was ambiguous)
    if use_llm:
        llm_result = _llm_entailment(claim, evidence)
        
        # Hard rule override: deterministic REFUTES overrides LLM ENTAILS
        if det_result and det_result.label == EntailmentLabel.REFUTES:
            if llm_result.label == EntailmentLabel.ENTAILS:
                return EntailmentResult(
                    label=EntailmentLabel.REFUTES,
                    confidence=max(det_result.confidence, 0.8),
                    method="deterministic_override",
                    reason=f"Hard rule override: {det_result.reason}",
                    key_facts=det_result.key_facts,
                )
        
        return llm_result
    
    # Fallback: return deterministic or neutral
    if det_result:
        return det_result
    
    return EntailmentResult(
        label=EntailmentLabel.NEUTRAL,
        confidence=0.5,
        method="fallback",
        reason="Could not determine entailment",
        key_facts={},
    )


def batch_entailment(claims: List[str], evidence: str, use_llm: bool = True) -> List[EntailmentResult]:
    """Check entailment for multiple claims against one evidence text."""
    return [check_entailment(c, evidence, use_llm=use_llm) for c in claims]
