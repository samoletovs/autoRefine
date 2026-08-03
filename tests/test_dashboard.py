"""Tests for agent.dashboard — HTML dashboard rendering."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent.dashboard import (
    _health_colour,
    _render_cost_section,
    _render_feature_suggestions,
    _render_project_table,
    _render_url_health,
    render_html_dashboard,
)


# ── _health_colour ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "score,expected_class",
    [
        (15, "score-green"),
        (10, "score-green"),
        (9, "score-yellow"),
        (6, "score-yellow"),
        (5, "score-red"),
        (0, "score-red"),
        (None, "score-unknown"),
    ],
)
def test_health_colour(score: int | None, expected_class: str) -> None:
    assert _health_colour(score) == expected_class


# ── _render_project_table ──────────────────────────────────────────────────


def test_render_project_table_empty() -> None:
    result = _render_project_table({}, {})
    assert "No project data" in result


def test_render_project_table_includes_repo_and_scores() -> None:
    github_data: dict[str, Any] = {
        "era": {
            "open_issues": 3,
            "bug_count": 1,
            "open_prs": 0,
            "commits_7d": 5,
            "ci_status": "success",
        }
    }
    scores: dict[str, Any] = {"era": {"R": 4, "L": 4, "M": 5, "health": 13}}
    html = _render_project_table(github_data, scores)
    assert "era" in html
    assert "✅" in html  # CI success badge
    assert "score-green" in html  # health=13 is green
    assert ">13<" in html


def test_render_project_table_yellow_score() -> None:
    github_data: dict[str, Any] = {"proj": {"ci_status": "none"}}
    scores: dict[str, Any] = {"proj": {"R": 2, "L": 2, "M": 2, "health": 6}}
    html = _render_project_table(github_data, scores)
    assert "score-yellow" in html


def test_render_project_table_red_score() -> None:
    github_data: dict[str, Any] = {"proj": {"ci_status": "failure"}}
    scores: dict[str, Any] = {"proj": {"R": 1, "L": 1, "M": 1, "health": 3}}
    html = _render_project_table(github_data, scores)
    assert "score-red" in html
    assert "❌" in html  # CI failure badge


def test_render_project_table_missing_score() -> None:
    """Repos without scores should still render without error."""
    github_data: dict[str, Any] = {"unknown-repo": {"ci_status": "none"}}
    html = _render_project_table(github_data, {})
    assert "unknown-repo" in html
    assert "score-unknown" in html


# ── _render_cost_section ───────────────────────────────────────────────────


def test_render_cost_section_on_track() -> None:
    cost_data: dict[str, Any] = {
        "total": 30.0,
        "projected": 60.0,
        "budget": 150.0,
        "remaining": 120.0,
        "by_resource_group": {"rg-era": 30.0},
    }
    html = _render_cost_section(cost_data)
    assert "cost-green" in html
    assert "rg-era" in html
    assert "30.0" in html


def test_render_cost_section_yellow_warning() -> None:
    cost_data: dict[str, Any] = {
        "total": 110.0,
        "projected": 140.0,
        "budget": 150.0,
        "remaining": 40.0,
    }
    html = _render_cost_section(cost_data)
    assert "cost-yellow" in html
    assert "🟡" in html


def test_render_cost_section_over_budget() -> None:
    cost_data: dict[str, Any] = {
        "total": 130.0,
        "projected": 160.0,  # > budget of 150
        "budget": 150.0,
        "remaining": 20.0,
    }
    html = _render_cost_section(cost_data)
    assert "cost-red" in html
    assert "🔴" in html
    assert "OVER BUDGET" in html


def test_render_cost_section_unavailable() -> None:
    html = _render_cost_section({"error": "no creds", "total": -1})
    assert "unavailable" in html.lower()
    assert "no creds" in html


# ── _render_url_health ─────────────────────────────────────────────────────


def test_render_url_health_empty() -> None:
    assert _render_url_health({}) == ""


def test_render_url_health_ok() -> None:
    data: dict[str, Any] = {
        "era": {"ok": True, "status": 200, "response_ms": 120, "size_kb": 14}
    }
    html = _render_url_health(data)
    assert "era" in html
    assert "✅" in html
    assert "120" in html


def test_render_url_health_failed() -> None:
    data: dict[str, Any] = {"broken": {"ok": False, "status": 503, "response_ms": 0, "size_kb": 0}}
    html = _render_url_health(data)
    assert "❌" in html
    assert "503" in html


# ── render_html_dashboard ──────────────────────────────────────────────────


_GITHUB_DATA: dict[str, Any] = {
    "era": {
        "open_issues": 2,
        "bug_count": 0,
        "open_prs": 1,
        "commits_7d": 3,
        "ci_status": "success",
    }
}
_COST_DATA: dict[str, Any] = {
    "total": 20.0,
    "projected": 40.0,
    "budget": 150.0,
    "remaining": 130.0,
    "by_resource_group": {},
}
_ANALYSIS: dict[str, Any] = {
    "health_scores": {"era": {"R": 4, "L": 3, "M": 5, "health": 12}},
    "alerts": ["CI failed on golazo"],
    "recommendations": ["Add tests to era", "Monitor costs"],
    "focus_project": "era — high momentum",
    "issues_to_create": [{"repo": "era", "title": "Fix slow API"}],
}


def test_render_html_dashboard_is_valid_html() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert html.startswith("<!DOCTYPE html>")
    assert "<html" in html
    assert "</html>" in html


def test_render_html_dashboard_includes_project_data() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "era" in html
    assert "score-green" in html  # health=12


def test_render_html_dashboard_includes_focus() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "era — high momentum" in html


def test_render_html_dashboard_includes_alerts() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "CI failed on golazo" in html


def test_render_html_dashboard_includes_recommendations() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "Add tests to era" in html
    assert "Monitor costs" in html


def test_render_html_dashboard_includes_cost() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "20.0" in html
    assert "cost-green" in html


def test_render_html_dashboard_includes_issues() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "Fix slow API" in html


def test_render_html_dashboard_includes_url_health_section() -> None:
    url_data: dict[str, Any] = {"era": {"ok": True, "status": 200, "response_ms": 80, "size_kb": 5}}
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, url_health_data=url_data)
    assert "Deployed Apps" in html
    assert "era" in html


def test_render_html_dashboard_no_url_health_omits_section() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, url_health_data=None)
    assert "Deployed Apps" not in html


def test_render_html_dashboard_telemetry_with_issues() -> None:
    app_insights: dict[str, Any] = {
        "era": {
            "exception_count": 5,
            "failed_request_count": 2,
            "page_views_24h": 100,
        }
    }
    html = render_html_dashboard(
        _GITHUB_DATA, _COST_DATA, _ANALYSIS, app_insights_data=app_insights
    )
    assert "App Telemetry" in html
    assert "exception" in html.lower()


def test_render_html_dashboard_telemetry_clean_omits_section() -> None:
    app_insights: dict[str, Any] = {
        "era": {"exception_count": 0, "failed_request_count": 0}
    }
    html = render_html_dashboard(
        _GITHUB_DATA, _COST_DATA, _ANALYSIS, app_insights_data=app_insights
    )
    assert "App Telemetry" not in html


def test_render_html_dashboard_xss_escaped() -> None:
    """Values with HTML special characters must be escaped."""
    github_data: dict[str, Any] = {
        "<script>alert(1)</script>": {"ci_status": "none"}
    }
    html = render_html_dashboard(github_data, _COST_DATA, {})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_dashboard_empty_data() -> None:
    """render_html_dashboard must not crash with minimal/empty data."""
    html = render_html_dashboard({}, {"total": -1, "error": "none"}, {})
    assert "<!DOCTYPE html>" in html
    assert "No project data" in html


# ── _render_feature_suggestions ────────────────────────────────────────────


def test_render_feature_suggestions_empty() -> None:
    html = _render_feature_suggestions([])
    assert "No improvement suggestions" in html


def test_render_feature_suggestions_dict_items() -> None:
    suggestions: list[Any] = [
        {
            "title": "Goal-aligned capability: ship daily",
            "description": "Implement shipping automation.",
            "priority": "P1",
            "category": "feature",
        },
        {
            "title": "Feature parity review with Competitor X",
            "description": "Compare and fill gaps.",
            "priority": "P0",
            "category": "feature-parity",
        },
    ]
    html = _render_feature_suggestions(suggestions)
    assert "Goal-aligned capability" in html
    assert "Feature parity review" in html
    assert "priority-p1" in html
    assert "priority-p0" in html
    assert "feature-parity" in html
    assert "Implement shipping automation" in html


def test_render_feature_suggestions_string_items() -> None:
    """Plain string suggestions are also handled gracefully."""
    html = _render_feature_suggestions(["Add dark mode", "Improve onboarding"])
    assert "Add dark mode" in html
    assert "Improve onboarding" in html


def test_render_feature_suggestions_xss_escaped() -> None:
    suggestions: list[Any] = [{"title": "<script>evil()</script>", "description": ""}]
    html = _render_feature_suggestions(suggestions)
    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;" in html


# ── interactive features ───────────────────────────────────────────────────


def test_render_html_dashboard_auto_refresh_present_by_default() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "setTimeout" in html
    assert "location.reload()" in html
    # default is 300 s
    assert "300000" in html


def test_render_html_dashboard_auto_refresh_custom_interval() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, refresh_seconds=60)
    assert "60000" in html


def test_render_html_dashboard_auto_refresh_disabled() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, refresh_seconds=0)
    assert "setTimeout" not in html
    assert "location.reload()" not in html


def test_render_html_dashboard_sortable_table_script() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "sortTable" in html
    assert "data-sort" in html


def test_render_html_dashboard_includes_improvement_suggestions_section() -> None:
    analysis_with_suggestions: dict[str, Any] = {
        **_ANALYSIS,
        "feature_suggestions": [
            {
                "title": "Goal-aligned capability: launch MVP",
                "description": "Build the core feature set.",
                "priority": "P0",
                "category": "feature",
            }
        ],
    }
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, analysis_with_suggestions)
    assert "Improvement Suggestions" in html
    assert "Goal-aligned capability" in html
    assert "priority-p0" in html


def test_render_html_dashboard_improvement_suggestions_empty_by_default() -> None:
    """When analysis has no feature_suggestions the section still renders."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "Improvement Suggestions" in html
    assert "No improvement suggestions" in html


