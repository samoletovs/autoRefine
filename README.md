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

### 1. Add a `project.yaml` to your repo

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

### 2. Configure

```bash
cp .env.example .env
# Set your Foundry connection string and GitHub token
```

### 3. Run

```bash
# Evaluate a single project
python -m agent.main --repo owner/repo --mode evaluate

# Evaluate + plan + file idea memos (closed loop)
python -m agent.main --repo owner/repo --mode file-ideas

# Full cycle (evaluate + plan + execute) — local dev only, bypasses the loop
python -m agent.main --repo owner/repo --mode refine

# All projects in a manifest
python -m agent.main --manifest config/workspace-manifest.json --mode file-ideas

# Generate a browser dashboard with health, ideas, and progress tracking
python -m agent.main --repo owner/repo --mode dashboard --output dashboard.html
```

`--model` selects the deployment for `plan`, `refine`, and both technical and functional
planning in `file-ideas`. It overrides `FOUNDRY_DEFAULT_DEPLOYMENT`; the default remains
`gpt-4o-mini`.

`file-ideas --dry-run` suppresses issue creation and Telegram notifications, but can
still use Foundry to generate plans. The governance filer must support `--repo` and
any requested `--dry-run` or `--needs-approval` flag; missing capabilities stop filing
rather than silently dropping these safeguards. `health-scan --dry-run` reads services
and runs AI analysis (still billed), but does not persist/prune reports, create/assign
issues, or send Telegram. Its JSON includes the proposed report and issues. The health
workflow's `dry_run` dispatch input also dry-runs the PR sweep and suppresses failure
notifications. Normal scans remain unchanged; identity configuration still needs
operator approval.

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

- **Model**: `gpt-4o-mini` by default (cheap, runs across 11 repos). Bump to
  `gpt-5` for one-off deep analysis via `python -m agent.main --model gpt-5`.
  The closed-loop PR reviewer (deep-review) lives in `samoletovs/nauroLabs-github`
  and uses Claude Opus 4 (via GitHub Models) — see that repo's AGENT_ROLES.md.
- **Tools**: GitHub API, file system, test runner, quality checkers
- **Human-in-the-loop**: agent asks for confirmation on risky or uncertain changes
- **Safety**: all changes are made on branches, tested, and submitted as PRs — never direct to main

## Stack

- Python 3.11+
- Microsoft Foundry (Azure AI Projects SDK)
- Azure OpenAI (`gpt-4o-mini` default, configurable via CLI / env)
- GitHub API (`gh` CLI + REST)
- PyYAML for `project.yaml` parsing

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

Reports carry `currency`, `budget_name`, `budget_currency`, `budget_time_grain`,
inclusive `period_start`/`period_end`, `next_reset` (the following day), `query_end`,
`remaining_budget` and `latest_usage_date`. The legacy `remaining` key aliases
**remaining budget**, never actual remaining credit. There is no credit-ledger
integration; remaining credit is explicitly unavailable.

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
