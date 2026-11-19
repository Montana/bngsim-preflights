#!/usr/bin/env python3
"""Pre-PR gate for lanl/bngsim branches: the review findings that recur, as checks.

Run from a branch's worktree (or pass --repo). Everything is judged on what the
branch ADDS over its merge base with the upstream main, never on main's own code.

    python prepr_check.py --issue 1660
    python prepr_check.py --issue 1660 --body pr.md --title "simulator: ... (#1660)" --floor
    python prepr_check.py --issue 1660 --body pr.md --count --main-pkg ~/work/ab-1660/main-pkg
    python prepr_check.py --body pr.md --check-stamp      # just before pushing or opening the PR
    python prepr_check.py --issue 1681 --body pr.md --evidence ~/work/ab-1681 --main-tree ~/work/bngsim-main
    python prepr_check.py --comment reply.md               # a drafted PR comment

Everything at once, before opening a PR:

    python prepr_check.py --issue N --body pr.md --title "part: change (#N)" --floor \
        --evidence ~/work/ab-N --main-tree ~/work/bngsim-main \
        --full --docs-python ~/work/docsvenv/bin/python

A branch with an open PR is checked against it (or pass --pr): every comment and
review since its last pushed commit fails the gate until --thread-read says they
were read. A run that passes writes a stamp of HEAD, the tracked tree and the
body; --check-stamp exits 1 if any of the three changed since.

Each check prints PASS, FAIL, WARN or SKIP with the reason and, for a FAIL or a
WARN, the lesson it comes from: a review finding on an earlier pull request.
Exit status 1 if any check FAILs. A MANUAL list closes the run: the classes no
script can decide, to be answered in the PR body.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

RESULTS: list[tuple[str, str, str, str]] = []  # (status, name, detail, lesson)
COLOR = sys.stdout.isatty()
MARK = {"PASS": "32", "FAIL": "31", "WARN": "33", "SKIP": "90"}

LIBRARY = ("src/", "include/", "python/bngsim/", "third_party/", "CMakeLists.txt")
OVERCLAIM = re.compile(
    r"\b([Ee]very|[Aa]ll|[Aa]lways|[Nn]ever|none|[Uu]nchanged|no change|bit[- ]for[- ]bit|"
    r"bit-identical|identical|[Ee]xactly|[Oo]nly)\b"
)
STDLIB_NAMES = {
    "bisect", "random", "json", "sample", "math", "os", "sys", "re", "time", "types", "copy",
    "string", "io", "abc", "ast", "csv", "enum", "glob", "heapq", "inspect", "logging",
    "numbers", "operator", "pickle", "queue", "select", "signal", "socket", "statistics",
    "subprocess", "tempfile", "test", "threading", "token", "typing", "uuid", "warnings",
}


def sh(args, cwd, check=False, timeout=600):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def record(status, name, detail, lesson=""):
    RESULTS.append((status, name, detail, lesson))


# ─── the branch ─────────────────────────────────────────────────────────────


class Branch:
    def __init__(self, repo: Path, base_ref: str):
        self.repo = repo
        self.base_ref = base_ref
        self.merge_base = sh(["git", "merge-base", "HEAD", base_ref], repo, check=True).stdout.strip()
        # Committed + staged + unstaged changes, plus untracked files, against the merge base.
        names = sh(["git", "diff", "--name-status", self.merge_base], repo, check=True).stdout
        self.changes = [ln.split("\t") for ln in names.splitlines() if ln.strip()]
        self.untracked = [
            f for f in sh(["git", "ls-files", "--others", "--exclude-standard"], repo).stdout.splitlines() if f
        ]
        self.files = [c[-1] for c in self.changes] + self.untracked
        self.diff = sh(["git", "diff", "-U0", self.merge_base], repo, check=True).stdout
        self.added_at: dict[str, set[int]] = {}
        self.added = self._added_lines()

    def _added_lines(self):
        out, path, at = [], None, 0
        for line in self.diff.splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("@@") and path:
                at = int(re.search(r"\+(\d+)", line).group(1))
            elif line.startswith("+") and not line.startswith("+++") and path:
                out.append((path, line[1:]))
                self.added_at.setdefault(path, set()).add(at)
                at += 1
        for f in self.untracked:  # an untracked file is all added
            p = self.repo / f
            if p.is_file() and p.stat().st_size < 2_000_000:
                try:
                    lines = p.read_text(errors="replace").splitlines()
                except OSError:
                    continue
                out.extend((f, ln) for ln in lines)
                self.added_at[f] = set(range(1, len(lines) + 1))
        return out

    def prose_added(self, path: str) -> list[str]:
        """Added lines of a Python file that are comments or string literals (docstrings,
        messages): where a claim can be made. Code lines are not prose."""
        p = self.repo / path
        rows = self.added_at.get(path, set())
        if not rows or not p.is_file():
            return []
        text = p.read_text(errors="replace")
        prose: set[int] = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING) or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                    prose.update(range(tok.start[0], tok.end[0] + 1))
        except (tokenize.TokenError, SyntaxError):
            return []
        lines = text.splitlines()
        return [lines[i - 1] for i in sorted(rows & prose) if i <= len(lines)]

    def added_in(self, prefix=(), suffix=()):
        return [(p, ln) for p, ln in self.added if p.startswith(prefix or ("",)) and p.endswith(suffix or ("",))]


# ─── checks ─────────────────────────────────────────────────────────────────


def check_attribution(b: Branch, body: str | None):
    """No AI attribution anywhere (no-attribution-in-commits)."""
    pat = re.compile("cl" + "aude|co-authored-by|generated with|anthr" + "opic|\U0001F916", re.I)
    hits = []
    log = sh(["git", "log", f"{b.merge_base}..HEAD", "--format=%H%n%B"], b.repo).stdout
    hits += [f"commit message: {ln.strip()[:80]}" for ln in log.splitlines() if pat.search(ln)]
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    if pat.search(branch):
        hits.append(f"branch name {branch}")
    hits += [f"{p}: {ln.strip()[:80]}" for p, ln in b.added if pat.search(ln) and ("cl" + "aude") in ln.lower()]
    if body and pat.search(body):
        hits.append("PR body mentions it")
    record("FAIL" if hits else "PASS", "no attribution", "; ".join(hits[:4]) or "none in commits, diff, branch or body",
           "no-attribution-in-commits")


def check_staged_deletions(b: Branch):
    """Stage by name; never sweep the main checkout's deletions in (stage-files-by-name)."""
    staged = sh(["git", "diff", "--cached", "--name-status"], b.repo).stdout.splitlines()
    deleted = [ln for ln in staged if ln.startswith("D")]
    committed_del = [c for c in b.changes if c[0] == "D"]
    bad = deleted + ["\t".join(c) for c in committed_del]
    record("FAIL" if bad else "PASS", "no deleted files staged or committed",
           f"{len(bad)}: " + ", ".join(x.split()[-1] for x in bad[:5]) if bad else "none",
           "stage-files-by-name (#1157 pushed 104 deletions)")


def check_base(b: Branch):
    """Merge main and rerun before review; rebuild when main moved the core."""
    behind = sh(["git", "rev-list", "--count", f"{b.merge_base}..{b.base_ref}"], b.repo).stdout.strip()
    core = sh(["git", "diff", "--stat", b.merge_base, b.base_ref, "--", "src", "include", "third_party",
               "CMakeLists.txt", "cmake"], b.repo).stdout.strip()
    if behind == "0":
        record("PASS", "on current main", f"merge base is {b.base_ref}", "keep-prs-mergeable-review-proof")
    elif core:
        record("FAIL", "on current main",
               f"{behind} commits behind, and main changed the core since the merge base "
               f"({core.splitlines()[-1].strip()}): rebase, rebuild, rerun", "check-core-drift-before-touch")
    else:
        record("WARN", "on current main", f"{behind} commits behind (Python/docs only): rebase and rerun",
               "keep-prs-mergeable-review-proof")


def check_changelog(b: Branch):
    """A library change stages a fragment; fragments are well-formed and under 1,200 chars."""
    lib = [f for f in b.files if f.startswith(LIBRARY)]
    frags = [f for f in b.files if f.startswith("changelog.d/") and f.endswith(".md") and "README" not in f]
    if lib and not frags:
        record("FAIL", "changelog fragment", f"{len(lib)} library files changed, no changelog.d fragment",
               "changelog-fragment-required (#1411)")
        return
    if not frags:
        record("SKIP", "changelog fragment", "no library change")
        return
    problems = []
    for f in frags:
        p = b.repo / f
        if p.exists():
            n = len(p.read_text())
            if n > 1200:
                problems.append(f"{f}: {n} chars > 1,200")
            elif n > 1100:
                problems.append(f"{f}: {n} chars, near the 1,200 limit")
    r = sh([sys.executable, "ci/changelog.py", "check"], b.repo)
    if r.returncode:
        record("FAIL", "changelog fragment", r.stdout.strip()[-300:] + r.stderr.strip()[-200:], "ci/changelog.py check")
    elif any(">" in x for x in problems):
        record("FAIL", "changelog fragment", "; ".join(problems), "#1130: fragment over 1,200 chars")
    elif problems:
        record("WARN", "changelog fragment", "; ".join(problems), "#1130")
    else:
        record("PASS", "changelog fragment", ", ".join(frags))


def check_untracked_new_files(b: Branch):
    """pre-commit --all-files skips untracked files (precommit-all-files, #1377)."""
    loose = [f for f in b.untracked if f.endswith((".py", ".cpp", ".hpp", ".md")) and not f.startswith("build/")]
    record("FAIL" if loose else "PASS", "new files tracked (git add -N)",
           ", ".join(loose[:6]) + " — add them before pre-commit --all-files" if loose else "all new files are tracked",
           "precommit-all-files (#1377)")


def check_stacklevel(b: Branch):
    """Never a fixed stacklevel (#1379, #1388, #1421, #1642, #1643)."""
    hits = [f"{p}: {ln.strip()[:70]}" for p, ln in b.added_in(("python/bngsim/",), (".py",))
            if re.search(r"stacklevel\s*=\s*\d", ln)]
    record("FAIL" if hits else "PASS", "no fixed stacklevel", "; ".join(hits[:4]) or "none added",
           "use _stacklevel_outside_bngsim() (class 5; repeated 5 times)")


_TEXT_IO = re.compile(r"(?<![\w.])open\(|\.read_text\(|\.write_text\(|NamedTemporaryFile\(|TemporaryFile\(")


def _call_args(text: str, start: int) -> str:
    """The text of the call whose `(` ends at text[start - 1], up to its matching `)`."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def check_text_encoding(b: Branch):
    """Text read or written in the locale's encoding (#1653, #1665): UTF-8 on macOS and Linux,
    cp1252 on Windows. write_net wrote `Aµ()` as a byte the loader refuses, and could not
    write `AΩ()` at all. bngsim's ruff selection does not include PLW1514."""
    hits = []
    for path in sorted({p for p, _ in b.added_in(("python/bngsim/",), (".py",))}):
        rows, p = b.added_at.get(path, set()), b.repo / path
        if not rows or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _TEXT_IO.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            if line not in rows:
                continue
            args = _call_args(text, m.end())
            name = m.group(0).rstrip("(").lstrip(".")
            if "encoding" in args:
                continue
            if name in ("open", "NamedTemporaryFile", "TemporaryFile"):
                mode = re.search(r"""(?:^|,|mode\s*=)\s*["']([rwxabt+]+)["']""", args)
                if mode is None and name != "open":
                    continue  # a TemporaryFile's default mode is binary
                if "b" in (mode.group(1) if mode else "r"):
                    continue
            hits.append(f"{path}:{line}: {name}()")
    record("WARN" if hits else "PASS", "text I/O names its encoding", "; ".join(hits[:4]) or "none added",
           "#1653, #1665: pass encoding='utf-8' (and errors= where bytes may not decode)")


_APPROX = re.compile(r"(?:approx|assert_allclose)\((?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)")
_FLOAT = r"[-+]?(?:\d[\d_]*\.\d*|\.\d+|\d[\d_]*)(?:[eE][-+]?\d+)?"


def _relative_half_ulp(literal: str) -> tuple[int, float] | None:
    """(significant digits, relative rounding of the literal's last digit), or None for 0."""
    from decimal import Decimal, InvalidOperation

    try:
        d = Decimal(literal.replace("_", ""))
    except InvalidOperation:
        return None
    if d == 0:
        return None
    digits = d.as_tuple().digits
    while len(digits) > 1 and digits[0] == 0:
        digits = digits[1:]
    return len(digits), float(Decimal(5).scaleb(d.as_tuple().exponent - 1) / abs(d))


