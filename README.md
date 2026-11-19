# bngsim-preflights

<br><img width="1870" height="841" alt="BNGsim-PREflights simulation library banner" src="https://github.com/user-attachments/assets/763c5ba5-553c-4c34-b46f-f4be0dfbb627" /></br>

These are Pre-flight checks I've made for Los Alamos National Laboratories project: [lanl/bngsim](https://github.com/lanl/bngsim) pull requests.

bngsim's maintainers re-run every claim a pull request makes. When they find a problem, it is rarely the fix itself. It is a test that could not have failed, a sibling code path still wrong, a number in the description that no log supports, or a CI leg that never ran the new tests. This repository turns those review findings into checks that run before a pull request is opened, so the same gap is caught by a script instead of by a reviewer.

| Script | What it does |
|---|---|
| [`prepr_check.py`](prepr_check.py) | The pre-PR gate: 37 checks of a branch, its tests, its evidence and its pull-request description. |
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

### From a review of all 18 open pull requests

A code review of every open pull request at once gave 161 candidate findings, and real regressions in 9 of the 18. Eleven of its lessons, and of the reviews after it, can be checked by a script:

| Check | It catches | From |
|---|---|---|
| No one else pushed | A commit on the pull request's branch that this branch lacks, such as a reviewer's fix. A push from here would be refused, or would drop their work if forced. **FAIL** | #1772 |
| Untouched readers | Each function the branch changes, with its callers in the package that the branch leaves alone. Each needs a run on the issue's model. | #1763 |
| Nothing newer than Python 3.10 | `locale.getencoding`, `tomllib`, `datetime.UTC`, `StrEnum`, `typing.Self`, `except*` and others, unless behind a version check. **FAIL** | #1775 |
| Model math read as the engine reads it | A new `lambdify`, `.evalf()`, `np.rint`/`np.mod`/`np.round` or `% 1` in the package. numpy rounds a half to even and floors `mod`; the engine does neither. | #1800 |
| Every new test can fail | A new test whose every assertion is negative (`not x`, `== []`, `not in`), which passes whether or not the code ran | #1770, #1763 |
| BNGPATH resolved | A new test that runs BNG2.pl with `cwd=` and does not resolve `BNGPATH`: a relative one errors instead of skipping | #1768, #1765, #1774 |
| Changelog claims hold at every site | Each code name in the changelog fragment, and the files holding it that the branch leaves untouched | #1756 |
| Parse bindings before the model's names | A `local_dict.update(...)` added after the model's own names, which shadows a parameter of that name | #1809 |
| `%` spelled before a new parse | A new `parse_expr`/`sympify` with no `_spell_mod` in reach: sympy reads `%` as its floored `Mod` and folds it | #1809, #1815 |
| Review covers the head | Code changed after the last commit a reported `/code-review` round read: run a round on the fixes, or say they were not reviewed | #1809, #1791 |
| A writer change is checked on a corpus | A change to a converter writer with no evidence log counting identical files over a corpus, main against the branch | #1791 |

Run against two of those pull requests as first pushed, the Python 3.10 check fails #1775 for `locale.getencoding`, as the review found. The readers check warns on #1763 but not about the right functions: its regressions came from readers of a function whose *answer* changed while its code did not, which no text search can see.

### From a week of fixes: #1815, #1821 and two NFsim patches

Each of these happened, and was caught by eye or by luck.

