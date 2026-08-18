"""
T027 — Semantic Knowledge Graph Pipeline
==========================================
Builds a semantic knowledge graph from:
  1. LightRAG entity/relation extraction (primary source)
  2. Entity registry (entity_resolver.py)
  3. Structured records (kp parameters)

The pipeline:
  1. Export from LightRAG storage (entities, relationships)
  2. Canonicalize entities via entity_resolver
  3. Map raw relations to typed predicates (relation_ontology)
  4. Create GraphStatements with evidence refs
  5. Store as graph-export.json

This module handles both:
  - Building the graph (export + map)
  - Serving graph queries (for retrieval/graph.py)
"""
import os
import sys
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from relation_ontology import (
    GraphStatement, AssertionStatus, Polarity, Modality,
    RELATIONS, ONTOLOGY_VERSION
)
from entity_resolver import build_seed_registry, EntityRegistry

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
GRAPH_EXPORT_PATH = REPO / "data" / "lightrag" / "graph-export.json"
LIGHTRAG_STORAGE = REPO / "data" / "lightrag"


@dataclass
class SemanticGraph:
    """In-memory semantic knowledge graph."""
    statements: List[GraphStatement] = field(default_factory=list)
    entity_index: Dict[str, Set[int]] = field(default_factory=dict)  # entity_id → statement indices
    predicate_index: Dict[str, Set[int]] = field(default_factory=dict)  # predicate → statement indices
    record_index: Dict[int, Set[int]] = field(default_factory=dict)  # record_id → statement indices
    metadata: dict = field(default_factory=dict)

    def add_statement(self, stmt: GraphStatement):
        idx = len(self.statements)
        self.statements.append(stmt)

        # Index by subject and object
        if stmt.subject_id:
            self.entity_index.setdefault(stmt.subject_id, set()).add(idx)
        if stmt.object_id:
            self.entity_index.setdefault(stmt.object_id, set()).add(idx)

        # Index by predicate
        self.predicate_index.setdefault(stmt.predicate, set()).add(idx)

        # Index by evidence record IDs
        for ref in stmt.evidence_refs:
            rid = ref.get("record_id")
            if rid is not None:
                self.record_index.setdefault(rid, set()).add(idx)

    def query_by_entity(self, entity_id: str, limit: int = 50) -> List[GraphStatement]:
        """Get all statements involving an entity (as subject or object)."""
        indices = self.entity_index.get(entity_id, set())
        return [self.statements[i] for i in sorted(indices)[:limit]]

    def query_by_predicate(self, predicate: str, limit: int = 50) -> List[GraphStatement]:
        """Get all statements with a specific predicate."""
        indices = self.predicate_index.get(predicate, set())
        return [self.statements[i] for i in sorted(indices)[:limit]]

    def query_by_record(self, record_id: int, limit: int = 50) -> List[GraphStatement]:
        """Get all statements with evidence from a specific record."""
        indices = self.record_index.get(record_id, set())
        return [self.statements[i] for i in sorted(indices)[:limit]]

    def multi_hop(
        self,
        start_entity: str,
        max_hops: int = 2,
        limit_per_hop: int = 20,
    ) -> List[List[GraphStatement]]:
        """Find multi-hop paths starting from an entity.
        
        Returns paths as lists of statements.
        Note: Multi-hop results are DISCOVERY ONLY — no inference without explicit relation.
        """
        if max_hops <= 0:
            return []

        paths = []
        visited = {start_entity}
        frontier = [start_entity]

        for hop in range(max_hops):
            next_frontier = []
            for entity in frontier:
                stmts = self.query_by_entity(entity, limit=limit_per_hop)
                for stmt in stmts:
                    other = stmt.object_id if stmt.subject_id == entity else stmt.subject_id
                    if other and other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
                        paths.append([stmt])
            frontier = next_frontier

        return paths

    def stats(self) -> dict:
        return {
            "total_statements": len(self.statements),
            "total_entities": len(self.entity_index),
            "total_predicates": len(self.predicate_index),
            "total_records_linked": len(self.record_index),
            "metadata": self.metadata,
        }


