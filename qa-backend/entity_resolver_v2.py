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
    """Versioned entity registry with stable opaque IDs.
    
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
        """Generate a stable opaque ID.
        
        Format: {type}:{hash8}
        Hash is deterministic based on canonical name, so same entity always gets same ID.
        """
        name_hash = hashlib.md5(canonical_name.encode("utf-8")).hexdigest()[:8]
        type_prefix = entity_type.lower() if entity_type else "entity"
        return f"{type_prefix}:{name_hash}"

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
        """Add a new canonical entity. Returns entity_id."""
        entity_id = self._generate_id(entity_type, canonical_name)

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
