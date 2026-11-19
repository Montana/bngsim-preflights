#!/usr/bin/env python3
"""Run a bngsim worktree's tests on x86-64 Linux in Docker, and sort what fails.

    python linux_check.py                            # the whole of python/tests
    python linux_check.py --tests python/tests/test_x.py python/tests/test_y.py
    python linux_check.py --compare-main ~/work/bngsim-main
    python linux_check.py --dry-run                  # print what would run

A fix that depends on rounding can pass on Apple Silicon and fail on CI's
x86-64 Linux. This builds the worktree from source in a container and runs its
tests there. The worktree is copied in, without `.venv`, `build`, `.git` or
logs, so the container's build is its own.

Before running, it checks that a container actually starts, not only that
Docker answers: after an update, Docker Desktop on macOS can answer `docker
info` and start no container. With `--restart-docker` it then quits Docker
Desktop by its full name, stops its backend, opens it again, and probes once
more.

With `--compare-main DIR`, the test files that failed are run again on a
checkout of main, in the same image. A test that fails on main too fails
because of the image (a git checkout the copy leaves out, the container's
root user), not because of the change; one that fails only here is the
change's. The exit status is 0 only if nothing fails that main passes.

The image needs Python, a C and C++ toolchain, and `uv`. Each run writes, under
`--out`: `build.log`, `pytest.log`, and `linux.log`, which the pre-PR gate
reads.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

IMAGE = "bngsim-linux-amd64:latest"
PLATFORM = "linux/amd64"
EXCLUDE = (".venv", "build", ".git", "*.log", "__pycache__", ".mutate-backup")


@dataclass
class Run:
    summary: str
    failed: list[str] = field(default_factory=list)


def container_script(tests: list[str], workers: int) -> str:
    """The script the container runs: build the tree, then pytest the tests."""
    selected = " ".join(shlex.quote(t) for t in tests) or "python/tests"
    return "\n".join(
        [
            "#!/bin/bash",
            "set -euo pipefail",
            "cd /src",
            # 2 compiles at once: ExprTk's translation unit alone takes several GB,
            # and 3 ran cc1plus out of memory in Docker Desktop's default 8 GB VM.
            "export UV_LINK_MODE=copy CMAKE_BUILD_PARALLEL_LEVEL=2 SUNLOGGER_WARNING_FILENAME=/dev/null",
            "uv sync --frozen --extra test > /out/build.log 2>&1 || { tail -50 /out/build.log; exit 1; }",
            '.venv/bin/python -c "import bngsim, platform; '
            'print(bngsim.__version__, platform.machine(), platform.libc_ver())"',
            f".venv/bin/python -m pytest -q -p no:cacheprovider -n {workers} -rf {selected} "
            "2>&1 | tee /out/pytest.log | grep -E '^FAILED|passed|failed' | tail -30",
            "",
        ]
    )


def parse(output: str) -> Run:
    """The summary line and the failed test ids of a pytest run's output."""
    failed = [m.group(1) for m in re.finditer(r"^FAILED (\S+)", output, re.M)]
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return Run(rows[-1] if rows else "", list(dict.fromkeys(failed)))


def classify(here: list[str], on_main: list[str]) -> tuple[list[str], list[str]]:
    """(failures the image causes, failures the change causes): a test that fails
    on main in the same image is the image's."""
    main = set(on_main)
    return [t for t in here if t in main], [t for t in here if t not in main]


def failed_files(tests: list[str]) -> list[str]:
    return sorted({t.split("::", 1)[0] for t in tests})


