"""Configurable anti-abuse controls and a persistent daily cost fuse."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class GuardrailSettings:
    per_minute: int = env_int("QA_RATE_LIMIT_PER_MINUTE", 3, 1)
    per_client_day: int = env_int("QA_RATE_LIMIT_PER_DAY", 30, 1)
    global_day: int = env_int("QA_GLOBAL_LIMIT_PER_DAY", 300, 1)
    concurrency: int = env_int("QA_MAX_CONCURRENCY", 3, 1)
    daily_budget_usd: float = env_float("QA_DAILY_BUDGET_USD", 0.0)
    estimated_request_cost_usd: float = env_float("QA_ESTIMATED_REQUEST_COST_USD", 0.02)
    admin_key: str = os.environ.get("QA_ADMIN_KEY", "")


class RateLimiter:
    def __init__(self, settings: GuardrailSettings):
        self.settings = settings
        self._lock = threading.Lock()
        self._minute: dict[str, deque[float]] = defaultdict(deque)
        self._daily: dict[tuple[str, str], int] = defaultdict(int)
        self._global_daily: dict[str, int] = defaultdict(int)

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    def check(self, client_id: str, bypass: bool = False) -> tuple[bool, str, int]:
        if bypass:
            return True, "admin bypass", 0
        now = time.time()
        day = self._day_key(now)
        with self._lock:
            recent = self._minute[client_id]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= self.settings.per_minute:
                retry = max(1, int(60 - (now - recent[0])))
                return False, "每分钟请求次数已达上限", retry
            if self._daily[(day, client_id)] >= self.settings.per_client_day:
                return False, "今日请求次数已达上限", 3600
            if self._global_daily[day] >= self.settings.global_day:
                return False, "今日全站请求次数已达上限", 3600
            recent.append(now)
            self._daily[(day, client_id)] += 1
            self._global_daily[day] += 1
        return True, "ok", 0


class BudgetFuse:
    def __init__(self, settings: GuardrailSettings, state_path: Path):
        self.settings = settings
        self.state_path = state_path
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("date") != self._today():
            return {"date": self._today(), "estimated_cost_usd": 0.0, "requests": 0}
        return state

    def reserve(self, bypass: bool = False) -> tuple[bool, float]:
        if bypass or self.settings.daily_budget_usd <= 0:
            return True, 0.0
        with self._lock:
            state = self._load()
            projected = float(state.get("estimated_cost_usd", 0.0)) + self.settings.estimated_request_cost_usd
            if projected > self.settings.daily_budget_usd:
                return False, float(state.get("estimated_cost_usd", 0.0))
            state["estimated_cost_usd"] = round(projected, 6)
            state["requests"] = int(state.get("requests", 0)) + 1
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.state_path)
            return True, projected

    def status(self) -> dict:
        state = self._load()
        return {
            "enabled": self.settings.daily_budget_usd > 0,
            "budget_usd": self.settings.daily_budget_usd,
            "estimated_cost_usd": float(state.get("estimated_cost_usd", 0.0)),
            "requests": int(state.get("requests", 0)),
        }


def admin_bypass(configured_key: str, supplied_key: str | None) -> bool:
    return bool(configured_key and supplied_key and hmac.compare_digest(configured_key, supplied_key))


def client_identifier(headers, fallback: str) -> str:
    """Use Cloudflare's authenticated header first; otherwise use socket IP."""
    return headers.get("cf-connecting-ip") or fallback or "unknown"
