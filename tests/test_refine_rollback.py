"""Refine must be transactional: an incomplete run leaves no partial writes behind.

An ``incomplete`` refine run may already have called ``write_project_file``. If
those half-applied edits stay in the worktree, a later run's ``git status``
sweep would commit them as if they were a finished improvement. These tests use
a real git repo so the rollback is proven against actual git behaviour.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import main as agent_main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    (tmp_path / "user_edited.txt").write_text("user original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_rollback_reverts_agent_modified_and_created_files(repo: Path) -> None:
    baseline = agent_main._worktree_snapshot(repo)
    assert baseline == set()

    # Simulate a partially-applied refine run.
    (repo / "tracked.txt").write_text("agent partial edit\n", encoding="utf-8")
    (repo / "new_file.py").write_text("# half-written\n", encoding="utf-8")

    reverted = agent_main._rollback_agent_changes(repo, baseline)

    assert set(reverted) == {"tracked.txt", "new_file.py"}
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    assert not (repo / "new_file.py").exists()
    assert agent_main._worktree_snapshot(repo) == set()


def test_rollback_preserves_pre_existing_user_changes(repo: Path) -> None:
    # A change the user already had in the worktree before autoRefine ran.
    (repo / "user_edited.txt").write_text("user work in progress\n", encoding="utf-8")
    (repo / "user_scratch.txt").write_text("untracked user file\n", encoding="utf-8")
    baseline = agent_main._worktree_snapshot(repo)

    (repo / "tracked.txt").write_text("agent partial edit\n", encoding="utf-8")

    reverted = agent_main._rollback_agent_changes(repo, baseline)

    assert reverted == ["tracked.txt"]
    assert (repo / "user_edited.txt").read_text(encoding="utf-8") == "user work in progress\n"
    assert (repo / "user_scratch.txt").exists()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"


def test_refine_project_rolls_back_and_returns_false_on_incomplete(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.config import ProjectConfig
    from agent.foundry_agent import FoundryRunIncompleteError

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/foundry")

    committed: list[str] = []

    def fake_run_agent(_client, _agent_id, project_dir, _config, _task):
        # The agent writes a file, then the run is cut short.
        (Path(project_dir) / "half_done.py").write_text("# partial\n", encoding="utf-8")
        raise FoundryRunIncompleteError("run-1", "max_prompt_tokens")

    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **_kw: SimpleNamespace(delete_agent=lambda _id: None))
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **_kw: object())
    monkeypatch.setattr("agent.foundry_agent.create_agent", lambda *_a, **_kw: "agent-1")
    monkeypatch.setattr("agent.foundry_agent.run_agent", fake_run_agent)
    monkeypatch.setattr("agent.foundry_agent.build_refine_task", lambda *_a, **_kw: "task")
    monkeypatch.setattr("agent.tools.github_tools.create_branch", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "agent.tools.github_tools.commit_and_push",
        lambda *_a, **_kw: committed.append("pushed") or True,
    )
    monkeypatch.setattr("agent.tools.github_tools.create_pr", lambda *_a, **_kw: "url")

    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    result = agent_main.refine_project(
        repo, config, {"improvements": [], "score": 50}, "owner/demo"
    )

    assert result is False
    assert not (repo / "half_done.py").exists(), "partial write survived an incomplete run"
    assert committed == [], "incomplete run must not commit or push"
