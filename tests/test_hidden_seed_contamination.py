"""Fail-closed regression: RT101 hidden seed/salt material must never land in
the tracked repo (incl. evidence docs).

Narrow by design (phase09 RT101 round, 2026-09-14):

1. Builder/selection salt literals of the canonical owner-side shape
       RT101-V<gen>-FRESH-YYYYMMDD-12hex
   (covers the V6 leak remediated this round and the same shape for any
   future generation, e.g. V7).
2. Seed-bearing key assignments (`builder_salt`, `selection_seed`,
   `hidden_candidate_seed`, `candidate_seed`, `selection_salt`, `salt`)
   whose value is a >=12 lowercase-hex token — the secret-token shape used
   owner-side.

Explicitly out of scope (must NOT be flagged):
- ordinary public SHA-256 digests (64-hex) not assigned to a seed key,
- `selection_seed_proof` / other per-case derived proof digests,
- schema field-name mentions without a secret-shaped value.

Fail closed: if the tracked-file enumeration cannot be obtained, the scan
errors out (never passes silently). Synthetic salt fixtures used by the
self-tests are constructed programmatically so this file never embeds a
salt-shaped literal.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# canonical owner-side builder/selection salt shape (any generation)
SALT_LITERAL_RE = re.compile(r"RT101-V\d+-FRESH-[0-9]{8}-[0-9a-f]{12}")

# seed-bearing keys assigned a >=12 lowercase-hex secret-shaped token.
# The value anchor requires ':'/'=' immediately after the key, so
# `selection_seed_proof` (followed by `_proof`) can never match.
SEED_ASSIGNMENT_RE = re.compile(
    r"\b(builder_salt|selection_seed|hidden_candidate_seed|candidate_seed"
    r"|selection_salt|salt)\b[\"']?\s*[:=]\s*[\"']?[0-9a-f]{12,}"
)


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def tracked_files() -> list[Path]:
    """Tracked files ∪ staged files; fail closed on any git error."""
    names = set(_git("ls-files").splitlines())
    names |= set(_git("diff", "--cached", "--name-only").splitlines())
    return [REPO_ROOT / n for n in sorted(names) if n]


def iter_text(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            yield from fh
    except (UnicodeDecodeError, OSError):
        return  # binary/unreadable: out of the narrow scope (matches runner gate)


def scan(lines) -> list[tuple[int, str, str]]:
    hits = []
    for no, line in enumerate(lines, start=1):
        for m in SALT_LITERAL_RE.finditer(line):
            hits.append((no, "salt_literal", m.group(0)))
        for m in SEED_ASSIGNMENT_RE.finditer(line):
            hits.append((no, "seed_assignment", m.group(0)))
    return hits


def scan_repo() -> list[tuple[str, int, str, str]]:
    findings = []
    for path in tracked_files():
        for no, kind, text in scan(iter_text(path)):
            findings.append((str(path.relative_to(REPO_ROOT)), no, kind, text))
    return findings


def fake_salt(gen: str = "7", date: str = "20260101", tail: str = "ab" * 6) -> str:
    """Synthetic salt built at runtime — no salt literal in tracked source."""
    return f"RT101-V{gen}-FRESH-{date}-{tail}"


class HiddenSeedContaminationTests(unittest.TestCase):
    def test_detector_catches_salt_literals(self):
        for candidate in (fake_salt(), fake_salt(gen="6"), fake_salt(gen="12")):
            self.assertTrue(scan([candidate]))

    def test_detector_ignores_benign_content(self):
        benign = [
            "plain public digest: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            'per-case derived proof: {"selection_seed_proof": '
            '"fadc383d4373cb0ab9b37343be00987cd1c911c9b8c8c9dc249e016d7728797"}',
            "the fresh owner-side selection seed/salt never enters this repo",
            'schema field {"selection_seed": {"type": "string"}}',
            "seed value was REDACTED",
            "dataset_snapshot_id sha256:950ff008aacdb7d11f749a67f69082b",
        ]
        self.assertEqual(scan(benign), [])

    def test_detector_catches_seed_key_assignments(self):
        self.assertTrue(scan(['"selection_seed": "0a024779f3bc"']))
        self.assertTrue(scan(["selection_seed = 0a024779f3bc"]))
        self.assertTrue(scan(['"salt": "0a024779f3bc"']))
        self.assertTrue(scan(["hidden_candidate_seed=abcdef0123456789"]))

    def test_tracked_repo_is_free_of_hidden_seed_material(self):
        findings = scan_repo()
        self.assertEqual(
            findings, [],
            "hidden seed/salt material leaked into tracked repo:\n"
            + "\n".join(f"  {f}:{no} [{kind}] {text}" for f, no, kind, text in findings),
        )

    def test_file_enumeration_fail_closed(self):
        self.assertGreater(len(tracked_files()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
