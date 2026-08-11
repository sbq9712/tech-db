"""Evaluation report generation."""
import json
from pathlib import Path
from datetime import datetime


def generate_report(results: list, mode: str = "retrieval") -> dict:
    """Generate a structured evaluation report.

    Args:
        results: List of per-case result dicts
        mode: "retrieval" or "end_to_end"

    Returns:
        Structured report dict
    """
    report = {
        "mode": mode,
        "timestamp": datetime.now().isoformat(),
        "total_cases": len(results),
        "metrics": {},
        "per_case": results,
    }

    if mode == "retrieval":
        # Aggregate retrieval metrics
        recalls = [r.get("recall@25", 0) for r in results]
        mrrs = [r.get("mrr", 0) for r in results]
        ndcgs = [r.get("ndcg@25", 0) for r in results]
        report["metrics"] = {
            "avg_recall@25": sum(recalls) / len(recalls) if recalls else 0,
            "avg_mrr": sum(mrrs) / len(mrrs) if mrrs else 0,
            "avg_ndcg@25": sum(ndcgs) / len(ndcgs) if ndcgs else 0,
            "hit_rate": sum(1 for r in recalls if r > 0) / len(recalls) if recalls else 0,
        }

    elif mode == "end_to_end":
        # Aggregate end-to-end metrics
        report["metrics"] = {
            "answer_status_counts": _count_statuses(results),
            "avg_citation_precision": _safe_avg([r.get("citation_precision", 0) for r in results]),
            "avg_claim_support_rate": _safe_avg([r.get("claim_support_rate", 0) for r in results]),
            "avg_unsupported_claim_rate": _safe_avg([r.get("unsupported_claim_rate", 0) for r in results]),
            "exact_span_validity": _safe_avg([r.get("exact_span_validity", 0) for r in results]),
            "abstention": _abstention_summary(results),
        }

    return report


def save_report(report: dict, output_path: str = None) -> str:
    """Save report to JSON file. Returns the path."""
    if output_path is None:
        output_path = "evaluation_report.json"
    Path(output_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def print_report(report: dict):
    """Print a human-readable report summary."""
    print(f"\n{'='*70}")
    print(f"  Evaluation Report — {report.get('mode', '?')} mode")
    print(f"  Cases: {report.get('total_cases', 0)}")
    print(f"  Time: {report.get('timestamp', '?')}")
    print(f"{'='*70}\n")

    metrics = report.get("metrics", {})
    if report.get("mode") == "retrieval":
        print(f"  Recall@25:     {metrics.get('avg_recall@25', 0):.2%}")
        print(f"  MRR:           {metrics.get('avg_mrr', 0):.4f}")
        print(f"  nDCG@25:       {metrics.get('avg_ndcg@25', 0):.4f}")
        print(f"  Hit Rate:      {metrics.get('hit_rate', 0):.2%}")
    elif report.get("mode") == "end_to_end":
        print(f"  Answer Status Distribution:")
        for status, count in metrics.get("answer_status_counts", {}).items():
            print(f"    {status}: {count}")
        print(f"\n  Citation Precision:       {metrics.get('avg_citation_precision', 0):.2%}")
        print(f"  Claim Support Rate:       {metrics.get('avg_claim_support_rate', 0):.2%}")
        print(f"  Unsupported Claim Rate:   {metrics.get('avg_unsupported_claim_rate', 0):.2%}")
        print(f"  Exact Span Validity:      {metrics.get('exact_span_validity', 0):.2%}")

        abst = metrics.get("abstention", {})
        if abst:
            print(f"\n  Abstention:")
            print(f"    Correct Abstention Rate: {abst.get('correct_abstention_rate', 0):.2%}")
            print(f"    False Refusal Rate:      {abst.get('false_refusal_rate', 0):.2%}")
            print(f"    Partial Answer Accuracy: {abst.get('partial_answer_accuracy', 0):.2%}")

    print(f"\n{'='*70}\n")


def _safe_avg(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def _count_statuses(results: list) -> dict:
    counts = {}
    for r in results:
        status = r.get("answer_status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _abstention_summary(results: list) -> dict:
    actual = [r.get("answer_status", "UNSUPPORTED") for r in results]
    expected = [r.get("expected_status", "SHOULD_ANSWER") for r in results]
    from metrics import abstention_accuracy
    return abstention_accuracy(actual, expected)
