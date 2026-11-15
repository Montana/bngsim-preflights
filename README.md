# bngsim-preflights

<br><img width="1870" height="841" alt="BNGsim-PREflights simulation library banner" src="https://github.com/user-attachments/assets/763c5ba5-553c-4c34-b46f-f4be0dfbb627" /></br>

These are Pre-flight checks I've made for Los Alamos National Laboratories project: [lanl/bngsim](https://github.com/lanl/bngsim) pull requests.

bngsim's maintainers re-run every claim a pull request makes. When they find a problem, it is rarely the fix itself. It is a test that could not have failed, a sibling code path still wrong, a number in the description that no log supports, or a CI leg that never ran the new tests. This repository turns those review findings into checks that run before a pull request is opened, so the same gap is caught by a script instead of by a reviewer.

| Script | What it does |
|---|---|
| [`prepr_check.py`](prepr_check.py) | The pre-PR gate: 33 checks of a branch, its tests, its evidence and its pull-request description. |
| [`floor_for_branch.py`](floor_for_branch.py) | Reports which path-filtered CI workflows run a branch's new test files, and works out the old `PYTEST_COLLECTED_FLOOR` line. The sketch behind [#1757](https://github.com/lanl/bngsim/issues/1757). |

Both are single files, standard library only. Run them from the root of a bngsim worktree.

## Quick start

```bash
# 1. The branch on its own: fast, no description needed
python prepr_check.py --issue 1740

# 2. Everything, once the description is drafted
python prepr_check.py --issue 1740 --body pr.md \
    --title "convert: replay the actions before generate_network in order (#1740)" \
    --evidence ~/work/ab-1740 --main-tree ~/work/bngsim-main \
    --full --docs-python ~/work/docsvenv/bin/python

# 3. Just before pushing: has anything changed since the gate passed?
python prepr_check.py --body pr.md --check-stamp
```

A run that passes writes a stamp of `HEAD`, the tracked tree and the description. Step 3 exits 1 if any of the three has changed since, so a last-minute edit cannot slip past the gate.

## What a run looks like

