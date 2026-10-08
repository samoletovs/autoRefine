"""Only completed Foundry responses may return plans or buy publication."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest
from azure.core.exceptions import AzureError

from agent import foundry_agent
from agent import main as agent_main
from agent.config import ProjectConfig
from tests.plan_fixtures import valid_plan
from tests.test_foundry_loop_guards import AGENT, _DummyToolCall, _response, _ToolLoopClient


def _client(error: object, *, status: str = "failed") -> _ToolLoopClient:
    client = _ToolLoopClient(lambda n: None)

    def respond() -> SimpleNamespace:
        response = _response("resp-1", status)
        response.error = error
        return response

    client.next_response = respond
    return client


def _config() -> ProjectConfig:
    return ProjectConfig(name="demo", purpose="", users="", stage="active")


def _status_error(status: int, body: object = None) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/openai/v1/responses")
    response = httpx.Response(status, request=request)
    cls = {
        400: openai.BadRequestError, 429: openai.RateLimitError, 500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return cls(f"HTTP {status}", response=response, body=body)


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

    with pytest.raises(foundry_agent.FoundryRunFailedError):
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task", mode="file-ideas")

    assert client.deleted == ["resp-1"]


@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded"])
def test_transient_plan_failure_can_retry_after_cleanup(code: str) -> None:
    client = _client(SimpleNamespace(code=code))

    assert foundry_agent.run_agent(
        client, AGENT, Path("."), _config(), "task", mode="file-ideas"
    ) is None
    assert client.deleted == ["resp-1"]


def test_refine_failure_always_reaches_the_partial_edit_rollback_handler() -> None:
    client = _client(SimpleNamespace(code="server_error"))

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task", mode="refine")


@pytest.mark.parametrize("status_code", [500, 429])
def test_http_failure_maps_onto_the_same_transient_contract(
    status_code: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous response surfaces service failure as HTTP, not as a run status.

    Transient errors still exhaust the bounded application retries first, then
    behave like the classic ``server_error``/``rate_limit_exceeded`` run: a
    retryable ``None`` for planning, a hard failure for refine.
    """
    monkeypatch.setattr(foundry_agent._call_foundry_with_retry.retry, "sleep", lambda _: None)
    client = _ToolLoopClient(lambda n: None)
    create = Mock(side_effect=_status_error(status_code))
    client.responses.create = lambda *, truncation=None, max_output_tokens=None, \
        previous_response_id=None, **kw: create(**kw)

    assert foundry_agent.run_agent(
        client, AGENT, Path("."), _config(), "task", mode="file-ideas",
    ) is None
    attempts = create.call_count
    assert attempts == (foundry_agent.MAX_FOUNDRY_RETRY_ATTEMPTS if status_code == 429 else 1)

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task", mode="refine")
    expected = "server_error" if status_code == 500 else "rate_limit_exceeded"
    assert error.value.reason == expected


def test_permanent_http_failure_names_the_service_code() -> None:
    client = _ToolLoopClient(lambda n: None)
    failure = _status_error(400, {"error": {"code": "content_filter", "message": "x"}})

    def create(*, truncation=None, max_output_tokens=None, previous_response_id=None, **kw):
        raise failure

    client.responses.create = create
    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task", mode="file-ideas")
    assert error.value.reason == "content_filter"
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("cleanup_error", [
    AzureError("cleanup offline"),
    OSError("cleanup offline"),
    openai.APIConnectionError(request=httpx.Request("DELETE", "https://example.test")),
])
def test_expected_cleanup_error_does_not_replace_the_permanent_failure(
    cleanup_error: Exception,
) -> None:
    client = _client(SimpleNamespace(code="invalid_prompt"))
    client.responses.delete = Mock(side_effect=cleanup_error)

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task")

    assert error.value.reason == "invalid_prompt"


