"""Single policy boundary for text allowed in primary factual indexes."""


def source_evidence_text(record: dict) -> str:
    """Return source-grounded text only; generated ``as`` is never a fallback."""
    return str(record.get("evidence_text") or record.get("fb") or record.get("b") or "")


def primary_bm25_text(record: dict) -> str:
    parts = [record.get("t", ""), source_evidence_text(record)]
    kp = record.get("kp", []) or []
    if isinstance(kp, list): parts.append(" ".join(str(value) for value in kp))
    parts.extend([record.get("tg", ""), record.get("tp", "")])
    return " ".join(str(value) for value in parts if value)
