"""Tests for prepr_check.py, each on a small git repository made for it.

Every check is run on a branch that should pass it and on one that should not,
so a check that can never fail would be caught here too.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _helpers import git, result, write  # noqa: E402, F401

import prepr_check as pc  # noqa: E402


def test_a_fixed_stacklevel_fails(make_branch):
    b = make_branch({}, {"python/bngsim/a.py": 'import warnings\nwarnings.warn("x", stacklevel=2)\n'})
    pc.check_stacklevel(b)
    assert result("no fixed stacklevel")[0] == "FAIL"


def test_a_computed_stacklevel_passes(make_branch):
    b = make_branch({}, {"python/bngsim/a.py": "warnings.warn('x', stacklevel=_outside())\n"})
    pc.check_stacklevel(b)
    assert result("no fixed stacklevel")[0] == "PASS"


def test_a_library_change_without_a_fragment_fails(make_branch):
    b = make_branch({"python/bngsim/a.py": "x = 1\n"}, {"python/bngsim/a.py": "x = 2\n"})
    pc.check_changelog(b)
    assert result("changelog fragment")[0] == "FAIL"


# bngsim's own fragment checker, which the gate runs; a stand-in here.
CHANGELOG_OK = {"ci/changelog.py": "import sys\nsys.exit(0)\n"}
CHANGELOG_BAD = {"ci/changelog.py": "import sys\nprint('1234.fixed.md: no bold lead')\nsys.exit(1)\n"}
FRAGMENT = {"changelog.d/1234.fixed.md": "- **A fix (#1234).** It does a thing.\n"}


def test_a_library_change_with_a_fragment_passes(make_branch):
    b = make_branch(
        {"python/bngsim/a.py": "x = 1\n", **CHANGELOG_OK},
        {"python/bngsim/a.py": "x = 2\n", **FRAGMENT},
    )
    pc.check_changelog(b)
    assert result("changelog fragment")[0] == "PASS"


def test_a_fragment_the_project_checker_refuses_fails(make_branch):
    b = make_branch(
        {"python/bngsim/a.py": "x = 1\n", **CHANGELOG_BAD},
        {"python/bngsim/a.py": "x = 2\n", **FRAGMENT},
    )
    pc.check_changelog(b)
    status, detail = result("changelog fragment")
    assert status == "FAIL" and "no bold lead" in detail


def test_a_fragment_over_the_length_limit_fails(make_branch):
    b = make_branch(
        {"python/bngsim/a.py": "x = 1\n", **CHANGELOG_OK},
        {"python/bngsim/a.py": "x = 2\n", "changelog.d/1234.fixed.md": "- **Long.** " + "x" * 1300 + "\n"},
    )
    pc.check_changelog(b)
    assert result("changelog fragment")[0] == "FAIL"


def test_a_stdlib_named_scratch_script_fails(make_branch):
    b = make_branch({}, {"bisect.py": "print(1)\n"}, commit=False)
    pc.check_scratch_names(b)
    assert result("no stdlib-named scripts")[0] == "FAIL"


def test_attribution_in_a_commit_message_fails(make_branch):
    b = make_branch({}, {"python/bngsim/a.py": "x = 1\n"}, message="fix\n\nCo-Authored-By: Someone <a@b.c>")
    pc.check_attribution(b, None)
    assert result("no attribution")[0] == "FAIL"


def test_no_attribution_passes(make_branch):
    b = make_branch({}, {"python/bngsim/a.py": "x = 1\n"}, message="fix: a thing (#1)")
    pc.check_attribution(b, "A body.")
    assert result("no attribution")[0] == "PASS"


def test_an_uncommitted_tracked_edit_warns(make_branch):
    b = make_branch({"python/bngsim/a.py": "x = 1\n"}, {"python/bngsim/a.py": "x = 2\n"})
    write(b.repo, {"python/bngsim/a.py": "x = 3\n"})
    pc.check_clean_tree(b)
    assert result("tracked tree committed")[0] == "WARN"


def test_a_subprocess_without_a_timeout_in_a_new_test_warns(make_branch):
    b = make_branch({}, {"python/tests/test_x.py": "import subprocess\nsubprocess.run(['ls'])\n"})
    pc.check_new_test_hygiene(b)
    status, detail = result("new-test and portability hygiene")
    assert status == "WARN" and "no timeout" in detail


# --- the description ------------------------------------------------------------------


def test_an_unfilled_marker_fails():
    pc.RESULTS.clear()
    pc.check_placeholders("Full suite: SUITE_LINE\n")
    assert result("no placeholders")[0] == "FAIL"


def test_a_marker_inside_code_is_not_a_placeholder():
    pc.RESULTS.clear()
    pc.check_placeholders("The marker:\n\n```\nSUITE_LINE\n```\n")
    assert result("no placeholders")[0] == "PASS"


GOOD_BODY = "Fixes #1.\n\n## Review\n\n**Not reviewed.** This PR needs `/code-review` before it merges.\n\n## Testing\n\nRan it.\n"


def test_the_template_passes_a_good_title_and_body():
    pc.RESULTS.clear()
    pc.check_template(GOOD_BODY, "part: a change (#1)")
    assert result("PR template")[0] == "PASS"


@pytest.mark.parametrize("title", ["A change", "part: a change", "Part: a change (#1)"])
def test_the_template_fails_a_title_out_of_shape(title):
    pc.RESULTS.clear()
    pc.check_template(GOOD_BODY, title)
    assert result("PR template")[0] == "FAIL"


def test_the_template_fails_a_body_without_review():
    pc.RESULTS.clear()
    pc.check_template("Fixes #1.\n\n## Testing\n\nRan it.\n", "part: a change (#1)")
    assert result("PR template")[0] == "FAIL"


# --- the evidence ----------------------------------------------------------------------


def test_a_log_older_than_the_code_it_ran_warns(make_branch, tmp_path):
    b = make_branch({"python/bngsim/a.py": "x = 1\n"}, {"python/bngsim/a.py": "x = 2\n"})
    evidence = tmp_path / "ab"
    evidence.mkdir()
    log = evidence / "suite_fix.log"
    log.write_text("=== 10 passed in 1.0s ===\n")
    old = time.time() - 3600
    os.utime(log, (old, old))  # the code changed after the suite ran
    pc.check_evidence_fresh(b, [evidence])
    assert result("evidence newer than the code")[0] == "WARN"


def test_a_log_newer_than_the_code_passes(make_branch, tmp_path):
    b = make_branch({"python/bngsim/a.py": "x = 1\n"}, {"python/bngsim/a.py": "x = 2\n"})
    evidence = tmp_path / "ab"
    evidence.mkdir()
    log = evidence / "suite_fix.log"
    log.write_text("=== 10 passed in 1.0s ===\n")
    new = time.time() + 60
    os.utime(log, (new, new))
    pc.check_evidence_fresh(b, [evidence])
    assert result("evidence newer than the code")[0] == "PASS"


def _linux_script(evidence: Path, tests: str) -> None:
    (evidence / "docker").mkdir(parents=True)
    (evidence / "docker" / "run_linux.sh").write_text(
        f".venv/bin/python -m pytest -q -p no:cacheprovider -n 3 -rf {tests} 2>&1 | tee /out/pytest.log\n"
    )


HILL_BASE = {
    "python/bngsim/hill.py": "def hill_guard(h):\n    return h\n",
    "python/tests/test_net_hill_below_one.py": "def test_a():\n    assert True\n",
}
HILL_HEAD = {
    "python/bngsim/hill.py": "def hill_guard(h):\n    return h + 0\n",
    "python/tests/test_hill_guard_for_every_h.py": "def test_b():\n    assert True\n",
}


def test_a_linux_list_that_leaves_out_a_touched_test_warns(make_branch, tmp_path):
    # #1636's shape: the Linux run named the new file, not the feature's old one.
    b = make_branch(HILL_BASE, HILL_HEAD)
    evidence = tmp_path / "ab"
    _linux_script(evidence, "python/tests/test_hill_guard_for_every_h.py")
    pc.check_linux_list(b, [evidence])
    status, detail = result("Linux run covers the touched tests")
    assert status == "WARN" and "test_net_hill_below_one.py" in detail


def test_a_linux_list_with_the_touched_tests_passes(make_branch, tmp_path):
    b = make_branch(HILL_BASE, HILL_HEAD)
    evidence = tmp_path / "ab"
    _linux_script(
        evidence,
        "python/tests/test_hill_guard_for_every_h.py python/tests/test_net_hill_below_one.py",
    )
    pc.check_linux_list(b, [evidence])
    assert result("Linux run covers the touched tests")[0] == "PASS"


def test_a_linux_run_of_the_whole_suite_passes(make_branch, tmp_path):
    b = make_branch(HILL_BASE, HILL_HEAD)
    evidence = tmp_path / "ab"
    _linux_script(evidence, "python/tests")
    pc.check_linux_list(b, [evidence])
    status, detail = result("Linux run covers the touched tests")
    assert status == "PASS" and "whole of python/tests" in detail


# --- the stamp ------------------------------------------------------------------------


def test_the_stamp_changes_with_the_body_and_the_tree(make_branch):
    b = make_branch({"python/bngsim/a.py": "x = 1\n"}, {"python/bngsim/a.py": "x = 2\n"})
    first = pc.fingerprint(b.repo, "body")
    assert pc.fingerprint(b.repo, "body") == first
    assert pc.fingerprint(b.repo, "another body") != first
    write(b.repo, {"python/bngsim/a.py": "x = 3\n"})
    assert pc.fingerprint(b.repo, "body") != first


# --- a drafted comment ----------------------------------------------------------------


def test_a_comment_that_promises_work_is_flagged(tmp_path):
    path = tmp_path / "reply.md"
    path.write_text("Thanks, I'll fix that tomorrow.\n")
    problems = pc.lint_comment(path)
    assert any("commits the user to work" in p and "I'll" in p for p in problems)


def test_a_plain_thank_you_is_not_flagged(tmp_path):
    path = tmp_path / "reply.md"
    path.write_text("Thank you, that makes sense.\n")
    assert pc.lint_comment(path) == []
