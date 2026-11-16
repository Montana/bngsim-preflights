"""The `make_branch` fixture: a small git repository with `main` and a branch on it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import git, pc, write  # noqa: E402


@pytest.fixture
def make_branch(tmp_path, monkeypatch):
    """A repo with `main` holding *base* and a branch adding *head* on top."""
    pc.RESULTS.clear()
    pc.STATE.clear()

    def make(base: dict[str, str], head: dict[str, str], *, message: str = "change", commit: bool = True):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "test")
        write(repo, {"README.md": "x\n", **base})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", "fix")
        write(repo, head)
        if commit:
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", message)
        return pc.Branch(repo, "main")

    return make
