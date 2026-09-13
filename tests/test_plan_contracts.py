"""Drive the real dispatcher and functional retry loop without service calls."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from agent import foundry_agent as f
from agent import main as m
from agent.config import ProjectConfig
from agent.plan_validation import is_specified, specificity_errors
from tests.plan_fixtures import valid_plan
from tests.test_foundry_loop_guards import (
    _DummyToolCall,
    _ToolLoopClient,
    loop_dummies,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("loop_dummies")


def _submit(plan: object) -> list[_DummyToolCall]:
    return [_DummyToolCall("submit", "submit_plan", json.dumps(plan))]


def _no_gap() -> dict:
    return {
        "score": 100,
        "summary": "The promised CSV export is present; no additional P0-P2 gap was found.",
        "outcome": "no_gap",
        "improvements": [],
        "no_gap_evidence": [{
            "path": "export.py",
            "observation": "The export function writes every visible row with csv.writer.",
        }],
    }


def _config() -> ProjectConfig:
    return ProjectConfig(name="fixture", purpose="Export visible rows.", users="", stage="active")


def _wire_functional(monkeypatch: pytest.MonkeyPatch, client: _ToolLoopClient) -> None:
    client.threads.create = Mock(side_effect=client.threads.create)
    client.delete_agent = Mock()
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **kw: client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(f, "create_agent", lambda *a, **kw: "agent-1")
    monkeypatch.setattr(m, "_extract_relevant_wiki_insights", lambda name: "")
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("mode", ["propose", "file", "cards"])
@pytest.mark.parametrize("empty", [[], "[]", " [ ]\n"])
def test_evidenced_no_gap_finishes_without_retry_filer_or_notification(
    mode: str, empty: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "export.py").write_text(
        "import csv\n\ndef export(rows, stream):\n    csv.writer(stream).writerows(rows)\n",
        encoding="utf-8",
    )
    steps = {
        1: [_DummyToolCall("read", "read_project_file", '{"path":"export.py"}')],
        2: _submit({**_no_gap(), "improvements": empty}),
    }
    client = _ToolLoopClient(steps.get)
    _wire_functional(monkeypatch, client)
    filer, notifier = Mock(), Mock()
    resolve = Mock(side_effect=AssertionError("no-gap must not resolve a filer"))
    monkeypatch.setattr(m, "_resolve_file_idea_script", resolve)

    result = m.plan_functional(tmp_path, _config())
    assert result == {**_no_gap(), "research_insights": []}
    assert m.handle_functional_ideas(
        "owner/fixture", result, mode=mode, filer=filer, notifier=notifier, carder=filer,
    ) == []
    assert m.file_ideas_for_plan("owner/fixture", result) == 0
    assert client.threads.create.call_count == 1
    client.delete_agent.assert_called_once_with(
        "agent-1", connection_timeout=ANY, read_timeout=ANY, retry_total=0,
    )
    assert client.deleted_threads == ["thread-1"]
    filer.assert_not_called()
    notifier.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "server_error", "rate_limit_exceeded"])
def test_missing_or_transient_output_still_retries_then_returns_a_valid_plan(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ToolLoopClient(lambda round_no: _submit(valid_plan()) if round_no == 2 else None)
    original = client.next_run

    def next_run() -> SimpleNamespace:
        run = original()
        if client.rounds == 1 and failure != "missing":
            run.status = "failed"
            run.last_error = {"code": failure}
        return run

    client.next_run = next_run
    _wire_functional(monkeypatch, client)

    result = m.plan_functional(tmp_path, _config())

    assert result["improvements"] == valid_plan()["improvements"]
    assert client.threads.create.call_count == 2
    assert len(client.deleted_threads) == 2


def test_missing_output_retries_only_the_existing_bounded_number_of_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ToolLoopClient(lambda round_no: None)
    _wire_functional(monkeypatch, client)

    assert m.plan_functional(tmp_path, _config()) is None
    assert client.threads.create.call_count == m.FUNCTIONAL_PLAN_ATTEMPTS


def test_invalid_memo_gets_item_field_feedback_and_correction_is_fileable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = valid_plan()
    invalid["improvements"][0]["approach"] = "Implement export the visible rows."
    del invalid["improvements"][0]["success_criteria"]
    client = _ToolLoopClient(lambda n: _submit(invalid) if n == 1 else None)
    feedback: list[dict] = []

    def submit_outputs(**kwargs: object) -> SimpleNamespace:
        response = json.loads(kwargs["tool_outputs"][0].output)
        feedback.append(response)
        if response["status"] == "plan_rejected":
            assert {e["field"] for e in response["errors"]} == {"approach", "success_criteria"}
            assert all(e["item"] == 1 for e in response["errors"])
            assert "files" in response["errors"][0]["error"]
            assert "reviewer" in response["errors"][1]["error"]
            assert response["repairs_remaining"] == 2
            return SimpleNamespace(
                id="run-1", status="requires_action",
                required_action=f.SubmitToolOutputsAction(_submit(valid_plan())),
            )
        return SimpleNamespace(id="run-1", status="completed")

    client.runs.submit_tool_outputs = submit_outputs
    result = f.run_agent(client, "agent", tmp_path, _config(), "task")
    assert result["improvements"] == valid_plan()["improvements"]
    assert [r["status"] for r in feedback] == ["plan_rejected", "plan_received"]
    assert client.cancelled == []
    assert m.is_specified is is_specified
    assert is_specified(result["improvements"][0])
    assert specificity_errors(invalid["improvements"][0])

    monkeypatch.setattr(m, "_resolve_file_idea_script", lambda: tmp_path / "file-idea.py")
    monkeypatch.setattr(m, "_open_idea_titles", lambda repo: [])
    monkeypatch.setattr(m, "_build_file_idea_command", lambda *a, **kw: ["fake-filer"])
    file_call = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(m.subprocess, "run", file_call)
    assert m.file_ideas_for_plan("owner/fixture", result) == 1
    file_call.assert_called_once()
    file_call.reset_mock()
    assert m.file_ideas_for_plan("owner/fixture", invalid) == 0
    file_call.assert_not_called()


@pytest.mark.parametrize("vary", [False, True])
def test_repeated_rejections_stop_without_buying_a_fresh_functional_run(
    vary: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(round_no: int) -> list[_DummyToolCall]:
        plan = valid_plan(round_no if vary else 72)
        plan["improvements"][0]["approach"] = ""
        return _submit(plan)

    client = _ToolLoopClient(script)
    _wire_functional(monkeypatch, client)

    with pytest.raises(f.FoundryRunAbortedError) as error:
        m.plan_functional(tmp_path, _config())

    assert error.value.reason == "invalid_plan"
    assert "approach" in str(error.value)
    assert client.rounds == f.MAX_PLAN_REJECTIONS
    assert client.threads.create.call_count == 1
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]


@pytest.mark.parametrize("payload", [
    {"score": 100, "summary": "No gaps", "improvements": []},
    {"outcome": "no_gap", "summary": "Fine", "no_gap_evidence": [{"path": "export.py",
                                                             "observation": "Works"}]},
    _no_gap(),
    {**_no_gap(), "improvements": [None]},
    {**_no_gap(), "improvements": "not an array"},
    {**_no_gap(), "no_gap_evidence": []},
    {**_no_gap(), "summary": ""},
    {**_no_gap(), "outcome": []},
    ["not an object"],
])
def test_invalid_or_unread_no_gap_never_becomes_a_successful_empty_plan(
    payload: object, tmp_path: Path,
) -> None:
    client = _ToolLoopClient(lambda n: _submit(payload) if n == 1 else None)
    with pytest.raises(f.FoundryRunAbortedError, match="without repairing"):
        f.run_agent(client, "agent", tmp_path, _config(), "task")


def test_free_text_cannot_bypass_the_specificity_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    client = _ToolLoopClient(lambda n: None)
    client.messages.list = lambda **kw: [SimpleNamespace(
        role=f.MessageRole.AGENT,
        text_messages=[SimpleNamespace(text=SimpleNamespace(
            value="Score: 90/100\n1. **Improve error handling** — Implement error handling."
        ))],
    )]
    caplog.set_level(logging.INFO)

    assert f.run_agent(client, "agent", tmp_path, _config(), "task") is None
    assert "plan_captured=False" in caplog.text


def test_functional_prompt_has_no_quota_and_explains_empty_evidence_contract() -> None:
    prompt = m._functional_task()
    assert "at least 2" not in prompt
    assert "do NOT return an empty plan" not in prompt
    assert "outcome='no_gap'" in prompt
    assert "no_gap_evidence" in prompt
    assert "success_criteria" in prompt


def test_repeating_an_accepted_plan_still_trips_the_existing_stuck_guard(tmp_path: Path) -> None:
    client = _ToolLoopClient(lambda n: _submit(valid_plan()))

    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")

    assert error.value.reason == "stuck_tool_loop"
    assert client.rounds == f.DEFAULT_STUCK_REPEATS
    assert client.cancelled == ["run-1"]


def test_null_category_is_rejected_in_dispatch_and_corrected_before_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = valid_plan()
    invalid["improvements"][0]["category"] = None
    client = _ToolLoopClient(lambda n: _submit(invalid) if n == 1 else None)
    responses: list[dict] = []

    def submit_outputs(**kwargs: object) -> SimpleNamespace:
        response = json.loads(kwargs["tool_outputs"][0].output)
        responses.append(response)
        if len(responses) == 1:
            assert response["status"] == "plan_rejected"
            assert response["errors"][0]["field"] == "category"
            assert response["errors"][0]["item"] == 1
            return SimpleNamespace(
                id="run-1", status="requires_action",
                required_action=f.SubmitToolOutputsAction(_submit(valid_plan())),
            )
        return SimpleNamespace(id="run-1", status="completed")

    client.runs.submit_tool_outputs = submit_outputs
    result = f.run_agent(client, "agent", tmp_path, _config(), "task")
    assert result["improvements"][0]["category"] == "feature"
    monkeypatch.setattr(m, "_resolve_file_idea_script", lambda: tmp_path / "file-idea.py")
    monkeypatch.setattr(m, "_open_idea_titles", lambda _: [])
    monkeypatch.setattr(m, "_discover_file_idea_options", lambda _: {"--repo"})
    file_call = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(m.subprocess, "run", file_call)
    assert m.file_ideas_for_plan("owner/fixture", result) == 1
    file_call.assert_called_once()
    file_call.reset_mock()
    assert m.file_ideas_for_plan("owner/fixture", invalid) == 0
    file_call.assert_not_called()


def test_repeated_invalid_category_exhausts_the_existing_repair_budget(tmp_path: Path) -> None:
    invalid = valid_plan()
    invalid["improvements"][0]["category"] = None
    client = _ToolLoopClient(lambda n: _submit(invalid))
    with pytest.raises(f.FoundryRunAbortedError) as error:
        f.run_agent(client, "agent", tmp_path, _config(), "task")
    assert error.value.reason == "invalid_plan"
    assert client.rounds == f.MAX_PLAN_REJECTIONS


def test_omitted_category_keeps_the_existing_default(tmp_path: Path) -> None:
    plan = valid_plan()
    del plan["improvements"][0]["category"]
    client = _ToolLoopClient(lambda n: _submit(plan) if n == 1 else None)
    result = f.run_agent(client, "agent", tmp_path, _config(), "task")
    assert is_specified(result["improvements"][0])
    assert m._map_improvement_type(result["improvements"][0].get("category", "quality")) == "refactor"
