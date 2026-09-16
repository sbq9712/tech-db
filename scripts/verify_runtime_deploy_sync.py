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
import glob
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
    "qa-backend/phase03_pipeline.py",
    # behavior-bearing surfaces named by the codex Review-A sweep:
    "qa-backend/config.py",
    "qa-backend/feature_flags.py",
    "qa-backend/citation_grounding.py",
    "qa-backend/answer_repair.py",
    "qa-backend/numeric_facts.py",
    "qa-backend/runtime_safety.py",
    # §F additions: gate instrumentation whose bytes decide capture
    # admissibility (single source: runtime_identity.py — keep both lists
    # in lockstep; the live leg cross-checks order + count)
    "qa-backend/corpus_compatibility.py",
    "qa-backend/formal_preflight.py",
)

DEFAULT_MIRROR = "/home/rhett/rt101-v5-formal-runner/repo"
DEFAULT_RUNTIME_BASE = "/home/rhett/rt101-v5-formal-runner"
DRIFT = "SERVING_RUNTIME_DRIFT"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load_runtime_identity_mod(repo: Path):
    """Import the canonical identity module from the EVALUATED repo tree.

    The expected digest is computed with the same canonical algorithm the
    evaluated HEAD ships — never from a drifted copy.
    """
    qb = str(repo / "qa-backend")
    if qb not in sys.path:
        sys.path.insert(0, qb)
    import importlib
    return importlib.import_module("runtime_identity")


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


def _listener_pid(port: int, proc_root: str = "/proc") -> int | None:
    """PID of the process listening on 127.0.0.1:port (procfs scan)."""
    hexport = "%04X" % port
    inodes = set()
    for tcp in ("tcp", "tcp6"):
        try:
            lines = open(f"{proc_root}/net/{tcp}").read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state = parts[1], parts[3]
            if local.endswith(":" + hexport) and state == "0A":
                inodes.add(parts[9])
    for fd in glob.glob(f"{proc_root}/[0-9]*/fd/*"):
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        m = re.match(r"socket:\[(\d+)\]", link)
        if m and m.group(1) in inodes:
            # fd path: <proc_root>/<pid>/fd/<n> — pid is the SECOND-from-
            # top component, not index 2 (proc_root may be multi-segment).
            parts = fd.split("/")
            return int(parts[-3])
    return None


