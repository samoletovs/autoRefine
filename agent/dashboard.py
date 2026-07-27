"""User dashboard for autoRefine — generates a self-contained HTML report.

The dashboard visualises the same data that ``health_scan.generate_report``
summarises as Markdown, but presents it as a browser-ready HTML page with
colour-coded health scores, cost indicators, and improvement suggestions.

Usage (standalone):
    from agent.dashboard import render_html_dashboard
    html = render_html_dashboard(github_data, cost_data, analysis)
    Path("dashboard.html").write_text(html, encoding="utf-8")

Or via CLI:
    python -m agent.main --mode dashboard --repo owner/repo
"""

from __future__ import annotations

import datetime
import html as html_lib
import logging
from typing import Any

log = logging.getLogger(__name__)

# Health score thresholds (max = 15: R+L+M)
_HEALTH_GREEN = 10
_HEALTH_YELLOW = 6

# Azure budget warning threshold (matches health_scan.BUDGET_WARNING_THRESHOLD_PCT)
_BUDGET_WARNING_PCT = 70


# ── Helpers ────────────────────────────────────────────────────────────────


def _health_colour(score: int | None) -> str:
    """Return a CSS colour class based on a 0–15 health score."""
    if score is None:
        return "score-unknown"
    if score >= _HEALTH_GREEN:
        return "score-green"
    if score >= _HEALTH_YELLOW:
        return "score-yellow"
    return "score-red"


def _esc(value: Any) -> str:
    """HTML-escape a value, converting it to str first."""
    return html_lib.escape(str(value))


def _ci_badge(status: str) -> str:
    mapping = {"success": "✅", "failure": "❌", "none": "⚪"}
    return mapping.get(status, "❓")


# ── Section renderers ──────────────────────────────────────────────────────


def _render_project_table(
    github_data: dict[str, Any],
    scores: dict[str, Any],
) -> str:
    if not github_data:
        return "<p>No project data available.</p>"

    rows = []
    for repo, data in github_data.items():
        s = scores.get(repo, {})
        health = s.get("health")
        colour = _health_colour(health)
        ci = _ci_badge(data.get("ci_status", ""))
        rows.append(
            f"<tr>"
            f"<td>{_esc(repo)}</td>"
            f"<td>{_esc(data.get('open_issues', '?'))}</td>"
            f"<td>{_esc(data.get('bug_count', '?'))}</td>"
            f"<td>{_esc(data.get('open_prs', '?'))}</td>"
            f"<td>{_esc(data.get('commits_7d', '?'))}</td>"
            f"<td>{ci}</td>"
            f"<td>{_esc(s.get('R', '?'))}</td>"
            f"<td>{_esc(s.get('L', '?'))}</td>"
            f"<td>{_esc(s.get('M', '?'))}</td>"
            f"<td class=\"{colour}\">{_esc(health if health is not None else '?')}</td>"
            f"</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>Project</th><th>Issues</th><th>Bugs</th><th>PRs</th>"
        "<th>Commits (7d)</th><th>CI</th>"
        "<th>R</th><th>L</th><th>M</th><th>Health</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody>"
        "</table>"
    )


def _render_cost_section(cost_data: dict[str, Any]) -> str:
    if cost_data.get("total", -1) < 0:
        error = _esc(cost_data.get("error", "unknown"))
        return f"<p class=\"muted\">Cost scan unavailable: {error}</p>"

    total = cost_data["total"]
    budget = cost_data.get("budget", 150)
    projected = cost_data.get("projected")
    remaining = cost_data.get("remaining")
    budget_pct = round(total / budget * 100) if budget > 0 else 0
    over_budget = projected is not None and projected > budget

    if over_budget or (remaining is not None and remaining < 0):
        cost_class = "cost-red"
        badge = "🔴 OVER BUDGET"
    elif budget_pct >= _BUDGET_WARNING_PCT:
        cost_class = "cost-yellow"
        badge = "🟡 Warning"
    else:
        cost_class = "cost-green"
        badge = "💰 On track"

    proj_str = f" (projected: ${_esc(projected)})" if projected is not None else ""
    rem_str = f"Remaining: ${_esc(remaining)}" if remaining is not None else ""

    rows = [
        f"<tr><td>Month-to-date</td><td><span class=\"{cost_class}\">${_esc(total)}</span></td></tr>",
        f"<tr><td>Budget</td><td>${_esc(budget)}{proj_str}</td></tr>",
    ]
    if rem_str:
        rows.append(f"<tr><td>Remaining</td><td>{rem_str}</td></tr>")
    rows.append(f"<tr><td>Status</td><td>{badge}</td></tr>")

    by_rg = cost_data.get("by_resource_group", {})
    if by_rg:
        rg_rows = "".join(
            f"<tr><td>{_esc(rg)}</td><td>${_esc(cost)}</td></tr>"
            for rg, cost in sorted(by_rg.items(), key=lambda x: -x[1])
        )
        rows.append(
            "<tr><td colspan=\"2\"><strong>By resource group</strong></td></tr>"
            + rg_rows
        )

    return "<table>" + "".join(rows) + "</table>"


def _render_url_health(url_health_data: dict[str, Any]) -> str:
    if not url_health_data:
        return ""
    rows = []
    for app, data in url_health_data.items():
        icon = "✅" if data.get("ok") else "❌"
        rows.append(
            f"<tr>"
            f"<td>{_esc(app)}</td>"
            f"<td>{icon} {_esc(data.get('status', '?'))}</td>"
            f"<td>{_esc(data.get('response_ms', '?'))} ms</td>"
            f"<td>{_esc(data.get('size_kb', '?'))} KB</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>App</th><th>Status</th><th>Response</th><th>Size</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody>"
        "</table>"
    )


