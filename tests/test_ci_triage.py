"""Tests for ci_triage.py, on job records and log excerpts shaped like GitHub's.

Each class has a job that must land in it, and the cases that tell it from its
neighbours: a red run that is the change's fault must never read as the
infrastructure's, and the reverse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ci_triage as ct  # noqa: E402

T0, T1 = "2026-11-18T17:36:19Z", "2026-11-18T20:32:10Z"


def job(**kw):
    base = {"id": 1, "name": "Python suite · ubuntu-latest · 1/2", "conclusion": "failure",
            "runner_name": "GitHub Actions 7", "created_at": T0, "completed_at": T1,
            "steps": [{"name": "Run tests", "conclusion": "failure"}]}
    return base | kw


def log(*lines):
    return "\n".join(f"2026-11-18T18:00:{i:02d}.0000000Z {ln}" for i, ln in enumerate(lines))


def test_a_job_no_runner_took_never_started():
    """Today's macOS wheel: no runner, no step, no log, cancelled after 2 h 55 min."""
    v = ct.classify(job(runner_name="", steps=[], conclusion="cancelled"), None)
    assert v.kind == "never started" and "2 h 55 min" in v.detail
    assert not v.about_the_change


def test_a_record_with_steps_but_no_runner_name_did_run():
    """Steps mean something ran: such a job is read from its log, not called never started."""
    v = ct.classify(job(runner_name=""), log("FAILED python/tests/test_a.py::test_b - AssertionError"))
    assert v.kind == "test failure"


def test_a_job_cancelled_by_a_newer_run_is_superseded():
    newer = [{"id": 99, "created_at": "2026-11-18T19:00:00Z", "display_title": "Merge #2"}]
    v = ct.classify(job(conclusion="cancelled"), log("Run tests", "##[error]The operation was canceled."), newer)
    assert v.kind == "superseded" and "99" in v.detail


def test_a_newer_run_after_the_cancel_does_not_explain_it():
    newer = [{"id": 99, "created_at": "2026-11-18T21:00:00Z"}]
    v = ct.classify(job(conclusion="cancelled"), log("Run tests"), newer)
    assert v.kind == "unexplained" and "by hand" in v.detail


def test_a_job_past_its_limit_timed_out():
    v = ct.classify(job(), log("##[error]The job running on runner X has exceeded the maximum execution time of 360 minutes."))
    assert v.kind == "timed out"


INFRA = {
    "dns": ("Temporary failure resolving 'azure.archive.ubuntu.com'", "DNS"),
    "curl dns": ("curl: (6) Could not resolve host: github.com", "DNS"),
    "apt mirror ign": ("Ign:2 http://azure.archive.ubuntu.com/ubuntu resolute InRelease", "mirror"),
    "apt fetch": ("E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/x.deb  404", "mirror"),
    "artifact html": ("Attempt 1 of 5 failed with error: Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON.",
                      "artifact"),
    "lost runner": ("The runner has received a shutdown signal.", "runner"),
    "disk": ("OSError: [Errno 28] No space left on device", "disk"),
}


@pytest.mark.parametrize("case", sorted(INFRA))
def test_an_infrastructure_failure_is_not_the_change(case):
    line, word = INFRA[case]
    v = ct.classify(job(), log("Run setup", line, "##[error]Process completed with exit code 100."))
    assert v.kind == "infrastructure" and word.lower() in v.detail.lower()
    assert not v.about_the_change


def test_a_failed_setup_step_is_likely_infrastructure():
    """#1643's LAPACK legs: apt timed out in the install step, no pattern to match."""
    steps = [{"name": "Checkout", "conclusion": "success"},
             {"name": "Install liblapack-dev (LAPACK leg only)", "conclusion": "failure"}]
    v = ct.classify(job(steps=steps), log("+ apt_install liblapack-dev", "##[error]Process completed with exit code 124."))
    assert v.kind == "infrastructure" and v.detail.startswith("likely")


def test_failing_tests_are_the_change_even_beside_network_noise():
    v = ct.classify(job(), log("Temporary failure resolving 'pypi.org' (retrying)",
                               "FAILED python/tests/test_a.py::test_b - AssertionError",
                               "= 1 failed, 10 passed in 3.0s ="))
    assert v.kind == "test failure" and v.evidence == ["python/tests/test_a.py::test_b"]
    assert v.about_the_change


def test_a_ctest_failure_is_the_change():
    v = ct.classify(job(), log("3/8 Test #3: test_dense_eigenvalues ......***Failed    0.02 sec"))
    assert v.kind == "test failure"


def test_a_count_check_s_error_is_the_change_and_its_script_is_not_quoted():
    """#1640's NFsim floor: the error is the runner's ##[error] line. The script that prints
    it, which the runner echoes in cyan before it runs, holds ::error:: strings that are
    source, not output, and are not quoted."""
    v = ct.classify(job(), log('\x1b[36;1m  echo "::error::No pytest summary line"\x1b[0m',
                               "##[error]NFSIM_PASSED_FLOOR is stale: NFSIM_PASSED is 395",
                               "##[error]Process completed with exit code 1."))
    assert v.kind == "test failure"
    assert v.evidence == ["NFSIM_PASSED_FLOOR is stale: NFSIM_PASSED is 395"]


def test_a_failure_in_a_test_step_with_no_signature_is_unexplained():
    v = ct.classify(job(), log("Segmentation fault (core dumped)", "##[error]Process completed with exit code 139."))
    assert v.kind == "unexplained" and "Run tests" in v.detail
    assert v.about_the_change


def test_the_summary_points_at_the_change_only_when_a_job_is_its():
    infra = ct.Verdict("a", "infrastructure", "DNS lookup failed")
    tests = ct.Verdict("b", "test failure", "1 failing")
    assert ct.summary([infra]).startswith("not the change")
    assert ct.summary([infra, tests]) == "look at the change: b"
    assert ct.summary([]) == "every job succeeded"


def test_main_reads_a_run_url_and_exits_by_verdict(monkeypatch, capsys):
    run = {"id": 5, "name": "wheels", "display_title": "Merge", "head_sha": "a61c9c6d", "conclusion": "failure",
           "workflow_id": 7, "head_branch": "main", "created_at": T0}
    jobs = {"jobs": [job(id=10, name="wheels · ubuntu", conclusion="success"),
                     job(id=11, name="wheels · macos-15", runner_name="", steps=[], conclusion="cancelled")]}
    pages = {"repos/lanl/bngsim/actions/runs/5": run, "repos/lanl/bngsim/actions/runs/5/jobs?per_page=100": jobs,
             "repos/lanl/bngsim/actions/workflows/7/runs?branch=main&per_page=20": {"workflow_runs": []}}
    monkeypatch.setattr(ct, "_gh", lambda path: json.dumps(pages[path]) if path in pages else None)
    assert ct.main(["https://github.com/lanl/bngsim/actions/runs/5"]) == 0
    out = capsys.readouterr().out
    assert "never started" in out and "wheels · ubuntu" not in out and "not the change" in out
