#!/usr/bin/env python3
"""Evaluate model math at its kinks, through the engine and through bngsim's Python
readers, and say where they disagree.

    python kink_check.py "rint(p)" "mod(p, 2)" "frac(p)" "p % 2"
    python kink_check.py --python ~/work/bngsim-x/.venv/bin/python "rint(p)*k"
    python kink_check.py --points=-2.5,-0.5,0.5,2.5 "frac(p)"
    python kink_check.py --json "p % 2"

bngsim evaluates an expression in two kinds of place. The engine (ExprTk, in C++)
computes the run itself. Python readers, built on sympy and numpy, compute what the
run needs beside it: a derived parameter's value where a crossing reads it, the
sensitivity seed of a derived initial condition, and the symbolic form of a rate law
that the analytic Jacobian and the sensitivity RHS are built from. Where a reader
defines a function otherwise than the engine does, the run is right and the number
beside it is wrong, with no error. numpy's ``rint`` rounds a half to even where the
engine rounds it up (#1800); sympy reads ``%`` as a floored modulo and folds
``-2.5 % 2`` to 1.5 where the engine's ``fmod`` gives -0.5 (#1815).

Each expression is written in ``p`` (and may use ``k``, which is 1). At each point,
by default halves, quarters, zero and their negatives, where rounding and modulo
part ways, it is evaluated four ways:

- **engine**: the initial amount of a species seeded with the expression, from a
  plain run (the engine's own value);
- **derived value**: ``_derived_expr_value_numeric``, as a crossing reads it;
- **seed**: the partial of ``(expr)*k`` in ``k``, as a sensitivity seed reads it;
- **rate law**: ``_exprtk_to_sympy``, the rate-law parse, evaluated at that point.
  The kink check and the switch analysis read this form; compiled code prints it
  its own way, so a disagreement here is wrong where that form is evaluated,
  which #1815's literal ``%`` reaches and a symbolic one may not.

A reader that cannot evaluate an expression (it declines it, or a bngsim build is
older than the reader) is shown as such, not as a disagreement. The exit status is
1 if any reader gives a number the engine does not.

The probe runs with ``--python`` (default: ``./.venv/bin/python``, the worktree's),
so this script needs no bngsim of its own.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

POINTS = (-2.5, -1.5, -1.25, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.25, 1.5, 2.5)
READERS = ("derived value", "seed", "rate law")

# Run by the worktree's interpreter: reads a JSON request on stdin, prints JSON.
PROBE = r'''
import json, logging, math, os, sys, tempfile, warnings
logging.disable(logging.WARNING)
warnings.simplefilter("ignore")
import bngsim

req = json.load(sys.stdin)

def num(v):
    if v is None:
        return None
    v = float(v)
    return "nan" if math.isnan(v) else ("inf" if v == math.inf else ("-inf" if v == -math.inf else v))

def engine(expr, p):
    d = tempfile.mkdtemp()
    f = os.path.join(d, "m.net")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("begin parameters\n    1 p %r\n    2 k 1.0\n    3 X0 %s\nend parameters\n"
                 "begin species\n    1 X() X0\nend species\nbegin reactions\nend reactions\n" % (p, expr))
    m = bngsim.Model.from_net(f)
    return bngsim.Simulator(m, method="ode").run(sample_times=[0.0, 1.0]).species[0, 0]

def derived(expr, p):
    from bngsim._codegen import _derived_expr_value_numeric
    return _derived_expr_value_numeric(expr, {"p", "k"}, set(), {"p": 0, "k": 1}, [p, 1.0])

def seed(expr, p):
    from bngsim._codegen import _direct_derived_partials_numeric
    partials, _ = _direct_derived_partials_numeric(
        "(%s)*k" % expr, {"p", "k"}, set(), {"p": 0, "k": 1}, [p, 1.0], False)
    return None if not partials or "k" not in partials else partials["k"]

def rate_law(expr, p):
    import sympy as sp
    from bngsim._jacobian import _exprtk_to_sympy
    e = _exprtk_to_sympy(expr)
    e = e[0] if isinstance(e, tuple) else e
    if e is None:  # the parse declined the expression
        return None
    subs = {s: (p if str(s) == "p" else 1.0) for s in e.free_symbols}
    return complex(e.subs(subs).evalf()).real if e.subs(subs).is_number else None

out = {"bngsim": getattr(bngsim, "__version__", "?"), "rows": []}
for expr in req["exprs"]:
    for p in req["points"]:
        row = {"expr": expr, "p": p}
        for name, fn in (("engine", engine), ("derived value", derived), ("seed", seed), ("rate law", rate_law)):
            try:
                row[name] = num(fn(expr, p))
            except Exception as e:
                row[name] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
        out["rows"].append(row)
print(json.dumps(out))
'''


def as_float(v):
    """A probe value as a float; None for a reader that gave no number."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if v in ("nan", "inf", "-inf"):
        return float(v)
    return None


