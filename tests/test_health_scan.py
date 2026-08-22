"""Unit tests for agent.health_scan.

Tests focus on logic (report generation, summary building, retention, repo
filtering) rather than live API calls. External services are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent import health_scan


# ── improvement tracking dashboard ──────────────────────────────────────────
def test_build_improvement_items_tracks_status_and_actions() -> None:
    items = health_scan._build_improvement_items(
        [
            {
                "title": "[idea] Add dashboard",
                "body": "### Source\n\nautorefine",
                "state": "open",
                "labels": [{"name": "idea"}, {"name": "approved"}],
                "assignees": [{"login": "Copilot"}],
                "comments": 1,
                "html_url": "https://github.com/samoletovs/era/issues/1",
            },
            {
                "title": "[idea] Old decline",
                "body": "### Source\n\nautorefine",
                "state": "closed",
                "labels": [{"name": "idea"}, {"name": "declined"}],
                "assignees": [],
                "comments": 0,
                "html_url": "https://github.com/samoletovs/era/issues/2",
            },
        ]
    )

    assert items == [
        {
            "title": "Add dashboard",
            "status": "in progress",
            "actions": "approved, assigned to Copilot, 1 comment",
            "url": "https://github.com/samoletovs/era/issues/1",
        },
        {
            "title": "Old decline",
            "status": "declined",
            "actions": "declined",
            "url": "https://github.com/samoletovs/era/issues/2",
        },
    ]


def test_build_improvement_items_ignores_non_autorefine_ideas() -> None:
    items = health_scan._build_improvement_items(
        [
            {
                "title": "Plain issue",
                "body": "not an idea memo",
                "state": "open",
                "labels": [{"name": "bug"}],
                "assignees": [],
                "comments": 0,
            },
            {
                "title": "[idea] Manual idea",
                "body": "source: human",
                "state": "open",
                "labels": [{"name": "idea"}],
                "assignees": [],
                "comments": 0,
            },
        ]
    )

    assert items == []


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


def test_build_summary_includes_azure_cost() -> None:
    report = "# NauroLabs Health Report\n"
    cost_data: dict[str, Any] = {
        "total": 42.5,
        "projected": 80.0,
        "budget": 150.0,
        "remaining": 107.5,
    }
    msg = health_scan.build_telegram_summary(report, None, [], cost_data=cost_data)
    assert "Azure" in msg
    assert "$42.5" in msg
    assert "projected $80.0" in msg
    assert "$150.0 budget" in msg
    assert "OVER BUDGET" not in msg
    assert "💰" in msg  # below 70 % threshold


def test_build_summary_azure_cost_yellow_warning() -> None:
    cost_data: dict[str, Any] = {
        "total": 110.0,
        "projected": 140.0,
        "budget": 150.0,
        "remaining": 40.0,
    }
    msg = health_scan.build_telegram_summary("# Report\n", None, [], cost_data=cost_data)
    assert "🟡" in msg  # 73 % used → yellow


def test_build_summary_azure_cost_over_budget() -> None:
    cost_data: dict[str, Any] = {
        "total": 130.0,
        "projected": 160.0,
        "budget": 150.0,
        "remaining": 20.0,
    }
    msg = health_scan.build_telegram_summary("# Report\n", None, [], cost_data=cost_data)
    assert "🔴" in msg  # projected > budget
    assert "OVER BUDGET" in msg


def test_build_summary_azure_cost_negative_remaining() -> None:
    cost_data: dict[str, Any] = {
        "total": 160.0,
        "projected": 190.0,
        "budget": 150.0,
        "remaining": -10.0,
    }
    msg = health_scan.build_telegram_summary("# Report\n", None, [], cost_data=cost_data)
    assert "OVER BUDGET" in msg


def test_build_summary_azure_cost_error_skipped() -> None:
    """When cost_data signals an error (total=-1), no cost line is added."""
    cost_data: dict[str, Any] = {"error": "no creds", "total": -1}
    msg = health_scan.build_telegram_summary("# Report\n", None, [], cost_data=cost_data)
    assert "Azure" not in msg


def test_build_summary_cost_data_none_skipped() -> None:
    """When cost_data is None (default), no cost line is added."""
    msg = health_scan.build_telegram_summary("# Report\n", None, [])
    assert "Azure" not in msg


# ── generate_report ────────────────────────────────────────────────────────
def test_generate_report_includes_project_table() -> None:
    github_data: dict[str, Any] = {
        "era": {
            "open_issues": 3,
            "bug_count": 1,
            "open_prs": 0,
            "commits_7d": 5,
            "ci_status": "success",
            "recent_ideas": [],
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


def test_generate_report_includes_improvement_tracking_table() -> None:
    report = health_scan.generate_report(
        github_data={
            "era": {
                "open_issues": 1,
                "bug_count": 0,
                "open_prs": 1,
                "commits_7d": 2,
                "ci_status": "success",
                "recent_ideas": [
                    {
                        "title": "Add dashboard",
                        "status": "in progress",
                        "actions": "approved, assigned to Copilot",
                        "url": "https://github.com/samoletovs/era/issues/1",
                    }
                ],
            }
        },
        cost_data={"total": 5, "projected": 10, "budget": 150, "remaining": 145, "by_resource_group": {}},
        analysis={"health_scores": {"era": {"R": 4, "L": 4, "M": 4, "health": 12}}},
    )

    assert "## Improvement Tracking" in report
    assert "[Add dashboard](https://github.com/samoletovs/era/issues/1)" in report
    assert "| era |" in report
    assert "approved, assigned to Copilot" in report


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


def test_create_github_issues_assigns_copilot() -> None:
    issues = [{"repo": "era", "title": "fix bug", "body": "..."}]
    mock_resp = MagicMock(status_code=201)
    mock_resp.json.return_value = {
        "html_url": "https://github.com/samoletovs/era/issues/12",
        "number": 12,
    }

    with (
        patch("agent.health_scan.httpx.Client") as mock_client_cls,
        patch("agent.health_scan.subprocess.run") as mock_subprocess_run,
    ):
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_resp
        mock_subprocess_run.return_value.returncode = 0

        created = health_scan.create_github_issues("tok", issues, allowed_repos=["era"])

    assert created == ["https://github.com/samoletovs/era/issues/12"]
    mock_subprocess_run.assert_called_once_with(
        [
            "gh",
            "issue",
            "edit",
            "12",
            "--repo",
            "samoletovs/era",
            "--add-assignee",
            "copilot",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_create_github_issues_can_skip_copilot_assignment() -> None:
    issues = [{"repo": "era", "title": "fix bug", "body": "..."}]
    mock_resp = MagicMock(status_code=201)
    mock_resp.json.return_value = {
        "html_url": "https://github.com/samoletovs/era/issues/12",
        "number": 12,
    }

    with (
        patch("agent.health_scan.httpx.Client") as mock_client_cls,
        patch("agent.health_scan.subprocess.run") as mock_subprocess_run,
    ):
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_resp

        created = health_scan.create_github_issues(
            "tok", issues, allowed_repos=["era"], assign_copilot=False
        )

    assert created == ["https://github.com/samoletovs/era/issues/12"]
    mock_subprocess_run.assert_not_called()


# ── workspace manifest helpers ─────────────────────────────────────────────

_SAMPLE_MANIFEST: dict[str, Any] = {
    "projects": [
        {
            "slug": "era",
            "repo": "samoletovs/era",
            "domain": "era.naurolabs.com",
            "health_path": "/health",
            "status": "active",
            "azure": {"resourceGroup": "rg-era"},
        },
        {
            "slug": "golazo",
            "repo": "samoletovs/golazo",
            "domain": "golazo.naurolabs.com",
            "status": "active",
            "azure": {"resourceGroup": "rg-golazo"},
        },
        {
            "slug": "playground",
            "repo": "samoletovs/playground",
            "domain": "playground.naurolabs.com",
            "status": "active",
        },
        {
            "slug": "oldProject",
            "repo": "samoletovs/oldProject",
            "domain": "old.naurolabs.com",
            "status": "archived",
            "azure": {"resourceGroup": "rg-old"},
        },
    ]
}


def test_build_app_urls_from_manifest() -> None:
    urls = health_scan._build_app_urls(_SAMPLE_MANIFEST)
    assert urls == {
        "era": "https://era.naurolabs.com/health",
        "golazo": "https://golazo.naurolabs.com/",
        "playground": "https://playground.naurolabs.com/",
    }
    assert "oldProject" not in urls  # archived projects excluded


def test_build_repo_resource_groups_from_manifest() -> None:
    rgs = health_scan._build_repo_resource_groups(_SAMPLE_MANIFEST)
    assert rgs == {
        "era": "rg-era",
        "golazo": "rg-golazo",
    }
    assert "playground" not in rgs  # no azure.resourceGroup
    assert "oldProject" not in rgs  # archived


def test_build_app_urls_slug_fallback() -> None:
    """Projects without a slug field fall back to the repo basename."""
    manifest = {
        "projects": [
            {
                "repo": "samoletovs/rosette",
                "domain": "rosette.naurolabs.com",
                "status": "active",
            }
        ]
    }
    urls = health_scan._build_app_urls(manifest)
    assert urls == {"rosette": "https://rosette.naurolabs.com/"}


def test_fetch_workspace_manifest_uses_cache(tmp_path: Path) -> None:
    """When the cache file exists the HTTP endpoint is not contacted."""
    cache_file = tmp_path / "manifest.json"
    cache_file.write_text(json.dumps(_SAMPLE_MANIFEST), encoding="utf-8")

    with patch("agent.health_scan.httpx.get") as mock_get:
        result = health_scan.fetch_workspace_manifest(cache_path=cache_file)

    mock_get.assert_not_called()
    assert result == _SAMPLE_MANIFEST


def test_fetch_workspace_manifest_fetches_and_caches(tmp_path: Path) -> None:
    """On a cache miss the manifest is downloaded and written to disk."""
    cache_file = tmp_path / "manifest.json"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _SAMPLE_MANIFEST

    with patch("agent.health_scan.httpx.get", return_value=mock_resp) as mock_get:
        result = health_scan.fetch_workspace_manifest(
            url="https://example.com/manifest.json", cache_path=cache_file
        )

    mock_get.assert_called_once_with(
        "https://example.com/manifest.json", timeout=15, follow_redirects=True
    )
    assert result == _SAMPLE_MANIFEST
    assert cache_file.exists()
    assert json.loads(cache_file.read_text()) == _SAMPLE_MANIFEST


def test_fetch_workspace_manifest_raises_on_non_200(tmp_path: Path) -> None:
    """A non-200 response raises RuntimeError so health-scan fails loudly."""
    cache_file = tmp_path / "manifest.json"

    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("agent.health_scan.httpx.get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="HTTP 403"):
            health_scan.fetch_workspace_manifest(
                url="https://example.com/manifest.json", cache_path=cache_file
            )


# ── Shipped vs not-shipped classification ───────────────────────────────────────
# Added 2026-08-22. `_improvement_status` returned "completed" for every closed
# issue that merely lacked a `declined` label, so GitHub's `not_planned` close -
# the won't-fix/duplicate state - was reported as delivered work.
#
# Measured on real data: in a five-issue sample from era-legacy, #54
# (not_planned, labelled `duplicate`) and #52 (not_planned, labelled `rejected`)
# were both counted as shipped. The lab judges its own self-improvement loop by
# this number, and a loop cannot be improved against an inflated success metric.
#
# These pin the REQUIREMENT - "only work that was actually built counts as
# shipped" - rather than the shape of the implementation.


def _issue(state="closed", reason=None, labels=(), assignees=()):
    return {
        "state": state,
        "state_reason": reason,
        "labels": [{"name": n} for n in labels],
        "assignees": [{"login": a} for a in assignees],
    }


def test_not_planned_is_not_shipped() -> None:
    """The exact shape of era-legacy#54: closed not_planned, no `declined` label."""
    assert health_scan._improvement_status(
        _issue(reason="not_planned", labels=["idea", "duplicate", "approved"])
    ) == "declined"


