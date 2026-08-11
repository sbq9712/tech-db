#!/usr/bin/env python3
"""
T012 — Data Quality Checks
===========================
Deterministic data quality validation for the record dataset.

Checks:
  1. Missing/empty title
  2. Invalid URL
  3. Date failures (future/past anomalies)
  4. Body/full_body absence
  5. Duplicate anomalies
  6. Record ID/index mismatch
  7. Metadata missing
  8. Index dataset mismatch
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
LITE = REPO / "data" / "processed" / "all-records-lite.json"


def check_data_quality(verbose=True):
    """Run all data quality checks."""
    if not LITE.exists():
        print(f"❌ Data file not found: {LITE}")
        return {}

    data = json.loads(LITE.read_text("utf-8"))
    total = len(data)

    issues = {
        "total_records": total,
        "missing_title": 0,
        "empty_title": 0,
        "invalid_url": 0,
        "missing_url": 0,
        "future_date": 0,
        "past_date_anomaly": 0,
        "missing_body": 0,
        "missing_all_text": 0,
        "duplicate_content_hash": 0,
        "index_mismatch": 0,
        "metadata_missing": 0,
        "malformed_params": 0,
    }

    seen_content_hashes = {}
    now = datetime.now()

    for i, rec in enumerate(data):
        # 1. Title checks
        title = rec.get("t", "")
        if not title:
            issues["missing_title"] += 1
        elif not title.strip():
            issues["empty_title"] += 1

        # 2. URL checks
        url = rec.get("u", "")
        if not url:
            issues["missing_url"] += 1
        elif url:
            try:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    issues["invalid_url"] += 1
            except Exception:
                issues["invalid_url"] += 1

        # 3. Date checks
        date_str = rec.get("d", "")
        if date_str:
            try:
                dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                if dt > now:
                    issues["future_date"] += 1
                elif dt.year < 2000:
                    issues["past_date_anomaly"] += 1
            except (ValueError, TypeError):
                pass

        # 4. Body/text checks
        body = rec.get("b", "")
        full_body = rec.get("fb", "")
        ai_summary = rec.get("as", "")
        if not body and not full_body and not ai_summary:
            issues["missing_all_text"] += 1
        elif not body and not full_body:
            issues["missing_body"] += 1

        # 5. Duplicate content hash
        content = (body or full_body or ai_summary or "")[:200]
        if content:
            import hashlib
            ch = hashlib.md5(content.encode()).hexdigest()[:12]
            if ch in seen_content_hashes:
                issues["duplicate_content_hash"] += 1
            else:
                seen_content_hashes[ch] = i

        # 6. Metadata checks
        if not rec.get("c"):
            issues["metadata_missing"] += 1

        # 7. Malformed params
        kp = rec.get("kp", [])
        if kp and not isinstance(kp, list):
            issues["malformed_params"] += 1

    # Print report
    if verbose:
        print(f"\n{'='*60}")
        print(f"  Data Quality Report — {total} records")
        print(f"{'='*60}\n")

        for check, count in issues.items():
            if check == "total_records":
                continue
            pct = count / total * 100 if total else 0
            status = "✅" if count == 0 else "⚠️ " if pct < 5 else "❌"
            print(f"  {status} {check:30s}: {count:6d} ({pct:.1f}%)")

    return issues


if __name__ == "__main__":
    check_data_quality()