def agrees(engine, reader) -> bool | None:
    """True if *reader* gives the engine's number (nan matches nan), False if it gives
    another number, None if either gave none (declined, or an error)."""
    a, b = as_float(engine), as_float(reader)
    if a is None or b is None:
        return None
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return a == b or abs(a - b) <= 1e-12 * max(abs(a), abs(b))


def disagreements(rows: list[dict]) -> list[tuple[str, float, str, object, object]]:
    """(expr, p, reader, engine value, reader value) for each reader that gives a
    number other than the engine's."""
    bad = []
    for r in rows:
        for name in READERS:
            if agrees(r.get("engine"), r.get(name)) is False:
                bad.append((r["expr"], r["p"], name, r.get("engine"), r.get(name)))
    return bad


def shown(v) -> str:
    if isinstance(v, dict):
        return "error"
    if v is None:
        return "declined"
    f = as_float(v)
    return repr(f) if f is not None else str(v)


def report(result: dict) -> str:
    rows = result["rows"]
    bad = disagreements(rows)
    lines = [f"bngsim {result.get('bngsim', '?')}: {len(rows)} evaluations"]
    if not bad:
        lines.append("every reader that gave a number gave the engine's")
    else:
        lines.append(f"{len(bad)} disagreement(s) with the engine:")
        lines.append(f"  {'expression':24s} {'p':>7s}  {'reader':14s} {'engine':>12s} {'reader gives':>14s}")
        for expr, p, name, e, v in bad:
            lines.append(f"  {expr:24s} {p:>7g}  {name:14s} {shown(e):>12s} {shown(v):>14s}")
    errors = sorted({(r["expr"], n) for r in rows for n in READERS + ("engine",) if isinstance(r.get(n), dict)})
    if errors:
        lines.append("could not evaluate (not counted as disagreements): "
                     + ", ".join(f"{n} of {e!r}" for e, n in errors))
    return "\n".join(lines)


def probe(python: str, exprs: list[str], points: list[float], timeout: float) -> dict:
    req = json.dumps({"exprs": exprs, "points": points})
    r = subprocess.run([python, "-c", PROBE], input=req, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    if r.returncode:
        raise SystemExit(f"the probe failed under {python}:\n{(r.stderr or r.stdout).strip()[-800:]}")
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if not line:
        raise SystemExit(f"the probe printed no result:\n{r.stdout[-800:]}")
    return json.loads(line[-1])


def _points(text: str) -> list[float]:
    """``--points=-2.5,0.5``: one argument, so the expressions after it stay expressions."""
    try:
        return [float(v) for v in text.split(",") if v.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a comma-separated list of numbers: {text!r}") from e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exprs", nargs="+", help="expressions in p (and k, which is 1), in ExprTk syntax")
    ap.add_argument("--python", default=str(Path(".venv/bin/python")), help="an interpreter with bngsim")
    ap.add_argument("--points", type=_points, default=list(POINTS),
                    help="values of p, comma-separated (--points=-2.5,0.5): default halves, quarters and 0")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--json", action="store_true", help="print the raw results as JSON")
    a = ap.parse_args(argv)
    if not Path(a.python).exists():
        raise SystemExit(f"{a.python}: no such interpreter (pass --python)")
    result = probe(a.python, a.exprs, a.points, a.timeout)
    print(json.dumps(result, indent=1) if a.json else report(result))
    return 1 if disagreements(result["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