def test_render_html_dashboard_meta_shows_refresh_interval() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, refresh_seconds=120)
    assert "auto-refresh every 120s" in html


def test_render_html_dashboard_no_refresh_meta_when_disabled() -> None:
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS, refresh_seconds=0)
    assert "auto-refresh" not in html


# ── customizable views ─────────────────────────────────────────────────────


def test_render_html_dashboard_filter_control_present() -> None:
    """A project-filter input must appear in the project-health section."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert 'id="project-filter"' in html
    assert 'type="search"' in html
    assert "Filter projects" in html


def test_render_html_dashboard_filter_script_present() -> None:
    """The live-filter JS must be injected into the page."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "project-filter" in html
    assert "project-health-table" in html
    # row-visibility toggle
    assert "style.display" in html


def test_render_html_dashboard_project_table_has_class() -> None:
    """The project health table must carry the 'project-health-table' CSS class."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert 'class="project-health-table"' in html


def test_render_html_dashboard_collapse_script_present() -> None:
    """The collapsible-sections JS must be injected into the page."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "collapse-indicator" in html
    assert "data.collapsed" in html or "dataset.collapsed" in html


def test_render_html_dashboard_collapse_css_present() -> None:
    """CSS for the collapse indicator and filter bar must be present."""
    html = render_html_dashboard(_GITHUB_DATA, _COST_DATA, _ANALYSIS)
    assert "collapse-indicator" in html
    assert "filter-bar" in html
    assert "project-filter" in html


