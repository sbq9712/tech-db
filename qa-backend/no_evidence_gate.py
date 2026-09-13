"""RT-101 repair RD-2 — deterministic generator no-evidence gate.

Phase09 corpus adjudication (docs/remediation/phase09_RT101_corpus_
adjudication.json, runtime defect RD-2): when the retrieval layer found
nothing relevant, the legacy path still generated an answer — the
generator followed its prompt contract and honestly declared "数据库中
没有相关信息" — and that self-abstention was then serialized as an
ANSWERED payload with displayed citations.  Honest semantics require the
canonical four-state terminal instead: UNSUPPORTED (no-evidence abstain),
empty citations, boundary message.

This module is the deterministic, no-LLM detector for the product's own
no-evidence contract.  It deliberately requires ALL of:

  1. the draft matches the generator prompt-contract phrase family
     (the exact instruction the product gives its generator: 若资料中
     没有相关信息，诚实回答 "数据库中没有相关信息" plus immediate
     paraphrase family), and
  2. the draft contains NO citation markers (a substantive answer that
     cites [1] is never an abstention), and
  3. the draft is short (an abstention notice is not a researched
     answer; substantive answers exceed the notice length), and
  4. retrieval did surface candidates (if retrieval already said
     weak/exhausted, the existing early gate handles it — this module
     must not widen that decision).

Any miss fails OPEN toward the existing pipeline (the draft simply goes
through normal verification) — the gate can only ever redirect a
self-declared no-content draft to the canonical abstain, never suppress
a substantive answer.
"""
from __future__ import annotations

import re

# The product's generator prompt contract (server.py legacy prompt):
#   "如果资料中没有相关信息，诚实回答「数据库中没有相关信息」"
# Phrase family provenance (codex review B): every phrase below is a
# product-authored string, NOT derived from any holdout material —
#   * the prompt-contract sentence itself (server.py generator prompt);
#   * canonical abstention/boundary messages the product already emits:
#     server.py weak-query exit "数据库中没有足够的情报来回答...",
#     topic-exhausted exit "...暂未找到...相关资料",
#     follow-up empty exit "上一轮未找到相关资料...", and the canonical
#     knowledge-boundary text "当前没有找到足够证据"
#     (server.py / knowledge_boundary.py);
#   * mechanical lexical recombinations of exactly those strings
#     (找到↔未找到, 相关信息↔相关资料) so a generator may restate the
#     contract in a neighboring wording and still be recognized.
# Kept deliberately narrow: false positives here would wrongly abstain a
# substantive answer, so every phrase contains an explicit
# no-relevant-information declaration.  The detector additionally fails
# OPEN on every other dimension (length, citation markers, retrieval
# state), so widening this list could never suppress a citing draft.
_NO_EVIDENCE_PHRASES = (
    "数据库中没有相关信息",        # generator prompt contract (verbatim)
    "数据库中没有找到相关信息",    # contract, 找到-variant
    "数据库中未找到相关信息",      # contract, 未找到-variant
    "没有找到相关信息",            # contract minus scope prefix
    "未找到相关资料",              # follow-up empty exit (server.py)
    "没有找到相关资料",            # same, 没找到-variant
    "当前数据库中暂未找到相关",    # topic-exhausted exit (server.py)
    "没有足够的情报来回答",        # weak-query boundary exit (server.py)
    "数据库中没有足够的情报",      # same, prefix variant
    "没有足够的信息来回答",        # same, 信息-variant
    "当前没有找到足够证据",        # canonical knowledge boundary message
    "没有相关的资料",              # contract, 资料-variant
    "没有相关信息",                # contract minus scope prefix
)

_CITATION_MARKER_RE = re.compile(r"\[\d{1,2}\]")

# Rationale (codex review B): an abstention notice is a one-liner plus
# optional courtesy sentence — the longest product-authored abstention
# message is ~60 chars.  400 chars is a ~6x margin that no honest
# abstention reaches, and ANY longer draft (or any draft containing a
# citation marker, or any empty retrieval result set) fails OPEN toward
# the normal verification pipeline, so the cap cannot become a
# behavioral special-case for substantive answers.
_MAX_ABSTAIN_DRAFT_CHARS = 400


def declared_no_evidence(draft: str, *, has_retrieval_results: bool = True) -> bool:
    """True iff the draft is a self-declared no-evidence abstention."""
    if not draft:
        return False
    text = draft.strip()
    if len(text) > _MAX_ABSTAIN_DRAFT_CHARS:
        return False
    if not has_retrieval_results:
        # retrieval already produced nothing; other canonical gates own
        # that decision — do not widen it here.
        return False
    if _CITATION_MARKER_RE.search(text):
        return False
    return any(phrase in text for phrase in _NO_EVIDENCE_PHRASES)
