#!/usr/bin/env python3
"""
T035 — Trace Replay / Regression Replay
========================================
Replay historical bad cases to detect regressions after model/prompt/index changes.

Usage:
    python eval/replay.py --trace-id <id>     # Replay single trace
    python eval/replay.py --date 2026-08-11   # Replay all traces from a date
    python eval/replay.py --bad-only           # Replay only failed traces

Outputs before/after diff for comparison.
"""
import json
import sys
import os
import asyncio
import argparse
import hashlib
import inspect
import re
from enum import Enum
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO))

TRACE_DIR = REPO / "runtime" / "traces"


class ReplayFidelity(str, Enum):
    HISTORICAL_EXACT = "HISTORICAL_EXACT"
    HISTORICAL_ARTIFACTS_CURRENT_MODEL = "HISTORICAL_ARTIFACTS_CURRENT_MODEL"
    CURRENT_COMPARISON = "CURRENT_COMPARISON"
    PARTIAL_REPLAY = "PARTIAL_REPLAY"


class ReplayDataError(ValueError):
    pass


_NONMODEL_PINS = (
    "original_query", "manifest_id", "manifest_artifact_hashes",
    "prompt_template_id", "prompt_content_hash", "profile",
    "feature_flags_hash", "deterministic_inputs", "source_snapshot_ids",
    "identity_snapshot_id",
)
_HISTORICAL_ARTIFACTS = (
    "manifest", "prompt", "source_snapshots", "identity_snapshot",
    "deterministic_config",
)


def inspect_historical_artifacts(case: dict) -> set[str]:
    """Verify caller-supplied historical artifact paths and hashes.

    A manifest id or a boolean is never artifact availability.  Each required
    artifact must resolve to an existing file whose sha256 matches the pinned
    descriptor.  Audit callers may instead supply an independently trusted
    resolver result to ``classify_replay_fidelity``.
    """
    available = set()
    descriptors = case.get("historical_artifact_paths") or {}
    if not isinstance(descriptors, dict):
        return available
    for name in _HISTORICAL_ARTIFACTS:
        item = descriptors.get(name)
        if not isinstance(item, dict):
            continue
        path, expected = item.get("path"), str(item.get("sha256") or "")
        if not path or len(expected) != 64:
            continue
        candidate = Path(path)
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        if actual == expected:
            available.add(name)
    return available


def classify_replay_fidelity(case: dict, *, requested_mode: str = "",
                             historical_model_available: bool = False,
                             artifact_availability=None) -> dict:
    """Truthfully classify what the canonical trace can reproduce."""
    if not isinstance(case, dict) or not str(case.get("trace_id") or ""):
        raise ReplayDataError("replay case requires trace_id")
    if artifact_availability is None:
        available = inspect_historical_artifacts(case)
    elif isinstance(artifact_availability, dict):
        available = {str(k) for k, value in artifact_availability.items()
                     if value is True}
    else:
        available = {str(value) for value in artifact_availability}
    missing = [name for name in _NONMODEL_PINS
               if not case.get(name) and name not in available]
    missing.extend(
        f"historical_artifact:{name}"
        for name in _HISTORICAL_ARTIFACTS if name not in available)
    requested = str(requested_mode or case.get("requested_mode") or "").upper()
    if requested == ReplayFidelity.CURRENT_COMPARISON.value:
        mode = ReplayFidelity.CURRENT_COMPARISON
    elif not missing and (case.get("model_identity") or
                          "model_identity" in available) \
            and historical_model_available:
        mode = ReplayFidelity.HISTORICAL_EXACT
    elif not missing:
        mode = ReplayFidelity.HISTORICAL_ARTIFACTS_CURRENT_MODEL
        if not case.get("model_identity") and "model_identity" not in available:
            missing.append("model_identity")
        missing.append("historical_model_runtime")
    else:
        mode = ReplayFidelity.PARTIAL_REPLAY
    return {
        "fidelity_mode": mode.value,
        "missing_components": sorted(set(missing)),
        "historical_model_available": bool(historical_model_available),
        "exact_replay_claim": mode is ReplayFidelity.HISTORICAL_EXACT,
    }


