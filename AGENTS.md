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
7. **Every measured number in this file and in code comments has expired.** They are
   observations with a date, not properties of the system. Quote one only with its date
   and sample size attached, and re-measure before sizing anything on it. This has now
   failed twice in place: the gate-widening count (see "What the score actually
   measures") and the 116-minute sweep duration that sized `replicaTimeout` for months
   after the real spread turned out to be 7.4–167.8 min (see "Sizing the job ceiling").
   The shelf-life warning existed for the first of those and was scoped to it, which is
   why it did not catch the second — so it is stated here, once, for all of them. The
   method survives; the number does not.

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

**Any number attached to that decision expires.** Widening the gate was priced on
2026-08-26 at 4 new findings across 3 repos (`folio`, `payArc`, `foundryLab`),
down from 8 across 6 before the detector was fixed. That figure describes the
fleet as it stood that day: a repo that grows a test file next week drops off it.
Re-run the measurement rather than quoting it — clone the manifest, run the real
`run_quality_checks_with_coverage()` twice per project (once as-is, once with
`tests`/`ci-cd` added to a copy of the config), and diff. The method survives;
the number does not. There is deliberately no committed script for this, because
a committed measurement becomes a number people trust without re-running, which
is the failure it exists to prevent.

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

## The workflows have five minutes to buy their tokens

Both agent workflows authenticate with OIDC federated credentials rather than a
service-principal secret. The two are not interchangeable, and the difference is
a clock.

`azure/login` exports **no environment variables** — `exportVariable` appears
nowhere in the action; it runs `az login` and stops. So `DefaultAzureCredential`
never reaches Azure through `EnvironmentCredential` here, under OIDC or before
it. The link that works is `AzureCliCredential`, which shells out to
`az account get-access-token` against the CLI session the action left behind.
Nothing in `agent/` needed changing to move to OIDC, and nothing should be
changed on the assumption that it did.

What did change is expiry. GitHub's federated token lasts about five minutes,
the Azure CLI persists it verbatim, and it cannot mint another. `az login` buys
an ARM token and nothing else, so the *first* request for any other scope is a
fresh round-trip that re-presents a token which has probably expired, and fails
with `AADSTS700024`. A client secret had no such window.

Three consequences, all load-bearing:

- **Step order is correctness, not tidiness.** Both workflows put every slow step
  — checkout, `setup-python`, `pip install` — *above* the login. Moving one below
  it spends the window. That failure is invisible locally, invisible in review,
  and surfaces as an Entra error deep inside a 43-minute run.
- **The pre-warm exists because of this.** The step after each login takes every
  token the job will need while the federated token is still valid, writing them
  to `~/.azure/msal_token_cache.json` where the Python finds them without
  presenting an assertion at all. It uses `--resource`, not `--scope`, because
  that is character-for-character the command `AzureCliCredential` itself runs —
  it strips `/.default` and passes `--resource` — so the entry written is the one
  later looked for, rather than one that ought to normalise to the same key.
- **The resource list cannot be derived, only maintained.** A client's scope is
  usually an SDK default rather than a string in our source: `AgentsClient` asks
  for `https://ai.azure.com` and that appears nowhere in this repository. Calling
  a new Azure service means adding its resource to the pre-warm by hand.
  `tests/test_workflow_hardening.py` guards that a pre-warm exists, follows the
  login immediately, hides its output and cannot fail the job — it deliberately
  does not guard the list, because a guard that hard-coded it would just be the
  workflow restated.

**Check the status rather than trusting this paragraph.** Read on 2026-08-27:
[azure-cli#28708](https://github.com/Azure/azure-cli/issues/28708) open since
2024-04-08, opened by an Azure CLI maintainer, and the `ubuntu-24.04` runner
image README listing Azure CLI 2.89.1. Neither was verified by running a job —
the version came from `actions/runner-images`, not from `az version` on a runner,
and no run has exercised any of this because the secrets do not exist yet. Some
sources claim the issue was fixed in 2.60.0; that did not match the still-open
issue, and it is the kind of claim worth re-checking rather than inheriting. If
the CLI learns to refresh the token, the pre-warm becomes dead weight and should
go — but confirm it, don't assume it.

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

## The entrypoint is baked in; the Python is not

`infrastructure/main.bicep` builds the job's command with
`loadTextContent('run-autorefine.sh')`, which inlines that file into the ARM template **at
deploy time**. The Python is the opposite: the script git-clones autoRefine at start-up, so
`agent/` really is whatever is on master. One repo, two very different freshness rules, and
the script's own header used to claim the runtime one for both.

The gap is silent by construction. Editing `run-autorefine.sh` and merging it produces no
error and no failed run — production just keeps executing the older copy. Measured
2026-08-27: the deployed job was running the 2,918-character pre-#12 script with no
`AUTOREFINE_COST_LOG` in it, while that same 06:00 run pulled new Python from master and
scored normally. The cost-telemetry block from #12 had never executed once, and the only
symptom was a file that never appeared in `reports/cost`.

**A change to `infrastructure/` is not live until someone redeploys.** There is no deploy
workflow; it is a human running `az deployment group create`. Say so in the PR when you
touch that directory.

No hermetic test can catch the drift itself — "is it deployed" is a fact about Azure, not
about the repo, and the only real check needs ARM credentials, which in CI means a skip,
and a test that skips where it matters is the kind this repo deletes.
`tests/test_infra_entrypoint.py` therefore guards the mechanism instead: every
`loadTextContent` target must exist, must say in prose that editing it requires a redeploy,
and must be pinned to `eol=lf` — it is inlined verbatim and then run by `/bin/sh` in a Linux
container, and deploys are issued from Windows.

Two things there are load-bearing:

- **The path filter must list `infrastructure/**`.** It was absent from `tests.yml`, so a PR
  touching only the bicep and the entrypoint ran no tests at all. Without it every guard
  above is decorative, which is why one of the tests asserts the filter itself.
- **The anti-vacuity test.** The per-file guards are parametrised over whatever the template
  inlines, so if nothing is inlined they collect nothing and pass. Moving the entrypoint out
  of the template is a deliberate change to the deployment model and should fail here rather
  than quietly emptying the module.

`workloadProfileName: 'Consumption'` is declared rather than left to the RP for a related
reason: omitting it kept the deployed value out of the template, so every `what-if` reported
`'Consumption' -> null` — a permanent red herring beside the one delta a deploy is actually
for. `cae-agents` offers exactly one profile and all seven workloads in it run on it, so
naming it is correct regardless of how the RP treats the omission.

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

### Sizing the job ceiling

`replicaTimeout` was 3h, sized on a single 116-minute sample whose comment described it as
"3h rather than a snug fit". Eight days of real execution history (2026-08-20..27) read
7.4 / 14.0 / 24.4 / 28.1 / 85.5 / 103.0 / 108.3 / 167.8 minutes — a 23× spread whose
longest run sat at **93.2% of the ceiling**, which is a snug fit by any reading. It is now
6h.

**It is a backstop, not a prediction, so do not size it off the distribution.** The usual
"measure before you pick a number" rule does not bind here because the errors are not
symmetric. Too generous costs a few percent of a free grant on a day that never arrives:
at 0.5 vCPU / 1Gi, burning the whole 6h is 10,800 vCPU-seconds and 21,600 GiB-seconds, 6.0%
of each monthly grant, and a ceiling that is never reached bills nothing. Too tight throws
away the sweep **and** leaks a Foundry agent, because a `replicaTimeout` kill is a hard kill
that never reaches `main.py`'s `finally` (see "Foundry agent lifecycle").

Three things that are easy to get wrong here:

- **No maximum is documented.** Not in the jobs article, the quotas page, the ARM schema
  (`int`, required, no range), or `az containerapp job create --help`. 21600 is accepted by
  the RP — confirmed by `what-if` — but if you need a much larger value, verify it rather
  than assuming the field is unbounded.
- **The timeout bounds the whole execution, not each attempt.** "The `replicaTimeout`
  setting takes precedence if it expires before all retries occur"
  ([jobs, Advanced job configuration](https://learn.microsoft.com/azure/container-apps/jobs)).
  So worst case is 6h total, not 6h × attempts — and `replicaRetryLimit: 1` only has room to
  achieve anything when the ceiling is well clear of a normal run. Under the old 3h, a run
  failing at 167.8 min left its retry 12 minutes.
- **The round guards do not make this redundant.** `DEFAULT_MAX_TOOL_ROUNDS = 200` and
  `DEFAULT_STUCK_REPEATS = 3` bound *iteration*, so a runaway tool loop is far less likely
  than when 10800 was chosen. They do not bound *wall clock*: `run_agent` uses
  `time.monotonic()` only to report `duration_s`, so a hang inside a single blocking call
  advances no rounds and trips neither guard. Everything outside `run_agent` — clone, pip,
  the per-project loop — is unguarded too. This ceiling is still the only wall-clock bound.

Provisional, and subject to hard rule 7. Eight samples spanning the activity-gate change is
two regimes rather than one distribution. The cost rows in `reports/cost` carry per-run
`mode` and `rounds`, which will say whether a long run is many projects or a few slow ones —
and that, not the ceiling, decides whether the gate is the thing to move.

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