def _table_literals(text: str, rows: set[int]) -> list[str]:
    """The decimal literals the branch adds to the file's module-level UPPERCASE tables
    (dicts, lists, tuples), strings left out."""
    out = []
    for m in re.finditer(r"^[A-Z][A-Z0-9_]*\s*(?::[^=\n]*)?=\s*[\[({]", text, re.M):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(text[i], 0)
            i += 1
        block = text[start:i]
        first = text.count("\n", 0, start) + 1
        for n, ln in enumerate(block.split("\n")):
            if first + n not in rows:
                continue
            code = re.sub(r"\"[^\"]*\"|'[^']*'|#.*", "", ln)
            out += [v for v in re.findall(_FLOAT, code) if re.search(r"[.eE]", v)]
    return out


def _rounded(path: str, where, values: list[str], tol: str) -> list[str]:
    out = []
    for value in dict.fromkeys(values):
        # An integer literal is a count, exact; only a decimal can be a rounded reading.
        measured = re.fullmatch(_FLOAT, value) and re.search(r"[.eE]", value)
        rounding = _relative_half_ulp(value) if measured else None
        if rounding and rounding[0] >= 5 and rounding[1] > float(tol) / 10:
            out.append(f"{path}:{where}: {value} rounds by {rounding[1]:.1e} of rel={tol}")
    return out


def check_rounded_referee(b: Branch):
    """A referee's value written to fewer digits than its tolerance can hold (#1502): run_network's
    0.896350 under rel=1e-6 is 5.6e-7 off by rounding alone, so a 2.3x margin read as a pass.
    Short literals (0.5, 10.0) are taken as exact; five or more digits look measured."""
    hits = []
    for path in sorted({p for p, _ in b.added_in(("python/tests/",), (".py",))}):
        p = b.repo / path
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        consts = {m.group(1): m.group(2)
                  for m in re.finditer(rf"^([A-Z][A-Z0-9_]*)\s*=\s*({_FLOAT})\s*(?:#.*)?$", text, re.M)}
        rows = b.added_at.get(path, set())
        tables = _table_literals(text, rows)
        variable_tols: list[str] = []
        for m in _APPROX.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            if line not in rows:
                continue
            args = [a.strip() for a in m.group("args").split(",")]
            tol = next((a.split("=", 1)[1].strip() for a in args if re.match(r"r(?:el|tol)\s*=", a)), None)
            tol = consts.get(tol, tol)
            if tol is None or not re.fullmatch(_FLOAT, tol):
                continue
            values = [consts.get(a, a) for a in args if "=" not in a]
            # A value read from a variable came from somewhere: in a test, most often a
            # parametrize table, as #1502's run_network values did. Which call reads which
            # column is not known, so a table is held to the loosest such tolerance.
            if any(not re.fullmatch(_FLOAT, v) and re.fullmatch(r"[a-z_][\w.\[\]]*", v) for v in values):
                variable_tols.append(tol)
            hits += _rounded(path, line, values, tol)
        if tables and variable_tols:
            loosest = max(variable_tols, key=float)
            hits += _rounded(path, "table", tables, loosest)
    record("WARN" if hits else "PASS", "referee constants hold their tolerance", "; ".join(hits[:4]) or "none",
           "#1502: write the referee's full-precision value, as it prints it")


# ─── the 2026-11-18 cloud reviews' lessons (#1751) ───────────────────────────

_ASSERTED = re.compile(
    r"""(?:match\s*=\s*r?|assert\s+r?)(["'])(?P<text>[^"'\n]{12,}?)\1(?:\s+in\b|\s*[,)])""")
_SOURCE_SUFFIXES = (".cpp", ".hpp", ".h", ".cc", ".py")
# A piece filled in at run time, just before or just after a literal: C++ `<< x <<` or
# `+ x +`, a Python f-string's `{x}`.
_FILLED_BEFORE = (r'(?:<<|\+)\s*(?P<what>[A-Za-z_][\w.\->()\[\]]*)\s*(?:<<|\+)\s*"\s*$', r"\{(?P<what>[^{}]+)\}\s*$")
_FILLED_AFTER = (r'^\s*"\s*(?:<<|\+)\s*(?P<what>[A-Za-z_][\w.\->()\[\]]*)', r"^\s*\{(?P<what>[^{}]+)\}")


def _library_lines(b: Branch):
    for d in ("src/", "include/", "python/bngsim/"):
        root = b.repo / d
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                if f.suffix in _SOURCE_SUFFIXES and f.is_file() and "third_party" not in f.parts:
                    rel = str(f.relative_to(b.repo))
                    for n, ln in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        yield rel, n, ln


def _filled_piece(asked: str, lines: list[tuple[str, int, str]]) -> str | None:
    """Where the asserted text runs past a literal of the code into a piece filled in at run
    time, as `The RHS returned a non-finite value` does: the code writes `<< w.source << "
    returned a non-finite value"`, and w.source is "RHS" or "compiled RHS". The place, or None."""
    words = asked.split()
    for k in range(len(words) - 1, 2, -1):  # the longest run of 3+ words, at either end
        for run, before in ((" ".join(words[-k:]), True), (" ".join(words[:k]), False)):
            for rel, n, ln in lines:
                at = ln.find(run)
                if at < 0:
                    continue
                side = ln[:at] if before else ln[at + len(run):]
                for pat in (_FILLED_BEFORE if before else _FILLED_AFTER):
                    m = re.search(pat, side)
                    if m:
                        rest = " ".join(words[:-k] if before else words[k:])
                        other = _longer_literal(rest, lines)
                        if other:
                            return (f'"{rest}" is {m.group("what").strip()} at {rel}:{n}, '
                                    f'which can also be "{other}"')
    return None


def _longer_literal(rest: str, lines: list[tuple[str, int, str]]) -> str | None:
    """A literal of the code that is `rest` with one to three words added, as "The compiled
    RHS" is "The RHS": the filled-in piece can say more on another route. A test that picks
    its input picks which of two unrelated phrases appears, and is not flagged."""
    want = rest.split()
    if not want:
        return None
    for _, _, ln in lines:
        for lit in re.findall(r'"([^"\n]{1,80})"', ln):
            have = lit.split()
            if not 0 < len(have) - len(want) <= 3:
                continue
            it = iter(have)
            if all(w in it for w in want) and have[0] == want[0] and have[-1] == want[-1]:
                return lit
    return None


def check_message_variants(b: Branch):
    """A new test asks for words of a message the code fills in at run time (#1751): it asked
    for "The RHS returned a non-finite value", where the code writes `<< w.source << "
    returned ..."`, and a compiled run's w.source is "compiled RHS". It passed on the
    interpreted route and failed under forced cc and forced MIR; CI's mir leg does not run
    that file, so CI would not have shown it."""
    paths = sorted({p for p, _ in b.added_in(("python/tests/",), (".py",))})
    if not paths:
        record("PASS", "asserted messages hold on every route", "no test added")
        return
    lines = [(rel, n, ln) for rel, n, ln in _library_lines(b) if '"' in ln or "{" in ln]
    # The library as one text, with adjacent literals joined ("... the " / "saved state
    # ..." in Python, "a " "b" in C++): a message the code writes whole is never flagged.
    whole = re.sub(r"""["']\s*\n\s*[rRbBfF]{0,2}["']""", "", "\n".join(ln for _, _, ln in _library_lines(b)))
    hits = []
    for path in paths:
        p, rows = b.repo / path, b.added_at.get(path, set())
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _ASSERTED.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            asked = m.group("text")
            if m.group(0).startswith("match"):
                # A pattern: only the text before its first special character is literal.
                # `The (compiled )?RHS ...` is cut to "The" and passes; `... at t=\d` keeps
                # the "The RHS" it still asks for.
                asked = re.split(r"[\\()\[\]{}.*+?|^$]", asked, maxsplit=1)[0]
            if line not in rows or asked in whole:
                continue
            where = _filled_piece(asked, lines)
            if where:
                hits.append(f"{path}:{line}: {where}")
    record("WARN" if hits else "PASS", "asserted messages hold on every route", "; ".join(hits[:3]) or "none run into a filled-in piece",
           "#1751: ask for what every route says (a pattern such as 'The (compiled )?RHS returned'), "
           "and run the file under BNGSIM_CODEGEN_THRESHOLD=1 and BNGSIM_CODEGEN_JIT=mir")


def check_skips(b: Branch, python: str):
    """New pytest.skip reasons must be declared (#1653, test_skip_audit)."""
    new = [ln for p, ln in b.added_in(("python/tests/",), (".py",)) if "pytest.skip(" in ln or "skipif(" in ln]
    if not new:
        record("SKIP", "skip reasons declared", "no new skips")
        return
    r = sh([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "python/tests/test_skip_audit.py"],
           b.repo, timeout=900)
    ok = r.returncode == 0
    record("PASS" if ok else "FAIL", "skip reasons declared",
           f"{len(new)} new skip line(s); test_skip_audit {'passes' if ok else 'fails: ' + r.stdout.strip()[-200:]}",
           "#1653; conftest _DECLARED_SKIPS")


def check_strict_xfail(b: Branch, issue: str | None):
    """A strict xfail citing the issue this PR fixes would XPASS on main (#841, #1350)."""
    if not issue:
        record("SKIP", "no stale xfail for the issue", "no --issue")
        return
    r = sh(["git", "grep", "-n", "-E", rf"xfail.*(#|issue[ _]?){issue}\b|(#|issue[ _]?){issue}\b.*xfail", "--",
            "python/tests", "tests"], b.repo)
    hits = [ln for ln in r.stdout.splitlines() if ln.strip()]
    record("FAIL" if hits else "PASS", "no stale xfail for the issue", "; ".join(hits[:3]) or f"none cite #{issue}",
           "#841, #1350: retire the issue's strict xfail")


def check_overclaims(b: Branch, body: str | None):
    """The most frequent finding (~40 PRs): a claim that is false at the head."""
    texts = [(p, ln) for p, ln in b.added if p.startswith("changelog.d/") or p.startswith("docs/")]
    for path in sorted({p for p, _ in b.added if p.startswith(("python/bngsim/", "python/tests/")) and p.endswith(".py")}):
        texts += [(path, ln) for ln in b.prose_added(path)]
    texts += [(p, ln) for p, ln in b.added_in(("src/", "include/")) if re.match(r"\s*(//|\*|/\*)", ln) or '"' in ln]
    flagged = [(p, ln.strip()) for p, ln in texts if OVERCLAIM.search(ln)]
    if body:
        flagged += [("PR body", ln.strip()) for ln in body.splitlines() if OVERCLAIM.search(ln)]
    nums = [(p, ln.strip()) for p, ln in b.added if p.startswith("changelog.d/") and re.search(r"\d", ln)]
    detail = f"{len(flagged)} lines with every/all/never/unchanged/identical/only…; {len(nums)} changelog lines with numbers"
    record("WARN" if flagged else "PASS", "claims to recount", detail + " — listed under MANUAL", "class: false or stale claims (~40 PRs)")
    return flagged


# Reach and comparison claims: the shape of every scope claim a review found
# false on 2026-11-19 (#1721, #1763, #1751, #1753), and that OVERCLAIM's words
# missed: "wherever it ran on main", "I found no run ... that returns", "each
# alone is covered by the others", "as on main", "nothing a caller can reach";
# and (#1767) which way an approximation errs.
SCOPE_CLAIM = re.compile(
    r"\bwherever\b|\bas (?:it (?:did|ran) )?on main\b|\b(?:same|alike) as (?:on )?main\b|\bas before\b"
    r"|\b[Ii] found no\b|\bno (?:run|model|case|shape|caller|path|route)s? (?:\w+ ){0,3}(?:found|reach(?:es)?|returns?)\b"
    r"|\bcovered by the others\b|\bcan(?:not|'t) (?:be )?reach|\bunreachable\b|\bnothing (?:\w+ ){0,6}(?:reach|reads|uses)\b"
    r"|\bonly (?:\w+ ){0,6}reach(?:es)?\b"
    # Which way an approximation errs ("can give a warning that is not needed,
    # not miss one": #1767, where a review found the one it missed).
    r"|\b(?:not|never|cannot|can't|won't|doesn't|does not) miss\w*\b|\bmiss(?:es)? none\b"
    r"|\bno \w+ (?:is |are )?(?:ever )?missed\b|\berrs? (?:only )?on the (?:safe )?side\b|\bon the safe side\b")
# A surviving mutant explained away: "survives ... because / since / only / nothing ...".
SURVIVOR_EXCUSE = re.compile(r"\bsurviv\w*\b.*\b(?:because|since|so|only|nothing|cannot|can't)\b", re.I)


