#!/usr/bin/env python3
"""Mutation testing for a fix: break it on purpose, one way at a time, and check a
test fails each time.

    python mutate.py mutants.json            # run every mutant, log to mutants.log
    python mutate.py mutants.json --only M3  # one of them
    python mutate.py --restore               # put back files a killed run left mutated

A mutants file is JSON:

    {
      "test": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "python/tests/test_x.py"],
      "build": null,
      "mutants": [
        {"name": "M1 the guard never applied", "file": "python/bngsim/x.py",
         "find": "if h < 1:", "replace": "if False:"},
        {"name": "M2 the C++ check dropped", "file": "src/x.cpp",
         "find": "check(x);", "replace": "", "build": ["python", "scripts/rebuild_editable.py"],
         "test": ["python", "-m", "pytest", "-q", "python/tests/test_y.py"]}
      ]
    }

Each mutant replaces `find` with `replace` in `file`; `find` must occur exactly
once (or exactly `count` times, every one replaced). A mutant's own `build` and
`test` override the file's. After the edit the build runs, if there is one, and
then the test. A test that fails kills the mutant; one that passes means no test
catches that way of being wrong.

Every file is restored after its mutant, and on the way out whatever happens,
an exception or Ctrl-C included. Before the first edit, each file is copied to
`.mutate-backup/`, so even a run killed outright can be undone with
`--restore`. Where a mutant needed a build, the original is built again at the
end. The run checks that every file is byte-identical to how it started.

The log (`mutants.log` by default) has one line per mutant, `NAME: KILLED [...]`
or `NAME: SURVIVED [...]`, with the tests that failed. Exit status 1 if any
mutant survived, could not be applied, or its build failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKUP = Path(".mutate-backup")


@dataclass
class Mutant:
    name: str
    file: Path
    find: str
    replace: str
    count: int = 1
    build: list[str] | None = None
    test: list[str] | None = None


@dataclass
class Outcome:
    name: str
    verdict: str  # KILLED, SURVIVED, NOT APPLIED, BUILD FAILED, TEST ERROR
    summary: str = ""
    failed: tuple[str, ...] = ()

    def line(self) -> str:
        head = f"{self.name}: {self.verdict}" + (f" [{self.summary}]" if self.summary else "")
        return head + "".join(f"\n    {f}" for f in self.failed[:5])


def load(path: Path) -> tuple[list[Mutant], list[str] | None, list[str]]:
    """(mutants, the default build, the default test) from a mutants file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    test = data.get("test")
    if not test:
        raise SystemExit(f"{path}: no 'test' command")
    mutants = []
    for i, m in enumerate(data.get("mutants", []), 1):
        missing = [k for k in ("file", "find", "replace") if k not in m]
        if missing:
            raise SystemExit(f"{path}: mutant {i} has no {', '.join(missing)}")
        mutants.append(
            Mutant(
                name=m.get("name", f"M{i}"),
                file=Path(m["file"]),
                find=m["find"],
                replace=m["replace"],
                count=int(m.get("count", 1)),
                build=m.get("build"),
                test=m.get("test"),
            )
        )
    if not mutants:
        raise SystemExit(f"{path}: no mutants")
    return mutants, data.get("build"), test


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_applicable(mutants: list[Mutant]) -> list[Outcome]:
    """A mutant whose `find` does not occur its `count` times is not applied: the
    file moved on, and replacing some other occurrence would test something else."""
    out = []
    for m in mutants:
        if not m.file.is_file():
            out.append(Outcome(m.name, "NOT APPLIED", f"{m.file} does not exist"))
            continue
        n = m.file.read_text(encoding="utf-8").count(m.find)
        if n != m.count:
            out.append(Outcome(m.name, "NOT APPLIED", f"`find` occurs {n} times in {m.file}, not {m.count}"))
    return out


def failed_tests(output: str) -> tuple[str, ...]:
    """The test ids pytest's short summary lists as FAILED or ERROR."""
    return tuple(m.group(2) for m in re.finditer(r"^(FAILED|ERROR) (\S+)", output, re.M))


def summary_line(output: str) -> str:
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    return rows[-1] if rows else ""


