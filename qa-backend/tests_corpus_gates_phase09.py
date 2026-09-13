#!/usr/bin/env python3
"""Phase09 corpus gates — RT-101 anti-recurrence protections.

Covers (phase09 remediation round, corpus adjudication ROOT_CAUSE_CLASS=B):

  A. corpus_compatibility  — blinded-safe candidate<->runtime binding,
     aggregate-only membership boundary, fail-closed formal-run gate
  B. source_coverage       — machine coverage artifact + fail-closed gaps
  C. ingest_guards         — contamination denylist: holdout/owner-secret/
     builder-workspace material never enters production ingest; glob,
     symlink, relative, env-override and recursive-repo-scan escapes all
     fail closed
  D. committed artifacts   — V5 formal failure record + adjudication
     record present, sanitized, and consistent with the aggregate truth
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

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


# ─────────────────────────── A. corpus_compatibility ───────────────────────
import corpus_compatibility as cc  # noqa: E402


def t_catalog_digest_stable():
    rows_a = [
        {"source_snapshot_id": "ss-1", "record_id": "r1", "content_hash": "h1"},
        {"source_snapshot_id": "ss-2", "record_id": "r2",
         "evidence_text_sha256": "h2"},
    ]
    rows_b = list(reversed(rows_a))
    d1 = cc.source_catalog_digest(rows_a)
    d2 = cc.source_catalog_digest(rows_b)
    check("catalog digest order-stable", d1 == d2 and len(d1) == 64)
    rows_c = [dict(rows_a[0], content_hash="h1-changed"), rows_a[1]]
    check("catalog digest content-sensitive",
          cc.source_catalog_digest(rows_c) != d1)


def t_binding_exact_checks():
    kw = dict(manifest_id="m", dataset_snapshot_id="ds",
              source_snapshot_catalog_id="cat",
              identity_snapshot_id="id", corpus_sha256="c", model="glm")
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True] * 13, [True, True])
    rep = cc.evaluate(cand, runtime, mem,
                      evaluated_git_sha_target="a" * 40,
                      evaluated_git_sha_runtime="a" * 40)
    check("compatible when all bindings exact", rep["compatible"] is True)
    check("failed_checks empty", rep["failed_checks"] == [])
    check("membership aggregate folded",
          rep["membership"]["answer_cases_member"] == 13
          and rep["membership"]["missing_hidden_sources"] == 0)
    for field, value in (("manifest_id", "OTHER"),
                         ("dataset_snapshot_id", "OTHER"),
                         ("source_snapshot_catalog_id", "OTHER"),
                         ("identity_snapshot_id", "OTHER"),
                         ("corpus_sha256", "OTHER")):
        bad = cc.CorpusBinding(**{**kw, field: value})
        rep_bad = cc.evaluate(cand, bad, mem)
        check(f"mismatch {field} fails closed",
              rep_bad["compatible"] is False
              and field + "_exact" in rep_bad["failed_checks"])


def t_membership_missing_fails_closed():
    kw = dict(manifest_id="m", dataset_snapshot_id="ds",
              source_snapshot_catalog_id="cat",
              identity_snapshot_id="id", corpus_sha256="c", model="")
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True, True, False], [True, True])
    rep = cc.evaluate(cand, runtime, mem)
    check("missing hidden source detected",
          rep["membership"]["missing_hidden_sources"] == 1
          and rep["compatible"] is False)
    try:
        cc.assert_formal_run_allowed(rep)
        check("formal-run gate raises on missing membership", False)
    except cc.CorpusCompatibilityError:
        check("formal-run gate raises on missing membership", True)
    ok_rep = cc.evaluate(cand, runtime,
                         cc.aggregate_membership([True], []))
    try:
        cc.assert_formal_run_allowed(ok_rep)
        check("formal-run gate passes compatible report", True)
    except cc.CorpusCompatibilityError:
        check("formal-run gate passes compatible report", False)


def t_blinded_safe_output():
    """The evaluate() output shape must never carry case/locator content."""
    kw = dict(manifest_id="m", dataset_snapshot_id="ds",
              source_snapshot_catalog_id="cat",
              identity_snapshot_id="id", corpus_sha256="c", model="glm")
    rep = cc.evaluate(cc.CorpusBinding(**kw), cc.CorpusBinding(**kw),
                      cc.aggregate_membership([True, False], [True]))
    blob = json.dumps(rep, ensure_ascii=False)
    forbidden_fragments = ("question", "answer_text", "locator", "evidence_span",
                           "query", "gold", "expected")
    hits = [f for f in forbidden_fragments if f in blob]
    check("evaluate() output contains no case-content keys", hits == [], str(hits))
    check("evaluate() output values are aggregates/identities only",
          all(isinstance(v, (bool, int, str, dict, list, type(None)))
              for v in rep.values()))


# ─────────────────────────── B. source_coverage ────────────────────────────
import source_coverage as sc  # noqa: E402


def _mini_snapshot_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE snapshots (source_snapshot_id TEXT, record_id TEXT,"
        " content_hash TEXT, evidence_text TEXT, normalized_text TEXT,"
        " extractor_version TEXT, eligibility TEXT, access_scope TEXT,"
        " raw_object_ref TEXT)")
    db.executemany(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("ss-a", "rid-1", "ha", "alpha evidence text", "alpha evidence text",
             "legacy-v1", "CITATION_ELIGIBLE", "public", None),
            ("ss-b", "rid-2", "hb", "beta evidence text", "beta evidence text",
             "legacy-v1", "CITATION_ELIGIBLE", "public", None),
        ])
    db.commit()
    db.close()


def t_coverage_report_clean():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "source_snapshots"
        _mini_snapshot_db(db)
        rep = sc.build_source_coverage_report(
            snapshot_db=db, manifest_id="m", identity_snapshot_id="i",
            dataset_snapshot_id="sha256:deadbeef", profile="legacy_hybrid",
            extractor_version_expected="legacy-v1")
        check("coverage counts eligible", rep["eligible_source_count"] == 2)
        check("coverage indexed==eligible", rep["indexed_source_count"] == 2)
        check("coverage missing 0 without dataset file",
              rep["missing_count"] is None)
        check("coverage scans clean",
              rep["no_secret_scan"]["clean"] and rep["no_gold_scan"]["clean"])
        problems = sc.validate_source_coverage(report=rep,
                                               require_no_missing=False)
        check("coverage validator passes clean report", problems == [])
        try:
            sc.assert_source_coverage_valid(rep, require_no_missing=False)
            check("coverage assert passes", True)
        except ValueError:
            check("coverage assert passes", False)


def t_coverage_gap_and_secret_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "source_snapshots"
        _mini_snapshot_db(db)
        # missing records vs dataset snapshot
        lite = Path(td) / "lite.json"
        lite.write_text(json.dumps([
            {"record_id": "rid-1"}, {"record_id": "rid-2"},
            {"record_id": "rid-3"}]), encoding="utf-8")
        rep = sc.build_source_coverage_report(snapshot_db=db,
                                              records_lite=lite)
        check("coverage detects missing record",
              rep["missing_count"] == 1)
        problems = sc.validate_source_coverage(report=rep)
        check("coverage validator fail-closed on gap",
              any("missing_count" in p for p in problems))
        # secret material in evidence
        sdb = Path(td) / "source_snapshots_secret"
        con = sqlite3.connect(sdb)
        con.execute(
            "CREATE TABLE snapshots (source_snapshot_id TEXT, record_id TEXT,"
            " content_hash TEXT, evidence_text TEXT, normalized_text TEXT,"
            " extractor_version TEXT, eligibility TEXT, access_scope TEXT,"
            " raw_object_ref TEXT)")
        con.execute(
            "INSERT INTO snapshots VALUES ('ss-s','rid-9','hx',"
            "'-----BEGIN RSA PRIVATE KEY-----xxxx','x','legacy-v1',"
            "'CITATION_ELIGIBLE','public',NULL)")
        con.commit()
        con.close()
        rep_s = sc.build_source_coverage_report(snapshot_db=sdb)
        check("secret scan detects private key material",
              rep_s["no_secret_scan"]["hits"] == 1
              and rep_s["no_secret_scan"]["clean"] is False)
        check("secret report fails closed",
              any("no_secret_scan" in p
                  for p in sc.validate_source_coverage(report=rep_s)))
        # gold digest in index
        gold_digest = hashlib.sha256(b"sealed-gold-bytes").hexdigest()
        rep_g = sc.build_source_coverage_report(
            snapshot_db=db, forbidden_digests=[gold_digest, "hb"])
        check("gold digest scan detects forbidden content hash",
              rep_g["no_gold_scan"]["hits"] == 1
              and rep_g["no_gold_scan"]["clean"] is False)
        # store path marker
        marked = Path(td) / "rt101-v5-gold-notes.db"
        _mini_snapshot_db(marked)
        rep_m = sc.build_source_coverage_report(snapshot_db=marked)
        check("store-path marker flagged",
              rep_m["no_gold_scan"]["store_path_marker_hits"]
              and rep_m["no_gold_scan"]["clean"] is False)


def t_committed_artifacts_sanitized():
    adj = ROOT / "docs/remediation/phase09_RT101_corpus_adjudication.json"
    fail = ROOT / "docs/remediation/phase09_RT101_V5_formal_failure.json"
    check("adjudication record committed", adj.is_file())
    check("V5 failure record committed", fail.is_file())
    if adj.is_file():
        a = json.loads(adj.read_text(encoding="utf-8"))
        check("adjudication declares ROOT_CAUSE_CLASS B",
              a["adjudication"]["ROOT_CAUSE_CLASS"] == "B")
        check("adjudication binds builder vs runtime universes as distinct",
              a["adjudication"]["CORPUS_ID_MATCH"] is False
              and a["adjudication"]["SOURCE_SNAPSHOT_MATCH"] is False)
        check("adjudication declares permanent guards",
              len(a.get("permanent_guards", [])) >= 3)
    if fail.is_file():
        f = json.loads(fail.read_text(encoding="utf-8"))
        check("V5 failure record final/consumed/reuse_forbidden",
              f["outcome"]["final"] == "FAIL"
              and f["outcome"]["consumed"] is True
              and f["outcome"]["reuse_forbidden"] is True)
        blob = json.dumps(f, ensure_ascii=False)
        check("V5 failure record sanitized (no gold fields)",
              not any(k in blob for k in (
                  '"query":', '"answer_text":', '"expected_answer_text":',
                  '"hidden_label_value":', '"evidence_span_text":',
                  '"rubric":')))
        pc = f.get("prohibited_content_check", {})
        check("V5 failure record declares prohibited content absent",
              bool(pc) and all(v is False for v in pc.values()))
        check("V5 failure record carries aggregate counts only",
              f["aggregate_counts"]["facts_total"] == 49
              and f["aggregate_counts"]["facts_matched"] == 0
              and f["aggregate_counts"]["verifier_technical_failures"] == 3)
        check("V5 failure record binds evaluated head + dataset",
              f["evaluation"]["evaluated_git_sha"].startswith("2a4c802")
              and f["evaluation"]["dataset_snapshot_id"].startswith(
                  "sha256:950ff008"))


# ─────────────────────────── C. ingest_guards ──────────────────────────────
import ingest_guards as ig  # noqa: E402


def t_guards_denylist():
    for bad in ("/home/rhett/rt101-v5-builder-n/pkg/gold.json",
                "/home/rhett/tech-db-owner-secrets/rt101/x",
                "/data/rt101-v4-blind-input/g.json",
                "/tmp/RT101-V6-BUILDER/g.json"):
        try:
            ig.assert_ingestable_path(bad)
            check(f"denylist rejects {bad}", False)
        except ig.IngestGuardError:
            check(f"denylist rejects {bad}", True)


def t_guards_symlink_escape():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "sources"
        root.mkdir()
        outside = Path(td) / "outside"
        outside.mkdir()
        (outside / "leak.csv").write_text("x", encoding="utf-8")
        os.symlink(outside, root / "link")
        try:
            ig.assert_ingestable_path(root / "link" / "leak.csv",
                                      allowlist_roots=[root])
            check("symlink escape fails closed", False)
        except ig.IngestGuardError:
            check("symlink escape fails closed", True)


def t_guards_glob_and_relative_escape():
    # glob-escape semantics: expanding a glob must stay inside the
    # allowlist; a pattern with .. is rejected by the path guard.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "src"
        root.mkdir()
        try:
            ig.assert_ingestable_path(root / ".." / ".." / "etc" / "passwd",
                                      allowlist_roots=[root])
            check("glob/.. escape fails closed", False)
        except ig.IngestGuardError:
            check("glob/.. escape fails closed", True)
        try:
            ig.assert_ingestable_path("relative/../escape.csv")
            check("relative traversal fails closed", False)
        except ig.IngestGuardError:
            check("relative traversal fails closed", True)


def t_guards_env_override_rejected():
    env = {"TECH_DB_INGEST_SOURCES_DIR":
           "/home/rhett/tech-db-owner-secrets/rt101"}
    try:
        ig.assert_env_ingest_config_safe(env)
        check("env override of forbidden root fails closed", False)
    except ig.IngestGuardError:
        check("env override of forbidden root fails closed", True)
    env_ok = {"TECH_DB_INGEST_SOURCES_DIR": "/data/canonical-sources"}
    try:
        ig.assert_env_ingest_config_safe(env_ok)
        check("benign env ingest config accepted", True)
    except ig.IngestGuardError:
        check("benign env ingest config accepted", False)


def t_guards_recursive_repo_scan_rejected():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src.py").write_text("x", encoding="utf-8")
        try:
            ig.assert_ingestable_tree(repo)
            check("accidental recursive repo scan fails closed", False)
        except ig.IngestGuardError:
            check("accidental recursive repo scan fails closed", True)
        # explicit adapter opt-in works and still guards contents
        files = ig.assert_ingestable_tree(repo, allow_git_repository=True)
        check("explicit adapter allows guarded repo scan",
              any(p.name == "src.py" for p in files))


def t_guards_secret_file_types_rejected():
    for name in ("hmac_key.txt.key", "server.pem", ".env"):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "src" / name
            p.parent.mkdir(parents=True)
            p.write_text("x", encoding="utf-8")
            try:
                ig.assert_ingestable_path(p, allowlist_roots=[p.parent])
                check(f"forbidden file type rejected: {name}", False)
            except ig.IngestGuardError:
                check(f"forbidden file type rejected: {name}", True)


def main() -> int:
    print("─" * 62)
    print("Phase09 corpus gates (compatibility / coverage / guards)")
    print("─" * 62)
    t_catalog_digest_stable()
    t_binding_exact_checks()
    t_membership_missing_fails_closed()
    t_blinded_safe_output()
    t_coverage_report_clean()
    t_coverage_gap_and_secret_fail_closed()
    t_committed_artifacts_sanitized()
    t_guards_denylist()
    t_guards_symlink_escape()
    t_guards_glob_and_relative_escape()
    t_guards_env_override_rejected()
    t_guards_recursive_repo_scan_rejected()
    t_guards_secret_file_types_rejected()
    print("═" * 62)
    print(f"  Phase09 corpus gates: {PASSED} passed, {FAILED} failed")
    print("═" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
