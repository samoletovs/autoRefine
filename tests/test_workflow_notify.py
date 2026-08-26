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


# ── The signal must never claim more than is true ────────────────────────────
#
# Reproduces the real dispatch of 2026-08-26 (run 32974315714): "Azure login"
# failed, so "Run autoRefine", "Parse scores" and "Create or update failure
# issue" were all skipped, and the notifier ran with STATUS=skipped and an
# empty ISSUE_URL. It told a human "Copilot has been notified to investigate"
# when no issue existed and nobody had been notified.


def test_infrastructure_failure_does_not_claim_copilot_was_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact message the failed dispatch produced. It must not reassure."""
    monkeypatch.setenv("STATUS", "skipped")
    monkeypatch.setenv("MODE", "plan")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/32974315714")
    # No ISSUE_URL: the issue step was skipped along with everything else.

    msg = workflow_notify.build_message()

    assert "Copilot has been notified" not in msg, (
        "a false reassurance is worse than silence — it stops the human looking"
    )
    assert "no issue was filed" in msg.lower()
    assert "needs a human" in msg
    assert "Tracking issue" not in msg


def test_infrastructure_failure_is_distinguishable_from_a_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different causes need different human responses, so they must read differently."""
    monkeypatch.setenv("RUN_URL", "https://example.test/run/1")

    monkeypatch.setenv("STATUS", "skipped")
    infrastructure = workflow_notify.build_message()

    monkeypatch.setenv("STATUS", "failure")
    parse = workflow_notify.build_message()

    assert infrastructure != parse
    # The run never reached a project, so "no scores parsed" would misdescribe it.
    assert "before any project was scored" in infrastructure
    assert "stopped before any project was evaluated" in infrastructure
    assert "no scores could be parsed" in parse
    assert "before any project was scored" not in parse


@pytest.mark.parametrize("status", ["skipped", "cancelled"])
def test_statuses_that_mean_the_step_never_ran(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub reports both when an earlier step ended the job."""
    monkeypatch.setenv("STATUS", status)

    assert workflow_notify._failure_kind(status, has_scores=False) == "infrastructure"
    assert "before any project was scored" in workflow_notify.build_message()


def test_parse_failure_without_an_issue_is_also_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even on the path that files an issue, the filing itself can fail."""
    monkeypatch.setenv("STATUS", "failure")

    msg = workflow_notify.build_message()

    assert "Copilot has been notified" not in msg
    assert "needs a human" in msg


def test_copilot_claim_returns_when_an_issue_really_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentence is not banned — it is conditioned on being true."""
    monkeypatch.setenv("STATUS", "failure")
    monkeypatch.setenv("ISSUE_URL", "https://github.com/o/r/issues/42")

    msg = workflow_notify.build_message()

    assert "Copilot has been notified to investigate." in msg
    assert "needs a human" not in msg
    assert "issues/42" in msg


def test_success_is_unaffected_by_the_failure_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCORES", "era: 80/100")
    monkeypatch.setenv("TOTAL", "1")
    monkeypatch.setenv("AVG", "80")
    monkeypatch.setenv("STATUS", "success")

    msg = workflow_notify.build_message()

    assert workflow_notify._failure_kind("success", has_scores=True) is None
    assert "FAILED" not in msg
    assert "Copilot" not in msg
    assert "era: 80/100" in msg