@pytest.mark.parametrize("code", ["invalid_prompt", "server_error"])
def test_cleanup_programming_error_propagates(code: str) -> None:
    client = _client(SimpleNamespace(code=code))
    programming_error = RuntimeError("unexpected cleanup bug")
    client.responses.delete = Mock(side_effect=programming_error)

    with pytest.raises(RuntimeError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task", mode="file-ideas")

    assert error.value is programming_error


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_terminal_response_discards_captured_plan(status: str) -> None:
    client = _ToolLoopClient(lambda n: None)
    created = iter([
        _response("resp-1", "completed", [
            _DummyToolCall("c1", "submit_plan", json.dumps(valid_plan(81))),
        ]),
        _response("resp-2", status),
    ])
    client.next_response = lambda: next(created)

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task")

    assert error.value.reason == status
    assert error.value.run_id == "resp-2"
    assert "Raise AUTOREFINE" not in str(error.value)
    assert client.deleted == ["resp-1", "resp-2"]


def test_unknown_terminal_status_fails_closed() -> None:
    client = _client(None, status="future_terminal_status")

    with pytest.raises(foundry_agent.FoundryRunFailedError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task")

    assert error.value.reason == "future_terminal_status"
    assert "Raise AUTOREFINE" not in str(error.value)


@pytest.mark.parametrize("failure_point", ["request", "tool", "poll"])
def test_responses_are_cleaned_when_the_lifecycle_raises(
    failure_point: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError(f"{failure_point} exploded")
    client = _ToolLoopClient(lambda n: None)
    if failure_point == "request":
        def create(*, truncation=None, max_output_tokens=None, previous_response_id=None, **kw):
            raise primary

        client.responses.create = create
    elif failure_point == "tool":
        def explode(_project_dir: Path, _args: dict[str, object]) -> str:
            raise primary

        client.next_response = lambda: _response(
            "resp-1", "completed", [_DummyToolCall("c1", "explode", "{}")],
        )
        monkeypatch.setitem(foundry_agent.TOOL_HANDLERS, "explode", explode)
    else:
        client.next_response = lambda: _response("resp-1", "queued")
        client.responses.retrieve = Mock(side_effect=primary)
        monkeypatch.setattr(foundry_agent.time, "sleep", lambda _: None)

    with pytest.raises(ValueError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task")

    assert error.value is primary
    assert client.deleted == ([] if failure_point == "request" else ["resp-1"])


@pytest.mark.parametrize("details", [
    SimpleNamespace(reason="max_output_tokens"), {"reason": "max_output_tokens"},
])
def test_expected_cleanup_error_does_not_mask_incomplete_state(details: object) -> None:
    client = _client(None, status="incomplete")
    original = client.next_response

    def incomplete() -> SimpleNamespace:
        response = original()
        response.incomplete_details = details
        return response

    client.next_response = incomplete
    client.responses.delete = Mock(side_effect=AzureError("cleanup offline"))

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as error:
        foundry_agent.run_agent(client, AGENT, Path("."), _config(), "task")

    assert error.value.reason == "max_output_tokens"
    assert not isinstance(error.value, foundry_agent.FoundryRunFailedError)


def _wire_functional(monkeypatch: pytest.MonkeyPatch, client: _ToolLoopClient) -> SimpleNamespace:
    project = SimpleNamespace(agents=Mock())
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr(foundry_agent, "open_foundry_clients", lambda endpoint: (project, client))
    monkeypatch.setattr(foundry_agent, "create_agent", lambda *a, **kw: AGENT)
    monkeypatch.setattr(agent_main, "_extract_relevant_wiki_insights", lambda name: "")
    return project


@pytest.mark.parametrize(("error", "status"), [
    (SimpleNamespace(code="invalid_prompt"), "failed"),
    (None, "cancelled"),
])
def test_terminal_failure_is_not_replayed_by_functional_planning(
    error: object, status: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(error, status=status)
    project = _wire_functional(monkeypatch, client)

    with pytest.raises(foundry_agent.FoundryRunIncompleteError):
        agent_main.plan_functional(Path("."), _config())

    assert len(client.requests) == 1
    assert project.agents.mock_calls == []
