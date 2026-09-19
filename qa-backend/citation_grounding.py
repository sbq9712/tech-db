"""
T003 — Citation Evidence Grounding
===================================
Every citation displayed to the user must be backed by an exact text span
from the original record, not a query-aware snippet or truncated beginning.

Flow:
  Answer Claim → Citation → Record → Full Original Text → Exact Evidence Span

Evidence text priority:
  1. fb / full_body  (full original text — highest priority)
  2. b / body        (truncated body)
  3. as / ai_summary (synthetic fallback ONLY — never shown as original evidence)

Key Rules:
  - The evidence_span must be verifiable in the original text (substring match).
  - If LLM-proposed span fails, retry with sentence/fuzzy locate.
  - If all attempts fail → grounding_fail (citation invalid).
  - NEVER fall back to "first 200 chars" as a valid citation span.
  - Returns start/end offsets for UI highlighting.

RT101-V14 grounding contract (qual P0 repair, 2026-09-19):
  LOCATE / VERIFY SOURCE CORRESPONDENCE / CLAIM SUPPORT are three distinct
  concerns. A fuzzy locator may only ever PRODUCE a candidate region; the
  region becomes grounding_status=VALID solely when the FULL proposed span
  corresponds to the raw canonical slice under the single deterministic
  canonical_match_view() normalization (markdown link wrappers collapse to
  their visible label; bare URLs and whitespace runs are formatting-only
  differences). A short prefix anchor can never by itself justify VALID for
  a longer proposed span — blind prefix extension is permanently removed.
  Every emitted offset is a RAW canonical-source coordinate (normalized
  indexes are mapped back through a reversible offset map and are never
  exposed as raw offsets).
"""
import re
import difflib
from typing import Optional


def get_original_text(record: dict) -> str:
    """Get the best available original text from a record.

    Priority: fb > b > as (synthetic, fallback only)
    Returns (text, source_field) where source_field indicates provenance.
    """
    for field in ("fb", "b"):
        text = record.get(field, "") or ""
        if text.strip():
            return text
    # AI summary is synthetic — not preferred as citation evidence
    return record.get("as", "") or ""


def get_text_source(record: dict) -> str:
    """Return which field was used as the original text source."""
    for field in ("fb", "b"):
        if (record.get(field, "") or "").strip():
            return field
    return "as"


def _find_sentence_boundaries(text: str, pos: int) -> tuple:
    """Find sentence boundaries around a position in text.

    Supports Chinese (。！？) and English (.!?) sentence delimiters.
    Returns (start, end) character offsets.
    """
    # Chinese + English sentence enders
    delimiters = "。！？!?；;\n"

    # Find start: walk backwards from pos to nearest sentence start
    start = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in delimiters:
            start = i + 1
            break

    # Find end: walk forwards from pos to nearest sentence end
    end = len(text)
    for i in range(pos, len(text)):
        if text[i] in delimiters:
            end = i + 1
            break

    return (start, end)


def _extract_sentences(text: str) -> list:
    """Split text into sentences with their start/end offsets."""
    delimiters = "。！？!?；;\n"
    sentences = []
    start = 0
    for i, ch in enumerate(text):
        if ch in delimiters:
            s = text[start:i + 1].strip()
            if s:
                sentences.append((s, start, i + 1))
            start = i + 1
    # Last segment (no delimiter)
    if start < len(text):
        s = text[start:].strip()
        if s:
            sentences.append((s, start, len(text)))
    return sentences


def verify_span_in_text(span: str, text: str) -> tuple:
    """Check if span exists in text. Returns (found, start_offset, end_offset).

    Exact match is required. If not found, returns (False, -1, -1).
    """
    if not span or not text:
        return (False, -1, -1)
    idx = text.find(span)
    if idx >= 0:
        return (True, idx, idx + len(span))
    return (False, -1, -1)


# ── RT101-V14: single canonical match normalization + raw offset map ────────
# (contract: exactly one deterministic view decides source correspondence;
# formatting-only differences — markdown link wrappers around a visible
# label, bare URLs, whitespace runs — never change substantive meaning and
# are the ONLY allowed differences between a proposed span and its raw slice.)
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((?:[^)(]|\([^)]*\))*\)")
_BARE_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


