"""Only completed Foundry runs may return plans or buy publication."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
from azure.core.exceptions import AzureError

from agent import foundry_agent
from agent import main as agent_main
from agent.config import ProjectConfig


class _Function:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        self.id = "call-1"
        self.function = _Function(name, json.dumps(arguments))


class _Action:
    def __init__(self, tool_call: _ToolCall) -> None:
        self.submit_tool_outputs = SimpleNamespace(tool_calls=[tool_call])


class _ToolOutput:
    def __init__(self, tool_call_id: str, output: str) -> None:
        self.tool_call_id = tool_call_id
        self.output = output


@pytest.fixture()
def tool_dummies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(foundry_agent, "RequiredFunctionToolCall", _ToolCall)
    monkeypatch.setattr(foundry_agent, "SubmitToolOutputsAction", _Action)
    monkeypatch.setattr(foundry_agent, "ToolOutput", _ToolOutput)


def _client(error: object, *, status: str = "failed") -> SimpleNamespace:
    def create(
        *, thread_id: str, agent_id: str, max_prompt_tokens: int | None = None,
        truncation_strategy: object = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(id="run-1", status=status, last_error=error)

    return SimpleNamespace(
        threads=SimpleNamespace(create=Mock(return_value=SimpleNamespace(id="thread-1")), delete=Mock()),
        messages=SimpleNamespace(create=Mock(), list=Mock(return_value=[])),
        runs=SimpleNamespace(create=create_autospec(create, side_effect=create)),
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


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_terminal_run_discards_captured_plan(
    status: str,
    tool_dummies: None,
) -> None:
    plan = {"score": 81, "summary": "partial", "improvements": []}
    client = _client(None)
    client.runs.create.side_effect = None
    client.runs.create.return_value = SimpleNamespace(
        id="run-1",
        status="requires_action",
        required_action=_Action(_ToolCall("submit_plan", plan)),
    )
    client.runs.submit_tool_outputs = Mock(
        return_value=SimpleNamespace(id="run-1", status=status)
    )
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value.reason == status
    assert "Raise AUTOREFINE" not in str(error.value)
    client.messages.list.assert_not_called()
    client.threads.delete.assert_called_once_with("thread-1")


def test_cancelling_is_polled_until_terminal() -> None:
    client = _client(None, status="cancelling")
    client.runs.get = Mock(return_value=SimpleNamespace(id="run-1", status="cancelled"))
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value.reason == "cancelled"
    assert "Raise AUTOREFINE" not in str(error.value)
    client.runs.get.assert_called_once_with(thread_id="thread-1", run_id="run-1")
    client.messages.list.assert_not_called()


def test_unknown_terminal_status_fails_closed() -> None:
    client = _client(None, status="future_terminal_status")
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value.reason == "future_terminal_status"
    assert "Raise AUTOREFINE" not in str(error.value)
    client.messages.list.assert_not_called()


@pytest.mark.parametrize(
    "failure_point",
    ["message", "run", "tool", "poll", "final_messages"],
)
def test_thread_is_cleaned_when_run_lifecycle_raises(
    failure_point: str,
    tool_dummies: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError(f"{failure_point} exploded")
    client = _client(None, status="completed")
    if failure_point == "message":
        client.messages.create.side_effect = primary
    elif failure_point == "run":
        client.runs.create.side_effect = primary
    elif failure_point == "tool":
        def explode(_project_dir: Path, _args: dict[str, object]) -> str:
            raise primary

        client.runs.create.side_effect = None
        client.runs.create.return_value = SimpleNamespace(
            id="run-1",
            status="requires_action",
            required_action=_Action(_ToolCall("explode", {})),
        )
        monkeypatch.setitem(foundry_agent.TOOL_HANDLERS, "explode", explode)
    elif failure_point == "poll":
        client.runs.create.side_effect = None
        client.runs.create.return_value = SimpleNamespace(id="run-1", status="queued")
        client.runs.get = Mock(side_effect=primary)
    else:
        client.messages.list.side_effect = primary
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(ValueError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value is primary
    client.threads.delete.assert_called_once_with("thread-1")


def test_expected_cleanup_error_does_not_mask_incomplete_state() -> None:
    client = _client(None, status="incomplete")
    client.runs.create.side_effect = None
    client.runs.create.return_value = SimpleNamespace(
        id="run-1",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_prompt_tokens"),
    )
    client.threads.delete.side_effect = AzureError("cleanup offline")
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as error:
        foundry_agent.run_agent(client, "agent", Path("."), config, "task")

    assert error.value.reason == "max_prompt_tokens"


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


def test_cancelled_run_is_not_replayed_by_functional_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(None, status="cancelled")
    client.delete_agent = Mock()
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(foundry_agent, "create_agent", lambda *a, **kw: "agent")
    monkeypatch.setattr(agent_main, "_extract_relevant_wiki_insights", lambda name: "")
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        agent_main.plan_functional(Path("."), config)

    client.runs.create.assert_called_once()
    client.delete_agent.assert_called_once_with("agent")
