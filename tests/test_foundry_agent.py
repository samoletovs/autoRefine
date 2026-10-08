"""Tests for foundry_agent module — units covering tool handlers, plan parsing, retry logic."""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import openai
import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
)

from agent import foundry_agent
from agent.config import ProjectConfig
from agent.foundry_agent import (
    _call_foundry_with_retry,
    create_agent,
)
from tests.plan_fixtures import valid_plan
from tests.test_foundry_loop_guards import AGENT, _DummyToolCall, _ToolLoopClient


class FakeAgents:
    """``project.agents`` double: versions newest first, as ``order="desc"`` returns."""

    def __init__(self, versions: list[SimpleNamespace] | None = None, *, missing: bool = False) -> None:
        self.versions = list(versions or [])
        self.missing = missing
        self.created: list[dict] = []
        self.list_kwargs: list[dict] = []

    def list_versions(self, agent_name: str, **kwargs: object) -> list[SimpleNamespace]:
        self.list_kwargs.append({"agent_name": agent_name, **kwargs})
        if self.missing:
            raise ResourceNotFoundError("agent not found")
        return [v for v in self.versions if v.name == agent_name]

    def create_version(self, agent_name: str, **kwargs: object) -> SimpleNamespace:
        self.created.append({"agent_name": agent_name, **kwargs})
        same_name = [int(v.version) for v in self.versions if v.name == agent_name]
        version = SimpleNamespace(
            name=agent_name, version=str(max(same_name, default=0) + 1),
            metadata=kwargs["metadata"],
        )
        self.versions.insert(0, version)
        self.missing = False
        return version


def _project(agents: FakeAgents | None = None) -> SimpleNamespace:
    return SimpleNamespace(agents=agents or FakeAgents())


def _tool_names(definition: object) -> set[str]:
    return {tool["name"] for tool in definition.as_dict()["tools"]}


def test_create_agent_publishes_a_plan_version_without_search_web_tool() -> None:
    project = _project()

    agent = foundry_agent.create_agent(project, mode="plan")

    assert agent == foundry_agent.AgentVersion("autorefine-plan", "1", "gpt-4o-mini")
    (created,) = project.agents.created
    assert created["agent_name"] == "autorefine-plan"
    assert _tool_names(created["definition"]) == {
        "read_project_file",
        "list_directory",
        "run_project_tests",
        "submit_plan",
    }
    assert created["definition"].instructions == foundry_agent.SYSTEM_PROMPT
    assert created["metadata"] == {
        foundry_agent.AGENT_DEFINITION_METADATA_KEY:
            foundry_agent.definition_fingerprint(created["definition"]),
    }


def test_refine_agent_is_a_separate_name_with_the_write_tools() -> None:
    project = _project()

    agent = foundry_agent.create_agent(project, mode="refine")

    assert agent.name == "autorefine-refine"
    assert _tool_names(project.agents.created[0]["definition"]) == {
        "read_project_file", "list_directory", "run_project_tests", "submit_plan",
        "write_project_file", "apply_improvement",
    }


def test_unchanged_definition_reuses_the_existing_version() -> None:
    """Nothing is created per run, so a crashed run can leave nothing behind."""
    project = _project()
    first = foundry_agent.create_agent(project, mode="plan")
    second = foundry_agent.create_agent(project, mode="plan")

    assert first == second
    assert len(project.agents.created) == 1
    assert project.agents.list_kwargs[-1]["order"] == "desc"


def test_changed_definition_gets_a_new_version_and_older_matches_are_reused() -> None:
    """A --model override must not flip-flop the agent into a new version each run."""
    project = _project()
    mini = foundry_agent.create_agent(project, mode="plan")
    gpt41 = foundry_agent.create_agent(project, mode="plan", model="gpt-4.1")
    mini_again = foundry_agent.create_agent(project, mode="plan")

    assert (mini.version, gpt41.version) == ("1", "2")
    assert gpt41.model == "gpt-4.1"
    assert mini_again == mini, "the older matching version is pinned, not republished"
    assert len(project.agents.created) == 2


