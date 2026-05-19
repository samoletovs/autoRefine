"""Tests for issue #idea-add-unit-tests-for-agent-main-and-foundry-agent."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from agent import foundry_agent
from agent.config import ProjectConfig
from agent.foundry_agent import _parse_plan_from_text, create_agent


class TestParsePlanFromText:
    def test_score_extracted(self) -> None:
        # Parser requires at least one improvement row to return a plan.
        text = "Findings\n\nScore: 84/100\n\n1. **Adopt CI** \u2014 ship green.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["score"] == 84

    def test_returns_none_without_improvements(self) -> None:
        """Parser bails out (returns None) when no numbered improvement rows
        are found, so the caller can fall back to other strategies."""
        plan = _parse_plan_from_text("just some text Score: 12/100 with no list")
        assert plan is None

    def test_em_dash_separator(self) -> None:
        text = "Score: 70/100\n\n1. **Add tests** \u2014 cover regressions.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Add tests"

    def test_middle_dot_separator(self) -> None:
        """Regression: previously the regex contained 'ù' (U+00F9, mojibake)
        instead of '·' (U+00B7, middle dot). Lines using '·' as the
        separator were silently dropped."""
        text = "Score: 65/100\n\n1. **Refactor config** \u00b7 split module.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Refactor config"

    def test_priority_tag_captured(self) -> None:
        text = "Score: 60/100\n\n1. [P0] **Add CI** \u2014 must ship.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["improvements"][0]["priority"] == "P0"

    def test_multiple_separators_in_one_response(self) -> None:
        text = (
            "Score: 72/100\n\n"
            "1. [P0] **Add CI tests** \u00b7 cover quality_tools.\n"
            "2. **Replace DDG scrape** \u2014 switch to a proper API.\n"
            "3. [P1] **Retry/backoff** : transient 429s fail evals.\n"
        )
        plan = _parse_plan_from_text(text)
        assert plan is not None
        titles = [i["title"] for i in plan["improvements"]]
        assert titles == [
            "Add CI tests",
            "Replace DDG scrape",
            "Retry/backoff",
        ]
        priorities = [i["priority"] for i in plan["improvements"]]
        assert priorities == ["P0", "P2", "P1"]


class TestCreateAgentSignature:
    def test_accepts_model_kwarg(self) -> None:
        """The CLI --model flag must reach create_agent. This test guards
        against regressions where the kwarg gets dropped from the signature."""
        sig = inspect.signature(create_agent)
        assert "model" in sig.parameters
        assert sig.parameters["model"].default is None


def test_handle_write_project_file_writes_inside_project(tmp_path: Path) -> None:
    payload = {"path": "src/new_file.py", "content": "print('ok')\n"}

    raw = foundry_agent._handle_write_project_file(tmp_path, payload)
    data = json.loads(raw)

    assert data["status"] == "written"
    assert data["path"] == "src/new_file.py"
    assert (tmp_path / "src" / "new_file.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_handle_write_project_file_blocks_path_traversal(tmp_path: Path) -> None:
    payload = {"path": "../escape.py", "content": "bad"}

    raw = foundry_agent._handle_write_project_file(tmp_path, payload)
    data = json.loads(raw)

    assert "Path traversal blocked" in data["error"]


@pytest.mark.parametrize(
    "blocked_path",
    [".git/config", ".env", "node_modules/pkg/index.js", ".github/workflows/ci.yml"],
)
def test_handle_write_project_file_blocks_protected_paths(
    tmp_path: Path, blocked_path: str
) -> None:
    payload = {"path": blocked_path, "content": "blocked"}

    raw = foundry_agent._handle_write_project_file(tmp_path, payload)
    data = json.loads(raw)

    assert "protected path" in data["error"]


def test_handle_apply_improvement_acknowledges_payload(tmp_path: Path) -> None:
    payload = {"title": "Add tests", "files_changed": ["tests/test_new.py"]}

    raw = foundry_agent._handle_apply_improvement(tmp_path, payload)
    data = json.loads(raw)

    assert data["status"] == "improvement_applied"
    assert data["title"] == "Add tests"
    assert data["files_changed"] == ["tests/test_new.py"]


def test_handle_run_tests_python_project_passes(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    with patch.object(
        foundry_agent.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=0, stdout="2 passed", stderr=""),
    ) as mock_run:
        raw = foundry_agent._handle_run_tests(tmp_path, {})

    data = json.loads(raw)
    assert data["passed"] is True
    assert "2 passed" in data["output"]
    mock_run.assert_called_once()


def test_handle_run_tests_python_project_failure(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    with patch.object(
        foundry_agent.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=1, stdout="1 failed", stderr="trace"),
    ):
        raw = foundry_agent._handle_run_tests(tmp_path, {})

    data = json.loads(raw)
    assert data["passed"] is False
    assert "1 failed" in data["output"]


def test_handle_run_tests_no_runner_detected(tmp_path: Path) -> None:
    raw = foundry_agent._handle_run_tests(tmp_path, {})
    data = json.loads(raw)
    assert data["error"] == "No test runner detected"


def test_handle_search_web_non_200_response_returns_error() -> None:
    request = httpx.Request("GET", "https://html.duckduckgo.com/html/")
    response = httpx.Response(status_code=503, request=request)

    with patch("httpx.get", return_value=response):
        raw = foundry_agent._handle_search_web(Path("."), {"query": "autorefine competitors"})

    data = json.loads(raw)
    assert data["query"] == "autorefine competitors"
    assert "Search failed" in data["error"]


def test_handle_search_web_empty_query_returns_error() -> None:
    raw = foundry_agent._handle_search_web(Path("."), {"query": ""})
    data = json.loads(raw)
    assert data["error"] == "Empty query"


def test_handle_read_project_file_reads_and_truncates(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    raw = foundry_agent._handle_read_project_file(
        tmp_path,
        {"path": "README.md", "max_lines": 2},
    )
    data = json.loads(raw)

    assert data["path"] == "README.md"
    assert data["content"] == "line1\nline2"
    assert data["truncated"] is True


def test_handle_read_project_file_blocks_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "README.md"
    outside.write_text("outside", encoding="utf-8")
    raw = foundry_agent._handle_read_project_file(tmp_path, {"path": "../README.md"})
    data = json.loads(raw)
    assert data["error"] == "Path traversal blocked"


def test_handle_list_directory_lists_entries_and_skips_dangerous_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    raw = foundry_agent._handle_list_directory(tmp_path, {"path": "."})
    data = json.loads(raw)

    names = [entry["name"] for entry in data["entries"]]
    assert "src" in names
    assert ".git" not in names


def test_handle_list_directory_blocks_traversal(tmp_path: Path) -> None:
    raw = foundry_agent._handle_list_directory(tmp_path, {"path": "../"})
    data = json.loads(raw)
    assert data["error"] == "Path traversal blocked"


def test_handle_search_web_parses_html_results() -> None:
    html = """
    <a class="result__a">Result One</a>
    <a class="result__snippet">Snippet One</a>
    <a class="result__url" href="https://example.com/1"></a>
    """
    response = httpx.Response(
        status_code=200,
        text=html,
        request=httpx.Request("GET", "https://html.duckduckgo.com/html/"),
    )

    with patch("httpx.get", return_value=response):
        raw = foundry_agent._handle_search_web(Path("."), {"query": "demo"})

    data = json.loads(raw)
    assert data["query"] == "demo"
    assert data["results"][0]["title"] == "Result One"
    assert data["results"][0]["snippet"] == "Snippet One"


def test_tool_definition_stubs_return_empty_string() -> None:
    assert foundry_agent.read_project_file("README.md") == ""
    assert foundry_agent.list_directory(".") == ""
    assert foundry_agent.search_web("query") == ""
    assert foundry_agent.run_project_tests() == ""
    assert foundry_agent.submit_plan(1, "summary", []) == ""
    assert foundry_agent.write_project_file("a.txt", "x") == ""
    assert foundry_agent.apply_improvement("title", "desc", []) == ""


def test_create_agent_uses_selected_model_and_refine_tools() -> None:
    captured: dict = {}

    def _create_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="agent-1")

    client = SimpleNamespace(create_agent=_create_agent)

    agent_id = foundry_agent.create_agent(client, mode="refine", model="gpt-4.1")

    assert agent_id == "agent-1"
    assert captured["model"] == "gpt-4.1"
    tool_names = {tool["function"]["name"] for tool in captured["tools"]}
    assert "write_project_file" in tool_names
    assert "apply_improvement" in tool_names


def test_build_plan_task_and_refine_task_include_expected_sections() -> None:
    config = SimpleNamespace(similar=["A", "B"])
    plan_text = foundry_agent.build_plan_task(
        findings=[{"priority": "P0", "category": "tests", "description": "Missing unit tests"}],
        config=config,
    )
    assert "Quality check findings" in plan_text
    assert "Similar products to research" in plan_text

    refine_text = foundry_agent.build_refine_task(
        plan={"improvements": [{"priority": "P1", "title": "Add tests", "description": "Cover main.py"}]},
        config=SimpleNamespace(),
    )
    assert "Improvements to apply" in refine_text
    assert "[P1] Add tests" in refine_text


def test_run_agent_processes_tool_calls_and_returns_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        text_messages=[
            SimpleNamespace(
                text=SimpleNamespace(value="Score: 72/100\n1. **Add tests** — keep coverage high")
            )
        ],
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
        def __init__(self) -> None:
            self.create_called = False

        def create(self, **_kwargs):
            self.create_called = True
            return requires_action_run

        def submit_tool_outputs(self, **_kwargs):
            return completed_run

        def get(self, **_kwargs):
            return completed_run

    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=RunsApi())

    result = foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")
    assert result == {"score": 72, "summary": "ok", "improvements": []}


def test_run_agent_falls_back_to_text_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    monkeypatch.setattr(foundry_agent, "SubmitToolOutputsAction", object)
    monkeypatch.setattr(foundry_agent, "RequiredFunctionToolCall", object)

    thread_api = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-2"),
        delete=lambda _thread_id: None,
    )
    agent_message = SimpleNamespace(
        role=foundry_agent.MessageRole.AGENT,
        text_messages=[
            SimpleNamespace(text=SimpleNamespace(value="Score: 77/100\n1. **Add tests** — done"))
        ],
    )
    messages_api = SimpleNamespace(
        create=lambda **_kwargs: None,
        list=lambda **_kwargs: [agent_message],
    )
    completed_run = SimpleNamespace(id="run-2", status="completed")
    runs_api = SimpleNamespace(
        create=lambda **_kwargs: completed_run,
        get=lambda **_kwargs: completed_run,
    )
    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=runs_api)

    result = foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")
    assert result is not None
    assert result["score"] == 77


def test_run_agent_returns_none_on_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ProjectConfig(name="demo", purpose="", users="", stage="active")
    monkeypatch.setattr(foundry_agent, "SubmitToolOutputsAction", object)
    monkeypatch.setattr(foundry_agent, "RequiredFunctionToolCall", object)

    thread_api = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-3"),
        delete=lambda _thread_id: None,
    )
    failed_run = SimpleNamespace(id="run-3", status="failed", last_error="boom")
    runs_api = SimpleNamespace(create=lambda **_kwargs: failed_run)
    messages_api = SimpleNamespace(create=lambda **_kwargs: None)
    client = SimpleNamespace(threads=thread_api, messages=messages_api, runs=runs_api)

    result = foundry_agent.run_agent(client, "agent-1", Path("."), config, "task")
    assert result is None
