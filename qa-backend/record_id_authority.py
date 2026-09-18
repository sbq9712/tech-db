"""RT101-V13 post-mortem (Repair B): authoritative record-id contract.

The V13 formal run surfaced a `legacy-idx:N` pseudo-id inside the
AUTHORIZED citation chain (grounding VALID + display_authorized=true) —
a structural invariant violation: positional/list indices are INTERNAL
lookup keys (§13) and must never surface as outward citation authority.
Any citation whose record identity is not a stable, universe-resolvable
`record_id` must be withheld from display authorization, and any claim
whose ONLY support is such a row cannot remain fully SUPPORTED.

This module is deliberately tiny and dependency-free so every surface
(citation build, grounding, claim mapping, display authorization,
reference cards, terminal serialization) can import it without cycles.
"""
from typing import Iterable, Optional

PSEUDO_PREFIXES = ("legacy-idx:", "legacy_idx:", "synthetic:", "_pos:")


def is_pseudo_record_id(record_id) -> bool:
    """True when the value is a positional/synthetic masquerade, not a
    stable record id (the V13 formal run surfaced a positional
    `legacy-idx:<n>` masquerade in the authorized chain). Numeric-only ids
    are also pseudo (raw list positions)."""
    if record_id is None:
        return True
    rid = str(record_id).strip()
    if not rid:
        return True
    low = rid.lower()
    if low.startswith(PSEUDO_PREFIXES):
        return True
    if rid.isdigit():
        return True
    return False


def citation_record_authority_error(citation: dict,
                                    universe: Optional[Iterable[str]] = None,
                                    ) -> str:
    """Return a fail-closed reason string when the citation's record
    identity may not serve as citation authority; "" when acceptable.

    Checks (in order):
      1. record_id present and not positional/synthetic (§11/§13);
      2. when a universe of canonical record ids is supplied, the id must
         resolve inside the current SourceSnapshot universe (§12).
    """
    rid = (citation or {}).get("record_id")
    if is_pseudo_record_id(rid):
        return f"pseudo_record_id:{rid!r:.48}"
    if universe is not None:
        try:
            if str(rid) not in set(universe):
                return f"record_unresolvable:{rid!r:.48}"
        except TypeError:
            return "universe_unreadable"
    return ""
