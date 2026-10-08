"""Real wall-clock regressions: transport inactivity limits are not deadlines."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest
from azure.core.exceptions import ServiceResponseError
from azure.core.pipeline.transport import HttpRequest, RequestsTransport

from agent import foundry_agent as f
from agent import main as m
from agent.config import ProjectConfig
from agent.sdk_boundary import SdkBoundary, client_boundary
from tests.test_foundry_loop_guards import AGENT, _DummyToolCall, _response, _ToolLoopClient


def _config() -> ProjectConfig:
    return ProjectConfig(name="fixture", purpose="", users="", stage="active")


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


def _wire_entrypoint(
    monkeypatch: pytest.MonkeyPatch, run: Mock,
) -> tuple[SimpleNamespace, SimpleNamespace, Mock, list[str]]:
    project = SimpleNamespace(agents=Mock())
    client = SimpleNamespace(responses=Mock())
    opened: list[str] = []
    create = Mock(return_value=AGENT)

    def open_clients(endpoint: str) -> tuple[SimpleNamespace, SimpleNamespace]:
        opened.append(endpoint)
        return project, client

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr(f, "open_foundry_clients", open_clients)
    monkeypatch.setattr(f, "create_agent", create)
    monkeypatch.setattr(f, "run_agent", run)
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")
    monkeypatch.setattr(m, "_worktree_snapshot", lambda path: set())
    monkeypatch.setattr(m, "_worktree_status", lambda path: {})
    monkeypatch.setattr(m, "_rollback_agent_changes", lambda *a: [])
    monkeypatch.setattr("agent.tools.github_tools.create_branch", lambda *a: True)
    return project, client, create, opened


@pytest.mark.parametrize("entrypoint", ["plan", "functional", "refine"])
@pytest.mark.parametrize("outcome", ["success", "deadline"])
def test_entrypoints_keep_the_persistent_agent_and_the_primary_outcome(
    entrypoint: str, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One endpoint, one project client, one responses client; no per-run agent teardown."""
    primary = f.FoundryRunAbortedError("run", "run_deadline", "synthetic timeout")
    no_gap = {"outcome": "no_gap", "score": 100, "improvements": [], "summary": "Reviewed"}
    run = Mock(side_effect=primary) if outcome == "deadline" else Mock(return_value=no_gap)
    project, client, create, opened = _wire_entrypoint(monkeypatch, run)

    def invoke() -> object:
        if entrypoint == "plan":
            return m.plan_project(tmp_path, _config(), [])
        if entrypoint == "functional":
            return m.plan_functional(tmp_path, _config())
        return m.refine_project(tmp_path, _config(), no_gap, "owner/fixture")

    if outcome == "deadline" and entrypoint != "refine":
        with pytest.raises(f.FoundryRunAbortedError) as error:
            invoke()
        assert error.value is primary
    else:
        assert invoke() == (False if entrypoint == "refine" else no_gap)

    assert opened == ["https://example.test"]
    assert create.call_args.args[0] is project
    assert create.call_args.kwargs["mode"] == ("refine" if entrypoint == "refine" else "plan")
    assert run.call_args.args[:2] == (client, AGENT)
    assert project.agents.mock_calls == [], "a persistent agent version is never deleted"


def test_late_response_never_dispatches_tools_and_cleanup_waits_for_its_client_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, deleted = threading.Event(), threading.Event()
    client = _ToolLoopClient(lambda n: None)
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    dispatched = Mock()
    monkeypatch.setitem(f.TOOL_HANDLERS, "write_project_file", dispatched)

    def late_response() -> SimpleNamespace:
        assert release.wait(4)
        return _response("late-resp", "completed", [
            _DummyToolCall("write", "write_project_file", '{"path":"late.py","content":"bad"}'),
        ])

    def delete(response_id: str, **kwargs: object) -> None:
        assert release.is_set(), "cleanup cannot use a client still owned by an in-flight call"
        assert response_id == "late-resp"
        deleted.set()

    client.next_response = late_response
    client.responses.delete = delete
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.run_agent(client, AGENT, tmp_path, _config(), "task", mode="refine")
        assert error.value.reason == "run_deadline"
        assert error.value.cancellation_unconfirmed is True
        assert time.monotonic() - started < 1.7
        assert not deleted.is_set()
        # Even a caller ignoring the failure cannot reuse this client for paid work.
        with pytest.raises(f._RunDeadlineExceeded, match="retired"):
            f._call_foundry_with_retry(
                "should not execute", dispatched, deadline=time.monotonic() + 1,
                boundary=client_boundary(client),
            )
    finally:
        release.set()
        assert deleted.wait(2)
    dispatched.assert_not_called()
    assert not (tmp_path / "late.py").exists()


