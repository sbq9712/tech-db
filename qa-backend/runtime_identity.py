"""Canonical RT101 formal-runtime identity contract (single source).

One module defines (a) the critical runtime file set, (b) the canonical
code-digest algorithm, and (c) the live-identity schema — consumed by:

  * qa-backend/server.py  — /api/runtime_identity  (RUNNING process self-report)
  * scripts/verify_runtime_deploy_sync.py — pre-seal LIVE leg (expected values
    computed from the evaluated mirror tree + pin)
  * tests — regressions for the identity contract

Design invariants (owner directive, V10 prep):
  * The digest is computed from BYTES OF THE RUNNING TREE: for imported
    modules via their __file__ (loaded-code proof), for the remaining
    critical files from the same tree directory that served those imports.
  * The digest is order-independent (sorted), deterministic, and cheap
    enough for server startup (18 small source files).
  * Fail-closed by construction: any missing/unreadable file, missing git
    sha, or malformed store path yields identity fields of None — and the
    verifier treats None as DRIFT, never as a silent skip.
No secrets, no gold, no salt material ever enters this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

# ── [F] critical runtime files — everything that decides RT101 answer
# semantics or gates the formal capture. Owner §F list + behavior-bearing
# surfaces from the codex Review-A sweep + the compat/preflight gates
# themselves. Deliberately explicit; do not silently prune.
CRITICAL_FILES = (
    "qa-backend/answer_status.py",
    "qa-backend/claim_mapping.py",
    "qa-backend/verifier.py",
    "qa-backend/generator_input.py",
    "qa-backend/llm_json.py",
    "qa-backend/evidence_package.py",
    "qa-backend/server.py",
    "qa-backend/retrieval/runtime.py",
    "qa-backend/phase02_pipeline.py",
    "qa-backend/phase03_pipeline.py",
    # behavior-bearing surfaces (codex Review-A sweep):
    "qa-backend/config.py",
    "qa-backend/feature_flags.py",
    "qa-backend/citation_grounding.py",
    "qa-backend/answer_repair.py",
    "qa-backend/numeric_facts.py",
    "qa-backend/runtime_safety.py",
    # gate instrumentation whose bytes decide capture admissibility:
    "qa-backend/corpus_compatibility.py",
    "qa-backend/formal_preflight.py",
)

SCHEMA_VERSION = "rt101-formal-runtime-identity-1.0"
SERVICE_ROLE_FORMAL = "RT101_FORMAL"

# Imported modules whose __file__ anchors the LOADED tree (loaded-code proof).
# Everything else in CRITICAL_FILES is digested from the same tree dir.
_IMPORTED_ANCHORS = (
    "qa-backend/answer_status.py",
    "qa-backend/claim_mapping.py",
    "qa-backend/verifier.py",
    "qa-backend/generator_input.py",
    "qa-backend/llm_json.py",
    "qa-backend/evidence_package.py",
    "qa-backend/server.py",
    "qa-backend/retrieval/runtime.py",
    "qa-backend/phase02_pipeline.py",
    "qa-backend/phase03_pipeline.py",
    "qa-backend/config.py",
    "qa-backend/feature_flags.py",
    "qa-backend/citation_grounding.py",
    "qa-backend/answer_repair.py",
    "qa-backend/numeric_facts.py",
    "qa-backend/runtime_safety.py",
    "qa-backend/corpus_compatibility.py",
    "qa-backend/formal_preflight.py",
)


def _sha256_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def compute_code_digest(tree_root: Path,
                        loaded_anchor_files: dict[str, str] | None = None
                        ) -> tuple[str | None, dict[str, str]]:
    """Canonical code digest over CRITICAL_FILES of a runtime tree.

    loaded_anchor_files maps relpath -> ABSOLUTE path of the module that
    was actually IMPORTED by the running process (from __file__). Those
    bytes are digested from the loaded location — proving the RUNNING code,
    not the disk. All other critical files are read from tree_root.

    Returns (digest, per_file) where digest is None on any missing or
    unreadable input (fail closed).
    """
    loaded_anchor_files = loaded_anchor_files or {}
    per_file: dict[str, str] = {}
    rows: list[tuple[str, str]] = []
    for rel in CRITICAL_FILES:
        src = loaded_anchor_files.get(rel)
        p = Path(src) if src else (Path(tree_root) / rel)
        digest = _sha256_file(p)
        if digest is None:
            return None, {}
        per_file[rel] = digest
        rows.append((rel, digest))
    rows.sort()
    h = hashlib.sha256()
    for rel, digest in rows:
        h.update(rel.encode("utf-8"))
        h.update(b"\n")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest(), per_file


def git_sha_of(tree_root: Path) -> str | None:
    """Git sha of the repo containing tree_root; None when unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tree_root),
            capture_output=True, text=True, timeout=15)
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and len(sha) == 40 \
                and all(c in "0123456789abcdef" for c in sha):
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def fixture_manifest_id(tree_root: Path) -> str | None:
    """fixture_id of the mini-runtime manifest inside the loaded tree."""
    mf = Path(tree_root) / "qa-backend" / "test_fixtures" / "mini_runtime" \
        / "manifest.json"
    try:
        return str(json.loads(mf.read_text(encoding="utf-8"))
                   .get("fixture_id") or "") or None
    except (OSError, ValueError):
        return None


