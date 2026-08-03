#!/usr/bin/env python3
"""Run the same lightweight project checks locally and in GitHub Actions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_TRACKED_PREFIXES = (
    "runtime/",
    "bge-m3-model/",
)
FORBIDDEN_TRACKED_INDEXES = {
    "data/lightrag/vector_index_v2.pkl.gz",
    "data/lightrag/bm25_index.pkl.gz",
    "data/lightrag/graph-export.json",
    "data/lightrag/graph-export-backup-old.json",
    "data/lightrag/jieba_custom_dict.txt",
}
SECRET_PATTERNS = (
    re.compile(r"ZAI_API_KEY\s*=\s*(?!replace-|\$\{|os\.environ)[^\s#]{16,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[\"']?"
        r"(?!replace-|example-|\$\{|os\.environ)[A-Za-z0-9._-]{20,}"
    ),
)


def run(command: list[str], env: dict | None = None) -> None:
    print("+", " ".join(command))
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    return [line for line in output.splitlines() if line]


def validate_repository_policy(files: list[str]) -> None:
    forbidden = [
        path for path in files
        if path in FORBIDDEN_TRACKED_INDEXES or path.startswith(FORBIDDEN_TRACKED_PREFIXES)
    ]
    if forbidden:
        raise RuntimeError("Runtime/model/index files must not be tracked:\n  " + "\n  ".join(forbidden))

    secret_hits: list[str] = []
    for relative in files:
        if relative.startswith("data/"):
            # Generated intelligence content can legitimately quote token-like strings.
            continue
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            secret_hits.append(relative)
    if secret_hits:
        raise RuntimeError("Possible committed secret found in: " + ", ".join(secret_hits))


def validate_json(files: list[str]) -> None:
    for relative in files:
        if not relative.endswith(".json"):
            continue
        path = ROOT / relative
        if path.is_file():
            json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    try:
        files = tracked_files()
        print("[1/6] Repository policy")
        validate_repository_policy(files)
        print("[2/6] JSON syntax")
        validate_json(files)
        print("[3/6] Python syntax")
        run([sys.executable, "-m", "compileall", "-q", "qa-backend", "scripts", "tests"])
        print("[4/6] Unit tests")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "qa-backend"), env.get("PYTHONPATH", "")]
        )
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], env=env)
        print("[5/6] Git whitespace check")
        run(["git", "diff", "--check"])
        print("[6/6] JavaScript syntax")
        if shutil.which("node"):
            run(["node", "--check", "qa.js"])
        else:
            print("  node is unavailable; skipped JavaScript syntax check")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("All Tech-DB checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
