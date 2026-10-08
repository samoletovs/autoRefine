"""Selective model costs and agent-version bounds, without service calls."""

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


@pytest.mark.parametrize(("model", "prompt_cap", "completion_cap"), [
    ("gpt-6-luna", 200_000, 16_000),
    ("gpt-6-sol", 40_000, 4_000),
    ("gpt-4.1", 200_000, 16_000),
    ("gpt-4o-mini", 200_000, 16_000),
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


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o-mini"])
def test_verified_agent_definition_preserves_legacy_parameters(model: str) -> None:
    agents = FakeAgents()
    version = agent.create_agent(SimpleNamespace(agents=agents), mode="plan", model=model)
    definition = agents.created[0]["definition"]
    assert version.model == definition.model == model
    assert definition.reasoning is None
    assert "max_tokens" not in definition.as_dict()
    assert definition.temperature == 0.3


def test_verified_model_and_caps_reach_every_request(tmp_path: Path) -> None:
    client = _ToolLoopClient(lambda n: None)
    agent.run_agent(
        client, agent_for("gpt-4.1"), tmp_path,
        ProjectConfig(name="synthetic", purpose="", users="", stage="active"),
        "Synthetic task", mode="plan", model="gpt-4.1",
    )
    (request,) = client.requests
    assert request["extra_body"]["agent_reference"]["name"] == "autorefine-plan"
    assert request["max_output_tokens"] == 16_000
    assert request["truncation"] == "auto"


def test_unconfigured_job_keeps_the_existing_model() -> None:
    assert AutoRefineConfig(repos=[]).model == "gpt-4o-mini"
    assert agent.DEFAULT_DEPLOYMENT == "gpt-4o-mini"
    agents = FakeAgents()
    agent.create_agent(SimpleNamespace(agents=agents))
    assert agents.created[0]["definition"].model == "gpt-4o-mini"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
@pytest.mark.parametrize("mode", ["plan", "refine"])
def test_unverified_models_are_blocked_before_any_agent_version_call(
    model: str, mode: str,
) -> None:
    project = Mock()
    with pytest.raises(ValueError, match="compatibility gate"):
        agent.create_agent(project, mode=mode, model=model)
    assert project.mock_calls == []


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
def test_unverified_models_cannot_run_an_existing_agent(
    model: str, tmp_path: Path,
) -> None:
    client = Mock()
    with pytest.raises(ValueError, match="compatibility gate"):
        agent.run_agent(
            client, agent_for(model), tmp_path,
            ProjectConfig(name="synthetic", purpose="", users="", stage="active"),
            "Synthetic task", mode="plan", model=model,
        )
    assert client.mock_calls == []

def test_iac_and_env_defaults_cannot_promote_an_unconfigured_job() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "infrastructure" / "main.bicep").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "param foundryDeployment string = 'gpt-4o-mini'" in template
    assert "@allowed(['gpt-4.1', 'gpt-4o-mini'])" in template
    assert "FOUNDRY_DEFAULT_DEPLOYMENT=gpt-4o-mini" in example
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


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
def test_unverified_classic_model_cannot_become_the_implicit_default(
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


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
def test_manual_permission_is_not_a_classic_compatibility_gate(
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


def test_health_chat_is_independent_of_a_rejected_classic_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-6-luna")
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


def test_reasoning_effort_is_not_set_on_any_agent_version() -> None:
    # Gated models stay blocked; admitted ones must not be told about effort they ignore.
    for mode in ("plan", "refine"):
        for model in ("gpt-4o-mini", "gpt-4.1"):
            assert agent.build_agent_definition(mode, model).reasoning is None