def _render_telemetry(app_insights_data: dict[str, Any]) -> str:
    if not app_insights_data or app_insights_data.get("error"):
        return ""
    items = []
    for repo, data in app_insights_data.items():
        if not isinstance(data, dict):
            continue
        exc_count = data.get("exception_count", 0)
        fail_count = data.get("failed_request_count", 0)
        if exc_count <= 0 and fail_count <= 0:
            continue
        views = data.get("page_views_24h", 0)
        items.append(
            f"<li><strong>{_esc(repo)}</strong> — "
            f"page views: {_esc(views)}, "
            f"exceptions: {_esc(exc_count)}, "
            f"failed requests: {_esc(fail_count)}"
            f"</li>"
        )
    if not items:
        return ""
    return "<ul>" + "".join(items) + "</ul>"


def _render_list(items: list[Any], ordered: bool = False) -> str:
    if not items:
        return "<p class=\"muted\">None.</p>"
    tag = "ol" if ordered else "ul"
    lis = "".join(f"<li>{_esc(item)}</li>" for item in items)
    return f"<{tag}>{lis}</{tag}>"


# ── Public API ─────────────────────────────────────────────────────────────


def render_html_dashboard(
    github_data: dict[str, Any],
    cost_data: dict[str, Any],
    analysis: dict[str, Any],
    app_insights_data: dict[str, Any] | None = None,
    url_health_data: dict[str, Any] | None = None,
) -> str:
    """Return a self-contained HTML dashboard page from health-scan data.

    All arguments mirror those of ``health_scan.generate_report``.
    The returned string is a complete HTML document that can be saved to a
    file or sent as an e-mail attachment.
    """
    scores = analysis.get("health_scores", {})
    alerts = analysis.get("alerts", [])
    recs = analysis.get("recommendations", [])
    focus = analysis.get("focus_project", "")
    issues = analysis.get("issues_to_create", [])

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    project_table = _render_project_table(github_data, scores)
    cost_section = _render_cost_section(cost_data)
    url_section = _render_url_health(url_health_data or {})
    telemetry_section = _render_telemetry(app_insights_data or {})
    alerts_html = _render_list(alerts)
    recs_html = _render_list(recs, ordered=True)

    issues_rows = "".join(
        f"<tr><td>{_esc(i.get('repo', '?'))}</td><td>{_esc(i.get('title', '?'))}</td></tr>"
        for i in issues
    )
    issues_table = (
        (
            "<table><thead><tr><th>Repo</th><th>Title</th></tr></thead>"
            "<tbody>" + issues_rows + "</tbody></table>"
        )
        if issues
        else "<p class=\"muted\">No issues queued.</p>"
    )

    focus_html = (
        f"<p class=\"focus\">{_esc(focus)}</p>"
        if focus
        else "<p class=\"muted\">No focus set.</p>"
    )

    css = """
        :root { font-family: system-ui, sans-serif; --green: #2da44e; --yellow: #d29922; --red: #cf222e; }
        body { margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328; }
        h1 { font-size: 1.5rem; margin-bottom: 4px; }
        .meta { color: #656d76; font-size: .85rem; margin-bottom: 24px; }
        h2 { font-size: 1.1rem; border-bottom: 1px solid #d0d7de; padding-bottom: 4px; margin-top: 28px; }
        table { border-collapse: collapse; width: 100%; margin-top: 8px; background: #fff;
                border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden; }
        th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #d0d7de; font-size: .9rem; }
        thead th { background: #f0f3f6; font-weight: 600; }
        tr:last-child td { border-bottom: none; }
        .score-green  { color: var(--green);  font-weight: 700; }
        .score-yellow { color: var(--yellow); font-weight: 700; }
        .score-red    { color: var(--red);    font-weight: 700; }
        .score-unknown { color: #656d76; }
        .cost-green  { color: var(--green); }
        .cost-yellow { color: var(--yellow); font-weight: 600; }
        .cost-red    { color: var(--red);    font-weight: 700; }
        .focus { background: #ddf4ff; border-left: 4px solid #0969da; padding: 10px 14px;
                 border-radius: 4px; font-style: italic; }
        ul, ol { padding-left: 1.4em; }
        li { margin-bottom: 4px; font-size: .9rem; }
        .muted { color: #656d76; font-size: .9rem; }
        section { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
                  padding: 16px 20px; margin-bottom: 20px; }
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NauroLabs Dashboard</title>
  <style>{css}</style>
</head>
<body>
  <h1>🤖 NauroLabs Dashboard</h1>
  <p class="meta">Generated {_esc(now)}</p>

  <section>
    <h2>📊 Project Health</h2>
    {project_table}
  </section>

  <section>
    <h2>🎯 Focus This Week</h2>
    {focus_html}
  </section>

  <section>
    <h2>🚨 Alerts</h2>
    {alerts_html}
  </section>

  <section>
    <h2>💡 Recommendations</h2>
    {recs_html}
  </section>

  <section>
    <h2>💲 Azure Costs</h2>
    {cost_section}
  </section>

  {"<section><h2>🌐 Deployed Apps</h2>" + url_section + "</section>" if url_section else ""}

  {"<section><h2>📡 App Telemetry (24h)</h2>" + telemetry_section + "</section>" if telemetry_section else ""}

  <section>
    <h2>📋 Issues to Create</h2>
    {issues_table}
  </section>
</body>
</html>"""
