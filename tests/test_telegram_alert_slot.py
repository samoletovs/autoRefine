"""Regression tests: the Telegram alert slot must contain alerts, nothing else.

``build_telegram_summary`` used to recover alerts by scraping the *rendered*
report for lines starting with ``"- "``:

    alerts = [line for line in lines if line.startswith("- ")
              and "🚨" not in line][:3]

The Azure cost section emits bullets too, so those bullets qualified. The
summary was reconstructing structure from a rendering of that same structure,
which is why anything shaped like a bullet could take an alert's place.

Measured against the real ``generate_report`` before the fix, the leak was
proportional to how few real alerts there were, because the Alerts section
renders above Azure Costs and fills the ``[:3]`` slice first:

    0 real alerts -> 3 of 3 slots were cost figures
    1 real alert  -> 2 of 3 slots were cost figures
    3+ real alerts-> no leak

So the worst case is a genuinely quiet week, which was reported to Telegram as
three bullet lines that read exactly like findings. These tests cover every
band, including 4+ alerts with a cost section present.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import health_scan

GITHUB_DATA: dict[str, Any] = {
    "era": {"open_issues": 2, "ci_status": "failure", "bug_count": 1,
            "open_prs": 0, "commits_7d": 4},
}
COST_DATA: dict[str, Any] = {
    "total": 5, "projected": 12, "budget": 150, "remaining": 145,
    "by_resource_group": {"rg-era": 5},
}
ALERTS = [
    "era: error rate up 4x",
    "atlas: deploy failing",
    "golazo: 3 critical CVEs",
    "folio: build red",
]
# The exact strings the cost section renders as bullets.
COST_BULLET_MARKERS = ("Month-to-date", "Projected:", "Budget:", "Remaining")


def _analysis(alerts: list[str]) -> dict[str, Any]:
    return {
        "health_scores": {"era": {"R": 3, "L": 4, "M": 2, "health": 9}},
        "alerts": alerts,
        "recommendations": ["Fix CI"],
        "focus_project": "era",
        "issues_to_create": [],
    }


@pytest.mark.parametrize("n_alerts", [0, 1, 2, 3, 4])
def test_cost_figures_never_appear_as_alerts(n_alerts: int) -> None:
    """The core regression, across every band of the old ``[:3]`` slice."""
    msg = health_scan.build_telegram_summary(
        _analysis(ALERTS[:n_alerts]), "reports/run/r.md", [], cost_data=COST_DATA
    )

    for marker in COST_BULLET_MARKERS:
        assert marker not in msg, f"cost bullet {marker!r} leaked into the summary"


def test_quiet_week_is_not_reported_as_three_findings() -> None:
    """0 alerts was the worst case: every slot was a cost figure."""
    msg = health_scan.build_telegram_summary(
        _analysis([]), "reports/run/r.md", [], cost_data=COST_DATA
    )

    assert "✅ No alerts" in msg
    assert "🚨" not in msg
    for marker in COST_BULLET_MARKERS:
        assert marker not in msg


def test_four_alerts_with_a_cost_section_shows_alerts_only() -> None:
    """The case the old implementation was wrongly assumed safe on.

    With 4 real alerts the slice does fill from the Alerts section, so nothing
    leaked — but the summary silently dropped the 4th with no indication. Both
    properties are asserted: real alerts present, cost bullets absent, and the
    overflow disclosed rather than hidden.
    """
    msg = health_scan.build_telegram_summary(
        _analysis(ALERTS), "reports/run/r.md", [], cost_data=COST_DATA
    )

    for alert in ALERTS[:3]:
        assert f"🚨 {alert}" in msg
    assert "and 1 more" in msg
    for marker in COST_BULLET_MARKERS:
        assert marker not in msg


def test_single_alert_reaches_the_summary_intact() -> None:
    """1 alert used to be padded out with 2 cost figures beside it."""
    msg = health_scan.build_telegram_summary(
        _analysis(["era: error rate up 4x"]), None, [], cost_data=COST_DATA
    )

    assert "🚨 era: error rate up 4x" in msg
    assert msg.count("🚨") == 2  # the count header plus the one alert
    for marker in COST_BULLET_MARKERS:
        assert marker not in msg


def test_cost_line_is_still_present_and_labelled() -> None:
    """Cost figures stay in the summary — they just aren't alerts."""
    msg = health_scan.build_telegram_summary(
        _analysis(ALERTS[:2]), None, [], cost_data=COST_DATA
    )

    assert "Azure: $5 used" in msg
    assert "/ $150 budget" in msg
    cost_line = next(line for line in msg.split("\n") if "Azure" in line)
    assert "🚨" not in cost_line


def test_alert_containing_the_alarm_emoji_is_not_dropped() -> None:
    """The old ``"🚨" not in line`` filter could discard a real alert.

    It existed to skip the ``## 🚨 Alerts`` heading, but the heading never
    started with ``"- "`` so the clause only ever misfired — on an alert whose
    own text carried the emoji.
    """
    msg = health_scan.build_telegram_summary(
        _analysis(["🚨 era: total outage"]), None, [], cost_data=COST_DATA
    )

    assert "era: total outage" in msg


def test_summary_does_not_read_the_rendered_report() -> None:
    """The report text is no longer an input, so it cannot leak into alerts.

    This is the property that makes the bug unreintroducible: there is nothing
    to scrape. Asserted via the signature rather than by string matching.
    """
    import inspect

    params = list(inspect.signature(health_scan.build_telegram_summary).parameters)

    assert "report" not in params
    assert params[0] == "analysis"


def test_alerts_survive_the_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: an alert the model raised reaches the Telegram message."""
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    analysis = _analysis(ALERTS[:2])
    sent: list[str] = []

    from unittest.mock import patch

    with (
        patch("agent.health_scan.scan_github", return_value=GITHUB_DATA),
        patch("agent.health_scan.scan_azure_costs", return_value=COST_DATA),
        patch("agent.health_scan.scan_app_insights", return_value={}),
        patch("agent.health_scan.check_deployed_urls", return_value={}),
        patch("agent.health_scan.analyze_with_ai", return_value=analysis),
        patch("agent.health_scan.commit_report", return_value="reports/run/r.md"),
        patch("agent.health_scan.enforce_report_retention"),
        patch("agent.health_scan.create_github_issues", return_value=[]),
        patch("agent.notify.send_telegram", side_effect=lambda m, **_: sent.append(m)),
    ):
        result = health_scan.run_health_scan(["era"])

    assert len(sent) == 1
    for alert in ALERTS[:2]:
        assert f"🚨 {alert}" in sent[0]
    for marker in COST_BULLET_MARKERS:
        assert marker not in sent[0]
    assert result["telegram_summary"] == sent[0]
