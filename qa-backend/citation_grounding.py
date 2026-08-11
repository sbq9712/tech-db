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
