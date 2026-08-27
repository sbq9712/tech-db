"""
Entity Resolution V2 (ER-001..ER-124)
=======================================
Comprehensive entity resolution pipeline with:
  - Stable opaque IDs (ER-001)
  - Multi-strategy matching: exact → normalized → fuzzy → LLM (ER-010..ER-030)
  - Ambiguity detection and disambiguation (ER-040..ER-060)
  - Cross-document coreference (ER-070..ER-080)
  - Entity linking with confidence scoring (ER-090)
  - Batch entity resolution for full corpus (ER-100..ER-120)
  - Registry persistence and versioning (ER-124)

Architecture:
  1. MentionExtractor: Extract entity mentions from text
  2. EntityLinker: Link mentions to canonical entities
  3. Disambiguator: Resolve ambiguous mentions
  4. EntityRegistryV2: Persistent registry with versioning
  
  All IDs are opaque: "org:a1b2c3" not "org:NVIDIA"
  Mention ≠ Entity: "英伟达" and "NVIDIA" are different mentions of same entity
"""
import os
import re
import json
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
# Q7: runtime/indexes is the single authoritative registry location.
# (Previously data/lightrag — the split path was the root cause of the
# V1/V2 format confusion. Delegates to registry_io for the env overrides.)
import os as _os
import registry_io as _rio
REGISTRY_PATH = _rio.registry_path()


class EntityType(str, Enum):
    ORG = "ORG"
    PERSON = "PERSON"
    PRODUCT = "PRODUCT"
    TECH = "TECH"
    MATERIAL = "MATERIAL"
    GEO = "GEO"
    EVENT = "EVENT"
    CONCEPT = "CONCEPT"


class LinkStatus(str, Enum):
    LINKED = "LINKED"          # High-confidence link to canonical entity
    AMBIGUOUS = "AMBIGUOUS"    # Multiple candidates, needs disambiguation
    NEW = "NEW"                # No match found, potential new entity
    LOW_CONFIDENCE = "LOW_CONFIDENCE"  # Weak match, needs review
    BLOCKED = "BLOCKED"          # Legacy facade mapping of canonical BLOCKED


@dataclass
class EntityMention:
    """A mention of an entity in text."""
    text: str
    start_offset: int
    end_offset: int
    context: str = ""  # Surrounding text
    source: str = ""   # Where mention was found (title/body/kp)
    mention_type: str = ""  # Detected type hint


@dataclass
class EntityLinkResult:
    """Result of linking a mention to an entity."""
    mention: str
    entity_id: Optional[str]
    canonical_name: Optional[str]
    entity_type: Optional[str]
    status: LinkStatus
    confidence: float
    candidates: List[Tuple[str, float]] = field(default_factory=list)  # (entity_id, score)
    method: str = ""  # exact/normalized/fuzzy/llm


@dataclass
class CanonicalEntity:
    """A canonical entity in the registry."""
    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: List[str] = field(default_factory=list)
    abbreviations: List[str] = field(default_factory=list)
    description: str = ""
    wikipedia_url: str = ""
    confidence: float = 1.0
    provenance: str = "manual"  # manual/llm/derived
    mention_count: int = 0
    document_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    ambiguous_with: List[str] = field(default_factory=list)


class EntityRegistryV2:
    """Legacy JSON compatibility registry; never canonical mutable authority.
    
    Key properties:
      - IDs are opaque: "org:a1b2c3d4" not "org:NVIDIA"
      - Mention ≠ Entity: multiple mentions can link to same entity
      - Versioned: track changes over time
      - Persistent: saved to JSON
    """

    SCHEMA_VERSION = "2.0"

    def __init__(self, registry_path: Path = None):
        self.registry_path = registry_path or _rio.registry_path()
        self.entities: Dict[str, CanonicalEntity] = {}
        self.alias_index: Dict[str, str] = {}  # normalized alias → entity_id
        self.ambiguous_aliases: Set[str] = set()
        self._next_seq = 0
        self.load()

    def _generate_id(self, entity_type: str, canonical_name: str) -> str:
        """Generate a new opaque ID without mutable name/type input.

        JSON is retained only as migration/compatibility input. New production
        identity creation uses :class:`identity_store.IdentityStore`.
        """
        from entity_resolution_types import new_opaque_id
        return new_opaque_id("ent")

    def load(self):
        """Load registry from disk (via registry_io — single reader, Q6/R12).

        registry_io normalizes any historical shape (V1 dict / V2 list /
        bare list) to the canonical entity shape and strips unknown fields,
        so CanonicalEntity(**...) construction can't crash on legacy files.
        V1-dict migrations keep their original IDs (stable across reloads,
        avoids hash-ID collisions for same-name entities).
        """
        import registry_io
        data = registry_io.read_registry(self.registry_path)
        if data["source_version"] in ("empty", "corrupt"):
            if data["source_version"] == "corrupt":
                logger.warning("Entity registry corrupt/unreadable — starting empty")
            return
        for ent_data in data["entities"]:
            try:
                ent = CanonicalEntity(**ent_data)
            except TypeError:
                logger.warning(f"Skipping malformed entity entry: {ent_data!r:.120}")
                continue
            self.entities[ent.entity_id] = ent
            self._index_entity(ent)
        logger.info(f"Loaded {len(self.entities)} entities from {self.registry_path}")

    def save(self):
        """Save registry to disk (via registry_io — single writer, Q6/R12)."""
        import registry_io
        registry_io.write_registry(
            self.registry_path,
            [asdict(e) for e in self.entities.values()],
            self.alias_index,
            self.ambiguous_aliases,
        )
        logger.info(f"Saved {len(self.entities)} entities to {self.registry_path}")

    def _normalize(self, text: str) -> str:
        """Normalize text for matching."""
        import unicodedata
        text = unicodedata.normalize("NFKC", text)
        return text.lower().strip()

    def _index_entity(self, entity: CanonicalEntity):
        """Add entity aliases to the lookup index."""
        # Canonical name
        norm = self._normalize(entity.canonical_name)
        if norm in self.alias_index and self.alias_index[norm] != entity.entity_id:
            self.ambiguous_aliases.add(norm)
        else:
            self.alias_index[norm] = entity.entity_id

        # Aliases
        for alias in entity.aliases:
            norm = self._normalize(alias)
            if norm in self.alias_index and self.alias_index[norm] != entity.entity_id:
                self.ambiguous_aliases.add(norm)
            else:
                self.alias_index[norm] = entity.entity_id

        # Abbreviations
        for abbr in entity.abbreviations:
            norm = self._normalize(abbr)
            if norm in self.alias_index and self.alias_index[norm] != entity.entity_id:
                self.ambiguous_aliases.add(norm)
            else:
                self.alias_index[norm] = entity.entity_id

    def add_entity(
        self,
        canonical_name: str,
        entity_type: str = "ORG",
        aliases: List[str] = None,
        abbreviations: List[str] = None,
        description: str = "",
        provenance: str = "manual",
        confidence: float = 1.0,
    ) -> str:
        """Add a legacy compatibility entity. Returns its existing/new ID.

        Reopening a migrated JSON registry reuses the already persisted row;
        the name is a lookup attribute here, never input to ID generation.
        """
        normalized_name = self._normalize(canonical_name)
        existing = next((e for e in self.entities.values()
                         if self._normalize(e.canonical_name) == normalized_name
                         and e.entity_type == entity_type), None)
        entity_id = (existing.entity_id if existing is not None
                     else self._generate_id(entity_type, canonical_name))

        if entity_id in self.entities:
            # Update existing
            ent = self.entities[entity_id]
            if aliases:
                for a in aliases:
                    if a not in ent.aliases:
                        ent.aliases.append(a)
            if abbreviations:
                for a in abbreviations:
                    if a not in ent.abbreviations:
                        ent.abbreviations.append(a)
            if description:
                ent.description = description
        else:
            ent = CanonicalEntity(
                entity_id=entity_id,
                canonical_name=canonical_name,
                entity_type=entity_type,
                aliases=aliases or [],
                abbreviations=abbreviations or [],
                description=description,
                provenance=provenance,
                confidence=confidence,
            )
            self.entities[entity_id] = ent

        self._index_entity(ent)
        return entity_id

    def get_entity(self, entity_id: str) -> Optional[CanonicalEntity]:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    def all_entities(self) -> List[CanonicalEntity]:
        """Return all entities."""
        return list(self.entities.values())

    def stats(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "total_entities": len(self.entities),
            "total_aliases": len(self.alias_index),
            "ambiguous_aliases": len(self.ambiguous_aliases),
            "by_type": self._type_stats(),
        }

    def _type_stats(self) -> dict:
        counts = {}
        for ent in self.entities.values():
            counts[ent.entity_type] = counts.get(ent.entity_type, 0) + 1
        return counts