def export_lightrag_graph(lightrag_storage: Path = None) -> dict:
    """Export entities and relations from LightRAG storage.
    
    Attempts to read from LightRAG's internal storage:
      - JSONKVStorage (entities, relationships)
      - NanoVectorDB (embeddings)
    
    If LightRAG isn't available or storage is empty, returns empty graph.
    """
    lightrag_storage = lightrag_storage or LIGHTRAG_STORAGE

    entities = {}
    relationships = []

    # Try to load LightRAG graph data from storage
    # LightRAG stores in various backends; check for JSON files
    json_storage = lightrag_storage / "json"
    if json_storage.exists():
        # Load entity data
        entity_file = json_storage / "graph_entity.json"
        if entity_file.exists():
            try:
                with open(entity_file) as f:
                    entities = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load LightRAG entities: {e}")

        # Load relationship data
        rel_file = json_storage / "graph_relation.json"
        if rel_file.exists():
            try:
                with open(rel_file) as f:
                    relationships = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load LightRAG relationships: {e}")

    # Also try networkx pickle
    pkl_file = lightrag_storage / "graph_nx.pkl"
    if pkl_file.exists():
        try:
            import pickle
            with open(pkl_file, "rb") as f:
                import networkx as nx
                G = pickle.load(f)
                for node, data in G.nodes(data=True):
                    entities[node] = data
                for u, v, data in G.edges(data=True):
                    relationships.append({
                        "source": u,
                        "target": v,
                        **data,
                    })
        except Exception as e:
            logger.warning(f"Could not load LightRAG networkx graph: {e}")

    # Try LightRAG's own export API
    try:
        from lightrag import LightRAG
        from lightrag.base import QueryParam
        # Just check if it's importable
        logger.info("LightRAG is available for runtime graph queries")
    except ImportError:
        logger.info("LightRAG not installed — graph export relies on pre-built data")

    return {
        "entities": entities,
        "relationships": relationships,
        "export_time": datetime.now().isoformat(),
    }


# ─── Relation Type Mapping ───

# Maps raw LightRAG relation strings to our typed predicates
RELATION_TYPE_MAP = {
    # RELEASED
    "发布": "RELEASED",
    "推出": "RELEASED",
    "published": "RELEASED",
    "released": "RELEASED",
    "launched": "RELEASED",
    # DEVELOPED
    "研发": "DEVELOPED",
    "开发": "DEVELOPED",
    "developed": "DEVELOPED",
    "发明": "DEVELOPED",
    "invented": "DEVELOPED",
    # USES
    "使用": "USES",
    "采用": "USES",
    "uses": "USES",
    "applies": "USES",
    # USES_MATERIAL
    "材料": "USES_MATERIAL",
    "contains": "USES_MATERIAL",
    "incorporates": "USES_MATERIAL",
    # SUPPORTS / PART_OF
    "支持": "SUPPORTS",
    "supports": "SUPPORTS",
    "组成": "PART_OF",
    "part_of": "PART_OF",
    "belongs_to": "PART_OF",
    # COMPETES_WITH
    "竞争": "COMPETES_WITH",
    "competes": "COMPETES_WITH",
    "rivals": "COMPETES_WITH",
    # ACHIEVES / IMPROVES
    "实现": "ACHIEVES",
    "achieved": "ACHIEVES",
    "提升": "IMPROVES",
    "improves": "IMPROVES",
    "增强": "IMPROVES",
    # REPLACES / SUPERSEDES
    "替代": "REPLACES",
    "replaces": "REPLACES",
    "取代": "SUPERSEDES",
    "supersedes": "SUPERSEDES",
    # PARTNERED_WITH / INVESTED_IN
    "合作": "PARTNERED_WITH",
    "partnered": "PARTNERED_WITH",
    "投资": "INVESTED_IN",
    "invested": "INVESTED_IN",
    # MEASURED_AT
    "测量": "MEASURED_AT",
    "measured": "MEASURED_AT",
    "效率": "MEASURED_AT",
    "密度": "MEASURED_AT",
}


def map_relation_type(raw_relation: str) -> str:
    """Map a raw relation string to a typed predicate."""
    raw_lower = raw_relation.lower().strip()

    # Direct lookup
    for key, pred in RELATION_TYPE_MAP.items():
        if key in raw_lower:
            return pred

    return "RELATED_CO_OCCURRENCE"


