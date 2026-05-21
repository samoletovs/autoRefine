"""Unit tests for agent.workflow_notify.build_message."""

from __future__ import annotations

import pytest

from agent import workflow_notify


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SCORES", "TOTAL", "AVG", "MODE", "STATUS", "RUN_URL", "ISSUE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_success_message_contains_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCORES", "era: 80/100\nturgo: 70/100")
    monkeypatch.setenv("TOTAL", "2")
    monkeypatch.setenv("AVG", "75")
    monkeypatch.setenv("MODE", "evaluate")
    monkeypatch.setenv("STATUS", "success")

    msg = workflow_notify.build_message()

    assert "🔧" in msg
    assert "evaluate" in msg
    assert "2 projects" in msg
    assert "avg 75/100" in msg
    assert "era: 80/100" in msg


def test_failure_message_when_status_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATUS", "failure")
    monkeypatch.setenv("MODE", "file-ideas")
    monkeypatch.setenv("RUN_URL", "https://github.com/owner/repo/actions/runs/123")
    monkeypatch.setenv("ISSUE_URL", "https://github.com/owner/repo/issues/9")

    msg = workflow_notify.build_message()

    assert "❌" in msg
    assert "FAILED" in msg
    assert "file-ideas" in msg
    assert "actions/runs/123" in msg
    assert "issues/9" in msg
    assert "Copilot" in msg


def test_failure_message_when_scores_empty_even_if_status_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATUS", "success")
    monkeypatch.setenv("SCORES", "")
    monkeypatch.setenv("TOTAL", "")
    monkeypatch.setenv("AVG", "")
    monkeypatch.setenv("RUN_URL", "https://example.test/run/1")

    msg = workflow_notify.build_message()

    assert "FAILED" in msg
    assert "https://example.test/run/1" in msg


def test_failure_message_without_optional_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATUS", "failure")

    msg = workflow_notify.build_message()

    assert "FAILED" in msg
    assert "View run" not in msg
    assert "Tracking issue" not in msg


def test_default_mode_when_unset() -> None:
    msg = workflow_notify.build_message()

    assert "file-ideas" in msg
