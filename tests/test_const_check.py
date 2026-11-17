"""Tests for const_check.py: the expressions it writes, and its report."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import const_check as cc  # noqa: E402
import kink_check  # noqa: E402


def test_the_same_seed_writes_the_same_expressions():
    assert cc.expressions(cc.FUNCTIONS, 300, 3) == cc.expressions(cc.FUNCTIONS, 300, 3)
    assert cc.expressions(cc.FUNCTIONS, 300, 3) != cc.expressions(cc.FUNCTIONS, 300, 4)


def test_it_writes_as_many_as_asked_and_none_twice():
    for count in (0, 5, 40, 900):
        exprs = cc.expressions(("%", "frac"), count, 1)
        assert len(exprs) == count == len(set(exprs))


def test_every_function_is_tried_on_every_operand_first():
    exprs = cc.expressions(("frac", "%"), 500, 1)
    for a in cc.OPERANDS:
        assert f"frac({a})" in exprs
        assert any(e.startswith(f"{a} % ") for e in exprs)


def test_the_cases_that_found_bugs_are_among_them():
    exprs = cc.expressions(cc.FUNCTIONS, 600, 1)
    assert "frac(-2.5)" in exprs and "rint(2.5)" in exprs and "frac(-_pi)" in exprs
    assert any(e.startswith("-2.5 % ") for e in exprs)
    assert any(e.startswith("10000000000000001 % ") for e in exprs)  # past 2^53
    assert any("5950.6916598874227" in e for e in exprs)  # a 17-digit literal


def test_no_divisor_is_zero():
    for e in cc.expressions(cc.FUNCTIONS, 900, 2):
        assert "% 0" not in e.replace("% 0.", "% x") and ", 0)" not in e


def test_only_the_functions_asked_for_are_used():
    for e in cc.expressions(("frac",), 60, 1):
        assert "%" not in e and "mod(" not in e and "rint(" not in e


def test_the_family_of_an_expression():
    assert cc.family("frac(-2.5 % 2)", cc.FUNCTIONS) == "%+frac"
    assert cc.family("mod(2.5, 2)", cc.FUNCTIONS) == "mod"


ROWS = [
    {"expr": "-2.5 % 2", "p": 0.0, "engine": -0.5, "derived value": -0.5, "seed": None, "rate law": 1.5},
    {"expr": "frac(-2.5)", "p": 0.0, "engine": -0.5, "derived value": 0.5, "seed": {"error": "E"}, "rate law": 0.5},
    {"expr": "frac(2.5)", "p": 0.0, "engine": 0.5, "derived value": 0.5, "seed": 0.5, "rate law": 0.5},
]


def test_the_report_counts_by_reader_and_function():
    text = cc.report({"bngsim": "1.0", "rows": ROWS}, cc.FUNCTIONS, 20)
    assert "2 expression(s) folded to another number by a reader (3 disagreements)" in text
    assert "    2  rate law       " not in text  # one of %, one of frac
    assert "    1  rate law       %" in text and "    1  rate law       frac" in text
    assert "    1  derived value  frac" in text
    assert "seed: 1 evaluated, 1 declined, 1 raised" in text
    assert "derived value: 3 evaluated, 0 declined, 0 raised" in text


def test_a_declined_or_raising_reader_is_not_a_disagreement():
    rows = [{"expr": "rint(2.5)", "p": 0.0, "engine": 3.0, "derived value": None, "seed": {"error": "E"},
             "rate law": 3.0}]
    assert "every reader that gave a number gave the engine's" in cc.report({"rows": rows}, cc.FUNCTIONS, 20)


def _run(monkeypatch, rows, capsys, *args):
    seen = {}

    def probe(python, exprs, points, timeout):
        seen.update(exprs=exprs, points=points)
        return {"bngsim": "1.0", "rows": rows}

    monkeypatch.setattr(kink_check, "probe", probe)
    code = cc.main(["--python", sys.executable, *args])
    return code, capsys.readouterr().out, seen


def test_main_exits_1_on_a_disagreement_and_0_without(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, ROWS, capsys, "--count", "30", "--functions", "%", "frac")
    assert code == 1 and "folded to another number" in out
    assert len(seen["exprs"]) == 30 and seen["points"] == [0.0]
    code, out, _ = _run(monkeypatch, ROWS[2:], capsys)
    assert code == 0 and "every reader" in out


def test_list_prints_the_expressions_without_an_interpreter(capsys):
    assert cc.main(["--list", "--count", "7", "--python", "/no/such/python"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 7
