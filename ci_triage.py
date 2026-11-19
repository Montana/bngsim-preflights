"""Why did a CI run go red: the change, or the infrastructure?

    python ci_triage.py https://github.com/lanl/bngsim/actions/runs/44360516767
    python ci_triage.py 44360516767 --job 135000275395

Each job that did not succeed is put in one class, from GitHub's record of it and
its log:

- never started: no runner was assigned, so no step ran and there is no log. The
  run reads "failure" only because the job did not finish.
- superseded: cancelled after a newer run of the same workflow on the same branch
  began, as a concurrency group with cancel-in-progress does.
- timed out: the job hit its time limit.
- infrastructure: the log shows the network, a mirror, the artifact service or the
  runner failing (`Could not resolve host`, apt's `Ign:` lines for the Ubuntu
  mirror, an HTML page where JSON was expected, a lost runner); or, marked
  "likely", the job failed in a setup step (install, checkout, cache, download)
  before any test ran.
- test failure: the log names failing tests (pytest `FAILED`, ctest `***Failed`)
  or a count check's `::error::`.
- unexplained: none of the above; read the log.

Only the last two say anything about the change. A run needs write access to be
re-run (`gh run rerun <id> --failed`), so a fork's author asks a maintainer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

INFRA = [
    (re.compile(r"Could not resolve host|Temporary failure (?:in name )?resolution|Temporary failure resolving"),
     "DNS lookup failed"),
    (re.compile(r"Failed to fetch https?://|Unable to fetch some archives|E: Unable to locate package|"
                r"^Ign:\d+ https?://\S*archive\.ubuntu\.com|apt-get failed or hung", re.M),
     "a package mirror failed"),
    (re.compile(r"Failed to (?:CreateArtifact|upload artifact|FinalizeArtifact)|<!DOCTYPE html>|"
                r"Unexpected token '<'|is not valid JSON"),
     "the artifact service returned an error page"),
    (re.compile(r"The runner has received a shutdown signal|lost communication with the server|"
                r"The hosted runner encountered an error"),
     "the runner was lost"),
    (re.compile(r"\b50[234] (?:Bad Gateway|Service Unavailable|Gateway Time-?out)\b|ECONNRESET|ETIMEDOUT"),
     "a server error or a dropped connection"),
    (re.compile(r"No space left on device"), "the runner ran out of disk"),
    (re.compile(r"API rate limit exceeded"), "a rate limit"),
]
SETUP_STEP = re.compile(r"\b(?:install|set ?up|checkout|cache|download|apt|toolchain|fetch)\b", re.I)
TIMEOUT = re.compile(r"has exceeded the maximum execution time|The job running on runner .* has exceeded")
TESTS = [
    re.compile(r"^(?:\S+\s)?FAILED (\S+)", re.M),          # pytest -rf summary
    re.compile(r"^(?:\S+\s)?ERROR (\S+::\S+)", re.M),      # pytest collection/setup errors
    re.compile(r"\*\*\*Failed\s+.*?(\S+)\s*$", re.M),      # ctest
    re.compile(r"^##\[error\](?!Process completed with exit code)(.*)$", re.M),  # a step's ::error::
]
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s")


@dataclass
class Verdict:
    job: str
    kind: str
    detail: str
    evidence: list[str] = field(default_factory=list)

    @property
    def about_the_change(self) -> bool:
        return self.kind in ("test failure", "unexplained")


def _strip(log: str) -> str:
    return "\n".join(TIMESTAMP.sub("", ln) for ln in log.splitlines())


def classify(job: dict, log: str | None, newer_runs: list[dict] | None = None) -> Verdict:
    """One job (GitHub's REST record), its log (None when there is none), and the runs of
    the same workflow and branch created after this job's run."""
    name = job.get("name", "?")
    steps = job.get("steps") or []
    if not job.get("runner_name") and not steps:
        waited = _minutes(job.get("created_at"), job.get("completed_at"))
        return Verdict(name, "never started",
                       f"no runner was assigned; it waited {waited} and was {job.get('conclusion')}"
                       if waited else "no runner was assigned")
    text = _strip(log or "")
    if TIMEOUT.search(text):
        return Verdict(name, "timed out", "the job hit its time limit", [TIMEOUT.search(text).group(0)])
    if job.get("conclusion") == "cancelled":
        later = [r for r in (newer_runs or []) if r.get("created_at", "") < (job.get("completed_at") or "~")]
        if later:
            r = later[0]
            return Verdict(name, "superseded", f"cancelled when run {r.get('id')} began ({r.get('display_title', '')[:60]})")
    found = []
    for rx in TESTS:
        found += [m.group(1).strip() for m in rx.finditer(text)]
    if found:
        return Verdict(name, "test failure", f"{len(found)} failing", list(dict.fromkeys(found))[:10])
    for rx, why in INFRA:
        m = rx.search(text)
        if m:
            line = next((ln for ln in text.splitlines() if rx.search(ln)), m.group(0))
            return Verdict(name, "infrastructure", why, [line.strip()[:200]])
    if job.get("conclusion") == "cancelled":
        return Verdict(name, "unexplained", "cancelled, with no newer run and no failure in its log; "
                       "most likely cancelled by hand")
    failed = [s.get("name", "?") for s in steps if s.get("conclusion") == "failure"]
    if failed and SETUP_STEP.search(failed[0]):
        return Verdict(name, "infrastructure", f"likely: setup step {failed[0]!r} failed before any test ran",
                       [ln.strip()[:200] for ln in text.splitlines() if "##[error]" in ln][:3])
    return Verdict(name, "unexplained", f"failed at step {failed[0]!r}" if failed else "no failing step named",
                   [ln.strip()[:200] for ln in text.splitlines()[-5:]])


def _minutes(start: str | None, end: str | None) -> str:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return ""
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def summary(verdicts: list[Verdict]) -> str:
    if not verdicts:
        return "every job succeeded"
    if any(v.about_the_change for v in verdicts):
        bad = [v.job for v in verdicts if v.about_the_change]
        return f"look at the change: {', '.join(bad)}"
    return "not the change: " + "; ".join(sorted({v.kind for v in verdicts})) + \
        " (a maintainer can re-run it: gh run rerun <id> --failed)"


# ─── GitHub ──────────────────────────────────────────────────────────────────


def _gh(path: str) -> str | None:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _parse(target: str, repo: str | None) -> tuple[str, str, str | None]:
    m = re.search(r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)(?:/job/(\d+))?", target)
    if m:
        return m.group(1), m.group(2), m.group(3)
    if target.isdigit() and repo:
        return repo, target, None
    raise SystemExit("give a run URL, or a run id with --repo OWNER/NAME")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="a run or job URL, or a run id")
    ap.add_argument("--repo", default="lanl/bngsim")
    ap.add_argument("--job", help="one job id only")
    ap.add_argument("--json", action="store_true", help="print the verdicts as JSON")
    a = ap.parse_args(argv)
    repo, run_id, job_id = _parse(a.target, a.repo)
    job_id = a.job or job_id
    run = json.loads(_gh(f"repos/{repo}/actions/runs/{run_id}") or "{}")
    if not run:
        print(f"run {run_id} not found in {repo}", file=sys.stderr)
        return 2
    jobs = json.loads(_gh(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100") or "{}").get("jobs", [])
    later = json.loads(_gh(f"repos/{repo}/actions/workflows/{run.get('workflow_id')}/runs?"
                           f"branch={run.get('head_branch')}&per_page=20") or "{}").get("workflow_runs", [])
    newer = sorted((r for r in later if r.get("created_at", "") > run.get("created_at", "")),
                   key=lambda r: r["created_at"])
    verdicts = []
    for job in jobs:
        if job_id and str(job.get("id")) != str(job_id):
            continue
        if job.get("conclusion") in ("success", "skipped", "neutral") and not job_id:
            continue
        log = _gh(f"repos/{repo}/actions/jobs/{job['id']}/logs") if job.get("runner_name") else None
        verdicts.append(classify(job, log, newer))
    if a.json:
        print(json.dumps([v.__dict__ | {"about_the_change": v.about_the_change} for v in verdicts], indent=1))
    else:
        print(f"{run.get('name')} · {run.get('display_title', '')[:70]} · {run.get('head_sha', '')[:8]} "
              f"· {run.get('conclusion') or run.get('status')}")
        for v in verdicts:
            print(f"  {v.kind:15s} {v.job}: {v.detail}")
            for e in v.evidence[:5]:
                print(f"  {'':15s}   {e}")
        print(summary(verdicts))
    return 1 if any(v.about_the_change for v in verdicts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