def run_one(m: Mutant, build: list[str] | None, test: list[str], timeout: float) -> Outcome:
    original = m.file.read_text(encoding="utf-8")
    m.file.write_text(original.replace(m.find, m.replace), encoding="utf-8")
    try:
        if build:
            b = subprocess.run(build, capture_output=True, text=True, timeout=timeout)
            if b.returncode:
                return Outcome(m.name, "BUILD FAILED", (b.stderr or b.stdout).strip()[-300:])
        t = subprocess.run(test, capture_output=True, text=True, timeout=timeout)
        output = t.stdout + t.stderr
        if t.returncode == 0:
            return Outcome(m.name, "SURVIVED", summary_line(output))
        failed = failed_tests(output)
        # pytest exits 1 when tests failed; another code means they did not run.
        if t.returncode == 1 or failed:
            return Outcome(m.name, "KILLED", summary_line(output), failed)
        return Outcome(m.name, "TEST ERROR", f"exit {t.returncode}: " + output.strip()[-300:])
    except subprocess.TimeoutExpired:
        return Outcome(m.name, "KILLED", f"timed out after {timeout:g} s")
    finally:
        m.file.write_text(original, encoding="utf-8")


def back_up(files: list[Path]) -> None:
    if BACKUP.exists():
        raise SystemExit(
            f"{BACKUP}/ exists: a run before this one did not finish. "
            "Run `mutate.py --restore` first, then delete it."
        )
    for f in files:
        dest = BACKUP / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)


def restore() -> list[Path]:
    """Copy every backed-up file back; the files that differed are returned."""
    changed = []
    for src in sorted(p for p in BACKUP.rglob("*") if p.is_file()):
        dest = src.relative_to(BACKUP)
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            shutil.copy2(src, dest)
            changed.append(dest)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mutants", nargs="?", type=Path, help="the mutants file (JSON)")
    ap.add_argument("--only", action="append", help="run only the mutants whose name starts with this")
    ap.add_argument("--log", type=Path, default=Path("mutants.log"))
    ap.add_argument("--timeout", type=float, default=1800, help="seconds for each build and test")
    ap.add_argument("--restore", action="store_true", help="restore files from .mutate-backup/ and exit")
    a = ap.parse_args(argv)

    if a.restore:
        if not BACKUP.exists():
            print("nothing to restore")
            return 0
        changed = restore()
        print(f"restored {len(changed)} file(s)" + "".join(f"\n  {p}" for p in changed))
        shutil.rmtree(BACKUP)
        return 0
    if a.mutants is None:
        ap.error("give a mutants file, or --restore")

    mutants, build, test = load(a.mutants)
    if a.only:
        mutants = [m for m in mutants if any(m.name.startswith(o) for o in a.only)]
        if not mutants:
            raise SystemExit("--only matches no mutant")
    outcomes = check_applicable(mutants)
    skip = {o.name for o in outcomes}
    files = sorted({m.file for m in mutants if m.file.is_file()})
    before = {f: digest(f) for f in files}

    back_up(files)
    built = False
    try:
        with a.log.open("w", encoding="utf-8") as log:
            for o in outcomes:
                log.write(o.line() + "\n")
            for m in mutants:
                if m.name in skip:
                    continue
                mbuild = m.build if m.build is not None else build
                o = run_one(m, mbuild, m.test or test, a.timeout)
                built = built or bool(mbuild)
                outcomes.append(o)
                log.write(o.line() + "\n")
                log.flush()
                print(o.line(), flush=True)
    finally:
        restore()
        shutil.rmtree(BACKUP, ignore_errors=True)
    if built:  # the last build was of a mutant: build the original again
        rebuild = next(m.build for m in mutants if m.build) if not build else build
        r = subprocess.run(rebuild, capture_output=True, text=True, timeout=a.timeout)
        print(f"original rebuilt: {'ok' if r.returncode == 0 else 'FAILED, rebuild by hand'}")
    moved = [str(f) for f in files if digest(f) != before[f]]
    if moved:
        print("NOT RESTORED: " + ", ".join(moved))
        return 1

    killed = sum(o.verdict == "KILLED" for o in outcomes)
    print(f"\n{killed} of {len(outcomes)} killed; files restored, byte for byte")
    with a.log.open("a", encoding="utf-8") as log:
        log.write(f"restored; {killed} of {len(outcomes)} killed\n")
    return 0 if killed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