def deterministic_output_diff(before: dict, after: dict) -> dict:
    """Stable machine diff; no claim that external generation is deterministic."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    keys = sorted(set(before) | set(after))
    return {
        "changed_fields": [key for key in keys if before.get(key) != after.get(key)],
        "before_sha256": __import__("hashlib").sha256(json.dumps(
            before, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode()).hexdigest(),
        "after_sha256": __import__("hashlib").sha256(json.dumps(
            after, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode()).hexdigest(),
    }


def _current_version_identity() -> dict:
    fixture = REPO / "qa-backend" / "test_fixtures" / "mini_runtime"
    manifest = json.loads((fixture / "manifest.json").read_text("utf-8"))
    prompt = fixture / "prompt_config.json"
    return {
        "manifest_id": manifest.get("fixture_id", ""),
        "identity_snapshot_id": manifest.get("identity_snapshot_id", ""),
        "prompt_content_hash": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "profile": "FAST_RAG",
        "runtime_adapter": "committed-mini-runtime-phase03-v1",
    }


async def execute_current_case(case: dict) -> dict:
    """Execute one case through the current canonical Phase03 pipeline.

    The committed mini-runtime supplies deterministic source/index authority;
    a lexical adapter produces typed route results, then the real
    ``run_phase03_retrieval`` policy/selection/package path owns the result.
    No caller-provided current output participates.
    """
    query = str(case.get("original_query") or "")
    if not query:
        raise ReplayDataError("current replay requires original_query")
    fixture = REPO / "qa-backend" / "test_fixtures" / "mini_runtime"
    records = json.loads((fixture / "records.json").read_text("utf-8"))
    snapshots = json.loads(
        (fixture / "source_snapshots.json").read_text("utf-8"))
    metadata = json.loads(
        (fixture / "evidence_metadata.json").read_text("utf-8"))
    by_id = {str(row["record_id"]): dict(row) for row in records}
    snapshot_by_id = {str(row["record_id"]): row for row in snapshots}
    metadata_by_id = {str(row["record_id"]): row for row in metadata}
    tokens = set(re.findall(r"[A-Za-z0-9_-]+", query.lower()))
    ranked = []
    for row in records:
        rid = str(row["record_id"])
        text = (str(row.get("title") or "") + " " +
                str((snapshot_by_id.get(rid) or {}).get("evidence_text") or ""))
        overlap = len(tokens & set(re.findall(
            r"[A-Za-z0-9_-]+", text.lower())))
        ranked.append((overlap, rid, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    from retrieval.vector import RetrievalResult
    route_results = {"vector": [], "bm25": []}
    for rank, (overlap, rid, row) in enumerate(ranked, 1):
        score = float(overlap) + (1.0 / (100 + rank))
        meta = {"record_id": rid, "t": row.get("title", ""),
                "fb": (snapshot_by_id.get(rid) or {}).get(
                    "evidence_text", "")[:200]}
        for route in route_results:
            route_results[route].append(RetrievalResult(
                record_id=rid, legacy_idx=row.get("legacy_idx"), route=route,
                raw_score=score, rank=rank, meta=dict(meta),
                route_details={"adapter": "committed-mini-runtime"}))
    from phase03_pipeline import run_phase03_retrieval
    result = await run_phase03_retrieval(
        query=query, route_results=route_results, mode="FAST_RAG",
        records_by_id=by_id, snapshot_index=snapshot_by_id,
        evidence_metadata=metadata_by_id,
        get_record_fn=lambda rid: by_id.get(rid), access_scope="public")
    context = str(result.get("context") or "")
    package = result.get("package_dict") or {}
    return {
        "pipeline_status": str(result.get("status") or ""),
        "selected_record_ids": list(result.get("selected_record_ids") or []),
        "citation_record_ids": [str(row.get("record_id") or "")
                                for row in result.get("citations") or []],
        "generator_input_sha256": hashlib.sha256(context.encode()).hexdigest(),
        "package_binding_hash": str(package.get("binding_hash") or ""),
    }


def _version_differences(historical: dict, current: dict) -> dict:
    differences = {}
    for key in sorted(set(historical) | set(current)):
        old, new = historical.get(key), current.get(key)
        if old != new:
            differences[key] = {"historical": old, "current": new}
    return differences


async def replay_case_group_async(
        document: dict, *, current_versions: dict | None = None,
        historical_model_available: bool = False, executor=None,
        compare_only: bool = False, artifact_availability_resolver=None) -> dict:
    """Execute a bounded case group or explicitly compare precomputed data."""
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise ReplayDataError("case group must contain a cases list")
    cases = document["cases"]
    if not cases or len(cases) > 100:
        raise ReplayDataError("case group size must be between 1 and 100")
    resolved_versions = _current_version_identity()
    resolved_versions.update(current_versions or {})
    executor = executor or execute_current_case
    rows = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ReplayDataError("each replay case must be an object")
        availability = (artifact_availability_resolver(raw)
                        if artifact_availability_resolver else None)
        fidelity = classify_replay_fidelity(
            raw, requested_mode=raw.get("requested_mode", ""),
            historical_model_available=historical_model_available,
            artifact_availability=availability)
        historical_raw = raw.get("historical_output") or {}
        current_raw = raw.get("current_output") or {}
        if not isinstance(historical_raw, dict):
            raise ReplayDataError("historical_output must be an object")
        if compare_only and not isinstance(current_raw, dict):
            raise ReplayDataError("current_output must be an object")
        historical = dict(historical_raw)
        execution_error = ""
        if compare_only:
            current = dict(current_raw)
        else:
            try:
                current = executor(raw)
                if inspect.isawaitable(current):
                    current = await current
                if not isinstance(current, dict):
                    raise ReplayDataError("current executor must return an object")
            except Exception as exc:  # fail closed, preserve machine report
                current = {}
                execution_error = f"{type(exc).__name__}:{str(exc)[:160]}"
        historical_versions = dict(raw.get("versions") or {})
        for key in ("manifest_id", "model_identity", "prompt_template_id",
                    "prompt_content_hash", "profile", "feature_flags_hash",
                    "identity_snapshot_id"):
            if raw.get(key) and key not in historical_versions:
                historical_versions[key] = raw[key]
        version_differences = _version_differences(
            historical_versions, resolved_versions)
        case_id = str(raw.get("case_id") or raw.get("trace_id") or "")
        rows.append({
            "trace_id": raw["trace_id"],
            "case_id": case_id,
            **fidelity,
            "execution_mode": "COMPARE_ONLY" if compare_only else "EXECUTE_CURRENT",
            "current_output_authority": (
                "PRECOMPUTED_COMPARISON" if compare_only
                else "EXECUTED_CURRENT_PIPELINE"),
            "historical_versions": historical_versions,
            "current_versions": dict(resolved_versions),
            "version_differences": version_differences,
            "manifest_differences": {
                k: v for k, v in version_differences.items()
                if "manifest" in k or "snapshot" in k},
            "model_differences": {
                k: v for k, v in version_differences.items() if "model" in k},
            "prompt_differences": {
                k: v for k, v in version_differences.items() if "prompt" in k},
            "profile_config_differences": {
                k: v for k, v in version_differences.items()
                if k in {"profile", "feature_flags_hash", "deterministic_inputs"}},
            "execution_result": current,
            "execution_error": execution_error,
            "supplied_current_output_ignored": (
                not compare_only and "current_output" in raw),
            "output_diff": deterministic_output_diff(historical, current),
            "external_nondeterminism_reproduced": False,
        })
    counts = {mode.value: sum(1 for row in rows
                              if row["fidelity_mode"] == mode.value)
              for mode in ReplayFidelity}
    return {
        "schema_version": "trace-replay-report-2.0",
        "case_group_id": str(document.get("case_group_id") or "unnamed"),
        "total_cases": len(rows),
        "execution_mode": "COMPARE_ONLY" if compare_only else "EXECUTE_CURRENT",
        "has_execution_errors": any(row["execution_error"] for row in rows),
        "fidelity_counts": counts,
        "cases": rows,
    }


def replay_case_group(document: dict, **kwargs) -> dict:
    """Synchronous compatibility wrapper around executable group replay."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(replay_case_group_async(document, **kwargs))
    raise RuntimeError(
        "replay_case_group cannot run synchronously inside an event loop; "
        "await replay_case_group_async instead")


