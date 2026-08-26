"""NauroLabs health scan — runs in autoRefine's GitHub Action.

Scans GitHub repos, Azure costs, App Insights telemetry and deployed URLs,
asks Azure OpenAI to score and recommend, generates a markdown report,
commits it to the governance repo, creates issues for critical findings,
and sends a Telegram summary via agent.notify.

Moved from agentMode/functions/nauro_bot.py (May 2026). Adapted for
GitHub Actions auth: uses GH_TOKEN env var instead of Key Vault, relies
on the `azure/login@v2` action (AZURE_CREDENTIALS) for DefaultAzureCredential.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
GITHUB_OWNER = "samoletovs"
REPORT_REPO = "nauroLabs-github"
REPORT_PATH_PREFIX = "reports/run"
REPORT_BRANCH = "master"
MAX_REPORTS = 10

AZURE_BUDGET_MONTHLY = 150.0  # VS Enterprise monthly credit
BUDGET_WARNING_THRESHOLD_PCT = 70  # yellow 🟡 when spend reaches this % of budget

MANIFEST_URL = (
    "https://raw.githubusercontent.com/samoletovs/nauroLabs-github/master"
    "/config/workspace-manifest.json"
)
_MANIFEST_CACHE_PATH = Path("/tmp/workspace-manifest.json")


# ── Workspace manifest helpers ─────────────────────────────────────────────
def fetch_workspace_manifest(
    url: str = MANIFEST_URL,
    cache_path: Path = _MANIFEST_CACHE_PATH,
) -> dict[str, Any]:
    """Fetch workspace-manifest.json, caching it for this invocation.

    Raises RuntimeError if the remote returns a non-200 status so that
    health-scan fails loudly rather than silently scanning a stale list.
    """
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as fh:
                return json.load(fh)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Manifest cache at %s is unreadable (%s) — re-fetching", cache_path, exc)
            cache_path.unlink(missing_ok=True)

    resp = httpx.get(url, timeout=15, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch workspace manifest from {url}: HTTP {resp.status_code}"
        )

    data: dict[str, Any] = resp.json()
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _project_slug(project: dict[str, Any]) -> str:
    """Return the short name for a project (slug field, else last path segment of repo)."""
    return project.get("slug") or project.get("repo", "").split("/")[-1]


def _build_app_urls(manifest: dict[str, Any]) -> dict[str, str]:
    """Build {slug: url} from active projects using domain+health_path."""
    result: dict[str, str] = {}
    for project in manifest.get("projects", []):
        if project.get("status") == "archived":
            continue
        slug = _project_slug(project)
        domain = project.get("domain")
        health_path = project.get("health_path", "/")
        if slug and domain:
            result[slug] = f"https://{domain}{health_path}"
    return result


def _build_repo_resource_groups(manifest: dict[str, Any]) -> dict[str, str]:
    """Build {slug: resourceGroup} from active projects that declare an Azure RG."""
    result: dict[str, str] = {}
    for project in manifest.get("projects", []):
        if project.get("status") == "archived":
            continue
        slug = _project_slug(project)
        rg = (project.get("azure") or {}).get("resourceGroup")
        if slug and rg:
            result[slug] = rg
    return result


# ── GitHub Scanner ─────────────────────────────────────────────────────────
def _label_names(issue: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name", "")).strip().lower()
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


def _assignee_logins(issue: dict[str, Any]) -> set[str]:
    return {
        str(assignee.get("login", "")).strip().lower()
        for assignee in issue.get("assignees", [])
        if isinstance(assignee, dict)
    }


def _is_autorefine_idea(issue: dict[str, Any]) -> bool:
    """True when the issue is an autoRefine-generated idea memo."""
    if "pull_request" in issue:
        return False

    labels = _label_names(issue)
    if "idea" not in labels:
        return False

    body = str(issue.get("body", "")).strip().lower()
    return "autopilot" in labels or ("source" in body and "autorefine" in body)


# Labels that mean "this was not built", whatever the close reason says. GitHub's
# state_reason is the primary signal, but a closer who used the label and left the
# reason as the default `completed` is still telling us it was not shipped.
_NOT_SHIPPED_LABELS = frozenset({"declined", "duplicate", "rejected", "wontfix", "won't fix", "invalid"})


def _improvement_status(issue: dict[str, Any]) -> str:
    """Map a tracked idea issue to a concise dashboard status.

    A closed issue only counts as shipped when it was closed as *completed*. Until
    2026-08-22 this returned "completed" for every closed issue that merely lacked a
    `declined` label, which silently counted GitHub's `not_planned` state - the
    won't-fix/duplicate close - as delivered work.

    That is not a rounding error. In a five-issue sample from `era-legacy`, two were
    miscounted: #54 (`not_planned`, labelled `duplicate`) and #52 (`not_planned`,
    labelled `rejected`). Both were reported to the dashboard as shipped. The
    published figure of "125 shipped, 204 declined" is therefore an overstatement of
    unknown size, and it is the number the lab uses to judge whether its own
    self-improvement loop is working.

    A loop cannot be improved against an inflated success metric, so the close
    reason is now read directly.
    """
    labels = _label_names(issue)
    assignees = _assignee_logins(issue)
    state = str(issue.get("state", "")).lower()

    if state == "closed":
        # `state_reason` is absent on older issues; treat that as completed, which
        # is what GitHub itself back-fills, rather than inventing a third state.
        reason = str(issue.get("state_reason") or "completed").lower()
        if reason == "not_planned" or labels & _NOT_SHIPPED_LABELS:
            return "declined"
        return "completed"
    if "needs-approval" in labels:
        return "awaiting approval"
    if "approved" in labels or any("copilot" in login for login in assignees):
        return "in progress"
    return "proposed"


def _improvement_actions(issue: dict[str, Any]) -> str:
    """Summarize the visible actions taken on an idea issue."""
    labels = _label_names(issue)
    assignees = _assignee_logins(issue)
    state = str(issue.get("state", "")).lower()
    actions: list[str] = []

    if "approved" in labels:
        actions.append("approved")
    if any("copilot" in login for login in assignees):
        actions.append("assigned to Copilot")
    if "needs-approval" in labels:
        actions.append("awaiting Telegram decision")
    if "declined" in labels:
        actions.append("declined")
    elif state == "closed":
        actions.append("closed")

    comments = int(issue.get("comments") or 0)
    if comments > 0:
        actions.append(f"{comments} comment{'s' if comments != 1 else ''}")

    return ", ".join(actions) if actions else "—"


def _build_improvement_items(issues: list[dict[str, Any]], limit: int = 5) -> list[dict[str, str]]:
    """Normalize recent autoRefine ideas for dashboard rendering."""
    items: list[dict[str, str]] = []
    for issue in issues:
        if not _is_autorefine_idea(issue):
            continue
        items.append(
            {
                "title": str(issue.get("title", "")).replace("[idea]", "").strip(),
                "status": _improvement_status(issue),
                "actions": _improvement_actions(issue),
                "url": str(issue.get("html_url", "")).strip(),
            }
        )
        if len(items) >= limit:
            break
    return items


def scan_github(token: str, repos: list[str]) -> dict[str, Any]:
    """Scan each repo for issues, PRs, recent commits and CI status."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results: dict[str, Any] = {}

    with httpx.Client(headers=headers, timeout=30) as client:
        for repo in repos:
            repo_data: dict[str, Any] = {"repo": repo}
            full_name = f"{GITHUB_OWNER}/{repo}"

            try:
                resp = client.get(
                    f"https://api.github.com/repos/{full_name}/issues",
                    params={"state": "open", "per_page": 30},
                )
                resp.raise_for_status()
                issues = [i for i in resp.json() if "pull_request" not in i]
                repo_data["open_issues"] = len(issues)
                repo_data["bug_count"] = sum(
                    1 for i in issues
                    if any(label["name"] == "bug" for label in i.get("labels", []))
                )
                if issues:
                    oldest = min(i["created_at"] for i in issues)
                    oldest_dt = datetime.datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                    repo_data["oldest_issue_days"] = (
                        datetime.datetime.now(datetime.timezone.utc) - oldest_dt
                    ).days
                else:
                    repo_data["oldest_issue_days"] = 0
            except httpx.HTTPError as e:
                log.warning("Failed to fetch issues for %s: %s", repo, e)
                repo_data["open_issues"] = -1

            try:
                resp = client.get(
                    f"https://api.github.com/repos/{full_name}/issues",
                    params={"state": "all", "labels": "idea", "sort": "updated", "direction": "desc", "per_page": 10},
                )
                resp.raise_for_status()
                repo_data["recent_ideas"] = _build_improvement_items(resp.json())
            except httpx.HTTPError as e:
                log.warning("Failed to fetch tracked ideas for %s: %s", repo, e)
                repo_data["recent_ideas"] = []

            try:
                resp = client.get(
                    f"https://api.github.com/repos/{full_name}/pulls",
                    params={"state": "open", "per_page": 10},
                )
                resp.raise_for_status()
                repo_data["open_prs"] = len(resp.json())
            except httpx.HTTPError:
                repo_data["open_prs"] = -1

            try:
                since = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=7)
                ).isoformat()
                resp = client.get(
                    f"https://api.github.com/repos/{full_name}/commits",
                    params={"since": since, "per_page": 50},
                )
                resp.raise_for_status()
                repo_data["commits_7d"] = len(resp.json())
            except httpx.HTTPError:
                repo_data["commits_7d"] = -1

            try:
                resp = client.get(
                    f"https://api.github.com/repos/{full_name}/actions/runs",
                    params={"per_page": 1},
                )
                resp.raise_for_status()
                runs = resp.json().get("workflow_runs", [])
                if runs:
                    latest = runs[0]
                    repo_data["ci_status"] = latest["conclusion"] or latest["status"]
                    repo_data["ci_name"] = latest["name"]
                else:
                    repo_data["ci_status"] = "none"
            except httpx.HTTPError:
                repo_data["ci_status"] = "unknown"

            results[repo] = repo_data

    return results


