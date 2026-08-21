# AGENTS.md — autoRefine

## What this repo is

autoRefine is a Foundry-powered AI agent that evaluates and improves software projects.
It reads `project.yaml` from each project, understands vision + tech stack, compares
against provided similar products, identifies gaps, creates improvement plans, executes
with tests + PRs, and can file idea memos.

## Hard rules

1. **Never push directly to main/master.** Always create a branch and PR.
2. **Never delete files** without explicit user confirmation.
3. **Never modify secrets or auth config.** Report findings, don't fix.
4. **Always run tests** after making changes. Revert if tests fail.
5. **Ask the user** when confidence is below 70% on any change.
6. **Cost discipline.** Default to `gpt-4o-mini` for the daily evaluate/health-scan
   passes (these run across 11 repos twice a day). Budget cap: €5/month on
   autoRefine's own consumption. See "Model strategy" below before bumping.

## Model strategy

autoRefine uses a **tiered model strategy** — cheap models for routine work,
strong models only where the cost is bounded.

| Tier | Used by | Default deployment | Override env |
|------|---------|--------------------|--------------|
| Cheap (daily) | `evaluate` mode (deterministic, no LLM) + `health-scan` AI analyst | `gpt-4o-mini` | `HEALTH_SCAN_MODEL` |
| Cheap (on-demand) | `plan` / `refine` Foundry agent | `gpt-4o-mini` | `FOUNDRY_DEFAULT_DEPLOYMENT`, or CLI `--model` |
| Deep (rare) | Closed-loop PR reviewer (lives in `samoletovs/nauroLabs-github/scripts/claude-deep-review.py`, **not** in this repo) | `claude-opus-4` with `claude-sonnet-4` fallback, via GitHub Models | configured in the governance repo |

Bump rules:

- For a one-off deep analysis pass: `python -m agent.main --model gpt-5 --mode plan --repo owner/name`
- For a sustained bump: set `FOUNDRY_DEFAULT_DEPLOYMENT` in `.env`. Coordinate
  with the €5/month cap — Foundry's `gpt-5` is ~10× more expensive than
  `gpt-4o-mini`, so a sustained bump means cutting daily-scan frequency.
- The **PR reviewer** path (Claude Opus 4 via GitHub Models) is configured
  outside this repo because the merge gate lives in the governance
  workflows. See `samoletovs/nauroLabs-github` → `.github/workflows/auto-review.yml`.

## Where things live

| What | Where |
|------|-------|
| Agent orchestrator | `agent/main.py` |
| Configuration + project.yaml parsing | `agent/config.py` |
| Foundry tool definitions | `agent/tools/` |
| System + task prompts | `agent/prompts/` |
| Tests | `tests/` |
| Bicep infra | `infrastructure/` |

## Code conventions

- Python 3.11+ with type hints on all functions
- Async for I/O (GitHub API, Foundry calls)
- `logging` module — never `print()` in production
- `pytest` + `pytest-asyncio` for tests
- `ruff` for linting

## Foundry agent lifecycle

autoRefine's agent is **ephemeral**: `create_agent()` makes one per run, `main.py`
deletes it in a `finally` block. A hard kill (CI timeout, OOM, container eviction)
never reaches that block, so agents leaked into the Foundry project at roughly one a
week until 2026-08-20.

`create_agent()` therefore also calls `sweep_orphaned_agents()`, which deletes agents
named `autorefine` older than `ORPHAN_AGENT_MAX_AGE` (6h) — a crashed run self-heals on
the next one. **The age gate is load-bearing:** a run takes ~43 min, so anything younger
may be a live agent belonging to a run in progress. Don't drop the gate, and don't widen
the name match — `atlas-*` and `lab-memory` share the project and are persistent.

Note the endpoint reads `…/api/projects/{proj}/assistants`. That is **Foundry classic
agents**, *not* the retired Azure OpenAI Assistants API — see
[PLATFORM.md §15.2](../.github/PLATFORM.md).

## Build & run

```bash
pip install -r requirements.txt
python -m agent.main --repo owner/repo --mode evaluate
python -m agent.main --repo owner/repo --mode file-ideas
```

`file-ideas` resolves `scripts/file-idea.py` in this order:
1. `$NAURO_GOVERNANCE_PATH/scripts/file-idea.py`
2. `./.github-gov/scripts/file-idea.py` (CI checkout path)
3. `/tmp/nauroLabs-github/scripts/file-idea.py` (cloned on demand)

