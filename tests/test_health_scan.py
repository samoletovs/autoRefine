"""Unit tests for agent.health_scan.

Tests focus on logic (report generation, summary building, retention, repo
filtering) rather than live API calls. External services are mocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent import health_scan


# ── build_telegram_summary ────────────────────────────────────────────────
def test_build_summary_minimal() -> None:
    report = "# NauroLabs Health Report\n\n## Project Health\n"
    msg = health_scan.build_telegram_summary(report, None, [])
    assert "NauroLabs Health Report" in msg
    assert "<b>" in msg  # HTML parse mode


def test_build_summary_with_focus_and_alerts() -> None:
    report = (
        "# NauroLabs Health Report\n\n"
        "## 🚨 Alerts\n"
        "- alert one\n"
        "- alert two\n"
        "- alert three\n"
        "- alert four\n\n"
        "## 🎯 This Week: focus on era\n"
    )
    msg = health_scan.build_telegram_summary(report, "reports/run/run-2026-05-17.md", ["url1"])
    assert "focus on era" in msg
    assert "alert one" in msg
    assert "alert four" not in msg  # capped at 3
    assert "Created 1 tech-debt issue" in msg
    assert 'href="https://github.com/samoletovs/nauroLabs-github' in msg


def test_build_summary_no_issues_no_focus() -> None:
    report = "# Report\n"
    msg = health_scan.build_telegram_summary(report, None, [])
    assert "Created" not in msg
    assert "Full report" not in msg


# ── generate_report ────────────────────────────────────────────────────────
def test_generate_report_includes_project_table() -> None:
    github_data: dict[str, Any] = {
        "era": {
            "open_issues": 3,
            "bug_count": 1,
            "open_prs": 0,
            "commits_7d": 5,
            "ci_status": "success",
        }
    }
    cost_data: dict[str, Any] = {
        "total": 12.5,
        "projected": 50,
        "budget": 150,
        "remaining": 137.5,
        "by_resource_group": {"rg-era": 12.5},
    }
    analysis: dict[str, Any] = {
        "health_scores": {"era": {"R": 4, "L": 4, "M": 5, "health": 13}},
        "alerts": [],
        "recommendations": ["test rec"],
    }

    report = health_scan.generate_report(github_data, cost_data, analysis)
    assert "| era |" in report
    assert "✅" in report  # ci_status=success
    assert "$12.5" in report
    assert "rg-era" in report
    assert "test rec" in report


def test_generate_report_handles_missing_cost() -> None:
    report = health_scan.generate_report(
        github_data={},
        cost_data={"error": "no creds", "total": -1},
        analysis={},
    )
    assert "Cost scan unavailable" in report


def test_generate_report_omits_telemetry_when_clean() -> None:
    report = health_scan.generate_report(
        github_data={"era": {"open_issues": 0, "bug_count": 0, "open_prs": 0, "commits_7d": 1, "ci_status": "success"}},
        cost_data={"total": 5, "projected": 20, "budget": 150, "remaining": 145, "by_resource_group": {}},
        analysis={},
        app_insights_data={"era": {"exception_count": 0, "failed_request_count": 0}},
    )
    assert "App Telemetry" not in report


# ── run_health_scan integration (mocked) ───────────────────────────────────
def test_run_health_scan_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "fake-token")

    with (
        patch("agent.health_scan.scan_github", return_value={"era": {"open_issues": 0, "ci_status": "success"}}),
        patch("agent.health_scan.scan_azure_costs", return_value={"total": 5, "projected": 20, "budget": 150, "remaining": 145, "by_resource_group": {}}),
        patch("agent.health_scan.scan_app_insights", return_value={}),
        patch("agent.health_scan.check_deployed_urls", return_value={}),
        patch("agent.health_scan.analyze_with_ai", return_value={"health_scores": {}, "alerts": [], "recommendations": [], "issues_to_create": []}),
        patch("agent.health_scan.commit_report", return_value="reports/run/run-2026-05-17.md"),
        patch("agent.health_scan.enforce_report_retention"),
        patch("agent.health_scan.create_github_issues", return_value=[]),
        patch("agent.notify.send_telegram", return_value=True) as mock_send,
    ):
        result = health_scan.run_health_scan(["era"])

    assert result["report_path"] == "reports/run/run-2026-05-17.md"
    assert result["github_repos_scanned"] == 1
    mock_send.assert_called_once()


def test_run_health_scan_missing_gh_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="GH_TOKEN"):
        health_scan.run_health_scan(["era"])


def test_run_health_scan_falls_back_to_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")

    with (
        patch("agent.health_scan.scan_github", return_value={}) as mock_scan,
        patch("agent.health_scan.scan_azure_costs", return_value={"total": 0, "by_resource_group": {}}),
        patch("agent.health_scan.scan_app_insights", return_value={}),
        patch("agent.health_scan.check_deployed_urls", return_value={}),
        patch("agent.health_scan.analyze_with_ai", return_value={}),
        patch("agent.health_scan.commit_report", return_value=None),
        patch("agent.health_scan.enforce_report_retention"),
        patch("agent.health_scan.create_github_issues", return_value=[]),
        patch("agent.notify.send_telegram", return_value=False),
    ):
        result = health_scan.run_health_scan(["era"])

    assert result["report_path"] is None
    mock_scan.assert_called_once()
    args, _ = mock_scan.call_args
    assert args[0] == "fallback-token"


# ── create_github_issues filters by allowed repos ──────────────────────────
def test_create_github_issues_skips_unknown_repo() -> None:
    issues = [
        {"repo": "era", "title": "fix bug", "body": "..."},
        {"repo": "ghost-repo", "title": "should be skipped", "body": "..."},
    ]
    mock_resp = MagicMock(status_code=201)
    mock_resp.json.return_value = {"html_url": "https://github.com/samoletovs/era/issues/1"}

    with patch("agent.health_scan.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_resp

        created = health_scan.create_github_issues("tok", issues, allowed_repos=["era"])

    assert len(created) == 1
    assert client_instance.post.call_count == 1


def test_create_github_issues_caps_at_five() -> None:
    issues = [{"repo": "era", "title": f"issue {i}", "body": "..."} for i in range(10)]
    mock_resp = MagicMock(status_code=201)
    mock_resp.json.return_value = {"html_url": "https://github.com/samoletovs/era/issues/1"}

    with patch("agent.health_scan.httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_resp

        created = health_scan.create_github_issues("tok", issues, allowed_repos=["era"])

    assert len(created) == 5
    assert client_instance.post.call_count == 5
