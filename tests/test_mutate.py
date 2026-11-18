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


# --- a mutant that tests nothing, and a run that is cut short -------------------------


def test_a_mutant_that_replaces_text_with_itself_is_not_applied(project):
    m = write_mutants(project, [{"name": "M1 same", "file": "calc.py", "find": "a + b", "replace": "a + b"},
                                {"name": "M2 subtracts", "file": "calc.py", "find": "a + b", "replace": "a - b"}])
    assert mutate.main([str(m)]) == 1
    assert "M1 same: NOT APPLIED" in log(project)
    assert log(project).splitlines()[-1] == "restored; 1 of 2 killed; 1 tested nothing (M1 same)"


def test_count_all_replaces_however_many_there_are(project):
    (project / "calc.py").write_text("def add(a, b):\n    c = a + b\n    return c if c else a + b\n")
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b", "count": "all"},
                                {"name": "M2", "file": "calc.py", "find": "a * b", "replace": "a", "count": "all"}])
    assert mutate.main([str(m)]) == 1
    assert "M1: KILLED" in log(project) and "M2: NOT APPLIED" in log(project)


def test_a_count_that_is_no_count_is_refused(project):
    m = write_mutants(project, [{"file": "calc.py", "find": "a + b", "replace": "a - b", "count": 0}])
    with pytest.raises(SystemExit, match="count"):
        mutate.main([str(m)])


def test_env_reaches_the_test(project):
    (project / "test_calc.py").write_text(
        "import os\nfrom calc import add\n\n\ndef test_add():\n    if os.environ.get('WHERE') == 'here':\n"
        "        assert add(2, 3) == 5\n")
    mutants = [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b"}]
    # The test looks at add only where WHERE is set: without it the mutant survives.
    assert mutate.main([str(write_mutants(project, mutants))]) == 1
    assert mutate.main([str(write_mutants(project, mutants, env={"WHERE": "here"}))]) == 0
    assert "M1: KILLED" in log(project)
    mutants[0]["env"] = {"WHERE": "elsewhere"}  # a mutant's own env wins
    assert mutate.main([str(write_mutants(project, mutants, env={"WHERE": "here"}))]) == 1


def test_a_mutant_that_hangs_is_killed_and_the_file_restored(project):
    m = write_mutants(project, [{"name": "M1 hangs", "file": "calc.py", "find": "    return a + b",
                                 "replace": "    while True:\n        pass"}])
    assert mutate.main([str(m), "--timeout", "3"]) == 0
    assert "M1 hangs: KILLED [timed out after 3 s]" in log(project)
    assert (project / "calc.py").read_text() == CALC


def test_a_build_that_runs_out_of_time_is_a_failed_build(project):
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b",
                                 "build": [sys.executable, "-c", "import time; time.sleep(60)"]}])
    assert mutate.main([str(m), "--timeout", "2"]) == 1
    assert "M1: BUILD FAILED [timed out after 2 s]" in log(project)
    assert (project / "calc.py").read_text() == CALC


def _git(project: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=project, check=True, capture_output=True, timeout=60)


def _repo(project: Path) -> None:
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "config", "user.name", "test")
    _git(project, "add", "calc.py", "test_calc.py")
    _git(project, "commit", "-q", "-m", "base")


def test_a_run_will_not_start_on_uncommitted_changes(project):
    _repo(project)
    (project / "calc.py").write_text(CALC + "\n# not committed\n")
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b"}])
    with pytest.raises(SystemExit, match="uncommitted changes in calc.py"):
        mutate.main([str(m)])
    assert not (project / ".mutate-backup").exists()
    assert mutate.main([str(m), "--allow-dirty"]) == 0
    _git(project, "commit", "-q", "-am", "now committed")
    assert mutate.main([str(m)]) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_run_ended_from_outside_puts_the_file_back_and_says_so(project):
    import signal
    import subprocess
    import time

    (project / "test_calc.py").write_text(
        "import time\nfrom calc import add\n\n\ndef test_add():\n    time.sleep(120)\n    assert add(2, 3) == 5\n")
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b"},
                                {"name": "M2", "file": "calc.py", "find": "a + b", "replace": "a * b"}])
    proc = subprocess.Popen([sys.executable, str(ROOT / "mutate.py"), str(m)], cwd=project,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 60
        while "a - b" not in (project / "calc.py").read_text() and time.time() < deadline:
            time.sleep(0.05)
        assert "a - b" in (project / "calc.py").read_text()  # the mutant is in the file
        time.sleep(1.0)  # and its test is running
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=60)
    finally:
        proc.kill()
    assert proc.returncode == 130
    assert (project / "calc.py").read_text() == CALC
    assert not (project / ".mutate-backup").exists()
    assert "INTERRUPTED (SIGTERM) after 0 of 2" in out and "every file put back" in out
    last = log(project).splitlines()[-1]
    assert last.startswith("INTERRUPTED") and "restored" not in log(project)


