#!/usr/bin/env python3
"""can-auto-merge.py — single-source-of-truth predicate for the auto-review gates.

Per plan §8.7 + §8.9. Mirrors the inline logic in .github/workflows/auto-review.yml
so that other callers (Sam's CLI, future workflows, other agents) can ask the same
question: "is this PR mergeable by automation right now?".

Inputs (CLI flags):
  --repo OWNER/NAME      Required.
  --pr   NUMBER          Required.
  --format json|github   json (default) prints one JSON line; github emits
                         `name=value` lines suitable for GitHub Actions outputs.

Behavior:
  1. Fetches the PR, its files, labels, reviews, and check runs via `gh`.
  2. Evaluates all gates in plan §8.7 and returns a structured result.

Exit codes:
  0  predicate succeeded (regardless of can_merge true/false)
  2  CLI usage error
  3  upstream API error (gh failed, malformed response, etc.)

Auth: relies on `gh` being authenticated. In a workflow, set GH_TOKEN to the
GITHUB_TOKEN of the run.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

# Windows stdout defaults to cp1252; force UTF-8 so JSON / emoji output works.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----- gate configuration ----------------------------------------------------

# Governance repo defaults: scripts/ and .github/workflows/ are in BOTH
# LOW_RISK (so Gate 2 doesn't block them) and HIGH_RISK (so Claude deep-review
# must APPROVE before merge). This lets Copilot evolve governance scripts and
# workflows autonomously, gated only by the LLM review — not a human.
#
# These lists are the *fallback*. The live values come from
# config/auto-review-patterns.json when it exists, so a repo can ship its own
# tiers without forking this script — which is what nauroLabs-github and
# autoRefine had done, drifting 37 lines apart by 2026-08-18. Keeping the
# defaults compiled in means a missing or corrupt config weakens nothing.
DEFAULT_LOW_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.md$", re.IGNORECASE),
    re.compile(r"\.json$", re.IGNORECASE),
    re.compile(r"^tests/"),
    re.compile(r"^skills/"),
    re.compile(r"^wiki/"),
    re.compile(r"^reports/"),
    re.compile(r"^\.github/workflow-templates/"),
    re.compile(r"^\.github/workflows/"),   # workflow changes allowed; deep review required
    re.compile(r"^scripts/"),              # script changes allowed; deep review required
    re.compile(r"(^|/)\.gitkeep$"),
]

DEFAULT_HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^src/"),
    re.compile(r"^infrastructure/"),
    re.compile(r"^api/"),
    re.compile(r"\.bicep$", re.IGNORECASE),
    re.compile(r"auth", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"^\.github/workflows/"),   # workflow changes need Claude deep review
    re.compile(r"^scripts/"),              # script changes need Claude deep review
]

PATTERNS_PATH = Path(__file__).resolve().parent.parent / "config" / "auto-review-patterns.json"


def load_patterns(
    path: Path | None = None,
) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """Return (low_risk, high_risk) from the config file, or the built-in defaults.

    Every failure mode returns the defaults rather than an empty list. An empty
    low-risk list would block every merge (loud, harmless); an empty high-risk
    list would merge infrastructure changes with no review at all (silent, not
    harmless). Falling back to the stricter known-good set is the only safe
    reading of a broken config.
    """
    path = path or PATTERNS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(DEFAULT_LOW_RISK_PATTERNS), list(DEFAULT_HIGH_RISK_PATTERNS)

    def compile_list(key: str, fallback: list[re.Pattern[str]]) -> list[re.Pattern[str]]:
        raw = data.get(key)
        if not isinstance(raw, list) or not raw:
            return list(fallback)
        compiled = []
        for entry in raw:
            try:
                compiled.append(re.compile(str(entry)))
            except re.error:
                # One bad regex must not silently shrink the tier it belongs to.
                return list(fallback)
        return compiled

    return (compile_list("low_risk", DEFAULT_LOW_RISK_PATTERNS),
            compile_list("high_risk", DEFAULT_HIGH_RISK_PATTERNS))


LOW_RISK_PATTERNS, HIGH_RISK_PATTERNS = load_patterns()

RISKY_LABELS: frozenset[str] = frozenset({
    "bicep", "secret", "dns", "major-bump", "auth", "breaking-change",
})

DAILY_CAP: int = 5
ORG_DAILY_CAP: int = 40
AUTO_MERGED_LABEL: str = "auto-merged"
REQUIRED_CHECK_OK_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})

# A check that has not finished is not a check that passed. ACTION_REQUIRED in
# particular reads like a status and is actually "a human must approve this
# workflow before it will run" — the five stuck `Auto-merge Copilot PRs` runs
# had been sitting in it since 2026-08-13.
CHECK_INCOMPLETE_STATES: frozenset[str] = frozenset({
    "queued", "in_progress", "waiting", "pending", "requested", "expected",
    "action_required",
})

NO_CI_REPOS_PATH = Path(__file__).resolve().parent.parent / "config" / "no-ci-repos.json"


def no_ci_repos() -> dict[str, str]:
    """Repos documented as having no PR-triggered CI, mapped to why.

    Deliberately a config file rather than a heuristic. Inferring "does this
    repo have CI" from its workflow files is a guess made at merge time, and a
    guess that fails open is how this hole existed in the first place.
    """
    try:
        data = json.loads(NO_CI_REPOS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable config means we cannot tell which repos are exempt. Return
        # nothing, so every repo is treated as CI-bearing and zero checks
        # blocks. Failing closed on a missing config is the whole point.
        return {}
    repos = data.get("repos")
    return repos if isinstance(repos, dict) else {}


def _normalise_check(node: dict) -> tuple[str, str, str]:
    """Return (name, state, conclusion) lowercased, for either rollup shape.

    statusCheckRollup mixes two node types: CheckRun (status + conclusion) and
    StatusContext (a single state). Treating them uniformly matters because a
    repo can be guarded entirely by one or the other.
    """
    name = (node.get("name") or node.get("context") or "unnamed").strip()
    if "state" in node and "status" not in node:
        state = (node.get("state") or "").strip().lower()
        return name, state, state
    status = (node.get("status") or "").strip().lower()
    conclusion = (node.get("conclusion") or "").strip().lower()
    return name, status, conclusion


def classify_checks(nodes: list[dict], *, repo: str,
                    exempt: dict[str, str] | None = None,
                    awaiting_approval: list[str] | None = None) -> tuple[bool, str]:
    """Decide whether a PR's checks permit an unattended merge.

    Returns (ok, reason). The reason is written for a human reading an
    escalation, so it names the offending check.

    The rule this replaces was `all(c in OK for c in conclusions)`, and `all()`
    over an empty sequence is True. With no branch protection requiring any
    context on any repo, and pr-janitor never fetching the rollup at all, that
    meant a PR with no checks whatsoever merged unattended. era#63 - "Harden
    tenant authorization and OAuth token validation", 5 files - reached
    production that way on 2026-08-21 with 0 check-runs and a commit status of
    `pending`.
    """
    exempt = no_ci_repos() if exempt is None else exempt
    short = repo.split("/")[-1]

    if not nodes:
        if awaiting_approval:
            return False, (
                f"{len(awaiting_approval)} workflow run(s) awaiting manual "
                f"approval and so never started: {', '.join(awaiting_approval[:4])}. "
                f"Approve them in the Actions tab — this is a stuck button, not "
                f"a repo without CI."
            )
        if short in exempt:
            return True, f"no checks, and {short} is documented as having none: {exempt[short]}"
        return False, (
            f"no checks reported — {short} is not on the documented no-CI list, "
            f"so zero checks means unverified, not verified. Add it to "
            f"config/no-ci-repos.json if it genuinely has no PR-triggered CI."
        )

    incomplete: list[str] = []
    failed: list[str] = []
    for node in nodes:
        name, state, conclusion = _normalise_check(node)
        if state in CHECK_INCOMPLETE_STATES or conclusion in CHECK_INCOMPLETE_STATES:
            incomplete.append(f"{name} ({state or conclusion})")
        elif conclusion not in REQUIRED_CHECK_OK_CONCLUSIONS:
            failed.append(f"{name} ({conclusion or 'no conclusion'})")

    if failed:
        return False, f"{len(failed)} check(s) not successful: {', '.join(failed[:4])}"
    if incomplete:
        return False, (
            f"{len(incomplete)} check(s) have not finished: "
            f"{', '.join(incomplete[:4])} — a check that has not completed is "
            f"not a check that passed"
        )
    return True, f"all {len(nodes)} check(s) successful"


# ----- result type -----------------------------------------------------------

@dataclass
class GateResult:
    can_merge: bool
    gate_failed: str | None
    reason: str
    low_risk_only: bool
    has_risky_label: bool
    under_daily_cap: bool
    under_org_daily_cap: bool
    needs_deep_review: bool
    merged_today: int
    merged_today_org: int
    files_count: int
    risky_labels_found: list[str] = field(default_factory=list)
    high_risk_files: list[str] = field(default_factory=list)
    non_low_risk_files: list[str] = field(default_factory=list)
    head_sha: str = ""


def review_and_ci_gates_pass(
    *,
    verdict: str,
    degraded: bool,
    labels: Iterable[str],
    required_check_conclusions: Iterable[str],
) -> bool:
    """Return True when review + CI-related gates allow auto-merge."""
    normalized_labels = {label.strip().lower() for label in labels}
    if verdict != "APPROVE":
        return False
    if degraded:
        return False
    if "needs-human" in normalized_labels:
        return False
    if any(label.startswith("risky-") for label in normalized_labels):
        return False
    return all(
        (conclusion or "").strip().lower() in REQUIRED_CHECK_OK_CONCLUSIONS
        for conclusion in required_check_conclusions
    )


# ----- gh helpers ------------------------------------------------------------

def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse its stdout as JSON.

    Raises SystemExit(3) on failure so the caller (workflow) sees a non-zero
    exit code without us needing to redefine an exception class.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print("error: `gh` CLI not found on PATH", file=sys.stderr)
        sys.exit(3)
    except subprocess.CalledProcessError as exc:
        print(
            f"error: gh {' '.join(args)} failed (exit {exc.returncode}):\n"
            f"{exc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"error: gh stdout was not JSON: {exc}", file=sys.stderr)
        sys.exit(3)


def _paginate(url: str, per_page: int = 100) -> list[dict]:
    """Page through a GitHub REST endpoint and concatenate the arrays."""
    sep = "&" if "?" in url else "?"
    result: list[dict] = []
    page = 1
    while True:
        chunk = _gh_json(["api", f"{url}{sep}per_page={per_page}&page={page}"])
        if not isinstance(chunk, list):
            print(f"error: expected list from {url}, got {type(chunk).__name__}",
                  file=sys.stderr)
            sys.exit(3)
        result.extend(chunk)
        if len(chunk) < per_page:
            return result
        page += 1
        if page > 50:  # hard safety; we don't expect repos this large in canary
            return result


def _count_org_auto_merges_today(owner: str, today: str) -> int:
    """Count org-wide merged PRs labeled `auto-merged` on the given UTC date."""
    query = f"org:{owner} is:pr is:merged label:{AUTO_MERGED_LABEL} merged:{today}"
    encoded_query = urllib.parse.quote_plus(query)
    payload = _gh_json(["api", f"/search/issues?q={encoded_query}&per_page=1"])
    if not isinstance(payload, dict):
        print("error: expected dict from search/issues", file=sys.stderr)
        sys.exit(3)
    total_count = payload.get("total_count", 0)
    try:
        return int(total_count)
    except (TypeError, ValueError):
        print(f"error: unexpected total_count from search/issues: {total_count}",
              file=sys.stderr)
        sys.exit(3)


# ----- gate evaluation -------------------------------------------------------

def _any_match(filename: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(p.search(filename) for p in patterns)


# Boilerplate the cloud agent emits when it never wrote a description. Compared
# after whitespace-collapsing and lowercasing.
BOILERPLATE_DESCRIPTIONS: frozenset[str] = frozenset({
    "pull request created by ai agent",
    "pull request created by copilot",
    "no description provided",
})

# Measured, not guessed. Sampled across turgo, rosette, atlas and era on
# 2026-08-21, cloud-agent PR bodies run 432-3,053 characters; the shortest
# genuine description was era#58 at 432. The two defective PRs that merged into
# this repo that morning were both exactly 32 ("Pull request created by AI
# Agent"). 120 sits an order of magnitude clear of the boilerplate and well
# under the shortest real description, so it separates the two populations
# without arbitrating between good descriptions.
MIN_DESCRIPTION_CHARS: int = 120

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def is_unreviewable_description(body: str | None) -> bool:
    """Is this PR body too thin for anyone to review the merge after the fact?

    Unattended merge trades human review for automation, and the PR description
    is what the trade leaves behind — the one artifact that survives into the
    notification, the digest and the phone screen. When it says nothing, nobody
    can tell what shipped without reading the diff, which is exactly what
    unattended merge assumed nobody would do.

    It is also a usable quality signal in its own right. On 2026-08-21 the only
    two cloud-agent PRs in the fleet carrying boilerplate bodies were
    nauroLabs-github#200 and #201 — and both shipped defects: a `parse_mode=
    Markdown` send that 400s on ordinary PR titles, a `|| true` that hides the
    failure, a digest that marked truncated items as delivered, and a "fix" for
    a dead Telegram channel that never once checked whether the channel was
    alive. A session that could not explain itself had not understood the
    problem.

    HTML comments are stripped before measuring: a body consisting only of a
    hidden template comment is empty to every reader.
    """
    text = _HTML_COMMENT.sub("", body or "").strip()
    if not text:
        return True
    collapsed = " ".join(text.split()).lower().rstrip(".")
    if collapsed in BOILERPLATE_DESCRIPTIONS:
        return True
    return len(text) < MIN_DESCRIPTION_CHARS


def declares_itself_incomplete(title: str) -> bool:
    """Does the PR's own title say it is not finished, or that it did nothing?

    Measured from the weekly reflection ledger on 2026-08-22: of 131 merged PRs,
    **19 were titled `[WIP]` or announced they had changed nothing** - and they
    were merged into default branches unattended. Real examples:

        era#31        [WIP] Add enhanced invoice recognition capabilities using AI
        agentMode#41  [WIP] Fix issues with Copilot integration
        agentMode#23  No-op: session reset command already implemented

    A builder that labels its own output work-in-progress is the cheapest possible
    signal, and nothing was reading it. `no-op ... already implemented` is worse
    than churn: it is the proposer having filed an idea for work that was already
    done, then the builder confirming it, then the gate merging the confirmation.

    Draft status does not catch these - `pr-janitor` un-drafts Copilot PRs by
    design - so the title is the signal that survives.
    """
    return bool(re.search(
        r"\[wip\]|\bwip\b|\bno-?op\b|already (implemented|done|exists)|"
        r"\bdo not merge\b|\bdon'?t merge\b|\bplaceholder\b|\bstub only\b",
        title or "", re.IGNORECASE,
    ))


def is_major_dependency_bump(title: str) -> bool:
    """Does this PR title describe a major version bump?

    `dependabot-auto-merge.yml` refuses these in all 12 repos that carry it -
    "Never auto-merge a major version bump, leave it for a human." pr-janitor,
    which sweeps every 6h as the backstop for exactly those workflows, had no
    such guard: it merged on file-tier plus checks alone. So the backstop was
    strictly MORE permissive than the fast path it backs up, and a major bump
    that the per-repo workflow deliberately declined would be merged unattended
    a few hours later.

    Latent rather than live so far - all four PRs the janitor merged on
    2026-08-20 were minor (5.5.8 to 5.11.0, 1.1.4 to 1.3.1). Fixed before it
    stops being latent.

    Matches Dependabot's own title format: "bump <pkg> from 1.2.3 to 2.0.0".
    A title that does not parse is not treated as major - this gate exists to
    catch a specific known-unsafe shape, and every other gate still applies.
    """
    match = re.search(
        r"\bfrom\s+v?(\d+)\.\S*\s+to\s+v?(\d+)\.", title or "", re.IGNORECASE)
    return bool(match) and match.group(1) != match.group(2)


def _gh_json_soft(args: list[str]) -> tuple[object, bool]:
    """Like _gh_json, but reports failure instead of exiting the process.

    Returns (parsed, ok). Callers that can degrade to a second source need to
    tell "the query failed" from "the answer is empty" — a distinction the hard
    variant destroys by exiting, and which fetch_check_rollup destroyed by
    returning [] for both.
    """
    try:
        proc = subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None, False
    try:
        return json.loads(proc.stdout), True
    except json.JSONDecodeError:
        return None, False


def resolve_checks(repo: str, pr_number: int,
                   head_sha: str | None = None) -> tuple[list[dict], str]:
    """(nodes, source) — the PR's checks, read however this token can read them.

    `source` is "rollup" or "actions-api", and callers must surface it: a verdict
    reached from a substitute source must not be indistinguishable from one
    reached normally.

    Why a substitute exists at all. GH_PAT is fine-grained and carries neither
    `Checks` nor `Commit statuses`, so `gh pr view --json statusCheckRollup` — the
    query those two permissions gate — returns "Resource not accessible by
    personal access token" for every PR in the fleet. Both callers then fail
    closed, correctly, and escalate everything: every daily card has read "0
    merged automatically, 1 waiting on you". The janitor was blind, not stuck,
    and the report could not tell the difference.

    The Actions API *is* readable, and in this fleet it is equivalent evidence:
    every check here is a GitHub Actions workflow, so it sees the same runs. It
    would NOT be equivalent anywhere an external provider posts commit statuses
    without a workflow run — which is why an empty result stays blocking rather
    than reading as clean.
    """
    nodes, readable = fetch_check_rollup_status(repo, pr_number)
    if readable:
        return nodes, "rollup"
    sha = head_sha or _head_sha(repo, pr_number)
    return (fetch_actions_checks(repo, sha) if sha else []), "actions-api"


def _head_sha(repo: str, pr_number: int) -> str:
    pr, ok = _gh_json_soft(["pr", "view", str(pr_number), "--repo", repo,
                            "--json", "headRefOid"])
    if not ok or not isinstance(pr, dict):
        return ""
    return pr.get("headRefOid") or ""


def annotate_substitute_source(reason: str, source: str) -> str:
    """Mark a verdict that was reached without the real check rollup."""
    if source != "rollup":
        return (f"{reason} [via the Actions API — this token cannot read the "
                f"check rollup; see nauroLabs-github#211]")
    return reason


def fetch_check_rollup(repo: str, pr_number: int) -> list[dict]:
    """statusCheckRollup nodes for a PR's head commit.

    Returns [] both when there are genuinely no checks and when the query
    fails; classify_checks() treats an empty list as blocking unless the repo
    is on the documented exemption list, so the ambiguity fails closed.

    Prefer fetch_check_rollup_status() where the difference matters.
    """
    nodes, _ = fetch_check_rollup_status(repo, pr_number)
    return nodes


def fetch_check_rollup_status(repo: str, pr_number: int) -> tuple[list[dict], bool]:
    """(nodes, readable). One call, so the caller can tell empty from denied.

    GH_PAT is fine-grained and, as of 2026-08-23, carries neither `Checks` nor
    `Commit statuses`. `gh pr view --json statusCheckRollup` is exactly the query
    those two permissions gate, so the janitor could not read a single check
    result and correctly refused to merge what it could not verify — which is why
    every daily card read "0 merged automatically, 1 waiting on you". Blind, not
    stuck, and indistinguishable from stuck in the report it produced.
    """
    out, ok = _gh_json_soft([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "statusCheckRollup",
    ])
    if not ok or not isinstance(out, dict):
        return [], False
    nodes = out.get("statusCheckRollup")
    return (nodes if isinstance(nodes, list) else []), True


def fetch_actions_checks(repo: str, head_sha: str) -> list[dict]:
    """Completed workflow runs for a commit, shaped like rollup CheckRun nodes.

    A fallback for when the rollup is unreadable. It is equivalent evidence
    *here* and only here: every check in this fleet is a GitHub Actions workflow,
    so the Actions API sees the same runs the Checks API would report. It is NOT
    equivalent in general — an external CI provider posts a commit status and no
    workflow run, and would be invisible to this. That is why the caller refuses
    to merge when this returns nothing rather than reading empty as clean.

    `action_required` runs are deliberately preserved rather than dropped: they
    never started, and a run that never started is not a run that passed.
    """
    runs, ok = _gh_json_soft(["api", f"/repos/{repo}/actions/runs?head_sha={head_sha}"])
    if not ok or not isinstance(runs, dict):
        return []
    nodes: list[dict] = []
    for run in runs.get("workflow_runs", []):
        if not isinstance(run, dict):
            continue
        nodes.append({
            "name": run.get("name") or "unnamed",
            "status": run.get("status") or "",
            "conclusion": run.get("conclusion") or "",
            "detailsUrl": run.get("html_url") or "",
        })
    return nodes


def runs_awaiting_approval(repo: str, pr_number: int) -> list[str]:
    """Workflow runs for this PR's head that a human must approve before they run.

    These are invisible in statusCheckRollup — they produce no check-run,
    because they never started — so a PR whose entire CI is stuck behind the
    approval prompt looks identical to a PR with no CI at all. The two need
    opposite responses: "click approve" versus "this repo has no checks", and
    telling someone the second when the first is true sends them to widen the
    exemption list to fix a button-press.

    nauroLabs-github#191 sat like this for two days: Governance Unit Tests,
    Auto-review and merge, and Auto-merge Dependabot PRs all `action_required`
    since 2026-08-18, on a PR that edits scripts/, tests/ AND
    config/schedule-budget.json.
    """
    pr = _gh_json(["pr", "view", str(pr_number), "--repo", repo,
                   "--json", "headRefOid"])
    if not isinstance(pr, dict) or not pr.get("headRefOid"):
        return []
    runs = _gh_json(["api", f"/repos/{repo}/actions/runs?head_sha={pr['headRefOid']}"])
    if not isinstance(runs, dict):
        return []
    return sorted({
        r.get("name", "unnamed")
        for r in runs.get("workflow_runs", [])
        if (r.get("conclusion") or r.get("status") or "").lower() == "action_required"
    })


def evaluate(repo: str, pr_number: int, *, allow_product_files: bool = False,
             ignore_run_id: str | None = None) -> GateResult:
    """Evaluate every invariant; product builders may bypass only the file allowlist."""
    pr_obj = _gh_json(["api", f"/repos/{repo}/pulls/{pr_number}"])
    assert isinstance(pr_obj, dict)

    label_names: list[str] = [lbl["name"] for lbl in pr_obj.get("labels", [])]
    risky_labels_found = [name for name in label_names if name in RISKY_LABELS]
    has_risky_label = bool(risky_labels_found)

    files = _paginate(f"/repos/{repo}/pulls/{pr_number}/files")
    filenames: list[str] = [f["filename"] for f in files]

    if filenames:
        non_low_risk = [f for f in filenames if not _any_match(f, LOW_RISK_PATTERNS)]
        high_risk = [f for f in filenames if _any_match(f, HIGH_RISK_PATTERNS)]
    else:
        # An empty PR (no file changes) is suspicious; treat as not-low-risk.
        non_low_risk = []
        high_risk = []
    low_risk_only = bool(filenames) and not non_low_risk
    needs_deep_review = bool(high_risk)

    # Daily cap: count auto-merged PRs closed today.
    today = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
    closed = _paginate(
        f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
    )
    merged_today = 0
    for pr in closed:
        if not pr.get("merged_at"):
            # stop early; once we hit a PR not merged today and not updated
            # today, older PRs are guaranteed out of window
            if (pr.get("updated_at") or "")[:10] < today:
                break
            continue
        if pr["merged_at"][:10] != today:
            # Updated today but merged earlier — skip but keep scanning.
            if pr.get("updated_at", "")[:10] < today:
                break
            continue
        if any(lbl["name"] == AUTO_MERGED_LABEL for lbl in pr.get("labels", [])):
            merged_today += 1
    owner = repo.split("/", 1)[0]
    merged_today_org = _count_org_auto_merges_today(owner, today)
    under_repo_daily_cap = merged_today < DAILY_CAP
    under_org_daily_cap = merged_today_org < ORG_DAILY_CAP
    under_daily_cap = under_repo_daily_cap and under_org_daily_cap

    # Compose verdict in plan §8.7 order.
    if not filenames:
        return GateResult(
            can_merge=False,
            gate_failed="empty-pr",
            reason="PR has no file changes",
            low_risk_only=False,
            has_risky_label=has_risky_label,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=False,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=0,
            risky_labels_found=risky_labels_found,
        )
    if declares_itself_incomplete(pr_obj.get("title") or ""):
        return GateResult(
            can_merge=False,
            gate_failed="self-declared-incomplete",
            reason=(
                "the PR title says it is unfinished or changed nothing — a builder "
                "that labels its own output [WIP] or 'no-op, already implemented' "
                "is the cheapest signal there is, and 19 such PRs were merged "
                "unattended before this gate existed"
            ),
            low_risk_only=low_risk_only,
            has_risky_label=False,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    # An undescribed PR is checked before anything that costs an API call: it is
    # a property of the PR as submitted, and no amount of green CI makes a merge
    # nobody can account for afterwards a good idea.
    if is_unreviewable_description(pr_obj.get("body")):
        return GateResult(
            can_merge=False,
            gate_failed="undescribed",
            reason=(
                "PR description is empty or boilerplate — unattended merge "
                "leaves the description as the only record of what shipped, "
                "and it is all a reviewer on a phone can read"
            ),
            low_risk_only=low_risk_only,
            has_risky_label=has_risky_label,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    # Checks first among the content gates: a PR nothing verified must not
    # merge however low-risk its files look.
    rollup, checks_source = resolve_checks(
        repo, pr_number, head_sha=(pr_obj.get("head") or {}).get("sha"))
    if ignore_run_id:
        own_run = re.compile(rf"/actions/runs/{re.escape(ignore_run_id)}(?:/|$)")
        rollup = [node for node in rollup
                  if not own_run.search(node.get("detailsUrl") or "")]
    checks_ok, checks_reason = classify_checks(
        rollup, repo=repo,
        awaiting_approval=runs_awaiting_approval(repo, pr_number) if not rollup else None)
    checks_reason = annotate_substitute_source(checks_reason, checks_source)
    if not checks_ok:
        return GateResult(
            can_merge=False,
            gate_failed="checks",
            reason=checks_reason,
            low_risk_only=low_risk_only,
            has_risky_label=has_risky_label,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if has_risky_label:
        return GateResult(
            can_merge=False,
            gate_failed="risky-label",
            reason=f"risky label(s) applied: {', '.join(risky_labels_found)}",
            low_risk_only=low_risk_only,
            has_risky_label=True,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if is_major_dependency_bump(pr_obj.get("title") or ""):
        return GateResult(
            can_merge=False,
            gate_failed="major-bump",
            reason=(
                "major version bump — the per-repo dependabot-auto-merge "
                "workflow declines these, so this backstop must too"
            ),
            low_risk_only=low_risk_only,
            has_risky_label=False,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if not low_risk_only and not allow_product_files:
        return GateResult(
            can_merge=False,
            gate_failed="file-tier",
            reason=(
                f"{len(non_low_risk)} file(s) outside low-risk allowlist: "
                f"{', '.join(non_low_risk[:3])}"
                + (" …" if len(non_low_risk) > 3 else "")
            ),
            low_risk_only=False,
            has_risky_label=False,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if not under_daily_cap:
        return GateResult(
            can_merge=False,
            gate_failed="daily-cap",
            reason=(
                ", ".join(
                    part for part in [
                        (
                            f"repo cap reached: {merged_today}/{DAILY_CAP}"
                            if not under_repo_daily_cap else ""
                        ),
                        (
                            f"org cap reached: {merged_today_org}/{ORG_DAILY_CAP}"
                            if not under_org_daily_cap else ""
                        ),
                    ] if part
                )
            ),
            low_risk_only=low_risk_only,
            has_risky_label=False,
            under_daily_cap=False,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
        )
    return GateResult(
        can_merge=True,
        gate_failed=None,
        reason="all pre-flight gates passed",
        low_risk_only=low_risk_only,
        has_risky_label=False,
        under_daily_cap=True,
        under_org_daily_cap=True,
        needs_deep_review=needs_deep_review,
        merged_today=merged_today,
        merged_today_org=merged_today_org,
        files_count=len(filenames),
        head_sha=(pr_obj.get("head") or {}).get("sha") or "",
    )


# ----- output formatting -----------------------------------------------------

def _emit_json(result: GateResult) -> None:
    json.dump(asdict(result), sys.stdout)
    sys.stdout.write("\n")


def _emit_github(result: GateResult) -> None:
    """Emit `name=value` lines for the GITHUB_OUTPUT file."""
    payload = asdict(result)
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, list):
            rendered = ",".join(str(v) for v in value)
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        # Multi-line strings would need a HEREDOC; our values are all single-line.
        sys.stdout.write(f"{key}={rendered}\n")


# ----- entrypoint ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="OWNER/NAME of the repo")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--ignore-run-id", help="Exclude only this merge workflow's own checks")
    parser.add_argument(
        "--format",
        choices=("json", "github"),
        default="json",
        help="Output shape (default: json)",
    )
    args = parser.parse_args(argv)

    if "/" not in args.repo:
        parser.error("--repo must be OWNER/NAME")

    result = evaluate(args.repo, args.pr, ignore_run_id=args.ignore_run_id)
    if args.format == "github":
        _emit_github(result)
    else:
        _emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
