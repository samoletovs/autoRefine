"""autoRefine entry point — orchestrates the evaluate → plan → execute cycle."""

import argparse
import json
import logging
import os
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
        result = run_agent(client, agent_id, project_dir, config, task)

        # Check if agent made any changes
        import subprocess

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
        choices=["evaluate", "plan", "refine", "health-scan"],
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
        project_config = read_project_yaml(project_dir)
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

    log.info("autoRefine complete.")


if __name__ == "__main__":
    main()
