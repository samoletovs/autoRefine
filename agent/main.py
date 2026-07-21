"""autoRefine entry point — orchestrates the evaluate → plan → execute cycle."""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.config import AutoRefineConfig, ProjectConfig
from agent.tools.github_tools import clone_repo, read_project_yaml
from agent.tools.quality_tools import run_quality_checks

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("autorefine")

# Suppress verbose Azure SDK HTTP logging
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)

MANIFEST_PATH = Path(__file__).parent.parent.parent / ".github" / "config" / "workspace-manifest.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
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
# Functional ideas are often P2; allow P0-P2 but hard-cap per project to avoid flooding.
FUNCTIONAL_PRIORITIES = {"P0", "P1", "P2"}
FUNCTIONAL_IDEA_CAP = 2
GOVERNANCE_REPO_URL = "https://github.com/samoletovs/nauroLabs-github.git"


def load_repos_from_manifest(manifest_path: Path) -> list[str]:
    """Load repo list from workspace-manifest.json."""
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    return [p["repo"] for p in data.get("projects", []) if p.get("status") != "archived"]


def evaluate_project(project_dir: Path, config: ProjectConfig) -> dict:
    """Run evaluation on a single project. Returns structured findings."""
    log.info("Evaluating: %s (%s)", config.name, config.stage)

    # Technical quality checks (deterministic)
    findings = run_quality_checks(str(project_dir), config)

    report = {
        "project": config.name,
        "stage": config.stage,
        "findings": [
            {"category": f.category, "description": f.description, "priority": f.priority}
            for f in findings
        ],
        "score": max(0, 100 - sum(f.weight for f in findings)),
    }

    log.info(
        "Evaluation complete: %s — score %d/100, %d findings",
        config.name, report["score"], len(findings),
    )
    return report


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
    _add_option("--approach", f"Implement '{title}' ({improvement.get('priority', 'P2')}).")
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
    seen_titles: set[str] = set()

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

        cmd = _build_file_idea_command(script_path, repo, improvement, references, dry_run=dry_run)
        run = subprocess.run(cmd, capture_output=True, text=True)
        if run.returncode == 0:
            filed += 1
            log.info("Filed idea for %s: %s", repo, title)
        else:
            log.warning(
                "Failed to file idea for %s: %s\nstdout=%s\nstderr=%s",
                repo,
                title,
                run.stdout.strip(),
                run.stderr.strip(),
            )

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


def _functional_task(cap: int = FUNCTIONAL_IDEA_CAP, avoid_context: str = "") -> str:
    """Foundry task that asks for vision-aligned feature ideas, not technical fixes.

    ``avoid_context`` carries recently declined ideas (+ their Telegram reasons) so the
    agent proposes something *different* — closing the loop from a 👎 back into ideation.
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
    try:
        return run_agent(
            client, agent_id, project_dir, config, _functional_task(avoid_context=avoid_context)
        )
    finally:
        client.delete_agent(agent_id)
        log.info("Functional agent cleaned up.")


def _select_functional_improvements(plan: dict) -> list[dict]:
    """Pick the fileable functional ideas: allowed priority, deduped, hard-capped."""
    selected: list[dict] = []
    seen: set[str] = set()
    for improvement in plan.get("improvements", []):
        if not isinstance(improvement, dict):
            continue
        priority = str(improvement.get("priority", "P2")).upper()
        if priority not in FUNCTIONAL_PRIORITIES:
            continue
        title = str(improvement.get("title", "")).strip()
        key = title.lower()
        if not title or key in seen:
            continue
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
        carded = (carder or file_and_card_functional_ideas)(repo, selected, dry_run=dry_run)
        log.info("Sent %d idea approval card(s) for %s", carded, repo)
        return selected

    return []


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

    from agent.foundry_agent import build_refine_task, create_agent, run_agent
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
        run_agent(client, agent_id, project_dir, config, task)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="autoRefine — project improvement agent")
    parser.add_argument("--repo", type=str, help="Single repo (owner/name)")
    parser.add_argument("--manifest", type=str, help="Path to workspace-manifest.json")
    parser.add_argument(
        "--mode",
        choices=["evaluate", "plan", "file-ideas", "refine", "health-scan"],
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
    args = parser.parse_args()
    if args.repo is not None and not _is_valid_repo_slug(args.repo):
        parser.error("--repo must be in the format owner/name")

    # Resolve repo list
    repos: list[str] = []
    if args.repo:
        repos = [args.repo]
    elif args.manifest:
        repos = load_repos_from_manifest(Path(args.manifest))
    else:
        if MANIFEST_PATH.exists():
            repos = load_repos_from_manifest(MANIFEST_PATH)
        else:
            log.error("No --repo or --manifest specified and no default manifest found.")
            sys.exit(1)

    # health-scan mode short-circuits the per-project clone+evaluate loop.
    if args.mode == "health-scan":
        log.info("autoRefine starting — mode=health-scan, %d repos", len(repos))
        run_health_scan_mode(repos, assign_copilot=not args.no_copilot_assign)
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
    )
    config.workdir.mkdir(parents=True, exist_ok=True)

    log.info("autoRefine starting — mode=%s, %d repos", config.mode, len(repos))

    for repo in repos:
        name = repo.split("/")[-1]
        project_dir = config.workdir / name

        # Clone / update
        if not clone_repo(repo, project_dir):
            log.warning("Failed to clone %s — skipping", repo)
            continue

        # Load project.yaml
        project_config = load_config(project_dir)
        if not project_config:
            log.warning("%s has no project.yaml — skipping", name)
            continue

        # Step 1: Always evaluate first (deterministic checks)
        report = evaluate_project(project_dir, project_config)

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
                continue

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

    log.info("autoRefine complete.")


if __name__ == "__main__":
    main()
