"""Tests for floor_for_branch.py, on small git repositories made for each case."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import floor_for_branch as ffb  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


FLOORS = """# Strict NAME=integer lines.
PYTEST_COLLECTED_FLOOR=20494
PYTEST_COLLECTED_MAX_SLACK=500
# a comment = with an equals sign
KLU_PASSED=120
"""

MIR = """name: mir
on:
  pull_request:
    paths:
      - "src/codegen*.cpp"
      - "python/tests/test_codegen*.py"
jobs:
  mir:
    steps:
      - run: |
          python -m pytest python/tests/test_codegen.py python/tests/test_codegen_outputs.py
"""

WHEELS = """name: wheels
on:
  pull_request:
    paths:
      - "src/**"
jobs:
  build:
    steps:
      - run: python -m build
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """`main` with a floors file, two workflows and one test file; checked out on a branch."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "test")
    write(
        r,
        {
            "ci/test_floors.env": FLOORS,
            ".github/workflows/mir.yml": MIR,
            ".github/workflows/wheels.yml": WHEELS,
            "python/tests/test_old.py": "def test_a():\n    pass\n",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
        },
    )
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    git(r, "checkout", "-q", "-b", "fix")
    monkeypatch.chdir(r)
    return r


def commit(repo: Path, files: dict[str, str]) -> None:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


def test_bounds_reads_name_value_lines_and_skips_comments(repo):
    assert ffb.bounds() == {
        "PYTEST_COLLECTED_FLOOR": 20494,
        "PYTEST_COLLECTED_MAX_SLACK": 500,
        "KLU_PASSED": 120,
    }


def test_added_tests_counts_a_new_file_and_a_grown_one(repo):
    commit(
        repo,
        {
            "python/tests/test_old.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
            "python/tests/test_new.py": "import pytest\n\n@pytest.mark.parametrize('x', [1, 2, 3])\ndef test_c(x):\n    pass\n",
        },
    )
    delta, files = ffb.added_tests("main")
    assert delta == 1 + 3  # one test added to an old file, three in a new one
    assert sorted(files) == ["python/tests/test_new.py", "python/tests/test_old.py"]


def test_added_tests_is_negative_where_the_branch_removes_tests(repo):
    commit(repo, {"python/tests/test_old.py": "x = 1\n"})
    delta, _ = ffb.added_tests("main")
    assert delta == -1


def test_added_tests_leaves_no_worktree_behind(repo):
    commit(repo, {"python/tests/test_new.py": "def test_c():\n    pass\n"})
    ffb.added_tests("main")
    assert git(repo, "worktree", "list").count("\n") == 0  # the main worktree only


def test_the_legs_report_says_which_workflow_runs_a_new_test(repo):
    commit(
        repo,
        {
            "python/tests/test_codegen_new.py": "def test_c():\n    pass\n",
            "src/codegen_x.cpp": "int x;\n",
        },
    )
    rows = ffb.legs_report("main")
    mir = next(r for r in rows if r.startswith("mir.yml"))
    wheels = next(r for r in rows if r.startswith("wheels.yml"))
    # mir fires on the C++ file, and its list does not name the new test file.
    assert "runs" in mir and "includes 0 of 1 new test files" in mir
    assert "not a pytest list" in wheels


def test_the_legs_report_counts_a_new_test_a_list_names(repo):
    commit(repo, {"python/tests/test_codegen_outputs.py": "def test_c():\n    pass\n"})
    mir = next(r for r in ffb.legs_report("main") if r.startswith("mir.yml"))
    assert "includes 1 of 1 new test files" in mir


def test_the_legs_report_with_no_filtered_workflow_firing(repo):
    commit(repo, {"docs/x.md": "words\n"})
    assert ffb.legs_report("main") == [
        "no filtered workflow runs on this branch (python-tests runs everything)"
    ]


def test_without_a_collected_count_floor_only_the_legs_are_reported(repo, capsys):
    # lanl/bngsim#1762 removed the collected-count floor; the legs report stays.
    commit(repo, {"ci/test_floors.env": "KLU_PASSED=120\n", "src/codegen_x.cpp": "int x;\n"})
    assert ffb.main(["--legs", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "no PYTEST_COLLECTED_FLOOR" in out
    assert "mir.yml" in out
