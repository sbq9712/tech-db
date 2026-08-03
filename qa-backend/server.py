#!/usr/bin/env python3
"""FastAPI backend server for tech-db Q&A system.

Hybrid retrieval: BM25 (keyword) + vector (semantic) fused via RRF.
GLM-5.2 streaming for answer generation.

Endpoints:
  POST /api/chat/stream  - Streaming chat (SSE)
  GET  /api/graph       - Get knowledge graph data for visualization
  GET  /api/stats       - System statistics
  GET  /api/health      - Health check
  GET  /api/search       - Direct search (debug)

Run:
  .venv/bin/python qa-backend/server.py
"""
import os
import sys
import json
import pickle
import asyncio
import re
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

# Add paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import (
    WORKING_DIR, llm_model_func, embedding_func,
    llm_stream_func, MODEL_NAME,
)

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
INDEX_FILE = WORKING_DIR / "vector_index_v2.pkl"
BM25_FILE = WORKING_DIR / "bm25_index.pkl"
JIEBA_DICT = WORKING_DIR / "jieba_custom_dict.txt"

# ── Global state ──
_vector_index = None  # numpy array (N, 1024)
_index_meta = None    # list of metadata dicts
_bm25_index = None    # BM25Okapi instance
_bm25_meta = None     # BM25 metadata list
_bm25_corpus = None   # tokenized corpus for BM25
_records = None       # full records from all-records-lite.json
_graph_data = None    # graph-export.json data (nodes, edges, entity_to_records)
_entity_index = None  # entity_name -> list of record indices

# ── RRF parameters ──
RRF_K = 60          # RRF constant (1/(rank+k))
RETRIEVAL_TOP_K = 50  # candidates per route
FINAL_TOP_K = 25     # max records after fusion
RELEVANCE_FLOOR = 0.3  # vector similarity floor for "honest answer" trigger


def load_vector_index():
    """Load the pre-built vector index."""
    global _vector_index, _index_meta
    if _vector_index is not None:
        return
    # Try v2 first, fall back to v1
    idx_file = INDEX_FILE if INDEX_FILE.exists() else WORKING_DIR / "vector_index.pkl"
    if idx_file.exists():
        print(f"[startup] Loading vector index from {idx_file.name}...", flush=True)
        with open(idx_file, "rb") as f:
            data = pickle.load(f)
        _vector_index = data["embeddings"]
        _index_meta = data["meta"]
        print(f"[startup] Vector index loaded: {len(_index_meta)} records, dim={data['dim']}", flush=True)
    else:
        print(f"[startup] WARNING: No vector index found", flush=True)


def load_bm25_index():
    """Load the pre-built BM25 index."""
    global _bm25_index, _bm25_meta, _bm25_corpus
    if _bm25_index is not None:
        return
    if BM25_FILE.exists():
        print(f"[startup] Loading BM25 index...", flush=True)
        with open(BM25_FILE, "rb") as f:
            data = pickle.load(f)
        _bm25_index = data["bm25"]
        _bm25_meta = data["meta"]
        _bm25_corpus = data.get("corpus_tokens")
        print(f"[startup] BM25 index loaded: {len(_bm25_meta)} documents", flush=True)
    else:
        print(f"[startup] BM25 index not found (hybrid search disabled)", flush=True)


def load_graph_index():
    """Load knowledge graph with entity→record mapping."""
    global _graph_data, _entity_index
    if _graph_data is not None:
        return
    graph_file = WORKING_DIR / "graph-export.json"
    if graph_file.exists():
        print(f"[startup] Loading knowledge graph...", flush=True)
        _graph_data = json.loads(graph_file.read_text("utf-8"))
        e2r = _graph_data.get("entity_to_records", {})
        _entity_index = {k: set(v) for k, v in e2r.items()}
        print(f"[startup] Graph loaded: {len(_graph_data.get('nodes',[]))} nodes, "
              f"{len(_graph_data.get('edges',[]))} edges, "
              f"{len(_entity_index)} entity→record mappings", flush=True)
    else:
        print(f"[startup] Knowledge graph not found (graph search disabled)", flush=True)


def load_records():
    """Load all-records-lite.json for full record lookup."""
    global _records
    if _records is None:
        _records = json.loads(LITE_PATH.read_text("utf-8"))
    return _records