def test_a_cache_of_one_mutant_is_not_run_for_the_next_or_left_behind(project):
    # Mutants the size of the original, and a test that takes a fraction of a
    # second: each is written, compiled to a .pyc and put back inside one second.
    # Python accepts a .pyc whose recorded second and size match the source, so
    # without care the cache of M3 is what M4 is judged on, and what is left.
    import subprocess

    mutants = [{"name": f"M{i}", "file": "calc.py", "find": "a + b", "replace": r}
               for i, r in enumerate(("a - b", "a * b", "a % b"), 1)]
    mutants.append({"name": "M4 the same sum", "file": "calc.py", "find": "a + b", "replace": "b + a"})
    for _ in range(2):
        assert mutate.main([str(write_mutants(project, mutants))]) == 1
        lines = log(project)
        assert all(f"M{i}: KILLED" in lines for i in (1, 2, 3))
        assert "M4 the same sum: SURVIVED" in lines  # judged on its own code
        assert (project / "calc.py").read_text() == CALC
        after = subprocess.run(PYTEST, cwd=project, capture_output=True, text=True, timeout=120)
        assert after.returncode == 0, after.stdout[-400:]  # and the original is what runs


def test_line_endings_come_back_as_they_were_after_each_mutant(project):
    crlf = CALC.replace("\n", "\r\n").encode()
    (project / "calc.py").write_bytes(crlf)
    # The second mutant's text spans a line ending: it applies only if the first
    # was put back with the file's own.
    m = write_mutants(project, [{"name": "M1", "file": "calc.py", "find": "a + b", "replace": "a - b"},
                                {"name": "M2", "file": "calc.py", "find": "a + b\r\n", "replace": "a * b\r\n"}])
    assert mutate.main([str(m)]) == 0
    assert "M1: KILLED" in log(project) and "M2: KILLED" in log(project)
    assert (project / "calc.py").read_bytes() == crlf


def test_restore_puts_a_file_back_in_a_later_second_than_the_mutant(project):
    # The recovery path, after a run that was killed: the file holds a mutant of
    # the original's size, written this second. Put back with the backup's old
    # time, or in the same second, a cache compiled from the mutant would match.
    import os
    import time

    backup = project / ".mutate-backup"
    backup.mkdir()
    (backup / "calc.py").write_text(CALC)
    mutate._WRITTEN.clear()
    (project / "calc.py").write_text(CALC.replace("a + b", "a - b"))
    mutant_second = int(os.stat(project / "calc.py").st_mtime)
    old = time.time() - 3600
    os.utime(backup / "calc.py", (old, old))
    assert mutate.restore() == [Path("calc.py")]
    assert (project / "calc.py").read_text() == CALC
    assert int(os.stat(project / "calc.py").st_mtime) > mutant_second


def test_a_mutant_with_no_build_does_not_run_what_the_last_mutant_built(project):
    # The "build" copies calc.py to built.py, and the test reads built.py, as a
    # compiled extension is what its tests run. M2 changes a comment and has no
    # build: left with M1's build, it would be judged on M1's bug.
    import subprocess

    (project / "test_calc.py").write_text(
        "from built import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    build = [sys.executable, "-c", "import shutil; shutil.copy('calc.py', 'built.py')"]
    subprocess.run(build, cwd=project, check=True, timeout=60)
    m = write_mutants(project, [
        {"name": "M1 subtracts", "file": "calc.py", "find": "a + b", "replace": "a - b", "build": build},
        {"name": "M2 a comment", "file": "calc.py", "find": "# the sum", "replace": "# the total"},
    ])
    assert mutate.main([str(m)]) == 1
    assert "M1 subtracts: KILLED" in log(project)
    assert "M2 a comment: SURVIVED" in log(project)
    assert (project / "built.py").read_text() == CALC
