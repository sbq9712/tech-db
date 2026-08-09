#!/usr/bin/env python3
"""
Shared LLM client for tech-db pipeline.
Replaces all `hermes` CLI calls with direct ZAI (GLM-5.2) API calls.
Works both locally and in GitHub Actions.
"""
import json, os, time, urllib.request
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


def call_glm_batch(prompt, items, batch_size=10, timeout=120, max_workers=5,
                   checkpoint_fn=None, max_consecutive_failures=20,
                   progress_every=50, backoff_delays=(0, 2, 4)):
    """
    Batch LLM calls for classification/scoring/summary.
    prompt: instruction prompt (JSON array will be appended)
    items: list of dicts to send as JSON data
    checkpoint_fn: optional callback(results_so_far) called after each batch completes

    Resilience:
      - exponential backoff between the per-batch retries (rides out transient rate-limits)
      - circuit breaker: after `max_consecutive_failures` batches fail in a row, cancel
        remaining batches and return partial results (only trips on persistent API outage)
      - progress logging every `progress_every` completed batches
    Returns: list of parsed JSON dicts from all completed batches
    """
    def call(batch):
        full_prompt = prompt + json.dumps(batch, ensure_ascii=False)
        for attempt, delay in enumerate(backoff_delays):
            if delay:
                time.sleep(delay)
            try:
                out = call_glm(full_prompt, timeout=timeout)
                s, e = out.find("["), out.rfind("]")
                if s >= 0 and e > s:
                    return json.loads(out[s:e + 1])
            except Exception:
                pass
        print(f"  [WARN] batch failed after {len(backoff_delays)} retries ({len(batch)} items)")
        return None

    results = []
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append([dict(items[i + j]) for j in range(min(batch_size, len(items) - i))])

    total_batches = len(batches)
    completed = 0
    consecutive_failures = 0
    aborted = False
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(call, b): b for b in batches}
        for f in as_completed(futures):
            completed += 1
            try:
                r = f.result()
                if r:
                    results.extend(r)
                    if checkpoint_fn:
                        checkpoint_fn(list(results))  # snapshot to avoid concurrent mutation
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            except Exception as e:
                print(f"  [WARN] batch exception: {e}")
                consecutive_failures += 1

            if completed % progress_every == 0:
                print(f"    进度: {completed}/{total_batches} 批, {len(results)}/{len(items)} 条 (连续失败 {consecutive_failures})")

            # Circuit breaker: only trips when the API is persistently failing
            if consecutive_failures >= max_consecutive_failures and not aborted:
                aborted = True
                print(f"  [ABORT] 连续 {max_consecutive_failures} 批失败，取消剩余 {total_batches - completed} 批，返回已完成的 {len(results)} 条")
                for ff in futures:
                    ff.cancel()
                break
    if aborted:
        print(f"  [ABORT] call_glm_batch 提前结束：完成 {completed}/{total_batches} 批，结果 {len(results)} 条")
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