def check_scope_claims(b: Branch, body: str | None):
    """A sentence that says what a change reaches, or that it matches main, with
    no run behind it (2026-11-19: all four reviews found one). Each line listed
    wants the run that would show otherwise, named beside it, or to be cut."""
    texts = [(p, ln) for p, ln in b.added if p.startswith(("changelog.d/", "docs/"))]
    if body:
        texts += [("PR body", ln) for ln in body.splitlines()]
    hits = [(p, ln.strip()) for p, ln in texts if SCOPE_CLAIM.search(ln) or SURVIVOR_EXCUSE.search(ln)]
    shown = "; ".join(f"{p}: {ln[:90]}" for p, ln in hits[:4]) + (f"; and {len(hits) - 4} more" if len(hits) > 4 else "")
    record("WARN" if hits else "PASS", "scope claims backed by a run", shown or "none",
           "#1721 'only a continued trajectory', #1763 'runs wherever it ran on main', #1751 'no joint shape "
           "returns', #1753 'each alone is covered by the others': name the run (case and numbers) beside each, "
           "or cut it; re-measure a reviewer's 'as on main' before repeating it")
    return hits


def check_scratch_names(b: Branch):
    """Never a stdlib name for a scratch script (#bisect.py fork bomb)."""
    bad = [f for f in b.untracked if f.endswith(".py") and Path(f).stem in STDLIB_NAMES]
    record("FAIL" if bad else "PASS", "no stdlib-named scripts", ", ".join(bad) or "none", "scratch-script-names")


def check_bngpath():
    v = os.environ.get("BNGPATH")
    if v is None:
        record("WARN", "absolute BNGPATH", "BNGPATH unset: ~45 BNG2 tests will skip", "absolute-bngpath-for-suite")
    elif not os.path.isabs(v):
        record("FAIL", "absolute BNGPATH", f"{v} is relative: ~27 tests that chdir fail", "absolute-bngpath-for-suite")
    else:
        record("PASS", "absolute BNGPATH", v)


def check_clang_format_tests(b: Branch):
    """The pre-commit hook skips tests/ (#1535)."""
    cpp = [f for f in b.files if f.startswith("tests/") and f.endswith((".cpp", ".hpp", ".h"))]
    if not cpp:
        record("SKIP", "clang-format tests/", "no C++ under tests/")
        return
    r = sh(["clang-format", "--dry-run", "--Werror", *cpp], b.repo)
    record("PASS" if r.returncode == 0 else "FAIL", "clang-format tests/", r.stderr.strip()[:200] or "clean", "#1535")


def check_inflight(b: Branch):
    """Check in-flight PRs on the same files, and say the merge order."""
    r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--limit", "100", "--json",
            "number,title,files,headRefName"], b.repo, timeout=120)
    if r.returncode:
        record("SKIP", "in-flight PRs on the same files", "gh unavailable")
        return
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    lib = set(f for f in b.files if f.startswith(LIBRARY) or f.startswith("docs/"))
    over = []
    for pr in json.loads(r.stdout):
        if pr["headRefName"] == mine:
            continue
        shared = lib & {f["path"] for f in pr.get("files", [])}
        if shared:
            over.append(f"#{pr['number']} ({', '.join(sorted(shared)[:3])})")
    record("WARN" if over else "PASS", "in-flight PRs on the same files", "; ".join(over) or "none",
           "say the merge order in the body (#1105/#1154)")


def ci_paths(repo: Path):
    """{workflow: (path globs, test files it runs)} for the filtered legs."""
    out = {}
    for wf in sorted((repo / ".github/workflows").glob("*.yml")):
        text = wf.read_text()
        on = text.split("\njobs:")[0]
        globs = re.findall(r"^\s+-\s+[\"']?([^\"'\s#]+)[\"']?", on.split("paths:", 1)[1], re.M) if "paths:" in on else []
        runs = set(re.findall(r"(python/tests/test_[\w*]+\.py)", text.split("\njobs:", 1)[-1]))
        if globs:
            out[wf.name] = (globs, runs)
    return out


def _glob_match(path, glob):
    rx = "^" + re.escape(glob).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
    return re.match(rx, path) is not None


def check_ci_legs(b: Branch):
    """Path filters decide which legs run; fixed lists miss new tests (#1629, Ely)."""
    legs = ci_paths(b.repo)
    new_tests = [f for f in b.files if f.startswith("python/tests/test_") and f.endswith(".py")]
    rows, missing = [], []
    for wf, (globs, runs) in legs.items():
        fires = [f for f in b.files if any(_glob_match(f, g) for g in globs if not g.startswith("!"))]
        if not fires:
            continue
        if not runs:
            rows.append(f"{wf}: fires ({fires[0]}{'…' if len(fires) > 1 else ''}); names no python test files (not a pytest list)")
            continue
        covered = [t for t in new_tests if any(_glob_match(t, r) for r in runs)]
        rows.append(f"{wf}: fires ({fires[0]}{'…' if len(fires) > 1 else ''}); runs {len(covered)}/{len(new_tests)} new test files")
        if len(covered) < len(new_tests):
            missing.append(wf)
    detail = "; ".join(rows) or "no filtered leg fires (python-tests runs everything)"
    record("WARN" if missing else "PASS", "CI legs table", detail + (f" — not covered on {', '.join(missing)}: dispatch windows-tail with pytest_args" if missing else ""),
           "ci-path-filters-decide-legs")


def check_counts(b: Branch, python: str):
    """Collected count against the floor and its slack (ci-test-count-bounds)."""
    env = dict(line.split("=", 1) for line in (b.repo / "ci/test_floors.env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    floor = int(env["PYTEST_COLLECTED_FLOOR"])
    slack = int(env["PYTEST_COLLECTED_MAX_SLACK"])
    r = sh([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "python/tests"],
           b.repo, timeout=1200)
    m = re.search(r"(\d+) tests? collected", r.stdout) or re.search(r"^(\d+) tests?", r.stdout, re.M)
    if not m:
        record("SKIP", "collected count in bounds", "could not collect: " + r.stdout.strip()[-150:])
        return
    n = int(m.group(1))
    if n < floor:
        record("FAIL", "collected count in bounds", f"{n} < floor {floor}", "ci-test-count-bounds")
    elif n - floor > slack:
        record("FAIL", "collected count in bounds", f"{n} is {n - floor} above floor {floor} (slack {slack}): floor stale, bump ci/test_floors.env",
               "#1539 went red on this")
    else:
        record("PASS" if n - floor < slack - 25 else "WARN", "collected count in bounds",
               f"{n} ({n - floor} above floor {floor}, slack {slack}; local count includes corpus-gated cases)", "ci-test-count-bounds")


def check_fail_on_main(b: Branch, main_pkg: str, boot: str):
    """Every new test file must fail on main somewhere (class: tests that cannot fail, 13+ PRs)."""
    new = [f for f in b.files if f.startswith("python/tests/test_") and f.endswith(".py")]
    if not new:
        record("SKIP", "new tests fail on main", "no test files changed")
        return
    if not boot:
        record("FAIL", "new tests fail on main", "--main-pkg needs --boot (or $BNGSIM_AB_BOOT): "
               "the dir with the sitecustomize that points pytest's workers at main's package")
        return
    env = dict(os.environ, BNGSIM_AB_PKG=main_pkg, PYTHONPATH=boot)
    rows, bad = [], []
    for f in new:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "4", f],
                           cwd=b.repo, capture_output=True, text=True, env=env, timeout=1800)
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        failed = re.search(r"(\d+) failed", tail)
        rows.append(f"{Path(f).name}: {tail.strip('= ')}")
        if not failed:
            bad.append(Path(f).name)
    record("FAIL" if bad else "PASS", "new tests fail on main", "; ".join(rows) + (f" — nothing fails on main in {', '.join(bad)}" if bad else ""),
           "revert the fix and watch each test fail (class 3, under-weighted)")


def check_body(body: str, issue: str | None):
    """PR body format (pr-body-diff-block, pr-body-verification-steps, pr-body-color-no-emoji)."""
    problems = []
    if issue and not re.search(rf"(Fixes|Closes|Refs) #{issue}\b", body):
        problems.append(f"no 'Fixes #{issue}'")
    if "## The diff (abridged)" not in body or "```diff" not in body:
        problems.append("no '## The diff (abridged)' with ```diff blocks")
    if not re.search(r"## How this was checked\s*\n+\s*1\.", body):
        problems.append("no numbered '## How this was checked'")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿✅❌⚠]", body):
        problems.append("emoji")
    other = re.findall(r"> \[!(NOTE|TIP|WARNING|CAUTION)\]", body)
    if other:
        problems.append(f"callouts other than IMPORTANT: {set(other)}")
    if "\\color" in body or "$\\textsf" in body:
        problems.append("math-mode colour labels (use bold)")
    if "Docker" not in body and "Linux" not in body:
        problems.append("no Linux/Docker step")
    if not re.search(r"mutant", body, re.I):
        problems.append("no mutant step")
    record("FAIL" if problems else "PASS", "PR body format", "; ".join(problems) or "diff blocks, numbered checks, no emoji, purple only",
           "pr-body-* memories")


# ─── checks added 2026-11-14 (what that day's five PRs ran into) ────────────

FENCE = re.compile(r"```.*?```", re.S)
INLINE = re.compile(r"`[^`\n]*`")


def _prose(body: str) -> str:
    """The body without fenced blocks and inline code: where a placeholder is a mistake."""
    return INLINE.sub("", FENCE.sub("", body))


def check_placeholders(body: str):
    """An unfilled marker left in the body (#1681's LINUX_LINE / PERF_LINE, caught by hand)."""
    prose = _prose(body)
    hits = re.findall(r"\b[A-Z][A-Z0-9]*_(?:LINE|HERE|TBD)\b|\bTODO\b|\bTBD\b|\bXXX\b|\bFIXME\b"
                      r"|<(?:name|handle|model|effort|N|SHAs?)[^>\n]*>|Fixes #\s*$", prose, re.M)
    record("FAIL" if hits else "PASS", "no placeholders in the body",
           ", ".join(sorted(set(hits))[:6]) or "none outside code", "fill every marker before the gate passes")


REVIEW_FORMS = (r"\*\*By a person:\*\*", r"\*\*By an AI model:\*\*", r"\*\*Not reviewed\.\*\*")


