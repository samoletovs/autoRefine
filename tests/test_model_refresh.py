"""Selective model costs and classic-agent bounds, without service calls."""

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.agents import AgentsClient
from azure.ai.agents.operations import RunsOperations

from agent import foundry_agent as agent
from agent import main
from agent.config import AutoRefineConfig, ProjectConfig
from tests.test_foundry_agent import _budget_run_client


@pytest.mark.parametrize(("model", "prompt_cap", "completion_cap"), [
    ("gpt-6-luna", 200_000, 16_000),
    ("gpt-6-sol", 40_000, 4_000),
    ("gpt-4.1", 200_000, 16_000),
    ("gpt-4o-mini", 200_000, 16_000),
])
def test_budget_parameters_do_not_imply_classic_model_admission(
    model: str, prompt_cap: int, completion_cap: int,
) -> None:
    options = agent._prompt_budget_kwargs(RunsOperations.create, model)
    assert options["max_prompt_tokens"] == prompt_cap
    assert options["max_completion_tokens"] == completion_cap
    assert options["truncation_strategy"].last_messages == 12


def test_kwargs_only_output_control_cannot_buy_an_unbounded_run() -> None:
    def unsupported(*, max_prompt_tokens: int, truncation_strategy: object, **kwargs: object) -> None:
        pass

    with pytest.raises(agent.FoundryPromptBudgetUnsupportedError, match="max_completion_tokens"):
        agent._prompt_budget_kwargs(unsupported)


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o-mini"])
def test_verified_classic_creation_preserves_legacy_parameters(model: str) -> None:
    create = Mock(return_value=SimpleNamespace(id="synthetic-agent"))
    assert agent.create_agent(SimpleNamespace(create_agent=create), mode="plan", model=model) == "synthetic-agent"
    options = create.call_args.kwargs
    assert options["model"] == model
    assert "reasoning_effort" not in options
    assert "max_tokens" not in options
    assert options["temperature"] == 0.3


def test_verified_model_and_caps_reach_run_creation_not_just_agent_defaults(tmp_path: Path) -> None:
    client, recorded, _ = _budget_run_client("completed")
    original = client.runs.create
    seen = {}

    def create(
        *, max_prompt_tokens: int, max_completion_tokens: int, truncation_strategy: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        seen.update(kwargs)
        return original(
            max_prompt_tokens=max_prompt_tokens, max_completion_tokens=max_completion_tokens,
            truncation_strategy=truncation_strategy, **kwargs,
        )

    client.runs.create = create
    agent.run_agent(
        client, "synthetic-agent", tmp_path,
        ProjectConfig(name="synthetic", purpose="", users="", stage="active"),
        "Synthetic task", mode="plan", model="gpt-4o-mini",
    )
    assert seen["model"] == "gpt-4o-mini"
    assert recorded["max_prompt_tokens"] == 200_000
    assert recorded["max_completion_tokens"] == 16_000


def test_unconfigured_job_keeps_the_existing_classic_model() -> None:
    assert AutoRefineConfig(repos=[]).model == "gpt-4o-mini"
    assert agent.DEFAULT_DEPLOYMENT == "gpt-4o-mini"
    create = Mock(return_value=SimpleNamespace(id="synthetic-agent"))
    agent.create_agent(SimpleNamespace(create_agent=create))
    assert create.call_args.kwargs["model"] == "gpt-4o-mini"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
@pytest.mark.parametrize("mode", ["plan", "refine"])
def test_unverified_classic_models_are_blocked_before_creation_or_housekeeping(
    model: str, mode: str,
) -> None:
    client = Mock()
    client.create_agent.return_value = SimpleNamespace(id="must-not-be-created")
    orphan_client = Mock()
    with pytest.raises(ValueError, match="compatibility gate"):
        agent.create_agent(client, mode=mode, model=model, orphan_client=orphan_client)
    assert client.mock_calls == []
    assert orphan_client.mock_calls == []


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
def test_unverified_classic_models_cannot_run_an_existing_agent(
    model: str, tmp_path: Path,
) -> None:
    client = Mock()
    with pytest.raises(ValueError, match="compatibility gate"):
        agent.run_agent(
            client, "existing-agent", tmp_path,
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


def test_classic_sdk_reasoning_support_is_not_assumed() -> None:
    # This is the blocker, not permission to tunnel unsupported JSON to Azure.
    assert "reasoning_effort" not in inspect.signature(AgentsClient.create_agent).parameters
    assert "reasoning_effort" not in inspect.signature(RunsOperations.create).parameters