async def vector_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Search the vector index for the most similar records."""
    if _vector_index is None:
        return []

    # Embed the query
    query_emb = await embedding_func([query])
    query_vec = np.array(query_emb[0], dtype=np.float32)
    query_vec = query_vec / max(np.linalg.norm(query_vec), 1e-8)

    # Cosine similarity (embeddings are pre-normalized)
    scores = _vector_index @ query_vec

    # Get top_k indices
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        meta = _index_meta[idx]
        score = float(scores[idx])
        results.append({
            "meta": meta,
            "score": score,
        })
    return results


def bm25_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Search the BM25 index for keyword matches."""
    if _bm25_index is None:
        return []

    import jieba
    tokens = list(jieba.cut_for_search(query))
    if not tokens:
        return []

    scores = _bm25_index.get_scores(tokens)
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        score = float(scores[idx])
        if score <= 0:
            continue
        meta = _bm25_meta[idx]
        results.append({
            "meta": meta,
            "score": score,
        })
    return results


def rrf_fuse(vec_results: list, bm25_results: list, graph_results: list = None,
             k: int = RRF_K, top_k: int = FINAL_TOP_K) -> list:
    """Reciprocal Rank Fusion of multiple result lists.

    Returns unified list of {meta, score} with RRF scores.
    """
    rrf_scores = {}  # record_idx -> {rrf, meta, vec_score, bm25_score, graph_score}
    all_routes = [
        ("vec", vec_results, "vec_score"),
        ("bm25", bm25_results, "bm25_score"),
    ]
    if graph_results:
        all_routes.append(("graph", graph_results, "graph_score"))

    for route_name, results, score_key in all_routes:
        for rank, result in enumerate(results):
            rec_idx = result["meta"]["idx"]
            if rec_idx not in rrf_scores:
                rrf_scores[rec_idx] = {"rrf": 0.0, "meta": result["meta"],
                                       "vec_score": 0.0, "bm25_score": 0.0,
                                       "graph_score": 0.0}
            rrf_scores[rec_idx]["rrf"] += 1.0 / (rank + k)
            rrf_scores[rec_idx][score_key] = result["score"]

    # Sort by RRF score
    fused = sorted(rrf_scores.values(), key=lambda x: -x["rrf"])[:top_k]

    return [{
        "meta": item["meta"],
        "score": item["rrf"],
        "vec_score": item["vec_score"],
        "bm25_score": item["bm25_score"],
        "graph_score": item.get("graph_score", 0),
    } for item in fused]