def check_template(body: str, title: str | None):
    """The PR template since #1746: title shape, ## Review in one form, ## Testing."""
    problems, warns = [], []
    m = re.search(r"^## Review\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if not m:
        problems.append("no '## Review' section (CONTRIBUTING, since #1746)")
    else:
        forms = [f for f in REVIEW_FORMS if re.search(f, m.group(1))]
        # The owner's form: a review round reported without the template's label
        # (no model named): the round, where it ran, and what each finding became.
        if not forms and re.search(r"rounds? of a whole-branch `?/code-review`?", m.group(1)) \
                and re.search(r"\b[0-9a-f]{8}\b", m.group(1)):
            forms = ["reported round"]
        if len(forms) != 1:
            problems.append(f"'## Review' must keep exactly one of the three forms (has {len(forms)})")
        elif "AI model" in forms[0]:
            problems.append("'By an AI model' names the model, which the attribution rule forbids: ask the user")
    if not re.search(r"^## Test(s|ing)\b", body, re.M):
        warns.append("no '## Testing' heading (the template's name for it)")
    if title is None:
        warns.append("title not checked (pass --title, or --pr)")
    elif not re.match(r"^[a-z][\w ./,+-]*: \S.*\((?:refs )?#\d+(?:, #\d+)*\)$", title):
        problems.append(f"title is not '<part>: <what changes> (#issue)': {title[:70]!r}")
    status = "FAIL" if problems else ("WARN" if warns else "PASS")
    record(status, "PR template (title, Review, Testing)", "; ".join(problems + warns) or "title, Review form and Testing as the template asks",
           "pr-template-review-section")


def check_cited_main(body: str, base_sha: str):
    """'main at <sha>' claims older than the base: rerun, or say they are history (Main moved)."""
    cited = set(re.findall(r"\bmain (?:at )?`([0-9a-f]{7,40})`", body))
    stale = sorted(c for c in cited if not base_sha.startswith(c))
    if not cited:
        record("WARN", "claims cite the current main", "the body names no 'main at `sha`'", "say which main each number is from")
    else:
        record("WARN" if stale else "PASS", "claims cite the current main",
               (f"main is {base_sha[:8]}; the body also cites {', '.join(stale)}: rerun those, or say they were before a rebase"
                if stale else f"every 'main at' is {base_sha[:8]}"), "Main moved (MANUAL)")


def _main_ci_count(repo: Path, base_sha: str):
    """(count, sha it was measured at) from main's python-tests count job, newest success."""
    r = sh(["gh", "run", "list", "-R", "lanl/bngsim", "--branch", "main", "--workflow", "python-tests.yml",
            "--limit", "15", "--json", "databaseId,headSha,conclusion,status"], repo, timeout=120)
    if r.returncode:
        return None, None
    runs = [x for x in json.loads(r.stdout) if x["conclusion"] == "success"]
    runs.sort(key=lambda x: x["headSha"] != base_sha)  # the base's own run first, if it finished
    for run in runs[:3]:
        jobs = sh(["gh", "run", "view", str(run["databaseId"]), "-R", "lanl/bngsim", "--json", "jobs"], repo, timeout=120)
        if jobs.returncode:
            continue
        for job in json.loads(jobs.stdout)["jobs"]:
            if "counts" in job["name"] and "ubuntu-latest" in job["name"]:
                log = sh(["gh", "run", "view", "-R", "lanl/bngsim", "--job", str(job["databaseId"]), "--log"], repo, timeout=180)
                m = re.search(r"PYTEST_COLLECTED: (\d+) \(floor", log.stdout)
                if m:
                    return int(m.group(1)), run["headSha"]
    return None, None


def _collected(repo: Path, files: list[str], python: str) -> int:
    if not files:
        return 0
    r = sh([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files], repo, timeout=600)
    m = re.search(r"(\d+) tests? collected", r.stdout)
    return int(m.group(1)) if m else 0


def check_floor_arithmetic(b: Branch, python: str):
    """What the floor line should read: main's CI count + this branch's new tests - 10.

    #1721 conflicted twice in a day on this line; every bump was worked out by hand."""
    floors = b.repo / "ci/test_floors.env"
    if not floors.is_file():
        record("SKIP", "collected floor arithmetic", "no ci/test_floors.env")
        return
    env = dict(ln.split("=", 1) for ln in floors.read_text().splitlines() if "=" in ln and not ln.startswith("#"))
    floor, slack = int(env["PYTEST_COLLECTED_FLOOR"]), int(env["PYTEST_COLLECTED_MAX_SLACK"])
    base_sha = sh(["git", "rev-parse", b.base_ref], b.repo).stdout.strip()
    main_count, at = _main_ci_count(b.repo, base_sha)
    if main_count is None:
        record("SKIP", "collected floor arithmetic", "main's CI count unavailable (gh)")
        return
    tests = [f for f in b.files if f.startswith("python/tests/") and Path(f).name.startswith("test_") and f.endswith(".py")]
    head_n = _collected(b.repo, [f for f in tests if (b.repo / f).is_file()], python)
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "base"
        sh(["git", "worktree", "add", "-q", "--detach", str(wt), b.merge_base], b.repo, check=True)
        try:
            base_n = _collected(wt, [f for f in tests if (wt / f).is_file()], python)
        finally:
            sh(["git", "worktree", "remove", "--force", str(wt)], b.repo)
    delta = head_n - base_n
    STATE["test_delta"] = delta
    expected = main_count + delta
    stale_note = "" if at == base_sha else f" (main's newest finished count, at {at[:8]}, not {base_sha[:8]})"
    detail = f"main CI {main_count}{stale_note} {'+' if delta >= 0 else ''}{delta} here = {expected}; floor line {floor}"
    others = []
    r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--limit", "100", "--json", "number,files,headRefName"],
           b.repo, timeout=120)
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    if not r.returncode:
        others = [f"#{p['number']}" for p in json.loads(r.stdout)
                  if p["headRefName"] != mine and any(f["path"] == "ci/test_floors.env" for f in p.get("files", []))]
    if others:
        detail += f"; open PRs also changing the floor line: {', '.join(others)} (whichever merges second conflicts)"
    if expected < floor:
        record("FAIL", "collected floor arithmetic", detail + f": {expected} is under the floor", "ci-test-count-bounds")
    elif expected - floor > slack:
        record("FAIL", "collected floor arithmetic", detail + f": {expected - floor} above, past the slack {slack}; "
               f"set PYTEST_COLLECTED_FLOOR={expected - 10} and add this PR's clause", "ci-test-count-bounds")
    else:
        record("WARN" if others or slack - (expected - floor) < 25 else "PASS", "collected floor arithmetic",
               detail + f": {expected - floor} above, {slack - (expected - floor)} inside the slack", "ci-test-count-bounds")


def check_thread(b: Branch, pr: str | None, acknowledged: bool):
    """Comments and reviews since this PR's last pushed commit (re-read before pushing)."""
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    if pr is None:
        r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--head", mine, "--json", "number"], b.repo, timeout=60)
        found = json.loads(r.stdout) if not r.returncode and r.stdout.strip() else []
        if not found:
            record("SKIP", "thread read since the last push", "no open PR for this branch yet")
            return
        pr = str(found[0]["number"])
    v = sh(["gh", "pr", "view", pr, "-R", "lanl/bngsim", "--json", "commits,author"], b.repo, timeout=60)
    if v.returncode:
        record("SKIP", "thread read since the last push", f"#{pr}: gh unavailable")
        return
    info = json.loads(v.stdout)
    me = info["author"]["login"]
    since = info["commits"][-1]["committedDate"] if info["commits"] else ""
    new = []
    for api, key in ((f"repos/lanl/bngsim/issues/{pr}/comments", "created_at"),
                     (f"repos/lanl/bngsim/pulls/{pr}/comments", "created_at"),
                     (f"repos/lanl/bngsim/pulls/{pr}/reviews", "submitted_at")):
        r = sh(["gh", "api", "--paginate", api], b.repo, timeout=60)
        if r.returncode:
            continue
        for c in json.loads(r.stdout or "[]"):
            if c["user"]["login"] != me and (c.get(key) or "") > since:
                first = (c.get("body") or c.get("state") or "").strip().splitlines()
                new.append(f"{c['user']['login']} {c.get(key, '')[:16]}: {(first[0] if first else '')[:70]}")
    if new and not acknowledged:
        record("FAIL", "thread read since the last push", f"#{pr}: {len(new)} new since {since[:16]}: " + " | ".join(new[:3])
               + " — read them, then rerun with --thread-read", "re-read a PR's thread before pushing (#1720's revert)")
    else:
        record("PASS", "thread read since the last push",
               f"#{pr}: {len(new)} new since the last push" + (", read (--thread-read)" if new else ""), "")


def check_build(b: Branch, python: str):
    """The installed core is built from this tree: tests and timing on a stale binary say nothing."""
    code = ("import bngsim._build_provenance as p; g = p.gather(); "
            "print(g.is_stale, g.is_source_checkout, g.build_commit or '-', g.source_root or '-')")
    r = sh([python, "-c", code], b.repo, timeout=120)
    words = r.stdout.split()
    if r.returncode or len(words) != 4:
        record("SKIP", "built from this tree", (r.stderr.strip().splitlines() or ["no bngsim in that python"])[-1][:100])
        return
    stale, checkout, built, root = words
    if checkout != "True" or Path(root).resolve() != b.repo:
        record("WARN", "built from this tree", f"{python} imports bngsim from {root}, not this tree", "use the worktree's own .venv")
    elif stale == "True":
        record("FAIL", "built from this tree", f"core built at {built}; C++ in this tree is newer: "
               "uv sync --frozen --extra dev --reinstall-package bngsim", "a plain uv sync does not rebuild")
    else:
        # Not stale is judged on the sources' bytes (digest) or mtimes, so a build of a
        # dirty tree that was then committed is current.
        record("PASS", "built from this tree", f"core current with this tree's C++ (built at {built})", "")


def stamp_path(repo: Path) -> Path:
    gitdir = Path(sh(["git", "rev-parse", "--git-dir"], repo).stdout.strip())
    return (gitdir if gitdir.is_absolute() else repo / gitdir) / "prepr_stamp.json"


def fingerprint(repo: Path, body: str | None) -> dict:
    head = sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    dirty = sh(["git", "status", "--porcelain", "--untracked-files=no"], repo).stdout
    return {"head": head, "tree_dirty": hashlib.sha256(dirty.encode()).hexdigest(),
            "body": hashlib.sha256((body or "").encode()).hexdigest()}


# ─── more from experience (2026-11-14, second pass) ─────────────────────────


def check_body_names_overlaps(b: Branch, body: str):
    """The body names each open PR on the same files, and each leg that skips the new tests."""
    missing = []
    for status, name, detail, _ in RESULTS:
        if name == "in-flight PRs on the same files" and status == "WARN":
            for n in re.findall(r"#(\d+) \(", detail):
                if f"#{n}" not in body:
                    missing.append(f"#{n} (shares files; say the merge order)")
        if name == "CI legs table" and "not covered on" in detail:
            if not re.search(r"\b(MIR|Windows|MSVC)\b", body):
                missing.append("the legs that do not run the new tests")
    record("FAIL" if missing else "PASS", "body names overlaps and uncovered legs",
           "; ".join(missing) or "every overlapping PR and uncovered leg is named", "say the merge order (#1105/#1154); ci-path-filters-decide-legs")


def check_new_test_hygiene(b: Branch):
    """New tests: Antimony-gated models skip without the extra (#1720 note 9); a subprocess
    with no timeout can hang a CI leg; a literal /dev/null in C++ breaks Windows (null_device)."""
    probs = []
    for p, ln in b.added_in(prefix=("python/tests/",), suffix=(".py",)):
        if 'importorskip("antimony")' in ln or "from_antimony_string" in ln:
            probs.append(f"{Path(p).name}: Antimony model (write it as .net so it runs without the extra)")
            break
    for p in {p for p, _ in b.added_in(prefix=("python/tests/",), suffix=(".py",))}:
        text = (b.repo / p).read_text(errors="replace") if (b.repo / p).is_file() else ""
        for m in re.finditer(r"subprocess\.(run|check_output|check_call)\((.*?)\)\n", text, re.S):
            if "timeout" not in m.group(2):
                probs.append(f"{Path(p).name}: subprocess.{m.group(1)} with no timeout")
                break
    for p, ln in b.added_in(prefix=("src/", "include/")):
        if '"/dev/null"' in ln or '"NUL"' in ln:
            probs.append(f"{p}: a literal null device (use bngsim::null_device)")
            break
    record("WARN" if probs else "PASS", "new-test and portability hygiene", "; ".join(dict.fromkeys(probs)) or "none",
           "lessons from #1720, #1680")


# ─── the 2026-11-15 review's lessons (all 18 open PRs, 161 findings) ─────────


def _pr_for(b: Branch, pr: str | None) -> str | None:
    """*pr*, or the open PR whose head is this branch."""
    if pr is not None:
        return pr
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--head", mine, "--json", "number"],
           b.repo, timeout=60)
    found = json.loads(r.stdout) if not r.returncode and r.stdout.strip() else []
    return str(found[0]["number"]) if found else None


def check_remote_branch(b: Branch, pr: str | None):
    """The PR's head holds no commit this branch lacks: someone (a maintainer, a
    reviewer) pushed to it, and a push from here would drop their work or be
    refused. #1772: Ely pushed 807e8a7c to the branch; only git's refusal saved it."""
    pr = _pr_for(b, pr)
    if pr is None:
        record("SKIP", "no one else pushed to the PR", "no open PR for this branch yet")
        return
    v = sh(["gh", "pr", "view", pr, "-R", "lanl/bngsim", "--json",
            "headRefOid,headRefName,headRepository,headRepositoryOwner"], b.repo, timeout=60)
    if v.returncode:
        record("SKIP", "no one else pushed to the PR", f"#{pr}: gh unavailable")
        return
    info = json.loads(v.stdout)
    head, ref = info["headRefOid"], info["headRefName"]
    url = f"https://github.com/{info['headRepositoryOwner']['login']}/{info['headRepository']['name']}.git"
    if sh(["git", "cat-file", "-e", f"{head}^{{commit}}"], b.repo).returncode:
        sh(["git", "fetch", "-q", url, ref], b.repo, timeout=300)
    if sh(["git", "cat-file", "-e", f"{head}^{{commit}}"], b.repo).returncode:
        record("WARN", "no one else pushed to the PR", f"#{pr}: could not fetch its head {head[:8]}", "#1772")
        return
    if sh(["git", "merge-base", "--is-ancestor", head, "HEAD"], b.repo).returncode == 0:
        record("PASS", "no one else pushed to the PR", f"#{pr}'s head {head[:8]} is in this branch")
        return
    theirs = sh(["git", "log", "--format=%h %an: %s", f"HEAD..{head}"], b.repo).stdout.strip().splitlines()
    record("FAIL", "no one else pushed to the PR",
           f"#{pr} has {len(theirs)} commit(s) this branch lacks: {'; '.join(theirs[:3])}: merge them, never force",
           "#1772 (Ely's 807e8a7c)")