class MentionExtractor:
    """Extracts entity mentions from text.
    
    Uses multiple strategies:
      1. Pattern-based: Chinese entity-like tokens, English capitalized words
      2. Dictionary-based: Match against known entity aliases
      3. Regex-based: Numbers with units, dates, etc.
    """

    # Common Chinese entity patterns
    CN_PATTERNS = [
        # Company names
        re.compile(r'[一-鿿]+(?:科技|技术|能源|动力|电池|材料|化学|半导体|芯片|生物|医药|集团|公司|股份|有限)'),
        # Technology terms
        re.compile(r'[一-鿿]+(?:电池|太阳能|储能|制氢|电解|催化|芯片|架构|工艺|材料|技术|系统|平台)'),
        # Material terms
        re.compile(r'[一-鿿]+(?:钙钛矿|硅片|晶硅|负极|正极|电解质|隔膜|极片|浆料|粉末|纤维)'),
    ]

    # English patterns
    EN_PATTERNS = [
        # All-caps acronyms (3+ chars)
        re.compile(r'\b[A-Z]{3,}\b'),
        # Capitalized words
        re.compile(r'\b[A-Z][a-zA-Z]+\b'),
        # Capitalized word sequences
        re.compile(r'\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+\b'),
    ]

    def __init__(self, entity_registry: EntityRegistryV2 = None):
        self.registry = entity_registry

    def extract(self, text: str, source: str = "") -> List[EntityMention]:
        """Extract all entity mentions from text."""
        if not text:
            return []

        mentions = []
        seen_spans = set()

        # Strategy 1: Dictionary-based (highest precision)
        if self.registry:
            for alias, entity_id in self.registry.alias_index.items():
                if len(alias) < 2:
                    continue
                # Search for alias in text
                start = 0
                text_lower = text.lower()
                alias_lower = alias.lower()
                while True:
                    idx = text_lower.find(alias_lower, start)
                    if idx < 0:
                        break
                    # Avoid overlapping matches
                    span = (idx, idx + len(alias))
                    if not any(s[0] <= idx < s[1] or s[0] < span[1] <= s[1] for s in seen_spans):
                        seen_spans.add(span)
                        mentions.append(EntityMention(
                            text=text[idx:idx + len(alias)],
                            start_offset=idx,
                            end_offset=idx + len(alias),
                            context=text[max(0, idx-20):idx+len(alias)+20],
                            source=source,
                        ))
                    start = idx + 1

        # Strategy 2: Pattern-based
        for pattern in self.CN_PATTERNS + self.EN_PATTERNS:
            for m in pattern.finditer(text):
                span = (m.start(), m.end())
                # Check overlap
                if not any(s[0] <= m.start() < s[1] or s[0] < m.end() <= s[1] for s in seen_spans):
                    seen_spans.add(span)
                    mentions.append(EntityMention(
                        text=m.group(),
                        start_offset=m.start(),
                        end_offset=m.end(),
                        context=text[max(0, m.start()-20):m.end()+20],
                        source=source,
                    ))

        return mentions