## What the sweep actually costs

The daily manifest sweep is the whole Foundry bill. On 2026-08-21 the live meter had
`gpt 4o mini 0718 cached Inp glbl Tokens` at €27.22 month-to-date on 425M tokens —
47% of the entire Azure subscription — against 0.79M output tokens. A 552:1 input to
output ratio is not analysis, it is the same context being re-sent.

Two things drive it, and they multiply:

| Driver | Why it costs | Lever |
|--------|--------------|-------|
| Every tool round re-sends the whole thread | one plan run is ~74 rounds, so input is O(rounds²) | `truncation_strategy` on the run (`AUTOREFINE_TRUNCATION_LAST_MESSAGES`) |
| Every project is planned every day | only 6.4 of 24 projects have a commit on a given day | the activity gate (`should_plan_repo`) |

**The truncation window trades cache for volume, and that is fine but not free.**
Azure bills a cached prefix at half rate, and the run was hitting cache 97.5% of the
time precisely *because* it re-sent an ever-growing prefix. A sliding window forfeits
that discount from the turn it starts sliding. Raw input tokens fall ~68%; the *bill*
falls ~42%. Expect the uncached `…-Inp-glbl` meter to rise while the cached one
collapses — that is the change working, not a regression. Don't tighten the window
below 12 without evidence that runs still reach `submit_plan`; and never reorder or
templatize `agent/prompts/system.md`, which is the one prefix still caching cleanly.

**The activity gate is the lever with no quality cost.** A project whose default
branch hasn't moved gets re-read and re-planned to produce the ideas it produced
yesterday. `should_plan_repo` plans a swept project when either its branch has a
substantive commit inside `AUTOREFINE_ACTIVITY_LOOKBACK_HOURS` (26h — the daily sweep
plus margin) or its deterministic rotation slot comes up (`AUTOREFINE_ROTATION_DAYS`,
7). The rotation slot is a stable hash of the repo name against the date, which is why
no state file is needed and why the floor never fires for the whole fleet at once.
Replayed over 30 days of real history across 24 repos, this plans 33% of project-days
instead of 100%.

**"Substantive" is load-bearing, and a naive commit check does not survive here.**
The lab pushes cross-repo housekeeping — a licence sweep, a synced `audit-leaks.ps1`,
a governance workflow update — to every project on the same day. On 2026-08-21 three
such sweeps had touched 22 of 24 projects inside 28 hours, so a gate counting any
commit as activity would have skipped two projects and planned the rest: a saving on
paper and none in the bill. `HOUSEKEEPING_PATHSPECS` excludes those paths from the
`git log` that measures activity. With it, `payArc` and `foundryLab` read as idle for
~90 days, which is exactly what they are — and exactly what was being re-planned
nightly. Add to that list when a new lab-wide sweep appears.

Two rules it must keep:

- **It fails open.** A git failure returns "unknown", which counts as activity. Being
  wrong costs one run; guessing "idle" would silently switch ideation off fleet-wide.
  Git succeeding with no match is different — that is an answer (`inf`), not an error.
- **`--repo` is never gated.** A human naming a project is asking for it now. The gate
  applies only to a manifest sweep (`AutoRefineConfig.gate_on_activity`).

Only the Foundry planning is gated. The deterministic evaluation still runs for every
project, so scores and the Telegram summary continue to cover the whole fleet.

## Why the PR-card sweep is a cron and not an event

`pr-ready-cards.yml` reads open PRs across every project in the workspace manifest.
GitHub delivers `pull_request` and `check_suite` only to workflows in the repo where
the event happened, so no trigger here can see them. Making it event-driven would mean
a `repository_dispatch` forwarder in all 23 other repos — more runs, not fewer.

So the only lever is run count, and run count *is* the bill: GitHub rounds each job up
to a whole minute and this job's real work is seconds of API calls, so 230 runs cost
230 minutes. It is now one scheduled sweep a day, with a second sweep appended to the
daily health-scan job, which has already paid for its checkout and dependencies and so
carries it for free. When something here needs to run often, prefer attaching it to a
job that is already running over giving it a schedule of its own.

## Test

```bash
pytest tests/ -x -q
```
