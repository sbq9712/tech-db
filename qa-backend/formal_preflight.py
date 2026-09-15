"""Formal pre-seal provider preflight (RT101 Phase09, Codex Cluster B P1-2).

Root finding: the provider health/latency logic lived only as inline shell
in the owner-side runner, untestable against failure classes.  This module
is the testable extraction — the runner call site now executes exactly the
code these tests exercise.

Contract (fail closed, pre-seal):
  * ``check_health``      — formal server /api/health must answer 200.
  * ``probe_latency``     — a tiny DIRECT upstream chat completion (same
    credential route, same model, same timeout class as formal traffic;
    thinking disabled; max_tokens bounded; content never parsed) must
    answer HTTP 200 within the latency budget.  The response ``model``
    field must equal the requested model — a gateway silently serving a
    different model is a probe failure, not a pass.
  * ``readiness_soak``    — a bounded sequence of consecutive successful
    probes (no infinite waiting) proves the provider is stably ready
    immediately before the one-shot marker is sealed.
  * ``run_preflight``     — health + probe + soak; returns a structured
    report; every failure is a precondition abort (never a semantic
    verdict, never a retry).  Callers must abort BEFORE creating any
    one-shot marker when this reports ok=False.

This module is side-effect-free: it never writes files, never creates
markers, never mutates state.  It never prints or logs the API key.
Stdlib only.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

PROBE_SCHEMA_VERSION = "rt101-formal-preflight-1.0"
DEFAULT_PROBE_TIMEOUT_S = 45
DEFAULT_LATENCY_BUDGET_S = 40
DEFAULT_SOAK_ITERATIONS = 3
DEFAULT_SOAK_INTERVAL_S = 0.2
DEFAULT_HEALTH_TIMEOUT_S = 10


def _probe_body(model: str) -> bytes:
    """Canonical minimal probe request: structural only, thinking off."""
    return json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": "Reply with the single word: ok"}],
        "temperature": 0,
        "max_tokens": 256,
        "thinking": {"type": "disabled"},
    }).encode("utf-8")


def probe_latency(base_url: str, model: str, *, api_key: str = "",
                  timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
                  latency_budget_s: float = DEFAULT_LATENCY_BUDGET_S,
                  require_model_echo: bool = True) -> dict:
    """One bounded direct-upstream completion. Never retries.

    Returns {"ok", "http_status", "latency_s", "error_class", "detail"}.
    Failure classes: unreachable, http_error, timeout, slow, model_mismatch,
    malformed_response.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url, data=_probe_body(model), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"}
                    if api_key else {})})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_status": e.code, "latency_s": round(
                    time.monotonic() - t0, 3),
                "error_class": "http_error", "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "http_status": None, "latency_s": round(
                    time.monotonic() - t0, 3),
                "error_class": "unreachable",
                "detail": str(e)[:200]}
    latency = time.monotonic() - t0
    if status != 200:
        return {"ok": False, "http_status": status,
                "latency_s": round(latency, 3),
                "error_class": "http_error", "detail": f"HTTP {status}"}
    if latency > latency_budget_s:
        return {"ok": False, "http_status": status,
                "latency_s": round(latency, 3), "error_class": "slow",
                "detail": f"{latency:.1f}s exceeds {latency_budget_s}s budget"}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "http_status": status,
                "latency_s": round(latency, 3),
                "error_class": "malformed_response",
                "detail": "body is not JSON"}
    served = body.get("model") if isinstance(body, dict) else None
    if require_model_echo:
        if not isinstance(served, str) or served != model:
            return {"ok": False, "http_status": status,
                    "latency_s": round(latency, 3),
                    "error_class": "model_mismatch",
                    "detail": f"served={served!r} requested={model!r}"}
    return {"ok": True, "http_status": status, "latency_s": round(latency, 3),
            "error_class": None, "detail": "ok"}