def _link_stripped_with_map(raw: str) -> tuple:
    """Rewrite ``[label](url)`` → ``label`` (empty label → removed) while
    tracking, for every character of the stripped copy, the raw index it
    came from. Returns (stripped_text, pos_map)."""
    out = []
    pos_map = []
    pos = 0
    for m in _MD_LINK_RE.finditer(raw):
        for i in range(pos, m.start()):
            out.append(raw[i])
            pos_map.append(i)
        label = m.group(1) or ""
        label_start = m.start(1) if label else m.start()
        for j, ch in enumerate(label):
            out.append(ch)
            pos_map.append(label_start + j)
        pos = m.end()
    for i in range(pos, len(raw)):
        out.append(raw[i])
        pos_map.append(i)
    return "".join(out), pos_map


def canonical_match_view(text: str) -> str:
    """The ONE allowed normalization for source correspondence (V14).

    CRLF → LF; markdown link wrappers collapse to their visible label;
    bare URLs removed; NFKC + whitespace-run collapse via the snapshot
    normalizer. Never rewrites substantive content.
    """
    if not isinstance(text, str):
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _MD_LINK_RE.sub(lambda m: m.group(1) or "", t)
    t = _BARE_URL_RE.sub(" ", t)
    from source_snapshot import normalize_with_map
    return normalize_with_map(t).text


def _normalized_source_view(raw_text: str):
    """Build the canonical view of a canonical source text together with a
    reversible view-index → raw-index map. Returns
    (view_text, view_to_raw_fn) where view_to_raw_fn(start, end) maps a
    half-open view range to the RAW source range it covers (or None when
    unmappable)."""
    stripped, pos_map = _link_stripped_with_map(raw_text)
    from source_snapshot import normalize_with_map
    view = normalize_with_map(stripped)
    offsets = view.offsets

    def view_to_raw(start: int, end: int):
        if start < 0 or end <= start or end > len(offsets):
            return None
        spans = offsets[start:end]
        left, right = spans[0][0], spans[-1][1]
        # monotonic guard: non-contiguous view range → refuse
        for prev, nxt in zip(spans, spans[1:]):
            if nxt[0] < prev[1]:
                return None
        if left >= len(pos_map) or right - 1 >= len(pos_map):
            return None
        # Defined right-bound semantics (V14): the raw range is
        # INCLUSIVE-EXHAUSTIVE — it spans every raw character covered by
        # the view range, including interior collapsed whitespace (e.g. a
        # view range ending mid-run of spaces/tabs collapsed to one view
        # char extends the raw right bound over the whole run). This
        # is never an authority source: grounding_status=VALID is decided
        # solely by _full_correspondence() on the returned slice, so a
        # whitespace-padded bound can only keep a genuine match VALID
        # (whitespace is formatting-only under the canonical view); it can
        # never manufacture one.
        return (pos_map[left], pos_map[right - 1] + 1)

    return view.text, view_to_raw


def _full_correspondence(proposed: str, raw_text: str,
                         raw_start: int, raw_end: int) -> bool:
    """True iff the raw slice corresponds to the FULL proposed span under
    canonical_match_view (never a prefix/partial correspondence)."""
    if raw_start is None or raw_end is None or raw_end <= raw_start:
        return False
    if raw_start < 0 or raw_end > len(raw_text):
        return False
    return (canonical_match_view(raw_text[raw_start:raw_end])
            == canonical_match_view(proposed))


