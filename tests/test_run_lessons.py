"""Tests for the checks that came from #1815, #1821 and the NFsim patches (2026-11-17)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import git, result, write  # noqa: E402

import prepr_check as pc  # noqa: E402

# --- check_stacked ------------------------------------------------------------------


def _open_prs(monkeypatch, prs: list[dict]):
    real = pc.sh

    def fake(args, cwd, check=False, timeout=600):
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
        return real(args, cwd, check, timeout)

    monkeypatch.setattr(pc, "sh", fake)


def _stacked(make_branch):
    """`fix` on main, and `next` on top of it: (branch read from `next`, fix's head)."""
    b = make_branch({}, {"a.py": "x = 1\n"})
    under = git(b.repo, "rev-parse", "HEAD")
    git(b.repo, "update-ref", "refs/remotes/origin/main", "main")
    git(b.repo, "checkout", "-q", "-b", "next")
    write(b.repo, {"b.py": "y = 2\n"})
    git(b.repo, "add", "b.py")
    git(b.repo, "commit", "-q", "-m", "on top")
    return pc.Branch(b.repo, "main"), under


def test_a_branch_on_main_alone_is_not_stacked(make_branch, monkeypatch):
    b = make_branch({}, {"a.py": "x = 1\n"})
    git(b.repo, "update-ref", "refs/remotes/origin/main", "main")
    _open_prs(monkeypatch, [{"number": 7, "headRefOid": "0" * 40, "headRefName": "other"},
                            {"number": 8, "headRefOid": git(b.repo, "rev-parse", "HEAD"), "headRefName": "fix"}])
    pc.check_stacked(b, "Fixes #1.")
    assert result("stacked on")[0] == "PASS" and pc.STATE["stacked_on"] == []


def test_a_branch_holding_an_open_prs_commits_must_name_it(make_branch, monkeypatch):
    b, under = _stacked(make_branch)
    _open_prs(monkeypatch, [{"number": 1809, "headRefOid": under, "headRefName": "fix"}])
    pc.check_stacked(b, "Fixes #1815.")
    status, detail = result("stacked on")
    assert status == "FAIL" and "#1809" in detail and "does not name" in detail
    assert pc.STATE["stacked_on"] == [(1809, under)]


def test_a_stacked_branch_whose_body_names_the_pr_is_a_warning(make_branch, monkeypatch):
    b, under = _stacked(make_branch)
    _open_prs(monkeypatch, [{"number": 1809, "headRefOid": under, "headRefName": "fix"}])
    pc.check_stacked(b, "Stacked on #1809; merge that first.")
    status, detail = result("stacked on")
    assert status == "WARN" and f"--base {under[:8]}" in detail


def test_a_stacked_branch_read_against_the_pr_underneath_is_not_told_to_rebase(make_branch, monkeypatch):
    b, under = _stacked(make_branch)
    b = pc.Branch(b.repo, under)
    assert b.files == ["b.py"]  # its own diff
    _open_prs(monkeypatch, [{"number": 1809, "headRefOid": under, "headRefName": "fix"}])
    pc.check_stacked(b, "Stacked on #1809.")
    status, detail = result("stacked on")
    assert status == "WARN" and "--base" not in detail


def test_a_merged_pr_is_not_something_to_be_stacked_on(make_branch, monkeypatch):
    b, under = _stacked(make_branch)
    git(b.repo, "update-ref", "refs/remotes/origin/main", under)  # it merged
    _open_prs(monkeypatch, [{"number": 1809, "headRefOid": under, "headRefName": "fix"}])
    pc.check_stacked(pc.Branch(b.repo, "origin/main"), None)
    assert result("stacked on")[0] == "PASS"


# --- check_private_paths --------------------------------------------------------------


def test_a_home_directory_in_an_added_line_fails(make_branch):
    b = make_branch({}, {"third_party/nfsim/VENDOR.json": '{"local_checkout": "/Users/ada/.tool/jobs/1/tmp/nfsim"}\n'})
    pc.check_private_paths(b, None)
    status, detail = result("no private paths")
    assert status == "FAIL" and "VENDOR.json: /Users/ada" in detail


def test_a_linux_or_windows_home_fails_too(make_branch):
    b = make_branch({}, {"a.py": 'P = "/home/ada/work"\n', "b.py": 'Q = r"C:\\Users\\ada\\work"\n'})
    pc.check_private_paths(b, None)
    status, detail = result("no private paths")
    assert status == "FAIL" and "a.py" in detail and "b.py" in detail


def test_this_machines_own_home_fails_under_any_root(make_branch, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/volumes/data/grace")))
    b = make_branch({}, {"a.py": 'P = "/volumes/data/grace/checkout"\n'})
    pc.check_private_paths(b, None)
    assert result("no private paths")[0] == "FAIL"


def _home(monkeypatch, tmp_path, *dirs: str):
    home = tmp_path / "home" / "grace"
    for d in dirs:
        (home / d).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_a_tools_working_directory_fails(make_branch, monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path, ".sometool", ".cache")
    b = make_branch({}, {"notes.md": "see ~/.sometool/jobs/1/tmp/out.log\n", "list.txt": ".sometool/worktrees/x\n"})
    pc.check_private_paths(b, None)
    status, detail = result("no private paths")
    assert status == "FAIL" and "notes.md: .sometool/" in detail and "list.txt: .sometool/" in detail


def test_a_dot_directory_every_machine_has_passes(make_branch, monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path, ".sometool", ".cache", ".config", ".ruff_cache")
    b = make_branch({}, {"docs.md": "the cache is in ~/.cache/bngsim, settings in ~/.config/bngsim\n",
                         ".gitignore": "/.ruff_cache/\n.othertool/\n"})
    pc.check_private_paths(b, None)
    assert result("no private paths")[0] == "PASS"


def test_a_home_directory_in_the_body_fails(make_branch):
    b = make_branch({}, {"a.py": "x = 1\n"})
    pc.check_private_paths(b, "Logs are in /Users/ada/results.\n")
    status, detail = result("no private paths")
    assert status == "FAIL" and "the PR body" in detail


def test_placeholders_and_lines_already_there_pass(make_branch):
    b = make_branch({"old.json": '{"p": "/Users/ada/x"}\n'},
                    {"docs.md": "cache: /home/you/.cache/bngsim\nCI: /home/runner/work\n/tmp/nfsim-vendor-candidate\n",
                     "ci.py": "# a ``.tool`` directory is skipped\n"})
    pc.check_private_paths(b, "Checked in ~/work/ab-1.\n")
    assert result("no private paths")[0] == "PASS"


# --- check_vendored -------------------------------------------------------------------

TRIPWIRE = "import sys\nprint('tree matches the queue')\nsys.exit(%d)\n"


def _vendor(checkout: str, commits: int, patches: int, exit_code: int = 0) -> dict[str, str]:
    files = {"third_party/nfsim/VENDOR.json": json.dumps(
        {"source": {"local_checkout": checkout}, "carry_queue": [{"topic": "t", "commits": ["c"] * commits}]}),
        "third_party/nfsim/src/system.cpp": "int x;\n",
        "scripts/vendor_nfsim.py": TRIPWIRE % exit_code}
    for i in range(patches):
        files[f"scripts/nfsim_vendor_patches/{i + 1:04d}-x.patch"] = "patch\n"
    return files


def _candidate(monkeypatch, tmp_path, present: bool = True):
    c = tmp_path / "candidate"
    if present:
        c.mkdir()
    monkeypatch.setattr(pc, "_VENDOR_CHECKOUTS", (str(c),))
    return str(c)


def test_no_vendored_change_is_skipped(make_branch):
    b = make_branch({}, {"python/bngsim/a.py": "x = 1\n"})
    pc.check_vendored(b, sys.executable)
    assert result("vendored NFsim")[0] == "SKIP"


def test_a_vendored_change_the_tripwire_accepts_passes(make_branch, monkeypatch, tmp_path):
    c = _candidate(monkeypatch, tmp_path)
    b = make_branch({}, _vendor(c, commits=2, patches=2))
    pc.check_vendored(b, sys.executable)
    status, detail = result("vendored NFsim")
    assert status == "PASS" and "--verify-clean --strict: exit 0" in detail


def test_a_tree_the_tripwire_rejects_fails(make_branch, monkeypatch, tmp_path):
    c = _candidate(monkeypatch, tmp_path)
    b = make_branch({}, _vendor(c, commits=2, patches=2, exit_code=1))
    pc.check_vendored(b, sys.executable)
    status, detail = result("vendored NFsim")
    assert status == "FAIL" and "exit 1" in detail and "tree matches the queue" in detail


def test_a_tripwire_that_could_not_run_is_not_a_pass(make_branch, monkeypatch, tmp_path):
    c = _candidate(monkeypatch, tmp_path, present=False)
    b = make_branch({}, _vendor(c, commits=2, patches=2))
    pc.check_vendored(b, sys.executable)
    status, detail = result("vendored NFsim")
    assert status == "FAIL" and "NOT run" in detail


def test_a_checkout_path_that_is_not_the_agreed_one_fails(make_branch, monkeypatch, tmp_path):
    _candidate(monkeypatch, tmp_path)
    b = make_branch({}, _vendor("/somewhere/else/nfsim", commits=2, patches=2))
    pc.check_vendored(b, sys.executable)
    status, detail = result("vendored NFsim")
    assert status == "FAIL" and "/somewhere/else/nfsim" in detail


def test_a_patch_the_queue_does_not_list_fails(make_branch, monkeypatch, tmp_path):
    c = _candidate(monkeypatch, tmp_path)
    b = make_branch({}, _vendor(c, commits=2, patches=3))
    pc.check_vendored(b, sys.executable)
    status, detail = result("vendored NFsim")
    assert status == "FAIL" and "3 patch files, 2 commits" in detail


# --- check_wall_clock -----------------------------------------------------------------

TIMED = ("import time\n\n\ndef test_fast():\n    t0 = time.perf_counter()\n    run()\n"
         "    elapsed = time.perf_counter() - t0\n    %s\n")


def _wall(make_branch, line: str, base: dict | None = None, text: str = TIMED):
    b = make_branch(base or {}, {"python/tests/test_speed.py": text % line})
    pc.check_wall_clock(b)
    return result("no wall-clock bound")


def test_an_assertion_that_bounds_elapsed_seconds_warns(make_branch):
    status, detail = _wall(make_branch, "assert elapsed < 2.0")
    assert status == "WARN" and "test_speed.py: assert elapsed < 2.0" in detail


def test_a_bound_on_the_clock_difference_itself_warns(make_branch):
    assert _wall(make_branch, "assert time.perf_counter() - t0 <= 5")[0] == "WARN"


def test_a_bound_on_a_name_that_ends_in_seconds_warns(make_branch):
    assert _wall(make_branch, "assert elapsed and solve_seconds < 0.5, 'slow'")[0] == "WARN"


def test_a_ratio_or_a_count_is_not_a_wall_clock_bound(make_branch):
    assert _wall(make_branch, "assert elapsed / baseline < ratio_limit")[0] == "PASS"


def test_a_step_count_bound_passes(make_branch):
    assert _wall(make_branch, "assert steps < 2000")[0] == "PASS"


def test_a_lower_bound_passes(make_branch):
    assert _wall(make_branch, "assert elapsed > 0.0")[0] == "PASS"


def test_a_file_that_reads_no_clock_passes(make_branch):
    text = "def test_x():\n    dt = 0.1\n    %s\n"
    assert _wall(make_branch, "assert dt < 0.5", text=text)[0] == "PASS"


def test_a_bound_that_was_already_there_passes(make_branch):
    old = TIMED % "assert elapsed < 2.0"
    b = make_branch({"python/tests/test_speed.py": old},
                    {"python/tests/test_speed.py": old + "\n\ndef test_more():\n    assert run() == 1\n"})
    pc.check_wall_clock(b)
    assert result("no wall-clock bound")[0] == "PASS"


# --- a mutant that tested nothing, and a run cut short --------------------------------

BODY = "- **Mutants: 2 of 2 killed.**\n"


def _evidence(tmp_path, log: str):
    d = tmp_path / "evidence"
    d.mkdir()
    (d / "mutants.log").write_text(log)
    pc.RESULTS.clear()
    pc.check_evidence(BODY, [d])
    return result("body numbers backed")


def test_two_killed_mutants_back_the_body(tmp_path):
    log = "M1 a: KILLED [1 failed]\nM2 b: KILLED [1 failed]\nrestored; 2 of 2 killed\n"
    assert _evidence(tmp_path, log)[0] == "PASS"


def test_a_mutant_that_was_not_applied_fails_whatever_the_body_says(tmp_path):
    log = ("M3 c: NOT APPLIED [`find` occurs 0 times in a.py, not 1]\nM1 a: KILLED [1 failed]\n"
           "M2 b: KILLED [1 failed]\nrestored; 2 of 3 killed; 1 tested nothing (M3 c)\n")
    status, detail = _evidence(tmp_path, log)
    assert status == "FAIL" and "tested nothing (M3: NOT APPLIED)" in detail


def test_a_build_that_failed_under_a_mutant_fails(tmp_path):
    log = "M1 a: KILLED [1 failed]\nM2 b: BUILD FAILED [error]\nrestored; 1 of 2 killed\n"
    status, detail = _evidence(tmp_path, log)
    assert status == "FAIL" and "M2: BUILD FAILED" in detail


def test_an_interrupted_run_fails(tmp_path):
    log = "M1 a: KILLED [1 failed]\nM2 b: KILLED [1 failed]\nINTERRUPTED (SIGTERM) after 2 of 3: not a result\n"
    status, detail = _evidence(tmp_path, log)
    assert status == "FAIL" and "interrupted" in detail


# --- the stacked head is what the new tests are run on --------------------------------


def test_a_stacked_branch_needs_the_head_underneath_not_main():
    pc.RESULTS.clear()
    pc.STATE["stacked_on"] = [(1809, "a" * 40)]
    pc.check_tree_underneath("main-build", "b" * 40, "b" * 40)
    status, detail = result("new tests fail before")
    assert status == "FAIL" and "stacked on #1809" in detail and "aaaaaaaa" in detail
    pc.RESULTS.clear()
    pc.check_tree_underneath("pr-build", "a" * 40, "b" * 40)
    assert pc.RESULTS == []


def test_an_unstacked_branch_is_warned_of_a_stale_main_build():
    pc.RESULTS.clear()
    pc.STATE.clear()
    pc.check_tree_underneath("main-build", "b" * 40, "b" * 40)
    assert pc.RESULTS == []
    pc.check_tree_underneath("main-build", "c" * 40, "b" * 40)
    assert result("main build is current")[0] == "WARN"


def test_the_new_checks_are_run_by_main():
    text = (ROOT / "prepr_check.py").read_text()
    main = text[text.index("def main():"):]
    for call in ("check_stacked(b, body)", "check_private_paths(b, body)", "check_vendored(b, python)",
                 "check_wall_clock(b)", "check_tree_underneath(mt.name, mb, base)"):
        assert call in main, call