def check_health(base_url: str, *,
                 timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S) -> dict:
    """Formal server readiness (/api/health, HTTP 200)."""
    url = f"{base_url.rstrip('/')}/api/health"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            status = resp.status
            resp.read()
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_status": e.code,
                "latency_s": round(time.monotonic() - t0, 3),
                "error_class": "http_error", "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "http_status": None,
                "latency_s": round(time.monotonic() - t0, 3),
                "error_class": "unreachable", "detail": str(e)[:200]}
    return {"ok": status == 200, "http_status": status,
            "latency_s": round(time.monotonic() - t0, 3),
            "error_class": None if status == 200 else "http_error",
            "detail": f"HTTP {status}"}


def readiness_soak(base_url: str, model: str, *, api_key: str = "",
                   iterations: int = DEFAULT_SOAK_ITERATIONS,
                   interval_s: float = DEFAULT_SOAK_INTERVAL_S,
                   timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
                   latency_budget_s: float = DEFAULT_LATENCY_BUDGET_S,
                   ) -> dict:
    """Bounded consecutive-success soak (no infinite waiting).

    All ``iterations`` probes must succeed back-to-back; any failure or
    slow probe fails the soak immediately.
    """
    latencies = []
    for i in range(max(1, int(iterations))):
        r = probe_latency(base_url, model, api_key=api_key,
                          timeout_s=timeout_s,
                          latency_budget_s=latency_budget_s)
        if not r["ok"]:
            return {"ok": False, "iterations_required": int(iterations),
                    "iterations_passed": i, "latencies": latencies,
                    "failed_probe": r}
        latencies.append(r["latency_s"])
        if i < int(iterations) - 1 and interval_s > 0:
            time.sleep(interval_s)
    return {"ok": True, "iterations_required": int(iterations),
            "iterations_passed": int(iterations), "latencies": latencies,
            "failed_probe": None}


def run_preflight(*, formal_server_url: str, provider_base_url: str,
                  model: str, api_key: str = "",
                  latency_budget_s: float = DEFAULT_LATENCY_BUDGET_S,
                  probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
                  soak_iterations: int = DEFAULT_SOAK_ITERATIONS,
                  soak_interval_s: float = DEFAULT_SOAK_INTERVAL_S) -> dict:
    """Full pre-seal preflight: server health + bounded readiness soak.

    Returns a structured report; ``ok=False`` means the caller must abort
    BEFORE sealing the one-shot marker (precondition abort — never a
    semantic verdict, never a retry).
    """
    health = check_health(formal_server_url)
    if not health["ok"]:
        return {"schema_version": PROBE_SCHEMA_VERSION, "ok": False,
                "stage": "health", "health": health, "soak": None}
    soak = readiness_soak(provider_base_url, model, api_key=api_key,
                          iterations=soak_iterations,
                          interval_s=soak_interval_s,
                          timeout_s=probe_timeout_s,
                          latency_budget_s=latency_budget_s)
    return {"schema_version": PROBE_SCHEMA_VERSION, "ok": soak["ok"],
            "stage": "soak" if soak["ok"] else "soak_failed",
            "health": health, "soak": soak}


def main(argv: list | None = None) -> int:
    """CLI: exits 0 (ready) or 2 (pre-seal abort). Never creates files."""
    import argparse
    import os
    import sys
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--formal-server-url", required=True)
    p.add_argument("--provider-base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--api-key-env", default="ZAI_API_KEY",
                   help="env var carrying the provider key (never printed)")
    p.add_argument("--latency-budget-s", type=float,
                   default=DEFAULT_LATENCY_BUDGET_S)
    p.add_argument("--probe-timeout-s", type=float,
                   default=DEFAULT_PROBE_TIMEOUT_S)
    p.add_argument("--soak-iterations", type=int,
                   default=DEFAULT_SOAK_ITERATIONS)
    p.add_argument("--soak-interval-s", type=float,
                   default=DEFAULT_SOAK_INTERVAL_S)
    a = p.parse_args(argv)
    report = run_preflight(
        formal_server_url=a.formal_server_url,
        provider_base_url=a.provider_base_url,
        model=a.model,
        api_key=os.environ.get(a.api_key_env, ""),
        latency_budget_s=a.latency_budget_s,
        probe_timeout_s=a.probe_timeout_s,
        soak_iterations=a.soak_iterations,
        soak_interval_s=a.soak_interval_s)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
