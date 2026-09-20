"""RT-116 PREP — Final core acceptance evaluator (gate list, fail-closed).

PREPARED / BLOCKED_ONLY_BY_RT101 (+RT-110..115 pass artifacts).
Enumerates the 25 final-spec completion gates (docs/remediation/final_spec.md
completion checklist) and evaluates them STRICTLY from provided artifacts:
a missing artifact/flag is an automatic FAIL — never skipped, never assumed.

This evaluator CANNOT pass today (RT-110..115 have no pass artifacts), and
that is the honest state: it exists so Phase10 execution only needs to feed
artifacts, not invent logic.
"""
from __future__ import annotations

from dataclasses import dataclass

# 25 completion gates: id, what artifact proves it, blocked_by_today
@dataclass
class Gate:
    gate_id: str
    description: str
    requires: str                   # artifact key expected in the evidence dict

GATES_25 = [
    ("G01", "remediation spec frozen", "rt001.spec_manifest"),
    ("G02", "no fake final acceptance assertions", "rt002.assertion_audit"),
    ("G03", "mini-runtime fixture reproducible", "rt003.fixture_hash"),
    ("G04", "production baseline + gap report", "rt004.baseline_report"),
    ("G05", "main protected / required checks", "rt005.protection_receipt"),
    ("G06", "registry stable record ids", "rt010.registry_evidence"),
    ("G07", "legacy idx migration map", "rt011.idmap_evidence"),
    ("G08", "immutable source snapshots", "rt012.snapshot_store"),
    ("G09", "reversible normalization + locators", "rt013.normalization"),
    ("G10", "evidence metadata enrichment", "rt014.enrichment"),
    ("G11", "synthetic isolation in indexes", "rt015.isolation"),
    ("G12", "global release manifest", "rt016.manifest"),
    ("G13", "atomic activation + pinning", "rt017.activation"),
    ("G14", "grounding rewrite on snapshots", "rt020.grounding"),
    ("G15", "claims/relations + entailment", "rt021.entailment"),
    ("G16", "canonical state machine", "rt024.state_machine"),
    ("G17", "fail-safe final verifier", "rt025.verifier"),
    ("G18", "terminal SSE contract", "rt027.terminal"),
    ("G19", "retrieval extraction complete", "rt030.extraction"),
    ("G20", "evidence package canonical", "rt037.package"),
    ("G21", "orchestrator + workers wired", "rt043.rt045.orchestrator"),
    ("G22", "release benchmark suites", "rt100.rt101.rt102.rt103.benchmarks"),
    ("G23", "E2E + failure-injection suites", "rt104.rt105.e2e"),
    ("G24", "CI tiering + provenance", "rt106.ci"),
    ("G25", "phase10 shadow/canary/rollback/SLO/docs", "rt110_115.phase10_bundle"),
]


def evaluate(evidence: dict) -> dict:
    """Fail-closed: every gate requires its artifact key with truthy value."""
    results, passed = [], 0
    for gid, desc, key in GATES_25:
        ok = bool(evidence.get(key))
        results.append({"gate": gid, "description": desc,
                        "requires_artifact": key, "pass": ok})
        passed += ok
    return {"passed": passed, "total": len(GATES_25),
            "all_pass": passed == len(GATES_25),
            "declaration": "COMPLETE" if passed == len(GATES_25) else "INCOMPLETE",
            "gates": results}
