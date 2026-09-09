"""Read subscription costs against an Azure budget's actual billing cycle."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from azure.core.exceptions import AzureError

log = logging.getLogger(__name__)

DEFAULT_BUDGET_NAME = "naurolabs-credit-cycle-eur-100"
BUDGET_WARNING_THRESHOLD_PCT = 70
_ARM = "https://management.azure.com"


def _date(value: Any) -> dt.date:
    if not isinstance(value, str):
        raise TypeError("Missing or invalid Azure date")
    return dt.datetime.fromisoformat(value).date()


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise TypeError("Missing or invalid monetary amount")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Non-finite monetary amount")
    return amount


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Missing or invalid currency")
    return value


def _pages(
    client: httpx.Client, url: str, *, query: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Follow ARM continuations without forwarding a bearer token off-resource."""
    original = urlsplit(url)
    seen: set[str] = set()
    while url:
        target = urlsplit(url)
        if (
            target.scheme != "https" or target.netloc != original.netloc
            or target.path != original.path or target.fragment or url in seen
        ):
            raise ValueError("Invalid or repeated Azure pagination link")
        seen.add(url)
        response = client.get(url) if query is None else client.post(url, json=query)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise TypeError("Invalid Azure response")
        page = body if query is None else body["properties"]
        if not isinstance(page, dict):
            raise TypeError("Invalid Azure response properties")
        yield page
        next_link = page.get("nextLink")
        if next_link is not None and not isinstance(next_link, str):
            raise ValueError("Invalid Azure pagination link")
        url = urljoin(url, next_link) if next_link else ""


def _billing_period(
    client: httpx.Client, scope_url: str, today: dt.date,
) -> tuple[dt.date, dt.date]:
    url = (
        f"{scope_url}/providers/Microsoft.Billing/billingPeriods"
        "?api-version=2018-03-01-preview"
    )
    matches: set[tuple[dt.date, dt.date]] = set()
    for page in _pages(client, url):
        periods = page["value"]
        if not isinstance(periods, list):
            raise TypeError("Invalid billing periods response")
        for period in periods:
            props = period["properties"]
            start = _date(props["billingPeriodStartDate"])
            end = _date(props["billingPeriodEndDate"])
            if end < start:
                raise ValueError("Invalid billing period range")
            if start <= today <= end:
                matches.add((start, end))
    if len(matches) != 1:
        raise ValueError("No unique current billing period available from Azure")
    return matches.pop()


def _budget(
    client: httpx.Client, scope_url: str, name: str, today: dt.date, end: dt.date,
) -> tuple[Decimal, str]:
    response = client.get(
        f"{scope_url}/providers/Microsoft.Consumption/budgets/{quote(name, safe='')}"
        "?api-version=2024-08-01"
    )
    response.raise_for_status()
    body = response.json()
    props = body["properties"]
    if body["name"] != name:
        raise ValueError("Azure returned a different budget")
    if props["category"] != "Cost" or props["timeGrain"] != "BillingMonth":
        raise ValueError("Selected budget must be a Cost budget with BillingMonth time grain")
    if props.get("filter") or props.get("filters"):
        raise ValueError("Selected budget is filtered; cannot compare subscription-wide costs")
    term = props["timePeriod"]
    # Budget lifetime starts on a calendar-month boundary even for BillingMonth.
    # It is not the recurring billing-cycle boundary supplied by Billing Periods.
    if _date(term["startDate"]) > today or (
        term.get("endDate") and _date(term["endDate"]) < end
    ):
        raise ValueError("Selected budget must be active now and through the billing period end")
    amount = _money(props["amount"])
    if amount <= 0:
        raise ValueError("Selected budget amount must be positive")
    # The budget amount has no currency field; currentSpend.unit supplies its unit.
    return amount, _currency(props["currentSpend"]["unit"])


def _cost_rows(
    client: httpx.Client, scope_url: str, start: dt.date, today: dt.date,
) -> tuple[Decimal, dict[str, Decimal], str, dt.date]:
    query = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{today.isoformat()}T23:59:59Z",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceGroupName"}],
        },
    }
    url = f"{scope_url}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    total = Decimal(0)
    by_rg: dict[str, Decimal] = {}
    currencies: set[str] = set()
    latest: dt.date | None = None
    for page in _pages(client, url, query=query):
        names = [column["name"] for column in page["columns"]]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate cost query columns")
        required = {"Cost", "Currency", "ResourceGroupName", "UsageDate"}
        if not required.issubset(names):
            raise ValueError("Missing cost query columns (Cost, Currency, ResourceGroupName, UsageDate)")
        rows = page["rows"]
        if not isinstance(rows, list):
            raise TypeError("Invalid cost query rows")
        for row in rows:
            if not isinstance(row, list) or len(row) != len(names):
                raise ValueError("Malformed cost query row")
            values = dict(zip(names, row))
            cost = _money(values["Cost"])
            currencies.add(_currency(values["Currency"]))
            usage_date = dt.date.fromisoformat(str(values["UsageDate"]))
            if not start <= usage_date <= today:
                raise ValueError("Cost row is outside the requested billing period")
            latest = max(latest, usage_date) if latest else usage_date
            rg = values["ResourceGroupName"]
            if rg is not None and not isinstance(rg, str):
                raise ValueError("Invalid resource group in cost query")
            rg = rg or "(unassigned)"
            by_rg[rg] = by_rg.get(rg, Decimal(0)) + cost
            total += cost
    if len(currencies) != 1 or latest is None:
        raise ValueError("Cost query must report exactly one currency; empty/mixed results unavailable")
    return total, by_rg, currencies.pop(), latest