def sh(cmd: list[str], timeout: float, dry: bool) -> subprocess.CompletedProcess:
    if dry:
        print("+ " + shlex.join(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def container_starts(image: str, platform: str, dry: bool) -> bool:
    try:
        r = sh(["docker", "run", "--rm", "--platform", platform, image, "uname", "-m"], 90, dry)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def restart_docker_desktop(dry: bool) -> None:
    """Quit Docker Desktop by its full name, stop its backend, open it again."""
    for cmd in (
        ["osascript", "-e", 'quit app "Docker Desktop"'],
        ["pkill", "-f", "com.docker.backend"],
        ["pkill", "-f", "Docker Desktop"],
        ["open", "-a", "Docker Desktop"],
    ):
        try:
            sh(cmd, 30, dry)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        if not dry:
            time.sleep(5)
    for _ in range(60):
        if dry or sh(["docker", "info"], 10, dry).returncode == 0:
            return
        time.sleep(4)


def copy_tree(src: Path, dest: Path, dry: bool) -> None:
    cmd = ["rsync", "-a", "--delete"]
    for pattern in EXCLUDE:
        cmd += ["--exclude", pattern]
    sh(["mkdir", "-p", str(dest)], 30, dry)
    sh(cmd + [f"{src}/", f"{dest}/"], 600, dry)


def run_in_docker(tree: Path, out: Path, tests: list[str], a: argparse.Namespace) -> Run:
    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    src = out / "src"
    copy_tree(tree, src, a.dry_run)
    script = out / "run_linux.sh"
    if a.dry_run:
        print(f"# {script}:\n{container_script(tests, a.workers)}")
    else:
        script.write_text(container_script(tests, a.workers), encoding="utf-8")
    cmd = [
        "docker", "run", "--rm", "--platform", a.platform,
        "-v", f"{src}:/src", "-v", f"{out}:/out", "-v", f"{script}:/run_linux.sh",
        a.image, "bash", "/run_linux.sh",
    ]  # fmt: skip
    r = sh(cmd, a.timeout, a.dry_run)
    if a.dry_run:
        return Run("")
    (out / "linux.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    pytest_log = out / "pytest.log"
    return parse(pytest_log.read_text(encoding="utf-8", errors="replace") if pytest_log.is_file() else r.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", type=Path, default=Path("."), help="the worktree (default: here)")
    ap.add_argument("--tests", nargs="*", default=[], help="test files or ids (default: python/tests)")
    ap.add_argument("--out", type=Path, default=Path("linux-check"), help="where the logs go")
    ap.add_argument("--compare-main", type=Path, help="a checkout of main, to rerun each failure on")
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--platform", default=PLATFORM)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=5400, help="seconds for the container run")
    ap.add_argument("--restart-docker", action="store_true",
                    help="if no container starts, restart Docker Desktop (macOS) and probe again")
    ap.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    a = ap.parse_args(argv)

    tree, out = a.tree.resolve(), a.out.resolve()
    if not container_starts(a.image, a.platform, a.dry_run):
        if not a.restart_docker:
            print(f"no container of {a.image} starts; pass --restart-docker, or restart Docker by hand")
            return 2
        print("no container starts; restarting Docker Desktop")
        restart_docker_desktop(a.dry_run)
        if not container_starts(a.image, a.platform, a.dry_run):
            print("still no container starts: look for a dialog from Docker Desktop")
            return 2

    here = run_in_docker(tree, out, a.tests, a)
    if a.dry_run:
        return 0
    print(f"linux: {here.summary or 'no pytest summary (see linux.log)'}")
    if not here.summary:
        return 1
    if not here.failed:
        return 0
    for t in here.failed:
        print(f"  FAILED {t}")
    if a.compare_main is None:
        print("pass --compare-main DIR to tell the image's failures from the change's")
        return 1
    main_run = run_in_docker(a.compare_main.resolve(), out / "main", failed_files(here.failed), a)
    image, change = classify(here.failed, main_run.failed)
    print(f"main, those files: {main_run.summary}")
    print(f"{len(image)} fail on main in the same image too (the image's); {len(change)} fail only here")
    for t in change:
        print(f"  ONLY HERE {t}")
    return 1 if change else 0


if __name__ == "__main__":
    sys.exit(main())