# ── Azure Cost Scanner ─────────────────────────────────────────────────────
def scan_azure_costs() -> dict[str, Any]:
    """Query Azure Cost Management for current month spend."""
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not subscription_id:
        return {"error": "AZURE_SUBSCRIPTION_ID not set", "total": -1}

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.costmanagement import CostManagementClient

        credential = DefaultAzureCredential()
        cost_client = CostManagementClient(credential)
        scope = f"/subscriptions/{subscription_id}"

        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        query = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "time_period": {
                "from": start_of_month.strftime("%Y-%m-%dT00:00:00+00:00"),
                "to": now.strftime("%Y-%m-%dT23:59:59+00:00"),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                },
                "grouping": [
                    {"type": "Dimension", "name": "ResourceGroupName"},
                ],
            },
        }

        result = cost_client.query.usage(scope=scope, parameters=query)

        costs_by_rg: dict[str, float] = {}
        total = 0.0
        if result.rows:
            for row in result.rows:
                rg_name = row[1] if len(row) > 1 else "unknown"
                cost = float(row[0]) if row[0] else 0.0
                costs_by_rg[rg_name] = round(cost, 2)
                total += cost

        days_elapsed = max((now - start_of_month).days, 1)
        days_in_month = 30
        projected = round(total / days_elapsed * days_in_month, 2)

        return {
            "total": round(total, 2),
            "projected": projected,
            "budget": AZURE_BUDGET_MONTHLY,
            "remaining": round(AZURE_BUDGET_MONTHLY - total, 2),
            "by_resource_group": costs_by_rg,
            "days_elapsed": days_elapsed,
        }
    except Exception as e:
        log.warning("Azure cost scan failed: %s", e)
        return {"error": str(e), "total": -1}