def _probe_live_identity(endpoint: str, base: Path, mirror: Path,
                         *, proc_root: str = "/proc",
                         timeout_s: float = 20.0) -> tuple[dict | None,
                                                           int | None,
                                                           dict]:
    """Fetch /api/runtime_identity and bind it to the listener process.

    Returns (identity_or_None, listener_pid_or_None, process_info).
    Any transport/format failure yields identity None (fail closed).
    """
    import urllib.error
    import urllib.request
    live = None
    try:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/api/runtime_identity",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None, None, {}
            live = json.loads(resp.read().decode("utf-8"))
        if not isinstance(live, dict) \
                or live.get("schema_version") is None:
            live = None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        live = None
    pid = None
    proc_info: dict = {}
    try:
        port = int(endpoint.rstrip("/").rsplit(":", 1)[1])
        pid = _listener_pid(port, proc_root=proc_root)
    except (ValueError, IndexError):
        pid = None
    if pid is not None:
        procp = Path(proc_root) / str(pid)
        try:
            cwd = os.readlink(procp / "cwd")
        except OSError:
            cwd = None
        try:
            cmdline = (procp / "cmdline").read_bytes().replace(
                b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        module_path = None
        m = re.search(r"(/[^ ]*qa-backend/server\.py)", cmdline)
        if m:
            module_path = m.group(1)
        else:
            try:
                module_path = os.readlink(procp / "exe")
            except OSError:
                module_path = None
        alive = (procp / "stat").exists()
        proc_info = {"cwd": cwd, "cmdline": cmdline[:300],
                     "module_path": module_path, "alive": alive}
    # TOCTOU closure (codex P1-1): re-fetch the identity and re-sample the
    # listener AFTER the procfs read. Both samples must agree on pid and
    # on the git_sha/code_digest, and the payload's self-reported pid must
    # equal the discovered listener — else the evidence is split across
    # two processes and the caller must fail closed.
    live2 = None
    pid2 = None
    try:
        req2 = urllib.request.Request(
            endpoint.rstrip("/") + "/api/runtime_identity",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req2, timeout=timeout_s) as resp2:
            if resp2.status == 200:
                live2 = json.loads(resp2.read().decode("utf-8"))
        port2 = int(endpoint.rstrip("/").rsplit(":", 1)[1])
        pid2 = _listener_pid(port2, proc_root=proc_root)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        live2 = None
        pid2 = None
    if live is not None:
        if live2 is None or pid2 != pid:
            return None, pid, proc_info  # unstable evidence — fail closed
        for key in ("git_sha", "runtime_code_digest", "pid", "service_role"):
            if live2.get(key) != live.get(key):
                return None, pid, proc_info  # mutated mid-proof
    if live is not None and live.get("pid") != pid:
        # payload pid must be the procfs-discovered listener pid
        return None, pid, proc_info
    return live, pid, proc_info


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
    ap.add_argument("--endpoint", default=None,
                    help="FORMAL_RUNTIME_ENDPOINT (single source of truth); "
                         "required with --require-live")
    ap.add_argument("--require-live", action="store_true",
                    help="formal mode: verify the RUNNING server process "
                         "identity via /api/runtime_identity (loaded code, "
                         "not disk) — absent endpoint is DRIFT")
    ap.add_argument("--expected-role", default="RT101_FORMAL",
                    help="required service_role from the live identity")
    ap.add_argument("--proc-root", default="/proc",
                    help="proc filesystem root (test seam)")
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
    if args.require_mirror and pin is None:
        # formal mode MUST bind all four legs; a pinless formal invocation
        # would silently reduce the contract to GIT+CODE (codex Review-A P0)
        print("FAIL_CLOSED: --require-mirror (formal mode) requires --pin "
              "(4-way CODE+CORPUS+MODEL+CONFIG binding)", file=sys.stderr)
        return 3

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
            if not want:
                drift.append(f"corpus binding {key}: pin value missing "
                             f"(malformed pin — fail closed)")
            elif got is None:
                drift.append(f"corpus binding {key}: serving store "
                             f"unreadable/missing — fail closed")
            elif got != want:
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
        serving_profile = last_export(start, "QA_PIPELINE_PROFILE")
        want_profile = pin.get("profile")
        if not want_profile:
            want_profile = (pin.get("prompt_schema_config_versions") or {}) \
                .get("runtime_profile")
        detail["serving_profile"] = serving_profile
        if serving_profile != want_profile:
            drift.append(f"config binding: serving profile "
                         f"{serving_profile} != pinned {want_profile}")
        # MANIFEST: the serving mirror's pinned mini-runtime manifest
        # (qa-backend/test_fixtures/mini_runtime/manifest.json fixture_id)
        # is the serving-side manifest identity the canonical benchmark
        # binds (build_provenance manifest_id). Missing/unreadable = DRIFT
        # (fail closed), never silently pass.
        want_manifest = pin.get("manifest_id")
        got_manifest = None
        mf = mirror / "qa-backend" / "test_fixtures" / "mini_runtime" \
            / "manifest.json"
        if mf.is_file():
            try:
                got_manifest = str(json.loads(
                    mf.read_text()).get("fixture_id") or "")
            except (ValueError, OSError):
                got_manifest = None
        detail["serving_manifest_id"] = got_manifest
        if want_manifest and got_manifest != want_manifest:
            drift.append(f"manifest binding: mirror mini-runtime "
                         f"fixture_id={got_manifest} != pinned "
                         f"{want_manifest}")
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

    # ── [LIVE] running-process identity (loaded code, not disk) ───────────
    # V9 postmortem follow-up (owner directive §C/§D): a synced mirror does
    # NOT imply the serving PROCESS imported the synced bytes. The live leg
    # queries the server's own /api/runtime_identity (computed at import
    # from the loaded modules' bytes) and fails closed unless the RUNNING
    # identity matches the evaluated HEAD, the mirror-computed canonical
    # code digest, the pinned corpus/model/manifest, and the formal
    # service role — plus process-level binding (listener PID, cwd,
    # module path).
    if args.require_live and pin is None:
        print(f"FAIL_CLOSED {DRIFT} — --require-live requires --pin "
              "(live corpus/model/config binding undefined without it)",
              file=sys.stderr)
        return 2
    if args.require_live:
        try:
            ri = _load_runtime_identity_mod(src)
            if tuple(ri.CRITICAL_FILES) != tuple(CRITICAL_FILES):
                print(f"FAIL_CLOSED {DRIFT} — critical-file list out of "
                      "lockstep between guard and runtime_identity.py",
                      file=sys.stderr)
                return 2
        except ImportError as exc:
            print(f"FAIL_CLOSED {DRIFT} — evaluated repo lacks the "
                  f"canonical runtime identity module: {exc}",
                  file=sys.stderr)
            return 2
        ep = args.endpoint
        if not ep:
            drift.append("live leg: --endpoint (FORMAL_RUNTIME_ENDPOINT) "
                         "missing — fail closed")
        elif not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{2,5}", ep):
            drift.append(f"live leg: endpoint {ep!r} malformed (must be "
                         "http://127.0.0.1:<port>)")
        else:
            live, pid, proc_info = _probe_live_identity(
                ep, base, mirror, proc_root=args.proc_root)
            detail["live"] = {
                "endpoint": ep, "identity": live, "pid": pid,
                "process": proc_info,
                "expected_code_digest": ri.compute_code_digest(mirror)[0],
                "expected_role": args.expected_role,
            }
            if live is None:
                drift.append("live leg: /api/runtime_identity unreachable, "
                             "malformed, or non-200 — fail closed "
                             "(false-positive health server suspected)")
            else:
                # role: the listener must self-declare the formal role
                got_role = live.get("service_role")
                if got_role != args.expected_role:
                    drift.append(f"live role: service_role={got_role!r} != "
                                 f"{args.expected_role!r} (foreign or "
                                 "undeclared server on the formal endpoint)")
                # git sha of the RUNNING process
                if live.get("git_sha") != expected_head:
                    drift.append(f"live git: running server git_sha="
                                 f"{live.get('git_sha')} != evaluated head "
                                 f"{expected_head} (stale import)")
                # canonical code digest of the LOADED tree
                want_digest = ri.compute_code_digest(mirror)[0]
                got_digest = live.get("runtime_code_digest")
                if not got_digest or got_digest != want_digest:
                    drift.append(f"live code: running runtime_code_digest="
                                 f"{str(got_digest)[:16]} != mirror-computed "
                                 f"{str(want_digest)[:16]} (loaded bytes "
                                 "diverge from evaluated HEAD)")
                if live.get("critical_file_count") != len(ri.CRITICAL_FILES):
                    drift.append("live code: critical_file_count "
                                 f"{live.get('critical_file_count')} != "
                                 f"{len(ri.CRITICAL_FILES)} (identity "
                                 "contract version drift)")
                # model/corpus/manifest as the RUNNING process saw them;
                # profile + citation schema come from the LOADED code
                # (feature_flags.active_profile / imported constant), not
                # from env text or script parsing (codex P1-4)
                if pin is not None:
                    want_profile = pin.get("profile") or \
                        (pin.get("prompt_schema_config_versions") or {}) \
                        .get("runtime_profile")
                    if want_profile and \
                            live.get("profile") != want_profile:
                        drift.append(f"live config: running profile "
                                     f"{live.get('profile')!r} != pinned "
                                     f"{want_profile!r}")
                    versions = pin.get("prompt_schema_config_versions") or {}
                    want_cit = versions.get("citation_schema_version")
                    if want_cit and live.get("citation_schema_version") != \
                            want_cit:
                        drift.append(f"live config: running citation_"
                                     f"schema_version "
                                     f"{live.get('citation_schema_version')!r} "
                                     f"!= pinned {want_cit!r}")
                    if live.get("model") != pin.get("model"):
                        drift.append(f"live model: running ZAI_MODEL="
                                     f"{live.get('model')} != pinned "
                                     f"{pin.get('model')}")
                    if live.get("corpus_store_sha256") != \
                            pin.get("source_snapshot_store_sha256"):
                        drift.append("live corpus: running store sha "
                                     f"{str(live.get('corpus_store_sha256'))[:16]}"
                                     " != pinned "
                                     f"{str(pin.get('source_snapshot_store_sha256'))[:16]}")
                    if live.get("corpus_manifest") != pin.get("manifest_id"):
                        drift.append(f"live manifest: running "
                                     f"{live.get('corpus_manifest')} != pinned "
                                     f"{pin.get('manifest_id')}")
                # process binding: the listener PID must be a live process
                # whose cwd is under the formal runtime base and whose
                # module path is inside the serving mirror
                if pid is None:
                    drift.append(f"live process: no listener PID found for "
                                 f"{ep} in {args.proc_root} — fail closed")
                else:
                    cwd = (proc_info or {}).get("cwd")
                    if cwd is None or not str(cwd).startswith(str(base)):
                        drift.append(f"live process: pid {pid} cwd={cwd} "
                                     f"not under runtime base {base}")
                    # Loaded-tree proof is carried by the code digest; the
                    # path check binds the PROCESS to the serving mirror via
                    # cwd (start contract: the server cd's to the mirror
                    # before import) or an explicit mirror path in cmdline.
                    modpath = (proc_info or {}).get("module_path") or ""
                    cmd = (proc_info or {}).get("cmdline") or ""
                    if not (str(cwd).startswith(str(mirror))
                            or str(modpath).startswith(str(mirror))
                            or str(mirror) in cmd):
                        drift.append(f"live process: pid {pid} (cwd={cwd}, "
                                     f"module={modpath}) not bound to "
                                     f"serving mirror {mirror}")
                    if not (proc_info or {}).get("alive"):
                        drift.append(f"live process: pid {pid} not alive "
                                     "— fail closed")

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
    legs = "GIT+CODE" + ("+CORPUS+MODEL+CONFIG" if pin else "") \
        + ("+LIVE-PROCESS" if args.require_live else "")
    print(f"deploy-sync: {DRIFT} absent — {legs} bound "
          f"(head {expected_head[:12]}, {len(CRITICAL_FILES)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