def fuzzy_locate_span(span: str, text: str, min_ratio: float = 0.75) -> tuple:
    """Attempt to LOCATE a candidate region for span in text (V14).

    A locator NEVER decides grounding validity — the caller must verify the
    full proposed/source correspondence over the returned RAW range before
    any status upgrade. Returns (found, raw_start, raw_end, matched_text)
    where offsets are RAW canonical-source coordinates obtained through the
    reversible normalized offset map (never normalized indexes).

    Strategies (all raw-mapped, none self-validating):
    1. Full canonical-view match (whitespace/NFKC/link view) — mapped back
       to raw coordinates. No offset approximation is ever used.
    2. Sentence-level similarity on the first proposed sentence chooses a
       candidate source sentence, extended only while source sentences
       remain inside the canonical length of the proposed span.
    3. Prefix anchoring (40/30/20 chars of the canonical view): the anchor
       only seeds a bounded candidate window of the proposed span's own
       view length. The historical behavior of returning
       ``anchor_start .. anchor_start + len(span)`` RAW characters after a
       20-char prefix match (blind prefix extension) is removed — a
       short anchor can never manufacture a long authoritative span.
    """
    if not span or not text:
        return (False, -1, -1, "")

    view_text, view_to_raw = _normalized_source_view(text)
    span_view = canonical_match_view(span)
    if not span_view:
        return (False, -1, -1, "")

    # Strategy 1: full canonical-view find, mapped back to raw.
    idx = view_text.find(span_view)
    if idx >= 0:
        rr = view_to_raw(idx, idx + len(span_view))
        if rr is not None:
            s0, e0 = rr
            return (True, s0, e0, text[s0:e0])

    # Strategy 2: sentence-similarity candidate (locator only).
    span_sentences = _extract_sentences(span)
    text_sentences = _extract_sentences(text)
    if span_sentences and text_sentences:
        best_score = 0
        best_match = None
        span_first = span_sentences[0][0]
        for sent_text, s_start, s_end in text_sentences:
            ratio = difflib.SequenceMatcher(None, span_first, sent_text).ratio()
            if ratio > best_score:
                best_score = ratio
                best_match = (s_start, s_end, sent_text)
        if best_match and best_score >= min_ratio:
            start_offset = best_match[0]
            end_offset = best_match[1]
            # Extend forward only while within the proposed span's own
            # canonical length — never past it.
            budget = len(span_view) - (end_offset - start_offset)
            if len(span_sentences) > 1 and budget > 0:
                for _, ss, se in text_sentences:
                    if ss < end_offset:
                        continue
                    if se - start_offset > len(span_view):
                        break
                    end_offset = se
                    break
            return (True, start_offset, end_offset,
                    text[start_offset:end_offset])

    # Strategy 3: prefix ANCHOR + bounded window (locator only).
    for prefix_len in (40, 30, 20):
        if len(span_view) >= prefix_len:
            pidx = view_text.find(span_view[:prefix_len])
            if pidx >= 0:
                # candidate window covers the anchor plus at most the
                # proposed span's canonical length in VIEW coordinates,
                # then is mapped back to raw — no blind raw extension.
                w_end = min(pidx + len(span_view), len(view_text))
                rr = view_to_raw(pidx, w_end)
                if rr is not None:
                    s0, e0 = rr
                    return (True, s0, e0, text[s0:e0])

    return (False, -1, -1, "")


