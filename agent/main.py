"""autoRefine entry point — orchestrates the evaluate → plan → execute cycle."""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent.config import AutoRefineConfig, ProjectConfig
from agent.tools.github_tools import clone_repo, read_project_yaml
from agent.tools.quality_tools import RepoContext, run_quality_checks_with_coverage

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("autorefine")

# Suppress verbose Azure SDK HTTP logging
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)
# httpx logs the full request URL at INFO, and the Telegram API puts the bot token in the
# path — which printed the live token into the container logs, where it is retained by Log
# Analytics. Silence it; there is no request detail here worth a leaked credential.
logging.getLogger("httpx").setLevel(logging.WARNING)

MANIFEST_PATH = Path(__file__).parent.parent.parent / ".github" / "config" / "workspace-manifest.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
# Manifest statuses that mean "no further work is wanted here". Evaluating these bought
# nothing and cost a great deal - see load_repos_from_manifest.
FINISHED_STATUSES = frozenset({"archived", "complete", "deleted"})
DEFAULT_IDEA_PRIORITIES = {"P0", "P1"}
BUGFIX_CATEGORIES = {
    "bug",
    "bugs",
    "defect",
    "security",
    "reliability",
    "stability",
}
# Functional categories map to the `feature` idea type (vision / UX / capability gaps,
# not defects). Used by _map_improvement_type for the observe-first functional pass.
FEATURE_CATEGORIES = {
    "feature",
    "features",
    "functionality",
    "capability",
    "ux",
    "ui",
    "user-experience",
    "onboarding",
    "feature-parity",
    "parity",
    "i18n",
    "internationalization",
    "a11y",
    "accessibility",
}
# Observe-first functional ideation only runs for these project stages (active work).
FUNCTIONAL_STAGES = {"active", "mvp"}
# Functional ideas are often P2; allow P0-P2 but hard-cap to 1 per project per daily run
# (the evaluate workflow runs once/day), so each project surfaces at most one idea card/day.
FUNCTIONAL_PRIORITIES = {"P0", "P1", "P2"}
FUNCTIONAL_IDEA_CAP = 1
# Foundry runs fail transiently (server_error / rate_limit on gpt-4o-mini); retry the
# functional plan a couple of times so a single blip doesn't skip the whole ideation.
FUNCTIONAL_PLAN_ATTEMPTS = 3
FUNCTIONAL_RETRY_DELAY_S = 5
IDEA_CARD_REMINDER_DAYS = 3
GOVERNANCE_REPO_URL = "https://github.com/samoletovs/nauroLabs-github.git"
# Functional ideation can draw on the lab wiki (memex-ingested insights/trends) so ideas
# reflect freshly-learned knowledge, not just the project's own files. Best-effort + capped.
WIKI_CONTEXT_MAX_PAGES = 6
WIKI_CONTEXT_MAX_CHARS = 2400
WIKI_CONTEXT_RECENT_DAYS = 60
WIKI_CONTEXT_OTHER_CAP = 2

# ── Activity gate ────────────────────────────────────────────────────────────
# The sweep's expense is almost entirely the two Foundry planning runs per project
# (technical + functional), not the deterministic evaluation around them. A plan run
# is ~78 tool rounds, and every round re-sends the accumulated thread, so one run is
# on the order of 850k input tokens. At ~23 active projects that is the 400M
# cached-input tokens a month showing up on the gpt-4o-mini meter.
#
# Most of those runs answer a question nothing changed the answer to. A project whose
# default branch has not moved since the previous sweep gets re-read, re-analysed and
# re-planned to produce the ideas it produced yesterday. Skipping the planning for an
# unchanged project is the one lever here with no quality cost at all.
#
# Two rules decide it, and a project is planned if either holds:
#   1. its default branch has a commit inside the lookback window, or
#   2. its deterministic rotation slot comes up.
#
# Rule 2 is the staleness floor and the reason no state file is needed: the slot is a
# pure function of the repo name and the date, so every project is planned at least
# once every AUTOREFINE_ROTATION_DAYS whatever its commit history, and the projects
# are spread evenly across the days rather than all landing on one.
DEFAULT_ACTIVITY_LOOKBACK_HOURS = 26
# The sweep is daily; 26h covers the previous run plus a two-hour margin for a late
# or retried schedule, so a commit cannot fall between two windows and be missed.
DEFAULT_ROTATION_DAYS = 7

# Priority scales the staleness floor, so attention follows the owner's actual
# interest rather than being spread evenly over 25 experiments.
#
# Before this, every project got the same guaranteed slot regardless of whether it
# was a flagship or an idea nobody has touched since spring. That is the wrong
# default for a lab whose whole constraint is budget: each planning run costs Foundry
# tokens, and each idea it files can start a 10-30 minute Copilot run. Spending the
# same on `payArc` (stage: idea) as on `era` (the flagship Q1 experiment) is a choice,
# and it was being made by omission.
#
# Multipliers, applied to AUTOREFINE_ROTATION_DAYS. Activity still overrides both:
# a project that was committed to inside the lookback window is always planned,
# whatever its priority, so a burst of work on a `low` project is never ignored.
PRIORITY_ROTATION_FACTOR = {
    "top": 0.5,     # ~3-4 days
    "normal": 1.0,  # ~7 days
    "low": 3.0,     # ~21 days
}
DEFAULT_PRIORITY = "normal"

# Paths the lab's own cross-repo housekeeping rewrites in every project on the same
# day: a licence sweep, a synced security script, a governance workflow update. On
# 2026-08-21 three such sweeps had touched 22 of 24 projects inside 28 hours, so a
# gate that counted any commit as activity would have skipped two projects and
# planned everything else — a saving on paper and none in the bill. None of these can
# change what an improvement plan says, so they do not count as activity.
HOUSEKEEPING_PATHSPECS = (
    ":(exclude)LICENSE",
    ":(exclude)LICENSE.*",
    ":(exclude)NOTICE",
    ":(exclude).gitignore",
    ":(exclude).gitattributes",
    ":(exclude).github/**",
    ":(exclude)scripts/audit-leaks.ps1",
    ":(exclude)scripts/can-auto-merge.py",
)


