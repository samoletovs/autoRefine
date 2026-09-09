"""Alerts and paid repair work must follow observations, not model assertions."""

from __future__ import annotations

import datetime
from unittest.mock import Mock

import httpx
import pytest

from agent import health_scan
from agent.dashboard import render_html_dashboard


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(health_scan, "fetch_workspace_manifest", lambda: {
        "projects": [{"repo": "samoletovs/era", "domain": "era.example", "health_path": "/health"}],
    })
    client = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=client)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(health_scan.httpx, "Client", Mock(return_value=context))
    monkeypatch.setattr("time.sleep", Mock())
    return client


def response(status: int, milliseconds: int) -> Mock:
    return Mock(
        status_code=status, content=b"healthy",
        elapsed=datetime.timedelta(milliseconds=milliseconds),
    )


def test_slow_first_response_is_rechecked_and_retained(probe: Mock) -> None:
    probe.get.side_effect = [response(200, 22989), response(200, 226)]

    result = health_scan.check_deployed_urls()["era"]

    assert probe.get.call_count == 2
    assert result["ok"] is True
    assert result["response_ms"] == 226
    assert [p["response_ms"] for p in result["attempts"]] == [22989, 226]


def test_recovered_503_does_not_discard_first_failure(probe: Mock) -> None:
    probe.get.side_effect = [response(503, 3261), response(200, 226)]

    result = health_scan.check_deployed_urls()["era"]

    assert result["ok"] is True
    assert [p["status"] for p in result["attempts"]] == [503, 200]


def test_persistent_503_remains_unhealthy_after_bounded_retries(probe: Mock) -> None:
    probe.get.return_value = response(503, 200)

    result = health_scan.check_deployed_urls()["era"]

    assert probe.get.call_count == 3
    assert result["ok"] is False
    assert len(result["attempts"]) == 3


def test_persistent_slowness_is_not_relabelled_as_recovery(probe: Mock) -> None:
    probe.get.return_value = response(200, 3001)

    result = health_scan.check_deployed_urls()
    analysis = health_scan.ground_analysis({}, {}, {}, {}, result)

    assert probe.get.call_count == 3
    assert any("3001ms" in alert for alert in analysis["alerts"])


def test_404_is_not_retried(probe: Mock) -> None:
    probe.get.return_value = response(404, 100)

    result = health_scan.check_deployed_urls()["era"]

    assert probe.get.call_count == 1
    assert result["ok"] is False


def test_transport_failures_keep_diagnostics(probe: Mock) -> None:
    probe.get.side_effect = httpx.ConnectError("connection failed")

    result = health_scan.check_deployed_urls()["era"]

    assert probe.get.call_count == 3
    assert result["ok"] is False
    assert all(p["error"] == "connection failed" for p in result["attempts"])


@pytest.mark.parametrize(("projected", "expected"), [
    (40.75, False), (150, False), (150.01, True), (-1, False),
    (float("nan"), False), (float("inf"), False),
])
def test_budget_alert_uses_numbers_not_model_claims(projected: float, expected: bool) -> None:
    analysis = health_scan.ground_analysis(
        {"alerts": ["Budget alert: Projected Azure costs exceed $150."]},
        {}, {"total": 10.87, "projected": projected, "budget": 150}, {}, {},
    )

    assert bool(analysis["alerts"]) is expected


def test_latest_operational_failure_is_named_not_called_ci() -> None:
    analysis = health_scan.ground_analysis({}, {
        "prime": {
            "ci_status": "failure", "ci_name": "prime daily check-in",
            "ci_event": "schedule", "ci_branch": "main",
            "ci_url": "https://github.com/samoletovs/prime/actions/runs/123",
        },
    }, {}, {}, {})

    assert len(analysis["alerts"]) == 1
    assert "prime daily check-in" in analysis["alerts"][0]
    assert "schedule" in analysis["alerts"][0]
    assert "CI failure" not in analysis["alerts"][0]
    assert "actions/runs/123" in analysis["alerts"][0]


def test_unsubstantiated_issue_body_never_reaches_filer() -> None:
    analysis = health_scan.ground_analysis({
        "alerts": ["era is broken"],
        "issues_to_create": [
            {"repo": "era", "title": "Optimize URL response time", "body": "Invented cause"},
            {"finding_id": "url:era", "body": "Invented cause"},
        ],
    }, {}, {"total": 10.87, "projected": 40.75, "budget": 150}, {}, {
        "era": {"url": "https://era.example", "status": 200, "ok": True, "response_ms": 226},
    })

    assert analysis["alerts"] == []
    assert analysis["issues_to_create"] == []