def load_traces(date: str = None, trace_id: str = None, bad_only: bool = False) -> list:
    """Load trace records from JSONL files.

    Args:
        date: Specific date (YYYY-MM-DD) or None for all
        trace_id: Specific trace_id or None for all
        bad_only: Only load traces with non-SUPPORTED status

    Returns:
        List of trace dicts
    """
    traces = []

    if trace_id:
        # Search all files for this trace_id
        files = sorted(TRACE_DIR.glob("*.jsonl"))
    else:
        date_str = date or datetime.now().strftime("%Y-%m-%d")
        files = [TRACE_DIR / f"{date_str}.jsonl"]
        if not files[0].exists():
            # Try all files
            files = sorted(TRACE_DIR.glob("*.jsonl"))

    for f in files:
        if not f.exists():
            continue
        try:
            for line in f.read_text("utf-8").strip().split("\n"):
                if not line:
                    continue
                record = json.loads(line)
                if trace_id and record.get("trace_id") != trace_id:
                    continue
                if bad_only:
                    status = record.get("result", {}).get("answer_status", "")
                    if status == "SUPPORTED":
                        continue
                traces.append(record)
        except (json.JSONDecodeError, OSError):
            continue

    return traces


def create_replay_case(trace: dict) -> dict:
    """Convert a trace into a replayable test case.

    Returns a case dict that can be used for regression testing.
    """
    result = trace.get("result", {})
    original_query = trace.get("original_query", "")

    return {
        "trace_id": trace.get("trace_id", ""),
        "timestamp": trace.get("timestamp", ""),
        "original_query": original_query,
        "question": original_query,
        "previous_answer": result.get("answer", ""),
        "previous_status": result.get("answer_status", ""),
        "previous_stop_reason": result.get("stop_reason", ""),
        "previous_citations": result.get("citations", []),
        "previous_cited_ids": result.get("cited_record_ids", []),
        "stages": trace.get("stages", []),
        # Ground truth is derived from previous answer (human-confirmed)
        "expected_records": result.get("cited_record_ids", []),
        "manifest_id": trace.get("manifest_id", ""),
        "model_identity": trace.get("model_identity", ""),
        "prompt_template_id": trace.get("prompt_template_id", ""),
        "profile": trace.get("profile", ""),
        "feature_flags_hash": trace.get("feature_flags_hash", ""),
        "deterministic_inputs": trace.get("deterministic_inputs"),
        "manifest_artifact_hashes": trace.get("manifest_artifact_hashes") or {},
        "prompt_content_hash": trace.get("prompt_content_hash", ""),
        "source_snapshot_ids": trace.get("source_snapshot_ids") or [],
        "identity_snapshot_id": trace.get("identity_snapshot_id", ""),
        "historical_artifact_paths": trace.get("historical_artifact_paths") or {},
    }


