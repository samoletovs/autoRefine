"""Unit tests for Foundry retry behavior."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import HttpResponseError

from agent import foundry_agent


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