def _hours_since_last_commit(project_dir: Path, now: datetime) -> float | None:
    """Age in hours of the newest substantive commit, or None if it can't be read.

    Commits touching only :data:`HOUSEKEEPING_PATHSPECS` are ignored. ``None`` means
    "unknown", which callers must treat as activity: guessing "idle" from a git
    failure would silently switch ideation off for every project at once.
    ``inf`` is different — git succeeded and reported no substantive commit at all.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", ".", *HOUSEKEEPING_PATHSPECS],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("git log failed in %s — treating as active", project_dir)
        return None

    if result.returncode != 0:
        log.warning("git log failed in %s — treating as active", project_dir)
        return None

    stamp = result.stdout.strip()
    if not stamp:
        # git worked and found nothing: every commit here is housekeeping.
        return float("inf")

    try:
        committed = datetime.fromisoformat(stamp)
    except ValueError:
        log.warning("Unparseable commit date %r — treating as active", stamp)
        return None

    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=timezone.utc)
    return (now - committed).total_seconds() / 3600.0


def _rotation_slot_due(repo: str, now: datetime, rotation_days: int) -> bool:
    """Whether today is this repo's guaranteed slot in the rotation.

    Deterministic and stateless: a stable hash of the repo name picks one day in
    every ``rotation_days``, so coverage does not depend on remembering when the
    project was last looked at.
    """
    if rotation_days <= 1:
        return True
    digest = hashlib.sha256(repo.encode("utf-8")).digest()
    slot = int.from_bytes(digest[:8], "big") % rotation_days
    return now.date().toordinal() % rotation_days == slot


def _activity_lookback_hours() -> float:
    """Lookback window in hours. ``0`` disables the gate and plans everything."""
    raw = os.environ.get("AUTOREFINE_ACTIVITY_LOOKBACK_HOURS", "").strip()
    if not raw:
        return float(DEFAULT_ACTIVITY_LOOKBACK_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        log.warning(
            "AUTOREFINE_ACTIVITY_LOOKBACK_HOURS=%r is not a number — using default %s",
            raw,
            DEFAULT_ACTIVITY_LOOKBACK_HOURS,
        )
        return float(DEFAULT_ACTIVITY_LOOKBACK_HOURS)
    return max(hours, 0.0)


def _rotation_days() -> int:
    """Staleness floor in days. Every project is planned at least this often."""
    raw = os.environ.get("AUTOREFINE_ROTATION_DAYS", "").strip()
    if not raw:
        return DEFAULT_ROTATION_DAYS
    try:
        days = int(raw)
    except ValueError:
        log.warning(
            "AUTOREFINE_ROTATION_DAYS=%r is not an integer — using default %d",
            raw,
            DEFAULT_ROTATION_DAYS,
        )
        return DEFAULT_ROTATION_DAYS
    return max(days, 1)


def _rotation_days_for(priority: str | None = None) -> int:
    """Staleness floor in days, scaled by the project's priority.

    An unknown or missing priority is treated as ``normal`` — a project that has not
    been triaged should not silently drop to a 21-day cadence.
    """
    base = _rotation_days()
    factor = PRIORITY_ROTATION_FACTOR.get((priority or DEFAULT_PRIORITY).lower(),
                                          PRIORITY_ROTATION_FACTOR[DEFAULT_PRIORITY])
    return max(int(round(base * factor)), 1)


def should_plan_repo(
    repo: str,
    project_dir: Path,
    now: datetime | None = None,
    priority: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a swept project earns its Foundry planning runs this cycle.

    Returns ``(should_plan, reason)``. Fails open: anything unknown counts as
    activity, so the gate can only ever skip a project it positively established
    was idle.
    """
    now = now or datetime.now(timezone.utc)
    lookback = _activity_lookback_hours()
    if lookback == 0:
        return True, "activity gate disabled (AUTOREFINE_ACTIVITY_LOOKBACK_HOURS=0)"

    age = _hours_since_last_commit(project_dir, now)
    if age is None:
        return True, "commit age unknown — planning rather than guessing"
    if age <= lookback:
        return True, f"changed {age:.1f}h ago (within {lookback:.0f}h)"

    since = "no substantive commit ever" if age == float("inf") else f"idle {age:.1f}h"
    rotation_days = _rotation_days_for(priority)
    pri = (priority or DEFAULT_PRIORITY).lower()
    if _rotation_slot_due(repo, now, rotation_days):
        return True, f"{since} but rotation slot due (every {rotation_days}d, priority={pri})"

    return (False,
            f"{since}, housekeeping only, and not in today's rotation slot "
            f"(every {rotation_days}d, priority={pri})")


def load_repos_from_manifest(manifest_path: Path) -> list[str]:
    """Load repo list from workspace-manifest.json, skipping finished projects.

    Ordered by priority, highest first. Ordering matters whenever a run is cut
    short — by a timeout, a budget brake, or a crash — because whatever it did get
    to should be the work the owner cares about most, not whichever project sorted
    first alphabetically.

    Only `archived` used to be skipped, which meant a project marked `complete` kept
    being evaluated, kept having ideas filed against it, and kept having them approved
    and assigned. Each assignment starts a 10-30 minute Copilot agent run whose PR then
    triggers CI, triage, auto-label and auto-merge.

    amberRepublic is what that costs: `status: complete` in the manifest and 191
    workflow runs in a single month - 545 billable Actions minutes, 11.7% of the fleet's
    entire bill - proposing improvements to a project nobody had asked to change, while
    the account sat at 243% of its allowance. Running out stops every workflow on the
    account, including the ones that recover it.

    "Complete" has to mean the loop stops, or it means nothing.
    """
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    live = [p for p in data.get("projects", [])
            if p.get("status") not in FINISHED_STATUSES]
    rank = {"top": 0, "normal": 1, "low": 2}
    live.sort(key=lambda p: (rank.get(str(p.get("priority", DEFAULT_PRIORITY)).lower(), 1),
                             p.get("repo", "")))
    return [p["repo"] for p in live]


def load_priorities_from_manifest(manifest_path: Path) -> dict[str, str]:
    """``{repo_slug: priority}`` so the planning gate can scale its rotation.

    Read separately rather than threaded through the repo list, because the list is
    also built from ``--repo`` arguments where no manifest entry exists. A repo absent
    here is planned at ``normal`` cadence, never silently demoted.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot read priorities from %s: %s — treating all as %s",
                    manifest_path, exc, DEFAULT_PRIORITY)
        return {}
    return {
        p["repo"]: str(p.get("priority", DEFAULT_PRIORITY)).lower()
        for p in data.get("projects", [])
        if p.get("repo")
    }


def github_token() -> str:
    """The token autoRefine reads GitHub with, or "" when there is none.

    ``GH_TOKEN`` first, matching what the workflows set (they pass
    ``secrets.GH_PAT``, which can read the other 24 repos — the Actions-provided
    ``GITHUB_TOKEN`` cannot, so a check that needs cross-repo reads must never
    silently fall back to it and call the result clean).
    """
    return os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")


def evaluate_project(
    project_dir: Path, config: ProjectConfig, repo: RepoContext | None = None,
) -> dict:
    """Run evaluation on a single project. Returns structured findings.

    *repo* carries GitHub identity for the checks that must ask GitHub. It is
    optional so a local evaluation still works; those checks then report
    ``tooling-unavailable`` rather than a clean bill of health.
    """
    log.info("Evaluating: %s (%s)", config.name, config.stage)

    # Technical quality checks (deterministic)
    findings, coverage = run_quality_checks_with_coverage(str(project_dir), config, repo)
    feature_suggestions = suggest_feature_improvements(config)

    report = {
        "project": config.name,
        "stage": config.stage,
        "findings": [
            {
                "category": f.category,
                "description": f.description,
                "priority": f.priority,
                # Carried into the report because this dict is what reaches
                # build_plan_task, which is where advisory findings are withheld
                # from the model. Dropping the key here would reopen that path.
                "advisory": f.advisory,
            }
            for f in findings
        ],
        "score": max(0, 100 - sum(f.weight for f in findings)),
        # The score's denominator. Most checks only run when the project
        # declares the trait, so 100/100 over two dimensions and 100/100 over
        # six are very different claims. Reported so the number never travels
        # without it.
        "coverage": coverage.as_dict(),
        "feature_suggestions": feature_suggestions,
    }

    log.info(
        "Evaluation complete: %s — score %d/100 (%s), %d findings, %d feature suggestions",
        config.name, report["score"], coverage.summary(), len(findings),
        len(feature_suggestions),
    )
    if coverage.skipped:
        log.info(
            "%s — dimensions not measured: %s",
            config.name, ", ".join(coverage.skipped),
        )
    return report


def suggest_feature_improvements(config: ProjectConfig) -> list[dict]:
    """Suggest potential functional improvements from project.yaml goals + similar products."""
    suggestions: list[dict] = []

    for goal in config.goals[:2]:
        goal_text = str(goal).strip()
        if not goal_text:
            continue
        suggestions.append(
            {
                "title": f"Goal-aligned capability: {goal_text}",
                "description": (
                    "Define and implement a user-facing capability that directly advances "
                    f"the goal: {goal_text}."
                ),
                "priority": "P1",
                "category": "feature",
            }
        )

    for product in config.similar[:2]:
        product_name = str(product).strip()
        if not product_name:
            continue
        suggestions.append(
            {
                "title": f"Feature parity review with {product_name}",
                "description": (
                    f"Compare current capabilities against {product_name} and implement one "
                    "high-value missing feature that fits this project's goals."
                ),
                "priority": "P1",
                "category": "feature-parity",
            }
        )

    return suggestions[:3]


def load_config(project_dir: Path) -> ProjectConfig | None:
    """Load project.yaml configuration from a cloned project directory."""
    return read_project_yaml(project_dir)


def _is_valid_repo_slug(repo: str) -> bool:
    """Validate GitHub repo slug format owner/name."""
    if repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    return bool(owner.strip() and name.strip())


def plan_project(
    project_dir: Path,
    config: ProjectConfig,
    findings: list[dict],
    model: str | None = None,
) -> dict | None:
    """Use Foundry agent to create an improvement plan."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    if not endpoint:
        log.error("FOUNDRY_PROJECT_ENDPOINT not set — cannot run plan mode.")
        log.info("Set it in .env or environment. See .env.example.")
        return None

    from azure.ai.agents import AgentsClient
    from azure.identity import DefaultAzureCredential

    from agent.foundry_agent import build_plan_task, create_agent, run_agent

    client = AgentsClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )

    agent_id = create_agent(client, mode="plan", model=model)

    try:
        task = build_plan_task(findings, config)
        plan = run_agent(client, agent_id, project_dir, config, task)

        if plan:
            log.info(
                "Plan received: score=%d, %d improvements",
                plan.get("score", 0),
                len(plan.get("improvements", [])),
            )
        return plan
    finally:
        client.delete_agent(agent_id)
        log.info("Agent cleaned up.")


