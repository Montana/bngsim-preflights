"""Tests for mutate.py, on a small project with one module and one test file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mutate  # noqa: E402

CALC = "def add(a, b):\n    # the sum\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_calc.py"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "test_calc.py").write_text(TEST)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_mutants(project: Path, mutants: list[dict], **top) -> Path:
    path = project / "mutants.json"
    path.write_text(json.dumps({"test": PYTEST, **top, "mutants": mutants}))
    return path


def log(project: Path) -> str:
    return (project / "mutants.log").read_text()


def test_a_mutant_a_test_catches_is_killed(project):
    m = write_mutants(project, [{"name": "M1 subtracts", "file": "calc.py", "find": "a + b", "replace": "a - b"}])
    assert mutate.main([str(m)]) == 0
    assert "M1 subtracts: KILLED" in log(project)
    assert "test_calc.py::test_add" in log(project)


def test_a_mutant_no_test_catches_survives(project):
    m = write_mutants(
        project, [{"name": "M1 the comment", "file": "calc.py", "find": "# the sum", "replace": "# a sum"}]
    )
    assert mutate.main([str(m)]) == 1
    assert "M1 the comment: SURVIVED" in log(project)


def test_every_file_is_restored_byte_for_byte(project):
    m = write_mutants(
        project,
        [
            {"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a * b"},
            {"name": "M2", "file": "calc.py", "find": "# the sum", "replace": ""},
        ],
    )
    mutate.main([str(m)])
    assert (project / "calc.py").read_text() == CALC
    assert not (project / ".mutate-backup").exists()


def test_each_mutant_runs_on_the_original_not_on_the_one_before(project):
    # M2 alone survives; run on M1's subtraction still in place, it would be "killed".
    m = write_mutants(
        project,
        [
            {"name": "M1 subtracts", "file": "calc.py", "find": "a + b", "replace": "a - b"},
            {"name": "M2 the comment", "file": "calc.py", "find": "# the sum", "replace": "# a sum"},
        ],
    )
    mutate.main([str(m)])
    assert "M2 the comment: SURVIVED" in log(project)


def test_a_find_that_is_not_there_is_not_applied(project):
    m = write_mutants(project, [{"name": "M1 moved", "file": "calc.py", "find": "a - b", "replace": "a + b"}])
    assert mutate.main([str(m)]) == 1
    assert "M1 moved: NOT APPLIED" in log(project) and "occurs 0 times" in log(project)


def test_a_find_that_occurs_more_often_than_said_is_not_applied(project):
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a", "replace": "b"}])
    assert mutate.main([str(m)]) == 1
    assert "NOT APPLIED" in log(project)


def test_count_replaces_every_occurrence(project):
    (project / "calc.py").write_text("def add(a, b):\n    return a + b + 0 + 0\n")
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "+ 0", "replace": "+ 1", "count": 2}])
    assert mutate.main([str(m)]) == 0  # 2 + 3 + 1 + 1 != 5


def test_a_failing_build_is_reported_and_the_file_restored(project):
    m = write_mutants(
        project,
        [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b",
          "build": [sys.executable, "-c", "raise SystemExit(3)"]}],
    )  # fmt: skip
    assert mutate.main([str(m)]) == 1
    assert "M1: BUILD FAILED" in log(project)
    assert (project / "calc.py").read_text() == CALC


def test_only_runs_the_named_mutants(project):
    m = write_mutants(
        project,
        [
            {"name": "M1 subtracts", "file": "calc.py", "find": "a + b", "replace": "a - b"},
            {"name": "M2 the comment", "file": "calc.py", "find": "# the sum", "replace": ""},
        ],
    )
    assert mutate.main([str(m), "--only", "M1"]) == 0
    assert "M2" not in log(project)


def test_restore_undoes_a_run_that_was_killed_outright(project):
    # As a kill -9 would leave it: the backup written, the file still mutated.
    backup = project / ".mutate-backup"
    backup.mkdir()
    (backup / "calc.py").write_text(CALC)
    (project / "calc.py").write_text(CALC.replace("a + b", "a - b"))
    assert mutate.main(["--restore"]) == 0
    assert (project / "calc.py").read_text() == CALC
    assert not backup.exists()


def test_a_run_refuses_to_start_over_an_unfinished_one(project):
    (project / ".mutate-backup").mkdir()
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b"}])
    with pytest.raises(SystemExit, match="did not finish"):
        mutate.main([str(m)])


def test_a_mutants_file_without_a_test_is_refused(project):
    path = project / "mutants.json"
    path.write_text(json.dumps({"mutants": [{"file": "calc.py", "find": "a", "replace": "b"}]}))
    with pytest.raises(SystemExit, match="no 'test' command"):
        mutate.main([str(path)])
