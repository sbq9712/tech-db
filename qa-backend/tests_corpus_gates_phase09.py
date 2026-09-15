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
    # Codex review Cluster B P2-8: binding digests/snapshot ids must be
    # format-valid, so the fixture uses realistic formats.
    kw = dict(manifest_id="m",
              dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64,
              model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
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
    kw = dict(manifest_id="m", dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64, model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True, True, False], [True, True])
    rep = cc.evaluate(cand, runtime, mem,
                      evaluated_git_sha_target="a" * 40,
                      evaluated_git_sha_runtime="a" * 40)
    check("missing hidden source detected",
          rep["membership"]["missing_hidden_sources"] == 1
          and rep["compatible"] is False)
    try:
        cc.assert_formal_run_allowed(rep)
        check("formal-run gate raises on missing membership", False)
    except cc.CorpusCompatibilityError:
        check("formal-run gate raises on missing membership", True)
    ok_rep = cc.evaluate(cand, runtime,
                         cc.aggregate_membership([True], [True]),
                         evaluated_git_sha_target="a" * 40,
                         evaluated_git_sha_runtime="a" * 40)
    try:
        cc.assert_formal_run_allowed(ok_rep)
        check("formal-run gate passes compatible report", True)
    except cc.CorpusCompatibilityError:
        check("formal-run gate passes compatible report", False)


def t_binding_empty_fields_fail_closed():
    """Codex review A1: empty identity values can never satisfy bindings."""
    kw = dict(manifest_id="m", dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64, model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True], [True])
    for field in ("model", "manifest_id"):
        empty = cc.CorpusBinding(**{**kw, field: ""})
        rep = cc.evaluate(cand, empty, mem,
                          evaluated_git_sha_target="a" * 40,
                          evaluated_git_sha_runtime="a" * 40)
        check(f"empty {field} fails binding closed",
              rep["compatible"] is False)
    rep = cc.evaluate(cand, runtime, mem,
                      evaluated_git_sha_target="",   # missing head
                      evaluated_git_sha_runtime="a" * 40)
    check("missing evaluated head fails closed",
          rep["compatible"] is False
          and "evaluated_git_sha_bound" in rep["failed_checks"])
    rep = cc.evaluate(cand, runtime, mem,
                      evaluated_git_sha_target="a" * 40,
                      evaluated_git_sha_runtime="a" * 40)
    check("mismatched prompt versions fail closed",
          rep["checks"]["prompt_schema_config_versions_exact"] is True)
    bad_versions = cc.CorpusBinding(
        **{**kw, "prompt_schema_config_versions": {"prompt": "v2"}})
    rep_bad = cc.evaluate(cand, bad_versions, mem,
                          evaluated_git_sha_target="a" * 40,
                          evaluated_git_sha_runtime="a" * 40)
    check("prompt version drift fails closed",
          rep_bad["compatible"] is False)


def t_assert_gate_deep_validation():
    """Codex review A1: the formal-run gate re-validates the whole
    report instead of trusting a truthy compatible flag."""
    kw = dict(manifest_id="m", dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64, model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True, True], [True])

    def base_report():
        return cc.evaluate(cand, runtime, mem,
                           evaluated_git_sha_target="a" * 40,
                           evaluated_git_sha_runtime="a" * 40)

    # tampered reports must each be rejected
    tampered = base_report(); tampered["compatible"] = 1  # truthy, not True
    tampered["checks"] = {**tampered["checks"]}
    try:
        cc.assert_formal_run_allowed(tampered)
        check("truthy-but-not-True compatible rejected", False)
    except cc.CorpusCompatibilityError:
        check("truthy-but-not-True compatible rejected", True)

    tampered = base_report(); tampered["schema_version"] = "other-1.0"
    try:
        cc.assert_formal_run_allowed(tampered)
        check("schema mismatch rejected", False)
    except cc.CorpusCompatibilityError:
        check("schema mismatch rejected", True)

    tampered = base_report()
    tampered["checks"] = {**tampered["checks"],
                          "model_exact": False}
    try:
        cc.assert_formal_run_allowed(tampered)
        check("check/flag inconsistency rejected", False)
    except cc.CorpusCompatibilityError:
        check("check/flag inconsistency rejected", True)

    tampered = base_report()
    tampered["candidate_binding"] = dict(tampered["candidate_binding"],
                                         model="")
    try:
        cc.assert_formal_run_allowed(tampered)
        check("empty binding field in report rejected", False)
    except cc.CorpusCompatibilityError:
        check("empty binding field in report rejected", True)

    tampered = base_report()
    tampered["membership"] = dict(tampered["membership"],
                                  answer_cases_member=99)
    try:
        cc.assert_formal_run_allowed(tampered)
        check("impossible membership counters rejected", False)
    except cc.CorpusCompatibilityError:
        check("impossible membership counters rejected", True)

    tampered = base_report()
    tampered["membership"] = dict(tampered["membership"],
                                  answer_cases_checked=0)
    try:
        cc.assert_formal_run_allowed(tampered)
        check("zero checked cases rejected", False)
    except cc.CorpusCompatibilityError:
        check("zero checked cases rejected", True)

    tampered = base_report()
    del tampered["membership"]["missing_hidden_sources"]
    try:
        cc.assert_formal_run_allowed(tampered)
        check("missing membership counter rejected", False)
    except cc.CorpusCompatibilityError:
        check("missing membership counter rejected", True)

    # the honest report still passes
    try:
        cc.assert_formal_run_allowed(base_report())
        check("honest report accepted by deep validation", True)
    except cc.CorpusCompatibilityError as exc:
        check("honest report accepted by deep validation", False, str(exc))