def _priority_in_scope(priority: str, allowed: set[str] | None = None) -> bool:
    allowed_priorities = allowed or DEFAULT_IDEA_PRIORITIES
    return priority.upper() in allowed_priorities


def _map_improvement_type(category: str) -> str:
    normalized = category.strip().lower()
    if normalized in BUGFIX_CATEGORIES:
        return "bugfix"
    if normalized in FEATURE_CATEGORIES:
        return "feature"
    return "refactor"


def _build_run_references() -> str:
    refs: list[str] = []
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        refs.append(f"- commit: `{sha}`")

    run_id = os.environ.get("GITHUB_RUN_ID")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if run_id and repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        refs.append(f"- workflow run: {server}/{repository}/actions/runs/{run_id}")

    if not refs:
        refs.append("- run: local invocation (no GitHub Actions metadata available)")
    return "\n".join(refs)


def _build_idea_memo(improvement: dict, references: str) -> str:
    title = improvement.get("title", "Untitled improvement")
    description = improvement.get("description", "No description provided.")
    priority = improvement.get("priority", "P2")
    category = improvement.get("category", "quality")
    idea_type = _map_improvement_type(category)

    return f"""## idea type
{idea_type}

## source
autorefine

## problem
{description}

## proposed approach
- Implement: {title}
- Priority: {priority}
- Category: {category}

## success criteria
- Improvement is represented as a structured idea memo.
- The idea includes source/type labels via file-idea.py.

## risk
- Medium: implementation details still need review by the Builder.

## references
{references}
"""


def _discover_file_idea_options(script_path: Path) -> set[str]:
    help_result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
    )
    if help_result.returncode != 0:
        log.warning(
            "Could not inspect file-idea.py options via --help (code=%d): %s",
            help_result.returncode,
            help_result.stderr.strip(),
        )
        return set()
    option_text = f"{help_result.stdout}\n{help_result.stderr}"
    return {token for token in option_text.split() if token.startswith("--")}


def _effort_to_requests(effort: object) -> str:
    """Map an S/M/L effort estimate to a rough Copilot premium-request count."""
    return {"S": "20", "M": "40", "L": "80"}.get(str(effort or "").strip().upper(), "40")


def is_specified(improvement: dict) -> bool:
    """True when the model supplied a real approach and a checkable success criterion.

    We used to synthesize both from the title so file-idea.py's schema check would
    pass. It passed; the memos were empty. 81 ideas reached the approval queue
    carrying "Implement '<title>' (P0)" as their plan and "'<title>' is implemented
    and usable as described" as their acceptance test — nothing a builder could act
    on, nothing a reviewer could check, and courier's recipient management got built
    four separate times because no one could tell it was already done.

    An unspecified improvement is now skipped rather than dressed up. Silence is a
    better signal than filler: it shows up as a project producing no ideas, which is
    visible, instead of a queue of work nobody can start.
    """
    approach = str(improvement.get("approach", "")).strip()
    criteria = str(improvement.get("success_criteria", "")).strip()
    if not approach or not criteria:
        return False
    # Mirror the governance-side guard: a section must say something its title
    # does not. Cheap local check so we skip before shelling out to file-idea.py.
    title_words = set(re.sub(r"[^0-9a-zA-Z]+", " ", str(improvement.get("title", ""))).lower().split())
    for section in (approach, criteria):
        words = set(re.sub(r"[^0-9a-zA-Z]+", " ", section).lower().split())
        if len(words - title_words - _FILLER_WORDS) < 2:
            return False
    return True


# Words common to every memo, so they cannot be what makes one specific.
_FILLER_WORDS = frozenset("""
implement implemented implementing implementation add added adding usable used
as per this that describe described description work works working correct
correctly proper properly success successful successfully expected regression
regressions existing current flow flows feature features functionality
change changes update updates ensure ensures make makes should must will can
no not any all and or but with without for from into the a an of to in on at
by is are be been being was were do does done p0 p1 p2 p3
enhance enhanced enhancing enhancement improve improved improving improvement
better optimize optimized optimizing optimization support new
""".split())


