"""
T028 — Contextual Chunking + Parent/Child Retrieval
====================================================
Small-granularity precise recall + Parent Record context recovery.

Chunk Schema:
{
    "chunk_id": str,
    "record_id": int,
    "text": str,
    "context_prefix": str,  # title, source, date, category, entities, section heading
    "start_offset": int,
    "end_offset": int,
    "section": str,
    "chunk_version": str,
}

Splitting Principles:
  1. Prefer paragraph/semantic/section boundaries
  2. Not simple N-char hard cuts
  3. Overlap when necessary but avoid massive duplication
  4. Preserve exact offsets
  5. Chunks are NOT "truth text" — evidence/citation returns to parent full_body
  6. Preserve table/parameter structure when possible
  7. Generated AI summary is NOT a source chunk

Retrieval Flow:
  query → chunk vector/BM25 hits → parent record metadata →
  reranker/selector → exact evidence grounding
"""
import os
import re
import hashlib
from typing import List, Dict, Optional


CHUNK_SIZE = int(os.environ.get("QA_CHUNK_SIZE", "300"))  # Target chunk size (chars)
CHUNK_OVERLAP = int(os.environ.get("QA_CHUNK_OVERLAP", "50"))  # Overlap between chunks
MIN_CHUNK_SIZE = int(os.environ.get("QA_MIN_CHUNK_SIZE", "50"))
CHUNK_VERSION = "0.1.0"


def chunk_record(record: dict, record_idx: int = -1) -> List[dict]:
    """Split a record's text into semantic chunks.

    Args:
        record: Record dict with at least 'fb' or 'b'
        record_idx: Index of the record in the dataset

    Returns:
        List of chunk dicts with exact offsets
    """
    # Get original text (prefer full_body)
    text = record.get("fb", "") or record.get("b", "")
    if not text or len(text) < MIN_CHUNK_SIZE:
        return []  # Too short to chunk

    # Build context prefix
    context_prefix = _build_context_prefix(record)

    # Split into paragraphs first
    paragraphs = _split_paragraphs(text)

    # Group paragraphs into chunks of ~CHUNK_SIZE
    chunks = []
    current_start = 0
    current_pos = 0

    for para_text, para_start, para_end in paragraphs:
        para_len = para_end - para_start

        # If paragraph is very long, split by sentences
        if para_len > CHUNK_SIZE * 1.5:
            sentence_chunks = _split_by_sentences(
                text, para_start, para_end,
                CHUNK_SIZE, CHUNK_OVERLAP
            )
            for s_start, s_end in sentence_chunks:
                chunks.append((s_start, s_end))
        # If adding this paragraph would exceed chunk size, start new chunk
        elif current_pos + para_len > CHUNK_SIZE and current_pos > 0:
            # Save current chunk and start new one
            chunk_end = para_start  # End before this paragraph
            chunks.append((current_start, chunk_end))
            current_start = max(para_start - CHUNK_OVERLAP, 0)
            current_pos = para_end - current_start
        else:
            current_pos = para_end - current_start

    # Don't forget the last chunk
    if current_pos > 0:
        chunks.append((current_start, len(text)))

    # Build chunk dicts
    result = []
    for i, (start, end) in enumerate(chunks):
        chunk_text = text[start:end].strip()
        if len(chunk_text) < MIN_CHUNK_SIZE:
            continue

        chunk_id = hashlib.md5(
            f"{record_idx}_{start}_{end}_{chunk_text[:50]}".encode()
        ).hexdigest()[:12]

        # Detect section heading
        section = _detect_section(chunk_text)

        result.append({
            "chunk_id": chunk_id,
            "record_id": record_idx,
            "text": chunk_text,
            "context_prefix": context_prefix,
            "start_offset": start,
            "end_offset": end,
            "section": section,
            "chunk_version": CHUNK_VERSION,
        })

    return result


def _build_context_prefix(record: dict) -> str:
    """Build context prefix from record metadata."""
    parts = []

    title = record.get("t", "")
    if title:
        parts.append(title)

    source = record.get("a", record.get("s", ""))
    if source:
        parts.append(source)

    date = record.get("d", "")
    if date:
        parts.append(date)

    category = record.get("c", "")
    if category:
        leaf = category.split("/")[-1]
        if leaf:
            parts.append(f"[{leaf}]")

    tags = record.get("tg", "")
    if tags:
        parts.append(f"#{tags}")

    return " | ".join(parts)


def _split_paragraphs(text: str) -> list:
    """Split text into paragraphs with offsets.

    Returns list of (text, start_offset, end_offset).
    """
    paragraphs = []
    start = 0

    # Split on double newlines or Chinese paragraph markers
    for m in re.finditer(r"\n\s*\n|。\s*\n|。\s*(?=[A-Z一-鿿])", text):
        para_end = m.start()
        para_text = text[start:para_end].strip()
        if para_text:
            paragraphs.append((para_text, start, para_end))
        start = m.end()

    # Last paragraph
    if start < len(text):
        para_text = text[start:].strip()
        if para_text:
            paragraphs.append((para_text, start, len(text)))

    return paragraphs


def _split_by_sentences(text: str, para_start: int, para_end: int,
                        target_size: int, overlap: int) -> list:
    """Split a long paragraph by sentence boundaries."""
    # Find all sentence boundaries
    sentence_ends = []
    for m in re.finditer(r"[。！？!?；;\n]", text[para_start:para_end]):
        sentence_ends.append(para_start + m.end())

    if not sentence_ends:
        # No sentence boundaries — hard cut
        chunks = []
        pos = para_start
        while pos < para_end:
            end = min(pos + target_size, para_end)
            chunks.append((pos, end))
            pos = end - overlap
        return chunks

    # Group sentences into chunks
    chunks = []
    chunk_start = para_start
    for sent_end in sentence_ends:
        if sent_end - chunk_start >= target_size:
            chunks.append((chunk_start, sent_end))
            chunk_start = max(sent_end - overlap, para_start)

    if chunk_start < para_end:
        chunks.append((chunk_start, para_end))

    return chunks


def _detect_section(chunk_text: str) -> str:
    """Detect section heading from chunk text."""
    # Common section patterns
    section_patterns = [
        r"^[【\[]*([0-9]+[、.])\s*(.+?)[】\]]*$",
        r"^[【\[]*(第.+?[章节部分])[】\]]*$",
        r"^[【\[]*(Overview|Background|Method|Result|Conclusion|摘要|引言|方法|结果|结论|背景)[】\]]*$",
    ]
    first_line = chunk_text.split("\n")[0].strip()
    for pattern in section_patterns:
        m = re.match(pattern, first_line, re.IGNORECASE)
        if m:
            return m.group(1) if m.lastindex else first_line[:30]
    return ""
