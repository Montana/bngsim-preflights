"""Tests for the checks that came from the 2026-11-15 review of 18 open PRs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import git, result  # noqa: E402

import prepr_check as pc  # noqa: E402

# --- check_remote_branch -------------------------------------------------------------


def _gh_answers(monkeypatch, head_sha: str):
    real = pc.sh

    def fake(args, cwd, check=False, timeout=600):
        if args[:3] == ["gh", "pr", "view"]:
            out = json.dumps({"headRefOid": head_sha, "headRefName": "fix",
                              "headRepository": {"name": "bngsim"}, "headRepositoryOwner": {"login": "me"}})
            return subprocess.CompletedProcess(args, 0, out, "")
        return real(args, cwd, check, timeout)

    monkeypatch.setattr(pc, "sh", fake)


def test_a_pr_head_already_in_the_branch_passes(make_branch, monkeypatch):
    b = make_branch({}, {"a.py": "x = 1\n"})
    _gh_answers(monkeypatch, git(b.repo, "rev-parse", "HEAD~1"))
    pc.check_remote_branch(b, "1")
    assert result("no one else pushed")[0] == "PASS"


def test_a_commit_someone_else_pushed_fails(make_branch, monkeypatch):
    b = make_branch({}, {"a.py": "x = 1\n"})
    git(b.repo, "checkout", "-q", "-b", "theirs", "main")
    (b.repo / "b.py").write_text("y = 2\n")
    git(b.repo, "add", "b.py")
    git(b.repo, "commit", "-q", "-m", "a reviewer's fix")
    theirs = git(b.repo, "rev-parse", "HEAD")
    git(b.repo, "checkout", "-q", "fix")
    _gh_answers(monkeypatch, theirs)
    pc.check_remote_branch(b, "1")
    status, detail = result("no one else pushed")
    assert status == "FAIL" and "a reviewer's fix" in detail


# --- check_untouched_readers ----------------------------------------------------------

ROOTS = "def roots(m):\n    return m\n"
READER = "from bngsim._a import roots\n\ndef run(m):\n    return roots(m)\n"


def test_a_changed_function_with_an_untouched_caller_warns(make_branch):
    b = make_branch({"python/bngsim/_a.py": ROOTS, "python/bngsim/_b.py": READER},
                    {"python/bngsim/_a.py": "def roots(m):\n    return [r for r in m if r]\n"})
    pc.check_untouched_readers(b)
    status, detail = result("untouched readers")
    assert status == "WARN" and "roots <- _b.py:run" in detail


def test_a_caller_the_branch_also_changed_is_not_listed(make_branch):
    b = make_branch(
        {"python/bngsim/_a.py": ROOTS, "python/bngsim/_b.py": READER},
        {"python/bngsim/_a.py": "def roots(m):\n    return [r for r in m if r]\n",
         "python/bngsim/_b.py": "from bngsim._a import roots\n\ndef run(m):\n    return list(roots(m))\n"},
    )
    pc.check_untouched_readers(b)
    assert result("untouched readers")[0] == "PASS"


# --- check_py310 -----------------------------------------------------------------------


def test_a_311_api_fails(make_branch):
    b = make_branch({}, {"python/tests/test_x.py": "import locale\nenc = locale.getencoding()\n"})
    pc.check_py310(b)
    status, detail = result("nothing newer than Python 3.10")
    assert status == "FAIL" and "getencoding" in detail


def test_a_311_api_behind_a_version_check_passes(make_branch):
    text = "import sys\nif sys.version_info >= (3, 11):\n    import tomllib\n"
    b = make_branch({}, {"python/bngsim/_x.py": text})
    pc.check_py310(b)
    status, detail = result("nothing newer than Python 3.10")
    assert status == "PASS" and "behind a version check" in detail


# --- check_engine_math -----------------------------------------------------------------


def test_a_bare_lambdify_warns(make_branch):
    b = make_branch({}, {"python/bngsim/_x.py": "import sympy as sp\nf = sp.lambdify(x, e, 'numpy')\n"})
    pc.check_engine_math(b)
    status, detail = result("model math read as the engine")
    assert status == "WARN" and "lambdify" in detail


def test_numpy_rint_warns_and_a_routed_lambdify_passes(make_branch):
    b = make_branch({}, {"python/bngsim/_x.py": "import numpy as np\ny = np.rint(x)\n"})
    pc.check_engine_math(b)
    assert result("model math read as the engine")[0] == "WARN"
    pc.RESULTS.clear()
    routed = "f = sp.lambdify(x, _engine_rounding(e, closed_form=False), modules=[_ENGINE_ROUNDING_NUMPY, 'numpy'])\n"
    (b.repo / "python/bngsim/_x.py").write_text(routed)
    pc.check_engine_math(pc.Branch(b.repo, "main"))
    assert result("model math read as the engine")[0] == "PASS"


# --- check_negative_only_tests ---------------------------------------------------------


def test_a_test_with_only_negative_assertions_warns(make_branch):
    text = "def test_quiet(caplog):\n    lines = [r for r in caplog.records if 'roots' in r.message]\n    assert lines == []\n    assert not caplog.records\n"
    b = make_branch({}, {"python/tests/test_q.py": text})
    pc.check_negative_only_tests(b)
    status, detail = result("every new test can fail")
    assert status == "WARN" and "test_quiet" in detail


def test_a_positive_control_passes(make_branch):
    text = "def test_quiet(caplog):\n    assert caplog.records == []\n    assert run() == 4\n"
    b = make_branch({}, {"python/tests/test_q.py": text})
    pc.check_negative_only_tests(b)
    assert result("every new test can fail")[0] == "PASS"


# --- check_test_bngpath ----------------------------------------------------------------

BNG_TEST = (
    "import os, subprocess\nBNG = os.environ.get('BNGPATH')\n"
    "def test_x(tmp_path):\n    subprocess.run(['perl', BNG + '/BNG2.pl', 'm.bngl'], cwd=tmp_path, timeout=60)\n"
)


def test_a_relative_bngpath_under_cwd_warns(make_branch):
    b = make_branch({}, {"python/tests/test_b.py": BNG_TEST})
    pc.check_test_bngpath(b)
    assert result("BNGPATH resolved")[0] == "WARN"


def test_a_resolved_bngpath_passes(make_branch):
    text = BNG_TEST.replace("os.environ.get('BNGPATH')", "Path(os.environ.get('BNGPATH', '.')).resolve()")
    b = make_branch({}, {"python/tests/test_b.py": "from pathlib import Path\n" + text})
    pc.check_test_bngpath(b)
    assert result("BNGPATH resolved")[0] == "PASS"


# --- check_docs_claims -----------------------------------------------------------------


def test_a_named_function_at_an_untouched_site_warns(make_branch):
    base = {"src/a.cpp": "void register_roots() { set_warning_file(); }\n",
            "src/b.cpp": "void ctor() { set_warning_file(); }\n"}
    head = {"src/b.cpp": "void ctor() { if (!user_set()) set_warning_file(); }\n",
            "changelog.d/1680.fixed.md": "- `set_warning_file()` now honours the variable.\n"}
    b = make_branch(base, head)
    pc.check_docs_claims(b, "1680")
    status, detail = result("changelog claims hold")
    assert status == "WARN" and "set_warning_file: a.cpp" in detail


def test_every_site_touched_passes(make_branch):
    base = {"src/b.cpp": "void ctor() { set_warning_file(); }\n"}
    head = {"src/b.cpp": "void ctor() { if (!user_set()) set_warning_file(); }\n",
            "changelog.d/1680.fixed.md": "- `set_warning_file()` now honours the variable.\n"}
    b = make_branch(base, head)
    pc.check_docs_claims(b, "1680")
    assert result("changelog claims hold")[0] == "PASS"


def test_a_success_check_is_positive(make_branch):
    # `returncode == 0` says something ran and succeeded: not a negative assertion.
    text = "def test_runs(tmp_path):\n    done = run()\n    assert done.returncode == 0\n"
    b = make_branch({}, {"python/tests/test_r.py": text})
    pc.check_negative_only_tests(b)
    assert result("every new test can fail")[0] == "PASS"


# --- check_binding_order ---------------------------------------------------------------

PREP = '''def prepare(sym_map, local_dict):
    local_dict.update(round_bindings())
    local_dict.update(sym_map)
    local_dict.update(Piecewise=Piecewise)
'''


def test_a_binding_after_the_models_names_warns(make_branch):
    late = PREP + "    local_dict.update(rounding_bindings())\n"
    b = make_branch({"python/bngsim/_p.py": PREP}, {"python/bngsim/_p.py": late})
    pc.check_binding_order(b)
    status, detail = result("parse bindings before")
    assert status == "WARN" and "_p.py:5 in prepare" in detail


def test_a_binding_before_the_models_names_passes(make_branch):
    early = PREP.replace("    local_dict.update(sym_map)\n",
                         "    local_dict.update(rounding_bindings())\n    local_dict.update(sym_map)\n")
    b = make_branch({"python/bngsim/_p.py": PREP}, {"python/bngsim/_p.py": early})
    pc.check_binding_order(b)
    assert result("parse bindings before")[0] == "PASS"


# --- check_percent_parse ---------------------------------------------------------------


def test_a_new_parse_with_no_spell_mod_warns(make_branch):
    text = "from sympy import parse_expr\n\ndef read(s):\n    return parse_expr(s)\n"
    b = make_branch({}, {"python/bngsim/_r.py": text})
    pc.check_percent_parse(b)
    status, detail = result("% spelled before")
    assert status == "WARN" and "parse_expr(s)" in detail


def test_a_new_parse_after_spell_mod_passes(make_branch):
    text = "from sympy import parse_expr\n\ndef read(s):\n    return parse_expr(_spell_mod(s))\n"
    b = make_branch({}, {"python/bngsim/_r.py": text})
    pc.check_percent_parse(b)
    assert result("% spelled before")[0] == "PASS"