class EntityLinker:
    """Links entity mentions to canonical entities.
    
    Multi-strategy linking:
      1. Exact match (confidence: 1.0)
      2. Normalized match — case/unicode insensitive (confidence: 0.95)
      3. Fuzzy match — edit distance (confidence: 0.7-0.9)
      4. LLM-assisted match (confidence: 0.6-0.8)
    """

    def __init__(self, registry: EntityRegistryV2, use_llm: bool = True):
        self.registry = registry
        self.use_llm = use_llm and bool(os.environ.get("ZAI_API_KEY", ""))

    def link(self, mention: EntityMention) -> EntityLinkResult:
        """Link a single mention to a canonical entity."""
        text = mention.text

        # Strategy 1: Exact match
        result = self._exact_match(text)
        if result and result.confidence >= 0.95:
            return result

        # Strategy 2: Normalized match
        result = self._normalized_match(text)
        if result and result.confidence >= 0.9:
            return result

        # Strategy 3: Fuzzy match
        result = self._fuzzy_match(text)
        if result and result.confidence >= 0.8:
            return result

        # Strategy 4: LLM-assisted
        if self.use_llm:
            result = self._llm_match(text, mention.context)
            if result and result.confidence >= 0.7:
                return result

        # No match found
        return EntityLinkResult(
            mention=text,
            entity_id=None,
            canonical_name=None,
            entity_type=None,
            status=LinkStatus.NEW,
            confidence=0.0,
            method="no_match",
        )

    def link_batch(self, mentions: List[EntityMention]) -> List[EntityLinkResult]:
        """Link multiple mentions."""
        return [self.link(m) for m in mentions]

    def _exact_match(self, text: str) -> Optional[EntityLinkResult]:
        """Try exact string match against aliases."""
        if text in self.registry.alias_index and text not in self.registry.ambiguous_aliases:
            entity_id = self.registry.alias_index[text]
            entity = self.registry.get_entity(entity_id)
            if entity:
                return EntityLinkResult(
                    mention=text,
                    entity_id=entity_id,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    status=LinkStatus.LINKED,
                    confidence=1.0,
                    method="exact",
                )

        # Check ambiguous
        if text in self.registry.ambiguous_aliases:
            candidates = [
                (eid, 1.0) for alias, eid in self.registry.alias_index.items()
                if alias == text
            ]
            return EntityLinkResult(
                mention=text,
                entity_id=None,
                canonical_name=None,
                entity_type=None,
                status=LinkStatus.AMBIGUOUS,
                confidence=0.5,
                candidates=candidates[:5],
                method="exact_ambiguous",
            )

        return None

    def _normalized_match(self, text: str) -> Optional[EntityLinkResult]:
        """Try normalized match (case-insensitive, unicode-normalized)."""
        import unicodedata
        normalized = unicodedata.normalize("NFKC", text).lower().strip()

        if normalized in self.registry.alias_index:
            if normalized in self.registry.ambiguous_aliases:
                # Multiple entities match
                candidates = [
                    (eid, 0.9) for alias, eid in self.registry.alias_index.items()
                    if eid and self._normalize(alias) == normalized
                ]
                # Deduplicate
                seen = set()
                unique_candidates = []
                for eid, score in candidates:
                    if eid not in seen:
                        seen.add(eid)
                        unique_candidates.append((eid, score))
                return EntityLinkResult(
                    mention=text,
                    entity_id=None,
                    canonical_name=None,
                    entity_type=None,
                    status=LinkStatus.AMBIGUOUS,
                    confidence=0.5,
                    candidates=unique_candidates[:5],
                    method="normalized_ambiguous",
                )

            entity_id = self.registry.alias_index[normalized]
            entity = self.registry.get_entity(entity_id)
            if entity:
                return EntityLinkResult(
                    mention=text,
                    entity_id=entity_id,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    status=LinkStatus.LINKED,
                    confidence=0.95,
                    method="normalized",
                )

        return None

    def _normalize(self, text: str) -> str:
        import unicodedata
        return unicodedata.normalize("NFKC", text).lower().strip()

    def _fuzzy_match(self, text: str) -> Optional[EntityLinkResult]:
        """Try fuzzy match using edit distance."""
        normalized = self._normalize(text)
        if not normalized or len(normalized) < 2:
            return None

        best_match = None
        best_score = 0.0
        candidates = []

        for alias, entity_id in self.registry.alias_index.items():
            score = self._similarity(normalized, alias)
            if score > 0.8:
                candidates.append((entity_id, score))
                if score > best_score:
                    best_score = score
                    best_match = entity_id

        if best_match and best_score >= 0.85:
            entity = self.registry.get_entity(best_match)
            if entity:
                return EntityLinkResult(
                    mention=text,
                    entity_id=best_match,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    status=LinkStatus.LINKED if best_score >= 0.9 else LinkStatus.LOW_CONFIDENCE,
                    confidence=best_score,
                    candidates=sorted(candidates, key=lambda x: -x[1])[:5],
                    method="fuzzy",
                )

        return None

    def _similarity(self, a: str, b: str) -> float:
        """Compute similarity between two strings."""
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        # Use Levenshtein distance ratio
        max_len = max(len(a), len(b))
        if max_len == 0:
            return 0.0

        distance = self._levenshtein(a, b)
        return 1.0 - (distance / max_len)

    def _levenshtein(self, a: str, b: str) -> int:
        """Compute Levenshtein edit distance."""
        if not a:
            return len(b)
        if not b:
            return len(a)

        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i]
            for j, cb in enumerate(b, 1):
                curr.append(min(
                    prev[j] + 1,
                    curr[j-1] + 1,
                    prev[j-1] + (0 if ca == cb else 1),
                ))
            prev = curr
        return prev[-1]

    def _llm_match(self, text: str, context: str = "") -> Optional[EntityLinkResult]:
        """Use LLM for entity matching when deterministic methods fail."""
        # Get candidate entities for LLM to choose from
        candidates = list(self.registry.entities.values())[:50]  # Limit for prompt

        if not candidates:
            return None

        candidate_strs = [f"- {e.canonical_name} ({e.entity_type}): {', '.join(e.aliases[:3])}"
                         for e in candidates[:20]]

        prompt = f"""判断以下提及是否对应某个已知实体。

提及: "{text}"
上下文: "{context[:100]}"

已知实体:
{chr(10).join(candidate_strs)}

如果匹配，返回JSON: {{"entity_id": "xxx", "confidence": 0.0-1.0, "reason": "..."}}
如果不匹配，返回: {{"entity_id": null, "confidence": 0.0, "reason": "no match"}}
"""

        try:
            import urllib.request
            api_key = os.environ.get("ZAI_API_KEY", "")
            base_url = os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")

            data = json.dumps({
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 100,
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            content = result["choices"][0]["message"]["content"]

            # Parse JSON
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                cleaned = re.sub(r'```json\s*', '', content)
                cleaned = re.sub(r'```\s*$', '', cleaned.strip())
                m = re.search(r'\{[^}]+\}', cleaned)
                if m:
                    parsed = json.loads(m.group(0))
                else:
                    return None

            entity_id = parsed.get("entity_id")
            confidence = float(parsed.get("confidence", 0.0))

            if entity_id and confidence >= 0.7:
                entity = self.registry.get_entity(entity_id)
                if entity:
                    return EntityLinkResult(
                        mention=text,
                        entity_id=entity_id,
                        canonical_name=entity.canonical_name,
                        entity_type=entity.entity_type,
                        status=LinkStatus.LINKED,
                        confidence=confidence,
                        method="llm",
                    )

        except Exception as e:
            logger.debug(f"LLM match failed: {e}")

        return None


class Disambiguator:
    """Resolves ambiguous entity mentions using context.
    
    Strategies:
      1. Context-based: Use surrounding text to determine correct entity
      2. Type-based: Use mention_type hint
      3. Frequency-based: Most commonly linked entity wins
      4. LLM-based: Ask LLM to disambiguate
    """

    def __init__(self, registry: EntityRegistryV2):
        self.registry = registry

    def disambiguate(
        self,
        mention: str,
        candidates: List[Tuple[str, float]],
        context: str = "",
    ) -> Optional[EntityLinkResult]:
        """Disambiguate an ambiguous mention.
        
        Args:
            mention: The ambiguous mention text
            candidates: List of (entity_id, score) candidates
            context: Surrounding text
            
        Returns:
            Best match or None if cannot disambiguate
        """
        if not candidates:
            return None

        if len(candidates) == 1:
            entity_id, score = candidates[0]
            entity = self.registry.get_entity(entity_id)
            if entity:
                return EntityLinkResult(
                    mention=mention,
                    entity_id=entity_id,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    status=LinkStatus.LINKED,
                    confidence=score * 0.8,
                    method="disambiguate_single",
                )
            else:
                # Entity not in registry but single candidate — still link
                return EntityLinkResult(
                    mention=mention,
                    entity_id=entity_id,
                    canonical_name=entity_id,
                    entity_type="UNKNOWN",
                    status=LinkStatus.LINKED,
                    confidence=score * 0.7,
                    method="disambiguate_single",
                )

        # Strategy 1: Context-based
        scored = []
        for entity_id, base_score in candidates:
            entity = self.registry.get_entity(entity_id)
            if not entity:
                # Try to create a dummy entity for scoring (for test cases)
                entity = CanonicalEntity(
                    entity_id=entity_id,
                    canonical_name="",
                    entity_type="ORG",
                )
                # Give it minimal context score
                scored.append((entity_id, base_score * 0.5))
                continue

            context_score = self._context_similarity(entity, context)
            combined = base_score * 0.6 + context_score * 0.4
            scored.append((entity_id, combined))

        scored.sort(key=lambda x: -x[1])

        # If best is significantly better than second
        if len(scored) >= 2 and scored[0][1] - scored[1][1] > 0.15:
            entity_id, score = scored[0]
            entity = self.registry.get_entity(entity_id)
            if entity:
                return EntityLinkResult(
                    mention=mention,
                    entity_id=entity_id,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    status=LinkStatus.LINKED,
                    confidence=score,
                    method="disambiguate_context",
                )

        # Strategy 2: Frequency-based (only for entities that exist in registry)
        valid_scored = [(eid, score) for eid, score in scored if self.registry.get_entity(eid)]
        if valid_scored:
            most_frequent = max(valid_scored, key=lambda x: self.registry.get_entity(x[0]).mention_count)
            entity = self.registry.get_entity(most_frequent[0])
            if entity and entity.mention_count > 0:
                return EntityLinkResult(
                mention=mention,
                entity_id=most_frequent[0],
                canonical_name=entity.canonical_name,
                entity_type=entity.entity_type,
                status=LinkStatus.LINKED,
                confidence=most_frequent[1] * 0.7,
                method="disambiguate_frequency",
            )

        # Still ambiguous
        return EntityLinkResult(
            mention=mention,
            entity_id=None,
            canonical_name=None,
            entity_type=None,
            status=LinkStatus.AMBIGUOUS,
            confidence=0.3,
            candidates=scored[:5],
            method="unresolved",
        )

    def _context_similarity(self, entity: CanonicalEntity, context: str) -> float:
        """Compute how well the context matches the entity's domain."""
        if not context:
            return 0.5

        context_lower = context.lower()
        score = 0.0

        # Check if canonical name appears in context
        if entity.canonical_name.lower() in context_lower:
            score += 0.3

        # Check aliases
        for alias in entity.aliases:
            if alias.lower() in context_lower:
                score += 0.2
                break

        # Check description keywords
        if entity.description:
            keywords = entity.description.lower().split()
            for kw in keywords[:5]:
                if len(kw) > 2 and kw in context_lower:
                    score += 0.1

        return min(score, 1.0)


# ─── Batch Processing ───

class EntityResolutionPipeline:
    """Full pipeline for batch entity resolution.
    
    Usage:
        pipeline = EntityResolutionPipeline()
        results = pipeline.process_records(records[:100])
        pipeline.save()
    """

    def __init__(self, registry_path: Path = None, use_llm: bool = False):
        self.registry = EntityRegistryV2(registry_path)
        self.extractor = MentionExtractor(self.registry)
        self.linker = EntityLinker(self.registry, use_llm=use_llm)
        self.disambiguator = Disambiguator(self.registry)

    def process_text(self, text: str, source: str = "") -> List[EntityLinkResult]:
        """Process a single text, extracting and linking entities."""
        mentions = self.extractor.extract(text, source=source)
        results = []

        for mention in mentions:
            result = self.linker.link(mention)

            # Disambiguate if ambiguous
            if result.status == LinkStatus.AMBIGUOUS and result.candidates:
                disambiguated = self.disambiguator.disambiguate(
                    mention.text, result.candidates, mention.context
                )
                if disambiguated:
                    result = disambiguated

            results.append(result)

            # Update mention count
            if result.entity_id and result.status == LinkStatus.LINKED:
                entity = self.registry.get_entity(result.entity_id)
                if entity:
                    entity.mention_count += 1

        return results

    def process_record(self, record: dict, record_id: int = None) -> Dict:
        """Process a single record, extracting and linking entities."""
        title = record.get("t", "")
        body = record.get("fb", "") or record.get("b", "") or record.get("as", "")
        source = record.get("s", "")

        all_results = []

        # Process title (higher weight)
        title_results = self.process_text(title, source="title")
        all_results.extend(title_results)

        # Process body
        body_results = self.process_text(body[:5000], source="body")  # Limit body length
        all_results.extend(body_results)

        # Deduplicate by entity_id
        seen_entities = {}
        for r in all_results:
            if r.entity_id:
                if r.entity_id not in seen_entities or r.confidence > seen_entities[r.entity_id].confidence:
                    seen_entities[r.entity_id] = r

        return {
            "record_id": record_id,
            "linked_entities": [
                {
                    "entity_id": r.entity_id,
                    "canonical_name": r.canonical_name,
                    "entity_type": r.entity_type,
                    "confidence": r.confidence,
                    "method": r.method,
                    "mention": r.mention,
                }
                for r in seen_entities.values()
                if r.status == LinkStatus.LINKED
            ],
            "new_mentions": [
                {"mention": r.mention, "context": ""}
                for r in all_results
                if r.status == LinkStatus.NEW
            ],
            "ambiguous_mentions": [
                {"mention": r.mention, "candidates": r.candidates[:3]}
                for r in all_results
                if r.status == LinkStatus.AMBIGUOUS
            ],
        }

    def process_records(self, records: List[dict], save_interval: int = 1000) -> Dict:
        """Process multiple records in batch.
        
        Returns summary statistics.
        """
        total_linked = 0
        total_new = 0
        total_ambiguous = 0
        total_records = len(records)

        for i, record in enumerate(records):
            result = self.process_record(record, record_id=i)

            total_linked += len(result["linked_entities"])
            total_new += len(result["new_mentions"])
            total_ambiguous += len(result["ambiguous_mentions"])

            # Add new entities to registry
            for new_mention in result["new_mentions"]:
                mention_text = new_mention["mention"]
                if len(mention_text) >= 2:
                    self.registry.add_entity(
                        canonical_name=mention_text,
                        entity_type="ORG",  # Default type
                        provenance="auto_extracted",
                        confidence=0.5,
                    )

            # Save periodically
            if (i + 1) % save_interval == 0:
                self.registry.save()
                logger.info(f"Processed {i+1}/{total_records} records")

        self.registry.save()

        return {
            "total_records": total_records,
            "total_linked": total_linked,
            "total_new_entities": total_new,
            "total_ambiguous": total_ambiguous,
            "avg_entities_per_record": round(total_linked / max(total_records, 1), 2),
            "registry_stats": self.registry.stats(),
        }

    def save(self):
        """Save the registry."""
        self.registry.save()


# ─── Seeding ───

def build_seed_registry_v2(registry_path: Path = None) -> EntityRegistryV2:
    """Build a seed registry with common tech entities."""
    registry = EntityRegistryV2(registry_path)

    seeds = [
        # Organizations
        ("NVIDIA", "ORG", ["英伟达", "Nvidia Corporation"], ["NVDA"]),
        ("AMD", "ORG", ["Advanced Micro Devices", "超威半导体"], []),
        ("Intel", "ORG", ["英特尔", "Intel Corporation"], []),
        ("TSMC", "ORG", ["台积电", "台湾积体电路制造"], []),
        ("CATL", "ORG", ["宁德时代", "Contemporary Amperex Technology"], []),
        ("BYD", "ORG", ["比亚迪", "Build Your Dreams"], []),
        ("LG Energy Solution", "ORG", ["LG新能源", "LGES"], ["LGES"]),
        ("Panasonic", "ORG", ["松下", "Panasonic Holdings"], []),
        ("Samsung SDI", "ORG", ["三星SDI"], []),
        ("SK Innovation", "ORG", ["SK Innovation", "SK E&C"], []),
        ("Toyota", "ORG", ["丰田", "Toyota Motor"], []),
        ("Tesla", "ORG", ["特斯拉"], []),
        ("Qualcomm", "ORG", ["高通", "Qualcomm Incorporated"], []),
        ("Huawei", "ORG", ["华为", "华为技术"], []),
        # Technologies
        ("Solid-State Battery", "TECH", ["固态电池"], []),
        ("Perovskite Solar Cell", "TECH", ["钙钛矿太阳能电池", "PSC"], []),
        ("Lithium-Ion Battery", "TECH", ["锂离子电池", "锂电池", "Li-ion"], ["LIB"]),
        ("CO2 Electrolysis", "TECH", ["CO2电解", "二氧化碳电解还原"], []),
        ("Brain-Computer Interface", "TECH", ["脑机接口", "BCI"], ["BCI"]),
        # Materials
        ("Perovskite", "MATERIAL", ["钙钛矿"], []),
        ("Silicon", "MATERIAL", ["硅", "硅片"], ["Si"]),
        ("Lithium", "MATERIAL", ["锂", "锂金属"], ["Li"]),
        # Products
        ("Blackwell", "PRODUCT", ["Blackwell Architecture", "Blackwell GPU"], []),
    ]

    for canonical, etype, aliases, abbrs in seeds:
        registry.add_entity(
            canonical_name=canonical,
            entity_type=etype,
            aliases=aliases,
            abbreviations=abbrs,
            provenance="seed",
        )

    return registry


# ---------------------------------------------------------------------------
# Canonical Phase06 boundary. The classes above remain a migration-compatible
# V1/V2 JSON facade only; production control-plane writes go through
# IdentityStore and serving reads go through an immutable IdentitySnapshotView.
# ---------------------------------------------------------------------------
from difflib import SequenceMatcher
from entity_resolution_types import (
    Candidate, CandidateSet, Mention, ResolutionDecision, ResolutionPolicy,
    ResolutionState, normalize_strong_id, normalize_surface, new_opaque_id,
    stable_hash,
)
from identity_snapshot import IdentitySnapshotView

CANONICAL_RESOLVER_VERSION = "er-v2.0"


class CandidateGenerator:
    """Deterministic, stage-attributed candidate cascade over one snapshot."""
    def __init__(self, snapshot: IdentitySnapshotView,
                 policy: ResolutionPolicy | None = None):
        self.snapshot = snapshot
        self.policy = policy or ResolutionPolicy()

    @staticmethod
    def _type_compatible(entity: dict, required_type: str | None) -> bool:
        if not required_type:
            return True
        wanted = required_type.upper().replace("PRODUCT", "PRODUCT_MODEL")
        actual = str(entity.get("entity_type", "OTHER_DOMAIN")).upper()
        actual = actual.replace("PRODUCT", "PRODUCT_MODEL").replace("TECH", "TECHNOLOGY")
        return actual == wanted

    def generate(self, mention: str, *, required_type: str | None = None,
                 strong_ids: list[dict] | None = None, top_k: int | None = None) -> CandidateSet:
        norm = normalize_surface(mention)
        top_k = int(top_k or self.policy.top_k)
        entities = self.snapshot.entities
        collected: dict[str, dict] = {}

        def add(entity_id, stage, score, *, reason, alias=None,
                provenance=None, strong=None, language=None):
            entity = entities.get(entity_id)
            if not entity or entity.get("lifecycle") in {"REJECTED", "TOMBSTONED"}:
                return
            compatible = self._type_compatible(entity, required_type)
            adjusted = score if compatible else score * 0.25
            item = collected.setdefault(entity_id, {
                "score": adjusted, "stage": stage, "reasons": set(),
                "aliases": set(), "provenance": set(), "strong": set(),
                "language": set(), "compatible": compatible,
                "features": {},
            })
            if adjusted > item["score"]:
                item["score"], item["stage"] = adjusted, stage
            item["compatible"] = item["compatible"] or compatible
            item["reasons"].add(reason)
            if alias: item["aliases"].add(alias)
            if provenance: item["provenance"].add(provenance)
            if strong: item["strong"].add(strong)
            if language: item["language"].add(language)
            item["features"][stage] = max(item["features"].get(stage, 0), score)

        # Validated typed strong identifiers precede textual candidate stages.
        for supplied in strong_ids or []:
            try:
                kind = str(supplied["id_type"]).upper()
                value = normalize_strong_id(kind, supplied["value"])
            except (KeyError, ValueError):
                continue
            for known in self.snapshot.payload["strong_ids"]:
                if (known.get("status") == "ACTIVE" and known.get("id_type") == kind
                        and known.get("normalized_value") == value):
                    add(known["entity_id"], "strong_id", 1.0,
                        reason="ER_STRONG_ID_MATCH", strong=f"{kind}:{value}")

        aliases = [a for a in self.snapshot.payload["aliases"]
                   if a.get("status") == "ACTIVE"]
        for alias in aliases:
            alias_norm = alias.get("normalized_surface", "")
            alias_type = str(alias.get("alias_type", "ALIAS")).upper()
            if alias_norm == norm:
                stage = "acronym" if alias_type == "ACRONYM" else (
                    "transliteration" if alias_type == "TRANSLITERATION" else "exact_alias")
                add(alias["entity_id"], stage, 0.99,
                    reason=f"ER_{stage.upper()}_MATCH", alias=alias.get("surface"),
                    provenance=alias.get("provenance"), language=alias.get("language"))

        # Fuzzy/trigram recall. This adds candidates; policy decides linkage.
        if norm:
            for alias in aliases:
                alias_norm = alias.get("normalized_surface", "")
                if not alias_norm or alias_norm == norm:
                    continue
                ratio = SequenceMatcher(None, norm, alias_norm).ratio()
                grams_a = {norm[i:i+3] for i in range(max(1, len(norm)-2))}
                grams_b = {alias_norm[i:i+3] for i in range(max(1, len(alias_norm)-2))}
                trigram = len(grams_a & grams_b) / max(1, len(grams_a | grams_b))
                score = max(ratio, trigram)
                if score >= 0.55:
                    add(alias["entity_id"], "fuzzy_trigram", score,
                        reason="ER_FUZZY_RECALL", alias=alias.get("surface"),
                        provenance=alias.get("provenance"))

        ordered = sorted(collected.items(), key=lambda item: (-item[1]["score"], item[0]))[:top_k]
        result = []
        for rank, (entity_id, item) in enumerate(ordered, 1):
            result.append(Candidate(
                entity_id=entity_id, stage=item["stage"], rank=rank,
                score=round(item["score"], 6),
                feature_scores=dict(sorted(item["features"].items())),
                exact_aliases=tuple(sorted(item["aliases"])),
                strong_id_matches=tuple(sorted(item["strong"])),
                type_compatible=item["compatible"],
                alias_provenance=tuple(sorted(x for x in item["provenance"] if x)),
                language_evidence=tuple(sorted(x for x in item["language"] if x)),
                reason_codes=tuple(sorted(item["reasons"])),
            ))
        return CandidateSet(mention, norm, tuple(result), top_k,
                            "candidate-cascade-v1")


class ConstrainedLLMAdjudicator:
    """Schema-validates a model choice; never generates registry candidates."""
    SCHEMA_VERSION = "llm-adjudication-1.0"

    def __init__(self, model_version="unconfigured", prompt_version="er-adjudicate-v1"):
        self.model_version = model_version
        self.prompt_version = prompt_version
        self._cache = {}

    def cache_key(self, mention: str, context: str, candidates: CandidateSet,
                  snapshot_id: str, policy_version: str) -> str:
        return stable_hash({
            "mention": normalize_surface(mention), "context_hash": stable_hash(context),
            "candidate_ids": [c.entity_id for c in candidates.candidates],
            "identity_snapshot_id": snapshot_id, "model_version": self.model_version,
            "prompt_version": self.prompt_version, "schema_version": self.SCHEMA_VERSION,
            "policy_version": policy_version,
        })

    def validate_output(self, output, candidates: CandidateSet,
                        *, required_type: str | None = None,
                        entities: dict | None = None) -> tuple[ResolutionState, str | None, tuple[str, ...]]:
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                return ResolutionState.AMBIGUOUS, None, ("ER_LLM_MALFORMED",)
        if not isinstance(output, dict):
            return ResolutionState.AMBIGUOUS, None, ("ER_LLM_MALFORMED",)
        try:
            decision = ResolutionState(str(output.get("decision", "")))
        except ValueError:
            return ResolutionState.AMBIGUOUS, None, ("ER_LLM_INVALID_DECISION",)
        selected = output.get("entity_id")
        allowed = {c.entity_id for c in candidates.candidates}
        if decision == ResolutionState.LINK:
            if selected not in allowed:
                return ResolutionState.BLOCKED, None, ("ER_LLM_FABRICATED_ID",)
            entity = (entities or {}).get(selected, {})
            if required_type and not CandidateGenerator._type_compatible(entity, required_type):
                return ResolutionState.BLOCKED, None, ("ER_LLM_WRONG_TYPE",)
            return decision, selected, ("ER_LLM_CONSTRAINED_CHOICE",)
        if selected is not None:
            return ResolutionState.AMBIGUOUS, None, ("ER_LLM_EXTRANEOUS_ID",)
        return decision, None, (f"ER_LLM_{decision.value}",)

    async def adjudicate(self, adapter, *, mention: str, context: str,
                         candidates: CandidateSet, snapshot: IdentitySnapshotView,
                         policy: ResolutionPolicy, required_type: str | None = None,
                         execution_context=None):
        safe_candidates = [{"entity_id": c.entity_id, "stage": c.stage,
                            "features": c.feature_scores,
                            "entity_type": snapshot.entities[c.entity_id].get("entity_type")}
                           for c in candidates.candidates]
        request = {"mention": mention, "context": context[:512],
                   "required_type": required_type, "candidates": safe_candidates,
                   "allowed_decisions": [s.value for s in ResolutionState],
                   "schema_version": self.SCHEMA_VERSION}
        async def invoke():
            value = adapter(request)
            if hasattr(value, "__await__"):
                value = await value
            return value
        key = self.cache_key(mention, context, candidates, snapshot.snapshot_id,
                             policy.version)
        if key in self._cache:
            output = self._cache[key]
        else:
            try:
                if execution_context is not None:
                    output = await execution_context.run_stage(
                        "entity_adjudicator", invoke,
                        safe_fallback_available=True)
                else:
                    output = await invoke()
            except Exception as exc:
                # Request cancellation remains owned by the request context;
                # technical model failures can only yield no-link ambiguity.
                from runtime_safety import RequestCancelled, StageExecutionError
                if isinstance(exc, RequestCancelled):
                    raise
                if isinstance(exc, StageExecutionError):
                    return (ResolutionState.AMBIGUOUS, None,
                            ("ER_LLM_RUNTIME_FAILURE",))
                raise
            self._cache[key] = output
        return self.validate_output(output, candidates, required_type=required_type,
                                    entities=snapshot.entities)


class CanonicalEntityResolver:
    """Canonical resolution boundary over one pinned immutable snapshot."""
    def __init__(self, snapshot: IdentitySnapshotView,
                 policy: ResolutionPolicy | None = None):
        self.snapshot = snapshot
        self.policy = policy or ResolutionPolicy()
        self.generator = CandidateGenerator(snapshot, self.policy)

    def _matching_rules(self, norm: str, strong_ids: list[dict]) -> list[dict]:
        strong_keys = set()
        for item in strong_ids:
            try:
                kind = str(item["id_type"]).upper()
                strong_keys.add(f"{kind}:{normalize_strong_id(kind, item['value'])}")
            except (KeyError, ValueError):
                continue
        matches = []
        for rule in self.snapshot.payload.get("rules", []):
            cond = rule.get("condition", {})
            if (normalize_surface(cond.get("mention", "")) == norm
                    or cond.get("strong_id") in strong_keys):
                matches.append(rule)
        return matches

    def resolve(self, mention: str, *, context: str = "",
                required_type: str | None = None,
                strong_ids: list[dict] | None = None) -> ResolutionDecision:
        strong_ids = strong_ids or []
        norm = normalize_surface(mention)
        rules = self._matching_rules(norm, strong_ids)
        blocks = [r for r in rules if r.get("rule_type") == "BLOCK"
                  and r.get("effective_status") in {"ACTIVE", "STALE_REVIEW_REQUIRED"}]
        if blocks:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_ACTIVE_BLOCK",), findings=tuple(r["rule_id"] for r in blocks))
        stale = [r for r in rules if r.get("effective_status") == "STALE_REVIEW_REQUIRED"]
        if stale:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_STALE_REVIEW_REQUIRED",), findings=tuple(r["rule_id"] for r in stale))

        # Validate supplied authoritative identifiers and detect cross-owner conflict.
        owners, strong_findings, invalid = set(), [], []
        for supplied in strong_ids:
            try:
                kind = str(supplied["id_type"]).upper()
                normalized = normalize_strong_id(kind, supplied["value"])
            except (KeyError, ValueError) as exc:
                invalid.append(type(exc).__name__)
                continue
            key = f"{kind}:{normalized}"
            matches = [s for s in self.snapshot.payload["strong_ids"]
                       if s.get("status") == "ACTIVE" and s.get("id_type") == kind
                       and s.get("normalized_value") == normalized]
            owners.update(s["entity_id"] for s in matches)
            strong_findings.append(key + (":MATCH" if matches else ":UNMATCHED"))
        if len(owners) > 1:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_CONFLICTING_STRONG_IDS",), strong=tuple(strong_findings))
        if len(owners) == 1 and required_type:
            owner = self.snapshot.entities.get(next(iter(owners)))
            if owner is None or not CandidateGenerator._type_compatible(owner, required_type):
                return self._decision(ResolutionState.BLOCKED, mention,
                    reasons=("ER_STRONG_ID_TYPE_CONFLICT",),
                    strong=tuple(strong_findings),
                    diagnostics=("MANUAL_REVIEW_REQUIRED",))

        overrides = [r for r in rules if r.get("rule_type") == "OVERRIDE"
                     and r.get("effective_status") == "ACTIVE"]
        if len({r.get("target_entity_id") for r in overrides}) > 1:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_CONFLICTING_OVERRIDES",), findings=tuple(r["rule_id"] for r in overrides))
        if overrides:
            selected = overrides[-1].get("target_entity_id")
            if owners and selected not in owners:
                return self._decision(ResolutionState.BLOCKED, mention,
                    reasons=("ER_OVERRIDE_STRONG_ID_CONFLICT",),
                    findings=tuple(r["rule_id"] for r in overrides), strong=tuple(strong_findings))
            return self._decision(ResolutionState.LINK, mention, selected=selected,
                reasons=("ER_ACTIVE_OVERRIDE",), findings=tuple(r["rule_id"] for r in overrides),
                strong=tuple(strong_findings))
        if invalid and strong_ids:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_INVALID_STRONG_ID",), strong=tuple(strong_findings))

        # A syntactically valid but unknown authoritative identifier must not
        # fall through to alias/fuzzy matching and fabricate an identity.
        if strong_ids and not owners:
            return self._decision(ResolutionState.BLOCKED, mention,
                reasons=("ER_UNKNOWN_STRONG_ID",), strong=tuple(strong_findings),
                diagnostics=("MANUAL_REVIEW_REQUIRED",))

        candidates = self.generator.generate(mention, required_type=required_type,
                                             strong_ids=strong_ids)
        if len(owners) == 1:
            return self._decision(ResolutionState.LINK, mention, selected=next(iter(owners)),
                                  candidates=candidates.candidates,
                                  reasons=("ER_STRONG_ID_UNIQUE",), strong=tuple(strong_findings))
        exact = [c for c in candidates.candidates
                 if c.stage in {"exact_alias", "acronym", "transliteration"}
                 and c.type_compatible]
        if len(exact) == 1:
            entity = self.snapshot.entities[exact[0].entity_id]
            flags = ("PROVISIONAL_ENTITY",) if entity.get("lifecycle") == "PROVISIONAL" else ()
            return self._decision(ResolutionState.LINK, mention, selected=exact[0].entity_id,
                candidates=candidates.candidates, reasons=("ER_EXACT_UNIQUE",), diagnostics=flags)
        if len(exact) > 1:
            return self._decision(ResolutionState.AMBIGUOUS, mention,
                candidates=candidates.candidates, reasons=("ER_EXACT_ALIAS_MANY_TO_MANY",))
        compatible = [c for c in candidates.candidates if c.type_compatible]
        if compatible:
            first = compatible[0]
            second_score = compatible[1].score if len(compatible) > 1 else 0.0
            kind = (required_type or self.snapshot.entities[first.entity_id].get("entity_type")
                    or "OTHER_DOMAIN").upper().replace("PRODUCT", "PRODUCT_MODEL").replace("TECH", "TECHNOLOGY")
            link_min = self.policy.link_min.get(kind, self.policy.link_min["OTHER_DOMAIN"])
            margin = self.policy.min_margin.get(kind, self.policy.min_margin["OTHER_DOMAIN"])
            if first.score >= link_min and first.score - second_score >= margin:
                return self._decision(ResolutionState.LINK, mention, selected=first.entity_id,
                    candidates=candidates.candidates, reasons=("ER_CALIBRATED_FUZZY_LINK",))
            return self._decision(ResolutionState.AMBIGUOUS, mention,
                candidates=candidates.candidates, reasons=("ER_LOW_CONFIDENCE_OR_MARGIN",),
                diagnostics=("LOW_CONFIDENCE",))
        return self._decision(ResolutionState.NEW, mention,
            candidates=candidates.candidates, reasons=("ER_NO_CANDIDATE",),
            proposal={"canonical_name": mention, "entity_type": required_type or "OTHER_DOMAIN",
                      "lifecycle": "PROVISIONAL"})

    def _decision(self, state, mention, *, selected=None, candidates=(), reasons=(),
                  findings=(), strong=(), diagnostics=(), proposal=None):
        return ResolutionDecision(
            decision=state, mention=mention, normalized_mention=normalize_surface(mention),
            selected_entity_id=selected, candidates=tuple(candidates),
            reason_codes=tuple(reasons), strong_id_findings=tuple(strong),
            override_block_findings=tuple(findings), provisional_proposal=proposal,
            resolver_version=CANONICAL_RESOLVER_VERSION,
            identity_snapshot_id=self.snapshot.snapshot_id,
            diagnostic_flags=tuple(diagnostics))


class QueryEntityResolver:
    """Read-only query resolver: parsing/resolution never mutates IdentityStore."""
    def __init__(self, snapshot_payload: dict,
                 policy: ResolutionPolicy | None = None):
        self.snapshot = IdentitySnapshotView(snapshot_payload)
        self.resolver = CanonicalEntityResolver(self.snapshot, policy)

    def parse(self, query: str) -> list[dict]:
        """Parse mentions and only syntactically safe typed identifiers."""
        query = str(query or "")[:2000]
        found = []
        occupied = []

        def add_typed(match, id_type, value, required_type):
            prefix = query[max(0, match.start() - 32):match.start()]
            explicit = re.search(
                r"(?i)\b(PERSON|ORG|ORGANIZATION|COMPANY|PRODUCT_MODEL|PRODUCT|TECHNOLOGY)\s*[:=]?\s*$",
                prefix)
            if explicit:
                required_type = {
                    "ORGANIZATION": "ORG", "COMPANY": "ORG",
                    "PRODUCT": "PRODUCT_MODEL",
                }.get(explicit.group(1).upper(), explicit.group(1).upper())
            found.append({"mention": match.group(0),
                          "required_type": required_type,
                          "strong_ids": [{"id_type": id_type, "value": value}]})
            occupied.append(match.span())

        doi_pattern = re.compile(
            r"(?i)(?:https?://doi\.org/|doi:\s*)?(10\.[^\s,;]+)")
        for match in doi_pattern.finditer(query):
            raw = match.group(1).rstrip(".)]}>'\"")
            add_typed(match, "DOI", raw, "OTHER_DOMAIN")
        ticker_pattern = re.compile(
            r"(?<![A-Za-z0-9._-])([A-Z][A-Z0-9._-]{1,11}:[A-Z0-9._-]{0,20})(?![A-Za-z0-9._-])")
        for match in ticker_pattern.finditer(query):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            add_typed(match, "EXCHANGE_TICKER", match.group(1), "ORG")
        url_pattern = re.compile(
            r"(?i)\b(?:https?://)?(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:/[^\s]*)?")
        for match in url_pattern.finditer(query):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            raw = match.group(0).rstrip(".)]}>'\"")
            kind = "OFFICIAL_URL" if "/" in raw.removeprefix("https://").removeprefix("http://") else "OFFICIAL_DOMAIN"
            add_typed(match, kind, raw, "ORG")

        normalized_query = normalize_surface(query)
        for alias in self.snapshot.payload["aliases"]:
            surface = alias.get("surface", "")
            if surface and normalize_surface(surface) in normalized_query:
                found.append({"mention": surface, "required_type": None,
                              "strong_ids": []})
        # Title-case names remain ordinary mentions, never typed identities.
        for mention in re.findall(r"\b[A-Z][A-Za-z0-9.-]{2,}\b", query):
            found.append({"mention": mention, "required_type": None,
                          "strong_ids": []})
        unique = {}
        for item in found:
            key = (normalize_surface(item["mention"]), stable_hash(item["strong_ids"]))
            unique.setdefault(key, item)
        return list(unique.values())[:10]

    def resolve_query(self, query: str) -> list[ResolutionDecision]:
        return [self.resolver.resolve(item["mention"],
                    required_type=item["required_type"],
                    strong_ids=item["strong_ids"]) for item in self.parse(query)]


def resolve_query_from_runtime_snapshot(query: str, runtime_snapshot) -> dict:
    """Production seam: consume only the request-pinned immutable resource."""
    payload = runtime_snapshot.resources.get("identity_snapshot")
    resolver = QueryEntityResolver(payload)
    decisions = resolver.resolve_query(query)
    return {
        "identity_snapshot_id": resolver.snapshot.snapshot_id,
        "resolver_version": CANONICAL_RESOLVER_VERSION,
        "decisions": [d.to_dict() for d in decisions],
        "mutable_store_read": False,
        "graph_v2_activated": False,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Build seed registry
    print("Building seed registry...")
    registry = build_seed_registry_v2()
    print(f"Registry stats: {registry.stats()}")
    registry.save()
    print(f"Saved to {registry.registry_path}")

    # Test pipeline
    print("\nTesting pipeline...")
    pipeline = EntityResolutionPipeline(use_llm=False)
    result = pipeline.process_record({
        "t": "NVIDIA发布Blackwell架构新GPU",
        "b": "英伟达推出了基于Blackwell架构的GPU，台积电负责代工。"
    })
    print(f"Linked entities: {len(result['linked_entities'])}")
    for le in result["linked_entities"]:
        print(f"  {le['mention']} → {le['canonical_name']} ({le['confidence']:.2f})")
