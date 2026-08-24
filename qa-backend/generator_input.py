"""
RT-039 — Generation input allowlist enforcement (T037, final_spec §14).

The Generator may receive ONLY:

  * current user query / scope
  * verified structured conversation premises (typed VerifiedPremise)
  * canonical Evidence Package (typed EvidencePackage — RT-037)
  * approved system/style instructions

It MUST NOT receive raw Trace/debug text, unselected retrieval text,
prior unverified assistant prose as fact, other workers' conclusions, or
hidden verifier reasoning. Enforcement is structural:

  * build_generator_input(...) rejects anything that is not exactly the
    allowlisted typed inputs (TypeError) — a raw dict / list / str
    payload cannot pass the gate
  * the EvidencePackage is accepted ONLY as the typed object; a look-
    alike dict is rejected (isinstance gate, not duck typing)
  * rendering reads ONLY allowlisted fields; unselected candidates /
    prior prose have no path into the rendered prompt
  * every evidence payload is wrapped in content_safety data boundaries
    (untrusted source text is DATA, never instructions)

Integration tests inject unique sentinel strings into unselected
candidates and prior UNVERIFIED answers and assert they never appear in
the rendered model input (see tests_remediation_phase03).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

APPROVED_SYSTEM_INSTRUCTIONS = (
    "You are a rigorous research assistant. Answer ONLY from the evidence "
    "package provided inside DATA boundaries. Cite evidence IDs for every "
    "factual claim. If the evidence package marks a requirement MISSING or "
    "GAP, or a conflict unresolved, say so explicitly instead of guessing."
)


@dataclass
class VerifiedPremise:
    """A claim-level verified premise from the conversation store (RT-040
    boundary). Prior assistant prose is NEVER accepted as a premise unless
    it passed claim verification — only these typed objects cross the
    generation boundary."""
    premise_id: str
    claim: str
    evidence_ids: List[str] = field(default_factory=list)
    verified: bool = True

    def to_dict(self) -> dict:
        return {"premise_id": self.premise_id, "claim": self.claim,
                "evidence_ids": list(self.evidence_ids),
                "verified": self.verified}


@dataclass
class GeneratorInput:
    """The complete, closed set of Generator inputs (final_spec §14)."""
    query: str
    evidence_package: object            # EvidencePackage (typed, RT-037)
    verified_premises: List[VerifiedPremise] = field(default_factory=list)
    scope: str = ""
    system_instructions: str = APPROVED_SYSTEM_INSTRUCTIONS
    style_instructions: str = ""

    def __post_init__(self):
        from evidence_package import EvidencePackage, PackedGenerationView
        if not isinstance(self.evidence_package,
                          (EvidencePackage, PackedGenerationView)):
            raise TypeError(
                "GeneratorInput.evidence_package must be an EvidencePackage "
                "or the capacity-packed PackedGenerationView (RT-037/RT-038 "
                "typed boundary) — raw search results, dicts, or prompt "
                "strings are forbidden generation context")
        for p in self.verified_premises:
            if not isinstance(p, VerifiedPremise):
                raise TypeError("verified_premises must be VerifiedPremise "
                                "instances (typed allowlist)")
            if not p.verified:
                raise ValueError("unverified premise rejected at the "
                                 "generation boundary")


def build_generator_input(*, query: str,
                          evidence_package,
                          verified_premises: Optional[List[VerifiedPremise]] = None,
                          scope: str = "",
                          system_instructions: str = APPROVED_SYSTEM_INSTRUCTIONS,
                          style_instructions: str = "") -> GeneratorInput:
    """Allowlisted constructor. Any non-allowlisted payload raises."""
    return GeneratorInput(
        query=query,
        evidence_package=evidence_package,
        verified_premises=list(verified_premises or []),
        scope=scope,
        system_instructions=system_instructions,
        style_instructions=style_instructions,
    )


def render_generator_prompt(gen_input: GeneratorInput) -> str:
    """Deterministically render the allowlisted model input.

    Reads ONLY GeneratorInput fields; wraps every evidence payload in
    data boundaries; never includes compressed text as evidence
    (navigation cards render as pointers, marked not-evidence).
    """
    from content_safety import wrap_retrieved_content
    if not isinstance(gen_input, GeneratorInput):
        raise TypeError("render_generator_prompt accepts GeneratorInput only")

    pkg = gen_input.evidence_package
    sections: List[str] = []

    sections.append("【用户问题】")
    sections.append(gen_input.query)
    if gen_input.scope:
        sections.append("")
        sections.append("【研究范围】")
        sections.append(gen_input.scope)

    if gen_input.verified_premises:
        sections.append("")
        sections.append("【已验证前提】(conversation-verified claims only)")
        for p in gen_input.verified_premises:
            sections.append(
                f" • [{p.premise_id}] {p.claim} "
                f"(evidence: {', '.join(p.evidence_ids) or 'none'})")

    sections.append("")
    sections.append("【证据包】")
    if hasattr(pkg, "view_hash") and getattr(pkg, "view_hash", ""):
        # the EXACT final packed object (review blocker 8): the view hash
        # binds precisely what the Generator renders
        sections.append(f"package_hash={pkg.package_hash} "
                        f"view_hash={pkg.view_hash} "
                        f"schema={pkg.schema_version}")
    else:
        sections.append(f"package_hash={pkg.package_hash} "
                        f"schema={pkg.schema_version}")
    if pkg.gaps:
        sections.append("⚠️ 证据缺口: " + "; ".join(pkg.gaps))

    for req in pkg.requirements:
        sections.append("")
        marker = "" if req.coverage == "COVERED" else \
            f" [{req.coverage}]" + (" (critical)" if req.critical else "")
        sections.append(f"--- 需求 {req.requirement_id}: "
                        f"{req.description}{marker} ---")
        if not req.support_evidence_ids:
            sections.append("  ⚠️ 缺失证据。必须明确告知用户此部分信息不足。")
            continue
        for eid in req.support_evidence_ids:
            e = pkg.evidence.get(eid)
            if e is None:
                continue
            header = (f"  [{eid}] record={e.record_id} "
                      f"role={e.source_role} "
                      f"group={e.independent_group_id} "
                      f"time={e.event_time or 'unknown'} "
                      f"temporal={e.temporal_status} "
                      f"relation={e.relation}")
            sections.append(header)
            if e.compressed:
                sections.append(
                    wrap_retrieved_content(
                        e.exact_text + " [导航卡片 — 非证据原文, 不得引用为证据]"))
            else:
                sections.append(wrap_retrieved_content(e.exact_text))

    # unresolved conflicts stay visible (final_spec §22)
    unresolved = [c for c in pkg.conflicts if not c.resolved]
    if unresolved:
        sections.append("")
        sections.append("【未解决冲突】(必须在回答中保持可见)")
        for c in unresolved:
            sections.append(
                f" • {c.conflict_id} [{c.severity}] {c.subject}: "
                f"states={c.states} evidence={sorted(c.evidence_ids)}")

    if pkg.conditions:
        sections.append("")
        sections.append("【数值/时间/范围条件】")
        for c in pkg.conditions:
            sections.append(f" • {c.condition_id} [{c.kind}] {c.detail} "
                            f"status={c.status} evidence={sorted(c.evidence_ids)}")

    if pkg.degraded_capabilities:
        sections.append("")
        sections.append("【降级状态】")
        for d in pkg.degraded_capabilities:
            sections.append(f" • {d}")

    sections.append("")
    sections.append("【系统指令】")
    sections.append(gen_input.system_instructions)
    if gen_input.style_instructions:
        sections.append("")
        sections.append("【风格指令】")
        sections.append(gen_input.style_instructions)

    return "\n".join(sections)