def test_a_forged_or_foreign_version_without_our_fingerprint_is_never_reused() -> None:
    foreign = SimpleNamespace(name="autorefine-plan", version="9", metadata={"other": "x"})
    project = _project(FakeAgents([foreign]))

    agent = foundry_agent.create_agent(project, mode="plan")

    assert agent.version == "10"


def test_missing_agent_is_created_on_first_use() -> None:
    project = _project(FakeAgents(missing=True))

    assert foundry_agent.create_agent(project, mode="plan").version == "1"


def test_definition_fingerprint_tracks_model_prompt_tools_and_sampling() -> None:
    fingerprint = foundry_agent.definition_fingerprint
    build = foundry_agent.build_agent_definition
    base = fingerprint(build("plan", "gpt-4o-mini"))

    assert base == fingerprint(build("plan", "gpt-4o-mini"))
    assert base != fingerprint(build("plan", "gpt-4.1"))
    assert base != fingerprint(build("refine", "gpt-4o-mini"))
    assert build("plan", "gpt-4o-mini").temperature == 0.3


def test_tool_schemas_match_the_dispatch_table() -> None:
    names = {tool.name for tool in foundry_agent.tool_definitions("refine")}
    assert names == set(foundry_agent.TOOL_HANDLERS)
    plan = {tool.as_dict()["name"]: tool.as_dict() for tool in foundry_agent.tool_definitions()}
    assert plan["submit_plan"]["parameters"]["properties"]["improvements"]["type"] == "array"
    assert plan["submit_plan"]["parameters"]["properties"]["score"]["type"] == "integer"
    assert all(tool["type"] == "function" for tool in plan.values())


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
    """A project with package.json builds the npm command.

    ``shutil.which`` is patched so this asserts the command that gets built rather than
    whether the developer's machine happens to have Node.js. Without it the test passes
    locally where npm is installed and fails on a runner where it is not, because
    ``_handle_run_tests`` now returns a terminal error before spawning anything.
    """
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    fake_run = SimpleNamespace(returncode=0, stdout="ok", stderr="")

    with patch("shutil.which", lambda cmd: f"/usr/bin/{cmd}"), \
            patch("subprocess.run", return_value=fake_run) as mock_run:
        _ = foundry_agent._handle_run_tests(tmp_path, {})

    assert mock_run.call_args.args[0] == ["npm", "test", "--", "--reporter=verbose"]


def test_handle_run_tests_no_runner_detected(tmp_path: Path) -> None:
    """The condition is still reported; the message is prose for a model, not a contract.

    This used to assert the exact string ``"No test runner detected"``. The message now
    also names what was looked for and carries the terminal fields, because a project
    with no manifest cannot grow one mid-run and a model that reads the old wording as
    transient retries it — see ``tests/test_terminal_tool_errors.py``. Nothing parses
    this text, so it is asserted by substring.
    """
    result = json.loads(foundry_agent._handle_run_tests(tmp_path, {}))
    assert result["error"].startswith("No test runner detected")
    assert result["retryable"] is False


def test_create_agent_uses_passed_model() -> None:
    project = _project()

    agent = foundry_agent.create_agent(project, mode="plan", model="gpt-4.1")

    assert agent.model == "gpt-4.1"
    assert project.agents.created[0]["definition"].model == "gpt-4.1"


def test_create_agent_deadline_is_a_run_deadline_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOREFINE_RUN_TIMEOUT_SECONDS", "1")
    agents = FakeAgents()
    agents.list_versions = Mock(side_effect=foundry_agent._RunDeadlineExceeded("late"))

    with pytest.raises(foundry_agent.FoundryRunAbortedError) as error:
        foundry_agent.create_agent(_project(agents))

    assert error.value.reason == "run_deadline"


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
        foundry_agent._handle_submit_plan(tmp_path, valid_plan())
    )
    assert parsed["status"] == "plan_received"
    assert parsed["improvements_count"] == 1


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
    client = _ToolLoopClient(lambda n: None)
    original = client.next_response

    def failed() -> SimpleNamespace:
        response = original()
        response.status = "failed"
        response.error = SimpleNamespace(code="server_error", message="boom")
        return response

    client.next_response = failed

    result = foundry_agent.run_agent(client, AGENT, Path("."), config, "task")
    assert result is None


