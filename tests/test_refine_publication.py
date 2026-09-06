"""Refine publishes only agent changes that pass a deterministic final test run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import main as agent_main
from agent.config import ProjectConfig


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


@pytest.fixture()
def config() -> ProjectConfig:
    return ProjectConfig(name="demo", purpose="", users="", stage="active")


def _patch_refine(
    monkeypatch: pytest.MonkeyPatch,
    run_agent: object,
    test_result: object,
) -> SimpleNamespace:
    client = SimpleNamespace(delete_agent=MagicMock())
    create_branch = MagicMock(return_value=True)
    create_agent = MagicMock(return_value="agent-1")
    final_tests = MagicMock(
        return_value=test_result if isinstance(test_result, str) else json.dumps(test_result)
    )
    commit_and_push = MagicMock(return_value=True)
    create_pr = MagicMock(return_value=True)

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/foundry")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **_kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **_kw: object())
    monkeypatch.setattr("agent.foundry_agent.create_agent", create_agent)
    monkeypatch.setattr("agent.foundry_agent.run_agent", run_agent)
    monkeypatch.setattr("agent.foundry_agent.build_refine_task", lambda *_a, **_kw: "task")
    monkeypatch.setattr("agent.foundry_agent._handle_run_tests", final_tests)
    monkeypatch.setattr("agent.tools.github_tools.create_branch", create_branch)
    monkeypatch.setattr("agent.tools.github_tools.commit_and_push", commit_and_push)
    monkeypatch.setattr("agent.tools.github_tools.create_pr", create_pr)

    return SimpleNamespace(
        client=client,
        create_branch=create_branch,
        create_agent=create_agent,
        final_tests=final_tests,
        commit_and_push=commit_and_push,
        create_pr=create_pr,
    )


@pytest.mark.parametrize(
    "test_result",
    [
        {"passed": False, "output": "1 failed"},
        {"passed": False, "error": "No test runner detected", "retryable": False},
        {"passed": False, "error": "Test run timed out after 300s"},
        {"passed": False, "error": "Could not run tests: access denied"},
        {"passed": 1, "output": "non-boolean result"},
        {"passed": "true"},
        {},
        None,
        [],
        "not-json",
    ],
    ids=[
        "failure", "runner-unavailable", "timeout", "runner-error", "non-boolean",
        "string-boolean", "missing-pass", "null", "list", "malformed",
    ],
)
def test_refine_rolls_back_and_never_publishes_without_explicit_test_pass(
    repo: Path,
    config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    test_result: object,
) -> None:
    def fake_run_agent(*_args: object, **_kwargs: object) -> dict[str, object]:
        (repo / "agent_change.py").write_text("# generated\n", encoding="utf-8")
        return {"status": "completed"}

    spies = _patch_refine(monkeypatch, fake_run_agent, test_result)

    result = agent_main.refine_project(
        repo, config, {"improvements": [], "score": 50}, "owner/demo"
    )

    assert result is False
    spies.final_tests.assert_called_once_with(repo, {})
    spies.commit_and_push.assert_not_called()
    spies.create_pr.assert_not_called()
    assert not (repo / "agent_change.py").exists()
    assert agent_main._worktree_snapshot(repo) == set()


def test_refine_publishes_only_after_final_tests_pass(
    repo: Path,
    config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent(*_args: object, **_kwargs: object) -> dict[str, object]:
        (repo / "agent_change.py").write_text("# generated\n", encoding="utf-8")
        return {"status": "completed"}

    spies = _patch_refine(
        monkeypatch,
        fake_run_agent,
        {"passed": True, "output": "4 passed"},
    )

    result = agent_main.refine_project(
        repo, config, {"improvements": [], "score": 50}, "owner/demo"
    )

    assert result is True
    spies.final_tests.assert_called_once_with(repo, {})
    spies.commit_and_push.assert_called_once()
    spies.create_pr.assert_called_once()


@pytest.mark.parametrize("dry_run", [False, True])
def test_refine_without_publication_does_not_run_redundant_final_tests(
    repo: Path,
    config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    def fake_run_agent(*_args: object, **_kwargs: object) -> dict[str, object]:
        if dry_run:
            (repo / "agent_change.py").write_text("# generated\n", encoding="utf-8")
        return {"status": "completed"}

    spies = _patch_refine(
        monkeypatch,
        fake_run_agent,
        {"passed": True, "output": "4 passed"},
    )

    result = agent_main.refine_project(
        repo, config, {"improvements": [], "score": 50}, "owner/demo", dry_run=dry_run,
    )

    assert result is False
    spies.final_tests.assert_not_called()
    spies.commit_and_push.assert_not_called()
    spies.create_pr.assert_not_called()


def test_refine_refuses_dirty_worktree_before_paid_work_and_preserves_user_edit(
    repo: Path,
    config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "tracked.txt").write_text("user work in progress\n", encoding="utf-8")
    run_agent = MagicMock(return_value={"status": "completed"})
    spies = _patch_refine(
        monkeypatch,
        run_agent,
        {"passed": True, "output": "4 passed"},
    )

    result = agent_main.refine_project(
        repo, config, {"improvements": [], "score": 50}, "owner/demo"
    )

    assert result is False
    spies.create_branch.assert_not_called()
    spies.create_agent.assert_not_called()
    run_agent.assert_not_called()
    spies.final_tests.assert_not_called()
    spies.commit_and_push.assert_not_called()
    spies.create_pr.assert_not_called()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "user work in progress\n"
