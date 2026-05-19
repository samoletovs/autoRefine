"""Regression tests for foundry_agent helpers that don't require Foundry."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import HttpResponseError

from agent import foundry_agent
from agent.foundry_agent import _parse_plan_from_text, create_agent


def _http_response_error(status_code: int) -> HttpResponseError:
    response = SimpleNamespace(status_code=status_code, reason="test", text="test")
    return HttpResponseError("boom", response=response)


def test_create_run_retries_on_429_then_succeeds(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(foundry_agent.time, "sleep", lambda _seconds: None)

    run = MagicMock()
    client = MagicMock()
    client.runs.create.side_effect = [
        _http_response_error(429),
        _http_response_error(429),
        run,
    ]

    with caplog.at_level(logging.WARNING):
        result = foundry_agent._create_run(client, "thread-1", "agent-1")

    assert result is run
    assert client.runs.create.call_count == 3
    warnings = [record for record in caplog.records if "Retrying Foundry call" in record.message]
    assert len(warnings) == 2


def test_create_run_does_not_retry_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(foundry_agent.time, "sleep", lambda _seconds: None)

    client = MagicMock()
    client.runs.create.side_effect = _http_response_error(400)

    with pytest.raises(HttpResponseError):
        foundry_agent._create_run(client, "thread-1", "agent-1")

    assert client.runs.create.call_count == 1


class TestParsePlanFromText:
    def test_score_extracted(self) -> None:
        text = "Findings\n\nScore: 84/100\n\n1. **Adopt CI** — ship green.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["score"] == 84

    def test_returns_none_without_improvements(self) -> None:
        plan = _parse_plan_from_text("just some text Score: 12/100 with no list")
        assert plan is None

    def test_em_dash_separator(self) -> None:
        text = "Score: 70/100\n\n1. **Add tests** — cover regressions.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Add tests"

    def test_middle_dot_separator(self) -> None:
        text = "Score: 65/100\n\n1. **Refactor config** · split module.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert len(plan["improvements"]) == 1
        assert plan["improvements"][0]["title"] == "Refactor config"

    def test_priority_tag_captured(self) -> None:
        text = "Score: 60/100\n\n1. [P0] **Add CI** — must ship.\n"
        plan = _parse_plan_from_text(text)
        assert plan is not None
        assert plan["improvements"][0]["priority"] == "P0"

    def test_multiple_separators_in_one_response(self) -> None:
        text = (
            "Score: 72/100\n\n"
            "1. [P0] **Add CI tests** · cover quality_tools.\n"
            "2. **Replace DDG scrape** — switch to a proper API.\n"
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
        sig = inspect.signature(create_agent)
        assert "model" in sig.parameters
        assert sig.parameters["model"].default is None