def test_not_planned_without_any_label_is_still_not_shipped() -> None:
    # The close reason alone must be enough. Relying on a label means anyone who
    # closes an issue the ordinary way silently inflates the shipped count.
    assert health_scan._improvement_status(_issue(reason="not_planned")) == "declined"


def test_completed_close_is_shipped() -> None:
    assert health_scan._improvement_status(
        _issue(reason="completed", labels=["idea", "approved"])
    ) == "completed"


def test_missing_state_reason_is_treated_as_completed() -> None:
    """Older issues predate state_reason. GitHub back-fills them as completed, so
    inventing a third state here would rewrite history rather than measure it."""
    assert health_scan._improvement_status(_issue(reason=None, labels=["idea"])) == "completed"


@pytest.mark.parametrize("label", ["declined", "duplicate", "rejected", "wontfix", "invalid"])
def test_not_shipped_labels_override_a_completed_reason(label) -> None:
    """A closer who used the label but left the default reason is still saying it
    was not built. era-legacy#52 was labelled `rejected`; #54 `duplicate`."""
    assert health_scan._improvement_status(
        _issue(reason="completed", labels=["idea", label])
    ) == "declined"


def test_label_casing_is_ignored() -> None:
    assert health_scan._improvement_status(
        _issue(reason="completed", labels=["idea", "Duplicate"])
    ) == "declined"


def test_open_issue_states_are_unchanged() -> None:
    # The fix must not disturb the three open states.
    assert health_scan._improvement_status(_issue(state="open", labels=["needs-approval"])) == "awaiting approval"
    assert health_scan._improvement_status(_issue(state="open", labels=["approved"])) == "in progress"
    assert health_scan._improvement_status(_issue(state="open", assignees=["copilot-swe-agent"])) == "in progress"
    assert health_scan._improvement_status(_issue(state="open", labels=["idea"])) == "proposed"
