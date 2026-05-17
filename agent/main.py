"""autoRefine entry point — orchestrates the evaluate → plan → execute cycle."""

import argparse
import json
import logging
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

    # Build evaluation context for LLM
    context = config.to_context()
    log.info("Project context:\n%s", context)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="autoRefine — project improvement agent")
    parser.add_argument("--repo", type=str, help="Single repo (owner/name)")
    parser.add_argument("--manifest", type=str, help="Path to workspace-manifest.json")
    parser.add_argument("--mode", choices=["evaluate", "plan", "refine"], default="evaluate")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workdir", default="/tmp/autorefine")
    args = parser.parse_args()

    # Resolve repo list
    repos: list[str] = []
    if args.repo:
        repos = [args.repo]
    elif args.manifest:
        repos = load_repos_from_manifest(Path(args.manifest))
    else:
        # Default: use NauroLabs manifest
        if MANIFEST_PATH.exists():
            repos = load_repos_from_manifest(MANIFEST_PATH)
        else:
            log.error("No --repo or --manifest specified and no default manifest found.")
            sys.exit(1)

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

        if config.mode == "evaluate":
            report = evaluate_project(project_dir, project_config)
            print(json.dumps(report, indent=2))

        elif config.mode in ("plan", "refine"):
            log.info("Mode '%s' not yet implemented — coming soon", config.mode)
            # TODO: Foundry agent integration for planning + execution

    log.info("autoRefine complete.")


if __name__ == "__main__":
    main()