def test_supported_issue_uses_observed_body_not_model_body() -> None:
    analysis = health_scan.ground_analysis({
        "issues_to_create": [{"finding_id": "url:era", "body": "Delete the database"}],
    }, {}, {}, {}, {
        "era": {"url": "https://era.example", "status": 503, "ok": False, "response_ms": 226},
    })

    [issue] = analysis["issues_to_create"]
    assert issue["repo"] == "era"
    assert "503" in issue["body"]
    assert "https://era.example" in issue["body"]
    assert "Delete the database" not in issue["body"]
    assert "<!-- autorefine-health:url:era -->" in issue["body"]


def test_common_url_failure_is_one_investigation_not_n_assignments() -> None:
    urls = {
        repo: {"url": f"https://{repo}.example", "status": 0, "ok": False,
               "response_ms": -1, "error": "DNS failure"}
        for repo in ("era", "turgo", "atlas")
    }
    analysis = health_scan.ground_analysis({
        "issues_to_create": [
            {"finding_id": f"url:{repo}"} for repo in urls
        ] + [{"finding_id": "url:multiple"}],
    }, {}, {}, {}, urls)

    [issue] = analysis["issues_to_create"]
    assert issue["repo"] == "autoRefine"
    assert all(repo in issue["body"] for repo in urls)
    assert "monitor" in issue["body"].lower()


def test_report_and_dashboard_show_first_and_repeat_observations(probe: Mock) -> None:
    probe.get.side_effect = [response(200, 22989), response(200, 226)]
    urls = health_scan.check_deployed_urls()

    report = health_scan.generate_report({}, {}, {}, url_health_data=urls)
    dashboard = render_html_dashboard({}, {}, {}, url_health_data=urls)

    for rendered in (report, dashboard):
        assert "22989" in rendered
        assert "226" in rendered
        assert "recovered" in rendered.lower()


def test_dry_run_pipeline_rejects_screenshot_false_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    for name, value in {
        "scan_github": {},
        "scan_azure_costs": {"total": 10.87, "projected": 40.75, "budget": 150},
        "scan_app_insights": {},
        "check_deployed_urls": {
            "era": {"url": "https://era.example", "status": 200, "ok": True, "response_ms": 226},
        },
        "analyze_with_ai": {
            "alerts": ["Budget alert: Projected Azure costs exceed $150.", "era is slow"],
            "issues_to_create": [{"repo": "era", "title": "Optimize URL response time"}],
        },
    }.items():
        monkeypatch.setattr(health_scan, name, Mock(return_value=value))
    writes = [Mock() for _ in range(4)]
    for name, mock in zip(
        ("commit_report", "enforce_report_retention", "create_github_issues"), writes[:3],
    ):
        monkeypatch.setattr(health_scan, name, mock)
    monkeypatch.setattr("agent.notify.send_telegram", writes[3])

    result = health_scan.run_health_scan(["era"], dry_run=True)

    assert "exceed" not in result["telegram_summary"]
    assert "era is slow" not in result["telegram_summary"]
    assert result["planned_issues"] == []
    for mock in writes:
        mock.assert_not_called()