def t_blinded_safe_output():
    """The evaluate() output shape must never carry case/locator content."""
    kw = dict(manifest_id="m", dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64, model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
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
        # codex review A1: the clean case binds a dataset snapshot whose
        # record ids exactly match the indexed universe.
        lite = Path(td) / "lite.json"
        lite.write_text(json.dumps([
            {"record_id": "rid-1", "body": "text one"},
            {"record_id": "rid-2", "body": "text two"}]), encoding="utf-8")
        rep = sc.build_source_coverage_report(
            snapshot_db=db, records_lite=lite,
            manifest_id="m", identity_snapshot_id="i",
            dataset_snapshot_id="sha256:deadbeef", profile="legacy_hybrid",
            extractor_version_expected="legacy-v1")
        check("coverage counts eligible", rep["eligible_source_count"] == 2)
        check("coverage indexed==eligible", rep["indexed_source_count"] == 2)
        check("coverage missing 0 with exact dataset match",
              rep["missing_count"] == 0)
        check("coverage dataset binding present",
              rep["dataset_binding_present"] is True)
        check("coverage scans clean",
              rep["no_secret_scan"]["clean"] and rep["no_gold_scan"]["clean"])
        check("vacuous digest scan visible",
              rep["no_gold_scan"]["digest_scan_meaningful"] is False)
        problems = sc.validate_source_coverage(report=rep,
                                               require_no_missing=True)
        check("coverage validator passes clean report", problems == [],
              str(problems))
        try:
            sc.assert_source_coverage_valid(rep)
            check("coverage assert passes", True)
        except ValueError:
            check("coverage assert passes", False)
        # codex review A1: an unproven universe (no dataset binding,
        # missing_count None) must FAIL CLOSED now.
        rep_nodata = sc.build_source_coverage_report(
            snapshot_db=db, manifest_id="m", identity_snapshot_id="i",
            dataset_snapshot_id="sha256:deadbeef", profile="legacy_hybrid",
            extractor_version_expected="legacy-v1")
        check("unbound dataset flagged",
              rep_nodata["dataset_binding_present"] is False)
        problems = sc.validate_source_coverage(report=rep_nodata,
                                               require_no_missing=False)
        check("missing dataset binding fails validation",
              any("dataset_binding_present" in p for p in problems))
        problems = sc.validate_source_coverage(report=rep_nodata,
                                               require_no_missing=True)
        check("missing_count None fails closed",
              any("missing_count unavailable" in p for p in problems))
        # codex review A1: unidentifiable dataset rows fail validation
        lite_bad = Path(td) / "lite_bad.json"
        lite_bad.write_text(json.dumps([
            {"record_id": "rid-1", "body": "x"},
            {"note": "no identity"}]), encoding="utf-8")
        rep_bad = sc.build_source_coverage_report(
            snapshot_db=db, records_lite=lite_bad,
            manifest_id="m", identity_snapshot_id="i",
            dataset_snapshot_id="sha256:deadbeef", profile="legacy_hybrid",
            extractor_version_expected="legacy-v1")
        check("unidentifiable rows counted",
              rep_bad["unidentifiable_dataset_rows"] == 1)
        problems = sc.validate_source_coverage(report=rep_bad,
                                               require_no_missing=False)
        check("unidentifiable rows fail validation",
              any("unidentifiable" in p for p in problems))


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
    # NOTE: probe paths are intentionally NOT echoed verbatim into the suite
    # tail: verify_spec_manifest V7 treats "*.json"-suffixed tokens in suite
    # tails as artifact references that must resolve inside the repository,
    # and forbidden gold paths are foreign workspaces by definition.  The
    # authoritative denylist lives in ingest_guards.py; probes are numbered.
    probes = ("/home/rhett/rt101-v5-builder-n/pkg/gold.json",
              "/home/rhett/tech-db-owner-secrets/rt101/x",
              "/data/rt101-v4-blind-input/g.json",
              "/tmp/RT101-V6-BUILDER/g.json")
    for i, bad in enumerate(probes):
        try:
            ig.assert_ingestable_path(bad)
            check(f"denylist rejects forbidden gold path #{i}", False)
        except ig.IngestGuardError:
            check(f"denylist rejects forbidden gold path #{i}", True)


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
            ig.assert_ingestable_tree(repo, allowlist_roots=[repo])
            check("accidental recursive repo scan fails closed", False)
        except ig.IngestGuardError:
            check("accidental recursive repo scan fails closed", True)
        # explicit adapter opt-in works and still guards contents
        files = ig.assert_ingestable_tree(repo, allowlist_roots=[repo],
                                          allow_git_repository=True)
        check("explicit adapter allows guarded repo scan",
              any(p.name == "src.py" for p in files))


def t_guards_empty_allowlist_fail_closed():
    """Codex review A1: an empty allowlist can never mean unrestricted."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ok.csv"
        p.write_text("x", encoding="utf-8")
        try:
            ig.assert_ingestable_path(p)
            check("empty allowlist rejected for path", False)
        except ig.IngestGuardError:
            check("empty allowlist rejected for path", True)
        try:
            ig.assert_ingestable_tree(p.parent)
            check("empty allowlist rejected for tree", False)
        except ig.IngestGuardError:
            check("empty allowlist rejected for tree", True)


def t_guards_env_file_family():
    """Codex review A1: .env hides in the name; variants bypass suffix."""
    for name in (".env", ".env.local", "app.env", ".envrc"):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "src" / name
            p.parent.mkdir(parents=True)
            p.write_text("x", encoding="utf-8")
            try:
                ig.assert_ingestable_path(p, allowlist_roots=[p.parent])
                check(f"env file family rejected: {name}", False)
            except ig.IngestGuardError:
                check(f"env file family rejected: {name}", True)


def t_guards_generic_gold_marker():
    """Codex review A1: the generic 'gold' marker is denylisted.

    Probe paths are intentionally NOT echoed verbatim into the suite tail
    (same reason as the denylist probes above: verify_spec_manifest V7
    treats .json-suffixed tail tokens as repository artifact references)."""
    probes = ("/data/gold/v5/answer.json", "/tmp/golden_holdout/x")
    for i, bad in enumerate(probes):
        try:
            ig.assert_ingestable_path(bad, allow_unrestricted=True)
            check(f"gold marker rejects forbidden path #{i}", False)
        except ig.IngestGuardError:
            check(f"gold marker rejects forbidden path #{i}", True)


def t_guards_symlinked_directory_in_tree():
    """Codex review A1: symlinked directories are rejected in tree scans."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "src"
        (root / "real").mkdir(parents=True)
        outside = Path(td) / "outside"
        outside.mkdir()
        (root / "real" / "a.csv").write_text("x", encoding="utf-8")
        os.symlink(outside, root / "real" / "dirlink")
        try:
            ig.assert_ingestable_tree(root, allowlist_roots=[root])
            check("symlinked dir fails tree scan", False)
        except ig.IngestGuardError:
            check("symlinked dir fails tree scan", True)


def t_guards_env_list_values_probed():
    """Codex review A1: list-shaped env values are probed element-wise."""
    smuggled = os.pathsep.join(["/data/ok", "/home/rhett/rt101-v5-builder"])
    try:
        ig.assert_env_ingest_config_safe({"TECH_DB_DATA_SOURCES": smuggled})
        check("env list smuggling rejected (pathsep)", False)
    except ig.IngestGuardError:
        check("env list smuggling rejected (pathsep)", True)
    try:
        ig.assert_env_ingest_config_safe(
            {"TECH_DB_DATA_SOURCES": "/data/ok,/data/rt101_v5-x"})
        check("env list smuggling rejected (comma)", False)
    except ig.IngestGuardError:
        check("env list smuggling rejected (comma)", True)


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
    t_binding_empty_fields_fail_closed()
    t_assert_gate_deep_validation()
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
    t_guards_empty_allowlist_fail_closed()
    t_guards_env_file_family()
    t_guards_generic_gold_marker()
    t_guards_symlinked_directory_in_tree()
    t_guards_env_list_values_probed()
    t_binding_format_validation()
    t_expected_binding_crosscheck()
    t_strict_coverage_validation()


def t_binding_format_validation():
    """Cluster B P2-8: equal-but-malformed bindings can never satisfy."""
    def bind(corpus="c" * 64, ds="sha256:" + "a" * 64,
             cat="b" * 64):
        return dict(manifest_id="m", dataset_snapshot_id=ds,
                    source_snapshot_catalog_id=cat,
                    identity_snapshot_id="id", corpus_sha256=corpus,
                    model="glm",
                    prompt_schema_config_versions={"prompt": "v1"})
    mem = cc.aggregate_membership([True], [True])
    good = cc.CorpusBinding(**bind())
    rep = cc.evaluate(good, good, mem,
                      evaluated_git_sha_target="a" * 40,
                      evaluated_git_sha_runtime="a" * 40)
    check("format-valid bindings pass", rep["compatible"] is True)
    for label, over in (
        ("corpus digest not hex", dict(corpus="z" * 64)),
        ("corpus digest short", dict(corpus="a" * 63)),
        ("dataset id without prefix", dict(ds="a" * 64)),
        ("dataset id bad hex", dict(ds="sha256:" + "g" * 64)),
        ("catalog id short", dict(cat="b" * 63)),
    ):
        bad = cc.CorpusBinding(**bind(**over))
        rep = cc.evaluate(bad, bad, mem,
                          evaluated_git_sha_target="a" * 40,
                          evaluated_git_sha_runtime="a" * 40)
        check(f"malformed binding rejected: {label}",
              rep["compatible"] is False
              and "binding_formats_valid" in rep["failed_checks"])
    rep = cc.evaluate(good, good, mem,
                      evaluated_git_sha_target="short",
                      evaluated_git_sha_runtime="a" * 40)
    check("malformed evaluated head rejected",
          rep["compatible"] is False
          and "binding_formats_valid" in rep["failed_checks"])


def t_expected_binding_crosscheck():
    """Cluster B P0-1: the gate binds the report to caller-recomputed
    identity — a structurally valid report with drifted bindings,
    membership counters, or measured head is rejected."""
    kw = dict(manifest_id="m", dataset_snapshot_id="sha256:" + "a" * 64,
              source_snapshot_catalog_id="b" * 64,
              identity_snapshot_id="id", corpus_sha256="c" * 64,
              model="glm",
              prompt_schema_config_versions={"prompt": "v1"})
    cand = cc.CorpusBinding(**kw)
    runtime = cc.CorpusBinding(**kw)
    mem = cc.aggregate_membership([True] * 13, [True, True])
    head = "a" * 40
    rep = cc.evaluate(cand, runtime, mem,
                      evaluated_git_sha_target=head,
                      evaluated_git_sha_runtime=head)
    cc.assert_formal_run_allowed(
        rep, expected_candidate_binding=cand.to_dict(),
        expected_membership=rep["membership"], expected_head=head)
    check("crosscheck passes on honest report", True)

    drifted = dict(cand.to_dict(), corpus_sha256="d" * 64)
    try:
        cc.assert_formal_run_allowed(
            rep, expected_candidate_binding=drifted, expected_head=head)
        check("drifted candidate binding rejected", False)
    except cc.CorpusCompatibilityError:
        check("drifted candidate binding rejected", True)

    try:
        cc.assert_formal_run_allowed(
            rep, expected_membership=dict(rep["membership"],
                                          answer_cases_member=12),
            expected_head=head)
        check("drifted membership counters rejected", True)
    except cc.CorpusCompatibilityError:
        check("drifted membership counters rejected", True)

    try:
        cc.assert_formal_run_allowed(
            rep, expected_candidate_binding=cand.to_dict(),
            expected_head="b" * 40)
        check("drifted measured head rejected", True)
    except cc.CorpusCompatibilityError:
        check("drifted measured head rejected", True)

    try:
        cc.assert_formal_run_allowed(
            rep, expected_candidate_binding=drifted, expected_head=head)
        check("equal-but-malformed binding drift detail surfaces", True)
    except cc.CorpusCompatibilityError as exc:
        check("equal-but-malformed binding drift detail surfaces",
              "corpus_sha256" in str(exc) or "drift" in str(exc))


def t_strict_coverage_validation():
    """Cluster B P1-4/P2-7/P2-10: formal-only strict coverage validation —
    arithmetic consistency, both-direction universe reconciliation,
    evidence-storage pairing, non-vacuous gold scan."""
    import source_coverage as sc

    def base_report():
        return {
            "schema_version": sc.SCHEMA_VERSION,
            "generated_from": {
                "snapshot_db_name": "source_snapshots",
                "snapshot_db_sha256": "e" * 64,
                "records_lite_sha256": "f" * 64,
                "manifest_id": "mini-runtime-a49a56f8861a0633",
                "identity_snapshot_id": "mini-identity-v1",
                "dataset_snapshot_id": "sha256:" + "a" * 64,
                "profile": "legacy_hybrid",
            },
            "eligible_source_count": 10,
            "retrieval_only_count": 0,
            "quarantined_count": 0,
            "indexed_source_count": 10,
            "missing_count": 0,
            "dataset_binding_present": True,
            "dataset_record_count": 10,
            "unidentifiable_dataset_rows": 0,
            "extraction_failures": 0,
            "index_failures": 0,
            "empty_evidence_count": 0,
            "empty_evidence_source_side_count": 0,
            "extractor_version_breakdown": {"v1": 10},
            "raw_object_ref_missing_count": 10,
            "evidence_storage": "inline",
            "extra_citation_eligible_count": 0,
            "source_type_breakdown": {},
            "no_secret_scan": {"pattern_families": 8, "hits": 0,
                               "clean": True},
            "no_gold_scan": {"forbidden_digests_checked": 3,
                             "digest_scan_meaningful": True,
                             "hits": 0, "store_path_marker_hits": [],
                             "clean": True},
        }

    check("strict accepts honest inline report",
          sc.validate_source_coverage_strict(base_report()) == [])

    r = base_report()
    r["indexed_source_count"] = 9
    check("strict rejects arithmetic drift",
          any("arithmetic" in p for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["empty_evidence_source_side_count"] = 5
    check("strict rejects side>empty inversion",
          any("arithmetic" in p for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    del r["generated_from"]["manifest_id"]
    check("strict rejects missing generated-from identity",
          any("generated_from" in p
              for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["missing_count"] = 2
    check("strict rejects missing records",
          any("missing_count" in p
              for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["extra_citation_eligible_count"] = 3
    check("strict rejects extra citation-eligible identities",
          any("extra_citation_eligible" in p
              for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["extra_citation_eligible_count"] = None
    check("strict rejects unreported extra count",
          any("unreported" in p
              for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["raw_object_ref_missing_count"] = 4  # inline requires ALL or NONE
    check("strict rejects inline storage with partial raw refs",
          any("raw" in p for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["evidence_storage"] = "raw_object"
    r["raw_object_ref_missing_count"] = 0
    check("strict accepts declared raw_object storage with zero missing",
          sc.validate_source_coverage_strict(r) == [])
    r = base_report()
    r["evidence_storage"] = "raw_object"
    check("strict rejects raw_object storage with missing refs",
          any("raw_object" in p
              for p in sc.validate_source_coverage_strict(r)))

    r = base_report()
    r["no_gold_scan"]["digest_scan_meaningful"] = False
    check("strict rejects vacuous gold digest scan",
          any("digest set empty" in p
              for p in sc.validate_source_coverage_strict(r)))
    print("═" * 62)
    print(f"  Phase09 corpus gates: {PASSED} passed, {FAILED} failed")
    print("═" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