def ground_citation_evidence(
    record: dict,
    proposed_span: str = "",
    claim_text: str = "",
    query: str = "",
) -> dict:
    """Ground a citation by finding the exact evidence span in the original text.

    V14 contract: LOCATE (may be fuzzy) is separate from VERIFY SOURCE
    CORRESPONDENCE (deterministic, full-span). grounding_status=VALID is
    emitted only when the FULL proposed span corresponds to a raw canonical
    slice under canonical_match_view(); locators merely nominate candidate
    regions, which are then independently verified. All emitted offsets are
    RAW canonical-source coordinates.

    Args:
        record: The full record dict from all-records-lite.json
        proposed_span: LLM-suggested evidence text (may be imprecise)
        claim_text: The claim this citation should support (for semantic locating)
        query: The original user query (for keyword fallback)

    Returns:
        {
            "evidence_span": str,       # verbatim raw source slice
            "start_offset": int,        # RAW start in original text
            "end_offset": int,          # RAW end in original text
            "grounding_status": str,    # "VALID" | "FUZZY" | "GROUNDING_FAIL"
            "source_field": str,        # "fb" | "b" | "as"
            "highlight": str,           # Key phrase to highlight in UI
            "match_type": str,          # exact | normalized_exact | verified_candidate
            "match_diagnostics": dict,  # locator provenance (V14, additive)
        }
    """
    original_text = get_original_text(record)
    source_field = get_text_source(record)

    # V14: windowed snippets may carry pure truncation decoration (leading/
    # trailing "..." / "…") added by query-relevant excerpt windows. That
    # decoration is formatting, not content — strip it for MATCHING only
    # (highlight keeps the original proposal).
    proposed_span = re.sub(r"^(?:\.{3,}|\u2026+|\s)+|(?:(?:\.{3,}|\u2026+|\s))+$",
                           "", proposed_span or "")

    def _fail():
        return {
            "evidence_span": "",
            "start_offset": -1,
            "end_offset": -1,
            "grounding_status": "GROUNDING_FAIL",
            "source_field": source_field,
            "highlight": "",
            "match_type": "none",
            "match_diagnostics": {},
        }

    if not original_text.strip():
        return _fail()

    # --- Attempt 1: RAW exact match of the full proposed span ---
    if proposed_span:
        found, start, end = verify_span_in_text(proposed_span, original_text)
        if found and _full_correspondence(proposed_span, original_text,
                                          start, end):
            return {
                "evidence_span": original_text[start:end],
                "start_offset": start,
                "end_offset": end,
                "grounding_status": "VALID",
                "source_field": source_field,
                "highlight": proposed_span[:100],
                "match_type": "exact",
                "match_diagnostics": {},
            }

    # --- Attempt 2: canonical-view exact (links/whitespace/NFKC), raw-mapped ---
    if proposed_span:
        view_text, view_to_raw = _normalized_source_view(original_text)
        span_view = canonical_match_view(proposed_span)
        if span_view:
            vidx = view_text.find(span_view)
            if vidx >= 0:
                rr = view_to_raw(vidx, vidx + len(span_view))
                if rr is not None and _full_correspondence(
                        proposed_span, original_text, rr[0], rr[1]):
                    return {
                        "evidence_span": original_text[rr[0]:rr[1]],
                        "start_offset": rr[0],
                        "end_offset": rr[1],
                        "grounding_status": "VALID",
                        "source_field": source_field,
                        "highlight": proposed_span[:100],
                        "match_type": "normalized_exact",
                        "match_diagnostics": {},
                    }

    # --- Attempt 3: fuzzy LOCATORS (never self-validating) ---
    # Every candidate region is independently verified over the FULL
    # proposed span; a locator hit alone can only ever produce FUZZY.
    candidates = []
    if proposed_span and len(proposed_span) >= 10:
        found, start, end, matched = fuzzy_locate_span(
            proposed_span, original_text)
        if found:
            candidates.append({
                "start": start, "end": end, "matched": matched,
                "match_type": "fuzzy_located",
            })
    search_text = claim_text or query
    if search_text:
        result = _keyword_semantic_locate(search_text, original_text)
        if result:
            kstart, kend, highlight = result
            candidates.append({
                "start": kstart, "end": kend, "matched":
                original_text[kstart:kend],
                "match_type": "keyword_located",
            })

    best = None
    for cand in candidates:
        if cand["end"] <= cand["start"]:
            continue
        ok = _full_correspondence(proposed_span, original_text,
                                  cand["start"], cand["end"])
        if ok:
            best = (cand, True)
            break
        ratio = difflib.SequenceMatcher(
            None,
            canonical_match_view(proposed_span),
            canonical_match_view(cand["matched"])).ratio()
        if best is None or ratio > best[2]:
            best = (cand, False, ratio)
    if best is not None:
        cand, verified = best[0], best[1]
        if verified:
            return {
                "evidence_span": original_text[cand["start"]:cand["end"]],
                "start_offset": cand["start"],
                "end_offset": cand["end"],
                "grounding_status": "VALID",
                "source_field": source_field,
                "highlight": proposed_span[:100],
                "match_type": "verified_candidate",
                "match_diagnostics": {"locator": cand["match_type"]},
            }
        # Diagnostics-only fuzzy region: provenance kept for highlight /
        # internal verification, NEVER display authority.
        return {
            "evidence_span": original_text[cand["start"]:cand["end"]],
            "start_offset": cand["start"],
            "end_offset": cand["end"],
            "grounding_status": "FUZZY",
            "source_field": source_field,
            "highlight": proposed_span[:100],
            "match_type": cand["match_type"],
            "match_diagnostics": {
                "prefix_len": len(canonical_match_view(proposed_span)[:20]),
                "full_candidate_length": cand["end"] - cand["start"],
                "similarity": round(float(best[2]), 4),
            },
        }

    # --- All attempts failed ---
    return _fail()


