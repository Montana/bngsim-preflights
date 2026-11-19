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


def test_a_review_round_reported_without_a_label_is_a_form():
    pc.RESULTS.clear()
    body = GOOD_BODY.replace(
        "**Not reviewed.** This PR needs `/code-review` before it merges.",
        "One round of a whole-branch `/code-review` on `ae2ffa96`. It returned 3 findings; "
        "`9cd18bd3` fixes them.",
    )
    pc.check_template(body, "parser: read x as y (#1)")
    assert result("PR template")[0] == "PASS"


def test_a_review_round_with_no_commit_named_is_not_a_form():
    pc.RESULTS.clear()
    body = GOOD_BODY.replace(
        "**Not reviewed.** This PR needs `/code-review` before it merges.",
        "One round of a whole-branch `/code-review`. It went fine.",
    )
    pc.check_template(body, "parser: read x as y (#1)")
    assert result("PR template")[0] == "FAIL"


def _mutant_evidence(tmp_path, log_text):
    d = tmp_path / "evidence"
    d.mkdir()
    (d / "mutants_r2.json").write_text('{"mutants": []}')  # the definitions: no verdicts
    (d / "mutants_r2.log").write_text(log_text)
    return [d]


def test_mutants_of_any_name_and_a_definitions_file_are_read(tmp_path):
    pc.RESULTS.clear()
    ev = _mutant_evidence(tmp_path, "N1 a: KILLED [1 failed]\nN2 b: KILLED [1 failed]\nrestored; 2 of 2 killed\n")
    pc.check_evidence("3. **Mutants: 2 of 2 killed.**", ev)
    assert result("body numbers backed by logs")[0] == "PASS"


def test_a_cut_short_mutant_log_still_fails(tmp_path):
    pc.RESULTS.clear()
    ev = _mutant_evidence(tmp_path, "N1 a: KILLED [1 failed]\nN2 b: KILLED [1 failed]\n")
    pc.check_evidence("3. **Mutants: 2 of 2 killed.**", ev)
    status, detail = result("body numbers backed by logs")
    assert status == "FAIL" and "restored" in detail


def test_several_review_rounds_reported_are_a_form():
    pc.RESULTS.clear()
    body = GOOD_BODY.replace(
        "**Not reviewed.** This PR needs `/code-review` before it merges.",
        "Two rounds of a whole-branch `/code-review`. The first, on `ae2ffa96`, returned 10.",
    )
    pc.check_template(body, "parser: read x as y (#1)")
    assert result("PR template")[0] == "PASS"


def test_a_mutant_whose_name_holds_a_colon_is_counted(tmp_path):
    pc.RESULTS.clear()
    ev = _mutant_evidence(
        tmp_path,
        "N1 a cycle: whole block as declared: KILLED [1 failed]\nN2 b: KILLED [1 failed]\n"
        "restored; 2 of 2 killed\n",
    )
    pc.check_evidence("3. **Mutants: 2 of 2 killed.**", ev)
    assert result("body numbers backed by logs")[0] == "PASS"


# ── text I/O names its encoding (#1653, #1665) ──────────────────────────────

ENCODING_WARN = {
    "a read_text": "from pathlib import Path\ntext = Path('m.bngl').read_text()\n",
    "a write_text": "from pathlib import Path\nPath('m.net').write_text(text)\n",
    "a text open": "with open(path) as f:\n    pass\n",
    "an open for writing": "with open(path, 'w') as f:\n    pass\n",
    "an open across lines": "with open(\n    path,\n    mode='w',\n) as f:\n    pass\n",
    "a text temporary file": "import tempfile\ntempfile.NamedTemporaryFile('w', suffix='.net')\n",
}
ENCODING_PASS = {
    "an encoding": "from pathlib import Path\nPath('m.net').write_text(text, encoding='utf-8')\n",
    "an encoding on the next line": "with open(\n    path,\n    encoding='utf-8',\n) as f:\n    pass\n",
    "a binary open": "with open(path, 'rb') as f:\n    pass\n",
    "a binary mode keyword": "with open(path, mode='wb') as f:\n    pass\n",
    "a default temporary file": "import tempfile\ntempfile.NamedTemporaryFile(suffix='.h5')\n",
    "os.open": "import os\nfd = os.open(path, os.O_RDONLY)\n",
    "a method named open": "session.open(path)\n",
}


@pytest.mark.parametrize("case", sorted(ENCODING_WARN))
def test_text_io_without_an_encoding_warns(make_branch, case):
    b = make_branch({}, {"python/bngsim/a.py": ENCODING_WARN[case]})
    pc.check_text_encoding(b)
    assert result("text I/O names its encoding")[0] == "WARN"


@pytest.mark.parametrize("case", sorted(ENCODING_PASS))
def test_text_io_with_an_encoding_or_in_binary_passes(make_branch, case):
    b = make_branch({}, {"python/bngsim/a.py": ENCODING_PASS[case]})
    pc.check_text_encoding(b)
    assert result("text I/O names its encoding")[0] == "PASS"


