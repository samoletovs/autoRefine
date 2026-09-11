"""The monthly credit is USD 150, not the independent EUR alert budget."""

from __future__ import annotations

import re

import pytest

from agent import azure_costs, health_scan
from agent.dashboard import _render_cost_section


@pytest.fixture
def costs() -> dict:
    return {
        "total": 45.79, "currency": "EUR", "budget": 100,
        "budget_currency": "EUR", "budget_name": "naurolabs-credit-cycle-eur-100",
        "budget_time_grain": "BillingMonth", "remaining_budget": 54.21,
        "projected": 64.53, "projection_method": "linear_elapsed_cycle_days",
        "period_start": "2026-08-21", "period_end": "2026-09-20",
        "next_reset": "2026-09-21", "query_end": "2026-09-11",
        "latest_usage_date": "2026-09-10",
        "total_usd": 40.24, "projected_usd": 56.69,
        "monthly_credit_usd": 150, "estimated_credit_remaining_usd": 109.76,
        "cost_usd_source": "CostUSD",
    }


def test_phone_summary_uses_real_usd_cost_not_relabelled_eur(costs: dict) -> None:
    message = health_scan.build_telegram_summary({}, "reports/run/r.md", [], costs)
    assert "USD 40.24" in message
    assert "USD 150.00" in message
    assert "USD 109.76" in message
    assert "USD 56.69" in message
    assert "USD 45.79" not in message
    assert "EUR 100.00" not in message
    assert "estimate" in message.lower()
    assert "not a credit balance" in message
    assert "2026-08-21" in message and "2026-09-20" in message
    assert "2026-09-21" in message and "2026-09-10" in message
    assert "\n\n<b>Azure" in message
    assert "\n\n✅ No alerts" in message
    assert "naurolabs-credit-cycle-eur-100" not in message
    plain = re.sub("<[^>]+>", "", message)
    assert len(plain) < 750
    assert len([line for line in plain.splitlines() if line.strip()]) <= 12


def test_full_reports_preserve_native_budget_and_usd_credit_sources(costs: dict) -> None:
    for report in (
        health_scan.generate_report({}, costs, {}),
        _render_cost_section(costs),
    ):
        for value in ("EUR 45.79", "EUR 100.00", "USD 40.24", "USD 150.00", "CostUSD"):
            assert value in report
        assert "configured" in report
        assert "not a credit balance" in report


@pytest.mark.parametrize("usd", [None, True, float("nan"), float("inf"), -1])
def test_invalid_usd_is_never_replaced_with_eur_or_a_green_credit_verdict(
    costs: dict, usd: object,
) -> None:
    costs["total_usd"] = usd
    message = health_scan.build_telegram_summary({}, None, [], costs)
    assert "USD 45.79" not in message
    assert "USD credit estimate unavailable" in message
    assert azure_costs.credit_status(costs)[0] == "muted"


def test_missing_usd_source_is_not_treated_as_verified(costs: dict) -> None:
    costs.pop("cost_usd_source")
    assert azure_costs.credit_status(costs)[0] == "muted"


def test_usd_allowance_and_eur_alert_budget_are_independent(costs: dict) -> None:
    costs.update(total_usd=151, projected_usd=160)
    alerts = health_scan.ground_analysis({}, {}, costs, {}, {})["alerts"]
    assert any("USD 151.00" in alert and "USD 150.00" in alert for alert in alerts)
    assert not any("EUR" in alert for alert in alerts)
    message = health_scan.build_telegram_summary({}, None, [], costs)
    assert "OVER MONTHLY CREDIT" in message


def test_legacy_currency_mismatch_cannot_create_a_numeric_budget_alert(costs: dict) -> None:
    costs.update(total=200, budget_currency="USD")
    assert health_scan.ground_analysis({}, {}, costs, {}, {})["alerts"] == []


def test_failed_analysis_still_delivers_measured_alert_and_formatted_usd(costs: dict) -> None:
    message = health_scan.build_telegram_summary(
        {"error": "AI unavailable", "alerts": ["era: persistent HTTP 503"]}, None, [], costs,
    )
    assert "era: persistent HTTP 503" in message
    assert "USD 40.24" in message
    assert "No alerts" not in message
