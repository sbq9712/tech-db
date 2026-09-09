#!/usr/bin/env python3
"""Phase09 evidence-chain consistency validator (D7, P1 closure).

Validates the one-way chain
    test_summary.json -> release/ticket evidence -> PHASE_RESULT ->
    NEXT_PROMPT_ALLOWED -> completion report
and fails (exit 1) on any mismatch: count/suite/hash drift, decision/
blocker/graph/phase-status mismatch, PHASE_RESULT vs NEXT_PROMPT_ALLOWED
conflict, report tampering, stale generation order, stale/unresolvable
referenced SHAs, fabricated/missing referenced artifacts, wrong evidence
hashes, or authority-state drift versus the live environment channel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from phase09_authority import (  # noqa: E402
    RT101_AUTHORITY_ID,
    authority_results_from_env,
)
from phase09_release import (  # noqa: E402
    MANDATORY_AUTHORITIES,
    load_external_blockers,
)
from build_phase09_evidence import OWNED_EVIDENCE_PATHS  # noqa: E402

PHASE_RESULT_SCHEMA = "phase09-phase-result-2.0"
NEXT_PROMPT_SCHEMA = "phase09-next-prompt-gate-2.0"
SUMMARY_SCHEMA_MIN_FIELDS = ("generated_at", "git_sha", "all_passed",
                             "total_passed", "total_failed", "suites")

CHECKS: list[dict] = []


def record(check_id: str, name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append({"check": check_id, "name": name, "pass": bool(passed),
                   "detail": detail})
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {check_id} {name}" + (f" — {detail}" if detail else ""))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT,
                                       text=True).strip()
    except subprocess.CalledProcessError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-evidence", type=Path,
                        default=ROOT / "qa-backend/phase09_release_evidence.json")
    parser.add_argument("--ticket-status", type=Path,
                        default=ROOT / "qa-backend/phase09_ticket_status.json")
    parser.add_argument("--chain-root", type=Path, default=ROOT,
                        help="root holding the committed chain artifacts "
                             "(qa-backend/test_summary.json, docs/remediation/*); "
                             "defaults to the repository (hermetic tests may "
                             "point it at a temp copy)")
    parser.add_argument("--strict-machine", action="store_true",
                        help="require the CI-generated gate artifacts to be "
                             "present and semantically fresh (CI/local "
                             "post-gate mode)")
    args = parser.parse_args()

    chain_root = args.chain_root.resolve()
    summary_path = chain_root / "qa-backend/test_summary.json"
    result_path = chain_root / "docs/remediation/phase09_PHASE_RESULT.json"
    next_path = chain_root / "docs/remediation/phase09_NEXT_PROMPT_ALLOWED.json"
    report_path = chain_root / "docs/remediation/phase09_completion_report.md"
    policy_path = ROOT / "spec/phase09_release_policy.json"

    for path in (summary_path, result_path, next_path, report_path, policy_path):
        record("C0", "committed chain artifacts exist", path.exists(), str(path))
        if not path.exists():
            return _finish()

    summary = json.loads(summary_path.read_text("utf-8"))
    result = json.loads(result_path.read_text("utf-8"))
    nxt = json.loads(next_path.read_text("utf-8"))
    report = report_path.read_text("utf-8")
    policy = json.loads(policy_path.read_text("utf-8"))

    # C0b artifact schema versions (fail closed on unsupported/legacy)
    record("C0b", "chain artifact schema versions",
           result.get("schema_version") == PHASE_RESULT_SCHEMA
           and nxt.get("schema_version") == NEXT_PROMPT_SCHEMA
           and policy.get("schema_version") == "phase09-release-policy-1.1",
           f"result={result.get('schema_version')} "
           f"next={nxt.get('schema_version')} "
           f"policy={policy.get('schema_version')}")

    # C1 summary schema/arithmetic
    missing_fields = [f for f in SUMMARY_SCHEMA_MIN_FIELDS if f not in summary]
    suites = summary.get("suites", [])
    pass_sum = sum(s.get("passed", 0) for s in suites)
    fail_sum = sum(s.get("failed", 0) for s in suites)
    arithmetic_ok = (not missing_fields
                     and summary.get("total_passed") == pass_sum
                     and summary.get("total_failed") == fail_sum
                     and summary.get("all_passed") == (fail_sum == 0 and not summary.get("missing_suites")))
    record("C1", "test_summary schema + arithmetic", arithmetic_ok,
           f"missing={missing_fields}" if missing_fields else
           f"{summary.get('total_passed')}/{summary.get('total_failed')} across {len(suites)} suites")

    # C2 PHASE_RESULT mirrors summary counts
    ts = result.get("test_summary", {})
    record("C2", "PHASE_RESULT mirrors summary counts",
           ts.get("total_passed") == summary.get("total_passed")
           and ts.get("total_failed") == summary.get("total_failed")
           and ts.get("suite_count") == len(suites)
           and ts.get("all_passed") == summary.get("all_passed"),
           f"doc={ts.get('total_passed')}/{ts.get('total_failed')}x{ts.get('suite_count')} "
           f"summary={summary.get('total_passed')}/{summary.get('total_failed')}x{len(suites)}")

    # C3 summary hash binding
    actual_summary_sha = sha256_file(summary_path)
    record("C3", "PHASE_RESULT test_summary sha256 matches file",
           ts.get("sha256") == actual_summary_sha,
           f"doc={str(ts.get('sha256'))[:16]}… actual={actual_summary_sha[:16]}…")

    # C4 release decision vs fresh CI-generated evidence (when available)
    if args.release_evidence.exists():
        evidence = json.loads(args.release_evidence.read_text("utf-8"))
        decision = evidence.get("release_decision") if "release_decision" in evidence else evidence
        doc_decision = result.get("release_decision", {})
        keys = ("core_eligible", "production_release_eligible",
                "graph_activation_eligible", "graph_state")
        drift = [k for k in keys if doc_decision.get(k) != decision.get(k)]
        doc_blockers = sorted(doc_decision.get("external_blockers", []))
        ev_blockers = sorted(decision.get("external_blockers", []))
        if not drift and doc_blockers == ev_blockers:
            # review F3: reasons and per-authority sanitized state are part
            # of the decision — erasing the authority reason from either
            # side must fail, not just flipped booleans.
            if sorted(doc_decision.get("reasons") or []) != \
                    sorted(decision.get("reasons") or []):
                drift.append("reasons")

            def _strip_requirement(states):
                # PHASE_RESULT echoes the policy requirement block beside
                # each authority for self-containment; the fresh evidence
                # carries only the sanitized state.  Compare states only.
                return {aid: {k: v for k, v in state.items() if k != "requirement"}
                        for aid, state in (states or {}).items()}

            doc_auth = _strip_requirement(
                doc_decision.get("authorities") or result.get("authorities"))
            ev_auth = decision.get("authorities") or evidence.get("authorities")
            if ev_auth:
                if sorted(doc_auth) != sorted(ev_auth):
                    drift.append("authority-ids")
                elif doc_auth != _strip_requirement(ev_auth):
                    drift.append("authority-state")
            # flat evidence without an authorities section (hermetic
            # fixtures) is governed by C13's live-channel comparison.
        record("C4", "release decision matches fresh gate evidence",
               not drift and doc_blockers == ev_blockers,
               f"drift={drift} blockers doc={doc_blockers} evidence={ev_blockers}")
    else:
        if args.strict_machine:
            record("C4", "release decision matches fresh gate evidence",
                   False, "--strict-machine requires the gate evidence file")
        else:
            record("C4", "release decision vs fresh evidence", True,
                   "evidence absent (clean checkout) — skipped")

    # C5 phase-status implication
    decision = result.get("release_decision", {})
    phase_status = result.get("phase_status")
    core = decision.get("core_eligible") is True
    prod = decision.get("production_release_eligible") is True
    expected_status = "PASS" if (core and prod) else (
        "PASS_WITH_EXTERNAL_BLOCKER" if core else "NOT_SATISFIED")
    record("C5", "phase_status implies decision",
           phase_status == expected_status,
           f"phase_status={phase_status} expected={expected_status}")

    # C6 NEXT_PROMPT_ALLOWED consistency
    record("C6", "NEXT_PROMPT_ALLOWED derivation rule",
           nxt.get("NEXT_PROMPT_ALLOWED") == (phase_status == "PASS" and core and prod
                                              and not decision.get("external_blockers")),
           f"allowed={nxt.get('NEXT_PROMPT_ALLOWED')}")
    record("C6b", "NEXT_PROMPT_ALLOWED mirrors PHASE_RESULT state",
           nxt.get("phase_status") == phase_status
           and nxt.get("core_eligible") == decision.get("core_eligible")
           and nxt.get("production_release_eligible") == decision.get("production_release_eligible")
           and sorted(nxt.get("external_blockers", [])) == sorted(decision.get("external_blockers", []))
           and nxt.get("graph") == result.get("graph"),
           "state mirror mismatch" if CHECKS[-1]["pass"] is False else "")
    if nxt.get("NEXT_PROMPT_ALLOWED") is True and phase_status != "PASS":
        record("C6c", "next-prompt safety implication", False,
               "NEXT_PROMPT_ALLOWED=true with non-PASS phase")
    else:
        record("C6c", "next-prompt safety implication", True)

    # C7 completion report machine block vs PHASE_RESULT
    m = re.search(r"<!-- machine-block begin.*?```text\n(.*?)```.*?<!-- machine-block end -->",
                  report, re.S)
    if not m:
        record("C7", "completion report machine block present", False, "marker block missing")
    else:
        facts = {}
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                facts[key.strip()] = value.strip()
        expected_facts = {
            "tested_git_sha": result.get("tested_git_sha"),
            "total": (f"{summary.get('total_passed')} passed, "
                      f"{summary.get('total_failed')} failed across "
                      f"{len(suites)} suites"),
            "phase_status": phase_status,
            "core_eligible": str(decision.get("core_eligible")).lower(),
            "production_release_eligible": str(decision.get("production_release_eligible")).lower(),
            "graph_state": result.get("graph", {}).get("state"),
            "external_blockers": "[" + ", ".join(
                sorted(result.get("external_blockers", {}))) + "]",
            "rt101_authority_satisfied": str(
                bool((result.get("authorities", {}).get(RT101_AUTHORITY_ID) or {}).get("satisfied"))).lower(),
            "NEXT_PROMPT_ALLOWED": str(bool(nxt.get("NEXT_PROMPT_ALLOWED"))).lower(),
            "phase10": nxt.get("phase10"),
        }
        drift = {k: (facts.get(k), v) for k, v in expected_facts.items()
                 if facts.get(k) != v}
        record("C7", "completion report matches machine artifacts",
               not drift, f"drift={drift}" if drift else f"{len(expected_facts)} facts verified")

    # C8 generation order (monotone along the chain)
    stamps = [
        ("summary", parse_ts(summary.get("generated_at"))),
        ("phase_result", parse_ts(result.get("generated_at"))),
        ("next_prompt", parse_ts(nxt.get("generated_at"))),
    ]
    if args.ticket_status.exists():
        stamps.insert(1, ("ticket_status", parse_ts(
            json.loads(args.ticket_status.read_text("utf-8")).get("generated_at"))))
    from datetime import timezone as _tz
    def _aware(ts):
        # run_all_tests writes a naive LOCAL timestamp; interpret it as
        # local time. tz-aware stamps pass through unchanged.
        if ts is not None and ts.tzinfo is None:
            return ts.astimezone()
        return ts
    stamps = [(name, _aware(ts)) for name, ts in stamps]
    now = datetime.now(_tz.utc)
    not_future = all(v is not None and v <= now + timedelta(minutes=5)
                     for _, v in stamps)
    ordered = (all(v is not None for _, v in stamps)
               and all(a[1].tzinfo is not None and b[1].tzinfo is not None
                       and a[1] <= b[1]
                       for a, b in zip(stamps, stamps[1:]))
               and not_future)
    record("C8", "generation order monotone, none materially in the future",
           ordered, " -> ".join(f"{n}={v}" for n, v in stamps if v))

    # C9 SHA semantics: tested must be a real commit, ancestor-or-equal of
    # the base; when base != tested the diff must touch ONLY chain-owned
    # evidence files (an evidence-only descendant commit never claims to
    # have tested itself; CI at the exact head binds exact-head evidence).
    tested = result.get("tested_git_sha", "")
    base = result.get("evidence_generation_base_sha", "")
    head = git("rev-parse", "HEAD")

    def _is_ancestor(older: str, younger: str) -> bool:
        probe = subprocess.run(
            ["git", "merge-base", "--is-ancestor", older, younger],
            cwd=ROOT, capture_output=True)
        return probe.returncode == 0

    sha_ok = (len(tested) == 40 and len(base) == 40
              and git("cat-file", "-e", f"{tested}^{{commit}}") == ""
              and git("cat-file", "-e", f"{base}^{{commit}}") == "")
    # review F4: the base itself must be a real commit satisfying the
    # declared semantics tested <= base <= head; unresolvable or unrelated
    # base SHAs fail instead of collapsing to an empty diff.
    order_ok = (sha_ok and _is_ancestor(tested, base)
                and _is_ancestor(base, head))
    owned_drift_ok = False
    drift_note = "unverified"
    if order_ok and tested != base:
        diff_probe = subprocess.run(
            ["git", "diff", "--name-only", tested, base],
            cwd=ROOT, capture_output=True, text=True)
        if diff_probe.returncode != 0:
            drift_note = f"diff failed: {diff_probe.stderr.strip()[:80]}"
        else:
            drift = diff_probe.stdout.splitlines()
            unowned = [p for p in drift if p not in OWNED_EVIDENCE_PATHS]
            owned_drift_ok = not unowned
            drift_note = (f"evidence-only drift ({len(drift)} files)"
                          if owned_drift_ok else f"unowned={unowned}")
    elif order_ok:
        owned_drift_ok = True
        drift_note = "tested==base"
    record("C9", "sha semantics (real commits, tested<=base<=head, "
           "owned-only drift)",
           sha_ok and order_ok and owned_drift_ok,
           f"tested={tested[:12]} base={base[:12]} head={head[:12]} ({drift_note})")
    record("C9b", "NEXT_PROMPT_ALLOWED sha binding mirrors PHASE_RESULT",
           nxt.get("tested_git_sha") == tested
           and nxt.get("evidence_generation_base_sha") == base)

    # C10 evidence_chain entries: committed artifacts must exist with
    # matching hashes; ci_generated artifacts must match when present.
    for entry in result.get("evidence_chain", []):
        path = chain_root / entry["path"]
        storage = entry.get("storage")
        if storage == "committed":
            ok = path.exists() and (
                not entry.get("sha256") or sha256_file(path) == entry["sha256"])
            record("C10", f"committed chain artifact {entry['path']}", ok,
                   "missing/hash mismatch" if not ok else "")
        elif entry.get("sha256_at_generation"):
            # review F8: a recorded ci_generated entry missing from the
            # checkout is "allowed absent" only in non-strict mode; strict
            # machine mode (CI/post-gate) demands its presence.
            if not path.exists():
                record("C10", f"ci_generated artifact {entry['path']}",
                       not args.strict_machine,
                       "missing from checkout"
                       + (" (strict mode requires it)" if args.strict_machine
                          else " (clean-checkout mode: allowed absent)"))
            else:
                same = sha256_file(path) == entry["sha256_at_generation"]
                record("C10", f"ci_generated artifact {entry['path']}", True,
                       "present; byte-identical" if same else
                       "present; regenerated after generation (acceptable — "
                       "semantic check C4 governs)")

    # C11 policy required suites all PASS in summary
    by_tag = {s.get("tag"): s for s in suites}
    drift = [name for name in policy.get("required_suites", [])
             if by_tag.get(name, {}).get("status") != "PASS"]
    record("C11", "policy required suites all PASS in summary",
           not drift, f"non-pass={drift}" if drift else f"{len(policy['required_suites'])} suites")

    # C12 graph state
    record("C12", "graph state follows sealed gain conclusion",
           result.get("graph", {}).get("gain_conclusion") == policy.get("graph_gain_conclusion")
           and result.get("graph", {}).get("state") == (
               "ON_ELIGIBLE" if policy.get("graph_gain_conclusion") == "GAIN"
               else "OFF_NO_GAIN")
           and nxt.get("graph") == result.get("graph"))

    # C13 authority state vs live environment channel.
    # review F2: the mandatory set is pinned at code level — a policy edit
    # stripping RT-101 cannot shrink the validated authority set, and the
    # committed PHASE_RESULT must carry a state entry for every authority.
    policy_requirements = policy.get("required_authorities", {}) or {}
    fresh = authority_results_from_env(policy_requirements, root=ROOT)
    required_ids = sorted(MANDATORY_AUTHORITIES | set(policy_requirements))
    doc_authorities = result.get("authorities") or {}
    missing_doc = [a for a in required_ids if a not in doc_authorities]
    record("C13pre", "PHASE_RESULT carries every mandatory authority entry",
           not missing_doc, f"missing={missing_doc}" if missing_doc else
           f"{len(required_ids)} authorities")
    undeclared_now = sorted(MANDATORY_AUTHORITIES - set(policy_requirements))
    record("C13policy", "policy still declares mandatory authorities",
           not undeclared_now,
           f"stripped={undeclared_now}" if undeclared_now else
           f"{len(policy_requirements)} declared")
    for authority_id in required_ids:
        fresh_result = fresh.get(authority_id)
        doc_authority = doc_authorities.get(authority_id) or {}
        live_satisfied = fresh_result.satisfied if fresh_result else False
        record("C13", f"authority {authority_id} state matches live channel",
               doc_authority.get("satisfied") == live_satisfied,
               f"doc={doc_authority.get('satisfied')} live={live_satisfied}")
    rt101 = doc_authorities.get(RT101_AUTHORITY_ID) or {}
    record("C13b", "RT-101 unsatisfied keeps phase fail-closed",
           (rt101.get("satisfied") is False) == (phase_status != "PASS"
                                                 or decision.get("core_eligible") is not True)
           or rt101.get("satisfied") is True)

    # C14 external-state freshness markers
    external = json.loads((ROOT / policy.get("external_state", "spec/phase09_external_state.json"))
                          .read_text("utf-8"))
    stale = []
    for control_id, row in external.get("controls", {}).items():
        evidence = row.get("evidence") or {}
        if not evidence.get("observed_at"):
            stale.append(f"{control_id}: observed_at missing")
        if row.get("satisfied") is not True and not evidence.get("re_verified_at"):
            stale.append(f"{control_id}: re_verified_at missing")
    record("C14", "external-state freshness markers present", not stale,
           "; ".join(stale) if stale else f"{len(external.get('controls', {}))} controls")
    if not args.strict_machine:
        try:
            blockers = load_external_blockers(ROOT / policy["external_state"])
            doc_blockers = sorted(result.get("external_blockers", {}))
            record("C14b", "external blockers match repo external-state rows",
                   doc_blockers == sorted(blockers),
                   f"doc={doc_blockers} state={sorted(blockers)}")
        except ValueError as exc:
            record("C14b", "external blockers match repo external-state rows",
                   False, str(exc))

    return _finish()


def _finish() -> int:
    failed = [c for c in CHECKS if not c["pass"]]
    print("=" * 66)
    total = len(CHECKS)
    print(f"  Phase09 evidence chain: {total - len(failed)}/{total} checks passed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
