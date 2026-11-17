#!/usr/bin/env python3
"""Fold constant expressions through the engine and through bngsim's Python readers,
and say where a reader folds one to another number.

    python const_check.py                              # 600 expressions, seed 1
    python const_check.py --functions % frac           # only these
    python const_check.py --count 2000 --seed 7
    python const_check.py --python ~/work/bngsim-x/.venv/bin/python --json

`kink_check.py` evaluates an expression you give it across points of a parameter.
This one writes the expressions: constants, where no parameter is involved and a
parser folds the whole thing before anything else reads it. That fold is where the
readers part from the engine. sympy reads ``-2.5 % 2`` as a floored modulo, 1.5,
where the engine's ``fmod`` gives -0.5; it folds ``0.5 % 0.1`` to 0 where ``fmod``
gives 0.0999..., the two signs agreeing; its ``frac(-2.5)`` is 0.5 for the engine's
-0.5 (#1815). numpy's ``rint`` takes a half to even where the engine takes it up
(#1800). The run is right each time, and a number beside it is wrong with no error.

Each expression is built from operands chosen to sit where the definitions part:
halves, negatives, a whole number written with and without a point, a decimal no
double holds (0.1), a 17-digit literal that rounds differently read twice, a whole
number past 2^53, and constants that are no literal (``_pi``, ``sqrt(2)``). They go
through `kink_check`'s four evaluations (the engine, the derived value, the
sensitivity seed, the rate-law parse). The same seed writes the same expressions.

A reader that declines an expression, or raises on it, is shown as such and is not
a disagreement. The exit status is 1 if any reader gives a number the engine does
not.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kink_check  # noqa: E402

FUNCTIONS = ("%", "mod", "frac", "rint", "round", "floor", "ceil", "trunc")
BINARY = ("%", "mod")

# Where the definitions part ways. A divisor is never zero: fmod by zero is nan.
OPERANDS = (
    "2.5", "-2.5", "0.5", "-0.5", "1.5", "-1.5", "7.5", "-7.5", "1.25", "-1.25",
    "7", "-7", "2", "-2", "3", "-3", "7.0", "-7.0", "2.0", "-2.0",
    "0.1", "-0.1", "0.001", "-0.001", "1000000.5", "-1000000.5",
    "5950.6916598874227", "-5950.6916598874227",  # 17 digits: read twice, rounds twice
    "10000000000000001", "-10000000000000001",  # past 2^53: no double is this number
    "_pi", "-_pi", "sqrt(2)", "-sqrt(2)",
)
DIVISORS = tuple(v for v in OPERANDS if v not in ("10000000000000001", "-10000000000000001"))


def call(fn: str, a: str, b: str | None = None) -> str:
    if fn == "%":
        return f"{a} % {b}"
    return f"{fn}({a}, {b})" if fn in BINARY else f"{fn}({a})"


def expressions(functions: tuple[str, ...], count: int, seed: int) -> list[str]:
    """*count* constant expressions over *functions*, the same for the same seed: every
    function on a spread of single operands first, then compositions."""
    rng = random.Random(seed)
    out: list[str] = []

    def add(expr: str) -> None:
        if expr not in seen:
            seen.add(expr)
            out.append(expr)

    seen: set[str] = set()
    for fn in functions:  # the plain forms: where a fix is first wrong
        for a in OPERANDS:
            if fn in BINARY:
                for b in rng.sample(DIVISORS, 4):
                    add(call(fn, a, b))
            else:
                add(call(fn, a))
    plain = len(out)
    shapes = (
        lambda f, g, a, b, c: f"{call(f, a, b)} * {c}",  # % binds as * does
        lambda f, g, a, b, c: f"{c} * {call(f, a, b)}",
        lambda f, g, a, b, c: f"{c} - {call(f, a, b)}",
        lambda f, g, a, b, c: f"-({a}) % {b}" if f == "%" else call(f, f"-({a})", b),
        lambda f, g, a, b, c: call(f, f"({a} + {c})", b),
        lambda f, g, a, b, c: call(f, a, f"({b} + {b})") if f in BINARY else call(f, f"({a} * {b})"),
        lambda f, g, a, b, c: call(g, f"({call(f, a, b)})", c),  # one inside another
        lambda f, g, a, b, c: f"{call(f, a, b)} % {c}" if f == "%" else call(f, a, b),  # left to right
    )
    tries = 0
    while len(out) < max(count, 0) and tries < 50 * max(count, 1):
        tries += 1
        f, g = rng.choice(functions), rng.choice(functions)
        a, b, c = rng.choice(OPERANDS), rng.choice(DIVISORS), rng.choice(DIVISORS)
        add(rng.choice(shapes)(f, g, a, b, c))
    if count < plain:
        out = rng.sample(out[:plain], count) if count > 0 else []
    return out[:count] if count >= plain else out


def family(expr: str, functions: tuple[str, ...]) -> str:
    """The functions an expression uses, for the summary."""
    used = [f for f in functions if (f == "%" and "%" in expr) or (f != "%" and f"{f}(" in expr)]
    return "+".join(used) or "?"


def report(result: dict, functions: tuple[str, ...], limit: int) -> str:
    rows = result["rows"]
    bad = kink_check.disagreements(rows)
    lines = [f"bngsim {result.get('bngsim', '?')}: {len(rows)} constant expressions over {', '.join(functions)}"]
    if not bad:
        lines.append("every reader that gave a number gave the engine's")
    else:
        exprs = {e for e, *_ in bad}
        lines.append(f"{len(exprs)} expression(s) folded to another number by a reader ({len(bad)} disagreements):")
        by = Counter((name, family(expr, functions)) for expr, _, name, _, _ in bad)
        for (name, fam), n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:5d}  {name:14s} {fam}")
        lines.append(f"  {'expression':40s} {'reader':14s} {'engine':>22s} {'reader gives':>22s}")
        for expr, _, name, e, v in bad[:limit]:
            lines.append(f"  {expr:40s} {name:14s} {kink_check.shown(e):>22s} {kink_check.shown(v):>22s}")
        if len(bad) > limit:
            lines.append(f"  ... and {len(bad) - limit} more (--show N, or --json)")
    gave = Counter()
    for r in rows:
        for name in kink_check.READERS:
            v = r.get(name)
            gave[(name, "error" if isinstance(v, dict) else "declined" if v is None else "number")] += 1
    for name in kink_check.READERS:
        lines.append(f"{name}: {gave[(name, 'number')]} evaluated, {gave[(name, 'declined')]} declined, "
                     f"{gave[(name, 'error')]} raised")
    no_engine = sum(isinstance(r.get("engine"), dict) for r in rows)
    if no_engine:
        lines.append(f"the engine could not evaluate {no_engine} (not compared)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--functions", nargs="+", default=list(FUNCTIONS), metavar="F",
                    help=f"what to fold (default: {' '.join(FUNCTIONS)}); any one-argument ExprTk function may be named")
    ap.add_argument("--count", type=int, default=600, help="how many expressions (default 600)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--python", default=str(Path(".venv/bin/python")), help="an interpreter with bngsim")
    ap.add_argument("--show", type=int, default=20, help="how many disagreements to list")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--list", action="store_true", help="print the expressions and exit")
    ap.add_argument("--json", action="store_true", help="print the raw results as JSON")
    a = ap.parse_args(argv)
    functions = tuple(dict.fromkeys(a.functions))
    exprs = expressions(functions, a.count, a.seed)
    if a.list:
        print("\n".join(exprs))
        return 0
    if not Path(a.python).exists():
        raise SystemExit(f"{a.python}: no such interpreter (pass --python)")
    # A constant has no parameter in it; the probe's p is given a value and not used.
    result = kink_check.probe(a.python, exprs, [0.0], a.timeout)
    print(json.dumps(result, indent=1) if a.json else report(result, functions, a.show))
    return 1 if kink_check.disagreements(result["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