# ── App Insights Scanner ───────────────────────────────────────────────────
def scan_app_insights() -> dict[str, Any]:
    """Query App Insights for exceptions, failed requests and page views (24h)."""
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not subscription_id:
        return {"error": "AZURE_SUBSCRIPTION_ID not set"}

    try:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        token = credential.get_token("https://api.loganalytics.io/.default").token
        mgmt_token = credential.get_token("https://management.azure.com/.default").token
    except Exception as e:
        log.warning("Failed to get Azure tokens: %s", e)
        return {"error": str(e)}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    mgmt_headers = {"Authorization": f"Bearer {mgmt_token}", "Content-Type": "application/json"}

    try:
        with httpx.Client(headers=mgmt_headers, timeout=30) as client:
            resp = client.get(
                f"https://management.azure.com/subscriptions/{subscription_id}"
                "/providers/Microsoft.Insights/components?api-version=2020-02-02"
            )
            if resp.status_code != 200:
                return {"error": f"Failed to list App Insights: {resp.status_code}"}
            components = resp.json().get("value", [])
    except httpx.HTTPError as e:
        log.warning("Failed to list App Insights components: %s", e)
        return {"error": str(e)}

    manifest = fetch_workspace_manifest()
    repo_resource_groups = _build_repo_resource_groups(manifest)

    results: dict[str, Any] = {}
    with httpx.Client(timeout=30) as client:
        for comp in components:
            app_id = comp.get("properties", {}).get("AppId", "")
            name = comp.get("name", "unknown")
            comp_id = comp.get("id", "")
            rg = (
                comp_id.split("/resourceGroups/")[-1].split("/")[0]
                if "/resourceGroups/" in comp_id
                else ""
            )
            repo_name = next(
                (repo for repo, rg_name in repo_resource_groups.items() if rg_name == rg),
                name,
            )
            if not app_id:
                continue

            app_data: dict[str, Any] = {"name": name, "resource_group": rg}

            try:
                resp = client.get(
                    f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                    params={
                        "query": (
                            "exceptions "
                            "| where timestamp > ago(24h) "
                            "| summarize count() by type, outerMessage "
                            "| order by count_ desc "
                            "| take 5"
                        )
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    rows = resp.json().get("tables", [{}])[0].get("rows", [])
                    app_data["exceptions_24h"] = [
                        {"type": r[0], "message": r[1][:100], "count": r[2]} for r in rows
                    ]
                    app_data["exception_count"] = sum(r[2] for r in rows)
                else:
                    app_data["exceptions_24h"] = []
                    app_data["exception_count"] = 0
            except httpx.HTTPError:
                app_data["exceptions_24h"] = []
                app_data["exception_count"] = -1

            try:
                resp = client.get(
                    f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                    params={
                        "query": (
                            "requests "
                            "| where timestamp > ago(24h) and success == false "
                            "| summarize failCount=count(), "
                            "  avgDuration=avg(duration) by name, resultCode "
                            "| order by failCount desc "
                            "| take 5"
                        )
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    rows = resp.json().get("tables", [{}])[0].get("rows", [])
                    app_data["failed_requests_24h"] = [
                        {"endpoint": r[0], "status": r[1], "count": r[2], "avg_ms": round(r[3])}
                        for r in rows
                    ]
                    app_data["failed_request_count"] = sum(r[2] for r in rows)
                else:
                    app_data["failed_requests_24h"] = []
                    app_data["failed_request_count"] = 0
            except httpx.HTTPError:
                app_data["failed_requests_24h"] = []
                app_data["failed_request_count"] = -1

            try:
                resp = client.get(
                    f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                    params={
                        "query": (
                            "pageViews "
                            "| where timestamp > ago(24h) "
                            "| summarize views=count(), avgDuration=avg(duration) "
                        )
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    rows = resp.json().get("tables", [{}])[0].get("rows", [])
                    if rows:
                        app_data["page_views_24h"] = rows[0][0]
                        app_data["avg_page_load_ms"] = round(rows[0][1]) if rows[0][1] else 0
                    else:
                        app_data["page_views_24h"] = 0
                        app_data["avg_page_load_ms"] = 0
            except httpx.HTTPError:
                app_data["page_views_24h"] = -1

            results[repo_name] = app_data

    return results


# ── URL Health Checker ─────────────────────────────────────────────────────
def check_deployed_urls() -> dict[str, Any]:
    """HTTP GET each deployed URL and capture status + latency."""
    manifest = fetch_workspace_manifest()
    app_urls = _build_app_urls(manifest)
    results: dict[str, Any] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for repo, url in app_urls.items():
            try:
                resp = client.get(url)
                results[repo] = {
                    "url": url,
                    "status": resp.status_code,
                    "ok": 200 <= resp.status_code < 400,
                    "response_ms": round(resp.elapsed.total_seconds() * 1000),
                    "size_kb": round(len(resp.content) / 1024, 1),
                }
            except httpx.HTTPError as e:
                results[repo] = {
                    "url": url,
                    "status": 0,
                    "ok": False,
                    "response_ms": -1,
                    "error": str(e)[:100],
                }
    return results


# ── AI Analysis ────────────────────────────────────────────────────────────
def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unusable.

    This is read outside the request's ``try``, so a typo'd value raising here
    would abort the whole scan — the failure mode this module is being hardened
    against. A bad knob should cost the knob, not the run.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — using %d", name, raw, default)
        return default
    if value <= 0:
        log.warning("%s=%d must be positive — using %d", name, value, default)
        return default
    return value


def _parse_analysis_reply(raw: str) -> dict[str, Any]:
    """Extract the analysis object from a model reply.

    Raises ValueError if no JSON object can be found, TypeError if the reply
    parses to something that is not an object.

    The reply is meant to be bare JSON, but models wrap it in a markdown fence
    or top and tail it with prose regardless. The previous unwrapper was
    ``raw.split("```")[1]``, which keeps only the span between the first two
    fences — so a reply whose issue body contained its own code fence was
    truncated mid-string and the whole sweep was discarded. That is not a
    corner case: the system prompt asks for issue bodies about "recurring JS
    exceptions with stack traces", which is exactly the content a model fences.

    So: try the raw text first (the documented, common case, and free), then
    fall back to the outermost ``{``…``}`` span. Taking the outermost braces is
    what makes an inner fence harmless — anything nested, fences included, is
    inside the span by construction.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("model returned an empty reply")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(
                f"no JSON object found in model reply: {raw[:120]!r}"
            ) from None
        parsed = json.loads(raw[start : end + 1])

    if not isinstance(parsed, dict):
        # Valid JSON, wrong shape. Every caller does analysis.get(...), so
        # letting a list or a bare string through crashes the sweep in
        # generate_report before the report is committed or Telegram is sent.
        raise TypeError(
            f"model returned {type(parsed).__name__}, expected a JSON object"
        )
    return parsed


def analyze_with_ai(
    github_data: dict[str, Any],
    cost_data: dict[str, Any],
    app_insights_data: dict[str, Any] | None = None,
    url_health_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask Azure OpenAI / Foundry to score projects and recommend fixes."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    if not endpoint:
        return {"error": "AZURE_OPENAI_ENDPOINT not set", "recommendations": []}

    from openai import AzureOpenAI

    api_key = os.environ.get("AZURE_OPENAI_KEY", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    if api_key:
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            api_key=api_key,
        )
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token_provider=token_provider,
        )

    # Resolve model. Default still ``gpt-4o-mini`` for cost-discipline on the
    # daily 06:00/18:00 health scan, but the deployment is configurable via
    # ``HEALTH_SCAN_MODEL``. The deep-analysis variant of this scan can be
    # bumped to ``gpt-5`` or another high-tier deployment per AGENTS.md
    # "Model strategy".
    model = os.environ.get("HEALTH_SCAN_MODEL", "gpt-4o-mini")

    # Output ceiling. The old value of 2000 was not enough for a full-fleet
    # answer: tokenised with o200k_base, the schema above for 24 repos with the
    # 5 issues create_github_issues will actually file (each with a stack-trace
    # body, as the prompt asks for) measures 2035 tokens compact and 2312
    # indented. health_scores alone costs 676 for 24 repos. Because
    # issues_to_create is emitted last, the overflow lands exactly on the part
    # that files issues, and the failure is silent. max_tokens is a ceiling and
    # not a reservation, so raising it bills nothing on the runs that don't need
    # it — and output is the cheap side of this workload anyway (AGENTS.md: 425M
    # input against 0.79M output).
    max_tokens = _int_env("HEALTH_SCAN_MAX_TOKENS", 4000)

    system_prompt = """You are NauroLabs' automated health analyst. Analyze project data, Azure costs, app telemetry, and URL health.

SCORING (per project):
- R (Revenue potential): 1-5
- L (Learning value): 1-5
- M (Momentum/activity): 1-5
- Health = R + L + M (max 15)

RULES:
- Bugs with open issues = lower health
- Failed CI = critical alert
- Projects with 0 commits in 7 days = momentum = 1
- Cost > $20/project/month = flag for review
- Total projected > $150 = budget alert
- SELF-IMPROVEMENT: Recurring exceptions or failed requests = create an issue with the error details so Copilot can fix it
- URL returning non-200 or response time > 3000ms = critical alert
- Page views = 0 for a deployed app = flag (nobody using it)

Respond with EXACTLY this JSON (no markdown):
{
  "health_scores": {"repo_name": {"R": 1-5, "L": 1-5, "M": 1-5, "health": 3-15}},
  "alerts": ["critical issues requiring attention"],
  "recommendations": ["top 3 actionable recommendations"],
  "focus_project": "which project to focus on this week and why",
  "issues_to_create": [{"repo": "repo_name", "title": "issue title", "body": "issue description", "labels": ["label"]}]
}

For issues_to_create: include genuinely actionable items that Copilot can auto-implement.
ESPECIALLY create issues for:
- Recurring JS exceptions with stack traces
- Failed API endpoints returning 500 errors
- Slow pages (avg load > 3s)
- Broken URLs returning non-200 status
Do NOT create issues for: subjective improvements, architecture decisions, or issues already in the open issues list."""

    user_msg = (
        f"GitHub data:\n{json.dumps(github_data, indent=2)}\n\n"
        f"Azure costs:\n{json.dumps(cost_data, indent=2)}\n\n"
        f"App Insights telemetry (last 24h):\n"
        f"{json.dumps(app_insights_data or {}, indent=2)}\n\n"
        f"Deployed URL health:\n{json.dumps(url_health_data or {}, indent=2)}\n\n"
        f"Date: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        choice = response.choices[0]
        raw = (choice.message.content or "").strip()

        # A truncated reply is cut off mid-structure, so it fails to parse and
        # the sweep files nothing. Say so plainly instead of reporting it as a
        # generic JSON error — the cause and the fix are different.
        if getattr(choice, "finish_reason", None) == "length":
            log.error(
                "AI analysis hit the %d-token output ceiling and was truncated "
                "mid-reply — no issues will be filed this run. Raise "
                "HEALTH_SCAN_MAX_TOKENS.",
                max_tokens,
            )
            raise ValueError(
                f"model reply truncated at max_tokens={max_tokens}"
            )

        return _parse_analysis_reply(raw)
    except Exception as e:
        log.error("AI analysis failed: %s", e)
        return {"error": str(e), "recommendations": [], "alerts": []}


# ── Report Generator ───────────────────────────────────────────────────────
def analysis_failed(analysis: dict[str, Any]) -> bool:
    """True when the AI analysis did not produce a usable answer.

    ``analyze_with_ai`` degrades to ``{"error": ...}`` on any failure. Without
    checking that, a failed analysis is indistinguishable from a clean one:
    both render empty alerts, no recommendations and ``?`` scores. Those two
    states demand opposite responses from whoever reads the report, so they
    must not look alike.
    """
    return bool(analysis.get("error"))


def generate_report(
    github_data: dict[str, Any],
    cost_data: dict[str, Any],
    analysis: dict[str, Any],
    app_insights_data: dict[str, Any] | None = None,
    url_health_data: dict[str, Any] | None = None,
) -> str:
    """Build a Markdown report from the scan + analysis results."""
    now = datetime.datetime.now(datetime.timezone.utc)
    report: list[str] = [
        f"# NauroLabs Health Report — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
    ]

    failed = analysis_failed(analysis)
    if failed:
        # Immediately under the title, before anything that looks like data.
        # No "- " bullets here: build_telegram_summary scrapes those as alerts.
        report.append("## ⚠️ AI ANALYSIS FAILED — THIS REPORT IS INCOMPLETE\n")
        report.append(
            "The model call did not return a usable answer, so health scores, "
            "alerts, recommendations and the focus project are **missing** — "
            "not empty. Scores below render as `?` for that reason. The scanned "
            "GitHub, cost and URL data is still accurate.\n"
        )
        report.append(f"> `{analysis.get('error', 'unknown error')}`\n")
        report.append("No issues were filed this run.\n")

    alerts = analysis.get("alerts", [])
    if alerts:
        report.append("## 🚨 Alerts\n")
        for a in alerts:
            report.append(f"- {a}")
        report.append("")
    elif not failed:
        # Say the healthy case out loud, so silence is never the only evidence.
        report.append("## ✅ No alerts — analysis ran and found nothing critical\n")

    focus = analysis.get("focus_project", "")
    if focus:
        report.append(f"## 🎯 This Week: {focus}\n")

    report.append("## Project Health\n")
    report.append(
        "| Project | Issues | Bugs | PRs | Commits(7d) | CI | R | L | M | Health |"
    )
    report.append(
        "|---------|--------|------|-----|-------------|-----|---|---|---|--------|"
    )
    scores = analysis.get("health_scores", {})
    for repo, data in github_data.items():
        s = scores.get(repo, {})
        ci_emoji = {"success": "✅", "failure": "❌", "none": "⚪"}.get(
            data.get("ci_status", ""), "❓"
        )
        report.append(
            f"| {repo} | {data.get('open_issues', '?')} "
            f"| {data.get('bug_count', '?')} | {data.get('open_prs', '?')} "
            f"| {data.get('commits_7d', '?')} | {ci_emoji} "
            f"| {s.get('R', '?')} | {s.get('L', '?')} | {s.get('M', '?')} "
            f"| {s.get('health', '?')} |"
        )
    report.append("")

    if any(data.get("recent_ideas") for data in github_data.values()):
        report.append("## Improvement Tracking\n")
        report.append("| Project | Suggestion | Status | Actions |")
        report.append("|---------|------------|--------|---------|")
        for repo, data in github_data.items():
            for item in data.get("recent_ideas", []):
                title = item.get("title", "Untitled improvement")
                url = item.get("url", "")
                suggestion = f"[{title}]({url})" if url else title
                report.append(
                    f"| {repo} | {suggestion} | {item.get('status', 'proposed')} "
                    f"| {item.get('actions', '—')} |"
                )
        report.append("")

    report.append("## Azure Costs\n")
    if cost_data.get("total", -1) >= 0:
        report.append(f"- **Month-to-date:** ${cost_data['total']}")
        report.append(f"- **Projected:** ${cost_data.get('projected', '?')}")
        report.append(f"- **Budget:** ${cost_data.get('budget', 150)}")
        report.append(f"- **Remaining:** ${cost_data.get('remaining', '?')}")
        report.append("")
        by_rg = cost_data.get("by_resource_group", {})
        if by_rg:
            report.append("| Resource Group | Cost |")
            report.append("|---------------|------|")
            for rg, cost in sorted(by_rg.items(), key=lambda x: -x[1]):
                report.append(f"| {rg} | ${cost} |")
            report.append("")
    else:
        report.append(f"- Cost scan unavailable: {cost_data.get('error', 'unknown')}\n")

    if url_health_data:
        report.append("## Deployed Apps\n")
        report.append("| App | Status | Response (ms) | Size (KB) |")
        report.append("|-----|--------|--------------|-----------|")
        for repo, data in url_health_data.items():
            status_icon = "✅" if data.get("ok") else "❌"
            report.append(
                f"| {repo} | {status_icon} {data.get('status', '?')} "
                f"| {data.get('response_ms', '?')} | {data.get('size_kb', '?')} |"
            )
        report.append("")

    if app_insights_data and not app_insights_data.get("error"):
        has_issues = any(
            isinstance(d, dict)
            and (d.get("exception_count", 0) > 0 or d.get("failed_request_count", 0) > 0)
            for d in app_insights_data.values()
        )
        if has_issues:
            report.append("## App Telemetry (24h)\n")
            for repo, data in app_insights_data.items():
                if not isinstance(data, dict):
                    continue
                exceptions = data.get("exception_count", 0)
                failed_reqs = data.get("failed_request_count", 0)
                views = data.get("page_views_24h", 0)
                if exceptions > 0 or failed_reqs > 0:
                    report.append(f"### {repo}")
                    report.append(
                        f"- Page views: {views} | Exceptions: {exceptions} "
                        f"| Failed requests: {failed_reqs}"
                    )
                    for exc in data.get("exceptions_24h", [])[:3]:
                        report.append(
                            f"  - `{exc.get('type', '?')}`: {exc.get('message', '?')} "
                            f"(×{exc.get('count', '?')})"
                        )
                    for fr in data.get("failed_requests_24h", [])[:3]:
                        report.append(
                            f"  - `{fr.get('endpoint', '?')}` → {fr.get('status', '?')} "
                            f"(×{fr.get('count', '?')}, avg {fr.get('avg_ms', '?')}ms)"
                        )
                    report.append("")

    recs = analysis.get("recommendations", [])
    if recs:
        report.append("## Recommendations\n")
        for i, r in enumerate(recs, 1):
            report.append(f"{i}. {r}")
        report.append("")

    issues = analysis.get("issues_to_create", [])
    if issues:
        report.append("## Auto-Created Issues\n")
        for iss in issues:
            report.append(f"- [{iss.get('repo')}] {iss.get('title')}")
        report.append("")

    return "\n".join(report)


# ── GitHub: commit report + create issues + prune ──────────────────────────
def commit_report(token: str, report_content: str) -> str | None:
    """Commit the report to the governance repo and return its path."""
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    file_name = f"run-{date_str}-{now.strftime('%H%M')}.md"
    file_path = f"{REPORT_PATH_PREFIX}/{file_name}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    content_b64 = base64.b64encode(report_content.encode()).decode()

    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{REPORT_REPO}/contents/{file_path}"
        )
        sha = resp.json().get("sha") if resp.status_code == 200 else None

        body: dict[str, Any] = {
            "message": f"chore(autorefine): health report {date_str}",
            "content": content_b64,
            "branch": REPORT_BRANCH,
        }
        if sha:
            body["sha"] = sha

        resp = client.put(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{REPORT_REPO}/contents/{file_path}",
            json=body,
        )
        if resp.status_code in (200, 201):
            log.info("Report committed: %s", file_path)
            return file_path
        log.error("Failed to commit report: %s %s", resp.status_code, resp.text[:300])
        return None


def enforce_report_retention(token: str) -> None:
    """Delete oldest reports if more than MAX_REPORTS exist."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{REPORT_REPO}/contents/{REPORT_PATH_PREFIX}"
        )
        if resp.status_code != 200:
            return
        files = sorted(resp.json(), key=lambda f: f["name"])
        while len(files) > MAX_REPORTS:
            oldest = files.pop(0)
            client.request(
                "DELETE",
                f"https://api.github.com/repos/{GITHUB_OWNER}/{REPORT_REPO}/contents/{oldest['path']}",
                json={
                    "message": f"chore(autorefine): prune old report {oldest['name']}",
                    "sha": oldest["sha"],
                    "branch": REPORT_BRANCH,
                },
            )
            log.info("Pruned old report: %s", oldest["name"])


def create_github_issues(
    token: str,
    issues: list[dict[str, Any]],
    allowed_repos: list[str],
    assign_copilot: bool = True,
) -> list[str]:
    """Create GitHub issues for critical findings."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    created: list[str] = []
    with httpx.Client(headers=headers, timeout=30) as client:
        for issue in issues[:5]:
            repo = issue.get("repo", "")
            if repo not in allowed_repos:
                continue
            body = {
                "title": f"🔧 Tech Debt: {issue.get('title', 'Untitled')}",
                "body": (
                    f"{issue.get('body', '')}\n\n"
                    f"---\n*Auto-created by autoRefine health scan*"
                ),
                "labels": issue.get("labels", ["tech-debt", "autorefine"]),
            }
            resp = client.post(
                f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/issues",
                json=body,
            )
            if resp.status_code == 201:
                issue_data = resp.json()
                url = issue_data.get("html_url", "")
                created.append(url)
                log.info("Created issue: %s", url)
                issue_num = issue_data.get("number")
                if assign_copilot and issue_num:
                    assign_result = subprocess.run(
                        [
                            "gh",
                            "issue",
                            "edit",
                            str(issue_num),
                            "--repo",
                            f"{GITHUB_OWNER}/{repo}",
                            "--add-assignee",
                            "copilot",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if assign_result.returncode != 0:
                        log.warning(
                            "Copilot assignment failed for %s#%s: %s",
                            repo,
                            issue_num,
                            (assign_result.stderr or assign_result.stdout or "").strip(),
                        )
            else:
                log.warning(
                    "Failed to create issue in %s: %s", repo, resp.text[:200]
                )
    return created


# ── Telegram summary ───────────────────────────────────────────────────────
def build_telegram_summary(
    analysis: dict[str, Any],
    report_path: str | None,
    created_issues: list[str],
    cost_data: dict[str, Any] | None = None,
) -> str:
    """Compose a short Telegram message from the analysis results.

    Alerts and the focus project are read from *analysis* directly. They used
    to be recovered by scraping the rendered markdown for lines starting with
    ``"- "``, which is how the Azure cost bullets ended up in the alert slot:
    the summary was reconstructing structure from a rendering of that same
    structure, so anything shaped like a bullet qualified. The structured data
    was always available; the report text is no longer an input at all, which
    is what makes the bug unreintroducible rather than merely fixed.

    When *cost_data* is provided (and not an error), a one-line Azure
    cost/budget summary is included so the recipient can see spending without
    opening the full report. It is labelled and rendered as a single line, so
    it cannot be mistaken for a finding.

    Three outcomes must stay visibly distinct here, because this message is the
    only part a human reliably reads: the analysis failed, the analysis ran and
    found nothing, and the analysis ran and found alerts.
    """
    parts: list[str] = ["🤖 <b>NauroLabs Health Report</b>"]

    failed = analysis_failed(analysis)
    if failed:
        parts.append("⚠️ <b>AI analysis FAILED — report is incomplete</b>")
        parts.append(f"<i>{str(analysis.get('error', 'unknown error'))[:180]}</i>")
        parts.append("No scores, no alerts, no issues filed this run.")

    focus = analysis.get("focus_project", "")
    if focus and not failed:
        parts.append(f"🎯 This Week: {focus}")

    if cost_data and cost_data.get("total", -1) >= 0:
        total = cost_data["total"]
        budget = cost_data.get("budget", AZURE_BUDGET_MONTHLY)
        projected = cost_data.get("projected")
        remaining = cost_data.get("remaining")
        budget_pct = round(total / budget * 100) if budget > 0 else 0
        over_budget = projected is not None and projected > budget
        cost_icon = "🔴" if over_budget else ("🟡" if budget_pct >= BUDGET_WARNING_THRESHOLD_PCT else "💰")
        cost_line = f"{cost_icon} Azure: ${total} used"
        if projected is not None:
            cost_line += f" (projected ${projected})"
        cost_line += f" / ${budget} budget"
        if over_budget or (remaining is not None and remaining < 0):
            cost_line += " — <b>⚠️ OVER BUDGET</b>"
        parts.append(cost_line)

    if not failed:
        alerts = analysis.get("alerts", []) or []
        if alerts:
            parts.append(f"🚨 <b>{len(alerts)} alert(s)</b>")
            # Each alert is prefixed, so an alert can never be confused with
            # the cost line above or with any other bullet in the message.
            parts.extend(f"🚨 {a}" for a in alerts[:3])
            if len(alerts) > 3:
                parts.append(f"…and {len(alerts) - 3} more — see the full report")
        else:
            parts.append("✅ No alerts")

    if created_issues:
        parts.append(f"📋 Created {len(created_issues)} tech-debt issue(s)")
    if report_path:
        parts.append(
            f'<a href="https://github.com/{GITHUB_OWNER}/{REPORT_REPO}/blob/{REPORT_BRANCH}/{report_path}">Full report</a>'
        )
    return "\n".join(parts)


# ── Entry point ────────────────────────────────────────────────────────────
def run_health_scan(repos: list[str], assign_copilot: bool = True) -> dict[str, Any]:
    """Execute the full health scan pipeline.

    Returns a summary dict with report_path, created_issues, scan stats.
    Raises ValueError if GH_TOKEN is missing.
    """
    from agent.notify import send_telegram

    github_token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise ValueError("GH_TOKEN environment variable not set")

    log.info("Starting NauroLabs health scan over %d repos", len(repos))

    github_data = scan_github(github_token, repos)
    log.info("GitHub scan complete: %d repos", len(github_data))

    cost_data = scan_azure_costs()
    log.info("Azure cost scan complete: total=$%s", cost_data.get("total", "?"))

    app_insights_data = scan_app_insights()
    log.info(
        "App Insights scan complete: %d apps",
        len(app_insights_data) if isinstance(app_insights_data, dict) else 0,
    )

    url_health_data = check_deployed_urls()
    log.info("URL health check complete: %d URLs", len(url_health_data))

    analysis = analyze_with_ai(github_data, cost_data, app_insights_data, url_health_data)
    if analysis_failed(analysis):
        log.error(
            "AI analysis failed — the report will be marked incomplete and no "
            "issues will be filed: %s",
            analysis.get("error"),
        )
    else:
        log.info("AI analysis complete")

    report = generate_report(
        github_data, cost_data, analysis, app_insights_data, url_health_data
    )

    report_path = commit_report(github_token, report)
    try:
        enforce_report_retention(github_token)
    except Exception as exc:  # never let pruning kill notifications
        log.warning("Report retention skipped: %s", exc)

    created_issues = create_github_issues(
        github_token,
        analysis.get("issues_to_create", []),
        repos,
        assign_copilot=assign_copilot,
    )

    summary = build_telegram_summary(
        analysis, report_path, created_issues, cost_data=cost_data
    )
    send_telegram(summary, parse_mode="HTML")

    log.info(
        "Health scan complete: report=%s, issues_created=%d",
        report_path,
        len(created_issues),
    )

    return {
        "report_path": report_path,
        "created_issues": created_issues,
        "github_repos_scanned": len(github_data),
        "urls_checked": len(url_health_data),
        "telegram_summary": summary,
        "analysis_failed": analysis_failed(analysis),
    }
