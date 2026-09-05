"""Only transient service failures may buy another whole Foundry run."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import AzureError

from agent import foundry_agent
from agent import main as agent_main
from agent.config import ProjectConfig


def _client(error: object) -> SimpleNamespace:
    def create(
        *, thread_id: str, agent_id: str, max_prompt_tokens: int | None = None,
        truncation_strategy: object = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(id="run-1", status="failed", last_error=error)

    return SimpleNamespace(
        threads=SimpleNamespace(create=Mock(return_value=SimpleNamespace(id="thread-1")), delete=Mock()),
        messages=SimpleNamespace(create=Mock()),
        runs=SimpleNamespace(create=create),
    )


@pytest.mark.parametrize(
    "error",
    [
        {"code": "invalid_prompt"},
        SimpleNamespace(code="content_filter"),
        SimpleNamespace(code="authentication_error"),
        None,
    ],
)
def test_permanent_or_unknown_failure_is_not_a_retryable_none(error: object) -> None:
    client = _client(error)
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        foundry_agent.run_agent(client, "agent", Path("."), config, "task", mode="file-ideas")

    client.threads.delete.assert_called_once_with("thread-1")


@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded"])
def test_transient_plan_failure_can_retry_after_thread_cleanup(code: str) -> None:
    client = _client(SimpleNamespace(code=code))
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    assert foundry_agent.run_agent(
        client, "agent", Path("."), config, "task", mode="file-ideas"
    ) is None
    client.threads.delete.assert_called_once_with("thread-1")


def test_refine_failure_always_reaches_the_partial_edit_rollback_handler() -> None:
    client = _client(SimpleNamespace(code="server_error"))
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        foundry_agent.run_agent(client, "agent", Path("."), config, "task", mode="refine")


@pytest.mark.parametrize("cleanup_error", [AzureError("cleanup offline"), OSError("cleanup offline")])
def test_expected_cleanup_error_does_not_replace_the_permanent_failure(
    cleanup_error: Exception,
) -> None:
    client = _client(SimpleNamespace(code="invalid_prompt"))
    client.threads.delete.side_effect = cleanup_error
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value.reason == "invalid_prompt"


@pytest.mark.parametrize("code", ["invalid_prompt", "server_error"])
def test_cleanup_programming_error_propagates(code: str) -> None:
    client = _client(SimpleNamespace(code=code))
    programming_error = RuntimeError("unexpected cleanup bug")
    client.threads.delete.side_effect = programming_error
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(RuntimeError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task", mode="file-ideas")

    assert error.value is programming_error


def test_terminal_failure_is_not_replayed_by_functional_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(SimpleNamespace(code="invalid_prompt"))
    client.delete_agent = Mock()
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(foundry_agent, "create_agent", lambda *a, **kw: "agent")
    monkeypatch.setattr(agent_main, "_extract_relevant_wiki_insights", lambda name: "")
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        agent_main.plan_functional(Path("."), config)

    client.threads.create.assert_called_once()
    client.delete_agent.assert_called_once_with("agent")