async def replay_trace(trace: dict, search_fn=None) -> dict:
    """Replay a single trace through the current pipeline.

    Args:
        trace: Original trace record
        search_fn: Async function to execute search

    Returns:
        Before/after comparison dict
    """
    case = create_replay_case(trace)
    question = case["question"]

    if not question:
        return {"error": "no question in trace", "trace_id": case["trace_id"]}

    # Execute current pipeline
    after = {
        "question": question,
        "answer": "",
        "answer_status": "",
        "cited_record_ids": [],
    }

    if search_fn:
        try:
            results, is_relevant, status = await search_fn(question)
            after["results_count"] = len(results) if results else 0
            after["is_relevant"] = is_relevant
            after["retrieved_ids"] = [r.get("meta", {}).get("idx", -1) for r in (results or [])[:10]]
        except Exception as e:
            after["error"] = str(e)

    # Compute diff
    before_ids = set(case["previous_cited_ids"])
    after_ids = set(after.get("cited_record_ids", []))
    after_retrieved = set(after.get("retrieved_ids", []))

    # Check if previously-found records are still found
    retained = before_ids & after_retrieved
    lost = before_ids - after_retrieved
    new = after_retrieved - before_ids

    return {
        "trace_id": case["trace_id"],
        "question": question[:80],
        "before": {
            "answer_status": case["previous_status"],
            "cited_ids": case["previous_cited_ids"],
            "answer_preview": case["previous_answer"][:100],
        },
        "after": {
            "results_count": after.get("results_count", 0),
            "retrieved_ids": after.get("retrieved_ids", []),
            "is_relevant": after.get("is_relevant", False),
            "error": after.get("error"),
        },
        "diff": {
            "retained_count": len(retained),
            "lost_count": len(lost),
            "new_count": len(new),
            "lost_ids": list(lost)[:10],
            "new_ids": list(new)[:10],
            "status_changed": case["previous_status"] != after.get("answer_status", ""),
        },
    }


