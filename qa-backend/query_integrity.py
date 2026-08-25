"""RT-041 — deterministic query semantic-diff and safe rewrite authority.

Model rewrites are advisory. Critical semantic fields are extracted and
compared locally; uncertainty preserves the original query or explicitly
escalates instead of blessing a rewrite.
"""
from __future__ import annotations

import dataclasses
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


_TIME = re.compile(
    r"(?:19|20)\d{2}(?:[-/.年](?:0?[1-9]|1[0-2])(?:月)?)?|"
    r"\bQ[1-4]\b|去年|今年|前年|明年|当前|目前|现在|最近|最新|"
    r"historical|current|latest|today|yesterday|last\s+year", re.I)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?\s*(?:%|％|[kmgt]?[bB](?:/s)?|GHz|MHz|W|kW|V|年|月|天|倍)?")
_NEG = re.compile(r"(?:不|没有|未|无|非|不能|不得|并非|not|no\b|never|without|isn't|aren't|doesn't|didn't|cannot)", re.I)
_MODAL = re.compile(r"(?:可能|也许|预计|计划|必须|应该|可以|无法|据称|预测|"
                    r"may|might|could|should|must|plan(?:ned)?|expected|reportedly)", re.I)
_COMPARE = re.compile(r"(?:\bvs\.?\b|\bversus\b|对比|相比|比较|区别|差异|还是|与|和)", re.I)
_DIMENSIONS = (
    "价格", "成本", "性能", "带宽", "延迟", "功耗", "能效", "容量", "速度",
    "安全", "可靠性", "吞吐", "精度", "市场份额", "架构", "材料", "路线图",
    "price", "cost", "performance", "bandwidth", "latency", "power",
    "capacity", "speed", "safety", "reliability", "throughput", "accuracy",
)
_INTENT_RULES = (
    ("comparison", re.compile(r"vs|versus|对比|相比|比较|区别|差异|哪个好", re.I)),
    ("trend", re.compile(r"趋势|演进|变化|历年|时间线|trend|evolution|over time", re.I)),
    ("current", re.compile(r"当前|目前|现在|最新|current|latest", re.I)),
    ("negative_existence", re.compile(r"不存在|没有任何|does not exist|no .* exists", re.I)),
)
_CONTEXT_REFERENCE = re.compile(
    r"(?:它|其|这个|该项|该产品|前者|后者|呢[？?]?$|"
    r"\bit\b|\bthey\b|\bthat\b|\bthose\b|"
    r"\bwhat about\b|\bhow about\b)", re.I)


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _ordered_unique(values: Iterable[str]) -> Tuple[str, ...]:
    out, seen = [], set()
    for value in values:
        value = _norm(value).strip(" ,，。?？!！:：;；()（）[]【】\"'")
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return tuple(out)


def _extract_entities(text: str) -> Tuple[str, ...]:
    """Conservative explicit entity candidates, not a probabilistic NER."""
    text = _norm(text)
    found: List[str] = []
    english = re.findall(r"\b[A-Za-z][A-Za-z0-9_.+-]*\b", text)
    stop = {"vs", "versus", "compare", "comparison", "current", "latest",
            "trend", "price", "cost", "performance", "bandwidth",
            "latency", "power", "and", "or", "not", "with", "without"}
    found.extend(token for token in english
                 if token.casefold() not in stop
                 and (any(ch.isupper() or ch.isdigit() for ch in token)
                      or " " in token))
    found.extend(re.findall(r"[“\"《]([^”\"》]{2,40})[”\"》]", text))
    for m in re.finditer(r"([\u3400-\u9fffA-Za-z0-9_.+-]{2,24})\s*(?:和|与|对比|相比|还是)\s*([\u3400-\u9fffA-Za-z0-9_.+-]{2,24})", text):
        found.extend(m.groups())
    for m in re.finditer(r"([\u3400-\u9fff]{2,12})(?=的?(?:价格|成本|性能|带宽|延迟|功耗|趋势|路线图|概述|材料))", text):
        candidate = m.group(1).removesuffix("的")
        if (not candidate.startswith(("它", "其", "这个", "该项"))
                and candidate not in {
                    "当前", "目前", "现在", "最近", "最新", "今年",
                    "去年", "明年", "全球", "中国", "国内"}):
            found.append(candidate)
    return _ordered_unique(found)


def _extract_time(text: str) -> Tuple[str, ...]:
    return _ordered_unique(m.group(0) for m in _TIME.finditer(_norm(text)))


