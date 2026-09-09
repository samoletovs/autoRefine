"""Exercise cost queries through the real SDK without making network calls."""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any, Self

import pytest
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from azure.mgmt.costmanagement import CostManagementClient

from agent import health_scan

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000001"
NOW = datetime.datetime(2026, 9, 9, 6, 30, tzinfo=datetime.UTC)


class StubCredential:
    def get_token(self, *_scopes: str, **_kwargs: Any) -> AccessToken:
        return AccessToken("stub", 9_999_999_999)


class CostResponse(HttpResponse):
    def __init__(
        self, request: HttpRequest, status: int, payload: dict[str, Any],
    ) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.content_type = "application/json"
        self.reason = "OK" if status == 200 else "Cost query failed"
        self.payload = payload

    def body(self) -> bytes:
        return json.dumps(self.payload).encode()

    def json(self) -> dict[str, Any]:
        return self.payload


class CostTransport(HttpTransport):
    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.status = 200
        self.payload: dict[str, Any] = {
            "properties": {
                "columns": [
                    {"name": "Cost", "type": "Number"},
                    {"name": "ResourceGroupName", "type": "String"},
                    {"name": "Currency", "type": "String"},
                ],
                "rows": [
                    [12.34, "rg-one", "USD"],
                    [7.66, "rg-two", "USD"],
                    [None, "rg-empty", "USD"],
                ],
            },
        }

    def send(self, request: HttpRequest, **_kwargs: Any) -> HttpResponse:
        self.requests.append(request)
        return CostResponse(request, self.status, self.payload)

    def open(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None: ...


@pytest.fixture
def cost_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[CostTransport]:
    transport = CostTransport()
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUBSCRIPTION_ID)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", StubCredential)

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(health_scan.datetime, "datetime", FrozenDatetime)

    with ExitStack() as stack:
        def client_factory(credential: TokenCredential, **kwargs: Any) -> CostManagementClient:
            return stack.enter_context(CostManagementClient(
                credential, transport=transport, retry_total=0, **kwargs,
            ))

        monkeypatch.setattr("azure.mgmt.costmanagement.CostManagementClient", client_factory)
        yield transport


def test_cost_query_identifies_application_on_wire(cost_transport: CostTransport) -> None:
    assert health_scan.scan_azure_costs()["total"] == 20.0

    assert len(cost_transport.requests) == 1
    assert cost_transport.requests[0].headers["ClientType"] == "samoletovs-autorefine"


def test_custom_date_range_reaches_api(cost_transport: CostTransport) -> None:
    assert health_scan.scan_azure_costs()["total"] == 20.0

    request = cost_transport.requests[0]
    assert request.method == "POST"
    assert f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.CostManagement/query" in request.url
    body = json.loads(request.body)
    assert body["type"] == "ActualCost"
    assert body["timeframe"] == "Custom"
    assert "time_period" not in body
    period = body["timePeriod"]
    assert datetime.datetime.fromisoformat(period["from"]) == NOW.replace(day=1, hour=0, minute=0)
    assert datetime.datetime.fromisoformat(period["to"]) == NOW.replace(hour=23, minute=59, second=59)
    assert body["dataset"] == {
        "granularity": "None",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        "grouping": [{"type": "Dimension", "name": "ResourceGroupName"}],
    }


def test_cost_results_keep_existing_contract(cost_transport: CostTransport) -> None:
    assert health_scan.scan_azure_costs() == {
        "total": 20.0,
        "projected": 75.0,
        "budget": health_scan.AZURE_BUDGET_MONTHLY,
        "remaining": health_scan.AZURE_BUDGET_MONTHLY - 20.0,
        "by_resource_group": {"rg-one": 12.34, "rg-two": 7.66, "rg-empty": 0.0},
        "days_elapsed": 8,
    }


def test_empty_cost_rows_are_a_real_zero(cost_transport: CostTransport) -> None:
    cost_transport.payload["properties"]["rows"] = []

    result = health_scan.scan_azure_costs()

    assert result["total"] == 0.0
    assert result["by_resource_group"] == {}
    assert "error" not in result


@pytest.mark.parametrize("status", [400, 403, 429])
def test_failed_cost_query_stays_unavailable(
    cost_transport: CostTransport, status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    cost_transport.status = status
    cost_transport.payload = {"error": {"code": str(status), "message": "Cost query unavailable"}}

    result = health_scan.scan_azure_costs()

    assert result["total"] == -1
    assert "Cost query unavailable" in result["error"]
    assert "Azure cost scan failed" in caplog.text
    assert len(cost_transport.requests) == 1


def test_missing_subscription_makes_no_request(
    cost_transport: CostTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID")

    assert health_scan.scan_azure_costs() == {
        "error": "AZURE_SUBSCRIPTION_ID not set", "total": -1,
    }
    assert cost_transport.requests == []