def graph_search(query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Graph-based retrieval: match entities in query → get associated records.

    Uses jieba posseg to extract entities from query, matches against
    graph node names, then retrieves associated records via entity_to_records.
    """
    if _entity_index is None:
        return []

    import jieba.posseg as pseg

    # Extract potential entities from query using POS tagging
    words = pseg.cut(query)
    query_terms = []
    for word, flag in words:
        word = word.strip()
        if len(word) >= 2 and flag in ('n', 'nr', 'ns', 'nt', 'nz', 'vn', 'eng'):
            query_terms.append(word)

    # Also try exact match of full query substrings against entity names
    # (handles multi-word entities like "磷酸铁锂")
    # Check each entity name: is it a substring of the query?
    matched_entities = set()
    for entity_name in _entity_index:
        if entity_name in query:
            matched_entities.add(entity_name)
    # Also check: is any query term a substring of an entity name?
    for term in query_terms:
        for entity_name in _entity_index:
            if term in entity_name or entity_name in term:
                matched_entities.add(entity_name)

    if not matched_entities:
        return []

    # Score records: each matched entity contributes 1.0/hop (hop=0 for direct)
    record_scores = {}  # record_idx -> score
    for entity in matched_entities:
        for rec_idx in _entity_index.get(entity, set()):
            if rec_idx not in record_scores:
                record_scores[rec_idx] = 0.0
            record_scores[rec_idx] += 1.0

    # Sort by score (more matched entities = higher score)
    sorted_records = sorted(record_scores.items(), key=lambda x: -x[1])[:top_k]

    results = []
    for rec_idx, score in sorted_records:
        # Find meta for this record
        meta = None
        for m in _index_meta:
            if m.get("idx") == rec_idx:
                meta = m
                break
        if not meta:
            for m in _bm25_meta:
                if m.get("idx") == rec_idx:
                    meta = m
                    break
        if meta:
            results.append({"meta": meta, "score": score})

    return results


async def hybrid_search(query: str) -> tuple:
    """Hybrid retrieval: vector + BM25 + graph → RRF fusion → dynamic cutoff.

    Returns (search_results, is_relevant) where is_relevant indicates
    whether results are good enough to generate an answer.
    """
    # Run all routes in parallel
    vec_task = vector_search(query, top_k=RETRIEVAL_TOP_K)
    bm25_task = asyncio.to_thread(bm25_search, query, RETRIEVAL_TOP_K)
    graph_task = asyncio.to_thread(graph_search, query, RETRIEVAL_TOP_K)

    vec_results, bm25_results, graph_results = await asyncio.gather(
        vec_task, bm25_task, graph_task
    )

    # Fuse via RRF
    results = rrf_fuse(vec_results, bm25_results, graph_results if graph_results else None)

    # Dynamic cutoff: check if best result is relevant enough
    is_relevant = False
    if results:
        best_vec_score = max((r.get("vec_score", 0) for r in results), default=0)
        has_bm25_hit = any(r.get("bm25_score", 0) > 0 for r in results)
        has_graph_hit = any(r.get("graph_score", 0) > 0 for r in results)
        is_relevant = best_vec_score >= RELEVANCE_FLOOR or has_bm25_hit or has_graph_hit

    return results, is_relevant


def build_context(search_results: list) -> tuple:
    """Build context string and citations from search results."""
    records = load_records()
    citations = []
    context_parts = []

    for i, result in enumerate(search_results):
        meta = result["meta"]
        score = result["score"]
        orig_idx = meta.get("idx", -1)
        record = records[orig_idx] if 0 <= orig_idx < len(records) else None
        if not record:
            continue

        title = record.get("t", "") or ""
        date = record.get("d", "") or ""
        source = record.get("a", record.get("s", "")) or ""
        body = record.get("b", "") or record.get("fb", "") or ""
        ai_summary = record.get("as", "") or ""
        cat = record.get("c", "") or ""
        tags = record.get("tg", [])
        if isinstance(tags, str):
            tags = [tags]
        url = record.get("u", "") or ""
        sc = record.get("sc", 0)

        # Add to context
        context_parts.append(
            f"[{i+1}] [{cat}] {title} ({date})\n"
            f"来源: {source}\n"
            f"摘要: {ai_summary[:300] if ai_summary else body[:300]}\n"
            f"相似度: {score:.2f}"
        )

        # Add citation
        citations.append({
            "id": i + 1,
            "record_id": orig_idx,
            "title": title,
            "date": date,
            "source": source,
            "score": sc,
            "tag": tags[0] if tags else "",
            "category": cat,
            "url": url,
            "body_snippet": (body[:200] if body else (ai_summary[:200] if ai_summary else "")),
            "similarity": round(score, 3),
        })

    context = "\n\n---\n\n".join(context_parts)
    return context, citations


# ── Request Models ──
class ChatRequest(BaseModel):
    query: str
    conversation_id: str = ""
    history: list = []


# ── Lifespan ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] Loading vector index...", flush=True)
    load_vector_index()
    print("[startup] Loading BM25 index...", flush=True)
    load_bm25_index()

    print("[startup] Loading knowledge graph...", flush=True)
    load_graph_index()

    # Load jieba custom dictionary for query tokenization
    if JIEBA_DICT.exists():
        import jieba
        jieba.load_userdict(str(JIEBA_DICT))
        print("[startup] Jieba custom dict loaded", flush=True)

    load_records()
    print(f"[startup] Records loaded: {len(_records)}", flush=True)
    print("[startup] Ready!", flush=True)
    yield
    print("[shutdown] Cleaning up...", flush=True)


# ── FastAPI App ──
app = FastAPI(title="Tech-DB Q&A API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Endpoints ──

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "vector_index_ready": _vector_index is not None,
        "bm25_ready": _bm25_index is not None,
        "indexed_records": len(_index_meta) if _index_meta else 0,
        "bm25_records": len(_bm25_meta) if _bm25_meta else 0,
        "total_records": len(_records) if _records else 0,
        "time": datetime.now().isoformat(),
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Streaming chat endpoint using SSE."""

    async def event_generator():
        try:
            # Step 1: Retrieval
            yield {"event": "status", "data": json.dumps({
                "step": "retrieving",
                "message": "正在检索相关知识..."
            })}

            if _vector_index is None:
                yield {"event": "error", "data": json.dumps({
                    "message": "向量索引尚未构建。请运行: python qa-backend/vector_index.py"
                })}
                return

            # Hybrid search (vector + BM25 → RRF)
            search_results, is_relevant = await hybrid_search(req.query)

            if not search_results or not is_relevant:
                yield {"event": "done", "data": json.dumps({
                    "answer": "抱歉，数据库中没有足够的情报来回答这个问题。请尝试用更具体的关键词或换个角度提问。",
                    "citations": []
                })}
                return

            # Build context and citations
            context, citations = build_context(search_results)

            # Yield citations
            if citations:
                yield {"event": "citations", "data": json.dumps({"citations": citations})}

            # Step 2: Analysis
            yield {"event": "status", "data": json.dumps({
                "step": "analyzing",
                "message": f"找到 {len(citations)} 条相关记录，正在分析..."
            })}

            await asyncio.sleep(0.1)  # Small delay for UX

            # Step 3: Generation
            yield {"event": "status", "data": json.dumps({
                "step": "generating",
                "message": "正在生成回答..."
            })}

            # Build source list for prompt
            source_list = "\n".join(
                f"[{i+1}] {c['title']} ({c['date']}, {c['source']})"
                for i, c in enumerate(citations)
            )

            system_prompt = f"""你是技术情报分析专家。基于以下检索到的技术情报资料回答用户问题。

要求：
1. 只基于提供的资料回答，不要编造信息
2. 在回答中用 [1][2] 等标注引用来源（对应来源列表的序号）
3. 如果资料中没有相关信息，诚实回答"数据库中没有相关信息"
4. 简单问题简短回答，复杂问题详细分析
5. 使用中文回答，使用markdown格式

检索到的资料：
{context}

来源列表：
{source_list}"""

            # Build conversation history for LLM
            llm_history = []
            for msg in req.history[-6:]:
                llm_history.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

            full_answer = ""
            try:
                # Stream directly from LLM
                async for chunk in llm_stream_func(
                    prompt=req.query,
                    system_prompt=system_prompt,
                    history_messages=llm_history,
                ):
                    if chunk:
                        full_answer += chunk
                        yield {"event": "token", "data": json.dumps({"text": chunk})}
            except Exception as e:
                # Fallback: non-streaming
                print(f"[stream-fallback] {e}", flush=True)
                try:
                    answer = await llm_model_func(
                        req.query,
                        system_prompt=system_prompt,
                        history_messages=llm_history,
                    )
                    if answer:
                        full_answer = answer
                        for i in range(0, len(answer), 3):
                            yield {"event": "token", "data": json.dumps({"text": answer[i:i+3]})}
                            await asyncio.sleep(0.015)
                except Exception as e2:
                    yield {"event": "error", "data": json.dumps({
                        "message": f"生成失败: {e2}"
                    })}
                    return

            yield {"event": "done", "data": json.dumps({
                "answer": full_answer,
                "citations": citations
            })}

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(event_generator())


@app.get("/api/graph")
async def get_graph(limit: int = 300):
    """Get knowledge graph data for visualization."""
    graph_file = WORKING_DIR / "graph-export.json"
    if graph_file.exists():
        data = json.loads(graph_file.read_text("utf-8"))
        nodes = sorted(data.get("nodes", []), key=lambda n: n.get("degree", 0), reverse=True)
        if limit and len(nodes) > limit:
            top_node_ids = {n["id"] for n in nodes[:limit]}
            nodes = nodes[:limit]
            edges = [e for e in data.get("edges", [])
                     if e["source"] in top_node_ids and e["target"] in top_node_ids]
        else:
            edges = data.get("edges", [])
        return {
            "nodes": nodes,
            "edges": edges,
            "total_nodes": len(data.get("nodes", [])),
            "total_edges": len(data.get("edges", [])),
        }
    return {
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "message": "Graph not yet built."
    }


@app.get("/api/stats")
async def stats():
    """Get system statistics."""
    total = len(_records) if _records else 0
    indexed = len(_index_meta) if _index_meta else 0

    graph_file = WORKING_DIR / "graph-export.json"
    nodes = edges = 0
    if graph_file.exists():
        graph = json.loads(graph_file.read_text("utf-8"))
        nodes = len(graph.get("nodes", []))
        edges = len(graph.get("edges", []))

    return {
        "total_records": total,
        "indexed_records": indexed,
        "bm25_records": len(_bm25_meta) if _bm25_meta else 0,
        "graph_nodes": nodes,
        "graph_edges": edges,
        "vector_index_ready": _vector_index is not None,
        "bm25_ready": _bm25_index is not None,
        "model": MODEL_NAME,
    }


@app.get("/api/search")
async def search(q: str, top_k: int = 10):
    """Quick vector search without LLM generation. Returns matching records."""
    if _vector_index is None:
        return JSONResponse(
            {"error": "Vector index not loaded", "results": []},
            status_code=503,
        )
    if not q.strip():
        return {"results": [], "query": q}

    results, is_relevant = await hybrid_search(q.strip())
    context, citations = build_context(results)
    return {
        "query": q,
        "results": citations,
        "total": len(citations),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