def generate_replay_report(results: list) -> dict:
    """Generate a summary report from replay results."""
    total = len(results)
    if not total:
        return {"total": 0}

    retained_avg = sum(r.get("diff", {}).get("retained_count", 0) for r in results) / total
    lost_avg = sum(r.get("diff", {}).get("lost_count", 0) for r in results) / total
    new_avg = sum(r.get("diff", {}).get("new_count", 0) for r in results) / total
    status_changes = sum(1 for r in results if r.get("diff", {}).get("status_changed"))
    errors = sum(1 for r in results if r.get("after", {}).get("error"))

    return {
        "total_cases": total,
        "retained_avg": round(retained_avg, 1),
        "lost_avg": round(lost_avg, 1),
        "new_avg": round(new_avg, 1),
        "status_changes": status_changes,
        "errors": errors,
        "retention_rate": round(retained_avg / max(retained_avg + lost_avg, 1), 2),
    }


async def main():
    parser = argparse.ArgumentParser(description="Trace Replay for Regression Testing")
    parser.add_argument("--trace-id", help="Replay specific trace_id")
    parser.add_argument("--date", help="Replay all traces from date (YYYY-MM-DD)")
    parser.add_argument("--bad-only", action="store_true", help="Only replay non-SUPPORTED traces")
    parser.add_argument("--limit", type=int, default=20, help="Max cases to replay")
    parser.add_argument("--case-group", help="Bounded JSON case-group input")
    parser.add_argument("--output", help="Machine-readable report path")
    parser.add_argument("--current-model", default="")
    parser.add_argument("--current-manifest", default="")
    parser.add_argument("--compare-only", action="store_true",
                        help="compare supplied outputs without claiming execution")
    args = parser.parse_args()

    if args.case_group:
        try:
            source = Path(args.case_group)
            document = json.loads(source.read_text("utf-8"))
            overrides = {}
            if args.current_model:
                overrides["model_identity"] = args.current_model
            if args.current_manifest:
                overrides["manifest_id"] = args.current_manifest
            report = await replay_case_group_async(
                document, current_versions=overrides,
                compare_only=args.compare_only)
            destination = Path(args.output or (REPO / "replay_report.json"))
            destination.write_text(json.dumps(
                report, ensure_ascii=False, indent=2) + "\n", "utf-8")
            print(f"Replay group: {report['case_group_id']} ({report['total_cases']} cases)")
            for mode, count in report["fidelity_counts"].items():
                print(f"  {mode}: {count}")
            print(f"Machine report: {destination}")
            if report["has_execution_errors"]:
                raise SystemExit(2)
            return
        except (OSError, json.JSONDecodeError, ReplayDataError) as exc:
            print(f"replay data rejected: {exc}", file=sys.stderr)
            raise SystemExit(2)

    traces = load_traces(date=args.date, trace_id=args.trace_id, bad_only=args.bad_only)
    print(f"Loaded {len(traces)} traces for replay")

    if not traces:
        print("No traces found. Try a different date or --bad-only flag.")
        return

    traces = traces[:args.limit]

    # Try to load search function
    search_fn = None
    try:
        import server as srv
        srv.load_vector_index()
        srv.load_bm25_index()
        srv.load_graph_index()
        srv.load_records()
        search_fn = srv.hybrid_search
        print("Search function loaded")
    except Exception as e:
        print(f"Warning: Could not load search function: {e}")
        print("Running retrieval-only replay (no live search)")

    results = []
    for trace in traces:
        result = await replay_trace(trace, search_fn)
        results.append(result)

        q = result.get("question", "?")
        retained = result.get("diff", {}).get("retained_count", 0)
        lost = result.get("diff", {}).get("lost_count", 0)
        print(f"  [{result.get('trace_id', '?')[:8]}] retained={retained} lost={lost} | {q}")

    # Summary
    report = generate_replay_report(results)
    print(f"\n{'='*60}")
    print(f"  Replay Report")
    print(f"{'='*60}")
    print(f"  Total cases:     {report['total_cases']}")
    print(f"  Retained avg:    {report['retained_avg']}")
    print(f"  Lost avg:        {report['lost_avg']}")
    print(f"  New avg:         {report['new_avg']}")
    print(f"  Retention rate:  {report['retention_rate']:.0%}")
    print(f"  Status changes:  {report['status_changes']}")
    print(f"  Errors:          {report['errors']}")

    # Save report
    report_file = REPO / "replay_report.json"
    report["cases"] = results
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 Report saved to: {report_file}")


if __name__ == "__main__":
    asyncio.run(main())