From a real run, on the branch for [#1740](https://github.com/lanl/bngsim/issues/1740) (trimmed):

```text
prepr_check  bngsim-1740  HEAD e091207e  base d2d63f8105  3 files changed

PASS  on current main                    merge base is origin/main
PASS  changelog fragment                 changelog.d/1740.fixed.md
PASS  skip reasons declared              2 new skip line(s); test_skip_audit passes
PASS  CI legs table                      wheels.yml: fires (python/bngsim/convert/__init__.py); names no python test files
PASS  no placeholders in the body        none outside code
PASS  body numbers backed by logs        each **N passed** and the mutant count are in 1418 logs
WARN  main build is current main         bngsim-main-ab is at df2821c4, main at d2d63f81: rebuild it
PASS  new tests fail on main (main build) test_net_as_generated_add_before_generation.py: 10 failed, 7 passed
PASS  evidence newer than the code       every suite, Linux, mutant, corpus and timing log is newer than the files it is about
PASS  Linux run covers the touched tests it runs the whole of python/tests
PASS  pre-commit --all-files             passed
PASS  sphinx -W                          passed
WARN  claims to recount                  8 lines with every/all/never/unchanged/identical/only…
                                         ↳ class: false or stale claims

MANUAL (answer each in the PR body):
  [ ] Siblings: every engine/route/loader that reads the same thing ...
  [ ] Cost at scale: paired interleaved timing vs main on the issue's model AND a large one ...

PASS: 0 failed, 3 warnings, 27 passed
stamp written: run with --check-stamp (and the same --body) just before pushing
```

Each line is `PASS`, `FAIL`, `WARN` or `SKIP` with its reason. A `FAIL` or `WARN` also names the lesson it comes from. The run exits 1 if any check fails, and writes no stamp.

## What it checks, and why

Every check exists because the gap it looks for reached a pull request. Some were found by the maintainers in review, and some by the gate's author before review.

### The branch

| Check | It catches | Came from |
|---|---|---|
| On current main | A branch whose base is behind main, where main changed files the branch touches | Evidence measured against an old main |
| Changelog fragment | A library change with no `changelog.d/` entry | CI's fragment check failing after push |
| Skip reasons declared | A new `pytest.skip("...")` whose reason conftest does not declare, which `test_skip_audit.py` fails on | [#1653](https://github.com/lanl/bngsim/issues/1653) |
| No fixed stack level | A warning raised with `stacklevel=2`, which points at bngsim's own code when called through another bngsim function | Review of several earlier pull requests |
| New files tracked | A new file left untracked, so the hooks and the diff skip it | `pre-commit --all-files` reads tracked files only |
| Overlapping pull requests | Open pull requests that edit the same files, whose merge order the description should state | Conflicts found only at merge |

### The tests

| Check | It catches | Came from |
|---|---|---|
| New tests fail on main | A test that passes with or without the fix, and so proves nothing. Each new file is copied into a built main's own tree, run, and removed, so its data and helper imports resolve as they will there | Run from a scratch folder instead, a count overstated how many tests failed on main ([#1712](https://github.com/lanl/bngsim/issues/1712)) |
| CI legs | `mir.yml` and the Windows workflows run fixed test lists on fixed paths, so a new test file can run on none of them | [#1629](https://github.com/lanl/bngsim/pull/1629) review; a draft for #1756 that misjudged which legs fire |
| Linux run covers the touched tests | A Docker Linux run whose hand-picked test list leaves out a file that exercises the change. Reads `docker/run_linux.sh` from the evidence | [#1636](https://github.com/lanl/bngsim/issues/1636): the feature's own test file was left out, and failed only in the full suite |
| New-test hygiene | A `subprocess` call with no timeout, which can hang a CI leg; an Antimony-only fixture; a literal `/dev/null` in C++ | Review findings |
| Built from this tree | A compiled core older than the C++ in the tree, so the tests ran the wrong code | Results from a stale build |

### The description

| Check | It catches | Came from |
|---|---|---|
| No placeholders | A `SUITE_LINE`-style marker left unfilled | A draft that reached the gate with one |
| Template | A title, `## Review` or `## Testing` section not in the form the project's template asks for | The template in [#1746](https://github.com/lanl/bngsim/pull/1746) |
| Numbers backed by logs | A count such as **20,468 passed** that no log in the evidence contains | False or stale claims, the most common review finding |
| Counts match the measured ones | A "fails on main" count that differs from the gate's own run | [#1712](https://github.com/lanl/bngsim/issues/1712) |
| Evidence newer than the code | A suite, mutant or Linux log older than the last edit to what it tested | Results quoted from before a late edit |
| Cites the current main | A commit cited as "main" that main has since moved past | Rebases after the evidence was gathered |
| Claims to recount | Words like *every*, *all*, *never*, *identical*, listed for a manual recount | False or stale claims |

### Once the pull request is open, and the slow ones

| Check | It catches |
|---|---|
| Thread read | A comment or review posted since the last push and not yet answered (`--thread-read` says it was read) |
| `--full` | `pre-commit run --all-files`, and `sphinx -W` with `--docs-python` |

## What it cannot judge

A clean run is not a review. The gate ends with a `MANUAL` list of what no script can decide, each to be answered in the description:

- **Siblings:** does every engine, route and loader that reads the same thing get the fix, or a reason it doesn't need it?
- **Cost at scale:** has the change been timed against main on a large model, not only the issue's small one?
- **Fixtures that can tell:** would a plausible wrong implementation give a different number?

And some checks are heuristics. "Linux run covers the touched tests" matches function names and topic words, so it can name a file that is only loosely related; that is why it warns rather than fails.

## Arguments

| Argument | Meaning |
|---|---|
| `--issue N` | The issue the branch fixes |
| `--body FILE` / `--title TEXT` | The drafted pull-request description and title |
| `--evidence DIR` | A directory of this change's logs: mutants, Docker, suite, timing. Repeatable. The worktree's own `*.log` files are always read |
| `--main-tree DIR` | A built checkout of main; its `.venv` runs the new tests |
| `--main-pkg DIR --boot DIR` | The older route: main's package directory, and the directory with the `sitecustomize` that points pytest's workers at it (or set `$BNGSIM_AB_BOOT`) |
| `--full`, `--docs-python PY` | Also run pre-commit on all files, and `sphinx -W` with that interpreter |
| `--pr N`, `--thread-read` | The open pull request this branch updates, and that its new comments have been read |
| `--check-stamp` | Only compare against the last passing run's stamp |
| `--comment FILE` | Lint a drafted pull-request comment, and exit |
| `--no-gh` | Skip the checks that ask GitHub |

## `floor_for_branch.py`

```bash
python floor_for_branch.py --legs              # the filtered-legs report
python floor_for_branch.py --main-count 20506  # the floor line, with main's count given by hand
```

Since [#1762](https://github.com/lanl/bngsim/pull/1762) holds each test file to main's own count, pull requests no longer edit `ci/test_floors.env`, so the floor arithmetic is history. The `--legs` report is still useful: it says, for each path-filtered workflow, whether the branch triggers it and how many of the branch's new test files it runs. [#1764](https://github.com/lanl/bngsim/issues/1764) proposes the same warning in CI itself.

## `mutate.py`

A test that passes on the fix should fail on every way of getting it wrong. `mutate.py` breaks the fix on purpose, one way at a time, and checks that a test fails each time.

```bash
python mutate.py mutants.json            # every mutant; one line each in mutants.log
python mutate.py mutants.json --only M3  # just one
python mutate.py --restore               # undo a run that was killed outright
```

A mutants file names the test command, an optional build, and each mutant as a `find` and `replace` in one file:

```json
{
  "test": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "python/tests/test_x.py"],
  "mutants": [
    {"name": "M1 the guard never applied", "file": "python/bngsim/x.py",
     "find": "if h < 1:", "replace": "if False:"},
    {"name": "M2 the C++ check dropped", "file": "src/x.cpp", "find": "check(x);", "replace": "",
     "build": ["python", "scripts/rebuild_editable.py"]}
  ]
}
```

Each verdict is one of these:

- `KILLED`: a test failed.
- `SURVIVED`: no test catches that mistake.
- `NOT APPLIED`: `find` does not occur exactly `count` times, usually because the file moved on.
- `BUILD FAILED`: the build step failed.
- `TEST ERROR`: pytest did not run the tests.

Files are copied to `.mutate-backup/` before the first edit and put back after every mutant. When the run ends, it checks that every file is byte-for-byte what it was at the start.

## `linux_check.py`

A fix that depends on rounding can pass on Apple Silicon and still fail on CI's x86-64 Linux. `linux_check.py` copies the worktree (without `.venv`, `build` or `.git`) into an x86-64 container, builds it from source there, and runs the tests.

```bash
python linux_check.py --tests python/tests/test_x.py --out ~/work/ab-123/docker
python linux_check.py --compare-main ~/work/bngsim-main   # sort each failure: the image's or the change's
python linux_check.py --restart-docker                    # if Docker answers but starts no container
python linux_check.py --dry-run                           # print the commands only
```

Before running anything, it checks that a container actually starts: after an update, Docker Desktop can answer `docker info` and still start nothing. With `--compare-main`, each test file that failed is run again on main in the same image. A test that fails there too fails because of the image (no `.git` in the copy, or the root user), not because of the change. The exit status is 0 only when nothing fails that main passes.

## Install

```bash
pipx install git+https://github.com/Montana/bngsim-preflights
```

This installs four commands: `bngsim-prepr`, `bngsim-floor`, `bngsim-mutate` and `bngsim-linux`. Each script also runs on its own with `python <script>.py`.

## Tests

```bash
python -m pip install ".[test]"
python -m ruff check . && python -m pytest -q
```

The tests build small git repositories and projects in temporary directories. Docker is not needed. CI runs them on Linux and macOS, with Python 3.10 and 3.13. The tests were themselves checked with `mutate.py`: 18 mutants of the four scripts, all killed.

## Requirements

- Python 3.10 or later, `git`, and the [`gh`](https://cli.github.com/) CLI for the checks that read GitHub.
- For the test checks, an interpreter with bngsim installed: the worktree's own `.venv` by default, or `--python`.
- For checks that run BNG2.pl, `BNGPATH` set to an absolute path. A relative one breaks the BioNetGen tests that change directory.