def test_response_cleanup_has_an_absolute_caller_boundary_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.1)
    attempted: list[str] = []

    def delete(response_id: str, **kwargs: object) -> None:
        attempted.append(response_id)
        try:
            assert release.wait(2)
        finally:
            finished.set()

    client = SimpleNamespace(responses=SimpleNamespace(delete=delete))
    started = time.monotonic()
    try:
        assert f._cleanup_responses(client, ["resp-1", "resp-2"]) is False
        assert time.monotonic() - started < 0.5
        assert attempted == ["resp-1"], "stop at the first failure; the service expires the rest"
    finally:
        release.set()
        assert finished.wait(2)


def test_response_cleanup_programming_error_still_propagates() -> None:
    primary = RuntimeError("synthetic programming bug")
    client = SimpleNamespace(responses=SimpleNamespace(delete=Mock(side_effect=primary)))
    with pytest.raises(RuntimeError) as error:
        f._cleanup_responses(client, ["resp-1"])
    assert error.value is primary


def test_agent_version_published_after_deadline_is_harmless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late version is a reusable definition, not an orphan needing cleanup."""
    release, published = threading.Event(), threading.Event()
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)

    def create_version(**kwargs: object) -> SimpleNamespace:
        try:
            assert release.wait(4)
            return SimpleNamespace(name=kwargs["agent_name"], version="1", metadata={})
        finally:
            published.set()

    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=lambda *a, **kw: [], create_version=create_version,
    ))
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.create_agent(project)
        assert error.value.reason == "run_deadline"
        assert time.monotonic() - started < 1.7
    finally:
        release.set()
        assert published.wait(2)


def test_real_sdk_blocked_auth_cannot_hold_the_caller_or_reuse_closed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real OpenAI client, authenticated the way ``get_openai_client`` does it."""
    release, deleted = threading.Event(), threading.Event()
    requests: list[str] = []
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)
    monkeypatch.setattr(f, "CLEANUP_TIMEOUT_SECONDS", 0.05)

    def token_provider() -> str:
        assert release.wait(4)
        return "synthetic-only"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "DELETE":
            deleted.set()
            return httpx.Response(200, json={"id": "resp_1", "object": "response", "deleted": True})
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 0, "status": "completed",
            "model": "gpt-4o-mini", "output": [], "parallel_tool_calls": True,
            "tool_choice": "auto", "tools": [],
        })

    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    client = openai.OpenAI(
        base_url="https://example.test/api/projects/synthetic/openai/v1",
        api_key=token_provider, max_retries=0, http_client=http_client,
    )
    # Resource modules load lazily on first access; keep that import out of the timing.
    assert callable(client.responses.create)
    started = time.monotonic()
    try:
        with pytest.raises(f.FoundryRunAbortedError) as error:
            f.run_agent(client, AGENT, tmp_path, _config(), "task")
        assert error.value.reason == "run_deadline"
        assert time.monotonic() - started < 1.7
        assert requests == []
    finally:
        release.set()
        assert deleted.wait(2), "late-created response must still get a cleanup attempt"
    assert requests == ["POST", "DELETE"]
    http_client.close()


def test_retired_agent_lease_does_not_retire_the_responses_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    monkeypatch.setattr(f, "resolve_run_timeout_seconds", lambda: 1)

    def stalled(*args: object, **kwargs: object) -> list:
        assert release.wait(4)
        return []

    project = SimpleNamespace(agents=SimpleNamespace(list_versions=stalled))
    client = _ToolLoopClient(lambda n: None)
    try:
        with pytest.raises(f.FoundryRunAbortedError):
            f.create_agent(project)
        assert client_boundary(project) is not client_boundary(client)
        assert f.run_agent(client, AGENT, tmp_path, _config(), "task") is None
    finally:
        release.set()


def test_expected_cleanup_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(delete=Mock(
        side_effect=ServiceResponseError("synthetic cleanup failure"),
    )))
    monkeypatch.setattr(f._call_foundry_with_retry.retry, "sleep", lambda _: None)
    assert f._cleanup_responses(client, ["resp-1"]) is False
    assert "Could not delete response resp-1" in caplog.text


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