def test_run_agent_processes_tool_calls_and_returns_plan() -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client = _ToolLoopClient(lambda n: [
        _DummyToolCall("call-1", "submit_plan", json.dumps(valid_plan())),
    ] if n == 1 else None)
    client.final_text = "Score: 72/100\n1. **Fix tests** — add tests"

    result = foundry_agent.run_agent(client, AGENT, Path("."), config, "task")
    assert result == {**valid_plan(), "research_insights": []}


def test_run_agent_falls_back_to_the_final_text_only_when_it_validates(tmp_path: Path) -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client = _ToolLoopClient(lambda n: None)
    client.final_text = "Score: 72/100\n1. **Fix tests** — add tests"

    assert foundry_agent.run_agent(client, AGENT, tmp_path, config, "task") is None
    assert len(client.requests) == 1, "the final text is read off the response, not refetched"


def test_run_agent_refuses_an_agent_pinned_to_another_model(tmp_path: Path) -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    client = _ToolLoopClient(lambda n: None)

    with pytest.raises(ValueError, match="pinned to gpt-4o-mini"):
        foundry_agent.run_agent(client, AGENT, tmp_path, config, "task", model="gpt-4.1")
    assert client.requests == []


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


def test_openai_transient_errors_are_retried_and_permanent_ones_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_call_foundry_with_retry.retry, "sleep", lambda _seconds: None)
    request = httpx.Request("POST", "https://example.test/openai/v1/responses")

    def status(code: int) -> openai.APIStatusError:
        return openai.APIStatusError("x", response=httpx.Response(code, request=request), body=None)

    for error, retried in [
        (status(429), True), (status(503), True), (status(500), False), (status(400), False),
        (openai.APIConnectionError(request=request), True),
        (openai.APITimeoutError(request=request), True),
    ]:
        operation = Mock(side_effect=[error, "ok"])
        if retried:
            assert _call_foundry_with_retry("client.responses.create", operation) == "ok"
        else:
            with pytest.raises(type(error)):
                _call_foundry_with_retry("client.responses.create", operation)
        assert operation.call_count == (2 if retried else 1)


# ── Prompt-token budget (cost cap) ───────────────────────────────────────────


def test_max_prompt_tokens_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOREFINE_MAX_PROMPT_TOKENS", raising=False)
    assert foundry_agent.resolve_max_prompt_tokens() == foundry_agent.DEFAULT_MAX_PROMPT_TOKENS


