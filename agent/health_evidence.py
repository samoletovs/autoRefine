"""Measured findings are the authority for alerts and automatic repair work."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from agent.azure_costs import budget_status, credit_status

log = logging.getLogger(__name__)

SLOW_RESPONSE_MS = 3000
FAILED_WORKFLOW_STATES = frozenset({"failure", "timed_out", "startup_failure"})


@dataclass(frozen=True)
class HealthFinding:
    finding_id: str
    message: str
    repo: str | None = None
    title: str = ""
    details: str = ""
    alert: bool = True


def describe_probes(data: dict[str, Any]) -> str:
    attempts = data.get("attempts", [])
    if not attempts:
        return data.get("error", "")
    samples = "; ".join(
        f"#{index}: HTTP {sample['status']}, {sample['response_ms']}ms"
        + (f" ({sample['error']})" if sample.get("error") else "")
        for index, sample in enumerate(attempts, 1)
    )
    if len(attempts) > 1 and data.get("ok") and 0 <= data["response_ms"] <= SLOW_RESPONSE_MS:
        return f"Recovered on retry (cause unconfirmed). {samples}"
    return samples


def _known_amount(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def collect_findings(
    github_data: dict[str, Any],
    cost_data: dict[str, Any],
    app_insights_data: dict[str, Any],
    url_health_data: dict[str, Any],
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    for repo, data in github_data.items():
        if data.get("ci_status") not in FAILED_WORKFLOW_STATES:
            continue
        name = data.get("ci_name", "unnamed workflow")
        message = (
            f"{repo}: latest workflow '{name}' {data['ci_status']} "
            f"(event={data.get('ci_event', 'unknown')}, "
            f"branch={data.get('ci_branch', 'unknown')})."
        )
        if data.get("ci_url"):
            message += f" {data['ci_url']}"
        findings.append(HealthFinding(
            f"workflow:{repo}:{data.get('ci_workflow_id', name)}", message,
            repo, f"Investigate failed workflow: {name}",
            "This is the latest workflow, not an aggregate CI/build verdict. "
            "Inspect its failed step before proposing a code or credential change.",
        ))

    budget = cost_data.get("budget")
    currency = cost_data.get("currency")
    if budget_status(cost_data)[0] != "muted" and _known_amount(budget) and budget > 0:
        for key, label in (("total", "Billing-cycle"), ("projected", "Projected cycle-end")):
            amount = cost_data.get(key)
            if _known_amount(amount) and amount > budget:
                findings.append(HealthFinding(
                    f"budget:{key}", f"{label} Azure cost {currency} {amount:.2f} exceeds "
                    f"the {currency} {budget:.2f} budget.",
                ))

    allowance = cost_data.get("monthly_credit_usd")
    if credit_status(cost_data)[0] != "muted" and _known_amount(allowance):
        for key, label in (("total_usd", "Billing-cycle"), ("projected_usd", "Projected cycle-end")):
            amount = cost_data[key]
            if _known_amount(amount) and amount > allowance:
                findings.append(HealthFinding(
                    f"credit:{key}", f"{label} Azure cost USD {amount:.2f} exceeds "
                    f"the configured USD {allowance:.2f} monthly credit (usage estimate, "
                    "not a credit balance).",
                ))

    url_findings: list[HealthFinding] = []
    for repo, data in url_health_data.items():
        if not data.get("ok"):
            observation = f"URL check failed (HTTP {data.get('status', 0)})"
        elif data.get("response_ms", -1) > SLOW_RESPONSE_MS:
            observation = f"URL response remains slow ({data['response_ms']}ms)"
        else:
            continue
        url_findings.append(HealthFinding(
            f"url:{repo}", f"{repo}: {observation}. {data.get('url', '')}",
            repo, "Investigate URL health", describe_probes(data),
        ))
    # Same common-cause rule as the governance uptime filer: a majority of
    # at least three sites is one investigation, not N paid agent assignments.
    if len(url_findings) >= 3 and len(url_findings) > len(url_health_data) / 2:
        findings.append(HealthFinding(
            "url:multiple", f"{len(url_findings)} URL checks failed or remained slow.",
            "autoRefine", "Investigate multiple URL health failures",
            "Check monitor connectivity and shared dependencies before blaming individual apps.\n\n"
            + "\n\n".join(f"{item.message}\n{item.details}" for item in url_findings),
        ))
    else:
        findings.extend(url_findings)

    for repo, data in app_insights_data.items():
        if not isinstance(data, dict):
            continue
        exceptions = data.get("exception_count", 0)
        failed_requests = data.get("failed_request_count", 0)
        if exceptions <= 0 and failed_requests <= 0:
            continue
        recurring_server_errors = [
            item for item in data.get("failed_requests_24h", [])
            if isinstance(item, dict) and item.get("count", 0) >= 2
            and str(item.get("status", "")).isdigit()
            and 500 <= int(item["status"]) <= 599
        ]
        details = "\n".join(
            f"{item.get('endpoint', 'unknown endpoint')}: HTTP {item['status']} "
            f"({item['count']} requests)" for item in recurring_server_errors
        )
        findings.append(HealthFinding(
            f"telemetry:{repo}",
            f"{repo}: telemetry recorded {exceptions} exceptions and "
            f"{failed_requests} failed requests in 24h.",
            repo if repo in github_data else None, "Investigate observed telemetry errors",
            details or "Counts alone do not establish a bug; inspect events before fixing.",
            alert=bool(recurring_server_errors),
        ))
    return findings


def ground_analysis(
    analysis: dict[str, Any],
    github_data: dict[str, Any],
    cost_data: dict[str, Any],
    app_insights_data: dict[str, Any],
    url_health_data: dict[str, Any],
) -> dict[str, Any]:
    """Retain AI advice, but require an exact observation for any automatic action."""
    findings = collect_findings(github_data, cost_data, app_insights_data, url_health_data)
    by_id = {finding.finding_id: finding for finding in findings}
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    proposals = [] if analysis.get("error") else analysis.get("issues_to_create", [])
    if not isinstance(proposals, list):
        log.warning("Rejected malformed health issue proposals: expected a list")
        proposals = []
    for proposal in proposals:
        key = proposal.get("finding_id") if isinstance(proposal, dict) else None
        finding = by_id.get(key) if isinstance(key, str) else None
        if finding is None or finding.repo is None:
            log.warning("Rejected health issue proposal without actionable evidence: %r", key)
            continue
        if finding.finding_id in seen:
            continue
        seen.add(finding.finding_id)
        issues.append({
            "repo": finding.repo,
            "title": finding.title,
            "body": (
                f"{finding.message}\n\n{finding.details}\n\n"
                "Observed symptoms are not a root-cause diagnosis.\n"
                f"<!-- autorefine-health:{finding.finding_id} -->"
            ),
            "labels": ["tech-debt", "autorefine"],
            "finding_id": finding.finding_id,
        })
    return {
        **analysis,
        "alerts": [finding.message for finding in findings if finding.alert],
        "issues_to_create": issues,
    }
