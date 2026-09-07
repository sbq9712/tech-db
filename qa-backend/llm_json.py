"""RT101 general reliability repair — shared bounded LLM-JSON normalization.

One canonical, deterministic, bounded normalization/repair step for
JSON-shaped live-model output. It is imported by the EXISTING parser seams
(verifier._extract_json, claim_mapping._extract_json_safe, epistemic
_parse_llm_json) — it is NOT a second verifier, second mapper, or second
citation authority; it never decides truth, support, or verdicts. It only
turns recoverable serialization variations into parseable text so the
canonical schema validation downstream still rules.

Guarantees:
  * Bounded work: every strategy is O(len(text)) with a bounded number of
    attempts and a bounded input window (QA_LLM_JSON_MAX_CHARS, default
    200_000). No retries, no network, no model calls.
  * Fail-closed: unparseable input returns None — callers keep their
    existing UNVERIFIED/fallback behavior. A response that cannot safely
    be parsed NEVER becomes PASSED/SUPPORTED.
  * No semantics: the function never injects verdict fields, never flips
    booleans, never fabricates missing required keys.

Repair strategies (pure serialization-level):
  0. BOM / zero-width / full-width bracket+colon punctuation folding
     (outside string literals), CRLF folding.
  1. <think>…</think> reasoning-block and bare-reasoning stripping —
     GLM-style models sometimes emit reasoning before/after the JSON.
  2. Markdown fence stripping (```json … ``` / ``` … ```).
  3. Leading/trailing prose trimming to the outermost balanced object or
     array span (string-literal aware).
  4. Truncated-stream closing: if the outermost span is unbalanced (the
     model hit the token cap), close the smallest number of open string /
     array / object contexts that can make the prefix parse. Values that
     were cut mid-token are DROPPED by the close, never guessed.
  5. Trailing-comma removal and control-character cleanup (existing
     behavior, preserved).
  6. Provider envelope unwrapping: if the parsed object is a single-key
     wrapper whose value is the JSON payload as a STRING
     ({"response": "..."} / {"content": "..."} / {"text": "..."} /
     {"result": "..."} / {"output": "..."}), unwrap one level. The inner
     text must itself parse; nothing is coerced.
  7. Smart-quote folding INSIDE string literals is NOT attempted (it
     changes data); only structural quotes are handled by refusing to
     "repair" them (fail-closed).
"""
from __future__ import annotations

import json
import os
import re

MAX_CHARS = int(os.environ.get("QA_LLM_JSON_MAX_CHARS", "200000"))

_ENVELOPE_KEYS = ("response", "content", "text", "result", "output",
                  "message", "data")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# An UNCLOSED <think> block: everything from <think> up to the LAST
# balanced JSON-ish closer is reasoning. We handle it by locating the
# first '{' or '[' that begins a balanced span AFTER <think> — done in
# normalize via _ salvage below (structural: we only DELETE the literal
# "<think>" marker line if no closer exists and a brace follows).
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")
# Structural (outside string literal) full-width punctuation that JSON
# tolerates after folding: braces, brackets, colon, comma, quotes.
_FW_CLASS = {"｛": "{", "｝": "}", "［": "[", "］": "]", "：": ":",
             "，": ",", "＂": '"', "＇": "'", "（": "(", "）": ")"}
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")


def _fold_fullwidth_outside_strings(text: str) -> str:
    """Fold full-width structural punctuation outside string literals."""
    out = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        out.append(_FW_CLASS.get(ch, ch))
    return "".join(out)


def _outermost_span(text: str, open_ch: str, close_ch: str):
    """Return (start, end_exclusive_of_close+1) of the outermost balanced
    span, string-aware. Returns (start, None) when unbalanced (truncated);
    returns None when no opener exists."""
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return (start, end)


def _open_stack(text: str):
    """Return the stack of still-open containers in `cut` text (string-aware)."""
    stack = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return stack