| Check | It catches | From |
|---|---|---|
| Stacked on another open PR | A branch that holds another open pull request's commits. The description must name it, the diff should be read against it (`--base`), and `--main-tree` must be a build of *its* head: on main, a test can fail for what the pull request underneath fixes, and the fix is then credited to the wrong change | #1815 on #1809: a switch-time fix claimed for #1815 was #1809's |
| No private paths | A home directory (`/Users/<name>/`, `/home/<name>/`, `C:\Users\<name>\`, or this machine's own home under any root) or a tool's working directory, in an added line or in the description. Placeholders such as `/home/you/` and `/home/runner/` pass | #1821: the vendoring script recorded the path of its checkout in `VENDOR.json` |
| Vendored NFsim tree is the carry queue's | When `third_party/nfsim`, the patch series or the vendoring script changes: runs `scripts/vendor_nfsim.py --verify-clean --strict`, compares the patch files with the queue in `VENDOR.json`, and refuses a recorded checkout other than the agreed one. A tripwire that could not run is a failure, not a pass | #1821: the tripwire found a mutant an interrupted run had left in the vendored tree |
| No wall-clock bound in a new test | An added `assert elapsed < 2.0`, or any bound on a measured time, in a test file that reads a clock. It fails on a busy runner and passes on a rerun | Two red CI legs in one week that were timing tests, not bugs |
| A mutant that tested nothing | A `NOT APPLIED`, `BUILD FAILED` or `TEST ERROR` line in a mutant log, or a run that ends `INTERRUPTED`: the count in the description is then not a count | Three mutants file entries gone stale in one week, each reading as a result until the log was opened |

### From the encoding and referee work: #1502 and #1653

| Check | It catches | From |
|---|---|---|
| Text I/O names its encoding (WARN) | An added `open()` in text mode, `read_text()`, `write_text()` or text-mode temporary file in `python/bngsim/` with no `encoding=`. Text then goes in the locale's encoding: UTF-8 on macOS and Linux, cp1252 on Windows. A call that spans lines is read whole; calls already on main are left alone | #1653: `write_net` wrote `Aµ()` as a byte the loader refuses, and could not write `AΩ()` at all; #1665 |
| Referee constants hold their tolerance (WARN) | A measured-looking value (five or more significant digits) whose last digit is worth more than a tenth of the tolerance it is compared under, in `pytest.approx` or `assert_allclose`, named constants and parametrize tables included. A table is held to the loosest tolerance among the comparisons that read a variable | #1502: run_network's `0.896350` under `rel=1e-6` is 5.6e-7 off by rounding alone, so a 2.3x margin read as a pass |

### From three cloud reviews: #1751, #1753 and #1758

| Check | It catches | From |
|---|---|---|
| Asserted messages hold on every route (WARN) | A new test that asks for words of a message the code fills in at run time (`<< x <<`, `+ x +`, an f-string's `{x}`), where the code also has a longer value for that piece. A `match=` pattern is read up to its first special character, so one written for both routes (`The (compiled )?RHS`) passes; a message written whole, adjacent literals joined, is never flagged | #1751: a test asked for "The RHS returned a non-finite value"; the code writes `<< w.source << " returned ..."`, and a compiled run's `w.source` is "The compiled RHS". It failed under forced cc and forced MIR, and CI's `mir` leg does not run that file |

Tried on the commits the reviews flagged and on the fixes: it warns on #1751's flagged commit and passes its fix, #1753's and #1758's commits, and 18 other branches, including 15 old ones that count about 1,100 of main's files as added. Eleven mutants of the check, all killed.

The other two findings could not be checked reliably, so they are in the `MANUAL` list. #1753's copy of the schedule shift used other names and operators (`shifted.t_start = times_in.t_start - origin` against `elapsed.t_start -= o`), and a version that renames identifiers also flagged #1751's fix, whose sibling call sites look alike on purpose. #1758's sympy parse ran inside a callable the loop was handed, which a line-based check cannot follow.

## What it cannot judge

A clean run is not a review. The gate ends with a `MANUAL` list of what no script can decide, each to be answered in the description:

- **Siblings:** does every engine, route and loader that reads the same thing get the fix, or a reason it doesn't need it?
- **Cost at scale:** has the change been timed against main on a large model, not only the issue's small one?
- **Fixtures that can tell:** would a plausible wrong implementation give a different number?
- **Other spellings:** does the fix hold for every way a model writes the same condition: the operator flipped, the bound on the left, `abs()` and squares, `<=` beside `<`? (#1758)
- **Nearest case main got right:** has a new keep, refuse or carry condition been run on its closest neighbour that main handled? (#1751)
- **Routes kept alike:** does a step two routes both take use one helper, or does the description say why not? (#1753)
- **A loop of costly steps:** for a loop whose step parses or evaluates sympy, even through a callable, what is its worst-case count, and how long does that case take against main? (#1758)
- **Every input a gate lists:** for a gate on what a run is handed, is every field listed and tested alone? (#1753)
- **State kept between runs:** does a second run on the same Simulator, a run on a new one, and the same run cut into `run_until` legs (warm and cold path) give the first run's numbers? (#1721, #1826)
- **Scope words tested:** is every "only X reaches it" or "runs wherever it ran on main", in the description, the changelog or a surviving mutant's excuse, backed by a run, or cut? (#1721, #1763, #1751, #1753) The check `scope claims backed by a run` lists the candidates: reach and comparison phrases such as "wherever", "I found no run that…", "covered by the others", "as on main", and a survivor explained away.

And some checks are heuristics. "Linux run covers the touched tests" matches function names and topic words, so it can name a file that is only loosely related; that is why it warns rather than fails.

## Arguments

| Argument | Meaning |
|---|---|
| `--issue N` | The issue the branch fixes |
| `--body FILE` / `--title TEXT` | The drafted pull-request description and title |
| `--evidence DIR` | A directory of this change's logs: mutants, Docker, suite, timing. Repeatable. The worktree's own `*.log` files are always read |
| `--main-tree DIR` | A built checkout of main, or, for a stacked branch, of the head of the pull request underneath; its `.venv` runs the new tests |
| `--base REF` | What the branch's diff is read against (default `origin/main`); for a stacked branch, the head underneath |
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
python mutate.py mutants.json --timeout 300   # seconds for each build and each test
```

A mutants file names the test command, an optional build, and each mutant as a `find` and `replace` in one file:

```json
{
  "test": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "python/tests/test_x.py"],
  "mutants": [
    {"name": "M1 the guard never applied", "file": "python/bngsim/x.py",
     "find": "if h < 1:", "replace": "if False:"},
    {"name": "M2 the C++ check dropped", "file": "src/x.cpp", "find": "check(x);", "replace": "",
     "build": ["python", "scripts/rebuild_editable.py"]},
    {"name": "M3 every call site", "file": "src/x.cpp", "find": "hold(r)", "replace": "(void)r", "count": "all",
     "build": ["python", "scripts/rebuild_editable.py"], "env": {"BNGPATH": "/abs/path/to/bng"}}
  ]
}
```

`count` is how many times `find` must occur (default 1), or `"all"` for however many there are. A mutant's own `build`, `test` and `env` override the file's, so one file can hold Python mutants and C++ mutants that need a rebuild.

Each verdict is one of these:

- `KILLED`: a test failed.
- `SURVIVED`: no test catches that mistake.
- `NOT APPLIED`: `find` does not occur exactly `count` times, usually because the file moved on; or `replace` is `find`.
- `BUILD FAILED`: the build step failed, or ran out of time.
- `TEST ERROR`: pytest did not run the tests.

The last three tested nothing. They fail the run, the log's last line names them, and the gate refuses a description that counts them.

A mutant is a deliberate bug, so the run is built not to leave one behind:

- **It will not start on uncommitted changes** to a file it would edit (`--allow-dirty` overrides). With the work committed, `git diff` shows a leftover and `git checkout` removes it.
- **Every file is put back after its mutant, and on the way out whatever ends the run:** an exception, Ctrl-C, or a terminal or calling tool stopping it (SIGTERM, SIGHUP). An interrupted run's log ends with `INTERRUPTED`, not with a count.
- **Files are copied to `.mutate-backup/` before the first edit,** so a run killed outright can be undone with `--restore`, and the next run refuses to start until it is.
- **Each build and test has a time limit** (`--timeout`, default 1800 s). A test that runs out is counted as killed, since a hang is a failure a test would show; the whole process group is ended, so a test runner's workers do not go on reading the mutated file.
- **A cached `.pyc` of a mutant cannot pass for the original.** Python trusts a cache whose recorded source time, in whole seconds, and size match the source. A mutant the size of the original (`a - b` for `a + b`), compiled by a quick test and put back within the second, leaves exactly that: the source is right and Python goes on running the mutant. Each write therefore lands in a later second than the last, which also keeps a build tool's timestamps in order.
- **At the end it checks that every file is byte-for-byte what it was at the start.**

## `linux_check.py`

A fix that depends on rounding can pass on Apple Silicon and still fail on CI's x86-64 Linux. `linux_check.py` copies the worktree (without `.venv`, `build` or `.git`) into an x86-64 container, builds it from source there, and runs the tests.

```bash
python linux_check.py --tests python/tests/test_x.py --out ~/work/ab-123/docker
python linux_check.py --compare-main ~/work/bngsim-main   # sort each failure: the image's or the change's
python linux_check.py --restart-docker                    # if Docker answers but starts no container
python linux_check.py --dry-run                           # print the commands only
```

Before running anything, it checks that a container actually starts: after an update, Docker Desktop can answer `docker info` and still start nothing. With `--compare-main`, each test file that failed is run again on main in the same image. A test that fails there too fails because of the image (no `.git` in the copy, or the root user), not because of the change. The exit status is 0 only when nothing fails that main passes.

**The image.** `linux_check.py` runs `bngsim-linux-amd64:latest` (`--image` picks another). That image is defined and built in [bngsim-docker](https://github.com/Montana/bngsim-docker):

```bash
git clone https://github.com/Montana/bngsim-docker && cd bngsim-docker
python bngsim_docker.py build      # bngsim-linux-amd64:latest, the image linux_check.py runs
```

bngsim-docker also has what `linux_check.py` does not:

| | `linux_check.py` | [bngsim-docker](https://github.com/Montana/bngsim-docker) |
|---|---|---|
| Debian 12 x86-64 image | runs it | defines and builds it |
| Ubuntu 26.04 image, set up as bngsim's CI (`ubuntu-latest`) | | `--kind ubuntu` |
| BioNetGen image, so the BNG2.pl tests run | | `--kind bionetgen` |
| arm64 | | `--arch arm64` |
| Failures sorted against main | `--compare-main` | `compare`, or `test` against a list of standing failures |
| Compiler cache between runs | | a ccache volume per image: a second run of bngsim main (build plus one test file) took 90 s, against 483 s cold |
| Docker Compose services | | `suite`, `suite-ubuntu`, `suite-bionetgen`, `shell` |

## `kink_check.py`

bngsim evaluates model math in two kinds of place: the engine (ExprTk, in C++) computes the run, and Python readers built on sympy and numpy compute what the run needs beside it, such as a crossing's threshold, a sensitivity seed, or the symbolic rate law the sensitivity RHS is built from. Where a reader defines a function otherwise than the engine, the run is right and the number beside it is wrong, with no error. `kink_check.py` evaluates expressions at their kinks (halves, quarters, zero and their negatives, where rounding and modulo part ways) through the engine and through each reader, and lists every disagreement.

```bash
python kink_check.py --python ~/work/bngsim-x/.venv/bin/python "rint(p)" "mod(p, 2)" "frac(p)" "p % 2"
python kink_check.py --points=-2.5,-0.5,0.5 "frac(p)*k"
```

```
bngsim 0.17.2: 65 evaluations
82 disagreement(s) with the engine:
  expression                     p  reader               engine   reader gives
  rint(p)                      2.5  seed                    3.0            2.0
  mod(p, 2)                   -0.5  seed                   -0.5            1.5
  frac(p)                     -0.5  derived value          -0.5            0.5
  ...
```

That is main before #1809: the bugs of #1800, found in one run. On #1809's branch the seed and derived-value readers agree everywhere, and what is left is in the rate-law parse, #1815's. The probe runs with the worktree's interpreter, so the script needs no bngsim of its own. Exit status 1 if any reader disagrees.

## `const_check.py`

`kink_check.py` evaluates the expressions you give it. `const_check.py` writes them: constant expressions, which a parser folds whole before anything else reads them, and that fold is where a reader parts from the engine. It builds them from operands that sit where the definitions differ (halves, negatives, `0.1`, a whole number with and without a point, a 17-digit literal, a whole number past 2^53, and constants that are no literal such as `_pi` and `sqrt(2)`) and runs them through the same four evaluations.

```bash
python const_check.py --python ~/work/bngsim-x/.venv/bin/python     # 600 expressions, seed 1
python const_check.py --functions % frac --count 2000 --seed 7
python const_check.py --list --count 20                              # just print the expressions
```

```
bngsim 0.17.2: 600 constant expressions over %, mod, frac, rint, round, floor, ceil, trunc
161 expression(s) folded to another number by a reader (347 disagreements):
     83  derived value  %
     83  rate law       %
     75  seed           %
     57  seed           mod
     13  derived value  frac
  ...
```

That is main. With #1809 and #1815 it is 2 expressions, both a product of two 17-digit literals under `%`, where sympy multiplies at more than a double's precision before the fold. The same seed writes the same expressions, so a count can be compared before and after a fix. A reader that declines or raises is listed and is not a disagreement. Exit status 1 if any reader gives a number the engine does not.

## `ci_triage.py`

Why did a CI run go red: the change, or the infrastructure? Give it a run or job URL:

```bash
python ci_triage.py https://github.com/lanl/bngsim/actions/runs/44360516767
python ci_triage.py 44360516767 --job 135000275395 --repo lanl/bngsim
```

```
wheels · Merge pull request #1823 from Montana/fix/1822-coinciding-switch-rema… · a61c9c6d · failure
  never started   wheels · macos-15: no runner was assigned; it waited 2 h 55 min and was cancelled
not the change: never started (a maintainer can re-run it: gh run rerun <id> --failed)
```

Each job that did not succeed is put in one class, from GitHub's record of it and its log:

| Class | How it is known | About the change? |
|---|---|---|
| never started | no runner was assigned, and no step ran | no |
| superseded | cancelled after a newer run of the same workflow and branch began | no |
| timed out | the log says the job exceeded its time limit | no |
| infrastructure | DNS (`Could not resolve host`, `Temporary failure resolving`), an Ubuntu mirror (apt's `Ign:` lines, `Failed to fetch`), the artifact service's HTML error page, a lost runner, a full disk; or, marked *likely*, a failed setup step (install, checkout, cache, download) before any test ran | no |
| test failure | pytest's `FAILED` and `ERROR` lines, ctest's `***Failed`, or a step's `##[error]`, quoted | **yes** |
| unexplained | none of the above, with the log's last lines | **yes** |

It was checked against the runs it was written from: the 2026-11-11 runner DNS outage (every job infrastructure, the LAPACK legs' apt timeout among them), #1633's artifact-service error, #1640's NFsim pass floor (a test failure, quoting the floor's message), and #1823's macOS wheel that never got a runner. Exit status 1 when a job points at the change. Re-running a run needs write access to the repository, so a fork's author asks a maintainer. Needs `gh`, logged in.

## Install

```bash
pipx install git+https://github.com/Montana/bngsim-preflights
```

This installs seven commands: `bngsim-prepr`, `bngsim-floor`, `bngsim-mutate`, `bngsim-linux`, `bngsim-kinks`, `bngsim-consts` and `bngsim-triage`. Each script also runs on its own with `python <script>.py`.

## Tests

```bash
python -m pip install ".[test]"
python -m ruff check . && python -m pytest -q
```

The tests build small git repositories and projects in temporary directories. Docker is not needed. CI runs them on Linux and macOS, with Python 3.10 and 3.13. The tests were themselves checked with `mutate.py`: 18 mutants of the four scripts, and 52 of the checks and safeguards added since, all killed.

## Requirements

- Python 3.10 or later, `git`, and the [`gh`](https://cli.github.com/) CLI for the checks that read GitHub.
- For the test checks, an interpreter with bngsim installed: the worktree's own `.venv` by default, or `--python`.
- For checks that run BNG2.pl, `BNGPATH` set to an absolute path. A relative one breaks the BioNetGen tests that change directory.
- For `linux_check.py`, Docker and the `bngsim-linux-amd64:latest` image, built from [bngsim-docker](https://github.com/Montana/bngsim-docker).