def _keyword_semantic_locate(query: str, text: str, context_chars: int = 150) -> Optional[tuple]:
    """Locate the most query-relevant region in text using keyword density.

    Returns (start_offset, end_offset, highlight_text) or None.
    """
    # Extract keywords (Chinese 2+ chars, English 3+ chars)
    keywords = set()
    for m in re.finditer(r'[一-鿿]{2,}', query):
        keywords.add(m.group())
    for m in re.finditer(r'[a-zA-Z0-9]{3,}', query):
        keywords.add(m.group().lower())

    if not keywords:
        return None

    text_lower = text.lower()
    positions = []
    for kw in keywords:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = text_lower.find(kw_lower, start)
            if idx == -1:
                break
            positions.append((idx, kw))
            start = idx + 1

    if not positions:
        return None

    # Find densest cluster
    import bisect
    pos_list = sorted([p[0] for p in positions])
    best_start = pos_list[0]
    best_density = 0

    window = context_chars
    for pos in pos_list:
        lo = bisect.bisect_left(pos_list, pos - window)
        hi = bisect.bisect_right(pos_list, pos + window)
        nearby = hi - lo
        if nearby > best_density:
            best_density = nearby
            best_start = pos

    # Extract semantic snippet around best cluster
    snippet_start = max(0, best_start - 30)
    snippet_end = min(len(text), snippet_start + context_chars)

    # Expand to sentence boundaries
    s_start, s_end = _find_sentence_boundaries(text, best_start)
    # Use sentence boundary if it gives reasonable length
    if s_end - s_start >= 20 and s_end - s_start <= 400:
        snippet_start = s_start
        snippet_end = s_end

    highlight = text[best_start:best_start + min(60, snippet_end - best_start)]
    return (snippet_start, snippet_end, highlight.strip())


def generate_semantic_snippet(
    record: dict,
    spans: list,
    max_total: int = 350,
    gap_marker: str = "……",
) -> str:
    """Generate a readable multi-span snippet from grounded evidence spans.

    For multiple spans far apart, inserts gap markers instead of showing
    the full text between them.

    Args:
        record: Full record dict
        spans: List of (start_offset, end_offset, highlight) tuples
        max_total: Maximum total snippet length
        gap_marker: Ellipsis between non-adjacent spans

    Returns:
        Readable snippet string
    """
    if not spans:
        return ""

    original_text = get_original_text(record)
    if not original_text:
        return ""

    # Sort spans by position
    spans_sorted = sorted(spans, key=lambda s: s[0])

    parts = []
    last_end = -1
    total_len = 0

    for start, end, _highlight in spans_sorted:
        if start < 0 or end < 0:
            continue

        if last_end >= 0 and start > last_end + 10:
            # Gap between spans
            if total_len + len(gap_marker) >= max_total:
                break
            parts.append(gap_marker)
            total_len += len(gap_marker)

        span_text = original_text[start:end]
        remaining = max_total - total_len
        if remaining <= 0:
            break
        if len(span_text) > remaining:
            span_text = span_text[:remaining]
        parts.append(span_text)
        total_len += len(span_text)
        last_end = end

    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════
# RT-020 — Exact grounding rewrite on immutable SourceSnapshot (T003/T032)
# ══════════════════════════════════════════════════════════════════════════
# Contract (final spec §4.4/§23.2, decision register Q140):
#   * Fuzzy/normalized methods may LOCATE a candidate, but the accepted
#     result must be an EXACT locator into the immutable evidence_text of a
#     CITATION_ELIGIBLE SourceSnapshot (Unicode code-point offsets).
#   * User-visible grounding validity is binary: EXACT or INVALID.
#   * Synthetic (`as`) summaries, query snippets, and body-start fallbacks
#     are never accepted as evidence.
#   * Multiple non-contiguous spans are supported.

GROUNDING_EXACT = "EXACT"
GROUNDING_INVALID = "INVALID"


def _eligible_evidence_text(record: dict) -> tuple:
    """Return (evidence_text, source_field) for citation-eligible records.

    Only immutable source body text is eligible. Synthetic summaries
    (`as`) are hints, never citation evidence (final spec §7)."""
    if not isinstance(record, dict):
        return ("", "none")
    if str(record.get("evidence_eligibility") or "CITATION_ELIGIBLE") != "CITATION_ELIGIBLE":
        return ("", "ineligible")
    for field in ("fb", "b"):
        text = record.get(field, "") or ""
        if isinstance(text, str) and text.strip():
            return (text, field)
    # Summary-only record: synthetic text is NOT citation evidence.
    return ("", "summary_only")


