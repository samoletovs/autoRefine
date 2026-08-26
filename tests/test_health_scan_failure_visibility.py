"""Tests that a failed AI analysis is visible to a human.

``analyze_with_ai`` degrades to ``{"error": ...}`` rather than raising, which
keeps the sweep alive — but until this change the degraded result rendered
exactly like a clean one: no alerts, no recommendations, ``?`` scores. A broken
scan and a quiet week were indistinguishable in both the committed report and
the Telegram message, which is the only part anyone reliably reads.

These tests pin the two states apart.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from agent import health_scan

FAILED: dict[str, Any] = {
    "error": "model returned list, expected a JSON object",
    "recommendations": [],
    "alerts": [],
}
CLEAN: dict[str, Any] = {
    "health_scores": {"era": {"R": 3, "L": 4, "M": 2, "health": 9}},
    "alerts": [],
    "recommendations": ["Ship the thing"],
    "focus_project": "era",
    "issues_to_create": [],
}
NOISY: dict[str, Any] = {
    "health_scores": {"era": {"R": 3, "L": 4, "M": 2, "health": 9}},
    "alerts": ["era: CI red for 3 runs"],
    "recommendations": ["Fix CI"],
    "focus_project": "era",
    "issues_to_create": [],
}
GITHUB_DATA: dict[str, Any] = {"era": {"open_issues": 2, "ci_status": "success"}}
COST_DATA: dict[str, Any] = {"total": 5, "budget": 150, "by_resource_group": {}}


# ── analysis_failed ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("label", "analysis", "expected"),
    [
        ("degraded result", FAILED, True),
        ("endpoint missing", {"error": "AZURE_OPENAI_ENDPOINT not set"}, True),
        ("clean result", CLEAN, False),
        ("result with alerts", NOISY, False),
        ("empty dict", {}, False),
        ("empty error string", {"error": ""}, False),
    ],
)
def test_analysis_failed(label: str, analysis: dict[str, Any], expected: bool) -> None:
    assert health_scan.analysis_failed(analysis) is expected


# ── the report says so ─────────────────────────────────────────────────────
def test_failed_analysis_report_is_visibly_degraded() -> None:
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, FAILED)

    assert "AI ANALYSIS FAILED" in report
    assert "INCOMPLETE" in report
    assert "model returned list" in report


def test_failure_banner_sits_above_the_data() -> None:
    """A warning below a table of numbers is a warning nobody reads."""
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, FAILED)

    assert report.index("AI ANALYSIS FAILED") < report.index("## Project Health")


def test_failed_analysis_explains_the_question_marks() -> None:
    """The table still renders ``?`` scores; the banner must account for them."""
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, FAILED)

    assert "| ? | ? | ? | ? |" in report
    assert "missing" in report
    assert "No issues were filed this run." in report


def test_clean_analysis_says_so_explicitly() -> None:
    """Silence is not evidence of health — the healthy case is stated."""
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, CLEAN)

    assert "No alerts" in report
    assert "AI ANALYSIS FAILED" not in report


def test_failed_and_clean_reports_are_distinguishable() -> None:
    """The property this whole change exists for."""
    failed = health_scan.generate_report(GITHUB_DATA, COST_DATA, FAILED)
    clean = health_scan.generate_report(GITHUB_DATA, COST_DATA, CLEAN)

    assert failed != clean
    assert "AI ANALYSIS FAILED" in failed
    assert "AI ANALYSIS FAILED" not in clean
    assert "No alerts" in clean
    assert "No alerts" not in failed


def test_real_alerts_still_render_and_suppress_the_clean_banner() -> None:
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, NOISY)

    assert "## 🚨 Alerts" in report
    assert "- era: CI red for 3 runs" in report
    assert "No alerts" not in report
    assert "AI ANALYSIS FAILED" not in report


def test_failure_banner_uses_no_bullet_lines() -> None:
    """The banner must not read as a list of findings.

    Kept after the scraper was removed: a bulleted banner would still look
    like a set of alerts to a human skimming the report.
    """
    report = health_scan.generate_report(GITHUB_DATA, COST_DATA, FAILED)
    banner = report.split("## Project Health")[0]

    assert not [line for line in banner.split("\n") if line.startswith("- ")]


# ── the Telegram message says so ───────────────────────────────────────────
def test_telegram_summary_reports_the_failure() -> None:
    msg = health_scan.build_telegram_summary(FAILED, None, [])

    assert "FAILED" in msg
    assert "model returned list" in msg
    assert "No scores, no alerts, no issues filed this run." in msg


def test_telegram_failure_notice_is_near_the_top() -> None:
    msg = health_scan.build_telegram_summary(
        FAILED, None, [], cost_data={"total": 42.5, "budget": 150.0}
    )
    lines = msg.split("\n")

    assert "FAILED" in lines[1]
    # Ahead of the cost line, which is the other thing competing for attention.
    assert next(i for i, x in enumerate(lines) if "Azure" in x) > 1


def test_telegram_summary_stays_quiet_on_a_clean_analysis() -> None:
    msg = health_scan.build_telegram_summary(CLEAN, None, [])

    assert "FAILED" not in msg


def test_failed_summary_shows_no_alerts_at_all() -> None:
    """A failed analysis produced no alerts, so none may be shown."""
    msg = health_scan.build_telegram_summary(
        FAILED, None, [], cost_data={"total": 5, "budget": 150}
    )

    assert "🚨" not in msg
    assert "✅ No alerts" not in msg  # that would claim the analysis ran
    assert "No scores, no alerts, no issues filed this run." in msg


def test_telegram_summary_is_three_distinct_messages() -> None:
    """Failed, ran-and-clean, and ran-with-alerts must never look alike."""
    cost: dict[str, Any] = {"total": 5, "budget": 150}
    failed = health_scan.build_telegram_summary(FAILED, None, [], cost_data=cost)
    clean = health_scan.build_telegram_summary(CLEAN, None, [], cost_data=cost)
    noisy = health_scan.build_telegram_summary(NOISY, None, [], cost_data=cost)

    assert failed != clean
    assert clean != noisy
    assert failed != noisy
    assert "FAILED" in failed
    assert "✅ No alerts" in clean and "🚨" not in clean
    assert "🚨 era: CI red for 3 runs" in noisy
    assert "✅ No alerts" not in noisy and "FAILED" not in noisy


def test_telegram_error_text_is_truncated() -> None:
    """A stack trace in the error must not become the whole message."""
    msg = health_scan.build_telegram_summary({"error": "x" * 900}, None, [])

    assert len(msg) < 500


# ── end to end through run_health_scan ─────────────────────────────────────
def _run_scan(analysis: dict[str, Any]) -> dict[str, Any]:
    with (
        patch("agent.health_scan.scan_github", return_value=GITHUB_DATA),
        patch("agent.health_scan.scan_azure_costs", return_value=COST_DATA),
        patch("agent.health_scan.scan_app_insights", return_value={}),
        patch("agent.health_scan.check_deployed_urls", return_value={}),
        patch("agent.health_scan.analyze_with_ai", return_value=analysis),
        patch("agent.health_scan.commit_report", return_value="reports/run/r.md"),
        patch("agent.health_scan.enforce_report_retention"),
        patch("agent.notify.send_telegram", return_value=True),
    ):
        return health_scan.run_health_scan(["era"])


def test_run_health_scan_surfaces_failure_and_files_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The degraded dict must reach the human and file no issues.

    ``analyze_with_ai``'s error shape carries no ``issues_to_create`` key,
    which is what makes a failed analysis file nothing. Assert the property
    rather than trusting it.
    """
    monkeypatch.setenv("GH_TOKEN", "fake-token")

    with (
        caplog.at_level(logging.ERROR, logger="agent.health_scan"),
        patch("agent.health_scan.create_github_issues", return_value=[]) as create,
    ):
        result = _run_scan(FAILED)

    assert result["analysis_failed"] is True
    assert "FAILED" in result["telegram_summary"]
    assert create.call_args.args[1] == []
    assert any("AI analysis failed" in r.message for r in caplog.records)


def test_run_health_scan_reports_a_clean_analysis_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fake-token")

    with patch("agent.health_scan.create_github_issues", return_value=[]):
        result = _run_scan(CLEAN)

    assert result["analysis_failed"] is False
    assert "FAILED" not in result["telegram_summary"]