def test_max_prompt_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_MAX_PROMPT_TOKENS", " 90000 ")
    assert foundry_agent.resolve_max_prompt_tokens() == 90000


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "100", "19999"])
def test_max_prompt_tokens_rejects_invalid(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_MAX_PROMPT_TOKENS", value)
    with pytest.raises(ValueError):
        foundry_agent.resolve_max_prompt_tokens()


def test_legacy_truncation_window_is_reported_not_silently_obeyed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from openai.resources.responses import Responses

    monkeypatch.setenv("AUTOREFINE_TRUNCATION_LAST_MESSAGES", "4")
    budget = foundry_agent._run_budget(Responses.create)
    assert budget.truncation == "auto"
    assert "AUTOREFINE_TRUNCATION_LAST_MESSAGES is ignored" in caplog.text


def test_installed_sdk_supports_the_response_bounds() -> None:
    """The pinned openai SDK must declare the bounds; guards silent no-ops."""
    from openai.resources.responses import Responses

    params = inspect.signature(Responses.create).parameters
    for name in ("truncation", "max_output_tokens", "previous_response_id", "extra_body"):
        assert name in params


def test_prompt_budget_fails_closed_when_sdk_lacks_params() -> None:
    """A future SDK dropping the params must error, never run unbounded."""

    def legacy_create(input: object, model: str) -> None: ...  # noqa: A002

    with pytest.raises(foundry_agent.FoundryPromptBudgetUnsupportedError):
        foundry_agent._run_budget(legacy_create)


def test_prompt_budget_rejects_kwargs_only_signature() -> None:
    """**kwargs is not evidence of support: a client may drop unknown kwargs."""

    def kwargs_only_create(input: object, **kwargs: object) -> None: ...  # noqa: A002

    with pytest.raises(foundry_agent.FoundryPromptBudgetUnsupportedError) as excinfo:
        foundry_agent._run_budget(kwargs_only_create)
    assert "truncation" in str(excinfo.value)
    assert "max_output_tokens" in str(excinfo.value)


def test_default_max_prompt_tokens_is_a_runaway_guard_not_a_per_call_cap() -> None:
    """max_prompt_tokens is run-wide cumulative; ~11.5k avg input/call means a
    dozen-round plan legitimately spends >100k. The default must not throttle that."""
    assert foundry_agent.DEFAULT_MAX_PROMPT_TOKENS >= 100_000


def test_real_sdk_puts_the_loop_on_the_wire(tmp_path: Path) -> None:
    """The bounds and the chain must reach Foundry, not merely reach ``responses.create``.

    Every other loop test drives a hand-written fake whose ``create`` is *defined*
    to accept these parameters, so all of them would still pass if the real OpenAI
    client dropped them before serialising. This one runs the real ``openai.OpenAI``
    client against a transport that captures the outgoing requests and asserts on
    the actual JSON bodies: the pinned ``agent_reference``, ``truncation``, the
    output budget, and a ``function_call_output`` chained by ``previous_response_id``.
    """
    bodies: list[dict] = []
    deleted: list[str] = []
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": "x", "object": "response", "deleted": True})
        body = json.loads(request.content)
        bodies.append(body)
        n = len(bodies)
        usage = {
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if n == 1:
            output = [{
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "read_project_file", "arguments": '{"path": "README.md"}',
                "status": "completed",
            }]
        else:
            output = [{
                "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
            }]
        return httpx.Response(200, json={
            "id": f"resp_{n}", "object": "response", "created_at": 0, "status": "completed",
            "model": "gpt-4o-mini", "output": output, "usage": usage,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        })

    client = openai.OpenAI(
        base_url="https://stub.services.ai.azure.com/api/projects/p/openai/v1",
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")

    assert foundry_agent.run_agent(client, AGENT, tmp_path, config, "task") is None

    reference = {"name": "autorefine-plan", "version": "7", "type": "agent_reference"}
    assert [body["agent_reference"] for body in bodies] == [reference, reference]
    assert all(body["truncation"] == "auto" and body["store"] is True for body in bodies)
    assert bodies[0]["max_output_tokens"] == foundry_agent.DEFAULT_MAX_COMPLETION_TOKENS
    assert bodies[1]["max_output_tokens"] == foundry_agent.DEFAULT_MAX_COMPLETION_TOKENS - 10
    assert "previous_response_id" not in bodies[0]
    assert bodies[1]["previous_response_id"] == "resp_1"
    (item,) = bodies[1]["input"]
    assert item["type"] == "function_call_output" and item["call_id"] == "call_1"
    assert json.loads(item["output"])["content"] == "# demo"
    assert "model" not in bodies[0], "the pinned agent version owns the model"
    assert deleted == ["resp_1", "resp_2"]


def test_open_foundry_clients_disables_hidden_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The application retry is the only one inside the elapsed deadline."""
    from azure.core.credentials import AccessToken

    class StubCredential:
        def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
            return AccessToken("stub", 9_999_999_999)

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", StubCredential)
    project, client = foundry_agent.open_foundry_clients(
        "https://stub.services.ai.azure.com/api/projects/p",
    )

    assert client.max_retries == 0
    assert str(client.base_url).rstrip("/").endswith("/api/projects/p/openai/v1")
    assert hasattr(project.agents, "create_version")