def _extract_numbers(text: str) -> Tuple[str, ...]:
    return _ordered_unique(m.group(0).replace(" ", "")
                           for m in _NUMBER.finditer(_norm(text)))


def _extract_negation(text: str) -> Tuple[str, ...]:
    return _ordered_unique(m.group(0) for m in _NEG.finditer(_norm(text)))


def _extract_modality(text: str) -> Tuple[str, ...]:
    return _ordered_unique(m.group(0) for m in _MODAL.finditer(_norm(text)))


def _extract_dimensions(text: str) -> Tuple[str, ...]:
    low = _norm(text).casefold()
    return _ordered_unique(d for d in _DIMENSIONS if d.casefold() in low)


def _extract_comparison_set(text: str) -> Tuple[str, ...]:
    return _extract_entities(text) if _COMPARE.search(text or "") else tuple()


def _intent(text: str) -> str:
    for name, pattern in _INTENT_RULES:
        if pattern.search(text or ""):
            return name
    return "fact"


def _scope(text: str) -> Tuple[str, ...]:
    value = _norm(text).casefold()
    scopes = []
    for name, terms in {
        "global": ("全球", "global", "worldwide"),
        "china": ("中国", "国内", "china"),
        "enterprise": ("企业", "enterprise"),
        "consumer": ("消费级", "consumer"),
        "device": ("单设备", "per-device", "device"),
        "system": ("系统总计", "system total", "cluster"),
    }.items():
        if any(term in value for term in terms):
            scopes.append(name)
    return tuple(scopes)


@dataclass(frozen=True)
class RewriteAuthority:
    """Deterministic entity-binding authority for follow-up rewrites.

    Raw assistant history is retained only as an explicit untrusted diagnostic
    surface. It can never authorize an entity introduced by a rewrite.
    """
    current_query_entities: Tuple[str, ...] = field(default_factory=tuple)
    prior_user_entities: Tuple[str, ...] = field(default_factory=tuple)
    latest_relevant_user_entities: Tuple[str, ...] = field(default_factory=tuple)
    verified_premise_entities: Tuple[str, ...] = field(default_factory=tuple)
    unverified_assistant_entities: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed_context_entities(self) -> Tuple[str, ...]:
        """The single deterministic contextual binding, or no authority.

        The most recent relevant USER turn has precedence.  It authorizes a
        rewrite only when it names exactly one entity.  Server-verified
        premises are considered only when that USER turn names none, and
        likewise must resolve to exactly one entity.  Multiple candidates are
        ambiguity, never a license for a model to choose one silently.
        """
        if len(self.latest_relevant_user_entities) == 1:
            return self.latest_relevant_user_entities
        if self.latest_relevant_user_entities:
            return ()
        if len(self.verified_premise_entities) == 1:
            return self.verified_premise_entities
        return ()

    @property
    def binding_status(self) -> str:
        if len(self.latest_relevant_user_entities) == 1:
            return "RESOLVED_FROM_LATEST_USER"
        if len(self.latest_relevant_user_entities) > 1:
            return "AMBIGUOUS_LATEST_USER"
        if len(self.verified_premise_entities) == 1:
            return "RESOLVED_FROM_VERIFIED_PREMISE"
        if len(self.verified_premise_entities) > 1:
            return "AMBIGUOUS_VERIFIED_PREMISES"
        return "UNRESOLVED"

    def authorizes(self, entity: str) -> bool:
        key = _norm(entity).casefold()
        return any(key == _norm(v).casefold()
                   for v in self.allowed_context_entities)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self) | {
            "allowed_context_entities": list(self.allowed_context_entities),
            "binding_status": self.binding_status,
            "assistant_history_is_authority": False,
        }


def build_rewrite_authority(current_query: str, history: Optional[list] = None,
                            verified_premises: Optional[list] = None
                            ) -> RewriteAuthority:
    """Build authority from current/user context and server premises only."""
    user_entities: List[str] = []
    latest_user_entities: Tuple[str, ...] = ()
    assistant_entities: List[str] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        entities = _extract_entities(str(message.get("content") or ""))
        if str(message.get("role") or "").lower() == "user":
            user_entities.extend(entities)
        elif str(message.get("role") or "").lower() == "assistant":
            assistant_entities.extend(entities)
    for message in reversed(history or []):
        if not isinstance(message, dict) \
                or str(message.get("role") or "").lower() != "user":
            continue
        entities = _extract_entities(str(message.get("content") or ""))
        if entities:
            latest_user_entities = entities
            break
    premise_entities: List[str] = []
    for premise in verified_premises or []:
        if hasattr(premise, "claim"):
            text = premise.claim
        elif isinstance(premise, dict):
            text = premise.get("claim") or premise.get("claim_text") or ""
        else:
            text = ""
        premise_entities.extend(_extract_entities(str(text)))
    return RewriteAuthority(
        current_query_entities=_extract_entities(current_query),
        prior_user_entities=_ordered_unique(user_entities),
        latest_relevant_user_entities=latest_user_entities,
        verified_premise_entities=_ordered_unique(premise_entities),
        unverified_assistant_entities=_ordered_unique(assistant_entities),
    )


