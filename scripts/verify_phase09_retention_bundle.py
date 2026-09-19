#!/usr/bin/env python3
"""Q-336 retention verifier — validate a durable-retention receipt.

The owner uploads the retention bundle (export_phase09_retention_bundle.py)
to a durable external store (>=180 days) and receives a retention receipt
JSON from that store. This script verifies:

1. bundle integrity: every manifest file digest + the bundle digest,
2. receipt plausibility: retention_days >= 180, stored_at <= now,
   bundle_sha256 binding, non-empty store identity.

It emits the machine-readable artifact (rt075-... no: q336-retention-1.0)
whose FILE sha256 is the `artifact_sha256` an owner-provisioned
EXTERNAL_CONTROL_SATISFACTION proof for Q-336 must declare. It never
clears Q-336 by itself and never fabricates receipts: a receipt is only
as trustworthy as the store that issued it, which is exactly why the
satisfaction proof must come from the owner's secret channel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_SCHEMA_VERSION = "q336-retention-receipt-1.0"
MIN_RETENTION_DAYS = 180


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="bundle .tar.gz")
    parser.add_argument("--manifest", required=True,
                        help="bundle manifest .json")
    parser.add_argument("--receipt", required=True,
                        help="retention receipt JSON from the durable store")
    parser.add_argument("--out", help="write verified receipt artifact here")
    args = parser.parse_args()

    findings: list[str] = []
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))

    # 0. manifest contract: expected schema + COMPLETE bundle (a bundle
    # missing expected evidence artifacts must never verify)
    if manifest.get("schema_version") != "phase09-retention-bundle-1.0":
        findings.append("manifest schema_version")
    missing = manifest.get("missing_files") or []
    if missing:
        findings.append(f"bundle incomplete, missing: {', '.join(missing)}")

    # 1. bundle digest
    actual_bundle = sha256_file(args.bundle)
    if actual_bundle != manifest.get("bundle_sha256"):
        findings.append("bundle_sha256 mismatch")
    # 2. per-file digests inside the tarball
    with tarfile.open(args.bundle, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        for entry in manifest.get("files", []):
            member = members.get(entry["path"])
            if member is None:
                findings.append(f"missing in bundle: {entry['path']}")
                continue
            data = tar.extractfile(member).read()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                findings.append(f"digest mismatch: {entry['path']}")
    # 3. receipt contract
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        findings.append("receipt schema_version")
    try:
        retention_days = float(receipt.get("retention_days", 0))
    except (TypeError, ValueError):
        retention_days = 0.0
        findings.append("retention_days unreadable")
    if retention_days < MIN_RETENTION_DAYS:
        findings.append(
            f"retention_days {retention_days} < {MIN_RETENTION_DAYS}")
    if receipt.get("bundle_sha256") != actual_bundle:
        findings.append("receipt does not bind this bundle")
    stored_at = receipt.get("stored_at_utc", "")
    try:
        if datetime.fromisoformat(stored_at) > datetime.now(timezone.utc):
            findings.append("stored_at_utc in the future")
    except ValueError:
        findings.append("stored_at_utc unreadable")
    if not str(receipt.get("store_id") or "").strip():
        findings.append("store_id missing")
    if not str(receipt.get("store_uri") or "").strip():
        findings.append("store_uri missing")

    verified_receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "verified": not findings,
        "findings": findings,
        "retention_days": retention_days,
        "min_required_days": MIN_RETENTION_DAYS,
        "bundle_sha256": actual_bundle,
        "manifest_sha256": sha256_file(args.manifest),
        "store_id": receipt.get("store_id", ""),
        "store_uri": receipt.get("store_uri", ""),
        "stored_at_utc": stored_at,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "issuer": receipt.get("issuer", "owner-provided-receipt"),
    }
    rendered = json.dumps(verified_receipt, ensure_ascii=False, indent=2,
                          sort_keys=True)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not findings:
        print("\nartifact_sha256 for the EXTERNAL_CONTROL_SATISFACTION "
              "proof =", hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
              file=__import__("sys").stderr)
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