def _close_truncated(fragment: str, open_ch: str, close_ch: str):
    """Bounded truncated-stream closing for one container type.

    The fragment starts at the outermost opener and is missing closers.
    Incomplete trailing values are DROPPED, never guessed:
      * value cut mid-string → drop from the dangling opening quote
        (and back to the previous comma if a `"key":` separator would
        dangle);
      * bare trailing token → drop back to the last complete element;
      * structurally ambiguous quote placement (a quote inside/after a
        completed string value that never balances) → fail closed (None).
    Nothing is ever synthesized inside the containers: closers are only
    appended for containers already open at the cut point.
    """
    in_str = False
    escaped = False
    last_complete = 0   # cut index: everything before it is fully complete
    last_comma = -1     # last comma position (safe point BEFORE next value)
    dangling_quote = -1
    pending = -1        # index after a string close awaiting key/value role
    for i, ch in enumerate(fragment):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
                pending = i + 1  # role (key vs value) not yet known
            continue
        if ch == '"':
            in_str = True
            dangling_quote = i
        elif ch == ":":
            pending = -1  # the closed string was a KEY, not a value
        elif ch == ",":
            if pending >= 0:
                last_complete = pending  # closed string was a VALUE
                pending = -1
            last_complete = i  # a comma is a safe cut BEFORE the next value
            last_comma = i
        elif ch in "}]":
            pending = -1
            last_complete = i + 1
    if not in_str and pending >= 0 and not fragment[pending:].strip():
        # A trailing completed string VALUE closed the document cleanly
        # (e.g. ... {"id": "c2"} cut before its closer is NOT this case —
        # only a value at the very end with nothing after it commits).
        last_complete = pending
        pending = -1
    if in_str:
        # Value cut mid-string: drop the partial string from its opening
        # quote — never synthesize string content.
        cand = fragment[:dangling_quote].rstrip()
        if cand.endswith(":"):
            # `"key":` would dangle without a value → drop the whole
            # incomplete pair back to the previous comma.
            if last_comma > 0:
                cand = fragment[:last_comma].rstrip()
                if cand.endswith(","):
                    cand = cand[:-1]
            else:
                return None
        elif cand.endswith(","):
            cand = cand[:-1].rstrip()
        cut = cand
    else:
        # Bare trailing token / dangling key: cut to the last complete
        # element, dropping the incomplete trailing value.
        cut = fragment[:last_complete].rstrip()
        if cut.endswith(","):
            cut = cut[:-1].rstrip()
    if not cut:
        return None
    stack2 = _open_stack(cut)
    if not stack2:
        return None
    closer = {"{": "}", "[": "]"}
    repaired = cut + "".join(closer[c] for c in reversed(stack2))
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _unwrap_envelope(obj: dict):
    """One-level provider-envelope unwrap: single meaningful key whose
    value is a string that itself parses as JSON."""
    if not isinstance(obj, dict):
        return None
    for key in _ENVELOPE_KEYS:
        if key in obj and isinstance(obj[key], str) and obj[key].strip():
            inner = parse_json(obj[key])
            if inner is not None:
                return inner
    return None


def normalize_llm_text(text: str) -> str:
    """Deterministic bounded text normalization (strategies 0-2)."""
    if not isinstance(text, str):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _ZERO_WIDTH_RE.sub("", t)
    t = _THINK_RE.sub("", t)
    # Unclosed <think>: keep only the portion from the first JSON opener on.
    low = t.find("<think>")
    if low >= 0:
        brace = min((x for x in (t.find("{", low), t.find("[", low))
                     if x >= 0), default=-1)
        if brace > low:
            t = t[brace:]
        else:
            t = t[:low]
    t = _FENCE_RE.sub("", t.strip())
    return t.strip()


def parse_json(text: str):
    """Parse JSON-shaped LLM output; returns Python object or None.

    Bounded, deterministic, semantics-free. None means "not safely
    parseable" — callers must fail closed.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # 0) direct (with one-level envelope unwrap for dict results)
    try:
        direct = json.loads(text)
    except Exception:
        direct = None
    if isinstance(direct, dict):
        inner = _unwrap_envelope(direct)
        if inner is not None:
            return inner
    if direct is not None:
        return direct

    t = normalize_llm_text(text)

    # 1) full-width folding (outside strings)
    t2 = _fold_fullwidth_outside_strings(t)

    # 2) plain attempt of normalized text
    for candidate in (t, t2):
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 3-6) outermost balanced span, object or array — whichever opener
    # appears FIRST decides the expected top-level shape (a caller asking
    # for an array must not receive the first inner object).
    for cand in (t2, t):
        obj_span = _outermost_span(cand, "{", "}")
        arr_span = _outermost_span(cand, "[", "]")
        spans = []
        if obj_span and obj_span[0] >= 0:
            spans.append((obj_span[0], obj_span[1], "{", "}"))
        if arr_span and arr_span[0] >= 0:
            spans.append((arr_span[0], arr_span[1], "[", "]"))
        if not spans:
            continue
        spans.sort(key=lambda x: x[0])
        for idx, (s, e, open_ch, close_ch) in enumerate(spans):
            frag = cand[s:e] if e else cand[s:]
            try:
                return json.loads(frag)
            except Exception:
                pass
            # trailing comma / control cleanup
            fixed = re.sub(r",\s*([}\]])", r"\1", frag)
            fixed = re.sub(r"[\x00-\x1f]", "", fixed)
            try:
                return json.loads(fixed)
            except Exception:
                pass
            if e is None and idx == 0:
                # Truncated stream: only the EARLIEST opener may be the
                # truncated TOP-LEVEL container; an unbalanced inner span
                # must never be closed into a fabricated smaller value.
                val = _close_truncated(frag, open_ch, close_ch)
                if val is not None:
                    return val

    return None


def parse_json_or_envelope(text: str):
    """parse_json + one-level envelope unwrap."""
    val = parse_json(text)
    if isinstance(val, dict):
        inner = _unwrap_envelope(val)
        if inner is not None:
            return inner
    return val
