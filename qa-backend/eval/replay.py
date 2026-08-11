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
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO))

TRACE_DIR = REPO / "runtime" / "traces"


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
        "question": original_query,
        "previous_answer": result.get("answer", ""),
        "previous_status": result.get("answer_status", ""),
        "previous_stop_reason": result.get("stop_reason", ""),
        "previous_citations": result.get("citations", []),
        "previous_cited_ids": result.get("cited_record_ids", []),
        "stages": trace.get("stages", []),
        # Ground truth is derived from previous answer (human-confirmed)
        "expected_records": result.get("cited_record_ids", []),
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
    args = parser.parse_args()

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
