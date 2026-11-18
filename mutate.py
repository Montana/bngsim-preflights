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
once, or exactly `count` times, every one replaced (`"count": "all"` takes
however many there are, at least one). A mutant's own `build` and `test`
override the file's. Where a mutant with no build follows one with a build, the
original is built again first, so its test does not run what the last mutant
built. An `"env"` object, in the file or in a mutant, is added to
the environment of the build and the test. After the edit the build runs, if
there is one, and then the test. A test that fails kills the mutant; one that
passes means no test catches that way of being wrong.

A mutant that cannot be applied tested nothing, and is a failure of the run,
not a result: its `find` is not in the file (the code moved on since the
mutants file was written), or it replaces the text with itself.

The run will not start on files with uncommitted changes (`--allow-dirty` lets
it): with the work committed, `git diff` shows a mutant left behind, and
`git checkout` removes it. Each write of a file, a mutant or the original put back,
is in a later second than the one before, so a cached `.pyc` or a built object of
a mutant never passes for the original's. Every file is restored after its mutant, and on the
way out whatever happens: an exception, Ctrl-C, or the terminal or a calling
tool ending the run (SIGTERM, SIGHUP). Before the first edit, each file is
copied to `.mutate-backup/`, so even a run killed outright (SIGKILL, a power
cut) can be undone with `--restore`. Where a mutant needed a build, the
original is built again at the end. The run checks that every file is
byte-identical to how it started.

Each build and each test has `--timeout` seconds. A test that runs out is
counted as killed, a mutant that hangs being one a test would catch; a build
that runs out is a failed build. The whole process group is ended, so a test
runner's workers do not outlive it.

The log (`mutants.log` by default) has one line per mutant, `NAME: KILLED [...]`
or `NAME: SURVIVED [...]`, with the tests that failed, and ends with a
`restored; N of M killed` line. A run that was cut short ends with
`INTERRUPTED` instead, and its count is not one. Exit status 1 if any mutant
survived, could not be applied, or its build failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BACKUP = Path(".mutate-backup")


@dataclass
class Mutant:
    name: str
    file: Path
    find: str
    replace: str
    count: int | None = 1  # None: every occurrence, however many
    build: list[str] | None = None
    test: list[str] | None = None
    env: dict[str, str] | None = None


@dataclass
class Outcome:
    name: str
    verdict: str  # KILLED, SURVIVED, NOT APPLIED, BUILD FAILED, TEST ERROR
    summary: str = ""
    failed: tuple[str, ...] = ()

    def line(self) -> str:
        head = f"{self.name}: {self.verdict}" + (f" [{self.summary}]" if self.summary else "")
        return head + "".join(f"\n    {f}" for f in self.failed[:5])


def _count(value, where: str) -> int | None:
    if value == "all":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise SystemExit(f"{where}: 'count' is a whole number from 1, or \"all\", not {value!r}")
    return n


def load(path: Path) -> tuple[list[Mutant], list[str] | None, list[str]]:
    """(mutants, the default build, the default test) from a mutants file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    env = data.get("env") or {}
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
                count=_count(m.get("count", 1), f"{path}: mutant {i}"),
                build=m.get("build"),
                test=m.get("test"),
                env={**env, **(m.get("env") or {})} or None,
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
        if m.find == m.replace:
            out.append(Outcome(m.name, "NOT APPLIED", "`replace` is `find`: it changes nothing"))
            continue
        # Read as the mutation reads it: bytes, line endings as they are.
        n = m.file.read_bytes().decode("utf-8").count(m.find) if m.find else 0
        if m.count is None and n == 0:
            out.append(Outcome(m.name, "NOT APPLIED", f"`find` does not occur in {m.file}"))
        elif m.count is not None and n != m.count:
            out.append(Outcome(m.name, "NOT APPLIED", f"`find` occurs {n} times in {m.file}, not {m.count}"))
    return out


def uncommitted(files: list[Path]) -> list[str]:
    """The files git shows as changed or untracked; none where this is no git checkout."""
    if not files:
        return []
    r = subprocess.run(["git", "status", "--porcelain", "--", *map(str, files)], capture_output=True, text=True)
    if r.returncode:  # not a repository, or no git
        return []
    return [ln[3:] for ln in r.stdout.splitlines() if ln.strip()]


def run(cmd: list[str], timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run *cmd* in a process group of its own, and end the whole group when the
    time is up or this process is stopped: a pytest left running its workers
    would go on reading the mutated file."""
    posix = os.name == "posix"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **env} if env else None, start_new_session=posix)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        if posix:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class Stopped(BaseException):
    """SIGTERM or SIGHUP: the run is being ended from outside."""


def _stop(signum, frame):
    raise Stopped(signal.Signals(signum).name)


def failed_tests(output: str) -> tuple[str, ...]:
    """The test ids pytest's short summary lists as FAILED or ERROR."""
    return tuple(m.group(2) for m in re.finditer(r"^(FAILED|ERROR) (\S+)", output, re.M))


def summary_line(output: str) -> str:
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    return rows[-1] if rows else ""


def run_one(m: Mutant, build: list[str] | None, test: list[str], timeout: float) -> Outcome:
    original = m.file.read_bytes()  # bytes, so the line endings come back as they were
    put(m.file, original.decode("utf-8").replace(m.find, m.replace).encode("utf-8"))  # every occurrence
    try:
        if build:
            try:
                b = run(build, timeout, m.env)
            except subprocess.TimeoutExpired:
                return Outcome(m.name, "BUILD FAILED", f"timed out after {timeout:g} s")
            if b.returncode:
                return Outcome(m.name, "BUILD FAILED", (b.stderr or b.stdout).strip()[-300:])
        t = run(test, timeout, m.env)
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
        put(m.file, original)


