"""Selective model costs and agent-version bounds, without service calls."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.resources.responses import Responses

from agent import foundry_agent as agent
from agent import main
from agent.config import AutoRefineConfig, ProjectConfig
from tests.test_foundry_agent import FakeAgents
from tests.test_foundry_loop_guards import _ToolLoopClient, agent_for


@pytest.fixture(autouse=True)
def _approve_plan(approve_agent_definition: Callable[..., None]) -> None:
    approve_agent_definition()


@pytest.mark.parametrize(("model", "prompt_cap", "completion_cap"), [
    ("gpt-6-luna", 200_000, 16_000),
    ("gpt-6-sol", 40_000, 4_000),
])
def test_budget_parameters_do_not_imply_model_admission(
    model: str, prompt_cap: int, completion_cap: int,
) -> None:
    budget = agent._run_budget(Responses.create, model)
    assert budget.max_prompt_tokens == prompt_cap
    assert budget.max_completion_tokens == completion_cap
    assert budget.truncation == "auto"


def test_kwargs_only_output_control_cannot_buy_an_unbounded_run() -> None:
    def unsupported(*, truncation: str, previous_response_id: str, **kwargs: object) -> None:
        pass

    with pytest.raises(agent.FoundryPromptBudgetUnsupportedError, match="max_output_tokens"):
        agent._run_budget(unsupported)


def test_luna_agent_definition_carries_no_sampling_parameters() -> None:
    agents = FakeAgents()
    version = agent.create_agent(SimpleNamespace(agents=agents), mode="plan", model="gpt-6-luna")
    definition = agents.created[0]["definition"]
    assert version.model == definition.model == "gpt-6-luna"
    assert definition.reasoning.effort == "none"
    assert "max_tokens" not in definition.as_dict()
    assert definition.temperature is None


def test_luna_and_caps_reach_every_request(tmp_path: Path) -> None:
    client = _ToolLoopClient(lambda n: None)
    agent.run_agent(
        client, agent_for("gpt-6-luna"), tmp_path,
        ProjectConfig(name="synthetic", purpose="", users="", stage="active"),
        "Synthetic task", mode="plan", model="gpt-6-luna",
    )
    (request,) = client.requests
    assert request["extra_body"]["agent_reference"]["name"] == "autorefine-plan"
    assert request["max_output_tokens"] == 16_000
    assert request["truncation"] == "auto"
    assert "temperature" not in request and "top_p" not in request


def test_unconfigured_job_defaults_to_luna() -> None:
    assert AutoRefineConfig(repos=[]).model == "gpt-6-luna"
    assert agent.DEFAULT_DEPLOYMENT == "gpt-6-luna"
    agents = FakeAgents()
    agent.create_agent(SimpleNamespace(agents=agents))
    assert agents.created[0]["definition"].model == "gpt-6-luna"


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-4o-mini", "gpt-4.1"])
@pytest.mark.parametrize("mode", ["plan", "refine"])
def test_unsupported_models_are_blocked_before_any_agent_version_call(
    model: str, mode: str,
) -> None:
    project = Mock()
    with pytest.raises(ValueError, match="only gpt-6-luna is enabled"):
        agent.create_agent(project, mode=mode, model=model)
    assert project.mock_calls == []


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-4o-mini", "gpt-4.1"])
def test_unsupported_models_cannot_run_an_existing_agent(
    model: str, tmp_path: Path,
) -> None:
    client = Mock()
    with pytest.raises(ValueError, match="only gpt-6-luna is enabled"):
        agent.run_agent(
            client, agent_for(model), tmp_path,
            ProjectConfig(name="synthetic", purpose="", users="", stage="active"),
            "Synthetic task", mode="plan", model=model,
        )
    assert client.mock_calls == []


def test_iac_and_env_defaults_name_luna() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "infrastructure" / "main.bicep").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "param foundryDeployment string = 'gpt-6-luna'" in template
    assert "@allowed(['gpt-6-luna'])" in template
    assert "FOUNDRY_DEFAULT_DEPLOYMENT=gpt-6-luna" in example
    assert "HEALTH_SCAN_MODEL=gpt-6-luna" in example

@pytest.mark.parametrize("arguments", [
    ["--manifest", "synthetic.json", "--mode", "plan", "--model", "gpt-6-sol"],
    ["--repo", "example/synthetic", "--mode", "file-ideas", "--model", "gpt-6-sol"],
    ["--repo", "example/synthetic", "--mode", "refine", "--model", "gpt-6-sol"],
])
def test_sol_cannot_enter_fleet_or_write_modes(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["autorefine", *arguments])
    process = Mock()
    monkeypatch.setattr(main, "_process_repo", process)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    process.assert_not_called()


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-4o-mini", "gpt-4.1"])
def test_unsupported_model_cannot_become_the_implicit_default(
    monkeypatch: pytest.MonkeyPatch, model: str,
) -> None:
    monkeypatch.setenv("FOUNDRY_DEFAULT_DEPLOYMENT", model)
    monkeypatch.setattr("sys.argv", ["autorefine", "--repo", "example/synthetic", "--mode", "plan"])
    process = Mock()
    monkeypatch.setattr(main, "_process_repo", process)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    process.assert_not_called()


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1"])
def test_manual_override_cannot_select_a_retiring_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str,
) -> None:
    monkeypatch.setattr("sys.argv", [
        "autorefine", "--repo", "example/synthetic", "--mode", "plan",
        "--model", model, "--workdir", str(tmp_path),
    ])
    process = Mock()
    monkeypatch.setattr(main, "_process_repo", process)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    process.assert_not_called()


def test_health_chat_is_independent_of_a_rejected_planning_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setattr("sys.argv", [
        "autorefine", "--repo", "example/synthetic", "--mode", "health-scan", "--dry-run",
    ])
    health = Mock()
    monkeypatch.setattr(main, "run_health_scan_mode", health)
    main.main()
    health.assert_called_once_with(["example/synthetic"], assign_copilot=True, dry_run=True)


def test_cost_estimates_do_not_assume_all_input_was_cached() -> None:
    run = SimpleNamespace(model="gpt-6-sol-2026-09-22", usage={
        "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
    })
    estimate = agent._run_cost_estimate(run)
    assert estimate["estimated_usd_uncached"] == pytest.approx(0.004)
    assert estimate["cost_basis"] == "uncached_input_upper_bound"
    assert agent.MODEL_PRICES["gpt-6-luna"] == (0.10, 0.50, 0.01)
    assert agent.MODEL_PRICES["gpt-6-sol"] == (2.00, 10.00, 0.20)


def test_unknown_model_has_no_silent_price_default() -> None:
    estimate = agent._run_cost_estimate(SimpleNamespace(
        model="unknown", usage={"prompt_tokens": 1000, "completion_tokens": 200},
    ))
    assert estimate["estimated_usd_uncached"] is None
    assert estimate["cost_basis"] == "unavailable"


def test_reasoning_effort_none_is_set_on_every_luna_agent_version() -> None:
    # Reasoning tokens bill as output; routine planning must not buy them.
    for mode in ("plan", "refine"):
        definition = agent.build_agent_definition(mode, "gpt-6-luna")
        assert definition.reasoning.effort == "none"