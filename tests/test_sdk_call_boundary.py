"""Real wall-clock regressions: transport inactivity limits are not deadlines."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.agents import AgentsClient
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceResponseError
from azure.core.pipeline.transport import HttpRequest, HttpTransport, RequestsTransport

from agent import foundry_agent as f
from agent import main as m
from agent.config import ProjectConfig
from agent.sdk_boundary import SdkBoundary, client_boundary
from tests.test_foundry_loop_guards import (
    _DummyAction,
    _DummyToolCall,
    _ToolLoopClient,
    loop_dummies,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("loop_dummies")


def test_continuously_arriving_body_bytes_cannot_extend_the_caller_deadline() -> None:
    stop = threading.Event()
    finished = threading.Event()

    class SlowBody(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    if stop.wait(0.04):
                        break
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowBody)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = RequestsTransport()

    def read_entire_body(**kwargs: object) -> bytes:
        try:
            kwargs.pop("retry_total")  # normally consumed by the SDK pipeline policy
            response = transport.send(
                HttpRequest("GET", f"http://127.0.0.1:{server.server_port}/synthetic"),
                **kwargs,
            )
            return response.body()
        finally:
            finished.set()

    started = time.monotonic()
    try:
        with pytest.raises(f._RunDeadlineExceeded):
            f._call_foundry_with_retry(
                "synthetic slow response", read_entire_body, deadline=started + 0.25,
            )
        assert time.monotonic() - started < 0.75
    finally:
        stop.set()
        assert finished.wait(2), "test must release the worker before closing its transport"
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("entrypoint", ["plan", "functional", "refine"])
@pytest.mark.parametrize("outcome", ["success", "deadline"])
def test_expected_agent_delete_failure_does_not_replace_primary_result_or_error(
    entrypoint: str, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = f.FoundryRunAbortedError("run", "run_deadline", "synthetic timeout")
    no_gap = {"outcome": "no_gap", "score": 100, "improvements": [], "summary": "Reviewed"}
    client = SimpleNamespace(delete_agent=Mock(side_effect=ServiceResponseError("cleanup offline")))
    run = Mock(side_effect=primary) if outcome == "deadline" else Mock(return_value=no_gap)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(f, "create_agent", lambda *a, **kw: "agent")
    monkeypatch.setattr(f, "run_agent", run)
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")
    monkeypatch.setattr(m, "_worktree_snapshot", lambda path: set())
    monkeypatch.setattr(m, "_worktree_status", lambda path: {})
    monkeypatch.setattr(m, "_rollback_agent_changes", lambda *a: [])
    monkeypatch.setattr("agent.tools.github_tools.create_branch", lambda *a: True)
    monkeypatch.setattr(f._call_foundry_with_retry.retry, "sleep", lambda _: None)
    config = ProjectConfig(name="fixture", purpose="", users="", stage="active")

    def invoke() -> object:
        if entrypoint == "plan":
            return m.plan_project(tmp_path, config, [])
        if entrypoint == "functional":
            return m.plan_functional(tmp_path, config)
        return m.refine_project(tmp_path, config, no_gap, "owner/fixture")

    if outcome == "deadline" and entrypoint != "refine":
        with pytest.raises(f.FoundryRunAbortedError) as error:
            invoke()
        assert error.value is primary
    else:
        assert invoke() == (False if entrypoint == "refine" else no_gap)
    client.delete_agent.assert_called()


def test_late_run_never_dispatches_tools_and_cleanup_waits_for_its_client_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, cancelled, deleted = threading.Event(), threading.Event(), threading.Event()
    client = _ToolLoopClient(lambda n: None)
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    dispatched = Mock()
    monkeypatch.setitem(f.TOOL_HANDLERS, "write_project_file", dispatched)

    def late_run() -> SimpleNamespace:
        assert release.wait(4)
        return SimpleNamespace(id="late-run", status="requires_action", required_action=_DummyAction([
            _DummyToolCall("write", "write_project_file", '{"path":"late.py","content":"bad"}'),
        ]))

    def cancel(**kwargs: object) -> None:
        assert release.is_set(), "cleanup cannot use a client still owned by an in-flight call"
        assert kwargs["run_id"] == "late-run"
        cancelled.set()

    def delete(thread_id: str, **kwargs: object) -> None:
        assert release.is_set()
        deleted.set()

    client.next_run = late_run
    client.runs.cancel = cancel
    client.threads.delete = delete
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.run_agent(client, "agent", tmp_path, ProjectConfig(
                name="fixture", purpose="", users="", stage="active",
            ), "task", mode="refine")
        assert error.value.reason == "run_deadline"
        assert time.monotonic() - started < 1.7
        assert not cancelled.is_set()
        assert not deleted.is_set()
        # Even a caller ignoring the failure cannot reuse this client for paid work.
        with pytest.raises(f._RunDeadlineExceeded, match="retired"):
            f._call_foundry_with_retry(
                "should not execute", dispatched, deadline=time.monotonic() + 1,
                boundary=client_boundary(client),
            )
    finally:
        release.set()
        assert cancelled.wait(2)
        assert deleted.wait(2)
    dispatched.assert_not_called()
    assert not (tmp_path / "late.py").exists()


def test_agent_delete_itself_has_an_absolute_caller_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.1)

    def delete(agent_id: str, **kwargs: object) -> None:
        try:
            assert release.wait(2)
        finally:
            finished.set()

    client = SimpleNamespace(delete_agent=delete)
    started = time.monotonic()
    try:
        assert f.cleanup_agent(client, "agent") is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        assert finished.wait(2)


def test_agent_cleanup_programming_error_still_propagates() -> None:
    primary = RuntimeError("synthetic programming bug")
    client = SimpleNamespace(delete_agent=Mock(side_effect=primary))
    with pytest.raises(RuntimeError) as error:
        f.cleanup_agent(client, "agent")
    assert error.value is primary


def test_agent_created_after_deadline_is_only_used_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, deleted = threading.Event(), threading.Event()
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)

    def create(**kwargs: object) -> SimpleNamespace:
        assert release.wait(4)
        return SimpleNamespace(id="late-agent")

    def delete(agent_id: str, **kwargs: object) -> None:
        assert agent_id == "late-agent"
        deleted.set()

    client = SimpleNamespace(create_agent=create, delete_agent=delete)
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.create_agent(client)
        assert error.value.reason == "run_deadline"
    finally:
        release.set()
        assert deleted.wait(2)


def test_real_sdk_blocked_auth_cannot_hold_the_caller_or_reuse_closed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, deleted = threading.Event(), threading.Event()
    requests: list[str] = []
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)

    class Credential:
        def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            assert release.wait(4)
            return AccessToken("synthetic-only", 9_999_999_999)

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        reason = "OK"
        content_type = "application/json"

        def json(self) -> dict:
            return {"id": "thread-1", "object": "thread", "created_at": 0, "metadata": {}}

        @property
        def content(self) -> bytes:
            return json.dumps(self.json()).encode()

        def text(self, encoding: str | None = None) -> str:
            return json.dumps(self.json())

        def body(self) -> bytes:
            return self.content

        def read(self) -> bytes:
            return self.content

    class Transport(HttpTransport):
        closed = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def __enter__(self) -> Transport:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

        def send(self, request: HttpRequest, **kwargs: object) -> Response:
            assert not self.closed, "a late SDK call must not access a closed transport"
            requests.append(request.method)
            response = Response()
            response.request = request
            if request.method == "DELETE":
                response.status_code = 204
                deleted.set()
            return response

    transport = Transport()
    client = AgentsClient(
        endpoint="https://example.test/api/projects/synthetic",
        credential=Credential(), transport=transport,
    )
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.run_agent(client, "agent", tmp_path, ProjectConfig(
                name="fixture", purpose="", users="", stage="active",
            ), "task")
        assert error.value.reason == "run_deadline"
        assert time.monotonic() - started < 1.7
        assert requests == []
    finally:
        release.set()
        assert deleted.wait(2), "late-created thread must still get a cleanup attempt"
        client.close()
    assert requests == ["POST", "DELETE"]


def test_work_queued_before_retirement_is_never_dispatched_afterwards() -> None:
    boundary = SdkBoundary()
    started, release, first_done = threading.Event(), threading.Event(), threading.Event()
    queued_work = Mock()
    errors: list[BaseException] = []

    def stalled(deadline: float) -> None:
        started.set()
        assert release.wait(4)

    def first() -> None:
        try:
            boundary.call(stalled, deadline=time.monotonic() + 0.5)
        except f._RunDeadlineExceeded as exc:
            errors.append(exc)
        finally:
            first_done.set()

    def second() -> None:
        try:
            boundary.call(queued_work, deadline=time.monotonic() + 3)
        except f._RunDeadlineExceeded as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=first, daemon=True)
    second_thread = threading.Thread(target=second, daemon=True)
    first_thread.start()
    try:
        assert started.wait(1)
        second_thread.start()
        assert first_done.wait(2)
    finally:
        release.set()
        first_thread.join(2)
        second_thread.join(2)
    assert len(errors) == 2
    queued_work.assert_not_called()


def test_stalled_isolated_orphan_sweep_does_not_retire_the_work_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.1)

    def list_agents(**kwargs: object) -> list:
        try:
            assert release.wait(3)
            return []
        finally:
            finished.set()

    orphan_client = SimpleNamespace(list_agents=list_agents)
    work_client = SimpleNamespace(create_agent=Mock(return_value=SimpleNamespace(id="agent")))
    started = time.monotonic()
    try:
        assert f.create_agent(work_client, orphan_client=orphan_client) == "agent"
        assert time.monotonic() - started < 0.75
        work_client.create_agent.assert_called_once()
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("cancel_status", ["cancelled", "cancelling"])
def test_known_run_cancels_through_independent_channel_while_poll_still_owns_client(
    cancel_status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    release, poll_finished, deleted = threading.Event(), threading.Event(), threading.Event()
    run = SimpleNamespace(id="known-run", status="in_progress")
    client = _ToolLoopClient(lambda n: None)
    client.next_run = lambda: run
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(f.time, "sleep", lambda _: None)

    def poll(**kwargs: object) -> SimpleNamespace:
        try:
            assert release.wait(4)
            return SimpleNamespace(id="known-run", status="completed")
        finally:
            poll_finished.set()

    def cancel(**kwargs: object) -> SimpleNamespace:
        assert not release.is_set(), "cancellation must not wait for the abandoned poll"
        assert kwargs["run_id"] == "known-run"
        return SimpleNamespace(status=cancel_status)

    client.runs.get = poll
    client.threads.delete = lambda *a, **kw: deleted.set()
    cancellation = Mock(side_effect=cancel)
    client._autorefine_cancel_client = SimpleNamespace(runs=SimpleNamespace(cancel=cancellation))
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.run_agent(client, "agent", tmp_path, ProjectConfig(
                name="fixture", purpose="", users="", stage="active",
            ), "task")
        assert time.monotonic() - started < 1.7
        assert error.value.cancellation_unconfirmed is (cancel_status != "cancelled")
        cancellation.assert_called_once()
        assert not poll_finished.is_set()
        assert not deleted.is_set(), "owning-client teardown must remain serialized"
        if cancel_status == "cancelling":
            assert "cancellation_unconfirmed" in caplog.text
    finally:
        release.set()
        assert poll_finished.wait(2)
        assert deleted.wait(2)


def test_failed_cancellation_is_explicitly_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client = SimpleNamespace(runs=SimpleNamespace(cancel=Mock(
        side_effect=ServiceResponseError("synthetic cancellation failure"),
    )))
    monkeypatch.setattr(f._call_foundry_with_retry.retry, "sleep", lambda _: None)
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f._abort_run(
            client, "thread", SimpleNamespace(id="run"), "run_deadline", "synthetic deadline",
        )
    assert error.value.cancellation_unconfirmed is True
    assert "cancellation_unconfirmed" in caplog.text


@pytest.mark.parametrize("entrypoint", ["plan", "functional", "refine"])
def test_entrypoints_provision_only_isolated_sdk_channels_at_the_existing_endpoint(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[SimpleNamespace] = []
    no_gap = {"outcome": "no_gap", "score": 100, "improvements": [], "summary": "Reviewed"}

    def new_client(**kwargs: object) -> SimpleNamespace:
        assert kwargs["endpoint"] == "https://example.test"
        client = SimpleNamespace(delete_agent=Mock())
        clients.append(client)
        return client

    def create(client: SimpleNamespace, **kwargs: object) -> str:
        assert client._autorefine_cancel_client is not client
        assert kwargs["orphan_client"] is not client
        assert kwargs["orphan_client"] is not client._autorefine_cancel_client
        return "agent"

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", new_client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(f, "create_agent", create)
    monkeypatch.setattr(f, "run_agent", Mock(return_value=no_gap))
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")
    monkeypatch.setattr(m, "_worktree_snapshot", lambda path: set())
    monkeypatch.setattr(m, "_worktree_status", lambda path: {})
    monkeypatch.setattr("agent.tools.github_tools.create_branch", lambda *a: True)
    config = ProjectConfig(name="fixture", purpose="", users="", stage="active")

    if entrypoint == "plan":
        assert m.plan_project(tmp_path, config, []) == no_gap
    elif entrypoint == "functional":
        assert m.plan_functional(tmp_path, config) == no_gap
    else:
        assert m.refine_project(tmp_path, config, no_gap, "owner/fixture") is False
    assert len(clients) == 3
