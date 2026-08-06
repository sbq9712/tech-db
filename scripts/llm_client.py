#!/usr/bin/env python3
"""
Shared LLM client for tech-db pipeline.
Replaces all `hermes` CLI calls with direct ZAI (GLM-5.2) API calls.
Works both locally and in GitHub Actions.
"""
import json, os, urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"


def _get_api_key():
    """Load ZAI_API_KEY from env, then from config file."""
    key = os.environ.get("ZAI_API_KEY", "")
    if key:
        return key
    for p in [
        Path.home() / ".config" / "anthropic-proxy.env",
        Path(__file__).resolve().parents[1] / ".gh_env",
    ]:
        if p.exists():
            for line in p.read_text().split("\n"):
                if line.startswith("ZAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def call_glm(prompt, system_msg="直接输出结果，不要输出思考过程。", model="glm-5.2",
             max_tokens=8192, temperature=0.4, timeout=180):
    """Single LLM call. Returns text output."""
    key = _get_api_key()
    if not key:
        raise RuntimeError("ZAI_API_KEY not found")
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return content.strip()


def call_glm_batch(prompt, items, batch_size=10, timeout=300, max_workers=3, checkpoint_fn=None):
    """
    Batch LLM calls for classification/scoring/summary.
    prompt: instruction prompt (JSON array will be appended)
    items: list of dicts to send as JSON data
    checkpoint_fn: optional callback(results_so_far) called after each batch completes
    Returns: list of parsed JSON dicts from all batches
    """
    def call(batch):
        full_prompt = prompt + json.dumps(batch, ensure_ascii=False)
        for attempt in range(3):
            try:
                out = call_glm(full_prompt, timeout=timeout)
                s, e = out.find("["), out.rfind("]")
                if s >= 0 and e > s:
                    return json.loads(out[s:e + 1])
            except Exception:
                pass
        print(f"  [WARN] batch failed after 3 retries ({len(batch)} items)")
        return None

    results = []
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append([dict(items[i + j]) for j in range(min(batch_size, len(items) - i))])

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(call, b): b for b in batches}
        for f in as_completed(futures):
            try:
                r = f.result()
                if r:
                    results.extend(r)
                    if checkpoint_fn:
                        checkpoint_fn(list(results))  # snapshot to avoid concurrent mutation
            except Exception as e:
                print(f"  [WARN] batch exception: {e}")
    return results


def call_glm_json(prompt, system_msg="你是资深产业信息编辑。直接输出JSON，不要输出思考过程和markdown标记。",
                  model="glm-5.2", max_tokens=8192, temperature=0.4, timeout=240):
    """Single LLM call that expects JSON output. Returns parsed dict or None."""
    try:
        out = call_glm(prompt, system_msg=system_msg, model=model,
                       max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        # Try to find JSON object in output
        s = out.find("{")
        e = out.rfind("}")
        if s >= 0 and e > s:
            return json.loads(out[s:e + 1])
    except Exception as e:
        print(f"  [WARN] GLM JSON parse error: {e}")
    return None
