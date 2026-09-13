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
# Phrase family = that sentence plus its observed paraphrases.  Kept
# deliberately narrow: false positives here would wrongly abstain a
# substantive answer, so every phrase contains an explicit
# no-relevant-information declaration.
_NO_EVIDENCE_PHRASES = (
    "数据库中没有相关信息",
    "数据库中没有找到相关信息",
    "数据库中未找到相关信息",
    "没有找到相关信息",
    "未找到相关资料",
    "没有找到相关资料",
    "当前数据库中暂未找到相关",
    "没有足够的情报来回答",
    "数据库中没有足够的情报",
    "没有足够的信息来回答",
    "当前没有找到足够证据",
    "没有相关的资料",
    "没有相关信息",
)

_CITATION_MARKER_RE = re.compile(r"\[\d{1,2}\]")

# An abstention notice is short.  Substantive grounded answers (which
# always carry [n] markers anyway) are far longer.
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
