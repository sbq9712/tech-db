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


def fuzzy_locate_span(span: str, text: str, min_ratio: float = 0.75) -> tuple:
    """Attempt to locate span in text using fuzzy matching.

    Tries:
    1. Normalized whitespace match
    2. Sentence-level similarity matching
    3. Partial substring (first 20+ chars)

    Returns (found, start_offset, end_offset, matched_text).
    """
    if not span or not text:
        return (False, -1, -1, "")

    # Strategy 1: Normalize whitespace
    span_norm = re.sub(r'\s+', ' ', span.strip())
    text_norm = re.sub(r'\s+', ' ', text)
    idx = text_norm.find(span_norm)
    if idx >= 0:
        # Map back to original text position (approximate)
        return (True, idx, idx + len(span_norm), text_norm[idx:idx + len(span_norm)])

    # Strategy 2: Sentence-level similarity
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
            # Extend to cover subsequent sentences if multi-sentence span
            start_offset = best_match[0]
            end_offset = best_match[1]
            if len(span_sentences) > 1:
                # Try to extend to cover more of the span
                remaining_sentences = text_sentences
                for i, (_, ss, se) in enumerate(remaining_sentences):
                    if ss == start_offset:
                        # Extend forward
                        covered = 1
                        for j in range(i + 1, min(i + len(span_sentences), len(remaining_sentences))):
                            end_offset = remaining_sentences[j][2]
                            covered += 1
                            if covered >= len(span_sentences):
                                break
                        break
            return (True, start_offset, end_offset, text[start_offset:end_offset])

    # Strategy 3: Partial substring (first significant chunk ≥20 chars)
    for prefix_len in (40, 30, 20):
        if len(span_norm) >= prefix_len:
            prefix = span_norm[:prefix_len]
            idx = text_norm.find(prefix)
            if idx >= 0:
                # Extend to approximate full span length
                end = min(idx + len(span_norm), len(text_norm))
                return (True, idx, end, text_norm[idx:end])

    return (False, -1, -1, "")


def ground_citation_evidence(
    record: dict,
    proposed_span: str = "",
    claim_text: str = "",
    query: str = "",
) -> dict:
    """Ground a citation by finding the exact evidence span in the original text.

    Args:
        record: The full record dict from all-records-lite.json
        proposed_span: LLM-suggested evidence text (may be imprecise)
        claim_text: The claim this citation should support (for semantic locating)
        query: The original user query (for keyword fallback)

    Returns:
        {
            "evidence_span": str,       # The exact text found
            "start_offset": int,        # Character offset in original text
            "end_offset": int,
            "grounding_status": str,    # "VALID" | "FUZZY" | "GROUNDING_FAIL"
            "source_field": str,        # "fb" | "b" | "as"
            "highlight": str,           # Key phrase to highlight in UI
        }
    """
    original_text = get_original_text(record)
    source_field = get_text_source(record)

    if not original_text.strip():
        return {
            "evidence_span": "",
            "start_offset": -1,
            "end_offset": -1,
            "grounding_status": "GROUNDING_FAIL",
            "source_field": "none",
            "highlight": "",
        }

    # --- Attempt 1: Exact match of proposed span ---
    if proposed_span:
        found, start, end = verify_span_in_text(proposed_span, original_text)
        if found:
            return {
                "evidence_span": original_text[start:end],
                "start_offset": start,
                "end_offset": end,
                "grounding_status": "VALID",
                "source_field": source_field,
                "highlight": proposed_span[:100],
            }

    # --- Attempt 2: Fuzzy match of proposed span ---
    if proposed_span and len(proposed_span) >= 10:
        found, start, end, matched = fuzzy_locate_span(proposed_span, original_text)
        if found:
            return {
                "evidence_span": matched,
                "start_offset": start,
                "end_offset": end,
                "grounding_status": "FUZZY",
                "source_field": source_field,
                "highlight": proposed_span[:100],
            }

    # --- Attempt 3: Semantic locate using claim text keywords ---
    search_text = claim_text or query
    if search_text:
        result = _keyword_semantic_locate(search_text, original_text)
        if result:
            start, end, highlight = result
            return {
                "evidence_span": original_text[start:end],
                "start_offset": start,
                "end_offset": end,
                "grounding_status": "FUZZY",
                "source_field": source_field,
                "highlight": highlight,
            }

    # --- All attempts failed ---
    return {
        "evidence_span": "",
        "start_offset": -1,
        "end_offset": -1,
        "grounding_status": "GROUNDING_FAIL",
        "source_field": source_field,
        "highlight": "",
    }


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