# ── run_dashboard_mode ─────────────────────────────────────────────────────


def test_run_dashboard_mode_writes_html_file(
    tmp_path: "pytest.TempPathFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setenv("GH_TOKEN", "fake-token")

    output_file = str(tmp_path / "out.html")

    github_data: dict[str, Any] = {"era": {"open_issues": 0, "ci_status": "success"}}
    cost_data: dict[str, Any] = {"total": 5, "projected": 20, "budget": 150, "remaining": 145, "by_resource_group": {}}
    analysis: dict[str, Any] = {
        "health_scores": {},
        "alerts": [],
        "recommendations": [],
        "focus_project": "",
        "issues_to_create": [],
    }

    with (
        patch("agent.health_scan.scan_github", return_value=github_data),
        patch("agent.health_scan.scan_azure_costs", return_value=cost_data),
        patch("agent.health_scan.scan_app_insights", return_value={}),
        patch("agent.health_scan.check_deployed_urls", return_value={}),
        patch("agent.health_scan.analyze_with_ai", return_value=analysis),
        patch("builtins.print"),
    ):
        from agent.main import run_dashboard_mode

        run_dashboard_mode(["samoletovs/era"], output=output_file)

    import os

    assert os.path.exists(output_file)
    content = open(output_file, encoding="utf-8").read()
    assert "<!DOCTYPE html>" in content
    assert "NauroLabs" in content