def _stem(word: str) -> str:
    """Crude suffix stripping, enough to see one idea behind two spellings.

    The proposer produces "Enhance Educational Features" one week and "Enhanced
    Educational Features" the next; without this they share only "educational" and
    read as distinct. Deliberately not a real stemmer - a dependency and a
    linguistics problem to compare two short English noun phrases would be a poor
    trade, and over-stemming only ever costs a false duplicate, which is logged with
    its match and is therefore visible.
    """
    for suffix in ("ements", "ement", "ances", "ance", "ences", "ence", "ings", "ing",
                   "ions", "ion", "ers", "er", "ed", "es", "s"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    # Then drop a trailing "e", so "enhance" and "enhanced" (-> "enhanc") agree.
    # Without this the single most common pair the proposer produces still reads as
    # two distinct ideas.
    if len(word) >= 5 and word.endswith("e"):
        word = word[:-1]
    return word


def _normalize_title(title: str) -> frozenset[str]:
    """The meaningful, stemmed words of a title, for near-duplicate comparison.

    Compared as a word set rather than a string because the proposer rarely repeats
    a title verbatim. Filler is dropped so two titles are not judged similar merely
    for both containing "add" and "the".
    """
    words = re.sub(r"[^0-9a-zA-Z]+", " ", title).lower().split()
    return frozenset(_stem(w) for w in words if w not in _FILLER_WORDS)


def _is_near_duplicate(title: str, existing: list[str], threshold: float = 0.5) -> str | None:
    """The title of an open idea this one substantially repeats, or None.

    Returns the match so the caller can name it in the log - "skipped, duplicates
    #54" is actionable where a bare "skipped" invites someone to disable the check.

    The threshold is set from the cost asymmetry, not tuned until examples pass.
    Two real pairs sit at exactly the same lexical distance - one shared meaningful
    word out of two:

        "Enhance README documentation"  vs  "Enhance onboarding documentation"
        "Improve invoice recognition"   vs  "Improve receipt recognition"

    A human judged the first pair the same work and closed one by hand; the second
    pair is arguably two different features. No word-overlap threshold can tell them
    apart, because the difference is semantic. So the choice is which error to make,
    and the errors are not equally expensive:

      false positive - the idea is skipped and logged with the title it matched.
                       One idea waits a cycle, visibly, and a human can override.
      false negative - a second 10-30 minute Copilot run is bought for work already
                       in flight, plus a duplicate in the approval queue, plus
                       somebody's time closing it.

    The cheap error is the one to prefer, so this leans toward catching duplicates.
    """
    new = _normalize_title(title)
    if not new:
        return None
    for other in existing:
        old = _normalize_title(other)
        if not old:
            continue
        overlap = len(new & old) / min(len(new), len(old))
        if overlap >= threshold:
            return other
    return None


def _open_idea_titles(repo: str) -> list[str]:
    """Titles of idea issues already open on `repo`.

    Failure returns an empty list rather than raising: a GitHub hiccup must not stop
    the run filing legitimate ideas. The cost of the fail-open is one duplicate; the
    cost of failing closed is a silent halt to the whole proposer.
    """
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--label", "idea", "--state", "open",
             "--limit", "100", "--json", "title", "--jq", ".[].title"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            log.warning("Could not read open ideas for %s: %s", repo, out.stderr.strip()[:200])
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not read open ideas for %s: %s", repo, exc)
        return []


def _build_file_idea_command(
    script_path: Path,
    repo: str,
    improvement: dict,
    references: str,
    dry_run: bool,
    needs_approval: bool = False,
) -> list[str]:
    options = _discover_file_idea_options(script_path)
    title = improvement.get("title", "Untitled improvement").strip()
    description = improvement.get("description", "").strip() or "No description provided."
    category = improvement.get("category", "quality")
    idea_type = _map_improvement_type(category)
    memo_body = _build_idea_memo(improvement, references)

    cmd = [sys.executable, str(script_path)]

    def _add_option(name: str, value: str) -> None:
        if name in options and value:
            cmd.extend([name, value])

    _add_option("--repo", repo)
    _add_option("--title", f"[idea] {title}")
    _add_option("--source", "autorefine")
    _add_option("--type", idea_type)
    _add_option("--problem", description)
    # Pass the model's own words through. Callers must gate on is_specified()
    # first — these fields are never fabricated to satisfy validation.
    _add_option("--approach", str(improvement.get("approach", "")).strip())
    _add_option("--success-criteria", str(improvement.get("success_criteria", "")).strip())
    _add_option("--sam-time", "10")
    _add_option("--azure-cost", "0")
    _add_option("--copilot-requests", _effort_to_requests(improvement.get("effort")))
    _add_option(
        "--risk",
        f"Surface: {repo} feature branch. Blast: scoped to this feature. Rollback: revert the PR.",
    )
    _add_option("--references", references)
    for memo_option in ("--body", "--description", "--content"):
        if memo_option in options:
            _add_option(memo_option, memo_body)
            break

    if needs_approval and "--needs-approval" in options:
        cmd.append("--needs-approval")

    if "--dry-run" in options and dry_run:
        cmd.append("--dry-run")

    return cmd


def _resolve_file_idea_script() -> Path | None:
    env_root = os.environ.get("NAURO_GOVERNANCE_PATH")
    if env_root:
        env_script = Path(env_root) / "scripts" / "file-idea.py"
        if env_script.exists():
            return env_script

    governance_checkout_script = REPO_ROOT / ".github-gov" / "scripts" / "file-idea.py"
    if governance_checkout_script.exists():
        return governance_checkout_script

    tmp_repo = Path("/tmp/nauroLabs-github")
    tmp_script = tmp_repo / "scripts" / "file-idea.py"
    if tmp_script.exists():
        return tmp_script

    clone = subprocess.run(
        ["git", "clone", GOVERNANCE_REPO_URL, str(tmp_repo)],
        capture_output=True,
        text=True,
    )
    if clone.returncode == 0 and tmp_script.exists():
        return tmp_script
    if clone.returncode != 0:
        log.warning("Failed to clone governance repo to %s: %s", tmp_repo, clone.stderr.strip())

    log.error(
        "Could not resolve scripts/file-idea.py. Set NAURO_GOVERNANCE_PATH or provide .github-gov checkout."
    )
    return None


def file_ideas_for_plan(
    repo: str,
    plan: dict,
    dry_run: bool = False,
    allowed_priorities: set[str] | None = None,
) -> int:
    script_path = _resolve_file_idea_script()
    if script_path is None:
        return 0

    priorities = allowed_priorities or DEFAULT_IDEA_PRIORITIES
    references = _build_run_references()
    filed = 0
    considered = 0
    unspecified = 0
    duplicates = 0
    seen_titles: set[str] = set()
    # Read once per repo, not per improvement. Ideas already open here are the ones
    # a new proposal can repeat: autoRefine deduplicated only WITHIN a single plan,
    # so the same idea reappeared every run until somebody closed it by hand. The
    # 2026-08-15 triage of the approval queue found 6 duplicate pairs among 81
    # issues, and each duplicate that gets approved buys a second 10-30 minute
    # Copilot run for work already in flight.
    already_open = [] if dry_run else _open_idea_titles(repo)

    for improvement in plan.get("improvements", []):
        priority = str(improvement.get("priority", "P2")).upper()
        if not _priority_in_scope(priority, priorities):
            continue

        title = str(improvement.get("title", "")).strip()
        if not title:
            continue

        title_key = title.lower()
        # Deduplicate case-insensitively so repeated titles in the same plan file once.
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        considered += 1

        # Skip rather than fabricate. See is_specified().
        if not is_specified(improvement):
            unspecified += 1
            log.warning(
                "Skipping unspecified improvement for %s: %r — the model gave no "
                "usable approach/success_criteria, and filling them in is what "
                "flooded the queue with unbuildable ideas.",
                repo, title,
            )
            continue

        match = _is_near_duplicate(title, already_open)
        if match:
            duplicates += 1
            log.info(
                "Skipping duplicate idea for %s: %r substantially repeats the open "
                "issue %r — filing it would buy a second Copilot run for work "
                "already queued.",
                repo, title, match,
            )
            continue

        cmd = _build_file_idea_command(script_path, repo, improvement, references, dry_run=dry_run)
        run = subprocess.run(cmd, capture_output=True, text=True)
        if run.returncode == 0:
            filed += 1
            # Count it as open so two near-duplicates inside one plan cannot both
            # pass - the in-run `seen_titles` check is exact-match only.
            already_open.append(title)
            log.info("Filed idea for %s: %s", repo, title)
        else:
            log.warning(
                "Failed to file idea for %s: %s\nstdout=%s\nstderr=%s",
                repo,
                title,
                run.stdout.strip(),
                run.stderr.strip(),
            )

    # Silence has two very different causes and they need different fixes: the project
    # is genuinely fine, or the model stopped answering the question. Say which.
    # Without this the first looks exactly like the second, which is how a broken
    # ideator stays broken.
    if considered and unspecified == considered:
        log.error(
            "%s: all %d improvement(s) were unspecified — the model is not supplying "
            "approach/success_criteria. This is a prompt or model problem, not a quiet "
            "project; no ideas were filed.",
            repo, considered,
        )
    elif unspecified:
        log.warning("%s: filed %d, dropped %d unspecified.", repo, filed, unspecified)

    return filed


# ── Functional (vision-aligned) ideation — observe-first ───────────────────
# The technical pass above optimizes code quality. This pass optimizes each project's
# north-star (EVOLUTION.md §4): it asks the Foundry agent for *feature* gaps vs the
# project's purpose/goals and its listed similar products.
#
# Gated by AUTOREFINE_FUNCTIONAL_MODE (off | propose | file), default "off":
#   - propose: log the proposed ideas + a Telegram summary, file NOTHING (observe-first).
#   - file:    file them as `idea` + `feature` memos (capped) and auto-feed the build loop.
#   - cards:   file them as `needs-approval` idea memos AND send a Telegram approval card;
#              Copilot is assigned only on a 👍 tap (nauroBot handles it). This is the
#              human-in-the-loop build trigger + the source of decline reasons fed back here.
# The workflow defaults to "propose" so a human reviews the first batches before anything
# auto-builds (EVOLUTION.md §6 guardrails / DGM anti-reward-hacking).


def _functional_mode() -> str:
    """Read the observe-first gate: off | propose | file | cards (default off; unknown → off)."""
    mode = os.environ.get("AUTOREFINE_FUNCTIONAL_MODE", "off").strip().lower()
    return mode if mode in {"off", "propose", "file", "cards"} else "off"


def _resolve_lab_wiki_dir() -> Path | None:
    """Locate the lab wiki (``.github/wiki``) from the governance checkout — best-effort.

    Mirrors ``_resolve_file_idea_script``'s candidates but never clones: wiki context is an
    optional enrichment for functional ideation, so a miss just means "no wiki context"
    (e.g. local dev without a governance checkout).
    """
    candidates: list[Path] = []
    env_root = os.environ.get("NAURO_GOVERNANCE_PATH")
    if env_root:
        candidates.append(Path(env_root) / "wiki")
    candidates.append(REPO_ROOT / ".github-gov" / "wiki")
    candidates.append(Path("/tmp/nauroLabs-github") / "wiki")
    return next((d for d in candidates if d.is_dir()), None)


def _wiki_recent(text: str) -> bool:
    """True when a page's ``**Last verified:**`` date is within WIKI_CONTEXT_RECENT_DAYS.

    Parsed from page content (not file mtime) so it survives a fresh CI checkout.
    """
    m = re.search(r"\*\*Last verified:\*\*\s*(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return False
    try:
        verified = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - verified).days <= WIKI_CONTEXT_RECENT_DAYS


def _wiki_title_and_tldr(stem: str, text: str) -> tuple[str, str]:
    """Pull a page's ``# Title`` and ``## TL;DR`` (or first real paragraph) for the prompt."""
    lines = text.splitlines()
    title = stem
    for line in lines:
        if line.strip().startswith("# "):
            title = line.strip()[2:].strip()
            break
    summary = ""
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## tl;dr"):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("## "):
                    break
                if nxt.strip():
                    body.append(nxt.strip())
            summary = " ".join(body)
            break
    if not summary:
        for line in lines:
            s = line.strip()
            if s and not s.startswith(("#", ">", "**", "|", "-", "`")):
                summary = s
                break
    return title, summary


def _extract_relevant_wiki_insights(project_name: str) -> str:
    """Compact markdown of recent lab-wiki insights/trends relevant to ``project_name``.

    Scores each ``insights/`` and ``trends/`` page by whether it names the project (+2) and
    whether it was verified recently (+1). Project-mentioning pages lead; a couple of recent
    lab-wide pages ride along for cross-pollination. Returns "" when no wiki is available.
    Best-effort — never raises into the ideation path.
    """
    wiki_dir = _resolve_lab_wiki_dir()
    if not wiki_dir:
        return ""
    name_l = project_name.lower()
    mentioned: list[tuple[float, str, str]] = []
    others: list[tuple[float, str, str]] = []
    for sub in ("insights", "trends"):
        d = wiki_dir / sub
        if not d.is_dir():
            continue
        for page in sorted(d.glob("*.md")):
            try:
                text = page.read_text(encoding="utf-8")
            except OSError:
                continue
            names_project = name_l in text.lower()
            score = (2.0 if names_project else 0.0) + (1.0 if _wiki_recent(text) else 0.0)
            if score <= 0:
                continue
            title, summary = _wiki_title_and_tldr(page.stem, text)
            (mentioned if names_project else others).append((score, title, summary))
    if not mentioned and not others:
        return ""
    mentioned.sort(key=lambda t: t[0], reverse=True)
    others.sort(key=lambda t: t[0], reverse=True)
    lead = mentioned[:WIKI_CONTEXT_MAX_PAGES]
    slots = max(0, WIKI_CONTEXT_MAX_PAGES - len(lead))
    chosen = lead + others[: min(WIKI_CONTEXT_OTHER_CAP, slots)]
    lines = [f"- **{title}** — {summary}"[:400].rstrip() for _s, title, summary in chosen]
    block = "\n".join(lines)
    if len(block) > WIKI_CONTEXT_MAX_CHARS:
        block = block[:WIKI_CONTEXT_MAX_CHARS].rsplit("\n", 1)[0]
    return block


def _functional_task(cap: int = FUNCTIONAL_IDEA_CAP, avoid_context: str = "", wiki_context: str = "") -> str:
    """Foundry task that asks for vision-aligned feature ideas, not technical fixes.

    ``avoid_context`` carries recently declined ideas (+ their Telegram reasons) so the
    agent proposes something *different* — closing the loop from a 👎 back into ideation.
    ``wiki_context`` carries recent lab knowledge (memex-ingested insights/trends) so ideas
    can draw on what the lab just learned, not only the project's own files.
    """
    task = (
        "Propose FUNCTIONAL improvements that advance this project's VISION — concrete, buildable "
        "user-facing capabilities that move it toward its stated purpose and goals (and toward "
        "parity with any listed similar products). Every active experiment has meaningful next "
        "capabilities to build: ALWAYS return at least 2 concrete feature ideas, even for a mature "
        "or healthy project — do NOT return an empty plan. This is NOT a technical-quality review: "
        "ignore tests, CI, linting, and dependencies. Explore the repo (read project.yaml, README, "
        "and key source files) to ground each idea in what already exists. For each idea set "
        "category to one of feature/functionality/ux/feature-parity/onboarding, give a specific "
        "title, a 1-2 sentence description, a realistic priority (P0-P2 — most will be P1 or P2) "
        f"and effort (S/M/L). Submit your best {cap + 3} ideas via the submit_plan tool, strongest first."
    )
    if wiki_context:
        task += (
            "\n\n## Recent lab knowledge (memex wiki — insights & trends)\n"
            "Use these where they suggest a capability for THIS project. If an idea is inspired "
            "by one, name it in the description (e.g. \"per lab insight: <title>\") so the source "
            "is traceable. Do not force-fit; ignore irrelevant ones.\n" + wiki_context
        )
    if avoid_context:
        task += "\n\n" + avoid_context
    return task


def plan_functional(
    project_dir: Path,
    config: ProjectConfig,
    model: str | None = None,
    avoid_context: str = "",
) -> dict | None:
    """Run the Foundry plan agent with a functional/vision focus. Returns a plan or None.

    ``avoid_context`` (recently declined ideas + reasons) is threaded into the task prompt.
    """
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    if not endpoint:
        log.error("FOUNDRY_PROJECT_ENDPOINT not set — cannot run functional ideation.")
        return None

    from azure.ai.agents import AgentsClient
    from azure.identity import DefaultAzureCredential

    from agent.foundry_agent import create_agent, run_agent

    client = AgentsClient(endpoint=endpoint, credential=DefaultAzureCredential())
    agent_id = create_agent(client, mode="plan", model=model)
    wiki_context = _extract_relevant_wiki_insights(config.name)
    if wiki_context:
        log.info(
            "Functional ideation for %s: injected %d chars of lab wiki context.",
            config.name,
            len(wiki_context),
        )
    task = _functional_task(avoid_context=avoid_context, wiki_context=wiki_context)
    try:
        # Foundry runs fail transiently (server_error / rate_limit); each run_agent uses
        # a fresh thread, so a retry with the same agent recovers a one-off blip.
        for attempt in range(1, FUNCTIONAL_PLAN_ATTEMPTS + 1):
            plan = run_agent(client, agent_id, project_dir, config, task)
            if plan:
                return plan
            if attempt < FUNCTIONAL_PLAN_ATTEMPTS:
                log.warning(
                    "Functional ideation returned no plan for %s (attempt %d/%d); retrying.",
                    config.name,
                    attempt,
                    FUNCTIONAL_PLAN_ATTEMPTS,
                )
                time.sleep(FUNCTIONAL_RETRY_DELAY_S)
        return None
    finally:
        client.delete_agent(agent_id)
        log.info("Functional agent cleaned up.")


def _normalize_priority(raw: object) -> str:
    """Extract Pn from a messy priority string (e.g. '[P0 — Critical]' → 'P0'; default P2).

    The Foundry agent sometimes returns free-text priorities; an exact 'P0'/'P1'/'P2'
    match would drop every idea. A clean out-of-range value (e.g. 'P3') is preserved so
    it still filters out; only an unrecognisable value defaults to P2.
    """
    match = re.search(r"P\s*(\d)", str(raw).upper())
    return f"P{match.group(1)}" if match else "P2"


def _select_functional_improvements(plan: dict) -> list[dict]:
    """Pick the fileable functional ideas: normalized priority, deduped, hard-capped."""
    selected: list[dict] = []
    seen: set[str] = set()
    for improvement in plan.get("improvements", []):
        if not isinstance(improvement, dict):
            continue
        priority = _normalize_priority(improvement.get("priority"))
        if priority not in FUNCTIONAL_PRIORITIES:
            continue
        title = str(improvement.get("title", "")).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        improvement["priority"] = priority  # clean value for the card + memo
        seen.add(key)
        selected.append(improvement)
        if len(selected) >= FUNCTIONAL_IDEA_CAP:
            break
    return selected


def _format_functional_summary(repo: str, improvements: list[dict]) -> str:
    """Human-readable summary of proposed functional ideas (for logs + Telegram)."""
    lines = [f"🌱 autoRefine functional ideas (PROPOSE-only) — {repo}"]
    for improvement in improvements:
        priority = str(improvement.get("priority", "P2")).upper()
        title = str(improvement.get("title", "Untitled")).strip()
        lines.append(f"• [{priority}] {title}")
    lines.append("Set repo variable AUTOREFINE_FUNCTIONAL_MODE=file to start filing these as idea memos.")
    return "\n".join(lines)


def _notify_functional(summary: str) -> None:
    """Best-effort Telegram notification (no-op if creds / module absent)."""
    try:
        from agent.notify import send_telegram

        send_telegram(summary)
    except Exception:  # pragma: no cover — notify is best-effort
        log.info("Telegram notify unavailable; functional summary logged only.")


def _notify_idea_card(repo: str, issue_number: int, improvement: dict) -> bool:
    """Send a Telegram approval card for a filed idea (best-effort)."""
    try:
        from agent.notify import send_idea_card

        return send_idea_card(
            repo,
            issue_number,
            str(improvement.get("title", "Untitled")).strip(),
            priority=str(improvement.get("priority", "P2")).upper(),
            description=str(improvement.get("description", "")).strip(),
        )
    except Exception:  # pragma: no cover — notify is best-effort
        log.info("Idea card unavailable; idea filed without a Telegram card.")
        return False


_ISSUE_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)")


