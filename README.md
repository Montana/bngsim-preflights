# bngsim-preflights

Checks to run on a [lanl/bngsim](https://github.com/lanl/bngsim) branch before opening a pull request. Each one encodes a finding the maintainers made in review of an earlier pull request, so that the same gap is caught before review rather than in it.

Two scripts, standard library only, run from the root of a bngsim worktree:

| Script | What it does |
|---|---|
| `prepr_check.py` | The pre-PR gate: about thirty checks of the branch, its tests and its pull-request description. |
| `floor_for_branch.py` | What a branch's `PYTEST_COLLECTED_FLOOR` line in `ci/test_floors.env` should read, and which path-filtered CI workflows run its new test files. The sketch behind [lanl/bngsim#1757](https://github.com/lanl/bngsim/issues/1757). Since #1762 replaced the floor with per-file counts, its `--legs` report is the part still in use. |

## `prepr_check.py`

```bash
# the branch alone
python prepr_check.py --issue 1660

# everything, before opening a pull request
python prepr_check.py --issue N --body pr.md --title "part: change (#N)" \
    --evidence ~/work/ab-N --main-tree ~/work/bngsim-main \
    --full --docs-python ~/work/docsvenv/bin/python

# just before pushing: did anything change since the gate passed?
python prepr_check.py --body pr.md --check-stamp
```

Every check prints `PASS`, `FAIL`, `WARN` or `SKIP` with its reason, and for a `FAIL` or `WARN` the lesson it comes from. The run exits 1 if any check fails. A run that passes writes a stamp of `HEAD`, the tracked tree and the description; `--check-stamp` exits 1 if any of the three has changed since.

What it checks, in groups:

- **The branch.** It is on the current main; nothing staged deletes a file; a changelog fragment exists for a library change; no new file is left untracked; warnings use the caller's stack level; a new skip's reason is declared; clang-format on the C++ tests; open pull requests that edit the same files.
- **The tests.** The new tests fail on a build of main (`--main-tree`, which copies each new file into main's own tree, so its data and helper imports resolve as they will there); new-test hygiene (a subprocess with no timeout, an Antimony-only fixture, a literal null device); which path-filtered CI workflows run the new files; whether the Linux run in the evidence covers the test files that exercise what the change touches.
- **The description** (`--body`). No placeholder is left; the title and sections follow the project's pull-request template; every count in it matches a log in the evidence; each log is newer than the code it ran; the main commit it cites is the current one; the open pull requests it shares files with are named; overclaiming words are listed for a recount.
- **The pull request, once open.** Every comment and review since the last push has been read (`--thread-read`).
- **The slow ones** (`--full`). `pre-commit run --all-files`, and `sphinx -W` with `--docs-python`.

A closing `MANUAL` list names what no script can decide (sibling code paths, cost at scale, fixtures that can tell right from wrong), to be answered in the description.

### Arguments worth knowing

- `--evidence DIR` (repeatable): a directory of this change's logs, such as mutants, Docker and suite runs. The worktree's own `*.log` files are always read.
- `--main-tree DIR`: a built checkout of main, whose `.venv` runs the new tests.
- `--main-pkg DIR --boot DIR`: the older A/B route, a package directory of main's code and the directory holding the `sitecustomize` that points pytest's workers at it (or set `BNGSIM_AB_BOOT`).
- `--no-gh`: skip the checks that ask GitHub.
- `--comment FILE`: lint a drafted pull-request comment and exit.

## `floor_for_branch.py`

```bash
python floor_for_branch.py                     # main's count read from CI (needs gh)
python floor_for_branch.py --main-count 20506  # or given by hand
python floor_for_branch.py --legs              # also the filtered-legs report
```

Nothing is written; the lines to paste are printed.

## Requirements

Python 3.10 or later, `git`, and the `gh` CLI for the checks that read GitHub. The test checks need an interpreter with bngsim installed, the worktree's own `.venv` by default, or `--python`. Checks that run BNG2.pl need `BNGPATH` set to an absolute path.