@dataclass(frozen=True)
class SemanticDiff:
    entities_original: Tuple[str, ...] = field(default_factory=tuple)
    entities_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    entities_added: Tuple[str, ...] = field(default_factory=tuple)
    entities_removed: Tuple[str, ...] = field(default_factory=tuple)
    time_original: Tuple[str, ...] = field(default_factory=tuple)
    time_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    negation_original: Tuple[str, ...] = field(default_factory=tuple)
    negation_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    modality_original: Tuple[str, ...] = field(default_factory=tuple)
    modality_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    numeric_original: Tuple[str, ...] = field(default_factory=tuple)
    numeric_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    comparison_original: Tuple[str, ...] = field(default_factory=tuple)
    comparison_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    dimensions_original: Tuple[str, ...] = field(default_factory=tuple)
    dimensions_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    scope_original: Tuple[str, ...] = field(default_factory=tuple)
    scope_rewritten: Tuple[str, ...] = field(default_factory=tuple)
    intent_original: str = "fact"
    intent_rewritten: str = "fact"
    critical_changes: Tuple[str, ...] = field(default_factory=tuple)
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    uncertain: bool = False

    @property
    def risk_level(self) -> str:
        if self.critical_changes or self.uncertain:
            return "high"
        if (self.entities_added or self.dimensions_rewritten != self.dimensions_original
                or self.scope_rewritten != self.scope_original):
            return "low"
        return "none"

    def to_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out.update({
            "time_changed": self.time_original != self.time_rewritten,
            "negation_changed": self.negation_original != self.negation_rewritten,
            "modality_changed": self.modality_original != self.modality_rewritten,
            "numeric_changed": self.numeric_original != self.numeric_rewritten,
            "comparison_objects_changed": self.comparison_original != self.comparison_rewritten,
            "scope_changed": ("unchanged" if self.scope_original == self.scope_rewritten else "changed"),
            "risk_level": self.risk_level,
        })
        return out


@dataclass(frozen=True)
class RewriteResult:
    original_query: str
    proposed_query: str
    rewritten_query: str
    semantic_diff: SemanticDiff
    accepted: bool
    action: str
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    seeking_novelty: bool = False

    def to_dict(self) -> dict:
        return {
            "original_query": self.original_query,
            "proposed_query": self.proposed_query,
            "rewritten_query": self.rewritten_query,
            "semantic_diff": self.semantic_diff.to_dict(),
            "accepted": self.accepted,
            "action": self.action,
            "diagnostics": list(self.diagnostics),
            "seeking_novelty": self.seeking_novelty,
        }


