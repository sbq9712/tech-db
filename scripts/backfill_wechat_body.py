#!/usr/bin/env python3
"""Backfill missing body text for wechat-sourced records (parallel version)."""
from __future__ import annotations
import json, os, sys, csv, re, subprocess
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
WECHAT_REPO = "wodewoping-png/wechat-daily-news-csv"
TOKEN = os.environ.get("GH_TOKEN", "")

def download_csv(fname: str) -> tuple[str, str | None]:
    """Download a wechat CSV from GitHub API."""
    api_url = f"https://api.github.com/repos/{WECHAT_REPO}/contents/csv/{fname}"
    tmp = f"/tmp/backfill_{fname}"
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    subprocess.run(
        ["curl", "-4", "-sSL", "-H", "Accept: application/vnd.github.v3.raw",
         "-H", f"Authorization: Bearer {TOKEN}",
         "--connect-timeout", "15", "--max-time", "60",
         api_url, "-o", tmp],
        capture_output=True, timeout=90
    )
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        return fname, tmp
    return fname, None

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url.lower().rstrip("/"))
    return f"URL:{parsed.netloc}{parsed.path}"

def main():
    print("Loading lite JSON...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  {len(data)} records", flush=True)
    no_body_count = sum(1 for r in data if not r.get("b", "").strip())
    print(f"  {no_body_count} records without body", flush=True)

    # Build lookup maps
    url_map = {}
    title_map = {}
    for i, r in enumerate(data):
        u = normalize_url(r.get("u", ""))
        if u:
            url_map[u] = i
        t = r.get("t", "").strip().lower()[:80]
        if t:
            title_map[t] = i

    # Get file list
    print("Fetching wechat file list...", flush=True)
    result = subprocess.run(
        ["curl", "-sSL", f"https://api.github.com/repos/{WECHAT_REPO}/contents/csv",
         "-H", f"Authorization: Bearer {TOKEN}"],
        capture_output=True, text=True, timeout=30
    )
    files_data = json.loads(result.stdout)
    csv_files = [f["name"] for f in files_data if f["name"].endswith(".csv")]
    print(f"  {len(csv_files)} CSV files found", flush=True)

    # Download all CSVs in parallel
    print("Downloading CSVs (8 workers)...", flush=True)
    downloaded = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(download_csv, f): f for f in csv_files}
        for future in as_completed(futures):
            fname, local_path = future.result()
            if local_path:
                downloaded[fname] = local_path
                print(f"  ✓ {fname}", flush=True)
            else:
                print(f"  ✗ {fname}", flush=True)
    print(f"Downloaded {len(downloaded)}/{len(csv_files)}", flush=True)

    # Parse and match
    updated = 0
    for fname, local_path in downloaded.items():
        try:
            with open(local_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                file_updates = 0
                for row in reader:
                    title = row.get("title", "").strip()
                    body = row.get("clean_text", row.get("content_preview", row.get("digest", "")))
                    url_val = row.get("url", "").strip()
                    pub_date = row.get("publish_time", "")
                    if not title or not body:
                        continue
                    u = normalize_url(url_val)
                    t = title.strip().lower()[:80]
                    for idx in [url_map.get(u), title_map.get(t)]:
                        if idx is not None and not data[idx].get("b", "").strip():
                            data[idx]["b"] = body[:10000]
                            if pub_date:
                                m = re.search(r'(\d{4}-\d{2}-\d{2})', pub_date)
                                if m:
                                    data[idx]["d"] = m.group(1)
                            updated += 1
                            file_updates += 1
                            break
                if file_updates:
                    print(f"  {fname}: +{file_updates} bodies", flush=True)
        except Exception as e:
            print(f"  [ERROR] Parse {fname}: {e}", flush=True)

    print(f"\nTotal bodies updated: {updated}", flush=True)
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    print("Saved lite JSON", flush=True)

    # Rebuild shards
    print("Rebuilding shards...", flush=True)
    sys.path.insert(0, str(REPO / "scripts"))
    from build_snapshot import build_snapshot
    n = build_snapshot(data)
    print(f"Rebuilt {n} shards", flush=True)

if __name__ == "__main__":
    main()
