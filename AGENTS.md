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

## What the score actually measures

The 0-100 score is **not a quality measure — it is a coverage-weighted one**, and
it is easy to misread as the former because that is how it is labelled everywhere
it appears.

Nearly every check in `agent/tools/quality_tools.py` is gated. `check_tests`,
`check_ci_cd` and `check_i18n` early-return unless the project's own
`project.yaml` `quality:` list names the trait; `check_security_headers` and
`check_dependencies` early-return unless the stack carries
`staticwebapp.config.json` or `package.json`. Only `check_project_yaml` always
runs. An early return deducts nothing, so **a project that declares nothing
scores 100/100.** Two byte-identical Python apps with no tests and no CI score
100 and 70 depending purely on whether they were honest about their own
standards. The score rewards silence.

`run_quality_checks_with_coverage()` therefore returns a `QualityCoverage`
alongside the findings, and `evaluate_project` puts it in the report as
`coverage`. `DimensionResult.measured` is derived from `skip_reason`, so a check
that did not run can never be mistaken for one that passed:

| `skip_reason` | Means |
|---------------|-------|
| `not-declared` | the trait is absent from `project.yaml` `quality:` — a choice the project made |
| `not-applicable` | the stack has no such artefact (no `package.json`, no SWA config) |
| `tooling-unavailable` | the check ran and could not finish (no token, 403, timeout) |

The coverage rides along to Telegram (`agent/score_summary.py` renders
`100/100 (1/6 measured)`) and to the dashboard's Score Coverage section. The
dashboard clones nothing, so it cannot re-derive the scores: pass the
evaluate-mode report to it with
`--mode dashboard --report /tmp/autorefine-report.json`, or the section renders
empty rather than guessing.

**Do not "fix" this by making the checks unconditional.** That is a fleet-wide
behaviour change: scores drop across ~25 repos at once and `file-ideas` emits a
burst of P0 "no tests" ideas, each approvable into a 10-30 minute Copilot Coding
Agent run, against the €5/month cap above. The gap is deliberately visible and
unclosed. Closing it is a separate, budgeted decision — and if it is ever taken,
take it per-project, not fleet-wide.

`run_quality_checks()` keeps its findings-only contract; nothing about weights,
priorities or ordering changed, and `tests/test_score_coverage.py` pins the
scores so it stays that way.

### Detection must be as honest as the gate

The gate above decides *whether* a check runs. What it finds when it does run is
a separate way to be wrong, and it was. `check_tests` looked only at four
directories at the repository **root**. Measured across the 24 live manifest
projects on 2026-08-26, four of the six repos it would have called untested had
tests one level down — `app/tests/`, `harness/tests/`, a deep `smoke_test.py`, a
bare `test.js` — and two of those had CI running the very suite it said did not
exist. `glassBox` keeps `.test.ts` files beside its source and was carrying a
false P0 for it.

Detection now covers nested directories and file-naming conventions. Three
guards keep it from swinging the other way, because a detector too permissive to
fail is just another check that can never fire:

- vendor and dot directories are pruned during the walk, not filtered after —
  that is where a permissive glob finds its false comfort;
- a test file must carry a code suffix, so `test_plan.md` proves nothing;
- a runner config alone is not evidence — `pytest.ini` with no tests runs none.

`_has_tests` is deliberately written as "the old rule, then additions". Every
path the old rule accepted is accepted first and unchanged, which is what makes
this a contained change: it can only ever *remove* a finding, never create one,
so it cannot create an idea and cannot spend money. Verified across the fleet —
exactly one score moved, `glassBox` 70 → 90, and upward.

Fixtures in `tests/test_test_detection.py` are real layouts from real projects
rather than invented ones.

### Advisory findings

Some defects are real, worth scoring, and impossible to fix with a commit —
default-branch protection, org policy, infrastructure outside the repo. Left in
the planning prompt they become improvements, improvements at P0/P1 become filed
issues, and an approved issue becomes a 10-30 minute coding-agent run that is
*guaranteed* to produce nothing, or worse a plausible workflow file that pretends
to do the job. Near-certain no-op runs are the worst cost profile in the system.

`QualityFinding.advisory` marks them. They score, they appear in the report, and
they reach humans through Telegram and the dashboard — they are withheld only
from the LLM, by `plannable_findings()` inside `build_plan_task`.

Three things about that are load-bearing:

- **The filter lives at the chokepoint, not at the call sites.** `build_plan_task`
  is the one place a finding becomes prompt text, so it is the only place the
  exclusion can be enforced rather than merely remembered. A test asserts it stays
  the only such function; add a second prompt builder taking `findings` and that
  test fails on purpose.
- **Priority is not the mechanism.** Filing is gated on `DEFAULT_IDEA_PRIORITIES`,
  but a P2 finding still enters the prompt and the model may answer it with a P1
  improvement. Only removing it from the prompt closes the path.
- **`advisory` is not `fixable`.** `fixable` means autoRefine's deterministic fixer
  can repair it without an LLM; almost every finding is `fixable=False` yet
  perfectly repairable by a coding agent. Reusing that field would starve the
  model of nearly every finding it sees today.

`advisory` governs the prompt only. Whether such a finding should also deduct
score was left open when the flag landed; it is now decided. **Weight it, but
only where it is actionable.** The score exists to tell a human something true,
and a fleet where 0 of 25 repos have branch protection — while rule 1 above says
"never push directly to main/master" and enforcement lives in a workflow a direct
push bypasses — is a fleet with a real weakness. Omitting it repeats the failure
that the coverage work above was about.

The deduction is uniform, so it shifts every score by a constant and does nothing
for ranking. That is fine, because it is **self-resolving**: a one-time drop that
recovers the moment someone changes 25 settings, which is minutes of work or a
single org-level ruleset. A score that drops, prompts a cheap fix and recovers has
done its whole job. Do not zero the weight when you see the fleet-wide drop in the
history — the drop is the mechanism working.

"Only where actionable" is the other half and it is not optional. A project that
*cannot* buy the fix — a plan that does not offer the setting — is
`not-applicable` and leaves the denominator. It is never advisory-with-weight.
Never penalise a project for something it cannot buy.

The first and so far only producer is `measure_branch_protection`. Measured on
2026-08-26: **31 of 31 non-archived repositories have no classic protection and
zero rulesets**, so it fires everywhere and every scored project drops 10 points
at once. That drop is the mechanism working, not a regression.

It checks classic protection **and** rulesets, and that is load-bearing rather
than thorough: rulesets are the modern way to protect a branch, so a check blind
to them would keep reporting a repo that had just been fixed. A finding that
cannot clear is a permanent penalty, which is what the paragraph above says never
to weight. Archived repositories and repositories with no default branch are
`not-applicable`.

### The dependency check

`measure_dependencies` reads **Dependabot alerts over the API**. It used to shell
out to `npm audit`, and that check had never produced a finding in production:
the job image is `python:3.12` with no node (`infrastructure/main.bicep`), so
every run raised `FileNotFoundError`, which the old code swallowed and returned
no findings for — indistinguishable from a clean tree. Someone once debugged that
as an OOM. It was also gated on `package.json`, so it never looked at a Python
project at all.

Two rules it must keep:

- **No failure may look clean.** Absent token, absent slug, 403, 404, timeout,
  malformed body, kill switch — every one is `tooling-unavailable`.
  `fetch_dependabot_alerts` raises rather than returning `[]`, because `[]` is a
  real answer meaning "no open critical or high alerts" and the entire bug being
  fixed was those two being indistinguishable. `TestNoFailureLooksClean` walks
  every failure mode there is.
- **It paginates by cursor, not by page.** Sending `page` earns
  `HTTP 400 — Pagination using the `page` parameter is not supported.` from the
  live API, which fails closed but makes the check dead on every repo. Follow the
  `Link` header. Mocked responses cannot catch this; only running it against the
  real API did.

`AUTOREFINE_SKIP_DEPENDABOT=1` stops the network call entirely, degrading to
`tooling-unavailable`. It exists because this is the first thing in a previously
offline module to call out to the network, fleet-wide, twice a day.
`AUTOREFINE_SKIP_BRANCH_PROTECTION=1` does the same for the other network check —
separate switches, so an emergency in one cannot silently take the other with it.

Cost is not a concern here: one or two calls per repo per sweep against a
15,000/hr core budget. Measured 2026-08-26 — 30 non-archived repos, 0 with open
critical or high alerts, so the check currently produces no findings at all.
`samoletovs/.github` answers 403 with alerts disabled; that is the one repo where
the dimension is unmeasured rather than clean.

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