def map_assertion_status(raw_status: str, lightrag_meta: dict = None) -> AssertionStatus:
    """Map raw assertion status to AssertionStatus enum."""
    if not raw_status:
        return AssertionStatus.ASSERTED

    raw_lower = raw_status.lower().strip()

    if any(w in raw_lower for w in ["plan", "计划", "预计", "will", "future", "规划"]):
        return AssertionStatus.PLANNED
    if any(w in raw_lower for w in ["rumor", "传闻", "据称", "alleged"]):
        return AssertionStatus.RUMORED
    if any(w in raw_lower for w in ["disputed", "争议", "disagree"]):
        return AssertionStatus.DISPUTED
    if any(w in raw_lower for w in ["deprecated", "废弃", "淘汰"]):
        return AssertionStatus.DEPRECATED

    return AssertionStatus.ASSERTED


def build_semantic_graph(
    lightrag_export: dict = None,
    entity_registry: EntityRegistry = None,
    output_path: Path = None,
) -> SemanticGraph:
    """Build a SemanticGraph from LightRAG export data.
    
    Steps:
      1. Canonicalize entities via entity_registry
      2. Map raw relations to typed predicates
      3. Create GraphStatements with evidence refs
      4. Build indices
    """
    if lightrag_export is None:
        lightrag_export = export_lightrag_graph()

    if entity_registry is None:
        entity_registry = build_seed_registry()

    graph = SemanticGraph()
    graph.metadata = {
        "built_at": datetime.now().isoformat(),
        "source": "lightrag_export",
        "entity_count_raw": len(lightrag_export.get("entities", {})),
        "relationship_count_raw": len(lightrag_export.get("relationships", [])),
    }

    # Process entities
    entity_map = {}  # raw_name → canonical entity_id
    for ent_name, ent_data in lightrag_export.get("entities", {}).items():
        if isinstance(ent_data, dict):
            result = entity_registry.resolve(ent_name)
            if result["status"] == "LINKED":
                entity_map[ent_name] = result["entity_id"]
            else:
                # Create new entity
                entity_type = ent_data.get("type", "ORG").upper()
                # Map common types
                type_map = {"ORG": "ORG", "ORGANIZATION": "ORG", "PERSON": "PERSON",
                           "PRODUCT": "PRODUCT", "TECHNOLOGY": "TECH", "TECH": "TECH",
                           "MATERIAL": "MATERIAL", "LOCATION": "GEO", "GEO": "GEO",
                           "EVENT": "EVENT", "UNKNOWN": "ORG"}
                entity_type = type_map.get(entity_type, "ORG")

                import hashlib
                ent_hash = hashlib.md5(ent_name.encode("utf-8")).hexdigest()[:8]
                entity_id = f"lr:{entity_type.lower()}_{ent_hash}"
                try:
                    entity_registry.add_entity(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        canonical_name=ent_name,
                        aliases=[],
                        provenance="lightrag",
                    )
                except ValueError:
                    entity_type = "ORG"  # fallback
                    entity_registry.add_entity(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        canonical_name=ent_name,
                        aliases=[],
                        provenance="lightrag",
                    )
                entity_map[ent_name] = entity_id

    # Process relationships
    for rel in lightrag_export.get("relationships", []):
        if isinstance(rel, dict):
            source_name = rel.get("source", rel.get("subject", ""))
            target_name = rel.get("target", rel.get("object", ""))
            raw_predicate = rel.get("predicate", rel.get("relation", rel.get("description", "")))
            description = rel.get("description", "")
            evidence = rel.get("evidence", "")

            if not source_name or not target_name:
                continue

            # Canonicalize entities
            source_id = entity_map.get(source_name, source_name)
            target_id = entity_map.get(target_name, target_name)

            # Map relation type
            predicate = map_relation_type(raw_predicate or description)

            # Map assertion status
            assertion = map_assertion_status(description, rel)

            # Extract evidence refs
            evidence_refs = []
            if "source_id" in rel:
                evidence_refs.append({"record_id": rel["source_id"]})
            if evidence:
                evidence_refs.append({"text": evidence[:200]})

            # Create GraphStatement
            stmt = GraphStatement(
                subject_id=source_id,
                predicate=predicate,
                object_id=target_id,
                polarity="POSITIVE",
                modality="DECLARATIVE",
                assertion_status=assertion,
                evidence_refs=evidence_refs,
            )
            stmt.grounding_status = "VALID" if evidence_refs else "UNVERIFIED"
            graph.add_statement(stmt)

    # Also process from entity registry (seed entities as background knowledge)
    for ent in entity_registry.all_entities():
        if ent.get("aliases"):
            for alias in ent["aliases"][:2]:  # Limit to avoid flooding
                # Create co-occurrence statements between entity and its aliases
                pass  # Aliases are same entity, not separate statements

    # Save to output
    if output_path is None:
        output_path = GRAPH_EXPORT_PATH

    save_graph(graph, output_path)

    logger.info(f"Built semantic graph: {graph.stats()}")
    return graph


