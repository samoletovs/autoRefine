"""Billing-cycle cost contracts; all Azure and AI traffic is mocked."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent import azure_costs, health_scan
from agent.dashboard import _render_cost_section

_SCOPE = "https://management.azure.com/subscriptions/test-sub"
_PERIODS_URL = f"{_SCOPE}/providers/Microsoft.Billing/billingPeriods?api-version=2018-03-01-preview"
_QUERY_URL = f"{_SCOPE}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"


def _period(start: str, end: str) -> dict[str, Any]:
    return {"properties": {"billingPeriodStartDate": start, "billingPeriodEndDate": end}}


def _query_page(names: list[str], rows: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {"properties": {
        "columns": [{"name": name, "type": "Number" if name == "Cost" else "String"} for name in names],
        "rows": rows,
        **extra,
    }}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[dt.datetime]:
    class Clock(dt.datetime):
        today = dt.datetime(2026, 9, 8, 12, tzinfo=dt.UTC)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return cls.today

    monkeypatch.setattr(azure_costs.dt, "datetime", Clock)
    return Clock


@pytest.fixture
def azure(
    monkeypatch: pytest.MonkeyPatch, clock: type[dt.datetime],
) -> dict[str, Any]:
    """A paginated real-shaped ARM response, not a fake SDK row-order contract."""
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "test-sub")
    budget_url = (
        f"{_SCOPE}/providers/Microsoft.Consumption/budgets/{azure_costs.DEFAULT_BUDGET_NAME}"
        "?api-version=2024-08-01"
    )
    state: dict[str, Any] = {
        "requests": [],
        "responses": {
            _PERIODS_URL: {
                "value": [_period("2026-07-21", "2026-08-20")],
                "nextLink": _PERIODS_URL + "&$skiptoken=next",
            },
            _PERIODS_URL + "&$skiptoken=next": {
                "value": [_period("2026-08-21", "2026-09-20")],
            },
            budget_url: {
                "name": azure_costs.DEFAULT_BUDGET_NAME,
                "properties": {
                    "category": "Cost", "timeGrain": "BillingMonth", "amount": 100,
                    "timePeriod": {"startDate": "2026-08-01T00:00:00Z",
                                   "endDate": "2027-08-31T00:00:00Z"},
                    "currentSpend": {"amount": 38, "unit": "EUR"},
                },
            },
            _QUERY_URL: _query_page(
                ["Currency", "ResourceGroupName", "UsageDate", "Cost", "CostUSD"],
                [["EUR", "rg-demo", 20260821, 24.25, 20]],
                nextLink=_QUERY_URL + "&$skiptoken=next",
            ),
            _QUERY_URL + "&$skiptoken=next": _query_page(
                ["CostUSD", "UsageDate", "Cost", "Currency", "ResourceGroupName"],
                [[11, 20260907, 13.75, "EUR", "rg-demo"]],
            ),
        },
        "budget_url": budget_url,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        response = state["responses"][str(request.url)]
        if isinstance(response, int):
            return httpx.Response(response, json={"error": "unavailable"})
        if isinstance(response, Exception):
            raise response
        if request.method == "POST" and isinstance(response, dict) and "properties" in response:
            metric = json.loads(request.content)["dataset"]["aggregation"]["totalCost"]["name"]
            response = deepcopy(response)
            props = response["properties"]
            if "columns" in props and "rows" in props:
                indices = [
                    i for i, column in enumerate(props["columns"])
                    if column["name"] not in {"Cost", "CostUSD"} or column["name"] == metric
                ]
                props["columns"] = [props["columns"][i] for i in indices]
                props["rows"] = [[row[i] for i in indices if i < len(row)] for row in props["rows"]]
        return httpx.Response(200, json=response)

    class AzureClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(azure_costs.httpx, "Client", AzureClient)
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value.token = "test-token"
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: credential)
    return state


def test_cycle_spanning_months_reads_budget_currency_columns_and_both_pages(
    azure: dict[str, Any],
) -> None:
    result = health_scan.scan_azure_costs()
    assert result == {
        "total": 38.0, "currency": "EUR", "budget": 100.0,
        "budget_name": azure_costs.DEFAULT_BUDGET_NAME,
        "budget_currency": "EUR", "budget_time_grain": "BillingMonth",
        "remaining_budget": 62.0, "remaining": 62.0,
        "period_start": "2026-08-21", "period_end": "2026-09-20", "next_reset": "2026-09-21",
        "query_end": "2026-09-08", "latest_usage_date": "2026-09-07",
        "projected": 62.0, "projection_method": "linear_elapsed_cycle_days",
        "days_elapsed": 19, "days_in_period": 31, "by_resource_group": {"rg-demo": 38.0},
        "total_usd": 31.0, "projected_usd": 50.58, "monthly_credit_usd": 150.0,
        "estimated_credit_remaining_usd": 119.0, "cost_usd_source": "CostUSD",
        "latest_usage_date_usd": "2026-09-07", "by_resource_group_usd": {"rg-demo": 31.0},
    }
    queries = [r for r in azure["requests"] if r.method == "POST"]
    assert len(queries) == 4
    assert all(
        r.headers["ClientType"] == "samoletovs-autorefine" for r in azure["requests"]
    )
    body = json.loads(queries[0].content)
    assert body == json.loads(queries[1].content)
    assert body["timeframe"] == "Custom"
    assert "time_period" not in body
    assert body["timePeriod"] == {
        "from": "2026-08-21T00:00:00Z", "to": "2026-09-08T23:59:59Z",
    }
    assert body["type"] == "ActualCost"
    assert body["dataset"]["granularity"] == "Daily"
    assert body["dataset"]["aggregation"] == {"totalCost": {"name": "Cost", "function": "Sum"}}
    usd_body = json.loads(queries[2].content)
    assert usd_body["dataset"]["aggregation"] == {
        "totalCost": {"name": "CostUSD", "function": "Sum"},
    }
    assert usd_body["timePeriod"] == body["timePeriod"]


def test_budget_lifetime_start_is_not_the_billing_cycle_start(azure: dict[str, Any]) -> None:
    budget = azure["responses"][azure["budget_url"]]["properties"]
    budget["timePeriod"]["startDate"] = "2026-09-01T00:00:00Z"

    result = health_scan.scan_azure_costs()

    assert result["total"] == 38
    assert result["budget"] == 100
    assert result["period_start"] == "2026-08-21"
    assert result["period_end"] == "2026-09-20"
    assert result["next_reset"] == "2026-09-21"
    assert result["days_elapsed"] == 19
    assert result["projected"] == 62
    queries = [r for r in azure["requests"] if r.method == "POST"]
    assert json.loads(queries[0].content)["timePeriod"]["from"] == "2026-08-21T00:00:00Z"


@pytest.mark.parametrize("budget_spend", [0, 999, None])
def test_budget_current_spend_never_supplies_costs_or_freshness(
    azure: dict[str, Any], budget_spend: int | None,
) -> None:
    budget = azure["responses"][azure["budget_url"]]["properties"]
    budget["timePeriod"]["startDate"] = "2026-09-01T00:00:00Z"
    budget["currentSpend"]["amount"] = budget_spend

    result = health_scan.scan_azure_costs()

    assert result["total"] == 38
    assert result["remaining_budget"] == 62
    assert result["projected"] == 62
    assert result["latest_usage_date"] == "2026-09-07"
    assert result["query_end"] == "2026-09-08"
    details = dict(azure_costs.cost_details(result))
    assert details["Latest reported usage"] == "2026-09-07; ingestion freshness unavailable"
    assert len([r for r in azure["requests"] if r.method == "POST"]) == 4


def test_zero_budget_current_spend_never_masks_a_failed_cost_query(azure: dict[str, Any]) -> None:
    azure["responses"][azure["budget_url"]]["properties"]["currentSpend"]["amount"] = 0
    azure["responses"][_QUERY_URL] = 503

    result = health_scan.scan_azure_costs()

    assert result["total"] == -1
    assert result["error"]
    assert "latest_usage_date" not in result
    assert "remaining_budget" not in result


@pytest.mark.parametrize("threshold_type", ["Actual", "Forecasted"])
def test_notification_names_do_not_turn_a_projection_into_a_forecast(
    azure: dict[str, Any], threshold_type: str,
) -> None:
    budget = azure["responses"][azure["budget_url"]]["properties"]
    budget["notifications"] = {
        "forecast_100": {"thresholdType": threshold_type, "threshold": 100},
    }
    result = health_scan.scan_azure_costs()
    assert result["projection_method"] == "linear_elapsed_cycle_days"
    assert result["projected"] == 62
    assert "Cycle-end estimate (linear)" in health_scan.build_telegram_summary({}, None, [], result)


@pytest.mark.parametrize(("day", "elapsed"), [(21, 1), (20, 31)])
def test_inclusive_cycle_first_and_last_day(
    azure: dict[str, Any], clock: Any, day: int, elapsed: int,
) -> None:
    clock.today = dt.datetime(2026, 8 if day == 21 else 9, day, tzinfo=dt.UTC)
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "UsageDate", "ResourceGroupName", "Currency", "CostUSD"],
        [[31, 20260821, "", "EUR", 27]],
    )
    result = health_scan.scan_azure_costs()
    assert result["days_elapsed"] == elapsed
    assert result["projected"] == round(31 / elapsed * 31, 2)
    assert result["next_reset"] == "2026-09-21"
    assert result["by_resource_group"] == {"(unassigned)": 31}


def test_new_cycle_does_not_reuse_previous_period(
    azure: dict[str, Any], clock: Any,
) -> None:
    clock.today = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert "current billing period" in result["error"]
    assert not [r for r in azure["requests"] if r.method == "POST"]


@pytest.mark.parametrize(
    "periods",
    [
        [], [{}], [_period("2026-09-21", "2026-08-20")],
        [_period("2026-08-21", "not-a-date")],
        [_period("2026-08-21", "2026-09-20"), _period("2026-09-01", "2026-09-30")],
    ],
)
def test_absent_invalid_or_ambiguous_period_is_unavailable(
    azure: dict[str, Any], periods: list[dict[str, Any]],
) -> None:
    azure["responses"][_PERIODS_URL] = {"value": periods}
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert result["error"]
    assert not [r for r in azure["requests"] if r.method == "POST"]


def test_selected_budget_is_configurable(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOREFINE_AZURE_BUDGET_NAME", "another-budget")
    old_url = azure["budget_url"]
    response = deepcopy(azure["responses"][old_url])
    response["name"] = "another-budget"
    response["properties"]["amount"] = 200
    azure["responses"][old_url.replace(azure_costs.DEFAULT_BUDGET_NAME, "another-budget")] = response
    result = health_scan.scan_azure_costs()
    assert result["budget_name"] == "another-budget"
    assert result["budget"] == 200
    assert result["remaining_budget"] == 162


@pytest.mark.parametrize("currency", ["GBP", "USD", "JPY"])
def test_currency_is_not_hardcoded(azure: dict[str, Any], currency: str) -> None:
    azure["responses"][azure["budget_url"]]["properties"]["currentSpend"]["unit"] = currency
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "UsageDate", "ResourceGroupName", "Currency", "CostUSD"],
        [[38, 20260907, "rg-demo", currency, 31]],
    )
    result = health_scan.scan_azure_costs()
    assert result["currency"] == currency
    assert azure_costs.format_cost(result["total"], result) == f"{currency} 38.00"


@pytest.mark.parametrize("stage", ["period", "budget", "query", "second-query-page"])
@pytest.mark.parametrize("failure", [403, 404, 500, httpx.ReadTimeout("timed out")])
def test_failed_reads_never_become_clean_or_default_budget(
    azure: dict[str, Any], stage: str, failure: Any,
) -> None:
    url = {
        "period": _PERIODS_URL, "budget": azure["budget_url"],
        "query": _QUERY_URL, "second-query-page": _QUERY_URL + "&$skiptoken=next",
    }[stage]
    azure["responses"][url] = failure
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert result["error"]
    assert "budget" not in result
    assert "remaining_budget" not in result
    for rendered in (
        _render_cost_section(result),
        health_scan.generate_report({}, result, {}),
        health_scan.build_telegram_summary({}, None, [], result),
    ):
        assert "unavailable" in rendered
        assert not re.search(r"\$\d", rendered)
        assert "150" not in rendered
        assert "cost-green" not in rendered
        assert "✅ No alerts" not in rendered


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"timeGrain": "Monthly"}, "BillingMonth"),
        ({"category": "Usage"}, "Cost budget"),
        ({"amount": None}, "monetary amount"),
        ({"amount": 0}, "positive"),
        ({"amount": "NaN"}, "Non-finite"),
        ({"currentSpend": {"unit": "USD"}}, "does not match"),
        ({"currentSpend": {"unit": ""}}, "currency"),
        ({"filter": {"dimensions": {"name": "ResourceGroupName"}}}, "filtered"),
        ({"timePeriod": {"startDate": "2026-10-01"}}, "active now"),
        ({"timePeriod": {"startDate": "2026-08-01", "endDate": "2026-09-01"}}, "active now"),
        ({"timePeriod": {"startDate": "2026-09-01", "endDate": "2026-09-10"}}, "period end"),
    ],
)
def test_unusable_budget_never_compared(
    azure: dict[str, Any], change: dict[str, Any], error: str,
) -> None:
    azure["responses"][azure["budget_url"]]["properties"].update(change)
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert error in result["error"]


@pytest.mark.parametrize(
    "rows",
    [
        [], [[1, 20260907, "rg", None]], [[1, 20260907, "rg", ""]],
        [[1, 20260907, "rg", "EUR"], [2, 20260907, "rg", "USD"]],
        [[None, 20260907, "rg", "EUR"]], [["NaN", 20260907, "rg", "EUR"]],
        [[1, 20260907]], [[1, 20260820, "rg", "EUR"]],
        [[1, 20260909, "rg", "EUR"]],
    ],
)
def test_unknown_currency_malformed_or_out_of_period_costs_fail_closed(
    azure: dict[str, Any], rows: list[list[Any]],
) -> None:
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "UsageDate", "ResourceGroupName", "Currency", "CostUSD"],
        [row + [1] for row in rows],
    )
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert result["error"]


@pytest.mark.parametrize(
    "page",
    [{}, {"properties": {}}, _query_page(["Cost"], [[1]]),
     _query_page(["Cost", "Cost"], [[1, 2]])],
)
def test_invalid_column_metadata_is_not_a_clean_scan(
    azure: dict[str, Any], page: dict[str, Any],
) -> None:
    azure["responses"][_QUERY_URL] = page
    assert health_scan.scan_azure_costs()["total"] == -1


def test_zero_cost_with_known_currency_is_valid(azure: dict[str, Any]) -> None:
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "UsageDate", "ResourceGroupName", "Currency", "CostUSD"],
        [[0, 20260907, None, "EUR", 0]],
    )
    result = health_scan.scan_azure_costs()
    assert result["total"] == 0
    assert result["remaining_budget"] == 100
    assert result["currency"] == "EUR"


@pytest.mark.parametrize("usd", [None, True, "NaN", "Infinity"])
def test_invalid_usd_meter_cannot_fall_back_to_native_currency(
    azure: dict[str, Any], usd: Any,
) -> None:
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "CostUSD", "UsageDate", "ResourceGroupName", "Currency"],
        [[38, usd, 20260907, "rg-demo", "EUR"]],
    )
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert "total_usd" not in result


def test_missing_usd_column_fails_instead_of_relabelling_native_cost(azure: dict[str, Any]) -> None:
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "UsageDate", "ResourceGroupName", "Currency"],
        [[38, 20260907, "rg-demo", "EUR"]],
    )
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert "CostUSD" in result["error"]


def test_configured_credit_does_not_change_azure_alert_budget(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOREFINE_AZURE_MONTHLY_CREDIT_USD", "50")
    result = health_scan.scan_azure_costs()
    assert result["monthly_credit_usd"] == 50
    assert result["estimated_credit_remaining_usd"] == 19
    assert result["budget"] == 100
    assert result["remaining_budget"] == 62


@pytest.mark.parametrize("credit", ["0", "-1", "NaN", "Infinity", "not-money"])
def test_invalid_allowance_is_visible_before_any_azure_read(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch, credit: str,
) -> None:
    monkeypatch.setenv("AUTOREFINE_AZURE_MONTHLY_CREDIT_USD", credit)
    assert health_scan.scan_azure_costs()["total"] == -1
    assert not azure["requests"]


def test_usd_rounding_happens_after_aggregation_not_per_row(azure: dict[str, Any]) -> None:
    azure["responses"][_QUERY_URL] = _query_page(
        ["Cost", "CostUSD", "UsageDate", "ResourceGroupName", "Currency"],
        [[0, "0.005", 20260907, "rg-demo", "EUR"],
         [0, "0.005", 20260907, "rg-demo", "EUR"]],
    )
    result = health_scan.scan_azure_costs()
    assert result["total_usd"] == 0.01
    assert result["estimated_credit_remaining_usd"] == 149.99


@pytest.mark.parametrize(
    "link",
    [_QUERY_URL, "https://example.invalid/page", _SCOPE + "/another-resource?skip=1"],
)
def test_invalid_pagination_fails_without_following(azure: dict[str, Any], link: str) -> None:
    azure["responses"][_QUERY_URL]["properties"]["nextLink"] = link
    result = health_scan.scan_azure_costs()
    assert result["total"] == -1
    assert "pagination" in result["error"]
    assert len([r for r in azure["requests"] if r.method == "POST"]) == 1


def test_full_reports_keep_eur_audit_details_while_telegram_summarizes_usd(
    azure: dict[str, Any],
) -> None:
    result = health_scan.scan_azure_costs()
    for rendered in (
        _render_cost_section(result),
        health_scan.generate_report({}, result, {}),
    ):
        for expected in (
            "EUR 38.00", "EUR 100.00", "EUR 62.00", "Remaining budget",
            "2026-08-21", "2026-09-20", "2026-09-21", "2026-09-07",
            "inclusive", "linear", "ingestion freshness unavailable",
            "budget headroom is not a credit balance", azure_costs.DEFAULT_BUDGET_NAME,
        ):
            assert expected in rendered
        assert "$" not in rendered
        assert "Month-to-date" not in rendered
        assert "Remaining credit: EUR" not in rendered
    message = health_scan.build_telegram_summary({}, None, [], result)
    for expected in ("USD 31.00", "USD 150.00", "USD 119.00", "2026-09-21", "2026-09-07"):
        assert expected in message
    assert azure_costs.DEFAULT_BUDGET_NAME not in message


def test_legacy_reports_render_amounts_but_no_currency_or_green_budget_is_invented() -> None:
    legacy = {"total": 42.5, "budget": 150, "remaining": 107.5, "projected": 80}
    for rendered in (
        _render_cost_section(legacy),
        health_scan.generate_report({}, legacy, {}),
        health_scan.build_telegram_summary({}, None, [], legacy),
    ):
        assert "42.50" in rendered
        assert "currency unknown" in rendered
        assert "legacy report" in rendered
        assert "Budget status unavailable" in rendered
        assert "cost-green" not in rendered
        assert "$" not in rendered


def test_missing_budget_is_not_replaced_with_an_allowance() -> None:
    html = _render_cost_section({"total": 10, "currency": "EUR"})
    assert "150" not in html
    assert "100" not in html
    assert "Budget status unavailable" in html


def test_unverified_currency_mismatch_never_relabels_the_budget(
    azure: dict[str, Any],
) -> None:
    result = health_scan.scan_azure_costs()
    result["budget_currency"] = "USD"
    details = dict(azure_costs.cost_details(result))
    assert details["Budget"] == "USD 100.00"
    assert details["Remaining budget"] == "currency unknown 62.00"
    assert "unavailable" in azure_costs.budget_status(result)[1]


def test_currency_and_budget_names_are_html_escaped(azure: dict[str, Any]) -> None:
    result = health_scan.scan_azure_costs()
    result["budget_name"] = "<b>test & budget</b>"
    for rendered in (_render_cost_section(result),):
        assert "<b>test" not in rendered
        assert "&lt;b&gt;test &amp; budget&lt;/b&gt;" in rendered
    message = health_scan.build_telegram_summary({}, None, [], result)
    assert "<b>test" not in message
    assert "test &amp; budget" not in message

def test_ai_prompt_uses_only_reported_currency_period_budget(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "fake-key")
    result = health_scan.scan_azure_costs()
    with patch("openai.AzureOpenAI") as ctor:
        choice = MagicMock()
        choice.message.content = "{}"
        choice.finish_reason = "stop"
        ctor.return_value.chat.completions.create.return_value.choices = [choice]
        assert health_scan.analyze_with_ai({}, result) == {}
    messages = ctor.return_value.chat.completions.create.call_args.kwargs["messages"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "$" not in system + user
    assert "NOT remaining credit" in system
    assert "not an Azure forecast" in system
    assert '"currency": "EUR"' in user
    assert '"budget": 100.0' in user
    assert '"period_start": "2026-08-21"' in user
    assert '"latest_usage_date": "2026-09-07"' in user


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "cost_failure",
    [
        {"total": -1, "error": "403"},
        {"total": -1, "error": ""},
        {"total": -1},
        {"total": 0, "error": " \t "},
        {"total": 0, "error": None},
        {},
    ],
)
def test_cost_read_failure_is_an_incomplete_scan_but_other_stages_still_run(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool, cost_failure: dict[str, Any],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    with (
        patch.object(health_scan, "scan_github", return_value={}),
        patch.object(health_scan, "scan_azure_costs", return_value=cost_failure),
        patch.object(health_scan, "scan_app_insights", return_value={}) as telemetry,
        patch.object(health_scan, "check_deployed_urls", return_value={}),
        patch.object(health_scan, "analyze_with_ai", return_value={}) as analysis,
        patch.object(health_scan, "commit_report", return_value="report.md") as persist,
        patch.object(health_scan, "enforce_report_retention"),
        patch.object(health_scan, "create_github_issues", return_value=[]),
        patch("agent.notify.send_telegram", return_value=True) as send,
    ):
        result = health_scan.run_health_scan(["demo"], dry_run=dry_run)
    assert result["failed_stages"] == ["cost"]
    assert "Azure cost scan unavailable" in result["telegram_summary"]
    assert "✅ No alerts" not in result["telegram_summary"]
    report = result["report"] if dry_run else persist.call_args.args[1]
    assert "✅ No alerts" not in report
    telemetry.assert_called_once()
    analysis.assert_called_once()
    assert persist.call_count == send.call_count == (0 if dry_run else 1)


@pytest.mark.parametrize("message", ["", " \t\n "])
@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_message_timeout_fails_cli_after_other_stages_and_never_claims_all_clear(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], message: str, dry_run: bool,
) -> None:
    from agent.main import run_health_scan_mode

    azure["responses"][_QUERY_URL] = httpx.ReadTimeout(message)
    cost_data = health_scan.scan_azure_costs()
    assert cost_data["total"] == -1
    assert cost_data["error"] == "ReadTimeout"

    monkeypatch.setenv("GH_TOKEN", "fake-token")
    with (
        patch.object(health_scan, "scan_github", return_value={}),
        patch.object(health_scan, "scan_app_insights", return_value={}) as telemetry,
        patch.object(health_scan, "check_deployed_urls", return_value={}),
        patch.object(health_scan, "analyze_with_ai", return_value={}) as analysis,
        patch.object(health_scan, "commit_report", return_value="report.md") as persist,
        patch.object(health_scan, "enforce_report_retention"),
        patch.object(health_scan, "create_github_issues", return_value=[]),
        patch("agent.notify.send_telegram", return_value=True) as send,
    ):
        with pytest.raises(SystemExit) as exit_info:
            run_health_scan_mode(["demo"], assign_copilot=False, dry_run=dry_run)

    assert exit_info.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["failed_stages"] == ["cost"]
    report = summary["report"] if dry_run else persist.call_args.args[1]
    for rendered in (report, summary["telegram_summary"], _render_cost_section(cost_data)):
        assert "unavailable" in rendered
        assert "ReadTimeout" in rendered
        assert "✅ No alerts" not in rendered
        assert "cost-green" not in rendered
    telemetry.assert_called_once()
    analysis.assert_called_once()
    assert persist.call_count == send.call_count == (0 if dry_run else 1)


@pytest.mark.parametrize("message", ["", " \t ", None])
def test_explicit_error_never_renders_verified_costs_as_healthy(
    azure: dict[str, Any], message: str | None,
) -> None:
    cost_data = health_scan.scan_azure_costs()
    cost_data["error"] = message

    assert azure_costs.budget_status(cost_data)[0] == "muted"
    for rendered in (
        _render_cost_section(cost_data),
        health_scan.generate_report({}, cost_data, {}),
        health_scan.build_telegram_summary({}, None, [], cost_data),
    ):
        assert "unavailable" in rendered
        assert "✅ No alerts" not in rendered
        assert "cost-green" not in rendered
        assert "Within budget" not in rendered


def test_cost_success_log_is_debug_and_uses_reported_metadata(
    azure: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    result = health_scan.scan_azure_costs()
    with (
        patch.object(health_scan, "scan_github", return_value={}),
        patch.object(health_scan, "scan_azure_costs", return_value=result),
        patch.object(health_scan, "scan_app_insights", return_value={}),
        patch.object(health_scan, "check_deployed_urls", return_value={}),
        patch.object(health_scan, "analyze_with_ai", return_value={}),
        caplog.at_level(logging.DEBUG, logger="agent.health_scan"),
    ):
        health_scan.run_health_scan(["demo"], dry_run=True)
    record = next(r for r in caplog.records if "Azure billing-cycle spend" in r.message)
    assert record.levelno == logging.DEBUG
    assert "EUR 38.00" in record.message
    assert "2026-08-21 through 2026-09-20" in record.message
    assert "Latest reported usage: 2026-09-07" in record.message
    assert "$" not in record.message
