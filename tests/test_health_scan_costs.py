"""Preserve master's cost-query wire contracts in the billing-cycle REST reader."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent import azure_costs, health_scan

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime.datetime(2026, 9, 9, 6, 30, tzinfo=datetime.UTC)
PERIOD_START = datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC)


class CostTransport(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self.handle)
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.payload: dict[str, Any] = {
            "properties": {
                "columns": [
                    {"name": "Cost", "type": "Number"},
                    {"name": "ResourceGroupName", "type": "String"},
                    {"name": "Currency", "type": "String"},
                    {"name": "UsageDate", "type": "Number"},
                ],
                "rows": [
                    [12.34, "rg-one", "EUR", 20260821],
                    [7.66, "rg-two", "EUR", 20260908],
                    [0, "rg-empty", "EUR", 20260908],
                ],
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "management.azure.com"
        if request.url.path.endswith("/billingPeriods"):
            assert request.method == "GET"
            return httpx.Response(200, json={"value": [{"properties": {
                "billingPeriodStartDate": "2026-08-21",
                "billingPeriodEndDate": "2026-09-20",
            }}]})
        if request.url.path.endswith(f"/budgets/{azure_costs.DEFAULT_BUDGET_NAME}"):
            assert request.method == "GET"
            return httpx.Response(200, json={
                "name": azure_costs.DEFAULT_BUDGET_NAME,
                "properties": {
                    "category": "Cost", "timeGrain": "BillingMonth", "amount": 100,
                    "timePeriod": {"startDate": "2026-09-01", "endDate": "2027-08-31"},
                    "currentSpend": {"amount": 0, "unit": "EUR"},
                },
            })
        assert request.url.path.endswith("/providers/Microsoft.CostManagement/query")
        assert request.method == "POST"
        return httpx.Response(self.status, json=self.payload)

    @property
    def queries(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]


@pytest.fixture
def cost_transport(monkeypatch: pytest.MonkeyPatch) -> CostTransport:
    transport = CostTransport()
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUBSCRIPTION_ID)
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value.token = "stub"
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: credential)

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(azure_costs.dt, "datetime", FrozenDatetime)

    class CostClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(azure_costs.httpx, "Client", CostClient)
    return transport


def test_cost_requests_identify_application_on_wire(cost_transport: CostTransport) -> None:
    assert health_scan.scan_azure_costs()["total"] == 20.0

    assert len(cost_transport.requests) == 3
    assert all(
        request.headers["ClientType"] == "samoletovs-autorefine"
        for request in cost_transport.requests
    )


def test_custom_billing_date_range_reaches_api(cost_transport: CostTransport) -> None:
    assert health_scan.scan_azure_costs()["total"] == 20.0

    assert len(cost_transport.queries) == 1
    request = cost_transport.queries[0]
    assert request.method == "POST"
    assert request.url.path == (
        f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.CostManagement/query"
    )
    body = json.loads(request.content)
    assert body["type"] == "ActualCost"
    assert body["timeframe"] == "Custom"
    assert "time_period" not in body
    period = body["timePeriod"]
    assert datetime.datetime.fromisoformat(period["from"]) == PERIOD_START
    assert datetime.datetime.fromisoformat(period["to"]) == NOW.replace(hour=23, minute=59, second=59)
    assert body["dataset"] == {
        "granularity": "Daily",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        "grouping": [{"type": "Dimension", "name": "ResourceGroupName"}],
    }


def test_cost_results_keep_failure_sentinel_and_use_reported_eur_cycle(
    cost_transport: CostTransport,
) -> None:
    assert health_scan.scan_azure_costs() == {
        "total": 20.0,
        "currency": "EUR",
        "projected": 31.0,
        "projection_method": "linear_elapsed_cycle_days",
        "budget": 100.0,
        "budget_name": azure_costs.DEFAULT_BUDGET_NAME,
        "budget_currency": "EUR",
        "budget_time_grain": "BillingMonth",
        "remaining": 80.0,
        "remaining_budget": 80.0,
        "period_start": "2026-08-21",
        "period_end": "2026-09-20",
        "next_reset": "2026-09-21",
        "query_end": "2026-09-09",
        "latest_usage_date": "2026-09-08",
        "by_resource_group": {"rg-one": 12.34, "rg-two": 7.66, "rg-empty": 0.0},
        "days_elapsed": 20,
        "days_in_period": 31,
    }


def test_empty_cost_rows_do_not_invent_zero_spend_or_currency(
    cost_transport: CostTransport,
) -> None:
    cost_transport.payload["properties"]["rows"] = []

    result = health_scan.scan_azure_costs()

    assert result["total"] == -1
    assert "empty/mixed results unavailable" in result["error"]
    assert "remaining_budget" not in result


def test_explicit_zero_cost_with_reported_currency_is_valid(cost_transport: CostTransport) -> None:
    cost_transport.payload["properties"]["rows"] = [[0, "rg-empty", "EUR", 20260908]]

    result = health_scan.scan_azure_costs()

    assert result["total"] == 0.0
    assert result["currency"] == "EUR"
    assert result["remaining_budget"] == 100.0
    assert result["by_resource_group"] == {"rg-empty": 0.0}
    assert "error" not in result


@pytest.mark.parametrize("status", [400, 403, 429])
def test_failed_cost_query_stays_unavailable(
    cost_transport: CostTransport, status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    cost_transport.status = status
    cost_transport.payload = {"error": {"code": str(status), "message": "Cost query unavailable"}}

    result = health_scan.scan_azure_costs()

    assert result["total"] == -1
    assert str(status) in result["error"]
    assert "Azure cost scan unavailable" in caplog.text
    assert len(cost_transport.queries) == 1
    assert "remaining_budget" not in result


@pytest.mark.parametrize("status", [200, 429])
def test_dashboard_uses_same_billing_scanner_and_preserves_unavailable_results(
    cost_transport: CostTransport, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, status: int,
) -> None:
    from agent.main import run_dashboard_mode

    monkeypatch.setenv("GH_TOKEN", "stub")
    cost_transport.status = status
    output = tmp_path / "dashboard.html"
    with (
        patch.object(health_scan, "scan_github", return_value={}),
        patch.object(health_scan, "scan_app_insights", return_value={}),
        patch.object(health_scan, "check_deployed_urls", return_value={}),
        patch.object(health_scan, "analyze_with_ai", return_value={}) as analysis,
    ):
        run_dashboard_mode(["demo"], output=str(output))

    cost_data = analysis.call_args.args[1]
    html = output.read_text(encoding="utf-8")
    if status == 200:
        assert cost_data["total"] == 20.0
        assert cost_data["currency"] == "EUR"
        assert "EUR 20.00" in html
        assert "2026-08-21 through 2026-09-20" in html
        assert "Remaining budget" in html
    else:
        assert cost_data["total"] == -1
        assert "429" in cost_data["error"]
        assert "Cost scan unavailable" in html
        assert 'class="cost-green"' not in html
    assert len(cost_transport.queries) == 1
    assert cost_transport.queries[0].headers["ClientType"] == "samoletovs-autorefine"


def test_missing_subscription_makes_no_request(
    cost_transport: CostTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID")

    assert health_scan.scan_azure_costs() == {
        "error": "AZURE_SUBSCRIPTION_ID not set", "total": -1,
    }
    assert cost_transport.requests == []
