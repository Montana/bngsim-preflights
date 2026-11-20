"""--merged: the test files where the branch and main meet, run merged with main
in bngsim-docker's container. The CLI is stood in for by a script that records
its arguments and writes the report a run would."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import git, pc, result, write

BASE = {"python/tests/test_census.py": "COUNT = 29\n", "python/tests/test_other.py": "x\n", "src/a.cpp": "a\n"}
HEAD = {"src/a.cpp": "a; throw\n", "python/tests/test_new.py": "y\n"}


@pytest.fixture
def b_1753(make_branch):
    """#1753's shape: the branch changes code and adds a test; main, since the
    merge base, re-pinned a count in a test file the branch never touched."""
    b = make_branch(BASE, HEAD)
    git(b.repo, "checkout", "-q", "main")
    write(b.repo, {"python/tests/test_census.py": "COUNT = 30\n", "python/tests/test_moved_on_main.py": "z\n"})
    git(b.repo, "add", "-A")
    git(b.repo, "commit", "-q", "-m", "main moves")
    git(b.repo, "checkout", "-q", "fix")
    return pc.Branch(b.repo, "main")


def test_the_files_where_the_two_sides_meet(b_1753):
    files = pc.merged_test_files(b_1753)
    assert files == ["python/tests/test_new.py", "python/tests/test_census.py", "python/tests/test_moved_on_main.py"]
    assert "python/tests/test_other.py" not in files  # neither side touched it


def fake_cli(tmp_path: Path, *, rc: int, log: str = "", stdout: str = "") -> Path:
    cli = tmp_path / "bngsim_docker.py"
    cli.write_text(f"""import json, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(args))
out = Path(args[args.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "docker.log").write_text({log!r})
print({stdout!r})
sys.exit({rc})
""")
    return cli


def test_a_green_merged_run_passes(b_1753, tmp_path):
    cli = fake_cli(tmp_path, rc=0, log="image x\nsummary: 9 passed\nno failure but the standing ones\n",
                   stdout="testing 1234567 merged with origin's main abcdef0")
    pc.check_merged_run(b_1753, cli, "arm64")
    status, detail = result("tests pass merged with main")
    assert status == "PASS" and "3 test file(s)" in detail and "merged with origin's main abcdef0" in detail
    args = json.loads((tmp_path / "argv.json").read_text())
    assert args[:2] == ["test", "--tree"] and "--merge-main" in args and "--uncommitted" not in args
    assert args[args.index("--arch") + 1] == "arm64"
    assert args[args.index("--tests") + 1:] == pc.merged_test_files(b_1753)
    out = Path(args[args.index("--out") + 1])
    assert not out.is_relative_to(b_1753.repo) or ".git" in out.parts  # logs never land in the worktree


def test_a_failure_that_is_the_tree_s_fails_the_gate(b_1753, tmp_path):
    cli = fake_cli(tmp_path, rc=1, log="summary: 1 failed, 8 passed\n"
                   "  NEW: python/tests/test_census.py::test_count\nfailures that are the tree's\n")
    pc.check_merged_run(b_1753, cli, "amd64")
    status, detail = result("tests pass merged with main")
    assert status == "FAIL" and "1 failure(s) the tree's: python/tests/test_census.py::test_count" in detail


def test_a_merge_that_conflicts_fails_the_gate(b_1753, tmp_path):
    cli = fake_cli(tmp_path, rc=1, stdout="HEAD does not merge with origin's main: python/tests/test_census.py")
    pc.check_merged_run(b_1753, cli, "amd64")
    status, detail = result("tests pass merged with main")
    assert status == "FAIL" and "does not merge" in detail and "test_census.py" in detail


def test_a_run_that_does_not_finish_fails_the_gate(b_1753, tmp_path):
    cli = fake_cli(tmp_path, rc=1, stdout="no container of img starts: run doctor")
    pc.check_merged_run(b_1753, cli, "amd64")
    status, detail = result("tests pass merged with main")
    assert status == "FAIL" and "did not finish" in detail and "no container" in detail


def test_no_cli_is_a_skip(b_1753, tmp_path):
    pc.check_merged_run(b_1753, tmp_path / "missing.py", "amd64")
    assert result("tests pass merged with main")[0] == "SKIP"


def test_no_test_file_on_either_side_is_a_skip(make_branch, tmp_path):
    b = make_branch({"src/a.cpp": "a\n"}, {"src/a.cpp": "b\n"})
    pc.check_merged_run(b, fake_cli(tmp_path, rc=0), "amd64")
    status, detail = result("tests pass merged with main")
    assert status == "SKIP" and "either side" in detail


def test_the_cli_defaults_to_the_clone_beside_this_one(monkeypatch):
    monkeypatch.delenv("BNGSIM_DOCKER_CLI", raising=False)
    assert pc.default_docker_cli() == Path(pc.__file__).resolve().parent.parent / "bngsim-docker" / "bngsim_docker.py"
    monkeypatch.setenv("BNGSIM_DOCKER_CLI", "/x/cli.py")
    assert pc.default_docker_cli() == Path("/x/cli.py")