def test_an_untouched_call_is_not_this_branch_s(make_branch):
    """A call already on main is left alone; only the lines the branch adds count."""
    old = "from pathlib import Path\ntext = Path('m.bngl').read_text()\n"
    b = make_branch({"python/bngsim/a.py": old}, {"python/bngsim/a.py": old + "x = 1\n"})
    pc.check_text_encoding(b)
    assert result("text I/O names its encoding")[0] == "PASS"


def test_a_test_file_s_text_io_is_not_the_library_s(make_branch):
    b = make_branch({}, {"python/tests/test_a.py": "open(path).read()\n"})
    pc.check_text_encoding(b)
    assert result("text I/O names its encoding")[0] == "PASS"


# ── referee constants hold their tolerance (#1502) ──────────────────────────

REFEREE_WARN = {
    # #1502's own: run_network's six digits under rel=1e-6.
    "six digits under 1e-6": "assert p == pytest.approx(0.896350, rel=1e-6)\n",
    "a named tolerance": "TOL = 1e-6\n\ndef test_x():\n    assert p == pytest.approx(2.574841, rel=TOL)\n",
    "a named referee": "RN = 0.896350\nTOL = 1e-6\n\ndef test_x():\n    assert p == pytest.approx(RN, rel=TOL)\n",
    "assert_allclose": "np.testing.assert_allclose(p, 0.896350, rtol=1e-6)\n",
}
REFEREE_PASS = {
    "full precision": "assert p == pytest.approx(0.896350382205, rel=1e-8)\n",
    "a short exact value": "assert p == pytest.approx(0.5, rel=1e-12)\n",
    "an integer count": "assert n == pytest.approx(123456, rel=1e-9)\n",
    "a loose tolerance": "assert p == pytest.approx(0.896350, rel=1e-3)\n",
    "no tolerance": "assert p == pytest.approx(0.896350)\n",
    "an expression": "assert p == pytest.approx(2 + 5 * np.exp(-1), rel=1e-12)\n",
}


@pytest.mark.parametrize("case", sorted(REFEREE_WARN))
def test_a_referee_rounded_past_its_tolerance_warns(make_branch, case):
    b = make_branch({}, {"python/tests/test_a.py": REFEREE_WARN[case]})
    pc.check_rounded_referee(b)
    assert result("referee constants hold their tolerance")[0] == "WARN"


@pytest.mark.parametrize("case", sorted(REFEREE_PASS))
def test_a_referee_that_holds_its_tolerance_passes(make_branch, case):
    b = make_branch({}, {"python/tests/test_a.py": REFEREE_PASS[case]})
    pc.check_rounded_referee(b)
    assert result("referee constants hold their tolerance")[0] == "PASS"


def test_a_rounded_referee_in_a_parametrize_table_warns(make_branch):
    """#1502's own shape: the values reached the comparison through a table."""
    text = (
        'TWO = {\n    "s-e1": ("1,2", 0.896350),\n    "homodimer": ("1,1", 4.968136),\n}\n\n'
        '@pytest.mark.parametrize("case", sorted(TWO))\n'
        "def test_x(case):\n    reactants, p_at_1 = TWO[case]\n"
        "    assert run(reactants) == pytest.approx(p_at_1, rel=1e-6)\n"
    )
    b = make_branch({}, {"python/tests/test_a.py": text})
    pc.check_rounded_referee(b)
    status, detail = result("referee constants hold their tolerance")[:2]
    assert status == "WARN" and "0.896350" in detail


def test_a_full_precision_table_passes(make_branch):
    text = (
        'TWO = {"s-e1": ("1,2", 0.896350382205)}\n\n'
        "def test_x():\n    reactants, p_at_1 = TWO['s-e1']\n"
        "    assert run(reactants) == pytest.approx(p_at_1, rel=1e-8)\n"
    )
    b = make_branch({}, {"python/tests/test_a.py": text})
    pc.check_rounded_referee(b)
    assert result("referee constants hold their tolerance")[0] == "PASS"


def test_a_table_is_held_to_its_loosest_comparison(make_branch):
    """#1502's file: one table fed a closed-form check at rel=1e-12 and a referee check at
    a looser tolerance. Which column reaches which is not known, so the table is held to
    the loosest: six digits pass under 1e-2, where 1e-12 would flag every measured value."""
    text = (
        'TWO = {"s-e1": ("1,2", 0.896350, rate(10.0, 1.0))}\n\n'
        "def test_x():\n    reactants, p_at_1, rate_at_0 = TWO['s-e1']\n"
        "    assert rhs(reactants) == pytest.approx(rate_at_0, rel=1e-12)\n"
        "    assert run(reactants) == pytest.approx(p_at_1, rel=1e-2)\n"
    )
    b = make_branch({}, {"python/tests/test_a.py": text})
    pc.check_rounded_referee(b)
    assert result("referee constants hold their tolerance")[0] == "PASS"