def _parse_issue_number(url: str) -> int | None:
    """Extract the issue number from a `github.com/OWNER/REPO/issues/N` URL."""
    match = _ISSUE_URL_RE.search(url or "")
    return int(match.group(1)) if match else None


def _file_one_idea(
    script_path: Path,
    repo: str,
    improvement: dict,
    references: str,
    dry_run: bool,
) -> str | None:
    """File a single idea labelled `needs-approval`. Returns the issue URL (or None)."""
    if not is_specified(improvement):
        log.warning(
            "Skipping unspecified improvement for %s: %r — no usable "
            "approach/success_criteria to build or judge against.",
            repo, improvement.get("title", ""),
        )
        return None
    cmd = _build_file_idea_command(
        script_path, repo, improvement, references, dry_run=dry_run, needs_approval=True
    )
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        log.warning(
            "Failed to file idea for %s: %s\nstderr=%s",
            repo, improvement.get("title", ""), run.stderr.strip(),
        )
        return None
    return run.stdout.strip()


def _open_idea_card(repo: str) -> dict | None:
    """Return the oldest un-acted idea card, or ``None`` when there is no pending card.

    Keeps the cards flow to one pending card per project: a manual evaluate run plus the
    daily cron — which GitHub often delays by hours — won't stack a second un-acted card,
    and fresh ideas resume once you 👍/👎 the last one (which clears ``needs-approval``).
    Best-effort: any gh failure returns ``None`` so ideation is never blocked by a hiccup.
    """
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--label", "needs-approval",
             "--state", "open", "--json", "number,title,labels", "--limit", "5"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        issues = json.loads(out.stdout or "[]")
        return issues[-1] if issues else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _idea_card_reminder_due(repo: str, issue_number: int) -> bool:
    """Spread pending-card reminders over several days; manual runs always resurface them."""
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    slot = sum(repo.encode("utf-8")) + issue_number
    return (datetime.now(timezone.utc).date().toordinal() + slot) % IDEA_CARD_REMINDER_DAYS == 0


