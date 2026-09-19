#!/usr/bin/env python3
"""RT-075 locked-replay builder/verifier tests (hermetic — synthetic data).

Covers: sealed-dataset integrity (self-hash + corpus/registry binding),
privacy boundary (no record text leaves the corpus), provenance
separation (every case historical-real, synthetic count 0), outcome-blind
selection declared before replay, selection re-derivation reproducibility,
verdict-line presence, and fail-closed behavior on corpus/dataset tamper.
No test here clears RT-075: the external control stays owner-provisioned.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"

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


def run(*args):
    return subprocess.run(
        [sys.executable, *(str(a) for a in args)],
        capture_output=True, text=True, timeout=300)


def canonical_seal(dataset: dict) -> str:
    """Same seal construction as the builder: canonical JSON of the body
    without the seal field (an attacker could recompute this — the
    re-sealed-tamper test proves selection re-derivation still catches
    the edit)."""
    import hashlib
    body = {k: v for k, v in dataset.items() if k != "dataset_sha256"}
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def make_fixtures(base: Path):
    """Synthetic corpus + registry (fixture origin is EXPLICITLY declared
    inside the dataset via the builder's --corpus-kind flag; the dataset
    honestly labels itself fully synthetic and the verifier scores it
    UNSOUND — fixtures can never claim production origin)."""
    bodies = [
        "alpha-bravo body text marker {i} unseen-in-repo",
        "charlie-delta notes {i} body-privacy-marker",
        "echo-foxtrot content {i} fixture-only-prose",
    ]
    titles = [
        "quarterly review {i}",
        "zetacore platform rollout {i}",
        "orbit protocol alignment {i}",
        "zetacore platform migration notes {i}",
    ]
    records = []
    for i in range(400):
        records.append({
            "id": f"rec-{i:05d}",
            "t": titles[i % len(titles)].format(i=i),
            "fb": bodies[i % len(bodies)].format(i=i),
            "b": "additional body {i}".format(i=i),
            "d": "2026-08-1{}".format(i % 10),
        })
    corpus = base / "corpus.json"
    corpus.write_text(json.dumps(records, ensure_ascii=False))
    registry = {
        "saved_at": "2026-08-19T00:00:00+00:00",
        "entity_count": 2,
        "entities": [
            {"entity_id": "ent-001", "entity_type": "technology",
             "canonical_name": "Zetacore Platform",
             "aliases": ["Zetacore", "zetacore-platform"],
             "first_seen": "2026-08-01T00:00:00+00:00"},
            {"entity_id": "ent-002", "entity_type": "material",
             "canonical_name": "Orbit Protocol",
             "aliases": ["orbit protocol material"],
             "first_seen": "2026-08-02T00:00:00+00:00"},
        ],
    }
    registry_path = base / "registry.json"
    registry_path.write_text(json.dumps(registry, ensure_ascii=False))
    return corpus, registry_path


def test_builder_and_verifier():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
        base = Path(temp)
        corpus, registry_path = make_fixtures(base)
        dataset = base / "ds.json"
        out = run(SCRIPTS / "build_rt075_locked_replay.py",
                  "--corpus", corpus, "--registry", registry_path,
                  "--corpus-kind", "synthetic_fixture_corpus",
                  "--registry-kind", "synthetic_test_registry",
                  "--out", dataset)
        check("builder exits 0", out.returncode == 0, out.stderr[-300:])
        ds = json.loads(dataset.read_text(encoding="utf-8"))
        check("dataset sealed with sha256",
              len(ds.get("dataset_sha256", "")) == 64)
        check("dataset status SEALED",
              ds.get("status") == "SEALED_LOCKED_REPLAY_DATASET")
        check("fixture honestly labeled fully synthetic",
              ds.get("synthetic_cases") == ds["case_count"])
        check("case_count matches", ds["case_count"] == len(ds["cases"])
              and ds["case_count"] > 0)
        check("stride coverage present",
              any(c["corpus_index"] % ds["selection"]["stride"] == 0
                  for c in ds["cases"]))
        # privacy: no body text ever leaves the corpus
        ds_text = dataset.read_text(encoding="utf-8")
        check("no body text in dataset",
              "body-privacy-marker" not in ds_text
              and "unseen-in-repo" not in ds_text)
        check("no record body field values copied",
              "additional body" not in ds_text)
        # provenance: declared kinds are corpus-driven
        check("fixture corpus kind declared (not production)",
              ds["corpus"]["kind"] == "synthetic_fixture_corpus"
              and ds["registry"]["kind"] == "synthetic_test_registry")
        # selection honesty
        check("outcome-blind selection declared",
              ds["selection"]["outcome_based_selection"] is False
              and ds["selection"]["declared_before_any_replay"] is True)
        check("fixture case source_kind uniform (not production)",
              {c["source_kind"] for c in ds["cases"]} ==
              {"synthetic_fixture_record"})

        # verifier: machinery works, fail-closed on inadequate fixture
        # (400-record fixture cannot reach >=1000 events / 4 classes —
        # the verifier must NOT rubber-stamp it)
        report = base / "report.json"
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", dataset, "--out", report)
        check("verifier refuses inadequate fixture (rc 1)",
              out.returncode == 1, out.stderr[-300:])
        rep = json.loads(report.read_text(encoding="utf-8"))
        for line in ("LOCKED_REPLAY_INTEGRITY",
                     "PRODUCTION_REPRESENTATIVENESS_SUPPORTED",
                     "CLASS_COVERAGE_ADEQUATE",
                     "RESULT_SELECTION_BIAS_PREVENTED",
                     "REAL_VS_SYNTHETIC_PROVENANCE_SOUND",
                     "PRIVACY_BOUNDARY_PRESERVED",
                     "EQUIVALENT_TO_REQUIRED_SHADOW_WINDOW",
                     "RT075_REPLAY_TECHNICALLY_ACCEPTABLE"):
            check(f"verdict line {line}", line in rep.get("verdicts", {}))
        check("verifier integrity PASS",
              rep["verdicts"]["LOCKED_REPLAY_INTEGRITY"] == "PASS")
        check("equivalence honestly NOT proposed for fixture",
              rep["verdicts"]["EQUIVALENT_TO_REQUIRED_SHADOW_WINDOW"]
              == "NOT_EQUIVALENT"
              and rep["verdicts"]["RT075_REPLAY_TECHNICALLY_ACCEPTABLE"]
              == "NOT_ACCEPTABLE")
        check("selection bias prevented",
              rep["verdicts"]["RESULT_SELECTION_BIAS_PREVENTED"]
              == "PREVENTED")
        check("privacy preserved",
              rep["verdicts"]["PRIVACY_BOUNDARY_PRESERVED"] == "PRESERVED")
        check("fixture provenance honestly UNSOUND",
              rep["verdicts"]["REAL_VS_SYNTHETIC_PROVENANCE_SOUND"]
              == "UNSOUND")
        check("deterministic repeat", rep["deterministic_repeat"] is True)
        check("report binds dataset sha",
              rep["dataset_sha256"] == ds["dataset_sha256"])

        # pass-path verdict computation (unit): qualifying replay report
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rt075_verify", SCRIPTS / "verify_rt075_locked_replay.py")
        verify_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify_mod)
        qualifying = {"representative_event_count": 25825,
                      "decision_counts": {"LINK": 3741, "NEW": 13073,
                                          "AMBIGUOUS": 5481, "BLOCKED": 3530},
                      "rollback_triggers": {"high_impact_false_link": False,
                                            "block_rule_violation": False}}
        # synthetic fixture + qualifying metrics must STILL be rejected
        verdicts = verify_mod.compute_verdicts(ds, qualifying, issues=[],
                                               deterministic=True,
                                               selection_ok=True)
        check("synthetic provenance => NOT_ACCEPTABLE even if qualifying",
              verdicts["RT075_REPLAY_TECHNICALLY_ACCEPTABLE"]
              == "NOT_ACCEPTABLE"
              and verdicts["REAL_VS_SYNTHETIC_PROVENANCE_SOUND"]
              == "UNSOUND"
              and verdicts["PRODUCTION_REPRESENTATIVENESS_SUPPORTED"]
              == "NOT_SUPPORTED")
        # production-declared copy mirrors the real sealed dataset
        import copy
        ds_prod = copy.deepcopy(ds)
        ds_prod["corpus"]["kind"] = "historical_real_production_ingest_corpus"
        ds_prod["registry"]["kind"] = "live_production_entity_registry"
        for c in ds_prod["cases"]:
            c["source_kind"] = "historical_real_production_record"
        ds_prod["synthetic_cases"] = 0
        verdicts = verify_mod.compute_verdicts(ds_prod, qualifying,
                                               issues=[], deterministic=True,
                                               selection_ok=True)
        check("qualifying production report => ACCEPTABLE",
              verdicts["RT075_REPLAY_TECHNICALLY_ACCEPTABLE"]
              == "ACCEPTABLE")
        missing_class = {"representative_event_count": 25825,
                         "decision_counts": {"LINK": 100, "NEW": 0,
                                             "AMBIGUOUS": 0, "BLOCKED": 0},
                         "rollback_triggers": qualifying[
                             "rollback_triggers"]}
        verdicts = verify_mod.compute_verdicts(ds, missing_class,
                                               issues=[], deterministic=True,
                                               selection_ok=True)
        check("missing decision class => NOT_ACCEPTABLE",
              verdicts["RT075_REPLAY_TECHNICALLY_ACCEPTABLE"]
              == "NOT_ACCEPTABLE"
              and verdicts["CLASS_COVERAGE_ADEQUATE"] == "INADEQUATE")
        verdicts = verify_mod.compute_verdicts(ds, qualifying,
                                               issues=["corpus sha mismatch"],
                                               deterministic=True,
                                               selection_ok=True)
        check("seal issue => integrity FAIL",
              verdicts["LOCKED_REPLAY_INTEGRITY"] == "FAIL"
              and verdicts["RT075_REPLAY_TECHNICALLY_ACCEPTABLE"]
              == "NOT_ACCEPTABLE")
        verdicts = verify_mod.compute_verdicts(ds, qualifying, issues=[],
                                               deterministic=False,
                                               selection_ok=True)
        check("non-deterministic => NOT_EQUIVALENT",
              verdicts["EQUIVALENT_TO_REQUIRED_SHADOW_WINDOW"]
              == "NOT_EQUIVALENT")

        # NOTE: verdicts on a FIXTURE corpus do not clear RT-075; the
        # verifier's report records fixture provenance via the declared
        # corpus kind, and no test may claim production qualification.

        # tamper 1: corpus drift -> clean fail-closed (rc 2, no crash)
        drifted = base / "corpus2.json"
        drifted.write_text(corpus.read_text(encoding="utf-8") + "x")
        ds2 = json.loads(dataset.read_text(encoding="utf-8"))
        ds2["corpus"]["path"] = str(drifted)
        drifted_ds = base / "ds_drifted.json"
        drifted_ds.write_text(json.dumps(ds2, ensure_ascii=False))
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", drifted_ds, "--out", base / "r3.json")
        check("corpus drift fails closed (rc 2)", out.returncode == 2,
              out.stdout[-200:])
        check("corpus drift reported as issue",
              "unreadable" in out.stdout or "mismatch" in out.stdout
              or "differs" in out.stdout, out.stdout[-200:])

        # tamper 2: dataset mutation -> self-hash fail closed
        ds3 = json.loads(dataset.read_text(encoding="utf-8"))
        ds3["cases"][0]["corpus_index"] += 1
        tampered = base / "ds_tampered.json"
        tampered.write_text(json.dumps(ds3, ensure_ascii=False))
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", tampered, "--out", base / "r4.json")
        check("dataset tamper fails closed", out.returncode == 2,
              out.stdout[-200:])

        # tamper 3 (sophisticated): edit cases AND re-seal — only the
        # independent selection re-derivation can catch this
        ds4 = json.loads(dataset.read_text(encoding="utf-8"))
        ds4["cases"] = ds4["cases"][:-1]          # silently drop a case
        ds4["case_count"] = len(ds4["cases"])
        ds4["dataset_sha256"] = canonical_seal(ds4)  # attacker re-seals
        resealed = base / "ds_resealed.json"
        resealed.write_text(json.dumps(ds4, ensure_ascii=False))
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", resealed, "--out", base / "r5.json")
        check("re-sealed selection tamper still fails closed (rc 2)",
              out.returncode == 2
              and "INDEPENDENT" in out.stdout, out.stdout[-200:])

        # approval-artifact cross-binding
        approval = {
            "schema_version": "rt075-equivalent-replay-approval-1.0",
            "evidence": {"replay_dataset_sha256":
                         canonical_seal(ds),
                         "replay_corpus_sha256":
                         json.loads(corpus.read_text(encoding="utf-8"))
                         and ds["corpus"]["sha256"]},
        }
        approval_path = base / "approval.json"
        approval_path.write_text(json.dumps(approval))
        report2 = base / "report2.json"
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", dataset, "--out", report2,
                  "--approval-artifact", approval_path)
        rep2 = json.loads(report2.read_text(encoding="utf-8"))
        check("matching approval binding accepted",
              out.returncode in (0, 1)
              and rep2.get("approval_cross_binding_verified") is True,
              out.stdout[-200:])
        approval["evidence"]["replay_dataset_sha256"] = "0" * 64
        approval_path.write_text(json.dumps(approval))
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", dataset, "--out", base / "r6.json",
                  "--approval-artifact", approval_path)
        check("mismatched approval binding fails closed (rc 2)",
              out.returncode == 2 and "DIFFERENT dataset" in out.stdout,
              out.stdout[-200:])

        # determinism-skip honesty: skipped proof can NOT qualify
        report3 = base / "report3.json"
        out = run(SCRIPTS / "verify_rt075_locked_replay.py",
                  "--dataset", dataset, "--out", report3,
                  "--skip-determinism")
        rep3 = json.loads(report3.read_text(encoding="utf-8"))
        check("skipped determinism is honestly reported",
              rep3["deterministic_repeat"] is None
              and rep3["determinism_proof_executed"] is False)
        check("skipped determinism can NOT pass the gate",
              out.returncode == 1
              and rep3["verdicts"]["LOCKED_REPLAY_INTEGRITY"] == "FAIL"
              and rep3["verdicts"]["EQUIVALENT_TO_REQUIRED_SHADOW_WINDOW"]
              == "NOT_EQUIVALENT"
              and rep3["equivalent_window_gate"]["deterministic"] is False,
              out.stdout[-200:])


def main():
    test_builder_and_verifier()
    print("=" * 66)
    print(f"  RT-075 locked replay: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
