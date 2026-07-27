"""Tests for issue #idea-add-unit-tests-for-agent-main-and-foundry-agent."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.config import ProjectConfig
from agent.main import evaluate_project, load_config, load_repos_from_manifest, main


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(
        name="demo",
        purpose="Test project",
        users="Developers",
        stage="mvp",
        goals=["Ship tests"],
        similar=[],
        quality=["tests"],
    )


def test_load_repos_from_manifest_filters_archived(tmp_path: Path) -> None:
    manifest = tmp_path / "workspace-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "projects": [
                    {"repo": "owner/active", "status": "active"},
                    {"repo": "owner/archived", "status": "archived"},
                    {"repo": "owner/default-active"},
                ]
            }
        ),
        encoding="utf-8",
    )

    repos = load_repos_from_manifest(manifest)
    assert repos == ["owner/active", "owner/default-active"]


def test_load_config_reads_project_yaml(tmp_path: Path) -> None:
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / "project.yaml").write_text(
        "\n".join(
            [
                "name: demo",
                "purpose: tests",
                "users: devs",
                "stage: mvp",
                "quality:",
                "  - tests",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_config(project_dir)
    assert loaded is not None
    assert loaded.name == "demo"
    assert loaded.quality == ["tests"]


@pytest.mark.parametrize("mode", ["evaluate", "plan", "refine", "health-scan", "dashboard"])
def test_main_accepts_valid_modes(
    tmp_path: Path, project_config: ProjectConfig, mode: str
) -> None:
    argv = [
        "autorefine",
        "--repo",
        "owner/repo",
        "--mode",
        mode,
        "--workdir",
        str(tmp_path),
    ]
    with (
        patch("sys.argv", argv),
        patch("agent.main.clone_repo", return_value=True),
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [], "score": 100}),
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 100}),
        patch("agent.main.refine_project", return_value=False),
        patch("agent.main.run_health_scan_mode"),
        patch("agent.main.run_dashboard_mode"),
        patch("builtins.print"),
    ):
        main()


def test_main_rejects_invalid_mode() -> None:
    with patch("sys.argv", ["autorefine", "--repo", "owner/repo", "--mode", "bad"]):
        with pytest.raises(SystemExit, match="2"):
            main()


def test_evaluate_project_adds_feature_suggestions_from_project_yaml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("agent.main.run_quality_checks", lambda _p, _c: [])
    config = ProjectConfig(
        name="demo",
        purpose="Test",
        users="Devs",
        stage="active",
        goals=["Improve onboarding"],
        similar=["CompetitorX"],
        quality=[],
    )

    report = evaluate_project(tmp_path, config)

    suggestions = report["feature_suggestions"]
    assert len(suggestions) == 2
    assert "Improve onboarding" in suggestions[0]["title"]
    assert "CompetitorX" in suggestions[1]["title"]


def test_evaluate_project_returns_empty_feature_suggestions_without_inputs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("agent.main.run_quality_checks", lambda _p, _c: [])
    config = ProjectConfig(
        name="demo",
        purpose="Test",
        users="Devs",
        stage="active",
        goals=[],
        similar=[],
        quality=[],
    )

    report = evaluate_project(tmp_path, config)

    assert report["feature_suggestions"] == []


@pytest.mark.parametrize(
    "repo",
    [
        "owner",
        "owner/repo/extra",
        "/repo",
        "owner/",
        "",
    ],
)
def test_main_rejects_malformed_repo(repo: str) -> None:
    with patch("sys.argv", ["autorefine", "--repo", repo]):
        with pytest.raises(SystemExit, match="2"):
            main()


def test_main_exits_when_no_repo_or_manifest_and_default_missing() -> None:
    with (
        patch("sys.argv", ["autorefine"]),
        patch("agent.main.MANIFEST_PATH", Path("/definitely/missing/workspace-manifest.json")),
    ):
        with pytest.raises(SystemExit, match="1"):
            main()


def test_main_health_scan_short_circuits_per_repo_flow() -> None:
    with (
        patch("sys.argv", ["autorefine", "--repo", "owner/repo", "--mode", "health-scan"]),
        patch("agent.main.run_health_scan_mode") as mock_scan,
        patch("agent.main.clone_repo") as mock_clone,
    ):
        main()

    mock_scan.assert_called_once_with(["owner/repo"], assign_copilot=True)
    mock_clone.assert_not_called()


def test_main_plan_passes_model_to_plan_project(
    tmp_path: Path, project_config: ProjectConfig
) -> None:
    with (
        patch(
            "sys.argv",
            [
                "autorefine",
                "--repo",
                "owner/repo",
                "--mode",
                "plan",
                "--model",
                "gpt-4.1",
                "--workdir",
                str(tmp_path),
            ],
        ),
        patch("agent.main.clone_repo", return_value=True),
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [{"priority": "P1"}], "score": 90}),
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 95}) as mock_plan,
        patch("builtins.print"),
    ):
        main()

    assert mock_plan.call_count == 1
    assert mock_plan.call_args.kwargs["model"] == "gpt-4.1"


def test_main_refine_passes_model_to_plan_and_refine(
    tmp_path: Path, project_config: ProjectConfig
) -> None:
    with (
        patch(
            "sys.argv",
            [
                "autorefine",
                "--repo",
                "owner/repo",
                "--mode",
                "refine",
                "--model",
                "gpt-4.1",
                "--workdir",
                str(tmp_path),
            ],
        ),
        patch("agent.main.clone_repo", return_value=True),
        patch("agent.main.load_config", return_value=project_config),
        patch("agent.main.evaluate_project", return_value={"findings": [{"priority": "P1"}], "score": 90}),
        patch("agent.main.plan_project", return_value={"improvements": [], "score": 95}) as mock_plan,
        patch("agent.main.refine_project", return_value=False) as mock_refine,
        patch("builtins.print"),
    ):
        main()

    assert mock_plan.call_args.kwargs["model"] == "gpt-4.1"
    assert mock_refine.call_args.kwargs["model"] == "gpt-4.1"


def test_plan_project_passes_model_to_create_agent(project_config: ProjectConfig, tmp_path: Path) -> None:
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
    fake_client.delete_agent.assert_called_once_with("agent-1")


def test_refine_project_passes_model_to_create_agent(project_config: ProjectConfig, tmp_path: Path) -> None:
    from agent import main as main_module

    fake_client = SimpleNamespace(delete_agent=MagicMock())

    with (
        patch.dict("os.environ", {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test"}),
        patch("azure.ai.agents.AgentsClient", return_value=fake_client),
        patch("azure.identity.DefaultAzureCredential"),
        patch("agent.foundry_agent.create_agent", return_value="agent-2") as mock_create,
        patch("agent.foundry_agent.build_refine_task", return_value="refine-task"),
        patch("agent.foundry_agent.run_agent", return_value={"score": 99}),
        patch("agent.tools.github_tools.create_branch", return_value=True),
        patch(
            "subprocess.run",
            side_effect=[
                SimpleNamespace(stdout=" M README.md\n", returncode=0),
                SimpleNamespace(stdout="", returncode=0),
            ],
        ),
        patch("agent.tools.github_tools.commit_and_push", return_value=True),
        patch("agent.tools.github_tools.create_pr", return_value=True),
    ):
        result = main_module.refine_project(
            tmp_path, project_config, {"improvements": [{"title": "x", "description": "y"}]}, "owner/repo", model="gpt-4.1"
        )

    assert result is True
    assert mock_create.call_args.kwargs["model"] == "gpt-4.1"
    fake_client.delete_agent.assert_called_once_with("agent-2")