def _functions(text: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) of each function in *text*."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [(n.name, n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def check_untouched_readers(b: Branch):
    """Each function the branch changes, and its callers in the package that the
    branch leaves untouched. #1763: the Hill fold went into one reader of the root
    set, and run_diffrax, steady_state, sensitivity runs, the stop budget and the
    kink check kept reading the old one: six regressions. Run each on the issue's
    model, branch against main. It finds callers of changed code only: a function
    whose code is the same but whose answer changed (#1763's root set, changed by
    the loader) needs its readers found by hand."""
    pkg = [f for f in b.files if f.startswith("python/bngsim/") and f.endswith(".py")]
    changed = set()
    for f in pkg:
        p = b.repo / f
        rows = b.added_at.get(f, set())
        if not p.is_file() or not rows:
            continue
        for name, lo, hi in _functions(p.read_text(errors="replace")):
            if not name.startswith("__") and any(lo <= r <= hi for r in rows):
                changed.add(name)
    if not changed:
        record("SKIP", "untouched readers of changed functions", "no package function changed")
        return
    touched = {f: b.added_at.get(f, set()) for f in pkg}
    readers: dict[str, set[str]] = {}
    for p in sorted((b.repo / "python/bngsim").rglob("*.py")):
        rel = str(p.relative_to(b.repo))
        text = p.read_text(errors="replace")
        hits = [n for n in changed if re.search(rf"\b{re.escape(n)}\(", text)]
        if not hits:
            continue
        funcs = _functions(text)
        for i, line in enumerate(text.splitlines(), 1):
            for n in hits:
                if not re.search(rf"\b{re.escape(n)}\(", line) or re.match(rf"\s*(async\s+)?def {re.escape(n)}\(", line):
                    continue
                owner = [(lo, hi, fn) for fn, lo, hi in funcs if lo <= i <= hi]
                lo, hi, fn = max(owner) if owner else (i, i, "<module>")
                if any(lo <= r <= hi for r in touched.get(rel, ())):
                    continue  # the branch changed this reader too
                readers.setdefault(n, set()).add(f"{rel.removeprefix('python/bngsim/')}:{fn}")
    if not readers:
        record("PASS", "untouched readers of changed functions",
               f"{len(changed)} changed; every caller is in code the branch touched")
        return
    rows = [f"{n} <- {', '.join(sorted(r)[:4])}{' +' + str(len(r) - 4) if len(r) > 4 else ''}"
            for n, r in sorted(readers.items())]
    record("WARN", "untouched readers of changed functions",
           f"{sum(len(r) for r in readers.values())} untouched caller(s): " + "; ".join(rows[:6]),
           "#1763 (one reader fixed, five not)")


_PY311 = [
    (r"\blocale\.getencoding\b", "locale.getencoding (3.11; getpreferredencoding(False))"),
    (r"^\s*(import tomllib|from tomllib\b)", "tomllib (3.11)"),
    (r"\bdatetime\.UTC\b|from datetime import [^#\n]*\bUTC\b", "datetime.UTC (3.11)"),
    (r"\bStrEnum\b", "enum.StrEnum (3.11)"),
    (r"\btyping\.Self\b|from typing import [^#\n]*\bSelf\b", "typing.Self (3.11)"),
    (r"^\s*except\s*\*", "except* (3.11)"),
    (r"\bTaskGroup\b", "asyncio.TaskGroup (3.11)"),
    (r"\bExceptionGroup\b", "ExceptionGroup (3.11)"),
    (r"\bmath\.(cbrt|exp2)\(", "math.cbrt / math.exp2 (3.11)"),
    (r"\bhashlib\.file_digest\b", "hashlib.file_digest (3.11)"),
]


def check_py310(b: Branch):
    """No API newer than Python 3.10, which the package supports (requires-python
    >= 3.10, cp310 wheels), unless it sits behind a version check. #1775: a test
    called locale.getencoding(), which 3.10 does not have; every CI leg runs 3.12."""
    bad, guarded = [], []
    for p, ln in b.added_in(prefix=("python/",), suffix=(".py",)):
        for pat, what in _PY311:
            if re.search(pat, ln):
                text = (b.repo / p).read_text(errors="replace") if (b.repo / p).is_file() else ""
                i = text.find(ln.strip())
                near = text[max(0, i - 300):i]
                (guarded if ("version_info" in near or "try:" in near) else bad).append(f"{Path(p).name}: {what}")
    if bad:
        record("FAIL", "nothing newer than Python 3.10", "; ".join(dict.fromkeys(bad)), "#1775 (locale.getencoding)")
    elif guarded:
        record("PASS", "nothing newer than Python 3.10", "behind a version check: " + "; ".join(dict.fromkeys(guarded)))
    else:
        record("PASS", "nothing newer than Python 3.10", "none added")


_ENGINE_MATH = [
    (r"\blambdify\(", "lambdify"),
    (r"\.evalf\(", ".evalf()"),
    (r"\b(np|numpy)\.(rint|round|around|mod|remainder)\(", "numpy rounding/mod"),
    (r"%\s*1\b", "% 1"),
]


def check_engine_math(b: Branch):
    """New numeric evaluation of model math reads it as the engine does. numpy's
    rint rounds a half to even, its mod is floored, sympy's frac is x - floor(x);
    the engine's are floor(x + 0.5), fmod and x - trunc(x). #1800: a seed read
    rint(2.5)*k as 2*k. Each hit: run rint/mod/frac at a half and below zero
    through it, or route it through _engine_rounding."""
    hits = []
    for p, ln in b.added_in(prefix=("python/bngsim/",), suffix=(".py",)):
        code = ln.split("#", 1)[0]
        for pat, what in _ENGINE_MATH:
            if re.search(pat, code):
                text = (b.repo / p).read_text(errors="replace") if (b.repo / p).is_file() else ""
                i = text.find(ln.strip())
                if "_engine_rounding" in text[max(0, i - 400):i + 400] or "_ENGINE_ROUNDING" in text[max(0, i - 400):i + 400]:
                    continue
                hits.append(f"{Path(p).name}: {what}")
    record("WARN" if hits else "PASS", "model math read as the engine reads it",
           "; ".join(dict.fromkeys(hits)) or "no new numeric evaluation", "#1800 (rint, mod, frac)")


def _negative(test: ast.expr) -> bool:
    """An assertion that holds when nothing happened: not x, x not in y, x == [] / () / None.
    Not `== 0`: `returncode == 0` checks that something succeeded."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, right = test.ops[0], test.comparators[0]
        if isinstance(op, (ast.NotIn, ast.IsNot)):
            return isinstance(op, ast.NotIn) or (isinstance(right, ast.Constant) and right.value is not None)
        if isinstance(op, (ast.Eq, ast.Is)):
            empty = (isinstance(right, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and not getattr(right, "elts", getattr(right, "keys", [1]))) \
                or (isinstance(right, ast.Constant) and (right.value is None or right.value is False or right.value == "")) \
                or (isinstance(right, ast.Call) and getattr(right.func, "id", "") in ("set", "list", "dict", "tuple") and not right.args)
            return bool(empty)
    return False


def check_negative_only_tests(b: Branch):
    """A new test whose every assertion is negative passes whether or not the code
    ran: give it a positive control. #1770: a `lossy` filter matched no real warning
    text, so `assert not lossy` could not fail; #1763: a "no such log line" test passed
    with the roots set or not."""
    weak = []
    for f in [f for f in b.files if f.startswith("python/tests/") and f.endswith(".py")]:
        p = b.repo / f
        rows = b.added_at.get(f, set())
        if not p.is_file() or not rows:
            continue
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
                continue
            if not any(fn.lineno <= r <= (fn.end_lineno or fn.lineno) for r in rows):
                continue
            src = ast.unparse(fn)
            if "pytest.raises" in src or "pytest.warns" in src or "pytest.approx" in src:
                continue
            asserts = [n.test for n in ast.walk(fn) if isinstance(n, ast.Assert)]
            if asserts and all(_negative(t) for t in asserts):
                weak.append(f"{Path(f).name}::{fn.name}")
    record("WARN" if weak else "PASS", "every new test can fail",
           ("only negative assertions: " + ", ".join(weak[:6])) if weak else "each has a positive assertion",
           "#1770 (a filter that matched nothing), #1763")


def check_test_bngpath(b: Branch):
    """A new test that runs BNG2.pl in another directory resolves BNGPATH first: a
    relative one passes the skip check from the repo and then fails under cwd=.
    #1768, #1765, #1774: each such test errored, not skipped."""
    bad = []
    for f in {p for p, _ in b.added_in(prefix=("python/tests/",), suffix=(".py",))}:
        p = b.repo / f
        text = p.read_text(errors="replace") if p.is_file() else ""
        if "BNGPATH" in text and re.search(r"subprocess\.\w+\(", text) and "cwd=" in text \
                and not re.search(r"BNGPATH[^\n]*\b(resolve|abspath|absolute)\(", text):
            bad.append(Path(f).name)
    record("WARN" if bad else "PASS", "BNGPATH resolved before a cwd= subprocess",
           ", ".join(bad) or "none needed", "#1768, #1765, #1774")