def semantic_diff(original: str, rewritten: str, *,
                  rewrite_authority: Optional[RewriteAuthority] = None
                  ) -> SemanticDiff:
    original, rewritten = _norm(original), _norm(rewritten)
    oe, re_ = _extract_entities(original), _extract_entities(rewritten)
    ot, rt = _extract_time(original), _extract_time(rewritten)
    on, rn = _extract_negation(original), _extract_negation(rewritten)
    om, rm = _extract_modality(original), _extract_modality(rewritten)
    oq, rq = _extract_numbers(original), _extract_numbers(rewritten)
    oc, rc = _extract_comparison_set(original), _extract_comparison_set(rewritten)
    od, rd = _extract_dimensions(original), _extract_dimensions(rewritten)
    os, rs = _scope(original), _scope(rewritten)
    oi, ri = _intent(original), _intent(rewritten)
    oec, rec = ({x.casefold(): x for x in oe}, {x.casefold(): x for x in re_})
    raw_added = tuple(rec[k] for k in rec.keys() - oec.keys())
    raw_removed = tuple(oec[k] for k in oec.keys() - rec.keys())
    added = tuple(x for x in raw_added if not any(
        x.casefold() in y.casefold() or y.casefold() in x.casefold()
        for y in raw_removed))
    removed = tuple(x for x in raw_removed if not any(
        x.casefold() in y.casefold() or y.casefold() in x.casefold()
        for y in raw_added))
    changes = []
    if removed: changes.append("entity_removed")
    if added:
        contextual = bool(_CONTEXT_REFERENCE.search(original))
        unauthorized = [e for e in added if not (
            not oe and contextual and rewrite_authority is not None
            and rewrite_authority.authorizes(e))]
        if unauthorized:
            changes.append("entity_added_without_authority")
    if ot != rt: changes.append("temporal_drift")
    if on != rn: changes.append("negation_drift")
    if om != rm: changes.append("modality_drift")
    if oq != rq: changes.append("numeric_drift")
    if oc != rc: changes.append("comparison_set_drift")
    if od != rd and od: changes.append("dimension_drift")
    if os != rs: changes.append("scope_drift")
    if oi != ri: changes.append("intent_drift")
    diagnostics, uncertain = [], False
    if original != rewritten and not oe and not re_ and len(original) > 2:
        if difflib.SequenceMatcher(None, original.casefold(),
                                   rewritten.casefold()).ratio() < 0.45:
            uncertain = True
            diagnostics.append("critical_subject_parse_uncertain")
    if added and rewrite_authority is not None:
        if rewrite_authority.binding_status.startswith("AMBIGUOUS_"):
            diagnostics.append("context_entity_binding_ambiguous")
        untrusted = {v.casefold() for v in
                     rewrite_authority.unverified_assistant_entities}
        if any(e.casefold() in untrusted and not rewrite_authority.authorizes(e)
               for e in added):
            diagnostics.append("entity_only_in_unverified_assistant_history")
    return SemanticDiff(
        entities_original=oe, entities_rewritten=re_, entities_added=added,
        entities_removed=removed, time_original=ot, time_rewritten=rt,
        negation_original=on, negation_rewritten=rn,
        modality_original=om, modality_rewritten=rm,
        numeric_original=oq, numeric_rewritten=rq,
        comparison_original=oc, comparison_rewritten=rc,
        dimensions_original=od, dimensions_rewritten=rd,
        scope_original=os, scope_rewritten=rs,
        intent_original=oi, intent_rewritten=ri,
        critical_changes=tuple(changes), diagnostics=tuple(diagnostics),
        uncertain=uncertain)


def build_rewrite_result(original: str, proposed: str, *,
                         seeking_novelty: bool = False,
                         model_diagnostics: Optional[dict] = None,
                         rewrite_authority: Optional[RewriteAuthority] = None
                         ) -> RewriteResult:
    original, proposed = _norm(original), _norm(proposed) or _norm(original)
    diff = semantic_diff(original, proposed,
                         rewrite_authority=rewrite_authority)
    diagnostics = list(diff.diagnostics)
    if model_diagnostics is not None:
        diagnostics.append("model_diff_advisory_only")
    if diff.critical_changes:
        diagnostics.extend(diff.critical_changes)
        return RewriteResult(original, proposed, original, diff, False,
                             "REJECT_TO_ORIGINAL", tuple(diagnostics),
                             seeking_novelty)
    if diff.uncertain:
        return RewriteResult(original, proposed, original, diff, False,
                             "ESCALATE_AMBIGUITY", tuple(diagnostics),
                             seeking_novelty)
    return RewriteResult(original, proposed, proposed, diff, True, "ACCEPT",
                         tuple(diagnostics), seeking_novelty)


def compute_semantic_diff(original: str, rewritten: str) -> dict:
    return semantic_diff(original, rewritten).to_dict()


def should_revert_rewrite(diff) -> bool:
    return (diff.risk_level if isinstance(diff, SemanticDiff)
            else (diff or {}).get("risk_level")) == "high"


def filter_verified_premises(history: list,
                             verified_status: Optional[Dict[int, str]] = None) -> list:
    """Legacy adapter, now fail-closed: client messages cannot self-authorize."""
    if not verified_status:
        return []
    return [msg for i, msg in enumerate(history or [])
            if str(verified_status.get(i, "")).upper() in
            ("SUPPORTED", "VERIFIED", "FINAL")]


def build_conversation_context(original_query: str, rewritten_query: str,
                               history: list,
                               verified_status: Optional[Dict[int, str]] = None) -> dict:
    result = build_rewrite_result(original_query, rewritten_query)
    return {
        "original_query": result.original_query,
        "rewritten_query": result.rewritten_query,
        "semantic_diff": result.semantic_diff.to_dict(),
        "verified_history": filter_verified_premises(history, verified_status),
        "use_original": not result.accepted,
        "rewrite_result": result.to_dict(),
    }
