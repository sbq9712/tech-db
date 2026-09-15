#!/usr/bin/env python3
"""RT-075 durable shadow-observation collector tests.

Ticket test requirements: shadow non-interference; report schema;
injected mismatch — applied to the append-only store:
1. non-interference: persistence is opt-in, off by default, and any store
   failure leaves the observe/serving path untouched.
2. store schema: header/record shape, dedupe, origin purity, hash chain,
   torn-tail tolerance, redaction allowlist.
3. qualification verifier: >=1,000 unique events + >=7-day window from
   PERSISTED timestamps; tampering (injected mismatch) fails closed;
   replay/synthetic origins never qualify as production shadow.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from entity_shadow_store import (EntityShadowStore, redact_observation,
                                 ALLOWED_ORIGINS)

sys.path.insert(0, str(HERE.parent / "scripts"))
import verify_rt075_shadow_qualification as verifier

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def _row(n: int, latency: float | None = None, entity_id: str | None = None):
    return {
        "event_id": f"seed-{n:05d}", "source": "query",
        "entity_class": "OTHER_DOMAIN",
        "serving_decision": {"decision": "LEGACY_UNCHANGED",
                             "selected_entity_id": None},
        "shadow_decision": {"decision": "LINK",
                            "selected_entity_id": entity_id or f"ent-{n:05d}",
                            "reason_codes": ["exact_match"]},
        "agreement": False, "false_link_candidate": False,
        "latency_ms": (n * 0.137 + 1.0) if latency is None else latency,
        "candidate_latency_ms": float(n), "adjudicator_latency_ms": 0.0,
        "model_calls": 0, "cost_proxy": 0.0, "cache_hit": bool(n % 2),
        "block_violation": False,
    }


def test_non_interference():
    # 1. default (no env): store machinery absent from the request path
    for var in ("TECH_DB_ENTITY_SHADOW_STORE", "TECH_DB_ENTITY_SHADOW_ORIGIN"):
        os.environ.pop(var, None)
    import server
    check("RT075 store off by default", server._ENTITY_SHADOW_STORE is None)
    # _persist_shadow_observation is a no-op and must not raise
    server._persist_shadow_observation(_row(1))
    check("RT075 persist no-op without env", True)
    # 2. broken store path: observe+persist must not raise
    with tempfile.TemporaryDirectory() as temp:
        os.environ["TECH_DB_ENTITY_SHADOW_STORE"] = \
            str(Path(temp) / "no-such-dir" / "store.jsonl")
        try:
            from entity_shadow_store import default_store_from_env
            store = default_store_from_env()
            check("RT075 env failure yields None store", store is None)
        finally:
            os.environ.pop("TECH_DB_ENTITY_SHADOW_STORE", None)
            server._ENTITY_SHADOW_STORE = None
    # 3. store append failure returns None, does not raise
    s = EntityShadowStore(Path(temp) / "x.jsonl" if False else
                          os.path.join(tempfile.gettempdir(),
                                       "rt075-unwritable-should-not-exist"),
                          origin="real")
    s.path = "/proc/definitely-not-writable/store.jsonl"
    check("RT075 append failure swallowed", s.append(_row(2)) is None)


def test_store_schema_and_dedupe():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "shadow.jsonl"
        store = EntityShadowStore(path, origin="real")
        first = store.append(_row(1))
        check("RT075 header + first record", first is not None
              and first["origin"] == "real"
              and first["record_type"] == "shadow_observation")
        check("RT075 observation redacted to allowlist",
              set(first["observation"].keys()) <= {
                  k for k in redact_observation(_row(1)).keys()})
        # dedupe: identical content rejected
        dup = store.append(_row(1))
        check("RT075 duplicate observation rejected", dup is None
              and store.count == 1)
        # distinct content accepted
        second = store.append(_row(2, latency=4.76))
        check("RT075 distinct observation accepted",
              second is not None and store.count == 2)
        # origin purity: replay into a real store refused
        mixed = store.append(_row(3), origin="replay")
        check("RT075 origin mixing refused", mixed is None
              and store.count == 2)
        # constructor-level origin binding
        try:
            EntityShadowStore(path, origin="synthetic")
            check("RT075 reopen with wrong origin raises", False)
        except ValueError:
            check("RT075 reopen with wrong origin raises", True)
        # chain: each record's prev_line_sha256 == sha256(prev line)
        lines = path.read_text(encoding="utf-8").splitlines()
        import hashlib
        ok = True
        prev = ""
        for line in lines:
            rec = json.loads(line)
            if rec.get("record_type") != "store_header" and \
                    rec.get("prev_line_sha256") != prev:
                ok = False
            prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        check("RT075 hash chain intact", ok and len(lines) == 3)
        # torn tail tolerated: corrupt ONLY the last line
        lines[-1] = lines[-1][:len(lines[-1]) // 2]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        reopened = EntityShadowStore(path, origin="real")
        check("RT075 torn tail tolerated on reopen", reopened.count == 1)


def test_qualification_verifier():
    import entity_shadow_store as ess
    real_now = ess._utc_now_iso

    class Clock:
        def __init__(self, start: datetime, step_seconds: float):
            self.current = start
            self.step = timedelta(seconds=step_seconds)

        def tick(self) -> str:
            value = self.current.isoformat()
            self.current += self.step
            return value

    with tempfile.TemporaryDirectory() as temp:
        # a. failing store: too few events, too short window
        short_path = Path(temp) / "short.jsonl"
        store = EntityShadowStore(short_path, origin="real")
        for n in range(5):
            store.append(_row(n))
        verdict = verifier.verify_store(str(short_path))
        check("RT075 short store not qualified",
              verdict["qualifies_as_production_shadow"] is False
              and verdict["unique_observations"] == 5)
        # b. qualifying store: 1000 unique events spanning >= 7 days
        #    (clock is controlled at the module seam; records stay
        #    chain-intact — no line rewriting)
        qual_path = Path(temp) / "qual.jsonl"
        store = EntityShadowStore(qual_path, origin="real")
        clock = Clock(datetime.fromisoformat(store_path_start(qual_path)),
                      step_seconds=7.1 * 86400 / 1000)
        ess._utc_now_iso = clock.tick
        try:
            for n in range(1000):
                store.append(_row(n))
        finally:
            ess._utc_now_iso = real_now
        verdict = verifier.verify_store(str(qual_path))
        check("RT075 qualifying store passes gate",
              verdict["qualifies_as_production_shadow"] is True
              and verdict["unique_observations"] == 1000
              and verdict["window_days"] >= 7.0,
              json.dumps(verdict["gate"]))
        # c. injected mismatch: tamper one observation -> fail closed
        lines = qual_path.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[2])
        rec["observation"]["shadow_decision"] = {"decision": "NEW"}
        lines[2] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        tampered = Path(temp) / "tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        verdict = verifier.verify_store(str(tampered))
        check("RT075 injected mismatch fails closed",
              verdict["qualifies_as_production_shadow"] is False
              and verdict["tamper_findings"], json.dumps(verdict["gate"]))
        # d. replay origin: gate numbers can pass but production flag false
        replay_path = Path(temp) / "replay.jsonl"
        store = EntityShadowStore(replay_path, origin="replay")
        clock = Clock(datetime.fromisoformat(store_path_start(replay_path)),
                      step_seconds=7.1 * 86400 / 1000)
        ess._utc_now_iso = clock.tick
        try:
            for n in range(1000):
                store.append(_row(n))
        finally:
            ess._utc_now_iso = real_now
        verdict = verifier.verify_store(str(replay_path))
        check("RT075 replay origin never production-qualified",
              verdict["origin"] == "replay"
              and verdict["qualifies_as_production_shadow"] is False)
        # e. duplication gaming: 1000 identical appends -> 1 event
        dup_path = Path(temp) / "dup.jsonl"
        store = EntityShadowStore(dup_path, origin="real")
        for _ in range(1000):
            store.append(_row(1, latency=2.5))
        verdict = verifier.verify_store(str(dup_path))
        check("RT075 duplication cannot inflate count",
              verdict["unique_observations"] == 1
              and verdict["qualifies_as_production_shadow"] is False)


def store_path_start(path) -> str:
    """Read created_at from the store header (ISO timestamp anchor)."""
    first = Path(path).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first)["created_at_utc"]


def main():
    test_non_interference()
    test_store_schema_and_dedupe()
    test_qualification_verifier()
    print("=" * 66)
    print(f"  RT-075 shadow store: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
