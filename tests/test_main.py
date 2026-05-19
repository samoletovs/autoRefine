"""Tests for issue #idea-add-unit-tests-for-agent-main-and-foundry-agent."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.config import ProjectConfig
from agent.main import load_config, load_repos_from_manifest, main


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(
        name="demo",
        purpose="Demo project",
        users="Engineers",
        stage="active",
        goals=["Ship quality"],
        similar=["foo"],
        quality=["tests"],
    )


def test_load_repos_from_manifest_filters_archived(tmp_path: Path) -> None:
    manifest = tmp_path / "workspace-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "projects": [
                    {"repo": "samoletovs/autoRefine", "status": "active"},
                    {"repo": "samoletovs/archived", "status": "archived"},
                ]
            }
        ),
        encoding="utf-8",
    )

    repos = load_repos_from_manifest(manifest)
    assert repos == ["samoletovs/autoRefine"]


def test_load_config_reads_project_yaml(tmp_path: Path) -> None:
    (tmp_path / "project.yaml").write_text(
        "\n".join(
            [
                "name: demo",
                "purpose: Demo purpose",
                "users: Engineers",
                "stage: active",
                "quality:",
                "  - tests",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config is not None
    assert config.name == "demo"
    assert config.purpose == "Demo purpose"
    assert config.quality == ["tests"]


@pytest.mark.parametrize("mode", ["evaluate", "plan", "refine", "health-scan"])
def test_main_accepts_valid_modes(
    tmp_path: Path, project_config: ProjectConfig, mode: str
) -> None:
    with (
        patch.object(sys, "argv", ["autorefine", "--repo", "owner/repo", "--mode", mode]),
        patch("agent.main.clone_repo", return_value=True) as mock_clone,
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [], "score": 100}) as mock_eval,
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 100}) as mock_plan,
        patch("agent.main.refine_project", return_value=False) as mock_refine,
        patch("agent.main.run_health_scan_mode") as mock_health_scan,
    ):
        main()

    if mode == "health-scan":
        mock_health_scan.assert_called_once_with(["owner/repo"], assign_copilot=True)
        mock_clone.assert_not_called()
        mock_eval.assert_not_called()
        mock_plan.assert_not_called()
        mock_refine.assert_not_called()
    elif mode == "evaluate":
        mock_clone.assert_called_once()
        mock_eval.assert_called_once()
        mock_plan.assert_not_called()
        mock_refine.assert_not_called()
    elif mode == "plan":
        mock_clone.assert_called_once()
        mock_eval.assert_called_once()
        mock_plan.assert_called_once()
        mock_refine.assert_not_called()
    elif mode == "refine":
        mock_clone.assert_called_once()
        mock_eval.assert_called_once()
        assert mock_plan.call_count == 1
        mock_refine.assert_called_once()
    else:
        mock_clone.assert_called_once()


def test_main_rejects_invalid_mode() -> None:
    with (
        patch.object(sys, "argv", ["autorefine", "--repo", "owner/repo", "--mode", "invalid"]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("repo", "expected_exit_code"),
    [
        ("owner", 2),
        ("owner/repo/extra", 2),
        ("/repo", 2),
        ("owner/", 2),
        ("", 1),
        ("owner /repo", 2),
        ("owner/re po", 2),
        ("owner/repo!", 2),
        ("own*er/repo", 2),
        ("owner/..repo", 2),
    ],
)
def test_main_rejects_malformed_repo(repo: str, expected_exit_code: int) -> None:
    with (
        patch.object(sys, "argv", ["autorefine", "--repo", repo, "--mode", "evaluate"]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == expected_exit_code


def test_main_plan_mode_passes_model_to_plan_project(project_config: ProjectConfig) -> None:
    with (
        patch.object(
            sys,
            "argv",
            ["autorefine", "--repo", "owner/repo", "--mode", "plan", "--model", "gpt-4.1"],
        ),
        patch("agent.main.clone_repo", return_value=True),
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [], "score": 100}),
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 100}) as mock_plan,
    ):
        main()

    assert mock_plan.call_args.kwargs["model"] == "gpt-4.1"


def test_main_refine_mode_passes_model_to_plan_and_refine(
    project_config: ProjectConfig,
) -> None:
    with (
        patch.object(
            sys,
            "argv",
            ["autorefine", "--repo", "owner/repo", "--mode", "refine", "--model", "gpt-4.1"],
        ),
        patch("agent.main.clone_repo", return_value=True),
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [], "score": 100}),
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 100}) as mock_plan,
        patch("agent.main.refine_project", return_value=False) as mock_refine,
    ):
        main()

    assert mock_plan.call_args.kwargs["model"] == "gpt-4.1"
    assert mock_refine.call_args.kwargs["model"] == "gpt-4.1"


def test_plan_project_passes_model_to_create_agent(
    project_config: ProjectConfig, tmp_path: Path
) -> None:
    from agent import main as main_module

    fake_client = SimpleNamespace(delete_agent=MagicMock())

    with (
        patch.dict("os.environ", {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test"}),
        patch("azure.ai.agents.AgentsClient", return_value=fake_client),
        patch("azure.identity.DefaultAzureCredential"),
        patch("agent.foundry_agent.create_agent", return_value="agent-1") as mock_create,
        patch("agent.foundry_agent.build_plan_task", return_value="plan-task"),
        patch("agent.foundry_agent.run_agent", return_value={"score": 80, "improvements": []}),
    ):
        result = main_module.plan_project(tmp_path, project_config, findings=[], model="gpt-4.1")

    assert result == {"score": 80, "improvements": []}
    assert mock_create.call_args.kwargs["model"] == "gpt-4.1"


def test_refine_project_passes_model_to_create_agent(
    project_config: ProjectConfig, tmp_path: Path
) -> None:
    from agent import main as main_module

    fake_client = SimpleNamespace(delete_agent=MagicMock())

    with (
        patch.dict("os.environ", {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test"}),
        patch("azure.ai.agents.AgentsClient", return_value=fake_client),
        patch("azure.identity.DefaultAzureCredential"),
        patch("agent.tools.github_tools.create_branch", return_value=True),
        patch("agent.foundry_agent.create_agent", return_value="agent-2") as mock_create,
        patch("agent.foundry_agent.build_refine_task", return_value="refine-task"),
        patch("agent.foundry_agent.run_agent", return_value={"score": 99}),
        patch(
            "subprocess.run",
            return_value=SimpleNamespace(stdout=" M README.md\n", returncode=0),
        ),
    ):
        result = main_module.refine_project(
            tmp_path,
            project_config,
            plan={"score": 42, "improvements": [{"title": "Improve tests"}]},
            repo="owner/repo",
            dry_run=True,
            model="gpt-4.1",
        )

    assert result is False
    assert mock_create.call_args.kwargs["model"] == "gpt-4.1"