def _fuzzy_locate_raw(span: str, evidence_text: str, min_ratio: float = 0.62):
    """Fuzzy-LOCATE a raw candidate region (location method only).

    The returned range consists of RAW code points carved from raw
    sentences — unlike the old T003 path, a fuzzy hit never yields an
    approximate normalized offset; the locator stays exact by
    construction (final spec §4.3: any fuzzy match that cannot resolve
    to an exact evidence_text range is invalid evidence)."""
    span_sentences = _extract_sentences(span)
    text_sentences = _extract_sentences(evidence_text)
    if not span_sentences or not text_sentences:
        return None
    # Probe with the longest span sentence — the most stable signal.
    probe = max(span_sentences, key=lambda item: len(item[0]))[0]
    best_score, best = 0.0, None
    for sent_text, s_start, s_end in text_sentences:
        ratio = difflib.SequenceMatcher(None, probe, sent_text).ratio()
        if ratio > best_score:
            best_score, best = ratio, (s_start, s_end)
    if best is not None and best_score >= min_ratio:
        return best
    # Prefix fallback: a stable ≥20-char normalized prefix must exist
    # verbatim in the RAW text; the located region is raw by definition.
    norm_prefix = re.sub(r"\s+", " ", span.strip())
    for prefix_len in (40, 30, 20):
        if len(norm_prefix) >= prefix_len:
            idx = evidence_text.find(norm_prefix[:prefix_len])
            if idx >= 0:
                end = min(idx + len(span), len(evidence_text))
                return (idx, end)
    return None


def ground_citation_exact(record: dict, proposed_spans, claim_text: str = "",
                          query: str = "", snapshot=None) -> dict:
    """RT-020 exact grounding over an immutable SourceSnapshot.

    Accepts one proposed span (str) or several non-contiguous spans (list).
    Returns an EvidenceRef-shaped dict:

        {
          "grounding_status": "EXACT" | "INVALID",
          "source_snapshot_id", "record_id", "evidence_sha256",
          "evidence_text_field": "fb" | "b",
          "evidence_spans": [{start, end, text, locator_type, match_type,
                              normalized_start?, normalized_end?}],
          "exact_text": matched raw text,
          "match_type": overall,
          "invalid_reason": "" | reason,
        }

    Location ladder (every rung ends at an EXACT raw code-point range or
    INVALID):
      1. exact substring of the immutable evidence_text
      2. normalized (NFKC+whitespace) locate mapped back through the
         reversible offset map — unmappable ⇒ fall through, never approximate
      3. fuzzy locate of a RAW candidate region (sentence similarity or
         verbatim prefix); the region is raw text so the locator is exact
    A span with no resolvable exact range ⇒ the whole citation is INVALID.
    """
    from source_snapshot import SourceSnapshot

    def invalid(reason):
        return {
            "grounding_status": GROUNDING_INVALID, "source_snapshot_id": "",
            "record_id": (record or {}).get("record_id") if isinstance(record, dict) else None,
            "evidence_sha256": "", "evidence_text_field": "none",
            "evidence_spans": [], "exact_text": "", "match_type": "none",
            "invalid_reason": reason,
        }

    evidence_text, field = _eligible_evidence_text(record)
    if not evidence_text:
        # Summary-only / quarantined / retrieval-only records are INVALID
        # citation evidence — never fall back to the AI summary (T049/§7).
        return invalid(field if field != "none" else "no_evidence_text")

    try:
        snap = snapshot or SourceSnapshot.from_record(
            (record or {}).get("record_id", "unknown"), record)
    except Exception as exc:  # unmappable normalization etc. — fail closed
        return invalid(f"snapshot_error:{type(exc).__name__}")

    if isinstance(proposed_spans, str):
        proposed_spans = [proposed_spans] if proposed_spans.strip() else []
    proposed_spans = [s for s in (proposed_spans or [])
                      if isinstance(s, str) and s.strip()]
    if not proposed_spans:
        # No proposed span: keyword/query locate is explicitly NOT accepted
        # (T032.DOD-01/DOD-06 — query-based excerpts are internal only and
        # can never become the final evidence-card core).
        return invalid("no_proposed_span")

    spans_out = []
    overall = "exact"
    for span in proposed_spans:
        # 1) exact substring
        idx = evidence_text.find(span)
        if idx >= 0:
            spans_out.append({"start": idx, "end": idx + len(span),
                              "text": evidence_text[idx:idx + len(span)],
                              "locator_type": "TEXT_SPAN", "match_type": "exact"})
            continue
        # 2) normalized locate mapped back exactly
        try:
            from source_snapshot import normalize_with_map, NormalizedView
            needle = normalize_with_map(span).text
            nidx = snap.normalized_text.find(needle)
            if nidx >= 0:
                mapping = NormalizedView(snap.normalized_text, snap.offset_map).raw_range(
                    nidx, nidx + len(needle))
                if mapping is not None:
                    s0, e0 = mapping
                    spans_out.append({
                        "start": s0, "end": e0, "text": snap.raw_text[s0:e0],
                        "locator_type": "TEXT_SPAN",
                        "match_type": "normalized_exact_map",
                        "normalized_start": nidx,
                        "normalized_end": nidx + len(needle)})
                    overall = "normalized_exact_map"
                    continue
        except Exception:
            pass
        # 3) fuzzy locate of a RAW region (locator stays exact raw offsets)
        hit = _fuzzy_locate_raw(span, evidence_text)
        if hit is not None:
            s0, e0 = hit
            spans_out.append({"start": s0, "end": e0,
                              "text": evidence_text[s0:e0],
                              "locator_type": "TEXT_SPAN",
                              "match_type": "fuzzy_located_exact"})
            overall = "fuzzy_located_exact"
            continue
        # Nothing resolved this span to an exact raw range ⇒ INVALID
        return invalid("span_not_found")

    if not spans_out:
        return invalid("span_not_found")

    return {
        "grounding_status": GROUNDING_EXACT,
        "source_snapshot_id": snap.source_snapshot_id,
        "record_id": snap.record_id,
        "evidence_sha256": snap.content_hash,
        "evidence_text_field": field,
        "evidence_spans": spans_out,
        "exact_text": "".join(s["text"] for s in spans_out),
        "match_type": overall,
        "invalid_reason": "",
    }


