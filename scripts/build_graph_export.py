#!/usr/bin/env python3
"""Build a lightweight graph-export.json from structured record fields.

Extracts entity mentions from titles, summaries, and key params.
Much faster than full LightRAG ingest (seconds vs hours).
"""
import json, re, sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
OUTPUT = REPO / "runtime" / "indexes" / "graph-export.json"

def extract_entities(record):
    """Extract entity candidates from record fields."""
    entities = set()
    
    title = record.get("t", "")
    summary = record.get("as", "") or ""
    body = record.get("b", "") or ""
    key_params = record.get("kp", [])
    source = record.get("a", "")
    category = record.get("c", "")
    
    # 1. Source/author as entity (company or institution)
    if source:
        # Clean source names
        src = source.strip()
        if len(src) >= 2 and len(src) <= 50:
            entities.add(("机构", src))
    
    # 2. Extract from title — look for patterns like "XX公司", "XX大学", "XX团队"
    # Also extract English organization names
    text = title + " " + summary
    if body:
        text += " " + body[:500]
    
    # Chinese organization patterns
    for m in re.finditer(r'([一-鿿]{2,8}(?:大学|学院|研究院|研究所|实验室|公司|集团|企业|科技|能源|动力|电池|材料|化工|制药|生物|半导体|芯片|汽车|航空|航天))', title + " " + summary):
        entities.add(("机构", m.group(1)))
    
    # English company/institution patterns  
    for m in re.finditer(r'([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]+){0,3})\s*(?:称|公布|研发|推出|宣布|开发|研制|建成|实现)', title + " " + summary):
        name = m.group(1).strip()
        if len(name) >= 3:
            entities.add(("公司", name))
    
    # Standalone English org names (capitalized words, 2+ words)
    for m in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b', title):
        name = m.group(1).strip()
        if len(name) >= 4 and not name.lower() in ('the', 'this', 'that', 'new', 'best'):
            entities.add(("公司", name))
    
    # 3. Materials/technologies from key params
    for kp in (key_params if isinstance(key_params, list) else []):
        kp_str = str(kp)
        # Extract parameter name (before colon)
        param_name = kp_str.split("[")[0].split(":")[0].strip()
        if param_name and len(param_name) >= 2:
            entities.add(("指标", param_name))
        
        # Extract condition (in brackets)
        for cond_match in re.finditer(r'\[([^\]]+)\]', kp_str):
            cond = cond_match.group(1).strip()
            if len(cond) >= 2:
                entities.add(("技术", cond))
    
    # 4. Category as a topic entity
    if category:
        for part in category.split("/"):
            part = part.strip()
            if part and len(part) >= 2:
                entities.add(("分类", part))
    
    # 5. Technology keywords from title
    tech_patterns = [
        r'([一-鿿]*(?:电池|电池组|太阳能|燃料电池|电解|催化|合成|材料|聚合物|半导体|芯片|量子|超导|纳米|生物|基因|储能|氢能|核聚变|碳捕获|碳循环))',
    ]
    for pattern in tech_patterns:
        for m in re.finditer(pattern, title):
            tech = m.group(1)
            if len(tech) >= 3:
                entities.add(("技术", tech))
    
    return entities


def build_graph():
    print("Loading records...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  Total records: {len(data)}", flush=True)
    
    entity_to_records = defaultdict(list)
    entity_descriptions = defaultdict(list)
    edges = defaultdict(int)  # (source, target) -> count
    
    print("Extracting entities...", flush=True)
    for idx, r in enumerate(data):
        if idx % 5000 == 0:
            print(f"  Processing {idx}/{len(data)}...", flush=True)
        
        entities = extract_entities(r)
        entity_names = [name for etype, name in entities]
        
        for etype, name in entities:
            entity_to_records[name].append(idx)
            # Build description from title
            if len(entity_descriptions[name]) < 3:
                title = r.get("t", "")[:100]
                if title:
                    entity_descriptions[name].append(title)
        
        # Build edges: entities co-occurring in the same record
        for i in range(len(entity_names)):
            for j in range(i + 1, min(i + 5, len(entity_names))):
                key = tuple(sorted([entity_names[i], entity_names[j]]))
                edges[key] += 1
    
    # Build nodes
    nodes = []
    for name, record_ids in entity_to_records.items():
        descs = entity_descriptions[name]
        nodes.append({
            "id": name,
            "label": name,
            "type": "概念",
            "description": "; ".join(descs[:2]),
            "degree": len(record_ids),
        })
    
    # Build edges (only keep edges with count >= 2 for quality)
    edge_list = []
    for (src, tgt), count in edges.items():
        if count >= 2 and src != tgt:
            edge_list.append({
                "source": src,
                "target": tgt,
                "label": f"共现{count}次",
                "weight": count,
            })
    
    graph = {
        "nodes": nodes,
        "edges": edge_list,
        "entity_to_records": {k: v for k, v in entity_to_records.items()},
    }
    
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(graph, ensure_ascii=False), "utf-8")
    
    print(f"\nDone!", flush=True)
    print(f"  Nodes: {len(nodes)}", flush=True)
    print(f"  Edges: {len(edge_list)}", flush=True)
    print(f"  Entity→records: {len(entity_to_records)}", flush=True)
    print(f"  Saved to: {OUTPUT}", flush=True)


if __name__ == "__main__":
    build_graph()
