# autoRefine

> Projects that improve themselves.

An AI agent powered by [Microsoft Foundry](https://learn.microsoft.com/en-us/azure/ai-foundry/) that continuously evaluates and improves software projects. Goes beyond linting and dependency updates — autoRefine understands your project's vision, compares with similar products listed in project context, identifies gaps, creates improvement plans, and executes changes with tests.

## How it works

```
project.yaml → Discover → Research → Evaluate → Plan → [Ask?] → Execute → Test → PR
```

1. **Discover** — reads `project.yaml` + code structure to understand what the project is
2. **Research** — compares with similar products listed in project context
3. **Evaluate** — technical quality checks + functional gap analysis
4. **Plan** — creates a prioritized improvement plan
5. **Ask** — when confidence is low, asks the user before proceeding
6. **Execute** — makes changes to the codebase
7. **Test** — runs the project's test suite to validate changes
8. **PR** — creates a pull request with the improvements

> ⚠️ **Closed-loop warning:** `--mode refine` bypasses the evaluator→builder loop by opening PRs directly. Prefer `--mode file-ideas` so autoRefine files idea memos and lets the Builder implement them.

## Getting started

### 1. Install dependencies

Use Python 3.11 or newer and install the project requirements from the repository root:

```bash
python --version
python -m pip install -r requirements.txt
```

### 2. Add a `project.yaml` to your repo

```yaml
name: my-project
purpose: "One-line description of what this project does"
users: "Who uses this"
stage: active          # idea | research | mvp | active | complete | archived
goals:
  - "Key goal 1"
  - "Key goal 2"
similar:               # products to research for inspiration
  - "Competitor A"
  - "Competitor B"
quality:               # traits you care about
  - tests
  - ci-cd
  - responsive
  - i18n
```

### 3. Configure

```bash
cp .env.example .env
```

Set `FOUNDRY_PROJECT_ENDPOINT` for Foundry planning modes, authenticate with Azure
(for example, run `az login` locally), and set `GH_TOKEN` or `GITHUB_TOKEN` for
GitHub access. Only configure the credentials needed for the mode you plan to run.
Keep `.env` local; it contains credentials and must not be committed.

### 4. Run

```bash
# Evaluate a single project
python -m agent.main --repo owner/repo --mode evaluate

# Evaluate + plan + file idea memos (closed loop)
python -m agent.main --repo owner/repo --mode file-ideas

# Full cycle (evaluate + plan + execute) — local dev only, bypasses the loop
python -m agent.main --repo owner/repo --mode refine

# All projects in a manifest
python -m agent.main --manifest config/workspace-manifest.json --mode file-ideas

# Open the self-guided setup guide (no repository access required)
python -m agent.main --mode onboarding --output onboarding.html

# Generate a browser dashboard with health, ideas, and progress tracking
python -m agent.main --repo owner/repo --mode dashboard --output dashboard.html
```

The onboarding guide walks through installation, `project.yaml`, evaluation, and
planning. It saves progress and clarity feedback in the browser; use its download
button to export feedback JSON for usability review. No feedback is sent automatically.

`--model` selects the deployment for `plan`, `refine`, and both technical and functional
planning in `file-ideas`. It overrides `FOUNDRY_DEFAULT_DEPLOYMENT`; the
default is `gpt-6-luna`, the only enabled deployment. Sol and retired models
fail before work starts (see [Models](#models)).

`file-ideas --dry-run` suppresses issue creation and Telegram notifications, but can
still use Foundry to generate plans. The governance filer must support `--repo` and
any requested `--dry-run` or `--needs-approval` flag; missing capabilities stop filing
rather than silently dropping these safeguards. `health-scan --dry-run` reads services
and runs AI analysis (still billed), but does not persist/prune reports, create/assign
issues, or send Telegram. Its JSON includes the proposed report and issues. The health
workflow's `dry_run` dispatch input also dry-runs the PR sweep and suppresses failure
notifications. Identity configuration still needs
operator approval.

## Usage

These examples assume dependencies are installed and the environment is configured as
described above. Replace `owner/repo` with a GitHub repository you can access.

### Evaluate a repository

Run the deterministic checks and inspect the JSON report:

```bash
python -m agent.main --repo owner/repo --mode evaluate
```

The output is a JSON object containing the project name, score, findings, measurement
coverage, and feature suggestions.

### Generate an improvement plan

Ask Foundry to turn the evaluation findings into a prioritized plan:

```bash
python -m agent.main --repo owner/repo --mode plan
```

The output includes the evaluation report, followed by an `--- IMPROVEMENT PLAN ---`
section with a summary and proposed improvements. This calls Foundry and may incur
usage charges.

### Preview idea filing

Generate a plan without creating GitHub issues or sending Telegram notifications:

```bash
python -m agent.main --repo owner/repo --mode file-ideas --dry-run
```

The command prints the evaluation report and runs planning, but suppresses issue creation
and Telegram delivery. Foundry is still called and may incur usage charges.

### Evaluate a workspace manifest

Evaluate every repository listed in a workspace manifest:

```bash
python -m agent.main --manifest config/workspace-manifest.json --mode evaluate
```

The command prints one JSON evaluation report per repository. If any repository fails,
it continues with the others and exits nonzero with a final `run_status: "failed"` object
listing the failed repositories.

### Health alert evidence

Health alerts use measured workflow outcomes, confirmed URL checks and numeric
budget comparisons. AI still supplies scores, focus and advice, but cannot invent
alert text or automatic issue bodies. Repair proposals must reference an exact
finding ID; unknown IDs and budget-only work are rejected. Open findings are
deduplicated before filing, and a majority failure affecting at least three URLs
becomes one monitor investigation rather than one paid agent assignment per app.

URL checks retry slow responses (over 3,000ms), HTTP 5xx and transport errors up
to three attempts, two seconds apart. Reports and dashboards keep all samples:
recovery is visible, not silently discarded or claimed to prove a cold start.
Persistent errors/slowness still alert; HTTP 4xx is not retried. No minimum replica
count, credentials, schedule or spending limit is changed.

The workflow column describes the **latest workflow**, with its actual name,
not aggregate CI health. A failed scheduled check-in is still reported as a
failure, but is not called a failed build. Health probes only cover the URL or
`health_path` declared in the manifest; a working homepage does not prove every
backend dependency is healthy.

Measured alerts also survive an AI outage; scores/advice and automatic issue
creation remain unavailable until analysis recovers. Repeated HTTP 5xx telemetry
(at least two requests for a recorded endpoint/status) alerts even if the homepage
works. Exception counts remain observations in the report, not proof of an outage.

`refine` requires a clean worktree before agent work and a passing deterministic
final test run before publishing. A failed or unavailable test runner blocks publication
and rolls back the run's edits, including files in new directories. Interruptions and
exceptions before validation also attempt rollback before propagating. An unreadable
Git status is an error, never a clean worktree. Dry runs do not publish or perform this
final test gate.

Per-project exceptions and failed clones do not stop the remaining projects in a sweep.
After those attempts, the CLI appends a `run_status: failed` object listing `failed_repos`
and exits nonzero. Existing score objects remain available, but the evaluate workflow
labels them as partial when execution failed. This does not change the Container Apps
entrypoint's separate best-effort exit policy or automatically retry the sweep.

## Project structure

```
autoRefine/
├── agent/
│   ├── main.py              # Entry point and orchestrator
│   ├── config.py            # Configuration and project.yaml parsing
│   ├── prompts/
│   │   ├── system.md        # Agent system prompt
│   │   ├── evaluate.md      # Evaluation prompt template
│   │   └── plan.md          # Planning prompt template
│   └── tools/
│       ├── github_tools.py  # Clone, read files, create PRs
│       ├── research_tools.py# Web search for similar products
│       ├── quality_tools.py # Technical quality checks
│       └── execute_tools.py # Code modification and test runner
├── tests/
├── scripts/
├── docs/
├── infrastructure/          # Bicep for Foundry project
├── project.yaml
├── requirements.txt
├── pyproject.toml
└── .env.example
```

## Architecture

autoRefine is a **Foundry hosted agent** with function-calling tools:

- **Model**: `gpt-6-luna` for Foundry agent planning and health Chat.
  Sol, including explicit manual Sol plans, remains blocked.
  The closed-loop PR reviewer (deep-review) lives in `samoletovs/nauroLabs-github`
  and uses Claude Opus 4 (via GitHub Models) — see that repo's AGENT_ROLES.md.
- **Tools**: GitHub API, file system, test runner, quality checkers
- **Human-in-the-loop**: agent asks for confirmation on risky or uncertain changes
- **Safety**: all changes are made on branches, tested, and submitted as PRs — never direct to main

## Stack

- Python 3.11+
- Microsoft Foundry (Azure AI Projects SDK)
- Azure OpenAI (`gpt-6-luna`)
- GitHub API (`gh` CLI + REST)
- PyYAML for `project.yaml` parsing

## Models

Every Azure OpenAI path in this repo uses **`gpt-6-luna`** (`foundrylab-aiservices`,
model version 2026-09-22). `gpt-4o`, `gpt-4o-mini` and `gpt-4.1*` retire on
2027-04-14 and are no longer accepted anywhere: not as code default, allowlist,
`--model` override, example environment or IaC. `gpt-4.1` has no announced
replacement. The prepared `gpt-6-sol` deep path stays **blocked**; nothing here
enables it.

| Surface | Deployment | Configuration |
|---|---|---|
| Health analyst, Chat Completions | `gpt-6-luna` | `AZURE_OPENAI_ENDPOINT=https://foundrylab-aiservices.cognitiveservices.azure.com/`, `HEALTH_SCAN_MODEL=gpt-6-luna` |
| Foundry agent planning | `gpt-6-luna` | `FOUNDRY_PROJECT_ENDPOINT=https://foundrylab-aiservices.services.ai.azure.com/api/projects/foundrylab`, `FOUNDRY_DEFAULT_DEPLOYMENT=gpt-6-luna` |

Luna request contract, enforced by `tests/test_model_migration.py`:

- Health Chat sends `max_completion_tokens<=4000` and `reasoning_effort=none`,
  never `max_tokens` (HTTP 400 on luna) or temperature. `HEALTH_SCAN_MAX_TOKENS`
  may lower but not raise the ceiling. `AZURE_OPENAI_DEPLOYMENT` is not used.
- Prompt-agent definitions keep planning's existing `temperature=0.3` and pair it
  with `reasoning.effort=none` (no `top_p`). Measured 2026-11-09: luna accepts and
  applies temperature/top_p only under reasoning effort none; without it, every
  `responses.create(agent_reference=...)` fails with HTTP 400 "Unsupported
  parameter: 'temperature'". Changing the model and adding reasoning changes the
  definition hash, so the next run publishes a new `autorefine-plan` version.

Foundry planning runs on the Foundry Agent Service API (`azure-ai-projects` 2.x):
persistent, versioned prompt agents `autorefine-plan` / `autorefine-refine`, driven
through the project's Responses API (`agent_reference` pinned to an exact version,
tool results chained with `previous_response_id`). The classic `azure-ai-agents`
threads/runs surface was removed on 2026-10-08 ahead of its 2027-03-31 retirement.
Runs keep run-wide input/output ceilings, summed locally over every response:
**200,000 / 16,000**. `AUTOREFINE_MAX_PROMPT_TOKENS` and
`AUTOREFINE_MAX_COMPLETION_TOKENS` can lower these. The classic 12-message window
has no Responses equivalent (`truncation="auto"` is used and
`AUTOREFINE_TRUNCATION_LAST_MESSAGES` is ignored with a warning; see AGENTS.md "What
the sweep actually costs"). The 1,800-second run deadline, round/repetition guards,
cleanup and fail-closed plan contracts are unchanged.

Run telemetry includes the service-reported model and an
`estimated_usd_uncached` upper-bound estimate. Global short-context USD/M:
Luna **0.10 input / 0.50 output / 0.01 cached input**; Sol **2 / 10 / 0.20**.
Responses report cached input (summed onto the `run_cost` log line as
`cached_prompt_tokens`), but the cost-row estimate still prices **all input
uncached**, as an upper bound, so the row schema is unchanged. Unknown model or missing usage yields
`cost_basis=unavailable` and no price estimate, with a warning.

### Deployment and rollback

- [autorefine-health-scan.yml](.github/workflows/autorefine-health-scan.yml),
  job `health-scan`, uses repository variable `HEALTH_SCAN_MODEL` ahead of its
  `gpt-6-luna` default. Secrets/auth are unchanged here.
- Planning production is the existing Container Apps Job declared in
  [main.bicep](infrastructure/main.bicep), not a deployment workflow.
  Its entrypoint clones default-branch Python on startup, and the live job sets
  no `FOUNDRY_DEFAULT_DEPLOYMENT`, so **merging changes the next scheduled run**:
  it publishes and uses a luna `autorefine-plan` version. IaC changes still need
  a manual `az deployment group create`.
- Agents `autorefine-plan` and `autorefine-refine` are persistent; a run reuses
  the newest version whose definition hash matches and publishes a new version
  only when model, instructions, tools or sampling change. Existing unrelated
  shared-project agents (`atlas-*`, `lab-memory`) must not be edited.
- Rollback is a revert of the code change. It reuses the older retiring-model
  agent version by hash and stops working when those models retire.
- There is no repository-owned `copilot-triage` workflow in this repo.
## Cost

Target: < €5/month on Azure consumption plan.

### Subscription budget reporting

Health scans and dashboards read the subscription named by `AZURE_SUBSCRIPTION_ID`
using the existing `DefaultAzureCredential` chain. `AUTOREFINE_AZURE_BUDGET_NAME`
selects the Azure subscription budget (default `naurolabs-credit-cycle-eur-100`).
This is a reporting selection, not budget provisioning or a credit allowance.
The selected budget must be an unfiltered **Cost / BillingMonth** budget that is
active on the query date and does not expire before the billing period ends.
Azure requires a first-of-calendar-month budget lifetime start even for
`BillingMonth`; that date can fall inside the current billing cycle. It must not
replace the authoritative billing-period start when querying or projecting costs.

The scanner reads current dates from Azure's **Billing Periods** API, the budget amount
and currency unit from the selected **Consumption Budget**, and paginated daily
**ActualCost** rows from Cost Management. It maps columns by name and requires one
reported currency matching the budget's `currentSpend.unit`. The budget's
`currentSpend.amount` is never used for costs or freshness: it may be zero before
budget evaluation or stale afterward. Costs are always queried independently.
No calendar-month, currency or budget defaults substitute for failed reads. Missing/unsupported
billing periods, inaccessible budgets, absent units, malformed data and empty cost
results without a currency make the cost scan unavailable. A zero-cost row with a
known currency is valid. Cost failures mark health-scan results incomplete
(`failed_stages: ["cost"]`, alongside any other failures) without preventing other
scans, report/notification attempts or dry-run output. Billing Periods is a
subscription-type-dependent preview API; unsupported subscriptions fail visibly.

On a cycle reset, Azure can still return only the period that ended yesterday.
The scanner names the missing query date and last completed period in the report,
dashboard and Telegram warning, rather than conflating this with overlapping
periods. It still marks the cost stage incomplete: it never reuses last cycle's
spend, assumes zero cost, or invents the new cycle's dates or credit balance.
Retry once Azure returns a period covering the query date. A later missing-period
read remains unavailable and is not described as a reset happening today.

Reports carry `currency`, `budget_name`, `budget_currency`, `budget_time_grain`,
inclusive `period_start`/`period_end`, `next_reset` (the following day), `query_end`,
`remaining_budget` and `latest_usage_date`. The legacy `remaining` key aliases
**remaining budget**, never actual remaining credit. There is no credit-ledger
integration; remaining credit is explicitly unavailable.

The Visual Studio Enterprise benefit is **USD 150 per month**, configured through
`AUTOREFINE_AZURE_MONTHLY_CREDIT_USD` (default `150`). This allowance is separate
from the native-currency Azure alert budget. Separate daily queries request
**CostUSD** and **Cost**: USD spending is supplied by Azure, never obtained
by relabelling EUR or applying a guessed exchange rate. Missing or malformed
CostUSD data fails visibly rather than substituting native-currency costs.
The metrics are deliberately not combined: on 2026-09-11 a live combined query
with CostUSD first labelled its two totals inconsistently with single-metric
queries. Each metric's amount is now unambiguous even when Currency is EUR
(that column still describes the subscription's native billing unit).

Reports retain the native costs and budget for audit, and add `total_usd`,
`projected_usd`, `monthly_credit_usd`, `estimated_credit_remaining_usd` and
`cost_usd_source`, `latest_usage_date_usd` and `by_resource_group_usd`.
Estimated credit left is the configured allowance minus
reported USD usage; it is **not an authoritative remaining-credit balance**.
Telegram leads with the USD benefit, spend, estimated headroom, cycle and reset,
with blank lines between sections. Budget IDs and query diagnostics stay in the
full report; independent alert-budget warnings remain visible on the phone.
No Azure budgets, billing currency, spending limit or credentials are changed.

`projected` is an explicitly labelled **linear cycle-end projection**, not an Azure
forecast: reported actual cost × inclusive cycle days ÷ inclusive elapsed cycle
days through the UTC query date. Actual and forecast rows are never added together.
Budget notification names do not establish forecast semantics; this projection
remains linear regardless of any notification's name or `thresholdType`.
The current day can be partial and ingestion can lag. `latest_usage_date` is only
the latest usage day returned by Azure, **not** an ingestion refresh timestamp or
proof that all costs through that day have arrived. Ingestion freshness is unknown.
Older stored reports still render amounts, but missing metadata is labelled unknown
and cannot produce a green budget verdict.

Successful cost summaries log at DEBUG; unavailable reads remain WARNING. No model,
scan-frequency, credential, live budget, Function App or infrastructure settings are
changed by this reporting code.
