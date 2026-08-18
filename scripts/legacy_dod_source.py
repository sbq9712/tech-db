#!/usr/bin/env python3
"""Parse the frozen legacy T/ER Definition-of-Done source.

This module is deliberately shared by the matrix generator and the linter.
It does not infer DoDs from ticket titles:

* T-ticket DoDs are explicit ``- [ ]`` items under either
  ``【Definition of Done】`` or ``【怎么算完成】``.  The adversarial execution
  section and the related frozen master section are unioned in source order;
  byte-identical repeated bullets are counted once.
* ER tickets use the compact frozen form ``完成标准：a；b；c``.  Each
  semicolon-delimited criterion is an independent DoD and retains its source
  line and clause number.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "remediation" / "legacy_normative_input.txt"
SOURCE_RELATIVE = "docs/remediation/legacy_normative_input.txt"
SOURCE_SHA256 = "341560040ea69e412270b347ff33ffc88c0d0be17de21fbbc6e78b88cf3cca3d"
MASTER_START_MARKER = "MASTER EXECUTION TICKET SPECIFICATION"
DONE_LABELS = {"【Definition of Done】", "【怎么算完成】"}


def source_sha256(path: Path = SOURCE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _section(line_number: int, master_start: int) -> str:
    return "frozen_master" if line_number >= master_start else "adversarial_execution"


def parse_frozen_dods(path: Path = SOURCE) -> dict[str, dict]:
    if source_sha256(path) != SOURCE_SHA256:
        raise ValueError(f"frozen legacy source hash mismatch: {path}")
    lines = path.read_text("utf-8").splitlines()
    master_start = next(
        i + 1 for i, line in enumerate(lines) if line.strip() == MASTER_START_MARKER
    )

    tickets: dict[str, dict] = {}
    t_heads: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(T\d{3})\s+-\s+(.+)$", line.strip())
        if match:
            t_heads.append((index, match.group(1), match.group(2).strip()))

    t_dods: dict[str, list[dict]] = defaultdict(list)
    t_seen: dict[str, set[str]] = defaultdict(set)
    for occurrence, (start, ticket_id, title) in enumerate(t_heads):
        end = t_heads[occurrence + 1][0] if occurrence + 1 < len(t_heads) else len(lines)
        section_lines = lines[start:end]
        label_offsets = [
            offset for offset, line in enumerate(section_lines)
            if line.strip() in DONE_LABELS
        ]
        if not label_offsets:
            raise ValueError(f"{ticket_id} at line {start + 1} has no frozen DoD label")
        label_offset = label_offsets[0]
        for offset in range(label_offset + 1, len(section_lines)):
            line = section_lines[offset]
            if line.startswith("【测试】") or re.match(r"^-{20,}$", line):
                break
            bullet = re.match(r"^- \[ \] (.+)$", line.strip())
            if not bullet:
                continue
            text = bullet.group(1).strip()
            if text in t_seen[ticket_id]:
                continue
            t_seen[ticket_id].add(text)
            line_number = start + offset + 1
            t_dods[ticket_id].append({
                "text": text,
                "source": {
                    "path": SOURCE_RELATIVE,
                    "sha256": SOURCE_SHA256,
                    "line": line_number,
                    "section": _section(line_number, master_start),
                    "kind": "checklist_bullet",
                },
            })
        tickets.setdefault(ticket_id, {"ticket_id": ticket_id, "title": title})

    er_dods: dict[str, list[dict]] = defaultdict(list)
    for index, line in enumerate(lines):
        match = re.match(r"^(ER-\d{3})\s+(.+)$", line.strip())
        if not match:
            continue
        ticket_id, title = match.group(1), match.group(2).strip()
        completion_index = None
        for probe in range(index + 1, min(index + 8, len(lines))):
            if lines[probe].startswith("完成标准："):
                completion_index = probe
                break
        if completion_index is None:
            continue  # references such as "ER-001 ... ER-124", not a ticket block
        raw = lines[completion_index].split("：", 1)[1].strip()
        clauses = [clause.strip() for clause in raw.split("；") if clause.strip()]
        if not clauses:
            raise ValueError(f"{ticket_id} at line {index + 1} has no completion criteria")
        for clause_number, text in enumerate(clauses, 1):
            er_dods[ticket_id].append({
                "text": text,
                "source": {
                    "path": SOURCE_RELATIVE,
                    "sha256": SOURCE_SHA256,
                    "line": completion_index + 1,
                    "section": "adversarial_execution",
                    "kind": "completion_standard_clause",
                    "clause": clause_number,
                    "raw": raw,
                },
            })
        tickets[ticket_id] = {"ticket_id": ticket_id, "title": title}

    for ticket_id, dods in {**t_dods, **er_dods}.items():
        tickets[ticket_id]["dods"] = dods
    missing = sorted(ticket_id for ticket_id, ticket in tickets.items() if not ticket.get("dods"))
    if missing:
        raise ValueError(f"frozen tickets missing DoDs: {missing}")
    return tickets


def source_counts(tickets: dict[str, dict] | None = None) -> dict[str, int]:
    tickets = tickets or parse_frozen_dods()
    t_tickets = sum(ticket_id.startswith("T") for ticket_id in tickets)
    er_tickets = sum(ticket_id.startswith("ER-") for ticket_id in tickets)
    t_dods = sum(len(ticket["dods"]) for ticket_id, ticket in tickets.items()
                 if ticket_id.startswith("T"))
    er_dods = sum(len(ticket["dods"]) for ticket_id, ticket in tickets.items()
                  if ticket_id.startswith("ER-"))
    return {
        "t_ticket_count": t_tickets,
        "er_ticket_count": er_tickets,
        "legacy_ticket_count": t_tickets + er_tickets,
        "t_dod_count": t_dods,
        "er_dod_count": er_dods,
        "legacy_dod_count": t_dods + er_dods,
    }


if __name__ == "__main__":
    parsed = parse_frozen_dods()
    counts = source_counts(parsed)
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
