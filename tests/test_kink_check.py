"""Tests for kink_check.py. The probe is replaced by a stand-in, so no bngsim is needed."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kink_check as kc  # noqa: E402


def row(expr, p, engine, **readers):
    return {"expr": expr, "p": p, "engine": engine, **{k.replace("_", " "): v for k, v in readers.items()}}


def test_a_reader_with_another_number_disagrees():
    assert kc.agrees(3.0, 2.0) is False
    assert kc.agrees(-0.5, -0.5) is True


def test_nan_matches_nan_and_not_a_number():
    assert kc.agrees("nan", "nan") is True
    assert kc.agrees("nan", 0.0) is False
    assert kc.agrees("inf", "inf") is True


def test_a_reader_that_gave_no_number_is_not_a_disagreement():
    assert kc.agrees(3.0, None) is None
    assert kc.agrees(3.0, {"error": "TypeError: x"}) is None


def test_disagreements_list_each_reader_off_the_engine():
    rows = [
        row("rint(p)", 2.5, 3.0, derived_value=3.0, seed=2.0, rate_law=3.0),
        row("frac(p)", -0.5, -0.5, derived_value=None, seed=-0.5, rate_law=0.5),
    ]
    assert kc.disagreements(rows) == [
        ("rint(p)", 2.5, "seed", 3.0, 2.0),
        ("frac(p)", -0.5, "rate law", -0.5, 0.5),
    ]


def test_the_report_says_when_all_agree_and_names_what_could_not_be_read():
    rows = [row("p", 1.0, 1.0, derived_value=1.0, seed=1.0, rate_law={"error": "ImportError: no"})]
    text = kc.report({"bngsim": "0.17.2", "rows": rows})
    assert "every reader that gave a number gave the engine's" in text
    assert "rate law of 'p'" in text


STUB = r"""
import json, sys
req = json.load(sys.stdin)
rows = [{"expr": e, "p": p, "engine": 1.0, "derived value": 1.0, "seed": 1.0,
         "rate law": (2.0 if "%" in e else 1.0)} for e in req["exprs"] for p in req["points"]]
print("noise from an import")
print(json.dumps({"bngsim": "stub", "rows": rows}))
print("noise at exit")
"""


def test_main_runs_the_probe_and_exits_1_on_a_disagreement(monkeypatch, capsys):
    monkeypatch.setattr(kc, "PROBE", STUB)
    assert kc.main(["--python", sys.executable, "--points=0.5", "p % 2"]) == 1
    out = capsys.readouterr().out
    assert "1 disagreement(s)" in out and "rate law" in out


def test_main_exits_0_when_every_reader_agrees(monkeypatch, capsys):
    monkeypatch.setattr(kc, "PROBE", STUB)
    assert kc.main(["--python", sys.executable, "--points=0.5,-0.5", "rint(p)"]) == 0
    assert "every reader that gave a number" in capsys.readouterr().out


def test_json_output_is_the_raw_result(monkeypatch, capsys):
    monkeypatch.setattr(kc, "PROBE", STUB)
    kc.main(["--python", sys.executable, "--points=0.5", "--json", "p"])
    assert json.loads(capsys.readouterr().out)["bngsim"] == "stub"


def test_a_probe_that_fails_says_so(monkeypatch):
    monkeypatch.setattr(kc, "PROBE", "raise SystemExit('no bngsim here')")
    with pytest.raises(SystemExit, match="the probe failed"):
        kc.main(["--python", sys.executable, "p"])


def test_a_missing_interpreter_is_refused():
    with pytest.raises(SystemExit, match="no such interpreter"):
        kc.main(["--python", "/no/such/python", "p"])


def test_the_default_points_hold_halves_of_both_signs_and_zero():
    pts = kc.POINTS
    assert 0.0 in pts and 2.5 in pts and -2.5 in pts
    assert all(math.isfinite(p) for p in pts)


def test_points_after_the_flag_leave_the_expressions_alone():
    assert kc._points("-2.5,0.5,1") == [-2.5, 0.5, 1.0]
    with pytest.raises(Exception, match="comma-separated"):
        kc._points("frac(p)")
