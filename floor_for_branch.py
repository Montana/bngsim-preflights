#!/usr/bin/env python3
"""What this branch's PYTEST_COLLECTED_FLOOR line should read, and which
filtered CI legs run its new tests (a sketch for lanl/bngsim#1757).

    python ci/floor_for_branch.py                    # main's count read from CI (needs gh)
    python ci/floor_for_branch.py --main-count 20506 # or given by hand
    python ci/floor_for_branch.py --legs             # also the filtered-legs report

The collected floor is main's CI count, plus the tests this branch adds, less
10 (the rule ci/test_floors.env states). Main's count is the one a "Python
suite counts" job printed (`PYTEST_COLLECTED: N`), not a local collection,
which also counts the gitignored corpora. The branch's tests are counted by
collecting the test files it changes, at HEAD and at its merge base with main.

Run from the repository root, with the interpreter that has bngsim installed.
Nothing is written; the lines to paste are printed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FLOORS = Path("ci/test_floors.env")
MARGIN = 10  # "Set the floor 10 below the count."


def run(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and done.returncode:
        sys.exit(f"{' '.join(args)}: {done.stderr.strip()[:300]}")
    return done.stdout


def bounds() -> dict[str, int]:
    out = {}
    for line in FLOORS.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            out[key.strip()] = int(value.strip())
    return out


def main_ci_count(repo: str) -> tuple[int, str]:
    """(count, sha) from main's newest successful python-tests run that printed one."""
    runs = json.loads(
        run(
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--branch",
            "main",
            "--workflow",
            "python-tests.yml",
            "--status",
            "success",
            "--limit",
            "5",
            "--json",
            "databaseId,headSha",
        )
    )
    for r in runs:
        jobs = json.loads(
            run("gh", "run", "view", str(r["databaseId"]), "-R", repo, "--json", "jobs")
        )["jobs"]
        for job in jobs:
            if job["name"].startswith("Python suite counts") and "ubuntu-latest" in job["name"]:
                log = run(
                    "gh",
                    "run",
                    "view",
                    "-R",
                    repo,
                    "--job",
                    str(job["databaseId"]),
                    "--log",
                    check=False,
                )
                m = re.search(r"PYTEST_COLLECTED: (\d+) \(floor", log)
                if m:
                    return int(m.group(1)), r["headSha"]
    sys.exit("no 'PYTEST_COLLECTED: N' in main's recent python-tests runs; pass --main-count")


def collected(files: list[str], cwd: Path) -> int:
    files = [f for f in files if (cwd / f).is_file()]
    if not files:
        return 0
    out = run(
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        *files,
        cwd=cwd,
        check=False,
    )
    m = re.search(r"(\d+) tests? collected", out)
    return int(m.group(1)) if m else 0


def added_tests(base: str) -> tuple[int, list[str]]:
    """Tests the branch adds over `base`: its changed test files, collected at HEAD
    and at the merge base."""
    merge_base = run("git", "merge-base", "HEAD", base).strip()
    changed = run("git", "diff", "--name-only", merge_base, "--", "python/tests").split()
    tests = [f for f in changed if Path(f).name.startswith("test_") and f.endswith(".py")]
    here = collected(tests, Path.cwd())
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "base"
        run("git", "worktree", "add", "-q", "--detach", str(tree), merge_base)
        try:
            there = collected(tests, tree)
        finally:
            run("git", "worktree", "remove", "--force", str(tree), check=False)
    return here - there, tests


def other_prs_on_the_line(repo: str) -> list[str]:
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD").strip()
    prs = json.loads(
        run(
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefName,files",
            check=False,
        )
        or "[]"
    )
    return [
        f"#{p['number']}"
        for p in prs
        if p["headRefName"] != branch and any(f["path"] == str(FLOORS) for f in p.get("files", []))
    ]


