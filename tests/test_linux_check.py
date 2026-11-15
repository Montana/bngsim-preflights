"""Tests for linux_check.py: the parts that need no Docker, and a dry run."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import linux_check as lc  # noqa: E402

PYTEST_TAIL = """\
FAILED python/tests/test_build_provenance.py::test_build_commit_stamp_present - AssertionError
FAILED python/tests/test_new.py::test_a - assert 1 == 2
FAILED python/tests/test_new.py::test_a - assert 1 == 2
= 2 failed, 19846 passed, 619 skipped, 1 xfailed in 1844.88s (0:30:44) =
"""


def test_the_script_runs_the_named_tests_quoted():
    script = lc.container_script(["python/tests/test_a.py", "python/tests/test_b.py::test_x y"], 3)
    assert "uv sync --frozen --extra test" in script
    assert "-n 3 -rf python/tests/test_a.py 'python/tests/test_b.py::test_x y'" in script


def test_the_script_runs_the_whole_suite_by_default():
    assert "-rf python/tests 2>&1" in lc.container_script([], 3)


def test_parse_reads_the_summary_and_each_failure_once():
    run = lc.parse(PYTEST_TAIL)
    assert run.summary.startswith("2 failed, 19846 passed")
    assert run.failed == [
        "python/tests/test_build_provenance.py::test_build_commit_stamp_present",
        "python/tests/test_new.py::test_a",
    ]


def test_parse_of_a_run_with_no_summary():
    assert lc.parse("Traceback (most recent call last):\n").summary == ""


def test_a_failure_main_shares_is_the_images():
    here = ["t/test_git.py::test_a", "t/test_new.py::test_b"]
    image, change = lc.classify(here, on_main=["t/test_git.py::test_a"])
    assert image == ["t/test_git.py::test_a"]
    assert change == ["t/test_new.py::test_b"]


def test_failed_files_are_the_files_of_the_failed_tests():
    assert lc.failed_files(["a.py::t1", "a.py::t2", "b.py::t3"]) == ["a.py", "b.py"]


def test_a_dry_run_prints_the_commands_and_runs_nothing(tmp_path, capsys):
    assert lc.main(["--tree", str(tmp_path), "--out", str(tmp_path / "out"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "+ docker run --rm --platform linux/amd64" in out  # the probe and the run
    assert "--exclude .venv" in out and "--exclude .git" in out
    assert not (tmp_path / "out").exists()


def test_no_container_without_restart_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lc, "container_starts", lambda *a: False)
    assert lc.main(["--tree", str(tmp_path), "--out", str(tmp_path / "out")]) == 2
    assert "--restart-docker" in capsys.readouterr().out
