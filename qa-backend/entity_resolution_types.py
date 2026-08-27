"""Canonical, versioned Entity Resolution V2 value types (RT-060..075).

Identity is retrieval metadata, never EvidenceRef authority.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit

SCHEMA_VERSION = "3.0.0"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class EntityLifecycle(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    TOMBSTONED = "TOMBSTONED"


class ResolutionState(str, Enum):
    LINK = "LINK"
    NEW = "NEW"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class RuleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    STALE_REVIEW_REQUIRED = "STALE_REVIEW_REQUIRED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


def normalize_surface(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = " ".join(value.casefold().strip().split())
    return value


def sanitize_business_text(value: str, *, limit: int = 512) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))[:limit]
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
    return html.escape(value, quote=True)


def _encode_crockford(number: int, width: int) -> str:
    chars = []
    for _ in range(width):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return "".join(reversed(chars))


def new_opaque_id(prefix: str = "ent") -> str:
    """Return a sortable ULID-class opaque identifier.

    The 48-bit timestamp supplies ordering and 80 random bits supply identity;
    no mutable entity attribute participates in the ID.
    """
    ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    random_bits = int.from_bytes(secrets.token_bytes(10), "big")
    return f"{prefix}_{_encode_crockford((ms << 80) | random_bits, 26)}"


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Mention:
    mention_id: str
    surface: str
    normalized_surface: str
    entity_type: str = "OTHER_DOMAIN"
    context: str = ""
    record_id: str = ""
    source_snapshot_id: str = ""
    start_offset: int = 0
    end_offset: int = 0
    resolver_version: str = "er-v2.0"
    identity_snapshot_id: str = ""


@dataclass(frozen=True)
class AliasRecord:
    alias_id: str
    entity_id: str
    surface: str
    normalized_surface: str
    alias_type: str = "ALIAS"
    language: str = "und"
    script: str = ""
    provenance: str = "manual"
    evidence_ref: Optional[dict] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    status: str = "ACTIVE"
    created_at: str = ""
    created_by: str = "system"
    reason: str = ""


@dataclass(frozen=True)
class StrongIdentifier:
    strong_id_id: str
    entity_id: str
    id_type: str
    value: str
    normalized_value: str
    provenance: str
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    status: str = "ACTIVE"


def normalize_strong_id(id_type: str, value: str) -> str:
    kind = str(id_type or "").upper().strip()
    raw = str(value or "").strip()
    if kind == "DOI":
        norm = raw.lower().removeprefix("https://doi.org/").removeprefix("doi:")
        if not re.fullmatch(r"10\.\d{4,9}/\S+", norm):
            raise ValueError("invalid DOI")
        return norm
    if kind == "LEI":
        norm = re.sub(r"\s+", "", raw).upper()
        if not re.fullmatch(r"[A-Z0-9]{20}", norm):
            raise ValueError("invalid LEI")
        return norm
    if kind in {"OFFICIAL_DOMAIN", "OFFICIAL_URL"}:
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError("invalid official domain/url")
        return host if kind == "OFFICIAL_DOMAIN" else host + (parsed.path.rstrip("/") or "")
    if kind == "EXCHANGE_TICKER":
        norm = re.sub(r"\s+", "", raw).upper()
        if not re.fullmatch(r"[A-Z0-9._-]{2,12}:[A-Z0-9._-]{1,20}", norm):
            raise ValueError("ticker must include exchange:ticker")
        return norm
    if kind in {"PRODUCT_ID", "MODEL_ID", "ONTOLOGY_ID"}:
        norm = raw.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", norm):
            raise ValueError("invalid authoritative identifier")
        return norm
    raise ValueError(f"unapproved strong identifier type: {kind}")


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    stage: str
    rank: int
    score: float
    feature_scores: dict = field(default_factory=dict)
    exact_aliases: tuple[str, ...] = ()
    strong_id_matches: tuple[str, ...] = ()
    type_compatible: bool = True
    alias_provenance: tuple[str, ...] = ()
    language_evidence: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateSet:
    mention: str
    normalized_mention: str
    candidates: tuple[Candidate, ...]
    top_k: int = 10
    generator_version: str = "candidate-cascade-v1"


@dataclass(frozen=True)
class ResolutionDecision:
    decision: ResolutionState
    mention: str
    normalized_mention: str
    selected_entity_id: Optional[str] = None
    candidates: tuple[Candidate, ...] = ()
    reason_codes: tuple[str, ...] = ()
    strong_id_findings: tuple[str, ...] = ()
    type_findings: tuple[str, ...] = ()
    override_block_findings: tuple[str, ...] = ()
    provisional_proposal: Optional[dict] = None
    resolver_version: str = "er-v2.0"
    identity_snapshot_id: str = ""
    diagnostic_flags: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        value = asdict(self)
        value["decision"] = self.decision.value
        return value


@dataclass(frozen=True)
class ResolutionPolicy:
    version: str = "resolution-policy-1.0"
    top_k: int = 10
    fuzzy_min: dict = field(default_factory=lambda: {
        "ORG": 0.86, "PERSON": 0.90, "PRODUCT_MODEL": 0.90,
        "TECHNOLOGY": 0.86, "OTHER_DOMAIN": 0.88,
    })
    link_min: dict = field(default_factory=lambda: {
        "ORG": 0.94, "PERSON": 0.96, "PRODUCT_MODEL": 0.96,
        "TECHNOLOGY": 0.94, "OTHER_DOMAIN": 0.95,
    })
    min_margin: dict = field(default_factory=lambda: {
        "ORG": 0.10, "PERSON": 0.14, "PRODUCT_MODEL": 0.14,
        "TECHNOLOGY": 0.10, "OTHER_DOMAIN": 0.12,
    })
