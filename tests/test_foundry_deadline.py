"""Elapsed deadlines exercise real status polling, retry waits and teardown."""

from __future__ import annotations

import json
import logging
import threading
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest
from azure.core.exceptions import AzureError, ServiceRequestError, ServiceResponseError

from agent import foundry_agent as f
from agent import main as m
from tests.plan_fixtures import valid_plan
from tests.test_foundry_loop_guards import (
    AGENT,
    _config,
    _DummyToolCall,
    _response,
    _ToolLoopClient,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(f.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(f.time, "sleep", clock.sleep)
    monkeypatch.setattr(f._call_foundry_with_retry.retry, "sleep", clock.sleep)
    monkeypatch.setenv("AUTOREFINE_RUN_TIMEOUT_SECONDS", "3")
    return clock


def _poll_client(status: str) -> _ToolLoopClient:
    """A first response stuck in ``status`` for as long as anyone polls it."""
    client = _ToolLoopClient(lambda n: None)
    response = _response("resp-1", status)
    client.next_response = lambda: response
    client.responses.retrieve = Mock(return_value=response)
    return client


def _wait_for(predicate, seconds: float = 2.0) -> bool:  # type: ignore[no-untyped-def]
    """Late cleanup runs on a daemon thread; give it real time to land."""
    end = real_time.perf_counter() + seconds
    while real_time.perf_counter() < end:
        if predicate():
            return True
        threading.Event().wait(0.01)  # time.sleep is the fake clock here
    return predicate()


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_endless_status_stops_cleans_up_and_records_deadline_evidence(
    status: str, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _poll_client(status)
    log_path = tmp_path / "cost.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))
    caplog.set_level(logging.INFO)

    with pytest.raises(f.FoundryRunIncompleteError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task", mode="file-ideas")

    assert error.value.reason == "run_deadline"
    # A response still queued/in progress may be billing; that is not hidden.
    assert error.value.cancellation_unconfirmed is True
    assert "cancellation_unconfirmed" in caplog.text
    assert clock.now == 3
    assert client.responses.retrieve.call_count == 2
    assert client.deleted == ["resp-1"]
    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["guard"] == "run_deadline"
    assert row["status"] == status
    assert row["duration_s"] == 3
    assert row["plan_captured"] is False
    assert "guard=run_deadline" in caplog.text
    for call in client.responses.retrieve.call_args_list:
        assert 0 < call.kwargs["timeout"].read <= 1
        assert 0 < call.kwargs["timeout"].connect <= 1


def test_transient_retry_backoff_never_outlives_the_remaining_budget(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("in_progress")
    client.responses.retrieve.side_effect = httpx.ConnectError("synthetic unavailable service")
    monkeypatch.setattr(f, "_foundry_retry_wait", lambda state: 60)

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert clock.waits == [1, 2]
    assert client.responses.retrieve.call_count == 1
    assert client.deleted == ["resp-1"]


def test_normal_queued_progress_tool_completed_flow_is_preserved(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("queued")
    created = iter([
        _response("resp-1", "queued"),
        _response("resp-2", "completed"),
    ])
    client.next_response = lambda: next(created)
    client.responses.retrieve.side_effect = [
        _response("resp-1", "in_progress"),
        _response("resp-1", "completed", [
            _DummyToolCall("submit", "submit_plan", json.dumps(valid_plan())),
        ]),
    ]

    result = f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert result["improvements"] == valid_plan()["improvements"]
    assert clock.now == 2
    assert client.requests[1]["previous_response_id"] == "resp-1"
    assert client.requests[1]["timeout"].read == 0.5
    assert client.deleted == ["resp-1", "resp-2"]


def test_expiry_after_plan_capture_still_discards_the_result(
    clock: Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    client = _ToolLoopClient(lambda n: None)
    created = iter([
        _response("resp-1", "completed", [
            _DummyToolCall("submit", "submit_plan", json.dumps(valid_plan())),
        ]),
        _response("resp-2", "in_progress"),
    ])
    client.next_response = lambda: next(created)
    client.responses.retrieve = Mock(return_value=_response("resp-2", "in_progress"))
    caplog.set_level(logging.INFO)

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert "plan_captured=False" in caplog.text
    assert client.deleted == ["resp-1", "resp-2"]


def test_tool_receives_remaining_budget_and_cannot_return_an_overdue_result(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ToolLoopClient(lambda n: [
        _DummyToolCall("tests", "run_project_tests", "{}"),
    ])
    budgets: list[float] = []

    def test_handler(project_dir: Path, args: dict, *, timeout_seconds: float) -> str:
        budgets.append(timeout_seconds)
        clock.sleep(timeout_seconds)
        return json.dumps({"passed": False, "error": "timeout"})

    monkeypatch.setitem(f.TOOL_HANDLERS, "run_project_tests", test_handler)
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert budgets == [3]
    assert len(client.requests) == 1, "an overdue tool output must never be sent"
    assert error.value.cancellation_unconfirmed is False


def test_cleanup_failure_preserves_deadline_with_bounded_cleanup_requests(
    clock: Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    client = _poll_client("queued")
    delete = Mock(side_effect=AzureError("offline"))
    client.responses.delete = delete

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert "Could not delete response" in caplog.text
    timeout = delete.call_args.kwargs["timeout"]
    assert timeout.read == f.CLEANUP_TIMEOUT_SECONDS / 2
    assert timeout.connect == f.CLEANUP_TIMEOUT_SECONDS / 2


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1.5", "bogus"])
def test_deadline_is_validated_before_starting_paid_work(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    monkeypatch.setenv("AUTOREFINE_RUN_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="AUTOREFINE_RUN_TIMEOUT_SECONDS"):
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert client.requests == []


def test_late_response_creation_is_only_used_for_cleanup(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("completed")

    def late_response() -> object:
        clock.sleep(4)
        return _response("resp-late", "completed", [
            _DummyToolCall("w", "write_project_file", '{"path":"late.py","content":"x"}'),
        ])

    client.next_response = late_response
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task", mode="refine")

    assert error.value.reason == "run_deadline"
    assert error.value.cancellation_unconfirmed is True
    assert _wait_for(lambda: client.deleted == ["resp-late"])
    assert not (tmp_path / "late.py").exists()


def test_deadline_is_not_replayed_by_functional_planning(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    project = SimpleNamespace(agents=Mock())
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr(f, "open_foundry_clients", lambda endpoint: (project, client))
    monkeypatch.setattr(f, "create_agent", lambda *a, **kw: AGENT)
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")

    with pytest.raises(f.FoundryRunAbortedError) as error:
        m.plan_functional(tmp_path, _config())

    assert error.value.reason == "run_deadline"
    assert len(client.requests) == 1
    assert project.agents.mock_calls == [], "the persistent agent is never deleted"


@pytest.mark.parametrize("failure", [
    AzureError("synthetic transport timeout"),
    openai.APIConnectionError(request=httpx.Request("GET", "https://example.test")),
])
def test_transport_failure_after_expiry_still_has_deadline_outcome(
    failure: Exception, clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("queued")

    def expired_poll(*args: object, **kwargs: object) -> None:
        clock.sleep(2)
        raise failure

    client.responses.retrieve = expired_poll
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, AGENT, tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert client.deleted == ["resp-1"]


@pytest.mark.parametrize("failure", [
    ServiceRequestError("temporary transport failure"),
    ServiceResponseError("temporary transport failure"),
    openai.APIConnectionError(request=httpx.Request("GET", "https://example.test")),
    openai.APITimeoutError(request=httpx.Request("GET", "https://example.test")),
])
def test_transient_transport_errors_recover_with_budget_remaining(
    failure: Exception, clock: Clock, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    client.responses.retrieve.side_effect = [failure, _response("resp-1", "completed")]
    monkeypatch.setattr(f, "_foundry_retry_wait", lambda state: 0.5)

    assert f.run_agent(client, AGENT, tmp_path, _config(), "task") is None
    assert client.responses.retrieve.call_count == 2
    assert clock.now == 1.5
    assert client.responses.retrieve.call_args.kwargs["timeout"].read == 0.75