def sha256_path(path: Path, *, size_cap: int | None = None) -> str | None:
    """Streaming sha256 of a file (used for the 619M corpus store)."""
    try:
        if size_cap is not None and Path(path).stat().st_size > size_cap:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def collect_identity(*, qa_backend_dir: str | Path,
                     module_files: dict[str, str],
                     working_dir: str | Path | None = None,
                     service_role: str | None = None,
                     started_at: str | None = None,
                     include_corpus_store: bool = True,
                     loaded_profile: str | None = None,
                     loaded_citation_schema: str | None = None) -> dict:
    """Build the live identity payload for a RUNNING server process.

    module_files: relpath -> ABSOLUTE __file__ of each imported module.
    EVERY module in _IMPORTED_ANCHORS MUST be present — an absent anchor
    raises (fail closed): a disk-only fallback would silently downgrade
    the loaded-code proof. working_dir: server WORKING_DIR holding the
    source_snapshots store. loaded_profile / loaded_citation_schema carry
    the CONFIG the RUNNING code actually resolved (active_profile() and
    the imported CITATION_SCHEMA_VERSION), not environment/script text.
    Never includes secrets/gold/salt material.
    """
    qa_dir = Path(qa_backend_dir).resolve()
    tree_root = qa_dir.parent
    missing = [rel for rel in _IMPORTED_ANCHORS
               if rel not in module_files or not module_files[rel]]
    if missing:
        raise RuntimeError(
            "runtime identity: unimported critical modules (fail closed): "
            + ", ".join(sorted(missing)))
    anchors = {rel: module_files[rel] for rel in _IMPORTED_ANCHORS}
    code_digest, _ = compute_code_digest(tree_root, anchors)
    ident: dict = {
        "schema_version": SCHEMA_VERSION,
        "service_role": service_role or "UNDECLARED",
        "git_sha": git_sha_of(tree_root),
        "runtime_code_digest": code_digest,
        "critical_file_count": len(CRITICAL_FILES),
        "model": os.environ.get("ZAI_MODEL") or None,
        "profile": loaded_profile,
        "citation_schema_version": loaded_citation_schema,
        "corpus_manifest": fixture_manifest_id(tree_root),
        "corpus_store_sha256": None,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "started_at": started_at,
    }
    if include_corpus_store and working_dir is not None:
        store = Path(working_dir) / "source_snapshots"
        ident["corpus_store_sha256"] = sha256_path(store)
    return ident