def _remind_open_idea_card(repo: str, issue: dict) -> bool:
    """Resend a pending idea's Telegram card without filing a duplicate issue."""
    labels = {
        str(label.get("name", "")).upper()
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }
    priority = next((label for label in labels if label in FUNCTIONAL_PRIORITIES), "P2")
    title = re.sub(r"^\[idea\]\s*", "", str(issue.get("title", "")), flags=re.IGNORECASE)
    improvement = {"title": title, "priority": priority}
    return _notify_idea_card(repo, int(issue["number"]), improvement)


def file_and_card_functional_ideas(
    repo: str,
    improvements: list[dict],
    dry_run: bool = False,
    *,
    filer=None,
    carder=None,
) -> int:
    """File each idea as `needs-approval` and send a Telegram approval card.

    The human-in-the-loop build trigger: nothing is assigned to Copilot until a 👍 tap
    (nauroBot handles it). ``filer`` / ``carder`` are injectable for tests. Returns the
    number of ideas that got a card.
    """
    script_path = _resolve_file_idea_script()
    if script_path is None:
        return 0
    references = _build_run_references()
    _file = filer or (lambda imp: _file_one_idea(script_path, repo, imp, references, dry_run))
    _card = carder or _notify_idea_card
    carded = 0
    seen: set[str] = set()
    for improvement in improvements:
        title = str(improvement.get("title", "")).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        url = _file(improvement)
        if not url:
            continue
        if dry_run:
            log.info("[dry-run] would card %s: %s", repo, title)
            carded += 1
            continue
        number = _parse_issue_number(url)
        if number is None:
            log.warning("Filed %s but could not parse an issue number from %r", title, url)
            continue
        if _card(repo, number, improvement):
            carded += 1
    return carded