def legs_report(base: str) -> list[str]:
    """For each workflow with a `paths:` filter: does this branch fire it, and how
    many of the branch's new test files does its test list include?"""
    merge_base = run("git", "merge-base", "HEAD", base).strip()
    changed = run("git", "diff", "--name-only", merge_base).split()
    new_tests = [f for f in changed if f.startswith("python/tests/test_") and f.endswith(".py")]
    rows = []
    for wf in sorted(Path(".github/workflows").glob("*.yml")):
        text = wf.read_text()
        trigger, _, jobs = text.partition("\njobs:")
        if "paths:" not in trigger:
            continue  # runs on every change
        globs = re.findall(r"^\s+-\s+[\"']?([^\"'\s#]+)", trigger.split("paths:", 1)[1], re.M)
        fires = [
            f for f in changed if any(fnmatch.fnmatch(f, g.replace("**", "*")) for g in globs)
        ]
        if not fires:
            continue
        listed = set(re.findall(r"(python/tests/test_[\w*]+\.py)", jobs))
        if not listed:
            rows.append(f"{wf.name}: runs (on {fires[0]}); not a pytest list")
            continue
        runs_new = [t for t in new_tests if any(fnmatch.fnmatch(t, g) for g in listed)]
        rows.append(
            f"{wf.name}: runs (on {fires[0]}); "
            f"includes {len(runs_new)} of {len(new_tests)} new test files"
        )
    return rows or ["no filtered workflow runs on this branch (python-tests runs everything)"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--main-count", type=int, help="main's collected count, if not read from CI")
    ap.add_argument(
        "--main-sha", default="", help="the commit that count is from (for the clause)"
    )
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--repo", default="lanl/bngsim")
    ap.add_argument("--issue", default="NNNN", help="this PR's issue, for the history clause")
    ap.add_argument("--legs", action="store_true", help="also report the filtered CI legs")
    a = ap.parse_args(argv)

    b = bounds()
    if "PYTEST_COLLECTED_FLOOR" not in b:
        # Since lanl/bngsim#1762 each test file is held to main's own count, and
        # the floors file has no collected-count line to move.
        print("ci/test_floors.env has no PYTEST_COLLECTED_FLOOR: no floor line to set")
        if a.legs:
            print("\nfiltered CI legs:")
            for row in legs_report(a.base):
                print(f"  {row}")
        return 0

    if a.main_count is None:
        count, sha = main_ci_count(a.repo)
    else:
        count, sha = a.main_count, a.main_sha
    delta, files = added_tests(a.base)
    total = count + delta
    floor, slack = b["PYTEST_COLLECTED_FLOOR"], b["PYTEST_COLLECTED_MAX_SLACK"]
    n = len(files)
    print(
        f"main collects {count}{f' at {sha[:8]}' if sha else ''}; this branch adds {delta:+d} "
        f"({n} test file{'s' if n != 1 else ''} changed): {total}"
    )
    side = f"{total - floor} above it" if total >= floor else f"{floor - total} below it"
    print(f"the floor line reads {floor} (slack {slack}): {side}")
    if total < floor:
        verdict = "below the floor: CI will fail. Set the line"
    elif total - floor > slack:
        verdict = "past the slack: CI will call the floor stale. Set the line"
    elif total - floor != MARGIN:
        verdict = (
            "within bounds, so no change is needed; "
            "to keep it 10 below, as PRs that add tests usually do"
        )
    else:
        verdict = ""
    if verdict:
        print(f"\n{verdict}:")
        print(f"  PYTEST_COLLECTED_FLOOR={total - MARGIN}")
        print(
            f"  history clause: ..., and {total} with #{a.issue}'s {delta} tests on main"
            f"{f' at {sha[:8]}' if sha else ''} ({count} there)."
        )
    others = other_prs_on_the_line(a.repo) if a.main_count is None else []
    if others:
        print(
            f"\nother open PRs that change the line: {', '.join(others)} "
            "(whichever merges second will need this run again)"
        )
    if a.legs:
        print("\nfiltered CI legs:")
        for row in legs_report(a.base):
            print(f"  {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
