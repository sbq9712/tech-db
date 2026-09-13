"""RT-101 contamination guards — production ingest/index denylist.

Phase09 remediation §14: no holdout candidate material (rt101-v4/v5/v6),
no owner-secret workspace, no candidate gold digest path, and no builder
temporary workspace may ever enter the production ingest/index root.
Every escape class fails CLOSED:

  * glob escape (pattern escapes the allowlist root)
  * symlink escape (resolved target outside allowlist root)
  * relative path escape (.. traversal beyond the root)
  * env override (denylist roots re-injected via environment are still
    denied — the denylist is code-owned, not configuration-owned)
  * accidental recursive repository scan (scanning a git repository
    root requires an explicit allowlist adapter; recursive dumps of a
    repo are rejected)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

# Code-owned denylist.  NOT configurable via environment by design: an
# env override must never be able to un-forbid a forbidden root.
FORBIDDEN_SUBSTRINGS = (
    "rt101-v4",
    "rt101-v5",
    "rt101-v6",
    "rt101_v4",
    "rt101_v5",
    "rt101_v6",
    "tech-db-owner-secrets",
    "owner-secrets",
    "blind-input",
    "blind_package",
    "holdout-gold",
    "gold.json",
    "expected-answers",
    "hidden-rubric",
)

FORBIDDEN_DIR_NAMES = {
    ".git",
    "__pycache__",
}

# File types that are never production evidence when found in a raw
# tree scan (tooling, secrets, build artifacts).
FORBIDDEN_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dll",
    ".pem", ".key", ".p12", ".pfx",
    ".env", ".envrc",
)


class IngestGuardError(RuntimeError):
    """Raised when ingest material fails a contamination guard."""


def _normalized_parts(path: Path) -> tuple[str, ...]:
    return tuple(p for p in path.parts if p not in ("", "."))


def assert_ingestable_path(
    path: Path | str,
    *,
    allowlist_roots: Iterable[Path | str] = (),
) -> Path:
    """Fail-closed check for ONE candidate ingest path.

    Returns the fully-resolved path when safe; raises IngestGuardError
    on any denylist hit, escape, forbidden suffix/directory, or
    allowlist violation.
    """
    raw = Path(str(path))
    lexically_absolute = raw.is_absolute()
    resolved = Path(os.path.realpath(str(raw)))
    s_resolved = str(resolved)
    s_raw = str(raw)

    # 1. code-owned denylist (checked on BOTH raw and resolved forms so
    #    symlink/rename tricks cannot hide a forbidden component)
    for marker in FORBIDDEN_SUBSTRINGS:
        low_raw, low_res = s_raw.lower(), s_resolved.lower()
        if marker in low_raw or marker in low_res:
            raise IngestGuardError(
                f"contamination denylist hit ({marker!r}): {s_resolved}")

    # 2. forbidden suffix / directory components
    for part in _normalized_parts(resolved):
        if part in FORBIDDEN_DIR_NAMES:
            raise IngestGuardError(
                f"forbidden directory component ({part!r}): {s_resolved}")
    lowered_name = resolved.name.lower()
    if (resolved.suffix.lower() in FORBIDDEN_SUFFIXES
            or lowered_name in FORBIDDEN_SUFFIXES):
        raise IngestGuardError(
            f"forbidden file type ({resolved.name!r}): {s_resolved}")

    # 3. allowlist containment (symlink + relative escape class)
    roots = [Path(os.path.realpath(str(r))) for r in allowlist_roots]
    if roots:
        inside = any(
            resolved == root or root in resolved.parents
            for root in roots)
        if not inside:
            raise IngestGuardError(
                f"path escapes allowlist roots: {s_resolved} "
                f"not under {[str(r) for r in roots]}")

    # 4. relative-path escape: a relative ingest path that traverses
    #    above its declared base is rejected (callers that legitimately
    #    pass relative paths resolve them against their own root first
    #    and pass absolute paths here)
    if not lexically_absolute and ".." in raw.parts:
        raise IngestGuardError(
            f"relative path traversal rejected: {s_raw}")
    return resolved


def assert_ingestable_tree(
    root: Path | str,
    *,
    allowlist_roots: Iterable[Path | str] = (),
    allow_git_repository: bool = False,
    max_files: int = 200_000,
) -> list[Path]:
    """Fail-closed check for a recursive ingest scan root.

    Returns the list of ingestable files when the whole tree is safe.
    A git repository root (presence of ``.git``) is REJECTED unless
    ``allow_git_repository`` is explicitly True (an explicit allowlist
    adapter decision — never inferred, never env-configurable).
    """
    root = assert_ingestable_path(root, allowlist_roots=allowlist_roots)
    if not root.is_dir():
        raise IngestGuardError(f"ingest root is not a directory: {root}")
    if not allow_git_repository and (root / ".git").exists():
        raise IngestGuardError(
            "accidental recursive repository scan rejected: "
            f"{root} contains .git; use an explicit source adapter")
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in FORBIDDEN_DIR_NAMES]
        for name in filenames:
            p = Path(dirpath) / name
            out.append(assert_ingestable_path(p, allowlist_roots=allowlist_roots))
            if len(out) > max_files:
                raise IngestGuardError(
                    f"ingest tree exceeds max_files={max_files}")
    return out


def assert_env_ingest_config_safe(env: Mapping[str, str] | None = None) -> None:
    """Fail-closed check of environment-provided ingest configuration.

    Denylist roots re-injected via environment stay denied: this checks
    that no env value smuggles a forbidden root into an ingest allowlist.
    Raises IngestGuardError when an env-configured allowlist entry points
    at (or contains) a denylisted root.
    """
    env = dict(os.environ if env is None else env)
    for name, value in sorted(env.items()):
        if not any(k in name.upper() for k in
                   ("INGEST", "SOURCES", "CORPUS", "INDEX_ROOT")):
            continue
        if not value:
            continue
        probe = Path(value)
        try:
            assert_ingestable_path(probe)
        except IngestGuardError as exc:
            raise IngestGuardError(
                f"env ingest config {name!r} rejected: {exc}") from None
