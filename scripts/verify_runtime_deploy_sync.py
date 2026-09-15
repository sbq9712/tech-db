#!/usr/bin/env python3
"""RT101 deployment identity contract (V9 postmortem, owner directive §1-§2).

Postmortem (owner-v9-formal-20260915T070020Z, SCORER_INTEGRITY_VIOLATION,
consumed): the corpus-compatibility gate binds corpus/model/manifest bytes
exactly, but nothing verified that the SERVING runtime checkout carried the
evaluated code. The formal server's repo mirror sat at the V8-era head
(2a4c802) while the candidate was evaluated at fb43db1 — so committed
zero-claim repairs were never loaded into the serving process.

DEPLOYMENT IDENTITY CONTRACT (owner directive: bind CODE + CORPUS + MODEL
+ CONFIG; never corpus/model/manifest alone):

    A formal evaluation is only valid when the serving runtime process is
    running the EXACT evaluated identity:

      [GIT]    serving_runtime_git_sha == evaluated_git_sha
      [CODE]   every critical runtime file byte-identical (source vs mirror)
      [CORPUS] runtime store sha + dataset_snapshot_id +
               source_snapshot_catalog_id == candidate pin
      [MODEL]  serving start script's LAST `export ZAI_MODEL=` == pin model
      [CONFIG] serving profile (QA_PIPELINE_PROFILE, last export wins) and
               citation schema version measured from the MIRROR code == pin

    Any single mismatch is SERVING_RUNTIME_DRIFT → formal capture forbidden.

This guard never mutates anything. It exits
  0  synced (or no serving mirror on this host AND --require-mirror absent
     — CI/agent-only hosts keep the host-scoped skip)
  2  SERVING_RUNTIME_DRIFT — formal capture FORBIDDEN (pre-seal abort)
  3  usage error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# Repair-critical runtime files (V8/V9 postmortem surface + owner §1 list).
# Deliberately explicit: answer/claim/verify/generate/parse/package/serve/
# retrieval paths whose stale copies can produce unscoreable or dishonest
# payloads. phase02_pipeline.py is the V8/V9 defect emitter — kept locked.
CRITICAL_FILES = (
    "qa-backend/answer_status.py",
    "qa-backend/claim_mapping.py",
    "qa-backend/verifier.py",
    "qa-backend/generator_input.py",
    "qa-backend/llm_json.py",
    "qa-backend/evidence_package.py",
    "qa-backend/server.py",
    "qa-backend/retrieval/runtime.py",
    "qa-backend/phase02_pipeline.py",
)

DEFAULT_MIRROR = "/home/rhett/rt101-v5-formal-runner/repo"
DEFAULT_RUNTIME_BASE = "/home/rhett/rt101-v5-formal-runner"
DRIFT = "SERVING_RUNTIME_DRIFT"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def source_catalog_digest(rows) -> str:
    """Mirror of qa-backend.corpus_compatibility.source_catalog_digest.

    Same canonical identity projection (source_snapshot_id, record_id,
    content_hash), same sort, same digest — so this guard measures exactly
    the identity the corpus gate pins, without importing repo code.
    """
    identity = []
    for row in rows:
        identity.append({
            "source_snapshot_id": str(row.get("source_snapshot_id", "")),
            "record_id": str(row.get("record_id", "")),
            "content_hash": str(row.get("content_hash")
                                if row.get("content_hash") is not None
                                else row.get("evidence_text_sha256", "")),
        })
    identity.sort(key=lambda r: (r["source_snapshot_id"], r["record_id"],
                                 r["content_hash"]))
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def last_export(script: Path, var: str) -> str | None:
    """LAST `export VAR=value` in a start script (V5 formal semantics:
    ZAI_MODEL re-exported LAST so it MUST win)."""
    if not script.is_file():
        return None
    val = None
    for line in script.read_text(errors="replace").splitlines():
        m = re.match(rf"\s*(?:export\s+)?{re.escape(var)}=(\S+)", line)
        if m:
            val = m.group(1).strip("'\"")
    return val


def measure_runtime_store(base: Path) -> dict:
    """Measure the LIVE runtime store binding (corpus identity) exactly the
    way the formal corpus-compatibility gate does."""
    out = {}
    snap_file = base / "runtime" / "indexes" / "source_snapshots"
    rmap_file = base / "runtime" / "state" / "record_id_map.json"
    if snap_file.is_file():
        out["corpus_sha256"] = hashlib.sha256(
            snap_file.read_bytes()).hexdigest()
    if rmap_file.is_file():
        rmap = json.loads(rmap_file.read_text())
        out["dataset_snapshot_id"] = str(rmap.get("dataset_snapshot_id", ""))
    db = snap_file
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = [{"source_snapshot_id": r[0], "record_id": r[1],
                     "content_hash": r[2]}
                    for r in con.execute(
                        "SELECT source_snapshot_id, record_id, content_hash "
                        "FROM snapshots")]
            con.close()
            out["source_snapshot_catalog_id"] = source_catalog_digest(rows)
        except sqlite3.Error:
            pass
    return out


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-repo", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--runtime-repo", default=os.environ.get(
        "RT101_RUNTIME_REPO", DEFAULT_MIRROR),
        help="serving checkout (git mirror) of the evaluated repo")
    ap.add_argument("--runtime-base", default=os.environ.get(
        "RT101_RUNTIME_BASE", DEFAULT_RUNTIME_BASE),
        help="formal workspace holding runtime/ and start_server.sh")
    ap.add_argument("--expected-head", default=None,
                    help="evaluated_git_sha; default = source repo HEAD")
    ap.add_argument("--pin", default=None,
                    help="candidate corpus_pinning.json — enables the "
                         "CORPUS/MODEL/CONFIG legs of the 4-way binding")
    ap.add_argument("--require-mirror", action="store_true",
                    help="formal mode: absent mirror is DRIFT, not skip")
    ap.add_argument("--json-out", default=None,
                    help="optional path for the structured report")
    args = ap.parse_args(argv)

    src = Path(args.source_repo).resolve()
    mirror = Path(args.runtime_repo).resolve()
    base = Path(args.runtime_base).resolve()

    if not (src / "qa-backend/server.py").is_file():
        print(f"source repo invalid: {src}", file=sys.stderr)
        return 3

    expected_head = args.expected_head
    if expected_head is None:
        expected_head = git_head(src)
        if not expected_head:
            print("cannot determine expected head", file=sys.stderr)
            return 3

    pin = None
    if args.pin:
        pin = json.loads(Path(args.pin).read_text())

    mirror_present = ((mirror / ".git").is_dir()
                      and (mirror / "qa-backend").is_dir())
    if not mirror_present:
        if args.require_mirror:
            report = {"schema_version": "rt101-deploy-sync-2.0",
                      "checked": True, "mirror_present": False,
                      "synced": False, "verdict": DRIFT,
                      "drift": ["serving runtime mirror absent on formal "
                                "host — cannot prove deploy identity"],
                      "expected_head": expected_head}
            if args.json_out:
                Path(args.json_out).write_text(
                    json.dumps(report, indent=1, sort_keys=True))
            print(f"FAIL_CLOSED {DRIFT}: serving runtime mirror absent — "
                  "formal capture forbidden", file=sys.stderr)
            return 2
        # No serving runtime on this host (CI / agent-only): nothing to
        # enforce — the guard's contract is host-scoped by design.
        report = {"schema_version": "rt101-deploy-sync-2.0",
                  "checked": False, "mirror_present": False,
                  "reason": "no serving runtime mirror on this host",
                  "runtime_repo": str(mirror), "synced": True,
                  "critical_files": {}}
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(report, indent=1, sort_keys=True))
        print("deploy-sync: no serving runtime mirror (host-scoped skip)")
        return 0

    drift, detail = [], {}

    # ── [GIT] serving git identity ────────────────────────────────────────
    serving_head = git_head(mirror)
    detail["serving_runtime_git_sha"] = serving_head
    detail["evaluated_git_sha"] = expected_head
    if serving_head != expected_head:
        drift.append(
            f"serving_runtime_git_sha {serving_head} != evaluated_git_sha "
            f"{expected_head}")

    # ── [CODE] critical files byte-identity ───────────────────────────────
    files = {}
    for rel in CRITICAL_FILES:
        a, b = src / rel, mirror / rel
        if not a.is_file() or not b.is_file():
            drift.append(f"{rel}: missing in "
                         f"{'serving' if not b.is_file() else 'source'} "
                         "checkout")
            files[rel] = {"source": None, "serving": None, "equal": False}
            continue
        sa, sb = sha256_file(a), sha256_file(b)
        equal = sa == sb
        files[rel] = {"source": sa, "serving": sb, "equal": equal}
        if not equal:
            drift.append(f"{rel}: serving checkout is STALE")
    detail["critical_files"] = files

    # ── [CORPUS/MODEL/CONFIG] 4-way binding against candidate pin ─────────
    if pin is not None:
        # CORPUS: measure the LIVE runtime store under the formal base
        measured = measure_runtime_store(base)
        detail["runtime_store_measured"] = measured
        for key in ("corpus_sha256", "dataset_snapshot_id",
                    "source_snapshot_catalog_id"):
            want = pin.get(key) if key != "corpus_sha256" \
                else pin.get("source_snapshot_store_sha256")
            got = measured.get(key)
            if want and got != want:
                drift.append(f"corpus binding {key}: serving {got} != "
                             f"pinned {want}")
        # MODEL: serving start script's LAST ZAI_MODEL export must win
        start = base / "start_server.sh"
        serving_model = last_export(start, "ZAI_MODEL")
        detail["serving_model"] = serving_model
        if serving_model != pin.get("model"):
            drift.append(f"model binding: serving ZAI_MODEL="
                         f"{serving_model} != pinned {pin.get('model')}")
        # CONFIG: serving profile + citation schema measured mirror-side
        serving_profile = last_export(start, "QA_PIPELINE_PROFILE") \
            or last_export(start, "TECH_DB_RUNTIME_MODE")
        want_profile = pin.get("profile")
        if not want_profile:
            want_profile = (pin.get("prompt_schema_config_versions") or {}) \
                .get("runtime_profile")
        detail["serving_profile"] = serving_profile
        if serving_profile != want_profile:
            drift.append(f"config binding: serving profile "
                         f"{serving_profile} != pinned {want_profile}")
        versions = pin.get("prompt_schema_config_versions") or {}
        want_cit = versions.get("citation_schema_version")
        if want_cit:
            pp = mirror / "qa-backend" / "phase02_pipeline.py"
            m = re.search(r'^CITATION_SCHEMA_VERSION\s*=\s*"([^"]+)"',
                          pp.read_text(errors="replace"), re.M) \
                if pp.is_file() else None
            got_cit = m.group(1) if m else None
            detail["serving_citation_schema_version"] = got_cit
            if got_cit != want_cit:
                drift.append(f"config binding: mirror citation_schema_"
                             f"version {got_cit} != pinned {want_cit}")

    report = {"schema_version": "rt101-deploy-sync-2.0",
              "checked": True, "mirror_present": True,
              "source_repo": str(src), "runtime_repo": str(mirror),
              "runtime_base": str(base),
              "evaluated_git_sha": expected_head,
              "serving_runtime_git_sha": serving_head,
              "pin": bool(pin),
              "synced": not drift, "verdict": "SYNCED" if not drift else DRIFT,
              "drift": drift, "critical_files": files, "detail": detail}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1,
                                                  sort_keys=True))
    if drift:
        print(f"FAIL_CLOSED {DRIFT} — formal capture forbidden",
              file=sys.stderr)
        for d in drift:
            print(f"  - {d}", file=sys.stderr)
        return 2
    legs = "GIT+CODE" + ("+CORPUS+MODEL+CONFIG" if pin else "")
    print(f"deploy-sync: {DRIFT} absent — {legs} bound "
          f"(head {expected_head[:12]}, {len(CRITICAL_FILES)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
