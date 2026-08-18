"""Optional non-evidence index for generated summaries (RT-015)."""
from __future__ import annotations


def build_hint_documents(records: list[dict]) -> list[dict]:
    return [{"record_id": str(r.get("record_id") or ""), "text": str(r["as"]),
             "evidence_eligibility": "RETRIEVAL_ONLY", "can_support": False,
             "can_cite": False, "route": "synthetic_hint"}
            for r in records if r.get("as")]


def may_support_or_cite(hit: dict) -> bool:
    return bool(hit.get("can_support")) and bool(hit.get("can_cite")) and hit.get("evidence_eligibility") == "CITATION_ELIGIBLE"
