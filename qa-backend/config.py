"""LightRAG configuration for tech-db Q&A system.

Uses GLM-5.2 (ZAI API) for LLM and bge-m3 (sentence-transformers) for embeddings.
"""
import os
import sys
import json
import asyncio
import urllib.request
import urllib.error
from pathlib import Path

# ── Paths ──
REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
WORKING_DIR = Path(os.environ.get("TECH_DB_INDEX_DIR", RUNTIME_DIR / "indexes")).resolve()
MODEL_DIR = Path(os.environ.get("TECH_DB_MODEL_DIR", RUNTIME_DIR / "models" / "bge-m3")).resolve()
WORKING_DIR.mkdir(parents=True, exist_ok=True)

# ── API Config ──
ENV_FILE = Path(os.environ.get("TECH_DB_ENV_FILE", REPO / ".env"))
API_BASE = os.environ.get("ZAI_API_BASE", "https://api.z.ai/api/coding/paas/v4")
MODEL_NAME = os.environ.get("ZAI_MODEL", "glm-5.2")

def load_api_key():
    """Read the API key lazily so health checks can start without a secret."""
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.is_file():
        with ENV_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ZAI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        return key
    raise RuntimeError(
        "ZAI_API_KEY is not configured. Set it as an environment variable or "
        f"copy .env.example to {ENV_FILE.name} and fill it in."
    )


# ── LLM Function (OpenAI-compatible, async) ──
async def llm_model_func(
    prompt: str,
    system_prompt: str = None,
    history_messages: list = None,
    **kwargs
) -> str:
    """Call GLM-5.2 via ZAI API (OpenAI-compatible)."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": kwargs.get("temperature", 0.3),
        "max_tokens": kwargs.get("max_tokens", 4096),
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=data,
        method="POST",
    )
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {load_api_key()}")
    req.add_header("Accept", "application/json")

    def _do_request():
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        choices = result.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return content

    # Run in executor to make it async
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_request)


# ── Streaming LLM Function ──
async def llm_stream_func(
    prompt: str,
    system_prompt: str = None,
    history_messages: list = None,
    **kwargs
):
    """Stream tokens from GLM-5.2 via ZAI API. Yields text chunks."""
    from openai import AsyncOpenAI

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    client = AsyncOpenAI(
        api_key=load_api_key(),
        base_url=API_BASE,
    )

    stream = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=kwargs.get("temperature", 0.3),
        max_tokens=kwargs.get("max_tokens", 4096),
        stream=True,
    )

    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


# ── Embedding Function (bge-m3 via sentence-transformers, async) ──
from lightrag.utils import EmbeddingFunc

_MODEL = None
EMBEDDING_DIM = 1024
EMBEDDING_MAX_TOKEN = 8192

def _get_model():
    global _MODEL
    if _MODEL is None:
        import torch
        torch.set_num_threads(max(torch.get_num_threads(), 10))
        from sentence_transformers import SentenceTransformer
        # Runtime assets are versioned and installed outside Git history.
        model_candidates = [
            MODEL_DIR,
            REPO / "bge-m3-model",  # legacy local installation
            Path.home() / "bge-m3-model",  # home directory installation
        ]
        model_path = None
        for p in model_candidates:
            if (p / "pytorch_model.bin").exists() or (p / "model.safetensors").exists():
                model_path = str(p)
                break
        if not model_path:
            raise RuntimeError(
                "bge-m3 模型未找到。请运行 ./setup.sh 下载模型，"
                f"或运行 python scripts/runtime_assets.py install --components model。预期目录: {MODEL_DIR}"
            )
        _MODEL = SentenceTransformer(model_path, device="cpu")
    return _MODEL

async def _embedding_func_impl(texts: list) -> list:
    """Embed texts using bge-m3."""
    model = _get_model()
    loop = asyncio.get_event_loop()

    def _encode():
        if isinstance(texts, str):
            texts_list = [texts]
        else:
            texts_list = list(texts)
        embeddings = model.encode(
            texts_list,
            batch_size=32,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return embeddings

    return await loop.run_in_executor(None, _encode)

# Wrap with EmbeddingFunc so LightRAG knows the dimension and token limits
embedding_func = EmbeddingFunc(
    embedding_dim=EMBEDDING_DIM,
    max_token_size=EMBEDDING_MAX_TOKEN,
    func=_embedding_func_impl,
    model_name="bge-m3",
    max_execution_timeout=600,
)
