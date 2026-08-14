#!/usr/bin/env python3
"""重灌在 KP 补全之前已入图的记录：删除旧 doc，用含关键参数的新内容重新插入。

用法: .venv/bin/python scripts/polish_reingest.py
"""
import json, re, sys, asyncio
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "qa-backend"))
sys.path.insert(0, str(REPO))

from config import WORKING_DIR, llm_model_func, embedding_func

POLISH_IDS = json.loads((REPO / 'runtime/polish_reingest_ids.json').read_text())


def format_doc(r, data_idx):
    """与 qa-backend/ingest.py 完全一致的文档格式。"""
    title = r.get("t", "")
    category = r.get("c", "")
    tag = r.get("tg", "")
    source = r.get("a", "")
    date = r.get("d", "")
    ai_summary = r.get("as", "")
    body = r.get("b", "") or r.get("fb", "") or ""
    score = r.get("sc", 0)
    key_params = r.get("kp", [])
    topic = r.get("tp", "")

    parts = [f"标题：{title}"]
    if topic: parts.append(f"主题：{topic}")
    if category: parts.append(f"分类：{category}")
    if tag: parts.append(f"标签：{tag}")
    if source: parts.append(f"来源：{source}")
    if date: parts.append(f"日期：{date}")
    if score: parts.append(f"质量评分：{score}")
    if key_params: parts.append(f"关键参数：{', '.join(str(p) for p in key_params)}")
    parts.append("")
    if ai_summary:
        parts.append("AI摘要："); parts.append(ai_summary); parts.append("")
    if body:
        parts.append("正文：")
        if len(body) > 3000:
            body = body[:3000] + "..."
        parts.append(body)
    return f"[RECORD_ID:{data_idx}]\n" + "\n".join(parts)


async def main():
    from lightrag import LightRAG

    fd = json.loads((WORKING_DIR / 'kv_store_full_docs.json').read_text())
    doc2rec = {}
    for doc_id, v in fd.items():
        c = v.get('content', '') if isinstance(v, dict) else str(v)
        m = re.match(r'\[RECORD_ID:(\d+)\]', c[:50])
        if m: doc2rec[doc_id] = int(m.group(1))
    rec2doc = {v: k for k, v in doc2rec.items()}

    data = json.loads((REPO / 'data/processed/all-records-lite.json').read_text())

    rag = LightRAG(
        working_dir=str(WORKING_DIR),
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        chunk_token_size=1200,
        chunk_overlap_token_size=100,
        entity_extract_max_gleaning=1,
        default_embedding_timeout=600,
        default_llm_timeout=300,
        llm_model_max_async=20,
        embedding_func_max_async=8,
        max_parallel_insert=6,
        embedding_batch_num=32,
        addon_params={
            "language": "Simplified Chinese",
            "entity_types": ["公司", "机构", "技术", "材料", "产品", "人物", "地点",
                              "政策", "指标", "事件", "项目", "设备", "方法", "化学反应"],
        },
    )
    rag.addon_params["max_execution_timeout"] = 600
    rag.addon_params["llm_timeout"] = 180
    await rag.initialize_storages()
    print("LightRAG initialized", flush=True)

    done = set()
    for idx in POLISH_IDS:
        old_doc = rec2doc.get(idx)
        if not old_doc:
            print(f"[{idx}] 未找到旧 doc，跳过"); continue
        r = data[idx]
        new_content = format_doc(r, idx)
        old_content = fd[old_doc]['content'] if isinstance(fd[old_doc], dict) else fd[old_doc]
        if old_content == new_content:
            print(f"[{idx}] 内容已一致，跳过"); continue
        print(f"[{idx}] 删除旧 doc {old_doc}...", flush=True)
        res = await rag.adelete_by_doc_id(old_doc)
        print(f"    删除结果: {res.success if hasattr(res,'success') else res}", flush=True)
        print(f"    重新插入 ({len(r.get('kp',[]))} 个kp)...", flush=True)
        await rag.ainsert(new_content)
        done.add(idx)
        print(f"    ✓ 完成 {idx}", flush=True)

    print(f"\n重灌完成: {len(done)}/{len(POLISH_IDS)}")
    # 更新进度文件（内容变了但 RECORD_ID 不变，done_ids 不需改）

asyncio.run(main())
