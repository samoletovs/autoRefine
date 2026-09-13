"""Elapsed deadlines exercise real status polling, retry waits and teardown."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from azure.core.exceptions import AzureError, ServiceRequestError, ServiceResponseError
from azure.core.paging import ItemPaged

from agent import foundry_agent as f
from agent import main as m
from tests.plan_fixtures import valid_plan
from tests.test_foundry_loop_guards import (
    _DummyAction,
    _DummyToolCall,
    _ToolLoopClient,
    _config,
    loop_dummies,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("loop_dummies")


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
    client = _ToolLoopClient(lambda n: None)
    run = SimpleNamespace(id="run-1", status=status)
    client.next_run = lambda: run
    client.runs.get = Mock(return_value=run)
    return client


@pytest.mark.parametrize("status", ["queued", "in_progress", "cancelling"])
def test_endless_status_cancels_cleans_up_and_records_deadline_evidence(
    status: str, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _poll_client(status)
    log_path = tmp_path / "cost.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))
    caplog.set_level(logging.INFO)

    with pytest.raises(f.FoundryRunIncompleteError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task", mode="file-ideas")

    assert error.value.reason == "run_deadline"
    assert clock.now == 3
    assert client.runs.get.call_count == 2
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]
    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["guard"] == "run_deadline"
    assert row["status"] == status
    assert row["duration_s"] == 3
    assert row["plan_captured"] is False
    assert "guard=run_deadline" in caplog.text
    for call in client.runs.get.call_args_list:
        assert 0 < call.kwargs["connection_timeout"] <= 1
        assert 0 < call.kwargs["read_timeout"] <= 1
        assert call.kwargs["retry_total"] == 0


def test_transient_retry_backoff_never_outlives_the_remaining_budget(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("in_progress")
    client.runs.get.side_effect = httpx.ConnectError("synthetic unavailable service")
    monkeypatch.setattr(f, "_foundry_retry_wait", lambda state: 60)

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert clock.waits == [1, 2]
    assert client.runs.get.call_count == 1
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]


def test_normal_queued_progress_tool_completed_flow_is_preserved(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("queued")
    client.runs.get.side_effect = [
        SimpleNamespace(id="run-1", status="in_progress"),
        SimpleNamespace(id="run-1", status="requires_action", required_action=_DummyAction([
            _DummyToolCall("submit", "submit_plan", json.dumps(valid_plan())),
        ])),
    ]
    client.runs.submit_tool_outputs = Mock(
        return_value=SimpleNamespace(id="run-1", status="completed"),
    )

    result = f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert result["improvements"] == valid_plan()["improvements"]
    assert clock.now == 2
    assert client.cancelled == []
    assert client.deleted_threads == ["thread-1"]
    assert client.runs.submit_tool_outputs.call_args.kwargs["read_timeout"] == 0.5


def test_expiry_after_plan_capture_still_discards_the_result(
    clock: Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    client = _ToolLoopClient(lambda n: [
        _DummyToolCall("submit", "submit_plan", json.dumps(valid_plan())),
    ])
    client.runs.submit_tool_outputs = Mock(
        return_value=SimpleNamespace(id="run-1", status="in_progress"),
    )
    client.runs.get = Mock(return_value=SimpleNamespace(id="run-1", status="in_progress"))
    caplog.set_level(logging.INFO)

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert "plan_captured=False" in caplog.text
    assert client.cancelled == ["run-1"]


def test_tool_receives_remaining_budget_and_cannot_return_an_overdue_result(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ToolLoopClient(lambda n: [
        _DummyToolCall("tests", "run_project_tests", "{}"),
    ])
    outputs = Mock()
    client.runs.submit_tool_outputs = outputs
    budgets: list[float] = []

    def test_handler(project_dir: Path, args: dict, *, timeout_seconds: float) -> str:
        budgets.append(timeout_seconds)
        clock.sleep(timeout_seconds)
        return json.dumps({"passed": False, "error": "timeout"})

    monkeypatch.setitem(f.TOOL_HANDLERS, "run_project_tests", test_handler)
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert budgets == [3]
    outputs.assert_not_called()
    assert client.cancelled == ["run-1"]


@pytest.mark.parametrize("failure", ["cancel", "delete"])
def test_cleanup_failure_preserves_deadline_with_bounded_cleanup_requests(
    failure: str, clock: Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    client = _poll_client("queued")
    cancel = Mock(side_effect=AzureError("offline") if failure == "cancel" else None)
    delete = Mock(side_effect=AzureError("offline") if failure == "delete" else None)
    client.runs.cancel = cancel
    client.threads.delete = delete

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert "Could not" in caplog.text
    for call in (cancel.call_args, delete.call_args):
        assert call.kwargs["read_timeout"] == f.CLEANUP_TIMEOUT_SECONDS / 2
        assert call.kwargs["connection_timeout"] == f.CLEANUP_TIMEOUT_SECONDS / 2
        assert call.kwargs["retry_total"] == 0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1.5", "bogus"])
def test_deadline_is_validated_before_starting_paid_work(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    client.threads.create = Mock()
    monkeypatch.setenv("AUTOREFINE_RUN_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="AUTOREFINE_RUN_TIMEOUT_SECONDS"):
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    client.threads.create.assert_not_called()


def test_late_run_creation_retains_id_for_cancellation(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("completed")

    def late_run() -> SimpleNamespace:
        clock.sleep(4)
        return SimpleNamespace(id="run-1", status="completed")

    client.next_run = late_run
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]


def test_deadline_is_not_replayed_by_functional_planning(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    client.threads.create = Mock(side_effect=client.threads.create)
    client.delete_agent = Mock()
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(f, "create_agent", lambda *a, **kw: "agent")
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")

    with pytest.raises(f.FoundryRunAbortedError) as error:
        m.plan_functional(tmp_path, _config())

    assert error.value.reason == "run_deadline"
    assert client.threads.create.call_count == 1
    client.delete_agent.assert_called_once_with("agent")


def test_transport_failure_after_expiry_still_has_deadline_outcome(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("queued")

    def expired_poll(**kwargs: object) -> None:
        clock.sleep(2)
        raise AzureError("synthetic transport timeout")

    client.runs.get = expired_poll
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]


@pytest.mark.parametrize("exception_type", [ServiceRequestError, ServiceResponseError])
def test_sdk_transient_transport_errors_recover_with_budget_remaining(
    exception_type: type[Exception], clock: Clock, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("queued")
    client.runs.get.side_effect = [
        exception_type("temporary transport failure"),
        SimpleNamespace(id="run-1", status="completed"),
    ]
    monkeypatch.setattr(f, "_foundry_retry_wait", lambda state: 0.5)

    assert f.run_agent(client, "agent", tmp_path, _config(), "task") is None
    assert client.runs.get.call_count == 2
    assert clock.now == 1.5
    assert client.runs.get.call_args.kwargs["read_timeout"] == 0.75
    assert client.cancelled == []


def test_lazy_message_fetch_retries_in_budget_and_never_fetches_another_page(
    clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _poll_client("completed")
    attempts: list[float] = []
    message = SimpleNamespace(role=f.MessageRole.AGENT, text_messages=[])
    monkeypatch.setattr(f, "_foundry_retry_wait", lambda state: 0.5)

    def messages(**kwargs: object) -> ItemPaged:
        assert kwargs["run_id"] == "run-1"

        def fetch(token: str | None) -> list:
            assert token is None, "fetching the next page escapes the remaining budget"
            attempts.append(kwargs["read_timeout"])
            if len(attempts) == 1:
                raise ServiceResponseError("synthetic temporary read timeout")
            clock.sleep(2)
            return [message]

        return ItemPaged(fetch, lambda data: ("older-page", iter(data)))

    client.messages.list = messages

    assert f.run_agent(client, "agent", tmp_path, _config(), "task") is None
    assert attempts == [1.5, 1.25]
    assert clock.now == 2.5
    assert client.cancelled == []


def test_late_lazy_message_page_cannot_return_a_plan(
    clock: Clock, tmp_path: Path,
) -> None:
    client = _poll_client("completed")

    def messages(**kwargs: object) -> ItemPaged:
        def fetch(token: str | None) -> list:
            clock.sleep(4)
            return []

        return ItemPaged(fetch, lambda data: (None, iter(data)))

    client.messages.list = messages
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "run_deadline"
    assert client.deleted_threads == ["thread-1"]
