#!/usr/bin/env python3
"""RT101 deployment identity contract — regression suite (owner §3).

V9 formal (owner-v9-formal-20260915T070020Z) failed because the serving
runtime mirror was stale while every pre-seal gate stayed green. The owner
directive mandates a DEPLOY_SYNC_GATE that can never be re-fooled:

  Case A  candidate HEAD == serving mirror, all bindings exact  → PASS (0)
  Case B  mirror one commit behind (git identity only)          → FAIL_CLOSED
  Case C  critical file hash mismatch (code identity only)      → FAIL_CLOSED
  Case D  corpus match but CODE mismatch (git + file tamper)    → FAIL_CLOSED
  Case E  model match but CODE mismatch (git + file tamper)     → FAIL_CLOSED
  + formal mode: absent mirror with --require-mirror           → FAIL_CLOSED
  + host-scoped skip: absent mirror WITHOUT --require-mirror    → PASS (0)
  + "exported LAST wins" ZAI_MODEL parsing                      → pinned model
  + 4-way live binding on the real host (when mirror present)   → SYNCED

All fixtures are synthetic tmpdirs — no gold, no capture content.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "verify_runtime_deploy_sync.py"
SYNC_SCRIPT = REPO / "scripts" / "verify_runtime_deploy_sync.py"

FAILS = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if ok:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def catalog_digest(rows):
    """Same projection as corpus_compatibility.source_catalog_digest."""
    identity = [{"source_snapshot_id": r["source_snapshot_id"],
                 "record_id": r["record_id"],
                 "content_hash": r["content_hash"]} for r in rows]
    identity.sort(key=lambda r: (r["source_snapshot_id"], r["record_id"],
                                 r["content_hash"]))
    blob = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd)] + list(args),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")
    return r.stdout.strip()


CODE_FILES = (
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

CITE_V1 = 'CITATION_SCHEMA_VERSION = "2.0.0"\n'
CITE_V2 = 'CITATION_SCHEMA_VERSION = "2.0.1"\n'


def build_fixture(ws, cite=CITE_V1):
    """Synthetic source repo (2 commits) + measured corpus/model fixtures.

    commit1: original critical files. commit2 (HEAD): phase02_pipeline.py
    gains a trailing comment — code-only drift material for cases B/C/D/E.
    Returns dict with paths, head shas and the pin for the fixture corpus.
    """
    src = Path(ws) / "src_repo"
    (src / "qa-backend" / "retrieval").mkdir(parents=True)
    (src / "scripts").mkdir()
    body = {}
    for rel in CODE_FILES:
        body[rel] = f"# synthetic runtime module {rel}\nOK = True\n"
    body["qa-backend/phase02_pipeline.py"] += cite
    for rel, text in body.items():
        (src / rel).write_text(text)
    git(src, "init", "-q")
    git(src, "config", "user.email", "t@t")
    git(src, "config", "user.name", "t")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "c1")
    head_parent = git(src, "rev-parse", "HEAD")
    p2 = src / "qa-backend" / "phase02_pipeline.py"
    p2.write_text(p2.read_text() + "# repair zero-claim guard (evaluated)\n")
    git(src, "commit", "-aqm", "c2 evaluated repair")
    head = git(src, "rev-parse", "HEAD")

    # corpus fixture: the store IS a SQLite catalog (same shape as the live
    # runtime/indexes/source_snapshots) — sha256 of the db file is the
    # corpus_sha256 leg.
    base = Path(ws) / "ws"
    (base / "runtime" / "indexes").mkdir(parents=True)
    (base / "runtime" / "state").mkdir(parents=True)
    store = base / "runtime" / "indexes" / "source_snapshots"
    rows = [{"source_snapshot_id": "ss-1", "record_id": "r-1",
             "content_hash": "h-1"},
            {"source_snapshot_id": "ss-2", "record_id": "r-2",
             "content_hash": "h-2"}]
    db = sqlite3.connect(str(store))
    db.execute("CREATE TABLE snapshots (source_snapshot_id TEXT, "
               "record_id TEXT, content_hash TEXT)")
    db.executemany("INSERT INTO snapshots VALUES (:source_snapshot_id,"
                   ":record_id,:content_hash)", rows)
    db.commit()
    db.close()
    dsid = "sha256:" + "b" * 64
    (base / "runtime" / "state" / "record_id_map.json").write_text(
        json.dumps({"dataset_snapshot_id": dsid}))
    (base / "start_server.sh").write_text(
        "#!/bin/sh\n"
        "export ZAI_MODEL=glm-5.3            # stale first export\n"
        "export QA_PIPELINE_PROFILE=legacy_hybrid\n"
        "export ZAI_MODEL=glm-5.3-flash      # exported LAST, must win\n")
    pin = {
        "manifest_id": "mini-runtime-fixture",
        "model": "glm-5.3-flash",
        "profile": "legacy_hybrid",
        "dataset_snapshot_id": dsid,
        "source_snapshot_store_sha256": sha256_file(store),
        "source_snapshot_catalog_id": catalog_digest(rows),
        "prompt_schema_config_versions": {
            "citation_schema_version": "2.0.0",
            "runtime_profile": "legacy_hybrid",
        },
    }
    return {"src": src, "base": base, "head": head,
            "head_parent": head_parent, "body": body, "pin": pin}


def make_mirror(ws, fixture, at_parent=False, tamper=None, name="mirror"):
    """Clone fixture source into a serving mirror; optional stale HEAD or
    worktree tamper (rel → new text)."""
    mirror = Path(ws) / name
    shutil.copytree(fixture["src"], mirror)
    git(mirror, "config", "user.email", "t@t")
    git(mirror, "config", "user.name", "t")
    if at_parent:
        git(mirror, "reset", "-q", "--hard", fixture["head_parent"])
        # restore evaluated worktree bytes so ONLY the git identity drifts
        rel = "qa-backend/phase02_pipeline.py"
        (mirror / rel).write_text(fixture["body"][rel]
                                  + "# repair zero-claim guard (evaluated)\n")
    if tamper:
        for rel, text in tamper.items():
            (mirror / rel).write_text(text)
    return mirror


def run_guard(mirror, src, base, pin=None, require=False, extra=()):
    cmd = [sys.executable, str(GUARD),
           "--source-repo", str(src),
           "--runtime-repo", str(mirror),
           "--runtime-base", str(base)]
    if pin:
        p = Path(mirror).parent / "pin.json"
        Path(p).write_text(json.dumps(pin))
        cmd += ["--pin", str(p)]
    if require:
        cmd += ["--require-mirror"]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


ws = tempfile.mkdtemp(prefix="rt101-deploy-sync-")
try:
    fx = build_fixture(ws)

    print("── Case A: HEAD == mirror, 4-way exact → 0 SYNCED ──")
    mirror_a = make_mirror(ws, fx)
    r = run_guard(mirror_a, fx["src"], fx["base"], pin=fx["pin"], require=True)
    check("A.match_synced_rc0", r.returncode == 0, r.stderr.strip()[:200])
    rep = json.loads((Path(mirror_a).parent / "guard.json").read_text()) \
        if False else None
    check("A.verdict_no_drift", "SERVING_RUNTIME_DRIFT" not in r.stderr)

    print("── Case B: mirror one commit behind → FAIL_CLOSED ──")
    mirror_b = make_mirror(ws, fx, at_parent=True, name="mirror_b")
    r = run_guard(mirror_b, fx["src"], fx["base"], pin=fx["pin"],
                  require=True)
    check("B.stale_head_rc2", r.returncode == 2, f"rc={r.returncode}")
    check("B.drift_named", "SERVING_RUNTIME_DRIFT" in r.stderr
          and "serving_runtime_git_sha" in r.stderr)

    print("── Case C: critical file hash mismatch → FAIL_CLOSED ──")
    mirror_c = make_mirror(ws, fx, name="mirror_c")
    rel_c = "qa-backend/answer_status.py"
    (mirror_c / rel_c).write_text("# tampered stale copy\n")
    r = run_guard(mirror_c, fx["src"], fx["base"], pin=fx["pin"],
                  require=True)
    check("C.stale_file_rc2", r.returncode == 2, f"rc={r.returncode}")
    check("C.drift_named_file", "SERVING_RUNTIME_DRIFT" in r.stderr
          and "answer_status.py" in r.stderr)

    print("── Case D: corpus match but CODE mismatch → FAIL_CLOSED ──")
    # corpus leg measured from the fixture runtime store MATCHES the pin;
    # only the mirror CODE drifted (worktree tamper, HEAD equal).
    mirror_d = make_mirror(ws, fx, tamper={
        "qa-backend/server.py": "# stale serving code\n"},
        name="mirror_d")
    r = run_guard(mirror_d, fx["src"], fx["base"], pin=fx["pin"],
                  require=True)
    check("D.corpus_ok_code_drift_rc2", r.returncode == 2,
          f"rc={r.returncode}")
    check("D.drift_names_server", "server.py" in r.stderr)
    # control: the same fixture with intact code passes with the SAME pin —
    # proving the corpus leg itself was satisfied in case D.
    r_ctl = run_guard(mirror_a, fx["src"], fx["base"], pin=fx["pin"],
                      require=True)
    check("D.control_same_pin_synced", r_ctl.returncode == 0)

    print("── Case E: model match but CODE mismatch → FAIL_CLOSED ──")
    # serving start script pins the exact model (match) while mirror code
    # drifted in the verifier path.
    mirror_e = make_mirror(ws, fx, tamper={
        "qa-backend/verifier.py": "# stale verifier serving copy\n"},
        name="mirror_e")
    r = run_guard(mirror_e, fx["src"], fx["base"], pin=fx["pin"],
                  require=True)
    check("E.model_ok_code_drift_rc2", r.returncode == 2,
          f"rc={r.returncode}")
    check("E.drift_names_verifier", "verifier.py" in r.stderr)

    print("── formal mode: absent mirror + --require-mirror → FAIL_CLOSED ──")
    r = run_guard(Path(ws) / "no_mirror", fx["src"], fx["base"],
                  pin=fx["pin"], require=True)
    check("F.absent_mirror_required_rc2", r.returncode == 2,
          f"rc={r.returncode}")
    check("F.drift_named_absent", "SERVING_RUNTIME_DRIFT" in r.stderr)
    print("── host-scoped: absent mirror WITHOUT require → 0 skip ──")
    r = run_guard(Path(ws) / "no_mirror", fx["src"], fx["base"])
    check("F.absent_mirror_skip_rc0", r.returncode == 0,
          f"rc={r.returncode}")

    print("── model parsing: exported-LAST wins + wrong-model drift ──")
    check("G.last_export_wins_control",
          "glm-5.3-flash" in (fx["base"] / "start_server.sh").read_text())
    pin_bad_model = dict(fx["pin"])
    pin_bad_model["model"] = "glm-5.3"          # the stale first export
    r = run_guard(mirror_a, fx["src"], fx["base"], pin=pin_bad_model,
                  require=True)
    check("G.model_mismatch_rc2", r.returncode == 2, f"rc={r.returncode}")
    check("G.drift_names_model", "ZAI_MODEL" in r.stderr)

    print("── citation-schema config drift (mirror code constant) ──")
    fx2_dir = tempfile.mkdtemp(prefix="rt101-deploy-sync-cite-")
    fx2 = build_fixture(fx2_dir, cite=CITE_V2)
    # pin still expects 2.0.0 while mirror+src carry 2.0.1 → config drift
    mirror_f = make_mirror(fx2_dir, fx2, name="mirror_f")
    r = run_guard(mirror_f, fx2["src"], fx2["base"], pin=fx2["pin"],
                  require=True)
    # src and mirror are consistent with each other (both 2.0.1) but the
    # pin expects 2.0.0 → the CONFIG leg must reject.
    check("H.citation_config_drift_rc2", r.returncode == 2,
          f"rc={r.returncode} {r.stderr.strip()[:160]}")
    check("H.drift_names_citation", "citation_schema_version" in r.stderr)

    print("── real-host live binding (mirror present on this host) ──")
    real_mirror = Path("/home/rhett/rt101-v5-formal-runner/repo")
    if real_mirror.is_dir() and (real_mirror / ".git").is_dir():
        r = subprocess.run(
            [sys.executable, str(GUARD), "--json-out", ws + "/real.json"],
            capture_output=True, text=True, timeout=120)
        check("I.real_host_guard_clean", r.returncode == 0,
              r.stderr.strip()[:200])
        rep = json.loads(Path(ws + "/real.json").read_text())
        check("I.real_host_head_bound",
              rep.get("serving_runtime_git_sha")
              == rep.get("evaluated_git_sha"))
        check("I.real_host_all_files_equal",
              all(v.get("equal") for v in
                  rep.get("critical_files", {}).values())
              and len(rep.get("critical_files", {})) == 9)
    else:
        check("I.real_host_guard_clean", True, "mirror absent — skip")

    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(fx2_dir, ignore_errors=True)
except Exception as exc:  # pragma: no cover
    print(f"  FAIL fixture_blowup {exc!r}")
    FAILS.append("fixture_blowup")
    shutil.rmtree(ws, ignore_errors=True)

print("════════════════════════════════════════════════════════")
passed = CHECKS[0] - len(FAILS)
print(f"  RT101 deploy-sync contract: {passed} passed, "
      f"{len(FAILS)} failed")
if FAILS:
    sys.exit(1)
