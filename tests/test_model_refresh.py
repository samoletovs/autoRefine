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
from agent.config import ProjectConfig
from tests.test_foundry_agent import _budget_run_client


@pytest.mark.parametrize(("model", "prompt_cap", "completion_cap"), [
    ("gpt-6-luna", 200_000, 16_000),
    ("gpt-6-sol", 40_000, 4_000),
    ("gpt-4.1", 200_000, 16_000),
    ("gpt-4o-mini", 200_000, 16_000),
])
def test_real_sdk_declares_both_run_wide_caps(model: str, prompt_cap: int, completion_cap: int) -> None:
    options = agent._prompt_budget_kwargs(RunsOperations.create, model)
    assert options["max_prompt_tokens"] == prompt_cap
    assert options["max_completion_tokens"] == completion_cap
    assert options["truncation_strategy"].last_messages == 12


def test_kwargs_only_output_control_cannot_buy_an_unbounded_run() -> None:
    def unsupported(*, max_prompt_tokens: int, truncation_strategy: object, **kwargs: object) -> None:
        pass

    with pytest.raises(agent.FoundryPromptBudgetUnsupportedError, match="max_completion_tokens"):
        agent._prompt_budget_kwargs(unsupported)


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-4.1", "gpt-4o-mini"])
def test_creation_omits_unproven_reasoning_knobs(model: str) -> None:
    create = Mock(return_value=SimpleNamespace(id="synthetic-agent"))
    assert agent.create_agent(SimpleNamespace(create_agent=create), mode="plan", model=model) == "synthetic-agent"
    options = create.call_args.kwargs
    assert options["model"] == model
    assert "reasoning_effort" not in options
    assert "max_tokens" not in options
    if model.startswith("gpt-6-"):
        assert "temperature" not in options
    else:
        assert options["temperature"] == 0.3


def test_sol_model_and_caps_reach_run_creation_not_just_agent_defaults(tmp_path: Path) -> None:
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
        "Synthetic task", mode="plan", model="gpt-6-sol",
    )
    assert seen["model"] == "gpt-6-sol"
    assert recorded["max_prompt_tokens"] == 40_000
    assert recorded["max_completion_tokens"] == 4_000


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


def test_sol_cannot_become_the_implicit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-6-sol")
    monkeypatch.setattr("sys.argv", ["autorefine", "--repo", "example/synthetic", "--mode", "plan"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


def test_explicit_single_repo_sol_plan_uses_existing_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr("sys.argv", [
        "autorefine", "--repo", "example/synthetic", "--mode", "plan",
        "--model", "gpt-6-sol", "--workdir", str(tmp_path),
    ])
    process = Mock()
    monkeypatch.setattr(main, "_process_repo", process)
    main.main()
    assert process.call_args.args[1].model == "gpt-6-sol"
    assert not process.call_args.args[1].gate_on_activity


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