def check_docs_claims(b: Branch, issue: str | None):
    """Each code name the changelog fragment names, and the files that hold it which
    the branch leaves untouched: the claim must hold there too. #1756: the docs said
    bngsim honours SUNLOGGER_WARNING_FILENAME; register_roots and two steady-state
    sites overrode it."""
    frags = [f for f in b.files if f.startswith("changelog.d/") and f.endswith(".md")
             and (not issue or Path(f).name.startswith(f"{issue}."))]
    if not frags:
        record("SKIP", "changelog claims hold at every site", "no fragment")
        return
    text = " ".join((b.repo / f).read_text(errors="replace") for f in frags if (b.repo / f).is_file())
    names = {m.rstrip("()") for m in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*(?:\(\))?)`", text)}
    names = {n.split(".")[-1] for n in names if len(n.split(".")[-1]) > 3}
    touched = set(b.files)
    sites: dict[str, list[str]] = {}
    roots = [b.repo / "python/bngsim", b.repo / "src", b.repo / "include"]
    files = [p for r in roots if r.is_dir() for p in r.rglob("*") if p.suffix in (".py", ".cpp", ".hpp", ".h")]
    for n in sorted(names):
        where = [str(p.relative_to(b.repo)) for p in files
                 if re.search(rf"\b{re.escape(n)}\b", p.read_text(errors="replace"))]
        if not where or len(where) > 12:  # absent, or a word too common to point anywhere
            continue
        rest = [w for w in where if w not in touched]
        if rest:
            sites[n] = rest
    if not sites:
        record("PASS", "changelog claims hold at every site", f"{len(names)} name(s); no untouched site holds one")
        return
    rows = [f"{n}: {', '.join(Path(w).name for w in ws[:4])}{' +' + str(len(ws) - 4) if len(ws) > 4 else ''}"
            for n, ws in sites.items()]
    record("WARN", "changelog claims hold at every site",
           "read each for the fragment's claim: " + "; ".join(rows[:6]), "#1756 (sibling sites overrode it)")


def check_binding_order(b: Branch):
    """A parse binding added after the model's own names shadows a parameter of
    that name. #1809: rint, mod and frac were bound after
    `local_dict.update(sym_map)`, so a parameter named `frac` became the function
    and `frac*k` no longer parsed, where main had it. Bind before the model's
    names, as round() is (#684)."""
    late = []
    for f in [f for f in b.files if f.startswith("python/bngsim/") and f.endswith(".py")]:
        p = b.repo / f
        rows = b.added_at.get(f, set())
        if not p.is_file() or not rows:
            continue
        lines = p.read_text(errors="replace").splitlines()
        for name, lo, hi in _functions("\n".join(lines)):
            body = lines[lo - 1:hi]
            model = [i for i, ln in enumerate(body, lo) if re.search(r"local_dict\.update\(\s*sym_map\b", ln)
                     or re.search(r"local_dict\s*\[\s*\w+\s*\]\s*=\s*sp\.Symbol", ln)]
            if not model:
                continue
            first = min(model)
            for i, ln in enumerate(body, lo):
                if i > first and i in rows and re.search(r"local_dict\.update\(", ln) \
                        and not re.search(r"sym_map|Piecewise=|And=", ln):
                    late.append(f"{Path(f).name}:{i} in {name}")
    record("WARN" if late else "PASS", "parse bindings before the model's names",
           ("bound after the model's names, so a parameter of that name is shadowed: " + ", ".join(late))
           if late else "none added after them", "#1809 (a parameter named frac)")


def check_percent_parse(b: Branch):
    """A new parse of model text spells ExprTk's `%` (fmod) first. sympy parses
    `%` as its floored Mod and folds or simplifies it at once: `-2.5 % 2` reads
    1.5 for -0.5, `(p + 2) % 2` reads Mod(p, 2). #1809's second review; #1815 is
    the rate-law parse's case. `_spell_mod` (#1127) writes `a % b` as mod(a, b)."""
    bare = []
    for p, ln in b.added_in(prefix=("python/bngsim/",), suffix=(".py",)):
        code = ln.split("#", 1)[0]
        if not re.search(r"\b(parse_expr|sympify|parse_expr_cached)\(", code):
            continue
        text = (b.repo / p).read_text(errors="replace") if (b.repo / p).is_file() else ""
        owner = [(lo, hi, fn) for fn, lo, hi in _functions(text) if any(
            lo <= r <= hi and code.strip() in text.splitlines()[r - 1] for r in b.added_at.get(p, ()))]
        scope = "\n".join(text.splitlines()[owner[0][0] - 1:owner[0][1]]) if owner else text
        if "_spell_mod" not in scope and "%" not in code:
            bare.append(f"{Path(p).name}: {code.strip()[:60]}")
    record("WARN" if bare else "PASS", "% spelled before a new parse",
           ("parses with no _spell_mod in reach (is the text model math?): " + "; ".join(dict.fromkeys(bare)))
           if bare else "no new parse of model text", "#1809, #1815")


_CODE_PATHS = ("python/bngsim", "src", "include", "ci")


def check_review_covers_head(b: Branch, body: str):
    """The code the PR holds is the code a review round read. Fixes made after the
    last reviewed commit are unreviewed: #1809's round on its fixes found a
    regression they introduced, and #1791's found three gaps. Run a round on the
    fixes, or say in the description that they were not reviewed."""
    m = re.search(r"^## Review\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    section = m.group(1) if m else ""
    rounds = re.findall(r"\bon `([0-9a-f]{7,40})`", section)
    if not rounds:
        record("SKIP", "review covers the head", "no review round names a commit")
        return
    last = None
    for sha in rounds:  # the latest of the reviewed commits on this branch
        if sh(["git", "merge-base", "--is-ancestor", sha, "HEAD"], b.repo).returncode:
            continue
        if last is None or sh(["git", "merge-base", "--is-ancestor", last, sha], b.repo).returncode == 0:
            last = sha
    if last is None:
        record("WARN", "review covers the head", f"no reviewed commit ({', '.join(rounds)}) is on this branch",
               "#1809, #1791")
        return
    after = sh(["git", "log", "--format=%h %s", f"{last}..HEAD", "--", *_CODE_PATHS], b.repo).stdout.split("\n")
    after = [ln for ln in after if ln.strip()]
    if not after:
        record("PASS", "review covers the head", f"no code changed after the last reviewed commit {last[:8]}")
        return
    if re.search(r"not (been )?reviewed|unreviewed|no round (has )?read", section, re.I):
        record("PASS", "review covers the head",
               f"{len(after)} commit(s) after {last[:8]}, and the Review section says so")
        return
    record("WARN", "review covers the head",
           f"{len(after)} commit(s) changed code after the last reviewed commit {last[:8]} "
           f"({'; '.join(after[:3])}): run a round on the fixes, or say they were not reviewed",
           "#1809 (a regression in round 1's fix), #1791")


_WRITERS = re.compile(r"^python/bngsim/convert/(_\w*writer\w*\.py|__init__\.py|_sedml\.py|_protocol\.py)$")


def check_writer_corpus(b: Branch, evidence: list[Path] | None):
    """A change to a file writer is compared with main's, byte for byte, over a
    corpus: how many files are identical, how many changed, and that none changed
    without cause. #1791: reusing the codegen's sort rewrote 521 of 1291 files
    with nothing wrong; only the corpus showed it."""
    writers = [f for f in b.files if _WRITERS.match(f)]
    if not writers:
        record("SKIP", "a writer change is checked on a corpus", "no writer changed")
        return
    text = ""
    for d in evidence or []:
        for f in (d.rglob("*") if d.is_dir() else [d]):
            if f.is_file() and f.suffix in (".log", ".txt", ".md") and f.stat().st_size < 50_000_000:
                try:
                    text += f.read_text(errors="replace") + "\n"
                except OSError:
                    pass
    if re.search(r"\bidentical\s+\d+|\b\d+\s+(byte-)?identical\b", text, re.I) and re.search(r"\bcorpus\b", text, re.I):
        record("PASS", "a writer change is checked on a corpus", "an evidence log counts identical files over a corpus")
        return
    record("WARN", "a writer change is checked on a corpus",
           f"{', '.join(Path(w).name for w in writers)} changed and no evidence log counts identical "
           "files over a corpus: write a corpus with main and with this branch and compare them",
           "#1791 (521 files rewritten for nothing)")


def check_clean_tree(b: Branch):
    """Nothing tracked left uncommitted: a mutant run's leftovers, a stray edit (#1638)."""
    dirty = [ln for ln in sh(["git", "status", "--porcelain", "--untracked-files=no"], b.repo).stdout.splitlines() if ln.strip()]
    record("WARN" if dirty else "PASS", "tracked tree committed",
           f"{len(dirty)} uncommitted: " + ", ".join(d[3:] for d in dirty[:4]) if dirty else "nothing uncommitted",
           "never-truncate-mutant-runs: diff before commit")


def check_evidence(body: str, evidence: list[Path]):
    """Numbers in the body that a log in the evidence dir must show (false claims, ~40 PRs)."""
    logs = {}
    files = [f for d in evidence for f in (d.rglob("*") if d.is_dir() else [d])]
    for f in files:
        if f.is_file() and f.suffix in (".log", ".txt", ".md", ".output", ".json") and f.stat().st_size < 50_000_000:
            try:
                logs[f] = f.read_text(errors="replace")
            except OSError:
                pass
    text = "\n".join(logs.values())
    flat = text.replace(",", "")
    unbacked = []
    for n in sorted(set(re.findall(r"\*\*([\d,]+) passed\*\*", body))):
        if f"{n.replace(',', '')} passed" not in flat:
            unbacked.append(f"{n} passed")
    m = re.search(r"Mutants: (\d+) of (\d+) killed", body)
    if m:
        last = {}
        for f, t in sorted(logs.items(), key=lambda kv: kv[0].stat().st_mtime):
            if "mutant" not in f.name or f.suffix == ".json":  # a mutants file, not a log
                continue
            for name, verdict in re.findall(
                    r"^([A-Z]+\d+)\b[^\n]*?: (KILLED|SURVIVED|NOT APPLIED|BUILD FAILED|TEST ERROR)\b", t, re.M):
                last[name] = verdict
        killed = sum(v == "KILLED" for v in last.values())
        if last and (killed, len(last)) != (int(m.group(1)), int(m.group(2))):
            unbacked.append(f"mutants: body {m.group(1)} of {m.group(2)}, logs {killed} of {len(last)}")
        # A mutant that was not applied, or whose build or test did not run, tested
        # nothing: it is neither killed nor a survivor, and the count is not one.
        untested = sorted(n for n, v in last.items() if v in ("NOT APPLIED", "BUILD FAILED", "TEST ERROR"))
        if untested:
            unbacked.append(f"mutants that tested nothing ({', '.join(f'{n}: {last[n]}' for n in untested)}): "
                            "fix the mutants file and rerun")
        mutant_logs = [t for f, t in logs.items() if "mutant" in f.name and f.suffix != ".json"]
        if any(re.search(r"^INTERRUPTED\b", t, re.M) for t in mutant_logs):
            unbacked.append("a mutant run was interrupted: check the tree is clean (git status) and rerun it")
        elif any("restored" not in t for t in mutant_logs):
            unbacked.append("a mutant log has no 'restored' line (run cut short?)")
    record("FAIL" if unbacked else "PASS", "body numbers backed by logs",
           "; ".join(unbacked) + " (not found in the evidence logs)" if unbacked else f"each **N passed** and the mutant count are in {len(logs)} logs",
           "class: false or stale claims")


def lint_comment(path: Path):
    """A drafted PR comment: no attribution, no placeholders, no promise made for the user."""
    text = path.read_text()
    probs = []
    if re.search("cl" + "aude|anthr" + "opic|generated with|co-authored", text, re.I):
        probs.append("attribution")
    if re.search(r"\bTODO\b|\bXXX\b|_LINE\b", text):
        probs.append("placeholder")
    promise = re.findall(r"\b(I'll|I will|I can file|I'll file|we'll|will follow up|I'll open)\b[^.\n]{0,60}", text)
    if promise:
        probs.append("commits the user to work: " + "; ".join(promise[:2]) + " (ask first)")
    return probs


def check_against_main(b: Branch, main_python: str, main_tree: Path):
    """Run each new or changed test file with main's build: something must fail there."""
    tests = [f for f in b.files if f.startswith("python/tests/test_") and f.endswith(".py") and (b.repo / f).is_file()]
    if not tests:
        record("SKIP", "new tests fail on main (main build)", "no test files changed")
        return
    rows, bad = [], []
    for f in tests:
        dest = main_tree / f
        existed = dest.exists()
        saved = dest.read_bytes() if existed else None
        dest.write_bytes((b.repo / f).read_bytes())
        try:
            r = subprocess.run([main_python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f], cwd=main_tree,
                               capture_output=True, text=True, timeout=1800)
        finally:
            if existed:
                dest.write_bytes(saved)
            else:
                dest.unlink()
        tail = next((ln for ln in reversed(r.stdout.splitlines()) if " passed" in ln or " failed" in ln), "no result")
        rows.append(f"{Path(f).name}: {tail.strip('= ')}")
        nf = int((re.search(r"(\d+) failed", tail) or [0, 0])[1])
        npass = int((re.search(r"(\d+) passed", tail) or [0, 0])[1])
        got = STATE.get("main_failed", (0, 0))
        STATE["main_failed"] = (got[0] + nf, got[1] + nf + npass)
        if not nf:
            bad.append(Path(f).name)
    record("FAIL" if bad else "PASS", "new tests fail on main (main build)",
           "; ".join(rows) + (f" — nothing fails on main in {', '.join(bad)}" if bad else ""),
           "revert the fix and watch each test fail")


# ─── 2026-11-17: lessons from #1815, #1821 and the NFsim patches ─────────────


def check_stacked(b: Branch, body: str | None):
    """A branch that holds another open PR's commits is stacked on it. Comparing with
    main then credits this change with what that PR fixed (#1815 on #1809)."""
    r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--limit", "200", "--json",
            "number,headRefOid,headRefName"], b.repo, timeout=120)
    if r.returncode:
        record("SKIP", "stacked on another open PR", "gh unavailable")
        return
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    head = sh(["git", "rev-parse", "HEAD"], b.repo).stdout.strip()
    main = sh(["git", "rev-parse", "origin/main"], b.repo).stdout.strip() or b.merge_base

    def within(a, c):  # a is an ancestor of c; False where a is not in this clone
        return sh(["git", "merge-base", "--is-ancestor", a, c], b.repo).returncode == 0

    under = []
    for pr in json.loads(r.stdout):
        oid = pr["headRefOid"]
        if pr["headRefName"] == mine or oid == head:
            continue
        if within(oid, head) and not within(oid, main):
            under.append((pr["number"], oid))
    STATE["stacked_on"] = under
    if not under:
        record("PASS", "stacked on another open PR", "the branch holds no other open PR's commits")
        return
    names = ", ".join(f"#{n} ({oid[:8]})" for n, oid in under)
    unnamed = [f"#{n}" for n, _ in under if body is not None and f"#{n}" not in body]
    based = any(within(oid, b.merge_base) for _, oid in under)
    detail = f"holds the commits of {names}"
    if unnamed:
        detail += f"; the body does not name {', '.join(unnamed)} (say it is stacked, and the merge order)"
    if not based:
        detail += f"; the diff read here is against main and includes theirs: pass --base {under[-1][1][:8]}"
    record("FAIL" if unnamed else "WARN", "stacked on another open PR", detail,
           "what the PR underneath fixed is not this PR's to claim: compare with its head, not main")


_HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9._-]+)")
_PLACEHOLDER_HOMES = frozenset({"you", "user", "username", "me", "name", "runner", "runneradmin", "shared", "..."})


_SHARED_DOT_DIRS = frozenset({".cache", ".config", ".local", ".venv", ".git", ".github", ".ssh", ".pyenv",
                              ".cargo", ".npm", ".conda", ".docker", ".Trash"})


def _tool_dirs() -> list[str]:
    """The hidden directories in this machine's home that are some tool's own: where
    an agent, an editor or a job runner keeps its working files. The ones every
    machine has, and documentation names, are left out."""
    try:
        names = [d.name for d in Path.home().iterdir() if d.name.startswith(".") and d.is_dir()]
    except OSError:
        return []
    return sorted(n for n in names if n not in _SHARED_DOT_DIRS and not n.endswith("_cache") and len(n) > 2)


def check_private_paths(b: Branch, body: str | None):
    """An added line, or the body, that holds a home directory or a tool's working
    directory: a vendoring script wrote its checkout's path into VENDOR.json (#1821)."""
    own = Path.home().name
    tools = _tool_dirs()
    hits = []
    rows = list(b.added) + ([("the PR body", ln) for ln in body.splitlines()] if body else [])
    for p, ln in rows:
        found = [m.group(0) for m in _HOME.finditer(ln) if m.group(1) not in _PLACEHOLDER_HOMES]
        for d in tools:
            if f"/{d}/" in ln or ln.strip().startswith(f"{d}/"):
                found.append(f"{d}/")
        if own and len(own) > 2 and own not in _PLACEHOLDER_HOMES and re.search(rf"[/\\]{re.escape(own)}[/\\]", ln):
            found.append(f"/{own}/")
        if found:
            hits.append(f"{p}: {sorted(set(found))[0]}")
    hits = list(dict.fromkeys(hits))
    record("FAIL" if hits else "PASS", "no private paths in the diff",
           "; ".join(hits[:6]) + (f" (+{len(hits) - 6} more)" if len(hits) > 6 else "") if hits
           else "no home or tool directory in an added line or the body",
           "a path on this machine names its user to everyone who reads the repository")


_VENDOR = ("third_party/nfsim/", "scripts/nfsim_vendor_patches/", "scripts/vendor_nfsim.py")
_VENDOR_CHECKOUTS = ("/tmp/nfsim-vendor-candidate", "/private/tmp/nfsim-vendor-candidate")


def check_vendored(b: Branch, python: str):
    """A change under the vendored NFsim tree is made through the carry queue, and the
    tripwire shows the tree is what the queue produces: it caught a mutant an
    interrupted run left in third_party/nfsim (#1821)."""
    if not any(f.startswith(_VENDOR) for f in b.files):
        record("SKIP", "vendored NFsim tree is the carry queue's", "no vendored file changed")
        return
    probs = []
    meta = b.repo / "third_party/nfsim/VENDOR.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text())
        except ValueError:
            data = None
            probs.append("VENDOR.json is not JSON")
        if data:
            where = str((data.get("source") or {}).get("local_checkout", ""))
            if where and where not in _VENDOR_CHECKOUTS:
                probs.append(f"VENDOR.json records the checkout {where!r}: vendor from /tmp/nfsim-vendor-candidate")
            queued = sum(len(e.get("commits") or [None]) for e in data.get("carry_queue", []))
            patches = len(list((b.repo / "scripts/nfsim_vendor_patches").glob("*.patch")))
            if patches and queued != patches:
                probs.append(f"{patches} patch files, {queued} commits in VENDOR.json's carry queue")
    script = b.repo / "scripts/vendor_nfsim.py"
    ran = "the tripwire was not run (no scripts/vendor_nfsim.py)"
    if script.is_file():
        exe = python if Path(python).exists() else sys.executable
        if not any(Path(c).is_dir() for c in _VENDOR_CHECKOUTS):
            ran = "the tripwire was NOT run: no candidate checkout at /tmp/nfsim-vendor-candidate (rebuild it)"
            probs.append(ran)
        else:
            try:
                r = sh([exe, "scripts/vendor_nfsim.py", "--verify-clean", "--strict"], b.repo, timeout=1800)
                tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
                ran = f"vendor_nfsim.py --verify-clean --strict: exit {r.returncode}"
                if r.returncode:
                    probs.append(f"{ran} ({tail[0][:160]})")
            except subprocess.TimeoutExpired:
                probs.append("vendor_nfsim.py --verify-clean --strict did not finish in 30 minutes")
    record("FAIL" if probs else "PASS", "vendored NFsim tree is the carry queue's",
           "; ".join(probs) if probs else ran,
           "an edit in third_party/nfsim that is not a patch in the queue is lost at the next vendoring")


_CLOCK = re.compile(r"\b(perf_counter|monotonic|process_time|time\.time|timeit)\b")
_TIMED_NAME = r"(?:elapsed|duration|took|wall\w*|runtime|seconds|secs|dt|t_?(?:run|total)|\w*_(?:time|s|sec|secs|seconds|ms))"
_TIMED = re.compile(
    rf"\bassert\b[^#]*?(?:\b{_TIMED_NAME}\b|{_CLOCK.pattern}\(\)\s*-\s*\w+)[^#<>]*<=?\s*[\d.]+(?:e-?\d+)?\b")


def check_wall_clock(b: Branch):
    """A new test that bounds a measured time fails on a busy runner, and passes again
    on a rerun: two red legs in one week were such tests, not bugs (#1819, #1815)."""
    hits = []
    for p in sorted({p for p, _ in b.added_in(prefix=("python/tests/",), suffix=(".py",))}):
        f = b.repo / p
        if not f.is_file() or not _CLOCK.search(f.read_text(errors="replace")):
            continue
        for q, ln in b.added_in(prefix=(p,)):
            if q == p and _TIMED.search(ln):
                hits.append(f"{Path(p).name}: {ln.strip()[:70]}")
    hits = list(dict.fromkeys(hits))
    record("WARN" if hits else "PASS", "no wall-clock bound in a new test",
           "; ".join(hits[:4]) if hits else "no added assertion bounds a measured time",
           "bound the work (steps, evaluations, a ratio taken in one process), not the seconds")


def check_tree_underneath(name: str, at: str, main: str):
    """The build the new tests are run on to see them fail: main, or, for a stacked
    branch, the head of the PR underneath. On main a test of a stacked branch can
    fail for what that PR fixes, and the failure is then credited to this one."""
    under = STATE.get("stacked_on") or []
    if under and at not in {oid for _, oid in under}:
        n, oid = under[-1]
        record("FAIL", "new tests fail before this change",
               f"the branch is stacked on #{n}, and {name} is at {at[:8]}: a test that fails there may be "
               f"failing for what #{n} fixes. Pass --main-tree a build of {oid[:8]}",
               "compare with the head underneath, not main (#1815 on #1809)")
    elif not under and at != main:
        record("WARN", "main build is current main", f"{name} is at {at[:8]}, main at {main[:8]}: rebuild it", "")


# ─── third pass ─────────────────────────────────────────────────────────────

STATE: dict = {}  # numbers one check works out for another (test delta, main failures)


def check_issue_free(b: Branch, issue: str | None, body: str | None):
    """Nobody else is on the issue (check-open-prs-before-picking-issue), and the
    title, fragment and body agree on which issues this clears."""
    if not issue:
        record("SKIP", "issue free and named alike", "no --issue")
        return
    probs, warns = [], []
    mine = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], b.repo).stdout.strip()
    r = sh(["gh", "pr", "list", "-R", "lanl/bngsim", "--state", "open", "--search", f"{issue} in:title,body",
            "--json", "number,author,headRefName"], b.repo, timeout=60)
    if not r.returncode:
        others = [f"#{p['number']} ({p['author']['login']})" for p in json.loads(r.stdout or "[]") if p["headRefName"] != mine]
        if others:
            warns.append(f"other open PRs name #{issue}: {', '.join(others)}")
    v = sh(["gh", "issue", "view", issue, "-R", "lanl/bngsim", "--json", "state,assignees"], b.repo, timeout=60)
    if not v.returncode:
        info = json.loads(v.stdout)
        if info["state"] != "OPEN":
            probs.append(f"#{issue} is {info['state'].lower()}")
        who = [a["login"] for a in info["assignees"]]
        if who:
            warns.append(f"#{issue} is assigned to {', '.join(who)}")
    frags = [f for f in b.files if f.startswith("changelog.d/") and f.endswith(".md")]
    if frags and not any(Path(f).name.startswith(f"{issue}.") for f in frags):
        warns.append(f"no changelog.d/{issue}.*.md (fragments: {', '.join(Path(f).name for f in frags)})")
    subject = sh(["git", "log", "-1", "--format=%s"], b.repo).stdout.strip()
    if f"#{issue}" not in subject:
        warns.append(f"the last commit's subject does not name #{issue}")
    status = "FAIL" if probs else ("WARN" if warns else "PASS")
    record(status, "issue free and named alike", "; ".join(probs + warns) or f"#{issue} open, unassigned, no other PR; named in fragment and commit",
           "check-open-prs-before-picking-issue")


def check_body_counts(body: str):
    """The body's '+N' test count and 'N of M fail on main' against what the gate measured."""
    probs = []
    delta = STATE.get("test_delta")
    m = re.search(r"\*\*Test counts:\*\* \+(\d+)", body)
    if delta is not None and m and int(m.group(1)) != delta:
        probs.append(f"body says +{m.group(1)} tests, the branch adds {delta}")
    fails = STATE.get("main_failed")
    m = re.search(r"(\d+) of the (\d+)(?: new)? (?:cases |tests )?fail on main", body)
    if fails is not None and m and (int(m.group(1)), int(m.group(2))) != fails:
        probs.append(f"body says {m.group(1)} of {m.group(2)} fail on main, the run gave {fails[0]} of {fails[1]}")
    if delta is None and fails is None:
        record("SKIP", "body counts match the measured ones", "needs --floor and/or --main-tree")
        return
    record("FAIL" if probs else "PASS", "body counts match the measured ones", "; ".join(probs) or "test delta and fail-on-main as measured",
           "class: false or stale claims")


def check_evidence_fresh(b: Branch, evidence: list[Path]):
    """A log older than a file this branch changes is about other code (a rebase or a
    late edit since). Judged on the files' mtimes, which a commit leaves alone and a
    checkout or rebase moves."""
    watched = [f for f in b.files if f.startswith(LIBRARY) or (f.startswith("python/tests/") and f.endswith(".py"))]
    stamps = [((b.repo / f).stat().st_mtime, f) for f in watched if (b.repo / f).is_file()]
    if not stamps:
        record("SKIP", "evidence newer than the code", "no code or test file changed")
        return
    newest, which = max(stamps)
    lib = [(t, f) for t, f in stamps if f.startswith(LIBRARY)]
    newest_lib, which_lib = max(lib) if lib else (0.0, "")
    old = []
    for d in evidence:
        for f in (d.rglob("*") if d.is_dir() else [d]):
            if f.is_file() and f.suffix in (".log", ".output") and any(k in f.name for k in ("linux", "suite", "mutant", "corpus", "perf")):
                # A corpus or timing run reads no test file; the others run the tests.
                limit, what = (newest_lib, which_lib) if any(k in f.name for k in ("corpus", "perf")) else (newest, which)
                if f.stat().st_mtime < limit:
                    old.append(f"{f.name} (before {Path(what).name})")
    record("WARN" if old else "PASS", "evidence newer than the code",
           (f"{len(set(old))} logs predate an edit to what they ran: {', '.join(sorted(set(old))[:5])}: rerun, or say they were before it"
            if old else "every suite, Linux, mutant, corpus and timing log is newer than the files it is about"), "Main moved (MANUAL)")


#: Words in a test file's name that say nothing about its topic.
_NAME_STOP = frozenset(
    "test tests the and for from with that this when where every each its into onto than then "
    "after before does doesn not are was were have has one two runs run read reads".split()
)


def check_linux_list(b: Branch, evidence: list[Path]):
    """The Docker Linux run covers the test files that exercise what the branch changed (#1636).

    On #1636 the Linux run's "neighbour" files were picked by name and missed
    test_net_hill_below_one_at_zero.py, the file of the very feature changed; its
    old-rule test failed only in the full macOS suite, after Linux had passed. The
    rule since: pick the list with git grep for what the fix changes. This finds
    the test files that name a function or class the branch's library diff
    touches, and those whose name shares a topic word with a test file the branch
    adds, and warns about each the run's list leaves out."""
    scripts = [p for e in evidence for p in ([e] if e.is_file() else sorted(e.rglob("run_linux.sh")))
               if p.name == "run_linux.sh"]
    if not scripts:
        record("SKIP", "Linux run covers the touched tests", "no docker/run_linux.sh in the evidence")
        return
    text = "\n".join(p.read_text(errors="replace") for p in scripts)
    if re.search(r"pytest[^\n]*\spython/tests(?:/)?(?:\s|$)", text):
        record("PASS", "Linux run covers the touched tests", "it runs the whole of python/tests")
        return
    listed = set(re.findall(r"python/tests/(test_[\w]+\.py)", text))
    lib = [f for f in b.files if f.startswith(("python/bngsim/", "src/", "include/")) or f.startswith("ci/")]
    # Names from the library's diff only: a helper a test file defines says nothing
    # about what the fix changed.
    lib_diff = "".join(
        chunk for chunk in re.split(r"(?=^diff --git )", b.diff, flags=re.M)
        if re.match(r"diff --git a/(python/bngsim/|src/|include/|ci/)", chunk)
    )
    names: set[str] = set()
    for m in re.finditer(r"^@@[^@]*@@\s*(?:async\s+)?(?:def|class)\s+(\w+)", lib_diff, re.M):
        names.add(m.group(1))
    for m in re.finditer(r"^[+-]\s*(?:async\s+)?(?:def|class)\s+(\w+)", lib_diff, re.M):
        names.add(m.group(1))
    names = {n for n in names if len(n.strip("_")) >= 6 and not n.startswith("test_")}
    wanted: dict[str, str] = {}
    if lib and names:
        for n in sorted(names):
            r = sh(["git", "grep", "-l", "-w", n, "--", "python/tests/test_*.py"], b.repo)
            hits = [Path(x).name for x in r.stdout.split()]
            if 0 < len(hits) <= 40:  # a name in more files than that is too common to steer by
                for h in hits:
                    wanted.setdefault(h, f"names {n}")
    new_tests = [Path(f).name for f in b.files if re.match(r"python/tests/test_\w+\.py$", f)
                 and not (b.repo.joinpath(f).is_file() and sh(["git", "cat-file", "-e", f"{b.merge_base}:{f}"], b.repo).returncode == 0)]
    words = {w for t in new_tests for w in t[5:-3].split("_") if len(w) >= 4 and w not in _NAME_STOP}
    for t in sh(["git", "ls-files", "python/tests/test_*.py"], b.repo).stdout.split():
        name = Path(t).name
        shared = words & set(name[5:-3].split("_"))
        if shared and name not in new_tests:
            wanted.setdefault(name, f"shares '{sorted(shared)[0]}' with a new test file")
    missing = {k: v for k, v in wanted.items() if k not in listed}
    if not missing:
        record("PASS", "Linux run covers the touched tests",
               f"{len(listed)} files listed; each of the {len(wanted)} the change touches is among them")
        return
    rows = [f"{k} ({v})" for k, v in sorted(missing.items())[:8]]
    more = f"; and {len(missing) - 8} more" if len(missing) > 8 else ""
    record("WARN", "Linux run covers the touched tests",
           f"{len(missing)} of {len(wanted)} touched test files are not in the run: " + "; ".join(rows) + more,
           "#1636: pick the Linux list by git grep for what the fix changes, not by name")


def check_full(b: Branch, docs_python: str | None):
    """pre-commit --all-files and sphinx -W, as CI's lint legs run them (precommit-all-files)."""
    r = sh(["pre-commit", "run", "--all-files"], b.repo, timeout=1800)
    bad = [ln.split("...")[0].strip() for ln in r.stdout.splitlines() if ln.rstrip().endswith("Failed")]
    record("FAIL" if r.returncode else "PASS", "pre-commit --all-files", ", ".join(bad) or ("passed" if not r.returncode else r.stdout[-150:]),
           "precommit-all-files (it caught a mypy annotation on #1720)")
    if not docs_python:
        record("SKIP", "sphinx -W", "pass --docs-python (an env with docs/requirements-ci.txt)")
        return
    with tempfile.TemporaryDirectory() as out:
        r = sh([docs_python, "-m", "sphinx", "-W", "--keep-going", "-q", "-b", "html", "docs", out], b.repo, timeout=1800)
    record("FAIL" if r.returncode else "PASS", "sphinx -W", (r.stderr.strip().splitlines() or ["warnings"])[-1][:150] if r.returncode else "passed",
           "a docstring needs local sphinx -W")


MANUAL = [
    ("Siblings", "every engine/route/loader that reads the same thing: run, run_batch, parameter_scan, bifurcate, steady_state(_batch), sensitivities, clones, continued runs, SSA/PSA/NFsim/RuleMonkey/JAX, codegen cc/MIR — each fixed, tested or 'unaffected because'"),
    ("Cost at scale", "paired interleaved timing vs main on the issue's model AND a large one (and a pathological one: cycles, deep nesting, 2,000 rows/stops); gate new walks on a cheap predicate"),
    ("Fixtures that can tell", "would the plausible wrong implementation give different numbers? (fixed points, self-inverse, linear ODEs, symmetric matrices)"),
    ("Route asserted", "assert codegen_backend / linear_solver / Jacobian source, not just the answer"),
    ("Degenerate shapes", "0, empty, lo > hi, repeated sample time, zero replicates, NaN/inf each side, negative and far t_start"),
    ("Chains and order", "dependency walks 2–3 deep, self-reference, declarations reversed"),
    ("Scale of guards", "floors/steps on the quantity's own scale; rerun at nM and 1e12, rtol 1e-2 and 1e-12"),
    ("Remedies run", "every remedy a message or doc recommends, run on every control, at its edges (0, dominance)"),
    ("New refusals", "corpus sweep: nothing main ran is now refused; nothing main refused now returns silently wrong"),
    ("Main moved", "re-run the suite after the final rebase; 'claims cite the current main' lists the shas to recheck"),
    ("Other platforms", "Docker linux/amd64 (CMAKE_BUILD_PARALLEL_LEVEL=2); Windows via windows-tail dispatch when the legs miss the tests"),
    ("Other spellings", "every way a model writes the same condition: operator flipped, bound on the left, abs() and squares (#904's edges), <= beside <, == and != (#1758: the abs() spelling kept main's behaviour)"),
    ("Nearest case main got right", "a new keep/refuse/carry condition, run on its closest neighbour main handled: an in-tolerance undershoot, the overflowing law's own reactant (#1751: a regression the first tests could not see)"),
    ("Routes kept alike", "a step two routes both take (ODE and SSA, interpreted and compiled): one helper both call, or the body says why not (#1753: SSA's schedule shift is its own copy, without the ODE's repeat)"),
    ("A loop of costly steps", "a loop whose step parses or evaluates sympy, directly or through a callable it is handed: its worst-case count, and that case timed against main (#1758: 64 parses a solve, 17 ms against 1.7)"),
    ("Every input a gate lists", "a gate on what a run is handed: list every field (crossing stops, probes, switch times, events) and test each one alone (#1753: switch times were missing)"),
    ("State kept between runs", "anything a run keeps for later (user data, marks, a memo, a cached solver): run the same start twice on one Simulator, and once on a new one, and require them equal to the bit; and continued legs (run_until) against one run, on the warm and the cold path (#1721: a plain run reused the last run's marks; #1826)"),
    ("Scope words tested", "every 'only X reaches it', 'runs wherever it ran on main' or 'unchanged elsewhere' in the description, the changelog or a mutant's excuse is backed by a run that would show otherwise, or cut (#1721's surviving mutant was a bug; #1763's run_diffrax)"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--issue")
    ap.add_argument("--body", type=Path, help="the draft PR body to lint")
    ap.add_argument("--count", action="store_true", help="collect the suite and check the floor (slow)")
    ap.add_argument("--main-pkg", help="a package dir with main's code, to prove new tests fail on main")
    ap.add_argument("--boot", default=os.environ.get("BNGSIM_AB_BOOT", ""),
                    help="dir with the A/B sitecustomize that --main-pkg needs ($BNGSIM_AB_BOOT)")
    ap.add_argument("--no-gh", action="store_true")
    ap.add_argument("--title", help="the PR title, to check its shape")
    ap.add_argument("--pr", help="the open PR this branch updates (found from the branch if omitted)")
    ap.add_argument("--thread-read", action="store_true", help="the comments since the last push have been read")
    ap.add_argument("--floor", action="store_true", help="work out the floor line from main's CI count (gh, a minute)")
    ap.add_argument("--python", default=None, help="the interpreter with this tree's bngsim (default: .venv/bin/python)")
    ap.add_argument("--evidence", type=Path, action="append", help="a dir or log with this PR's results (mutants, Docker, suite); repeatable. The worktree's own *.log files are always read")
    ap.add_argument("--main-tree", type=Path, help="a built checkout of main (its .venv is used) to run the new tests on")
    ap.add_argument("--comment", type=Path, help="lint a drafted PR comment and exit")
    ap.add_argument("--full", action="store_true", help="also run pre-commit --all-files and sphinx -W (slow)")
    ap.add_argument("--docs-python", help="an interpreter with docs/requirements-ci.txt, for --full's sphinx -W")
    ap.add_argument("--check-stamp", action="store_true",
                    help="exit 0 only if the gate last passed on this HEAD, tree and body; run just before pushing")
    a = ap.parse_args()
    if a.comment:  # needs no checkout
        probs = lint_comment(a.comment)
        print("comment: " + ("; ".join(probs) if probs else "no attribution, placeholder or promise"))
        return 1 if probs else 0
    repo = a.repo.resolve()
    if str(repo).startswith(("/tmp/", "/private/tmp/")):
        record("WARN", "worktree outside /tmp", f"{repo} is under /tmp, which is reaped overnight", "tmp-is-reaped")
    sh(["git", "fetch", "-q", "origin"], repo, timeout=120)
    b = Branch(repo, a.base)
    body = a.body.read_text() if a.body else None
    python = a.python or str(repo / ".venv/bin/python")
    if a.check_stamp:
        sp = stamp_path(repo)
        ok = sp.is_file() and json.loads(sp.read_text()) == fingerprint(repo, body)
        print("stamp matches: the gate passed on this HEAD, tree and body" if ok else
              "STAMP MISMATCH: HEAD, the tree or the body changed since the gate last passed (or it never did): rerun it")
        return 0 if ok else 1
    title = a.title
    if title is None and a.pr and not a.no_gh:
        t = sh(["gh", "pr", "view", a.pr, "-R", "lanl/bngsim", "--json", "title", "--jq", ".title"], repo, timeout=60)
        title = t.stdout.strip() or None

    check_attribution(b, body)
    check_staged_deletions(b)
    check_base(b)
    check_changelog(b)
    check_untracked_new_files(b)
    check_stacklevel(b)
    check_text_encoding(b)
    check_rounded_referee(b)
    check_message_variants(b)
    check_skips(b, python)
    check_strict_xfail(b, a.issue)
    check_scratch_names(b)
    check_bngpath()
    check_clang_format_tests(b)
    if not a.no_gh:
        check_inflight(b)
        check_stacked(b, body)
    check_private_paths(b, body)
    check_ci_legs(b)
    if a.count:
        check_counts(b, python)
    if a.main_pkg:
        check_fail_on_main(b, a.main_pkg, a.boot)
    if body:
        check_body(body, a.issue)
        check_placeholders(body)
        check_template(body, title)
        check_cited_main(body, sh(["git", "rev-parse", b.base_ref], repo).stdout.strip())
        check_body_names_overlaps(b, body)
        if a.evidence:
            check_evidence(body, a.evidence + sorted(repo.glob("*.log")))
    check_new_test_hygiene(b)
    check_untouched_readers(b)
    check_py310(b)
    check_engine_math(b)
    check_negative_only_tests(b)
    check_test_bngpath(b)
    check_docs_claims(b, a.issue)
    check_binding_order(b)
    check_percent_parse(b)
    check_writer_corpus(b, a.evidence)
    check_wall_clock(b)
    check_vendored(b, python)
    if body:
        check_review_covers_head(b, body)
    check_clean_tree(b)
    if a.main_tree:
        mt = a.main_tree.resolve()
        mb = sh(["git", "rev-parse", "HEAD"], mt).stdout.strip()
        base = sh(["git", "rev-parse", b.base_ref], repo).stdout.strip()
        check_tree_underneath(mt.name, mb, base)
        check_against_main(b, str(mt / ".venv/bin/python"), mt)
    if (Path(python).exists()):
        check_build(b, python)
    if not a.no_gh:
        check_thread(b, a.pr, a.thread_read)
        check_remote_branch(b, a.pr)
        if a.floor:
            check_floor_arithmetic(b, python)
        check_issue_free(b, a.issue, body)
    if body:
        check_body_counts(body)
        if a.evidence:
            check_evidence_fresh(b, a.evidence + sorted(repo.glob("*.log")))
    if a.evidence:
        check_linux_list(b, a.evidence)
    if a.full:
        check_full(b, a.docs_python)
    claims = check_overclaims(b, body)
    check_scope_claims(b, body)

    head = sh(["git", "rev-parse", "--short", "HEAD"], repo).stdout.strip()
    print(f"prepr_check  {repo.name}  HEAD {head}  base {b.merge_base[:10]}  {len(b.files)} files changed\n")
    for status, name, detail, lesson in RESULTS:
        tag = f"\033[{MARK[status]}m{status:4s}\033[0m" if COLOR else f"{status:4s}"
        print(f"{tag}  {name:34s} {detail}")
        if lesson and status in ("FAIL", "WARN"):
            print(f"      {'':34s} ↳ {lesson}")
    if claims:
        print("\nClaims to recount at this head (every/all/never/identical/only …):")
        for p, ln in claims[:25]:
            print(f"  {p}: {ln[:110]}")
        if len(claims) > 25:
            print(f"  … and {len(claims) - 25} more")
    print("\nMANUAL (answer each in the PR body):")
    for k, v in MANUAL:
        print(f"  [ ] {k}: {v}")
    fails = sum(1 for r in RESULTS if r[0] == "FAIL")
    warns = sum(1 for r in RESULTS if r[0] == "WARN")
    print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failed, {warns} warnings, "
          f"{sum(1 for r in RESULTS if r[0] == 'PASS')} passed")
    if not fails:
        stamp_path(repo).write_text(json.dumps(fingerprint(repo, body)))
        print("stamp written: run with --check-stamp (and the same --body) just before pushing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