def enforce_display_integrity(citations, answer_status_str: str = "") -> list:
    """RT101-V14 display seam — the committed display-gating authority.

    A citation row may be user-DISPLAYED only when it carries prior display
    authorization AND grounding_status == "VALID" against the stored/pinned
    snapshot authority. FUZZY is diagnostic and can never carry display
    authority. An UNSUPPORTED terminal has nothing verified-supported, so
    every row is withheld.

    Withholding is always IN PLACE: ``display_authorized=False`` and
    ``supports_claim_ids=[]`` — rows are never removed from the payload
    (no payload surgery). Internal verifier evidence and the RTA/RTB/RTC
    diagnostics contract (non-authoritative rows stay visible for the
    pipeline, reference cards hidden) are untouched, and the formal
    scorer's displayed universe (display_authorized OR supports_claim_ids)
    excludes withheld rows entirely (withheld_not_displayed).

    Returns the same row list for call-site convenience.
    """
    rows = citations if isinstance(citations, list) else list(citations or [])
    unsupported = str(answer_status_str or "").upper() == "UNSUPPORTED"
    for c in rows:
        if not isinstance(c, dict):
            continue
        if unsupported or (not c.get("display_authorized", True)
                           or c.get("grounding_status") != "VALID"):
            c["display_authorized"] = False
            c["supports_claim_ids"] = []
    return rows


def is_valid_grounding(result: dict) -> bool:
    """True only for EXACT grounding — user-visible validity is binary."""
    return bool(result) and result.get("grounding_status") == GROUNDING_EXACT


def verify_exact_spans(result: dict, record: dict) -> bool:
    """Re-verify an exact grounding result against the immutable evidence
    text (defense in depth for RT-028 done-event filtering)."""
    if not is_valid_grounding(result):
        return False
    evidence_text, field = _eligible_evidence_text(record)
    if not evidence_text:
        return False
    for s in result.get("evidence_spans", []):
        start, end = s.get("start", -1), s.get("end", -1)
        if not (0 <= start < end <= len(evidence_text)):
            return False
        if evidence_text[start:end] != s.get("text"):
            return False
    return True