def _declined_reason(repo: str, number) -> str:
    """The Telegram decline reason logged on an issue, if any. Best-effort."""
    if number is None:
        return ""
    try:
        view = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "comments"],
            capture_output=True, text=True, timeout=30,
        )
        if view.returncode != 0:
            return ""
        comments = json.loads(view.stdout or "{}").get("comments", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for comment in comments:
        body = str(comment.get("body", "")).strip()
        if body.startswith("Feedback from Telegram:"):
            return body.split(":", 1)[1].strip()
    return ""


def _recent_declined_reasons(repo: str, limit: int = 6) -> list[str]:
    """Recently declined idea titles (+ any Telegram reason) for a repo. Best-effort.

    Reads closed `declined` idea issues via `gh`; extracts the reason nauroBot logs as a
    `Feedback from Telegram:` comment. Returns [] on any failure (missing gh, no auth, …).
    """
    try:
        listing = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--label", "declined",
             "--state", "closed", "--limit", str(limit), "--json", "number,title"],
            capture_output=True, text=True, timeout=30,
        )
        if listing.returncode != 0:
            return []
        issues = json.loads(listing.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []

    reasons: list[str] = []
    for issue in issues:
        title = str(issue.get("title", "")).replace("[idea]", "").strip()
        if not title:
            continue
        reason = _declined_reason(repo, issue.get("number"))
        reasons.append(f"{title} — {reason}" if reason else title)
    return reasons


def _format_avoid_context(declined: list[str]) -> str:
    """Render declined ideas as an 'avoid these' block for the generator prompt."""
    if not declined:
        return ""
    lines = [
        "Previously proposed ideas that were DECLINED — do NOT re-propose these; the text "
        "after the dash is the reason, so propose something meaningfully different:"
    ]
    lines.extend(f"- {item}" for item in declined)
    return "\n".join(lines)


def handle_functional_ideas(
    repo: str,
    plan: dict,
    mode: str,
    dry_run: bool = False,
    *,
    notifier=None,
    filer=None,
    carder=None,
) -> list[dict]:
    """Route selected functional ideas per the observe-first gate. Returns the selection.

    ``propose`` logs + notifies and files nothing; ``file`` files capped `feature` memos;
    ``cards`` files them as `needs-approval` + sends Telegram approval cards.
    ``notifier`` / ``filer`` / ``carder`` are injectable for tests.
    """
    selected = _select_functional_improvements(plan)
    if not selected:
        log.info("No functional ideas selected for %s", repo)
        return []

    if mode == "propose":
        summary = _format_functional_summary(repo, selected)
        log.info("Functional ideas (PROPOSE — not filed) for %s:\n%s", repo, summary)
        (notifier or _notify_functional)(summary)
        return selected

    if mode == "file":
        (filer or file_ideas_for_plan)(
            repo,
            {"improvements": selected},
            dry_run=dry_run,
            allowed_priorities=FUNCTIONAL_PRIORITIES,
        )
        log.info("Filed %d functional idea(s) for %s", len(selected), repo)
        return selected

    if mode == "cards":
        pending = None if dry_run else _open_idea_card(repo)
        if pending:
            reminded = False
            if _idea_card_reminder_due(repo, int(pending["number"])):
                reminded = _remind_open_idea_card(repo, pending)
            log.info(
                "%s already has an open idea card awaiting 👍/👎 — %s; skipping a new "
                "idea to keep one pending card per project.",
                repo,
                "reminder sent" if reminded else "reminder not due",
            )
            return selected
        carded = (carder or file_and_card_functional_ideas)(repo, selected, dry_run=dry_run)
        log.info("Sent %d idea approval card(s) for %s", carded, repo)
        return selected

    return []


def _worktree_snapshot(project_dir: Path) -> set[str]:
    """Porcelain status lines, identifying files dirty *before* the agent runs.

    Used to distinguish pre-existing user changes (which must never be
    discarded) from files the agent touched during a run.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    return {line for line in status.stdout.splitlines() if line.strip()}


def _status_path(line: str) -> str:
    """Extract the path from a porcelain status line, handling renames."""
    path = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip('"')


def _rollback_agent_changes(project_dir: Path, baseline: set[str]) -> list[str]:
    """Revert only the files this run dirtied, leaving pre-existing edits alone.

    An ``incomplete`` refine run may already have called ``write_project_file``,
    so partial edits can be sitting in the worktree. Left there, a later run
    would sweep them into a commit as if they were a finished improvement.
    """
    baseline_paths = {_status_path(line) for line in baseline}
    current = _worktree_snapshot(project_dir)
    reverted: list[str] = []

    for line in current:
        path = _status_path(line)
        if path in baseline_paths:
            continue  # pre-existing user change — never touch it

        if line.startswith("??"):
            target = project_dir / path
            try:
                if target.is_file():
                    target.unlink()
                    reverted.append(path)
            except OSError as exc:
                log.warning("Could not remove agent-created file %s: %s", path, exc)
            continue

        restore = subprocess.run(
            ["git", "checkout", "--", path],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if restore.returncode == 0:
            reverted.append(path)
        else:
            log.warning("Could not revert %s: %s", path, restore.stderr.strip())

    if reverted:
        log.warning("Rolled back %d partial change(s) from this run: %s", len(reverted), reverted)
    return reverted


def refine_project(
    project_dir: Path,
    config: ProjectConfig,
    plan: dict,
    repo: str,
    dry_run: bool = False,
    model: str | None = None,
) -> bool:
    """Use Foundry agent to execute improvements, then create a PR."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    if not endpoint:
        log.error("FOUNDRY_PROJECT_ENDPOINT not set — cannot run refine mode.")
        return False

    from azure.ai.agents import AgentsClient
    from azure.identity import DefaultAzureCredential

    from agent.foundry_agent import (
        FoundryRunIncompleteError,
        build_refine_task,
        create_agent,
        run_agent,
    )
    from agent.tools.github_tools import (
        commit_and_push,
        create_branch,
        create_pr,
    )

    client = AgentsClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )

    # Create branch
    import datetime

    today = datetime.date.today().isoformat()
    branch = f"autorefine/improve-{today}"

    if not create_branch(project_dir, branch):
        log.warning("Failed to create branch %s — may already exist", branch)
        # Try with a suffix
        branch = f"autorefine/improve-{today}-2"
        if not create_branch(project_dir, branch):
            log.error("Cannot create branch — skipping refine")
            return False

    agent_id = create_agent(client, mode="refine", model=model)

    try:
        task = build_refine_task(plan, config)
        # Refine writes into the live worktree, so an aborted run must not leave
        # half-applied edits behind for a later run to commit.
        baseline = _worktree_snapshot(project_dir)
        try:
            run_agent(client, agent_id, project_dir, config, task)
        except FoundryRunIncompleteError as exc:
            log.error("Refine run ended incomplete (%s) — rolling back partial changes.", exc.reason)
            _rollback_agent_changes(project_dir, baseline)
            return False

        # Check if agent made any changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        changed_files = [
            line.strip().split(maxsplit=1)[-1]
            for line in status.stdout.strip().splitlines()
            if line.strip()
        ]

        if not changed_files:
            log.info("No files changed — nothing to commit.")
            return False

        log.info("Files changed: %s", changed_files)

        if dry_run:
            log.info("[DRY RUN] Would commit %d files and create PR", len(changed_files))
            return False

        # Commit and push
        improvements = plan.get("improvements", [])
        titles = [imp.get("title", "") for imp in improvements[:5]]
        commit_msg = (
            f"feat(autorefine): apply {len(changed_files)} improvements\n\n"
            + "\n".join(f"- {t}" for t in titles)
        )

        if not commit_and_push(project_dir, commit_msg, branch):
            log.error("Failed to push changes")
            return False

        # Determine base branch
        base = "main"
        base_check = subprocess.run(
            ["git", "branch", "-r", "--list", "origin/master"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if "origin/master" in base_check.stdout:
            base = "master"

        # Create PR
        pr_body = "## autoRefine Improvements\n\n"
        pr_body += f"Score before: {plan.get('score', '?')}/100\n\n"
        pr_body += "### Changes\n"
        for imp in improvements[:5]:
            pr_body += f"- **{imp.get('title', '')}**: {imp.get('description', '')}\n"
        pr_body += (
            "\n### Safety\n"
            "All changes were applied by the autoRefine Foundry agent. "
            "Review carefully before merging.\n"
        )

        pr_created = create_pr(
            project_dir,
            repo,
            title=f"feat(autorefine): {len(changed_files)} improvements",
            body=pr_body,
            branch=branch,
            base=base,
        )

        if pr_created:
            log.info("PR created on %s", repo)
        else:
            log.warning("PR creation failed — changes are on branch %s", branch)

        return pr_created

    finally:
        client.delete_agent(agent_id)
        log.info("Agent cleaned up.")


def run_health_scan_mode(repos: list[str], assign_copilot: bool = True) -> None:
    """Run the NauroLabs health scan (GitHub + Azure cost + App Insights + URLs).

    Sends a Telegram summary via agent.notify and commits a markdown
    report to the governance repo. Distinct from per-project evaluation.
    """
    from agent.health_scan import run_health_scan

    short_repos = [r.split("/")[-1] for r in repos]
    summary = run_health_scan(short_repos, assign_copilot=assign_copilot)
    print(json.dumps(summary, indent=2))


def load_quality_coverage(report_path: str) -> list[dict]:
    """Read evaluation reports so the dashboard can show each score's denominator.

    Dashboard mode scans GitHub metadata and clones nothing, so it cannot re-run
    the quality checks — cloning ~25 repos to redraw a page is not a trade this
    lab makes. The scores already exist in the evaluate-mode report, and reading
    that file is free.

    Returns ``[]`` on any read or parse problem: a missing report costs the page
    one section, never the whole render.
    """
    from agent.parse_scores import extract_score_objects

    try:
        objects = extract_score_objects(report_path)
    except OSError as exc:
        log.warning(
            "Cannot read evaluation report %s: %s — dashboard omits score coverage",
            report_path, exc,
        )
        return []

    log.info("Loaded %d evaluation report(s) from %s", len(objects), report_path)
    return objects


def run_dashboard_mode(
    repos: list[str], output: str = "dashboard.html", report_path: str | None = None,
) -> None:
    """Run the health-scan pipeline and write an HTML dashboard to *output*.

    The dashboard renders the same data as the Markdown health report but as a
    browser-ready HTML file with colour-coded health scores and cost indicators.

    *report_path* is an optional evaluate-mode JSON report. When given, each
    project's 0-100 score is shown beside the number of quality dimensions it
    was actually measured on — see AGENTS.md, "What the score actually
    measures". Without it that section renders empty rather than guessing.
    """
    from agent.dashboard import render_html_dashboard
    from agent.health_scan import (
        analyze_with_ai,
        check_deployed_urls,
        generate_report,
        scan_app_insights,
        scan_azure_costs,
        scan_github,
    )

    github_token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        log.error("GH_TOKEN environment variable not set — cannot run dashboard mode.")
        sys.exit(1)

    short_repos = [r.split("/")[-1] for r in repos]

    log.info("Dashboard: scanning %d repos", len(short_repos))
    github_data = scan_github(github_token, short_repos)
    cost_data = scan_azure_costs()
    app_insights_data = scan_app_insights()
    url_health_data = check_deployed_urls()
    analysis = analyze_with_ai(github_data, cost_data, app_insights_data, url_health_data)

    if report_path:
        analysis["quality_coverage"] = load_quality_coverage(report_path)

    html = render_html_dashboard(
        github_data, cost_data, analysis, app_insights_data, url_health_data
    )
    out_path = Path(output)
    out_path.write_text(html, encoding="utf-8")
    log.info("Dashboard written to %s (%d bytes)", out_path, len(html))
    print(f"Dashboard saved: {out_path}")

    # Also print the Markdown report summary to stdout for CI logs.
    report = generate_report(github_data, cost_data, analysis, app_insights_data, url_health_data)
    print(report)


def main() -> None:
    parser = argparse.ArgumentParser(description="autoRefine — project improvement agent")
    parser.add_argument("--repo", type=str, help="Single repo (owner/name)")
    parser.add_argument("--manifest", type=str, help="Path to workspace-manifest.json")
    parser.add_argument(
        "--mode",
        choices=["evaluate", "plan", "file-ideas", "refine", "health-scan", "pr-cards", "dashboard"],
        default="evaluate",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-4o-mini"),
        help=(
            "Foundry deployment name to use for plan/refine modes. "
            "Defaults to FOUNDRY_DEFAULT_DEPLOYMENT env var, then gpt-4o-mini. "
            "Set to a higher-tier deployment (e.g. gpt-5) for deep analysis."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workdir", default="/tmp/autorefine")
    parser.add_argument(
        "--no-copilot-assign",
        action="store_true",
        help="Disable automatic Copilot assignment for health-scan-created issues",
    )
    parser.add_argument(
        "--output",
        default="dashboard.html",
        help="Output file path for dashboard mode (default: dashboard.html)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "Path to an evaluate-mode JSON report. Dashboard mode reads the 0-100 "
            "scores and their coverage from it (the CI workflow writes "
            "/tmp/autorefine-report.json). Omitted, the Score Coverage section is "
            "empty — dashboard mode clones nothing, so it cannot derive them itself."
        ),
    )
    args = parser.parse_args()
    if args.repo is not None and not _is_valid_repo_slug(args.repo):
        parser.error("--repo must be in the format owner/name")

    # Resolve repo list
    repos: list[str] = []
    swept_manifest = False
    priorities: dict[str, str] = {}
    if args.repo:
        repos = [args.repo]
    elif args.manifest:
        repos = load_repos_from_manifest(Path(args.manifest))
        priorities = load_priorities_from_manifest(Path(args.manifest))
        swept_manifest = True
    else:
        if MANIFEST_PATH.exists():
            repos = load_repos_from_manifest(MANIFEST_PATH)
            priorities = load_priorities_from_manifest(MANIFEST_PATH)
            swept_manifest = True
        else:
            log.error("No --repo or --manifest specified and no default manifest found.")
            sys.exit(1)

    # health-scan mode short-circuits the per-project clone+evaluate loop.
    if args.mode == "health-scan":
        log.info("autoRefine starting — mode=health-scan, %d repos", len(repos))
        run_health_scan_mode(repos, assign_copilot=not args.no_copilot_assign)
        log.info("autoRefine complete.")
        return
    # pr-cards mode also short-circuits the clone loop: it only talks to GitHub + Telegram,
    # carding ready + CI-green Copilot PRs for one-tap approval (nauroBot does the merge).
    if args.mode == "pr-cards":
        from agent.pr_cards import sweep_pr_cards

        log.info("autoRefine starting — mode=pr-cards, %d repos", len(repos))
        carded = sweep_pr_cards(repos, dry_run=args.dry_run)
        log.info("autoRefine complete — carded %d PR(s).", carded)
        return
    # dashboard mode: run health-scan pipeline then emit an HTML dashboard file.
    if args.mode == "dashboard":
        log.info("autoRefine starting — mode=dashboard, %d repos", len(repos))
        run_dashboard_mode(repos, output=args.output, report_path=args.report)
        log.info("autoRefine complete.")
        return
    if args.mode == "refine":
        log.warning(
            "refine mode bypasses the closed evaluator→builder loop and opens PRs directly; "
            "use only for local development."
        )

    config = AutoRefineConfig(
        repos=repos,
        mode=args.mode,
        model=args.model,
        dry_run=args.dry_run,
        workdir=Path(args.workdir),
        gate_on_activity=swept_manifest,
        priorities=priorities,
    )
    config.workdir.mkdir(parents=True, exist_ok=True)

    log.info("autoRefine starting — mode=%s, %d repos", config.mode, len(repos))

    for repo in repos:
        # A scan of every project must survive any single one of them. Two separate bugs
        # have already ended a run part-way through the list (a missing test runner, a
        # mistyped project.yaml), costing the remaining projects their evaluation for
        # reasons that had nothing to do with them. Log the failure against the project it
        # belongs to and carry on.
        try:
            _process_repo(repo, config)
        except Exception:
            log.exception("%s failed — continuing with the remaining projects", repo)

    log.info("autoRefine complete.")


def _process_repo(repo: str, config: AutoRefineConfig) -> None:
    """Clone, evaluate and act on a single repo according to the run mode."""
    name = repo.split("/")[-1]
    project_dir = config.workdir / name

    # Clone / update
    if not clone_repo(repo, project_dir):
        log.warning("Failed to clone %s — skipping", repo)
        return

    # Load project.yaml
    project_config = load_config(project_dir)
    if not project_config:
        log.warning("%s has no project.yaml — skipping", name)
        return

    # Step 1: Always evaluate first (deterministic checks)
    report = evaluate_project(
        project_dir, project_config, RepoContext(slug=repo, token=github_token()),
    )

    if config.mode == "evaluate":
        print(json.dumps(report, indent=2))

    elif config.mode == "plan":
        print(json.dumps(report, indent=2))
        plan = plan_project(
            project_dir, project_config, report["findings"], model=config.model,
        )
        if plan:
            print("\n--- IMPROVEMENT PLAN ---")
            print(json.dumps(plan, indent=2))

    elif config.mode == "refine":
        # Plan first
        plan = plan_project(
            project_dir, project_config, report["findings"], model=config.model,
        )
        if not plan:
            log.warning("No plan generated for %s — skipping refine", name)
            return

        print(json.dumps(plan, indent=2))

        # Execute improvements
        log.info(
            "Executing %d improvements on %s...",
            len(plan.get("improvements", [])),
            name,
        )
        success = refine_project(
            project_dir,
            project_config,
            plan,
            repo,
            dry_run=config.dry_run,
            model=config.model,
        )
        if success:
            log.info("PR created for %s", name)
        else:
            log.info("No PR created for %s (no changes or dry run)", name)

    elif config.mode == "file-ideas":
        print(json.dumps(report, indent=2))

        # The deterministic report above is printed for every project, so scores and
        # the Telegram summary still cover the whole fleet. Only the Foundry planning
        # below is gated, and only on a manifest sweep.
        if config.gate_on_activity:
            planning_due, reason = should_plan_repo(
                repo, project_dir, priority=config.priorities.get(repo)
            )
            if not planning_due:
                log.info("Skipping Foundry planning for %s — %s", name, reason)
                return
            log.info("Planning %s — %s", name, reason)

        plan = plan_project(project_dir, project_config, report["findings"])
        if plan:
            filed_count = file_ideas_for_plan(repo, plan, dry_run=config.dry_run)
            log.info("Filed %d technical ideas for %s", filed_count, name)
        else:
            log.warning("No technical plan generated for %s", name)

        # Observe-first functional ideation (default off; workflow sets 'propose').
        fmode = _functional_mode()
        if fmode != "off" and project_config.stage in FUNCTIONAL_STAGES:
            log.info("Functional ideation (%s) for %s...", fmode, name)
            # In cards mode, feed recently declined ideas (+ reasons) back to the
            # generator so a 👎 in Telegram reshapes the next proposals.
            avoid = (
                _format_avoid_context(_recent_declined_reasons(repo))
                if fmode == "cards"
                else ""
            )
            fplan = plan_functional(
                project_dir, project_config, model=config.model, avoid_context=avoid
            )
            if fplan:
                handle_functional_ideas(repo, fplan, mode=fmode, dry_run=config.dry_run)
            else:
                log.warning("No functional plan generated for %s", name)


if __name__ == "__main__":
    main()