def save_graph(graph: SemanticGraph, output_path: Path):
    """Save semantic graph to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "metadata": graph.metadata,
        "statements": [s.to_dict() for s in graph.statements],
        "stats": graph.stats(),
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved semantic graph to {output_path}")


def load_graph(input_path: Path = None) -> SemanticGraph:
    """Load a previously saved semantic graph."""
    if input_path is None:
        input_path = GRAPH_EXPORT_PATH

    if not input_path.exists():
        logger.warning(f"Graph file not found: {input_path}")
        return SemanticGraph()

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    graph = SemanticGraph()
    graph.metadata = data.get("metadata", {})

    for stmt_data in data.get("statements", []):
        stmt = GraphStatement.from_dict(stmt_data)
        graph.add_statement(stmt)

    return graph


def extract_facts_from_record(record: dict, record_id: str | int, entity_registry: EntityRegistry = None) -> List[GraphStatement]:
    """Extract structured facts (GraphStatements) from a single record.
    
    This is a supplementary extraction that doesn't require LightRAG.
    Uses kp (key_params) and title/body to extract factual statements.
    """
    if entity_registry is None:
        entity_registry = build_seed_registry()

    statements = []
    title = record.get("t", "")
    from primary_evidence import source_evidence_text
    body = source_evidence_text(record)
    source = record.get("s", "")
    date = record.get("d", "")

    # Extract from key parameters
    kp = record.get("kp", [])
    if isinstance(kp, list):
        for param in kp:
            param_str = str(param)
            # Try to extract entity + metric patterns
            # e.g., "能量密度: 400Wh/kg" → ENTITY ACHIEVES 400Wh/kg
            if ":" in param_str or "：" in param_str:
                parts = param_str.replace("：", ":").split(":", 1)
                if len(parts) == 2:
                    metric_name = parts[0].strip()
                    metric_value = parts[1].strip()

                    # Try to resolve title as entity
                    result = entity_registry.resolve(title)
                    if result["status"] == "LINKED":
                        entity_id = result["entity_id"]
                    else:
                        import hashlib
                        ent_hash = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
                        entity_id = f"rec:entity_{ent_hash}"

                    stmt = GraphStatement(
                        subject_id=entity_id,
                        predicate="MEASURED_AT",
                        object_id=f"{metric_name}={metric_value}",
                        polarity="POSITIVE",
                        modality="DECLARATIVE",
                        assertion_status=AssertionStatus.ASSERTED,
                        evidence_refs=[{"record_id": record_id, "field": "kp"}],
                    )
                    stmt.grounding_status = "VALID"
                    statements.append(stmt)

    return statements


def build_graph_from_records(
    records: List[dict],
    entity_registry: EntityRegistry = None,
    output_path: Path = None,
) -> SemanticGraph:
    """Build a semantic graph from structured records (no LightRAG required).
    
    This is a fallback when LightRAG isn't available.
    Extracts facts from key parameters and titles.
    """
    if entity_registry is None:
        entity_registry = build_seed_registry()

    graph = SemanticGraph()
    graph.metadata = {
        "built_at": datetime.now().isoformat(),
        "source": "records",
        "record_count": len(records),
    }

    for i, record in enumerate(records):
        stmts = extract_facts_from_record(record, i, entity_registry)
        for stmt in stmts:
            graph.add_statement(stmt)

    if output_path:
        save_graph(graph, output_path)

    return graph


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Try to build from LightRAG export first
    print("Building semantic graph...")
    graph = build_semantic_graph()
    print(f"Graph stats: {graph.stats()}")
    print(f"Saved to: {GRAPH_EXPORT_PATH}")