_WRITTEN: dict[Path, int] = {}  # the whole second of each file's last write here


def put(path: Path, data: bytes) -> None:
    """Write *path* in a later whole second than it was last written in.

    Python trusts a cached ``.pyc`` whose recorded source time, in whole seconds,
    and size match the source file. A mutant the same size as the original
    (``a + b`` for ``a - b``), written, compiled by a quick test and put back
    inside one second, leaves a cache of the mutant that matches the restored
    file: the source is right and Python goes on running the mutant, for the next
    mutant's test and for everyone after the run. A build tool that compares
    times needs the same: the restored source newer than what was built from the
    mutant. So each write waits for the next second where it has to; a test that
    takes longer than a second costs nothing."""
    last = _WRITTEN.get(path)
    if last is None and path.exists():
        last = int(path.stat().st_mtime)  # the original's own second
    if last is not None:
        wait = last + 1 - time.time()
        if wait > 0:
            time.sleep(wait)
    path.write_bytes(data)
    _WRITTEN[path] = int(path.stat().st_mtime)


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
            put(dest, src.read_bytes())  # a new time: a cache of the mutant must not match
            changed.append(dest)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mutants", nargs="?", type=Path, help="the mutants file (JSON)")
    ap.add_argument("--only", action="append", help="run only the mutants whose name starts with this")
    ap.add_argument("--log", type=Path, default=Path("mutants.log"))
    ap.add_argument("--timeout", type=float, default=1800, help="seconds for each build and test")
    ap.add_argument("--restore", action="store_true", help="restore files from .mutate-backup/ and exit")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run even though a file to mutate has uncommitted changes")
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
    dirty = uncommitted(files)
    if dirty and not a.allow_dirty:
        raise SystemExit(
            "uncommitted changes in " + ", ".join(dirty) + ": commit first, so that a mutant left "
            "behind shows in `git diff` and `git checkout` removes it (or pass --allow-dirty)"
        )

    back_up(files)
    built = False
    stale: tuple[list[str], dict[str, str] | None] | None = None  # a mutant's build is what is built
    stopped = None
    handlers = {}
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                handlers[sig] = signal.signal(sig, _stop)
            except ValueError:  # not the main thread
                pass
    try:
        with a.log.open("w", encoding="utf-8") as log:
            for o in outcomes:
                log.write(o.line() + "\n")
            for m in mutants:
                if m.name in skip:
                    continue
                mbuild = m.build if m.build is not None else build
                if stale and not mbuild:
                    # The last build was of a mutant, and this one has no build
                    # of its own: its test would run what that mutant built.
                    # Three Python mutants after five C++ ones tested nothing.
                    try:
                        ok = run(stale[0], a.timeout, stale[1]).returncode == 0
                    except subprocess.TimeoutExpired:
                        ok = False
                    if not ok:
                        o = Outcome(m.name, "BUILD FAILED", "the original could not be built again")
                        outcomes.append(o)
                        log.write(o.line() + "\n")
                        print(o.line(), flush=True)
                        continue
                    stale = None
                o = run_one(m, mbuild, m.test or test, a.timeout)
                if mbuild:
                    stale = (mbuild, m.env)
                built = built or bool(mbuild)
                outcomes.append(o)
                log.write(o.line() + "\n")
                log.flush()
                print(o.line(), flush=True)
    except (KeyboardInterrupt, Stopped) as e:
        stopped = str(e) or type(e).__name__
    finally:
        for sig in handlers:  # nothing may stop the restore itself
            signal.signal(sig, signal.SIG_IGN)
        restore()
        shutil.rmtree(BACKUP, ignore_errors=True)
        for sig, old in handlers.items():
            signal.signal(sig, old)
    if stopped:
        moved = [str(f) for f in files if digest(f) != before[f]]
        ran = len(outcomes)
        with a.log.open("a", encoding="utf-8") as log:
            log.write(f"INTERRUPTED ({stopped}) after {ran} of {len(mutants)}: not a result\n")
        print(f"\nINTERRUPTED ({stopped}) after {ran} of {len(mutants)} mutants; "
              + ("NOT PUT BACK: " + ", ".join(moved) if moved else "every file put back, byte for byte")
              + ("; the last build was of a mutant, rebuild the original" if built else ""))
        return 130
    if built:  # the last build was of a mutant: build the original again
        rebuild = next(m.build for m in mutants if m.build) if not build else build
        try:
            ok = run(rebuild, a.timeout, next((m.env for m in mutants if m.env), None)).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        print(f"original rebuilt: {'ok' if ok else 'FAILED, rebuild by hand'}")
    moved = [str(f) for f in files if digest(f) != before[f]]
    if moved:
        print("NOT RESTORED: " + ", ".join(moved))
        return 1

    killed = sum(o.verdict == "KILLED" for o in outcomes)
    untested = [o.name for o in outcomes if o.verdict in ("NOT APPLIED", "BUILD FAILED", "TEST ERROR")]
    extra = f"; {len(untested)} tested nothing ({', '.join(untested)})" if untested else ""
    print(f"\n{killed} of {len(outcomes)} killed{extra}; files restored, byte for byte")
    with a.log.open("a", encoding="utf-8") as log:
        log.write(f"restored; {killed} of {len(outcomes)} killed{extra}\n")
    return 0 if killed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
