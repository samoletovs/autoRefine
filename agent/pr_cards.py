"""Sweep for ready Copilot PRs and send a Telegram approval card for each.

This is the second half of the human-in-the-loop build trigger. autoRefine files an idea
→ a 👍 assigns the Copilot coding agent → Copilot opens a PR. This sweep watches those PRs
and, once one is ready (not a draft) and its CI is green, sends a Telegram card. nauroBot
turns a 👍 tap into an *approve + squash-merge* and a 👎 into a *close* (see nauroBot's
``handlers._approve_pr`` / ``_decline_pr``).

The sweep is deliberately near-read-only: the only write it makes to GitHub is adding a
``pr-card-sent`` label so a PR is carded exactly once. The approve/merge/close all happen
in nauroBot on the tap — never here — so this can run on a cheap cron with just a PAT.
"""
from __future__ import annotations

import json
import logging
import subprocess

from agent.notify import send_pr_card

log = logging.getLogger("autorefine.pr_cards")

# The sweep marks a PR carded with this label so it isn't re-carded on the next run.
PR_CARD_SENT_LABEL = "pr-card-sent"

# Conclusions/states that count as "done and fine" — anything else (or an unfinished
# check) means CI isn't green yet.
_GOOD_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _checks_green(rollup: list[dict]) -> bool:
    """True when every check in a PR's ``statusCheckRollup`` has finished successfully.

    An empty rollup (the repo has no CI) counts as green — there is nothing to wait for.
    A check still running (``status`` != COMPLETED), a non-success ``conclusion`` (check
    runs), or a non-SUCCESS ``state`` (legacy statuses) all block.
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


def _is_copilot(pr: dict) -> bool:
    """True when the PR was opened by the Copilot coding agent.

    GitHub surfaces the agent's login inconsistently (``Copilot``,
    ``copilot-swe-agent[bot]``, ``app/copilot-swe-agent``, ``github-copilot[bot]``), so we
    match any login containing ``copilot`` — no other lab author does.
    """
    login = str((pr.get("author") or {}).get("login", "")).lower()
    return "copilot" in login


def _already_carded(pr: dict) -> bool:
    return any(
        str(label.get("name", "")).lower() == PR_CARD_SENT_LABEL
        for label in pr.get("labels", [])
    )


def _list_open_prs(repo: str) -> list[dict]:
    """Open PRs for a repo with the fields the sweep needs. Returns [] on any gh failure."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "50",
             "--json", "number,title,isDraft,author,labels,url,statusCheckRollup"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.warning("gh pr list failed for %s: %s", repo, proc.stderr.strip())
            return []
        return json.loads(proc.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("gh pr list error for %s: %s", repo, exc)
        return []


def _mark_carded(repo: str, number: int) -> None:
    """Best-effort: create the label if missing, then add it to the PR."""
    subprocess.run(
        ["gh", "label", "create", PR_CARD_SENT_LABEL, "--repo", repo, "--color", "0e8a16"],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["gh", "pr", "edit", str(number), "--repo", repo, "--add-label", PR_CARD_SENT_LABEL],
        capture_output=True, text=True,
    )


def sweep_pr_cards(repos: list[str], dry_run: bool = False) -> int:
    """Card every ready + CI-green Copilot PR not yet carded. Returns the number carded."""
    carded = 0
    for repo in repos:
        for pr in _list_open_prs(repo):
            number = pr.get("number")
            if not _is_copilot(pr) or pr.get("isDraft") or _already_carded(pr):
                continue
            if not _checks_green(pr.get("statusCheckRollup", [])):
                log.info("%s#%s CI not green yet — skipping", repo, number)
                continue
            title = str(pr.get("title", "")).strip()
            if dry_run:
                log.info("[dry-run] would card PR %s#%s: %s", repo, number, title)
                carded += 1
                continue
            if send_pr_card(repo, int(number), title, pr_url=str(pr.get("url", ""))):
                _mark_carded(repo, int(number))
                carded += 1
                log.info("Carded PR %s#%s", repo, number)
    return carded
