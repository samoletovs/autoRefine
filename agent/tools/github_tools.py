"""GitHub tools — clone repos, read files, create PRs."""

import logging
import subprocess
from pathlib import Path

from agent.config import ProjectConfig

log = logging.getLogger(__name__)


def clone_repo(repo: str, target_dir: Path) -> bool:
    """Clone or pull a GitHub repo."""
    if target_dir.exists():
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0

    result = subprocess.run(
        ["gh", "repo", "clone", repo, str(target_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def read_project_yaml(project_dir: Path) -> ProjectConfig | None:
    """Read project.yaml from a project directory."""
    yaml_path = project_dir / "project.yaml"
    if not yaml_path.exists():
        return None

    try:
        return ProjectConfig.from_yaml(yaml_path)
    except Exception:
        log.exception("Failed to parse %s", yaml_path)
        return None


def read_file_safe(path: Path, max_lines: int = 200) -> str:
    """Read a file safely, returning empty string on failure."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[:max_lines])
    except OSError:
        return ""


def create_branch(project_dir: Path, branch_name: str) -> bool:
    """Create and switch to a new branch."""
    result = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def commit_and_push(project_dir: Path, message: str, branch: str) -> bool:
    """Stage, commit, and push changes."""
    subprocess.run(["git", "add", "-A"], cwd=str(project_dir), capture_output=True)

    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


def create_pr(
    project_dir: Path,
    repo: str,
    title: str,
    body: str,
    branch: str,
    base: str = "main",
) -> bool:
    """Create a pull request via gh CLI and enable auto-merge."""
    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", repo,
            "--title", title,
            "--body", body,
            "--head", branch,
            "--base", base,
        ],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False

    # Enable auto-merge (squash) so the PR merges when CI passes
    subprocess.run(
        [
            "gh", "pr", "merge",
            "--repo", repo,
            "--head", branch,
            "--squash",
            "--auto",
        ],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=15,
    )

    return True
