"""Sweep for ready Copilot PRs and send a Telegram approval card for each.

This is the second half of the human-in-the-loop build trigger. autoRefine files an idea
→ a 👍 assigns the Copilot coding agent → Copilot opens a PR. This sweep watches those PRs
and, once one is ready (not a draft) and its CI is green, sends a Telegram card. nauroBot
turns a 👍 tap into an *approve + squash-merge* and a 👎 into a *close* (see nauroBot's
``handlers._approve_pr`` / ``_decline_pr``).

The sweep is deliberately near-read-only: the only writes it makes to GitHub are the
``pr-card-sent`` / ``pr-blocked-card-sent`` labels, so a PR is carded exactly once. The
approve/merge/close all happen in nauroBot on the tap — never here — so this can run on a
cheap cron with just a PAT.

**An empty ``statusCheckRollup`` is not evidence that CI is green.** The rollup is built
from check runs, and a workflow run that never produced a job produces no check run — so a
run GitHub is holding for approval (``conclusion: action_required``, which is the default
for Copilot's own PRs in a repo with CI) is invisible in the rollup, exactly like a repo
with no CI at all. Reading that as green sends a card claiming "CI is green" for a PR whose
CI has never run, and a 👍 on it squash-merges untested code. When the rollup is empty the
sweep therefore asks the Actions API what ran on the head SHA — see ``_runs_verdict``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from agent.notify import send_pr_blocked_card, send_pr_card

log = logging.getLogger("autorefine.pr_cards")

# The sweep marks a PR carded with this label so it isn't re-carded on the next run.
PR_CARD_SENT_LABEL = "pr-card-sent"

# Same idea for the "your workflows need approving" nudge, kept separate so a PR can be
# nudged while blocked and still get a real ready-card once a human unblocks it.
PR_BLOCKED_CARD_SENT_LABEL = "pr-blocked-card-sent"

# Conclusions/states that count as "done and fine" — anything else (or an unfinished
# check) means CI isn't green yet.
_GOOD_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

# A workflow run GitHub is holding until a human presses "Approve and run workflows".
# This is a literal Actions API conclusion, not a heuristic over run or step names.
_ACTION_REQUIRED = "ACTION_REQUIRED"

# Verdicts from _runs_verdict.
GREEN = "green"
BLOCKED = "blocked"
NOT_GREEN = "not-green"
UNKNOWN = "unknown"


def _checks_green(rollup: list[dict]) -> bool:
    """True when every check in a PR's ``statusCheckRollup`` has finished successfully.

    A check still running (``status`` != COMPLETED), a non-success ``conclusion`` (check
    runs), or a non-SUCCESS ``state`` (legacy statuses) all block.

    An empty rollup returns True, but **that is not evidence of green** — see
    ``_runs_verdict``. The rollup is built from check runs, and a workflow run that never
    produced a job produces no check run, so a gated or startup-failed run is invisible
    here. Callers must treat an empty rollup as "no opinion" and ask ``_runs_verdict``.
    """
    for check in rollup or []:
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        if status and status != "COMPLETED":
            return False
        if conclusion and conclusion not in _GOOD_CONCLUSIONS:
            return False
        if state and state != "SUCCESS":
            return False
    return True


def _workflow_runs(repo: str, sha: str) -> list[dict] | None:
    """Workflow runs on ``sha``. ``[]`` means none exist; ``None`` means we could not tell.

    The two are deliberately different values. ``[]`` is a real answer — the repo ran no
    workflows on this commit — while ``None`` is the absence of an answer, and collapsing
    them is exactly the bug this module had (an empty ``statusCheckRollup`` meaning both
    "no CI" and "CI blocked"). Every failure path returns ``None``; none returns ``[]``.

    ``AUTOREFINE_SKIP_RUN_CHECK=1`` stops the network call, on the same reasoning as
    ``AUTOREFINE_SKIP_DEPENDABOT``: this is a new outbound call in a module that had one.
    It degrades to ``None`` — *not* to the old card-anyway behaviour. The switch exists to
    stop the call, never to re-enable the false green.
    """
    if os.environ.get("AUTOREFINE_SKIP_RUN_CHECK") == "1":
        log.info("AUTOREFINE_SKIP_RUN_CHECK=1 — not checking workflow runs for %s@%s", repo, sha)
        return None
    if not repo or not sha:
        log.warning("Cannot check workflow runs without both repo and head SHA (%r@%r)", repo, sha)
        return None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning("gh api actions/runs failed for %s@%s: %s", repo, sha, proc.stderr.strip())
            return None
        runs = json.loads(proc.stdout or "{}").get("workflow_runs")
        if not isinstance(runs, list):
            log.warning("gh api actions/runs returned no workflow_runs list for %s@%s", repo, sha)
            return None
        return runs
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("gh api actions/runs error for %s@%s: %s", repo, sha, exc)
        return None


def _runs_verdict(runs: list[dict] | None) -> tuple[str, list[str]]:
    """Read the head SHA's workflow runs as GREEN / BLOCKED / NOT_GREEN / UNKNOWN.

    Returns the verdict and, for BLOCKED, the names of the workflows awaiting approval.

    BLOCKED wins over NOT_GREEN when both are present: a gated run means CI has not
    actually reported on this commit yet, so "approve the workflows" is the true next
    action and any other run's verdict is a partial picture until then.

    Otherwise this applies ``_GOOD_CONCLUSIONS`` — the very set already applied to the
    rollup — to the runs the rollup could not see. It is the same rule, not a new one.
    """
    if runs is None:
        return UNKNOWN, []
    blocked = [
        str(run.get("name") or "workflow")
        for run in runs
        if str(run.get("conclusion") or "").upper() == _ACTION_REQUIRED
    ]
    if blocked:
        return BLOCKED, blocked
    for run in runs:
        if str(run.get("status") or "").upper() != "COMPLETED":
            return NOT_GREEN, []
        if str(run.get("conclusion") or "").upper() not in _GOOD_CONCLUSIONS:
            return NOT_GREEN, []
    return GREEN, []


def _is_copilot(pr: dict) -> bool:
    """True when the PR was opened by the Copilot coding agent.

    GitHub surfaces the agent's login inconsistently (``Copilot``,
    ``copilot-swe-agent[bot]``, ``app/copilot-swe-agent``, ``github-copilot[bot]``), so we
    match any login containing ``copilot`` — no other lab author does.
    """
    login = str((pr.get("author") or {}).get("login", "")).lower()
    return "copilot" in login


def _has_label(pr: dict, label: str) -> bool:
    return any(str(item.get("name", "")).lower() == label for item in pr.get("labels", []))


def _already_carded(pr: dict) -> bool:
    return _has_label(pr, PR_CARD_SENT_LABEL)


def _already_nudged(pr: dict) -> bool:
    return _has_label(pr, PR_BLOCKED_CARD_SENT_LABEL)


def _list_open_prs(repo: str) -> list[dict]:
    """Open PRs for a repo with the fields the sweep needs. Returns [] on any gh failure."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "50",
             "--json", "number,title,isDraft,author,labels,url,statusCheckRollup,headRefOid"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning("gh pr list failed for %s: %s", repo, proc.stderr.strip())
            return []
        return json.loads(proc.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("gh pr list error for %s: %s", repo, exc)
        return []


def _mark_carded(repo: str, number: int, label: str = PR_CARD_SENT_LABEL) -> None:
    """Best-effort: create the label if missing, then add it to the PR."""
    subprocess.run(
        ["gh", "label", "create", label, "--repo", repo, "--color", "0e8a16"],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["gh", "pr", "edit", str(number), "--repo", repo, "--add-label", label],
        capture_output=True, text=True,
    )


def _pr_verdict(repo: str, pr: dict) -> tuple[str, list[str]]:
    """GREEN / BLOCKED / NOT_GREEN / UNKNOWN for one PR, plus any blocking workflow names.

    A non-empty rollup is authoritative and costs no extra API call. Only an empty rollup
    — the ambiguous case — falls through to the head SHA's workflow runs.
    """
    rollup = pr.get("statusCheckRollup") or []
    if rollup:
        return (GREEN, []) if _checks_green(rollup) else (NOT_GREEN, [])
    sha = str(pr.get("headRefOid") or "")
    return _runs_verdict(_workflow_runs(repo, sha))


def sweep_pr_cards(repos: list[str], dry_run: bool = False) -> int:
    """Card every ready + CI-green Copilot PR not yet carded. Returns the number carded.

    A PR whose workflows are held for approval gets a distinct, button-less nudge instead
    (see ``send_pr_blocked_card``) and is not counted as carded. A PR whose CI state cannot
    be determined gets nothing at all and is retried on the next sweep.
    """
    carded = 0
    for repo in repos:
        for pr in _list_open_prs(repo):
            number = pr.get("number")
            if not _is_copilot(pr) or pr.get("isDraft") or _already_carded(pr):
                continue
            verdict, blocking = _pr_verdict(repo, pr)
            title = str(pr.get("title", "")).strip()
            url = str(pr.get("url", ""))
            if verdict == BLOCKED:
                if not _already_nudged(pr):
                    _nudge_blocked(repo, int(number), title, url, blocking, dry_run=dry_run)
                continue
            if verdict != GREEN:
                log.info("%s#%s CI %s — skipping", repo, number, verdict)
                continue
            if dry_run:
                log.info("[dry-run] would card PR %s#%s: %s", repo, number, title)
                carded += 1
                continue
            if send_pr_card(repo, int(number), title, pr_url=url):
                _mark_carded(repo, int(number))
                carded += 1
                log.info("Carded PR %s#%s", repo, number)
    return carded


def _nudge_blocked(
    repo: str, number: int, title: str, url: str, workflows: list[str], *, dry_run: bool
) -> None:
    """Send the approval nudge for a blocked PR, at most once per PR."""
    log.info("%s#%s CI blocked awaiting approval: %s", repo, number, ", ".join(workflows))
    if dry_run:
        log.info("[dry-run] would nudge blocked PR %s#%s: %s", repo, number, title)
        return
    if send_pr_blocked_card(repo, number, title, pr_url=url, workflows=workflows):
        _mark_carded(repo, number, PR_BLOCKED_CARD_SENT_LABEL)

