"""Helpers the test files share: a git call, a file writer, and the one recorded result."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prepr_check as pc  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def result(name: str) -> tuple[str, str]:
    """(status, detail) of the one recorded check whose name starts with *name*."""
    rows = [(s, d) for s, n, d, _ in pc.RESULTS if n.startswith(name)]
    assert len(rows) == 1, (name, pc.RESULTS)
    return rows[0]


# --- the branch ---------------------------------------------------------------------