def scan_azure_costs() -> dict[str, Any]:
    """Read costs, dates and budget; never infer a credit balance or a currency."""
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    name = os.environ.get("AUTOREFINE_AZURE_BUDGET_NAME", DEFAULT_BUDGET_NAME)
    if not subscription:
        return {"error": "AZURE_SUBSCRIPTION_ID not set", "total": -1}
    try:
        from azure.identity import DefaultAzureCredential

        today = dt.datetime.now(dt.UTC).date()
        scope_url = f"{_ARM}/subscriptions/{quote(subscription, safe='')}"
        with DefaultAzureCredential() as credential:
            token = credential.get_token(f"{_ARM}/.default").token
            with httpx.Client(
                headers={
                    "Authorization": f"Bearer {token}",
                    "ClientType": "samoletovs-autorefine",
                },
                timeout=30,
            ) as client:
                start, end = _billing_period(client, scope_url, today)
                budget, budget_currency = _budget(client, scope_url, name, today, end)
                total, by_rg, currency, latest = _cost_rows(client, scope_url, start, today)
        if currency != budget_currency:
            raise ValueError("Cost query currency does not match selected budget currency")
        days_elapsed = (today - start).days + 1
        days_in_period = (end - start).days + 1
        remaining = float(round(budget - total, 2))
        return {
            "total": float(round(total, 2)),
            "currency": currency,
            "budget": float(budget),
            "budget_name": name,
            "budget_currency": budget_currency,
            "budget_time_grain": "BillingMonth",
            "remaining_budget": remaining,
            "remaining": remaining,  # Backward-compatible alias, not remaining credit.
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "next_reset": (end + dt.timedelta(days=1)).isoformat(),
            "query_end": today.isoformat(),
            "latest_usage_date": latest.isoformat(),
            "projected": float(round(total * days_in_period / days_elapsed, 2)),
            "projection_method": "linear_elapsed_cycle_days",
            "days_elapsed": days_elapsed,
            "days_in_period": days_in_period,
            "by_resource_group": {rg: float(round(cost, 2)) for rg, cost in by_rg.items()},
        }
    except (AzureError, httpx.HTTPError, OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        log.warning("Azure cost scan unavailable: %s", exc)
        return {"error": str(exc), "total": -1, "budget_name": name}


def format_cost(amount: Any, data: dict[str, Any]) -> str:
    """Render old reports without inventing a currency for their stored amounts."""
    if amount is None:
        return "unavailable"
    try:
        currency = _currency(data.get("currency"))
    except ValueError:
        currency = "currency unknown"
    return f"{currency} {_money(amount):.2f}"


def budget_status(data: dict[str, Any]) -> tuple[str, str]:
    """Never paint an unverified legacy budget comparison green."""
    try:
        if data.get("error") or _money(data["total"]) < 0:
            raise ValueError("Unavailable")
        currency = _currency(data["currency"])
        if data["budget_currency"] != currency or data["budget_time_grain"] != "BillingMonth":
            raise ValueError("Unverified budget")
        if not data["budget_name"] or _date(data["period_end"]) < _date(data["period_start"]):
            raise ValueError("Unverified period")
        budget, total = _money(data["budget"]), _money(data["total"])
        if budget <= 0:
            raise ValueError("Invalid budget")
        if total > budget:
            return "cost-red", "🔴 OVER BUDGET"
        if data.get("projected") is not None and _money(data["projected"]) > budget:
            return "cost-red", "🔴 PROJECTED OVER BUDGET"
        if total / budget * 100 >= BUDGET_WARNING_THRESHOLD_PCT:
            return "cost-yellow", "🟡 Warning"
        return "cost-green", "💰 Within budget so far"
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return "muted", "⚠️ Budget status unavailable (currency, period or budget unverified)"


def cost_details(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Shared labels and metadata for Markdown, HTML and Telegram."""
    period = (
        f"{data['period_start']} through {data['period_end']} (inclusive)"
        if data.get("period_start") and data.get("period_end") else "unknown (legacy report)"
    )
    latest = data.get("latest_usage_date", "unavailable")
    remaining = data.get("remaining_budget", data.get("remaining"))
    budget_unit = {"currency": data.get("budget_currency")}
    remaining_unit = budget_unit if data.get("budget_currency") == data.get("currency") else {}
    return [
        ("Billing period", period),
        ("Next reset", data.get("next_reset", "unknown")),
        ("Budget name", data.get("budget_name", "unknown (legacy report)")),
        ("Budget", format_cost(data.get("budget"), budget_unit)),
        ("Remaining budget", format_cost(remaining, remaining_unit)),
        ("Cycle-end projection (linear, not an Azure forecast)", format_cost(
            data.get("projected"), data,
        )),
        ("Costs queried through", data.get("query_end", "unknown")),
        ("Latest reported usage", f"{latest}; ingestion freshness unavailable"),
        ("Remaining credit", "unavailable — budget headroom is not a credit balance"),
    ]