@pytest.mark.parametrize("existing", [
    {"body": "<!-- autorefine-health:url:era -->", "title": "Investigate"},
    {"body": "An older scan", "title": "🔧 Tech Debt: Optimize URL response time"},
])
def test_repeat_finding_does_not_create_or_assign_another_issue(
    probe: Mock, existing: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing["html_url"] = "https://github.com/samoletovs/era/issues/6"
    probe.get.return_value = httpx.Response(
        200, json=[existing], request=httpx.Request("GET", "https://api.github.com"),
    )
    assign = Mock()
    monkeypatch.setattr(health_scan.subprocess, "run", assign)
    issue = {"repo": "era", "title": "Investigate URL health", "body": "HTTP 503",
             "finding_id": "url:era"}

    assert health_scan.create_github_issues("test-token", [issue], ["era"]) == []
    probe.post.assert_not_called()
    assign.assert_not_called()


def test_failed_duplicate_read_never_creates_an_issue(probe: Mock) -> None:
    probe.get.return_value = httpx.Response(
        403, request=httpx.Request("GET", "https://api.github.com"),
    )
    issue = {"repo": "era", "title": "Investigate URL health", "body": "HTTP 503",
             "finding_id": "url:era"}

    with pytest.raises(health_scan.HealthIssueFilingError):
        health_scan.create_github_issues("test-token", [issue], ["era"])

    probe.post.assert_not_called()


def test_duplicate_check_reads_beyond_first_page(probe: Mock) -> None:
    issue = {"repo": "era", "title": "Investigate URL health", "body": "HTTP 503",
             "finding_id": "url:era"}
    request = httpx.Request("GET", "https://api.github.com")
    probe.get.side_effect = [
        httpx.Response(200, request=request, json=[{"body": "", "title": "unrelated"}] * 100),
        httpx.Response(200, request=request, json=[{
            "body": "<!-- autorefine-health:url:era -->",
            "html_url": "https://github.com/samoletovs/era/issues/6",
        }]),
    ]

    assert health_scan.create_github_issues("test-token", [issue], ["era"]) == []
    assert [call.kwargs["params"]["page"] for call in probe.get.call_args_list] == [1, 2]
    probe.post.assert_not_called()


def test_workflow_scan_keeps_run_provenance(probe: Mock) -> None:
    request = httpx.Request("GET", "https://api.github.com")
    probe.get.side_effect = [
        httpx.Response(200, request=request, json=[]) for _ in range(4)
    ] + [httpx.Response(200, request=request, json={"workflow_runs": [{
        "conclusion": "failure", "name": "prime daily check-in", "event": "schedule",
        "head_branch": "main", "html_url": "https://github.com/samoletovs/prime/actions/runs/123",
        "workflow_id": 17,
    }]})]

    result = health_scan.scan_github("test-token", ["prime"])["prime"]

    assert result["ci_name"] == "prime daily check-in"
    assert result["ci_event"] == "schedule"
    assert result["ci_branch"] == "main"
    assert result["ci_url"].endswith("/123")
    assert result["ci_workflow_id"] == 17


def test_model_failure_does_not_hide_measured_outage() -> None:
    analysis = health_scan.ground_analysis({"error": "model unavailable"}, {}, {}, {}, {
        "era": {"url": "https://era.example", "status": 503, "ok": False, "response_ms": 200},
    })

    summary = health_scan.build_telegram_summary(analysis, None, [])
    report = health_scan.generate_report({}, {}, analysis)
    dashboard = render_html_dashboard({}, {}, analysis)

    for text in (summary, report, dashboard):
        assert "503" in text
        assert "FAILED" in text
    assert analysis["issues_to_create"] == []


@pytest.mark.parametrize("app", ["era", "unmapped-app-insights-resource"])
def test_healthy_homepage_does_not_mask_repeated_backend_500s(app: str) -> None:
    analysis = health_scan.ground_analysis({}, {"era": {}}, {}, {
        app: {"exception_count": 250, "failed_request_count": 500,
              "failed_requests_24h": [
                  {"endpoint": "POST /api/bookings", "status": "500", "count": 500},
              ]},
    }, {"era": {"status": 200, "ok": True, "response_ms": 100}})

    assert len(analysis["alerts"]) == 1
    assert app in analysis["alerts"][0]
    assert "500 failed requests" in analysis["alerts"][0]


def test_issue_read_failure_does_not_silence_notification(
    probe: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    probe.get.return_value = httpx.Response(
        403, request=httpx.Request("GET", "https://api.github.com"),
    )
    for name, value in {
        "scan_github": {},
        "scan_azure_costs": {},
        "scan_app_insights": {},
        "check_deployed_urls": {
            "era": {"url": "https://era.example", "status": 503, "ok": False, "response_ms": 200},
        },
        "analyze_with_ai": {"issues_to_create": [{"finding_id": "url:era"}]},
        "commit_report": "reports/run/test.md",
        "enforce_report_retention": None,
    }.items():
        monkeypatch.setattr(health_scan, name, Mock(return_value=value))
    notify = Mock(return_value=True)
    monkeypatch.setattr("agent.notify.send_telegram", notify)

    result = health_scan.run_health_scan(["era"], assign_copilot=False)

    notify.assert_called_once()
    assert "503" in notify.call_args.args[0]
    assert "Issue filing failed" in notify.call_args.args[0]
    assert result["failed_stages"] == ["issues"]
    probe.post.assert_not_called()


def test_filing_failure_preserves_already_created_issue_urls(probe: Mock) -> None:
    request = httpx.Request("GET", "https://api.github.com")
    probe.get.side_effect = [
        httpx.Response(200, request=request, json=[]),
        httpx.Response(403, request=request),
    ]
    url = "https://github.com/samoletovs/era/issues/12"
    probe.post.return_value = httpx.Response(
        201, request=request, json={"html_url": url, "number": 12},
    )
    issues = [
        {"repo": repo, "title": "Investigate URL health", "body": "HTTP 503",
         "finding_id": f"url:{repo}"} for repo in ("era", "turgo")
    ]

    with pytest.raises(health_scan.HealthIssueFilingError) as failure:
        health_scan.create_github_issues("test-token", issues, ["era", "turgo"], False)

    assert failure.value.created == [url]
    probe.post.assert_called_once()


def test_budget_finding_respects_reported_currency() -> None:
    analysis = health_scan.ground_analysis(
        {}, {}, {"total": 35.99, "projected": 101, "budget": 100, "currency": "EUR"}, {}, {},
    )

    assert "EUR 101.00" in analysis["alerts"][0]
    assert "$" not in analysis["alerts"][0]


def test_report_does_not_claim_proposed_issues_were_created() -> None:
    report = health_scan.generate_report({}, {}, {
        "issues_to_create": [{"repo": "era", "title": "Investigate URL health"}],
    })

    assert "Proposed Repair Issues" in report
    assert "Auto-Created Issues" not in report
