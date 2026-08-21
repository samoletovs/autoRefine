"""Tests for foundry_agent module — units covering tool handlers, plan parsing, retry logic."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from azure.core.exceptions import HttpResponseError

from agent import foundry_agent
from agent.config import ProjectConfig
from agent.foundry_agent import (
    _call_foundry_with_retry,
    create_agent,
)


class FakeFunctionTool:
    def __init__(self, functions: set) -> None:
        self.functions = functions
        self.definitions = [{"name": fn.__name__} for fn in functions]


class FakeClient:
    def __init__(self) -> None:
        self.kwargs = {}

    def create_agent(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(id="agent-1")


def test_create_agent_works_without_search_web_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(foundry_agent, "FunctionTool", FakeFunctionTool)
    client = FakeClient()

    agent_id = foundry_agent.create_agent(client, mode="plan")

    assert agent_id == "agent-1"
    tool_names = {tool["name"] for tool in client.kwargs["tools"]}
    assert "search_web" not in tool_names
    assert tool_names == {
        "read_project_file",
        "list_directory",
        "run_project_tests",
        "submit_plan",
    }


def test_tool_handlers_do_not_include_search_web() -> None:
    assert "search_web" not in foundry_agent.TOOL_HANDLERS


def test_handle_write_project_file_writes_inside_project(tmp_path: Path) -> None:
    result = foundry_agent._handle_write_project_file(
        tmp_path, {"path": "src/utils.py", "content": "print('ok')"}
    )
    parsed = json.loads(result)

    assert parsed["status"] == "written"
    assert parsed["path"] == "src/utils.py"
    assert (tmp_path / "src" / "utils.py").read_text(encoding="utf-8") == "print('ok')"


def test_handle_write_project_file_blocks_path_traversal(tmp_path: Path) -> None:
    result = foundry_agent._handle_write_project_file(
        tmp_path, {"path": "../outside.txt", "content": "nope"}
    )
    assert json.loads(result)["error"] == "Path traversal blocked"


@pytest.mark.parametrize(
    "blocked_path",
    [".git/config", ".env", "node_modules/pkg/index.js", ".github/workflows/ci.yml"],
)
def test_handle_write_project_file_blocks_protected_paths(
    tmp_path: Path, blocked_path: str
) -> None:
    result = foundry_agent._handle_write_project_file(
        tmp_path, {"path": blocked_path, "content": "blocked"}
    )
    assert "protected path" in json.loads(result)["error"]


def test_handle_apply_improvement_acknowledges_payload(tmp_path: Path) -> None:
    result = foundry_agent._handle_apply_improvement(
        tmp_path,
        {"title": "Add tests", "files_changed": ["tests/test_a.py"], "description": "desc"},
    )
    parsed = json.loads(result)
    assert parsed["status"] == "improvement_applied"
    assert parsed["title"] == "Add tests"
    assert parsed["files_changed"] == ["tests/test_a.py"]


def test_handle_run_tests_python_project_passes(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    fake_run = SimpleNamespace(returncode=0, stdout="33 passed\n", stderr="")

    with patch("subprocess.run", return_value=fake_run) as mock_run:
        result = json.loads(foundry_agent._handle_run_tests(tmp_path, {}))

    assert result["passed"] is True
    assert "33 passed" in result["output"]
    assert mock_run.call_args.args[0] == ["python", "-m", "pytest", "tests/", "-x", "-q"]


def test_handle_run_tests_python_project_failure(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    fake_run = SimpleNamespace(returncode=1, stdout="F", stderr="AssertionError")

    with patch("subprocess.run", return_value=fake_run):
        result = json.loads(foundry_agent._handle_run_tests(tmp_path, {}))

    assert result["passed"] is False
    assert "AssertionError" in result["output"]


def test_handle_run_tests_node_project_uses_npm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    fake_run = SimpleNamespace(returncode=0, stdout="ok", stderr="")

    with patch("subprocess.run", return_value=fake_run) as mock_run:
        _ = foundry_agent._handle_run_tests(tmp_path, {})

    assert mock_run.call_args.args[0] == ["npm", "test", "--", "--reporter=verbose"]


def test_handle_run_tests_no_runner_detected(tmp_path: Path) -> None:
    result = json.loads(foundry_agent._handle_run_tests(tmp_path, {}))
    assert result["error"] == "No test runner detected"


def test_create_agent_uses_passed_model() -> None:
    fake_created = SimpleNamespace(id="agent-123")
    fake_client = SimpleNamespace()

    with patch("agent.foundry_agent.FunctionTool") as mock_tool, patch.object(
        fake_client, "create_agent", return_value=fake_created, create=True
    ) as mock_create_agent:
        mock_tool.return_value.definitions = ["tool-a"]
        agent_id = foundry_agent.create_agent(fake_client, mode="plan", model="gpt-4.1")

    assert agent_id == "agent-123"
    assert mock_create_agent.call_args.kwargs["model"] == "gpt-4.1"


class FakeSweepClient:
    """Client double exposing just the listing/deleting surface the sweep uses."""

    def __init__(self, agents: list[SimpleNamespace], undeletable: set[str] | None = None) -> None:
        self._agents = agents
        self._undeletable = undeletable or set()
        self.deleted: list[str] = []

    def list_agents(self) -> list[SimpleNamespace]:
        return self._agents

    def delete_agent(self, agent_id: str) -> None:
        if agent_id in self._undeletable:
            raise HttpResponseError("boom")
        self.deleted.append(agent_id)

    def create_agent(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id="agent-new")


def _agent(name: str, agent_id: str, age: timedelta) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, id=agent_id, created_at=datetime.now(timezone.utc) - age
    )


def test_sweep_deletes_only_stale_autorefine_agents() -> None:
    client = FakeSweepClient(
        [
            _agent("autorefine", "stale-1", timedelta(days=5)),
            _agent("autorefine", "stale-2", timedelta(hours=7)),
            _agent("autorefine", "live", timedelta(minutes=20)),
            _agent("atlas-teacher", "other-project", timedelta(days=90)),
        ]
    )

    swept = foundry_agent.sweep_orphaned_agents(client)

    assert swept == 2
    assert client.deleted == ["stale-1", "stale-2"]


def test_sweep_ignores_agents_without_creation_time() -> None:
    client = FakeSweepClient([SimpleNamespace(name="autorefine", id="x", created_at=None)])

    assert foundry_agent.sweep_orphaned_agents(client) == 0
    assert client.deleted == []


def test_sweep_returns_zero_when_listing_fails() -> None:
    client = SimpleNamespace()
    with patch.object(
        client, "list_agents", side_effect=HttpResponseError("nope"), create=True
    ):
        assert foundry_agent.sweep_orphaned_agents(client) == 0


def test_sweep_continues_after_a_failed_delete() -> None:
    client = FakeSweepClient(
        [
            _agent("autorefine", "undeletable", timedelta(days=2)),
            _agent("autorefine", "deletable", timedelta(days=2)),
        ],
        undeletable={"undeletable"},
    )

    assert foundry_agent.sweep_orphaned_agents(client) == 1
    assert client.deleted == ["deletable"]


def test_create_agent_sweeps_orphans_before_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(foundry_agent, "FunctionTool", FakeFunctionTool)
    client = FakeSweepClient([_agent("autorefine", "stale", timedelta(days=3))])

    assert foundry_agent.create_agent(client, mode="plan") == "agent-new"
    assert client.deleted == ["stale"]


def test_create_agent_still_works_when_client_cannot_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(foundry_agent, "FunctionTool", FakeFunctionTool)

    assert foundry_agent.create_agent(FakeClient(), mode="plan") == "agent-1"


def test_tool_definition_stubs_return_empty_string() -> None:
    assert foundry_agent.read_project_file("README.md") == ""
    assert foundry_agent.list_directory(".") == ""
    assert foundry_agent.run_project_tests() == ""
    assert foundry_agent.submit_plan(80, "summary", []) == ""
    assert foundry_agent.write_project_file("x.txt", "body") == ""
    assert foundry_agent.apply_improvement("t", "d", []) == ""


def test_handle_read_project_file_success_and_truncation(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    parsed = json.loads(foundry_agent._handle_read_project_file(tmp_path, {"path": "README.md", "max_lines": 2}))
    assert parsed["path"] == "README.md"
    assert parsed["content"] == "a\nb"
    assert parsed["truncated"] is True


def test_handle_read_project_file_not_found(tmp_path: Path) -> None:
    parsed = json.loads(foundry_agent._handle_read_project_file(tmp_path, {"path": "missing.md"}))
    assert "File not found" in parsed["error"]


def test_handle_read_project_file_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    parsed = json.loads(foundry_agent._handle_read_project_file(tmp_path, {"path": "docs"}))
    assert "Not a file" in parsed["error"]


def test_handle_read_project_file_blocks_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    parsed = json.loads(foundry_agent._handle_read_project_file(tmp_path, {"path": f"../{outside.name}"}))
    assert parsed["error"] == "Path traversal blocked"


def test_handle_list_directory_success_skips_known_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    parsed = json.loads(foundry_agent._handle_list_directory(tmp_path, {"path": "."}))
    names = {entry["name"] for entry in parsed["entries"]}
    assert "src" in names
    assert "README.md" in names
    assert ".git" not in names
    assert "node_modules" not in names


def test_handle_list_directory_not_found(tmp_path: Path) -> None:
    parsed = json.loads(foundry_agent._handle_list_directory(tmp_path, {"path": "missing"}))
    assert "Directory not found" in parsed["error"]


def test_handle_list_directory_blocks_traversal(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside-dir"
    outside_dir.mkdir(exist_ok=True)
    parsed = json.loads(foundry_agent._handle_list_directory(tmp_path, {"path": f"../{outside_dir.name}"}))
    assert parsed["error"] == "Path traversal blocked"


def test_handle_submit_plan_returns_ack(tmp_path: Path) -> None:
    parsed = json.loads(
        foundry_agent._handle_submit_plan(tmp_path, {"improvements": [{"title": "one"}, {"title": "two"}]})
    )
    assert parsed["status"] == "plan_received"
    assert parsed["improvements_count"] == 2


def test_parse_plan_from_text_parses_score_and_improvements() -> None:
    text = (
        "Findings\n"
        "Score: 88/100\n"
        "1. [P1] **Fix tests** — Improve unit test coverage\n"
        "2. **Improve docs**: Add architecture section\n"
    )
    parsed = foundry_agent._parse_plan_from_text(text)
    assert parsed is not None
    assert parsed["score"] == 88
    assert len(parsed["improvements"]) == 2
    assert parsed["improvements"][0]["priority"] == "P1"


def test_parse_plan_from_text_middle_dot_separator() -> None:
    text = "Score: 65/100\n\n1. **Refactor config** · split module.\n"
    parsed = foundry_agent._parse_plan_from_text(text)
    assert parsed is not None
    assert len(parsed["improvements"]) == 1
    assert parsed["improvements"][0]["title"] == "Refactor config"


def test_parse_plan_from_text_returns_none_without_improvements() -> None:
    assert foundry_agent._parse_plan_from_text("Score: 50/100\nNo numbered list") is None


# --- stringified improvements recovery (gpt-4o-mini serializes a numbered string) ---

# Verbatim shape observed live: submit_plan's improvements passed as prose, not JSON.
_STRINGIFIED_IMPROVEMENTS = (
    "1. Multi-Country Dashboard \u2014 Implement a dashboard that displays key financial "
    "metrics for multiple countries. \u2014 priority: P1, effort: M, category: feature\n"
    "2. Invoice Recognition Enhancement \u2014 Integrate advanced OCR to extract invoice "
    "data automatically. \u2014 priority: P1, effort: M, category: feature\n"
    "3. User Onboarding Flow \u2014 Develop a guided onboarding flow for new companies. "
    "\u2014 priority: P2, effort: L, category: onboarding"
)


def test_parse_improvements_list_recovers_structured_items() -> None:
    items = foundry_agent._parse_improvements_list(_STRINGIFIED_IMPROVEMENTS)
    assert len(items) == 3
    assert items[0]["title"] == "Multi-Country Dashboard"
    assert items[0]["description"].startswith("Implement a dashboard")
    assert items[0]["priority"] == "P1"
    assert items[0]["effort"] == "M"
    assert items[0]["category"] == "feature"
    assert items[2]["priority"] == "P2"
    assert items[2]["effort"] == "L"
    assert items[2]["category"] == "onboarding"


def test_normalize_plan_args_recovers_stringified_improvements() -> None:
    plan = foundry_agent._normalize_plan_args(
        {"score": "85", "summary": "s", "improvements": _STRINGIFIED_IMPROVEMENTS}
    )
    assert plan["score"] == 85
    assert len(plan["improvements"]) == 3
    assert all(isinstance(imp, dict) for imp in plan["improvements"])
    assert plan["improvements"][0]["title"] == "Multi-Country Dashboard"


def test_normalize_plan_args_prefers_valid_json_list() -> None:
    plan = foundry_agent._normalize_plan_args(
        {"improvements": '[{"title": "Real JSON", "priority": "P1"}]'}
    )
    assert len(plan["improvements"]) == 1
    assert plan["improvements"][0]["title"] == "Real JSON"


def test_build_plan_task_includes_findings_and_similar() -> None:
    config = ProjectConfig(
        name="demo",
        purpose="p",
        users="u",
        stage="active",
        goals=[],
        similar=["A", "B"],
        quality=[],
    )
    task = foundry_agent.build_plan_task([{"priority": "P0", "category": "tests", "description": "missing"}], config)
    assert "Quality check findings" in task
    assert "Similar products context" in task


class TestCreateAgentSignature:
    def test_accepts_model_kwarg(self) -> None:
        """Guards against regressions where the --model kwarg gets dropped from create_agent."""
        sig = inspect.signature(create_agent)
        assert "model" in sig.parameters
        assert sig.parameters["model"].default is None


def test_build_refine_task_lists_improvements() -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    task = foundry_agent.build_refine_task(
        {"improvements": [{"priority": "P1", "title": "Add tests", "description": "write more tests"}]},
        config,
    )
    assert "Improvements to apply" in task
    assert "[P1] Add tests" in task


def test_run_agent_handles_failed_status() -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    thread_api = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-1"),
        delete=lambda _thread_id: None,
    )
    messages_api = SimpleNamespace(create=lambda **_kwargs: None, list=lambda **_kwargs: [])
    def create_run(
        *,
        thread_id: str,
        agent_id: str,
        max_prompt_tokens: int | None = None,
        truncation_strategy: object = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(id="run-1", status="failed", last_error="boom")

    runs_api = SimpleNamespace(create=create_run)
    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=runs_api)

    result = foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")
    assert result is None


def test_run_agent_processes_tool_calls_and_returns_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    class DummyFunction:
        def __init__(self, name: str, arguments: str) -> None:
            self.name = name
            self.arguments = arguments

    class DummyToolCall:
        def __init__(self, tool_call_id: str, name: str, arguments: str) -> None:
            self.id = tool_call_id
            self.function = DummyFunction(name, arguments)

    class DummySubmitToolOutputsAction:
        def __init__(self, tool_calls: list[DummyToolCall]) -> None:
            self.submit_tool_outputs = SimpleNamespace(tool_calls=tool_calls)

    class DummyToolOutput:
        def __init__(self, tool_call_id: str, output: str) -> None:
            self.tool_call_id = tool_call_id
            self.output = output

    monkeypatch.setattr(foundry_agent, "RequiredFunctionToolCall", DummyToolCall)
    monkeypatch.setattr(foundry_agent, "SubmitToolOutputsAction", DummySubmitToolOutputsAction)
    monkeypatch.setattr(foundry_agent, "ToolOutput", DummyToolOutput)

    thread_api = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-1"),
        delete=lambda _thread_id: None,
    )

    agent_message = SimpleNamespace(
        role=foundry_agent.MessageRole.AGENT,
        text_messages=[SimpleNamespace(text=SimpleNamespace(value="Score: 72/100\n1. **Fix tests** — add tests"))],
    )
    messages_api = SimpleNamespace(
        create=lambda **_kwargs: None,
        list=lambda **_kwargs: [agent_message],
    )

    requires_action_run = SimpleNamespace(
        id="run-1",
        status="requires_action",
        required_action=DummySubmitToolOutputsAction(
            [DummyToolCall("call-1", "submit_plan", '{"score": 72, "summary": "ok", "improvements": []}')]
        ),
    )
    completed_run = SimpleNamespace(id="run-1", status="completed")

    class RunsApi:
        def create(
            self,
            *,
            thread_id: str,
            agent_id: str,
            max_prompt_tokens: int | None = None,
            truncation_strategy: object = None,
        ) -> SimpleNamespace:
            return requires_action_run

        def submit_tool_outputs(self, **_kwargs: object) -> SimpleNamespace:
            return completed_run

        def get(self, **_kwargs: object) -> SimpleNamespace:
            return completed_run

    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=RunsApi())

    result = foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")
    assert result == {"score": 72, "summary": "ok", "improvements": [], "research_insights": []}


# ── PR #23 additions: retry behaviour ────────────────────────────────────────


def _http_response_error(status_code: int) -> HttpResponseError:
    response = Mock()
    response.status_code = status_code
    response.reason = "mock-reason"
    response.headers = {}
    response.text = "mock-body"
    response.request = Mock()
    response.request.method = "POST"
    response.request.url = "https://example.test/foundry"
    return HttpResponseError(message=f"HTTP {status_code}", response=response)


def test_foundry_retry_retries_429_then_succeeds(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def flaky_operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _http_response_error(429)
        return "ok"

    monkeypatch.setattr(_call_foundry_with_retry.retry, "sleep", lambda _seconds: None)
    with caplog.at_level(logging.WARNING):
        result = _call_foundry_with_retry("client.runs.create_and_process", flaky_operation)

    assert result == "ok"
    assert calls == 3
    retry_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and rec.msg == "Retrying %s after attempt %d/%d due to %s: %s"
        and rec.args[0] == "client.runs.create_and_process"
    ]
    assert len(retry_warnings) == 2


def test_foundry_retry_does_not_retry_http_400() -> None:
    calls = 0

    def bad_request_operation() -> None:
        nonlocal calls
        calls += 1
        raise _http_response_error(400)

    with pytest.raises(HttpResponseError):
        _call_foundry_with_retry("client.runs.create_and_process", bad_request_operation)

    assert calls == 1


# ── Prompt-token budget (cost cap) ───────────────────────────────────────────


def test_max_prompt_tokens_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOREFINE_MAX_PROMPT_TOKENS", raising=False)
    monkeypatch.delenv("AUTOREFINE_TRUNCATION_LAST_MESSAGES", raising=False)
    assert foundry_agent.resolve_max_prompt_tokens() == foundry_agent.DEFAULT_MAX_PROMPT_TOKENS
    assert (
        foundry_agent.resolve_truncation_last_messages()
        == foundry_agent.DEFAULT_TRUNCATION_LAST_MESSAGES
    )


def test_max_prompt_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_MAX_PROMPT_TOKENS", " 90000 ")
    monkeypatch.setenv("AUTOREFINE_TRUNCATION_LAST_MESSAGES", "5")
    assert foundry_agent.resolve_max_prompt_tokens() == 90000
    assert foundry_agent.resolve_truncation_last_messages() == 5


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "100", "19999"])
def test_max_prompt_tokens_rejects_invalid(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_MAX_PROMPT_TOKENS", value)
    with pytest.raises(ValueError):
        foundry_agent.resolve_max_prompt_tokens()


def test_truncation_last_messages_rejects_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_TRUNCATION_LAST_MESSAGES", "1")
    with pytest.raises(ValueError):
        foundry_agent.resolve_truncation_last_messages()


def test_installed_sdk_supports_prompt_budget_kwargs() -> None:
    """The pinned azure-ai-agents SDK must accept the cap; guards silent no-ops."""
    from azure.ai.agents.operations import RunsOperations

    params = inspect.signature(RunsOperations.create).parameters
    assert "max_prompt_tokens" in params
    assert "truncation_strategy" in params


def test_prompt_budget_fails_closed_when_sdk_lacks_params() -> None:
    """A future SDK dropping the params must error, never run unbounded."""

    def legacy_create(thread_id: str, agent_id: str) -> None: ...

    with pytest.raises(foundry_agent.FoundryPromptBudgetUnsupportedError):
        foundry_agent._prompt_budget_kwargs(legacy_create)


def test_prompt_budget_rejects_kwargs_only_signature() -> None:
    """**kwargs is not evidence of support: generated clients drop unknown kwargs."""

    def kwargs_only_create(thread_id: str, **kwargs: object) -> None: ...

    with pytest.raises(foundry_agent.FoundryPromptBudgetUnsupportedError) as excinfo:
        foundry_agent._prompt_budget_kwargs(kwargs_only_create)
    assert "max_prompt_tokens" in str(excinfo.value)
    assert "truncation_strategy" in str(excinfo.value)


def test_default_max_prompt_tokens_is_a_runaway_guard_not_a_per_call_cap() -> None:
    """max_prompt_tokens is run-wide cumulative; ~11.5k avg input/call means a
    dozen-round plan legitimately spends >100k. The default must not throttle that."""
    assert foundry_agent.DEFAULT_MAX_PROMPT_TOKENS >= 100_000


def _budget_run_client(run_status: str, incomplete_reason: str | None = None):
    recorded: dict = {}
    incomplete_details = (
        SimpleNamespace(reason=incomplete_reason) if incomplete_reason is not None else None
    )
    run = SimpleNamespace(
        id="run-1",
        status=run_status,
        last_error=None,
        incomplete_details=incomplete_details,
    )

    class RunsApi:
        def create(
            self,
            *,
            thread_id: str,
            agent_id: str,
            max_prompt_tokens: int | None = None,
            truncation_strategy: object = None,
        ) -> SimpleNamespace:
            recorded.update(
                thread_id=thread_id,
                agent_id=agent_id,
                max_prompt_tokens=max_prompt_tokens,
                truncation_strategy=truncation_strategy,
            )
            return run

    deleted: list[str] = []
    thread_api = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-1"),
        delete=lambda thread_id: deleted.append(thread_id),
    )
    messages_api = SimpleNamespace(create=lambda **_kwargs: None, list=lambda **_kwargs: [])
    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=RunsApi())
    return client, recorded, deleted


def test_run_agent_passes_bounded_prompt_to_runs_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTOREFINE_MAX_PROMPT_TOKENS", raising=False)
    monkeypatch.setenv("AUTOREFINE_TRUNCATION_LAST_MESSAGES", "4")
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client, recorded, _deleted = _budget_run_client("completed")

    foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")

    assert recorded["thread_id"] == "thread-1"
    assert recorded["agent_id"] == "agent-1"
    assert recorded["max_prompt_tokens"] == foundry_agent.DEFAULT_MAX_PROMPT_TOKENS
    assert recorded["truncation_strategy"].as_dict() == {
        "type": "last_messages",
        "last_messages": 4,
    }
    assert set(recorded) == {
        "thread_id",
        "agent_id",
        "max_prompt_tokens",
        "truncation_strategy",
    }


def test_run_agent_raises_on_incomplete_max_prompt_tokens() -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client, _recorded, deleted = _budget_run_client("incomplete", "max_prompt_tokens")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as excinfo:
        foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")

    assert excinfo.value.reason == "max_prompt_tokens"
    assert excinfo.value.run_id == "run-1"
    assert deleted == ["thread-1"]


def test_run_agent_incomplete_without_details_still_raises() -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client, _recorded, _deleted = _budget_run_client("incomplete")

    with pytest.raises(foundry_agent.FoundryRunIncompleteError) as excinfo:
        foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")

    assert excinfo.value.reason is None
