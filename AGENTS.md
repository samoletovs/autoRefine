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

   One exemption, because the rule read literally destroys things worth keeping: a
   figure that records **why a past decision was taken**, and sizes nothing live, is a
   fossil and may stay undated. `main.bicep`'s header cites ~43 min/day of Actions time
   to explain why the job moved to Container Apps; that workflow is retired, so there is
   no measurement left to re-run. Dating it would imply it can be refreshed, and deleting
   it would destroy the only surviving record of the reason. The test is not whether a
   number is old — they all are — but whether anyone can still *size* something on it.

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

## `--mode dashboard` was dead, and the manifest fetch was why

The `--report` contract described above — pass the evaluate-mode report or the
Score Coverage section renders empty — was documented but had never been run.
Exercised end to end on 2026-08-28 it did not merely render an empty section: the
process died before writing any file at all.

`fetch_workspace_manifest()` GETs the manifest from `raw.githubusercontent.com`
and, until 2026-08-28, sent no credential. `nauroLabs-github` is **private**, and
an unauthenticated GET of a private repo's raw URL returns **404, not 401** —
GitHub hides existence rather than refusing access. So the fetch raised
`RuntimeError: … HTTP 404`, which reads as "the manifest is gone" rather than
"you did not ask as anyone". Both callers — `check_deployed_urls` and
`scan_app_insights` — are unguarded, so the exception took the whole run with it.

Three things about that are worth keeping:

- **The 404 is the trap, not the 401.** Any future check that reads a private
  repo over `raw.githubusercontent.com` will fail this same way, and the error
  will point at the path. `_manifest_auth_headers` is host-gated — `url` is a
  parameter, and a token must not follow an override to a non-GitHub host.
- **The loud failure stays loud.** It would have been easy to catch the error in
  dashboard mode and carry on. That renders a page reporting zero unhealthy URLs
  because none were checked, which is a false all-clear — the same class of bug
  as a green tick for a check that never ran. A manifest that cannot be read must
  stop the run.
- **It is not a dashboard bug.** `--mode health-scan` calls the same two
  functions and dies identically. That mode has never run: all 6 of its scheduled
  runs are `skipped`, and `reports/health` does not exist in the governance repo,
  so nothing has ever been committed by it. The defect was invisible because the
  only two modes that could have hit it were both switched off.

**Verified after the fix**, `--repo samoletovs/autoRefine`, exit 0: Score Coverage
renders `autoRefine | 90/100 | 4/7 | ci-cd (not-declared), security
(not-applicable), i18n (not-declared)`, matching the evaluate report exactly.
Omitting `--report` renders "No evaluation coverage available — run `--mode
evaluate` to populate it." Both halves of the documented contract hold.

Dashboard mode makes no writes: it files no issue, opens no PR and sends no
Telegram, and `analyze_with_ai` returns an error dict rather than calling a model
when `AZURE_OPENAI_ENDPOINT` is unset. That is what made it safe to run against
the live API at no token cost, and it is why this could be verified when
health-scan could not.

Two failures seen while doing this were **Windows-only** and must not be "fixed":
`/tmp/workspace-manifest.json` has no parent on Windows, and `print(report)`
raises `UnicodeEncodeError` under a cp1252 console. The job runs on Linux in a
`python:3.12` container where both are fine.

## What the ideation loop learns from

An idea can die in two places, and until 2026-08-27 the loop only watched one of
them. `_recent_declined_reasons` reads issues labelled `declined` — a 👎 on a
Telegram card, which costs one tap. `_abandoned_after_build` reads the other end
of the funnel: an idea that was approved, ran a 10-30 minute Copilot Coding
Agent, opened a PR, and had that PR closed unmerged. That run is the largest
single unit of spend the pipeline can produce, and nothing read its outcome back.

**Both channels are currently silent, and that is the most useful thing to know
about them.** Measured 2026-08-27 across all 25 manifest projects: no issue
anywhere in the org carries `declined`, and no `Feedback from Telegram:` comment
exists, so the older loop has returned `[]` on every production run it has ever
made. The new one returns `[]` too — there is exactly one closed-unmerged PR
tracing to an idea in the fleet's whole history, and it is excluded on purpose
(below). So this adds **0 tokens per run today**, and ~131 per entry when it
fires, capped at 4 entries. Do not read the empty block as the check being
broken; read the log, which says which it is.

Four things are load-bearing:

- **The linkage is GitHub's, not ours.** `closedByPullRequestsReferences` with
  `includeClosedPrs: true`. Without that argument the field reports only PRs that
  *merged* — precisely the outcome this is not looking for — so the check would be
  dead on every repo while every mocked test still passed. Verified against era#1
  on 2026-08-27: `gh issue view --json closedByPullRequestsReferences` returns
  `[]` there, because it does not pass the argument, while the raw query returns
  PR #2 with `merged: false`. It is a raw `gh api graphql` call for that reason,
  and being raw also sidesteps the CLI version pinned in `run-autorefine.sh`.
- **The issue must be closed too**, which `states: [CLOSED]` enforces. era#2 was
  closed unmerged and the human wrote "#1 stays open and is ready to be picked up
  again": the *idea* survived and only the build failed. Feeding that back as
  "avoid this" would contradict them outright, and `_is_near_duplicate` already
  suppresses a re-proposal for as long as the issue is open. An idea kept open
  after a failed PR is a retry request, not a lesson.
- **Ideas that died of a defect we have since fixed are skipped.**
  `unbuildable-memo` is what `is_specified()` now rejects before filing and
  `duplicate` is what `_is_near_duplicate` now stops. Measured 2026-08-27: 20 of
  the fleet's 23 closed idea issues carry one, every one of them predating the fix
  that would have prevented it. Replaying those would suppress ideas that file
  perfectly well today and spend tokens to re-punish a fixed bug.
- **Only a human's words are replayed.** A bot's status line costs tokens and
  teaches nothing, so the reason is the last OWNER/MEMBER/COLLABORATOR comment,
  hard-capped at `AVOID_REASON_MAX_CHARS`. The cap is not decoration: a Telegram
  decline is a phrase, but a human's PR post-mortem is an essay — era#2's is 1,615
  characters — and it would ride the planning prompt on every run.

They render as two blocks, not one, because the instruction differs. A declined
idea was refused on its face, so the answer is to propose something different in
kind. An abandoned one passed the human filter and died in the build: the area was
wanted, and telling the model to avoid it would throw away the part a human had
already said yes to.

`AUTOREFINE_SKIP_PR_OUTCOMES=1` stops the call, on the same reasoning as
`AUTOREFINE_SKIP_DEPENDABOT`. Every failure returns `[]` — a GitHub hiccup must
never stop the proposer — but failure logs at WARNING and emptiness at DEBUG, so
"could not tell" and "nothing to avoid" are distinguishable even though both look
the same to the caller. That is the `npm audit` lesson above, applied one function
over.

**The counts here expire; hard rule 7 applies.** They describe a 27-day-old
pipeline that had, at the time of measurement, never once had a human reject an
idea on its merits. Re-measure before sizing anything on them: list every
`idea`-labelled issue per project, and for the closed ones read `state_reason`,
the labels, and `closedByPullRequestsReferences(includeClosedPrs: true)`. There is
deliberately no committed script, for the reason given under "What the score
actually measures".

## Why ideation is not grounded in CI signal

The planning pass sees deterministic findings and `project.yaml` and nothing about
how the code behaves — no failing test, no crash, no error rate. Static grounding is
the weakest kind available, so feeding recent CI failures into `build_plan_task` was
investigated on 2026-08-27 and **declined**. The noise problem turned out to be
solvable; the signal turned out not to exist.

### The classifier works, and it is the part worth keeping

A failed workflow run is far more often a broken *workflow* than broken code, and
proposing a code fix for one is worse than proposing nothing. Three layers separate
them using only what the Actions API returns:

1. `conclusion == "startup_failure"` — the run never started; the YAML did not parse.
2. `name == path` — GitHub falls back to the workflow's file path when it cannot read
   `name:` from the YAML on that ref, so a run named `.github/workflows/x.yml` is a
   broken workflow file however else it looks.
3. Otherwise fetch `/actions/runs/{id}/jobs` and read the failed **step** names:
   deny-list infrastructure (`deploy`, `login`, `auth`, `secret`, `token`, `checkout`,
   `install`, `merge`, `provision`), allow-list code (`test`, `lint`, `typecheck`,
   `pytest`, `tsc`, `ruff`, `suite`).

**Layer 3 is not optional.** A workflow failing because a secret does not exist has a
proper name, a real job and `conclusion: failure` — at run level it is
indistinguishable from a failing test suite. Only step detail separates them, so a
design that skips the per-run `/jobs` call cannot work.

The layers were derived from the API shape *before* reading a description of the
noise already known by hand, and they independently reproduced both categories of it:
a batch of unparseable-YAML runs and a missing-secret run. Built the other way round
they would have been unfalsifiable.

**That provenance does not transfer.** It is a fact about the 2026-08-27 derivation,
not a property of the classifier, and the recipe below quietly destroys it: anyone
following it will have read this section first, so their layers are fitted to noise
already described to them. Re-derive from the API shape if you want the same
confidence, and strike this paragraph if you cannot — an unfalsifiability claim that
has silently become false is worse than none at all.

### The count that looks like signal, and the count that is

Measured 2026-08-27 across the 24 live manifest projects: **58 failed or
startup-failed runs in 10 of the 24.** Deduplicated to distinct broken things on a
branch anyone cares about, it is **0 of 24**. Three readings agree:

| Reading | Result |
|---------|--------|
| newest run per workflow on each default branch | 1 red project fleet-wide |
| worst project spot-checked (`portaBaltica`, 29 failures) | default branch fully green |
| workflows failing >=20% of default-branch runs | 2, both deploy/agent infrastructure |

Two artefacts inflate the aggregate, and both are properties of the API rather than
of the fleet:

- **Failures on merged and deleted branches persist forever.** `portaBaltica`'s
  code-level failures (`Test`, `Run the content-safety suite`, `Lint`, `Type check`)
  were all on pull-request branches and all fixed before merge — CI doing its job. A
  recent-failures window resurfaces them as live defects indefinitely.
- **One bad workflow file emits one failed run per push.** autoRefine's own 10
  path-named runs are a single defect counted ten times.

**This is the open-issue trap one layer down.** 5 open non-PR issues across 24
projects is visibly too thin to ground on; 58 failed runs is not visibly thin, and
reads as a fleet in trouble until it is deduplicated. The aggregate is the number the
API hands you first. Distrust it.

### The collision that outlives the counts

The one red default branch was autoRefine's own `Evaluate All Projects`, failing at
step `Azure login` — which **hard rule 3 forbids autoRefine from fixing.** A perfect
classifier, applied to the entire live population of failures it can find, yields one
idea, and that idea violates a hard rule.

Unlike the counts, this does not expire. Credential and infrastructure failures are
the class that survives *longest* on a default branch, precisely because no pull
request the idea could buy will clear them. A fleet that goes red tomorrow is most
likely red in exactly the way autoRefine may not repair.

### Why this is not the precedent set just above

`_abandoned_after_build` also adds 0 tokens per run today and was built anyway, so
the difference has to be stated or this section contradicts that one. That channel is
silent *temporally* — declines and closed-unmerged PRs accumulate as the pipeline
runs — and it needs no classifier, because a human's comment is self-evidently a
human's comment. This one is silent *structurally*: it is empty because CI is
working, and it would need a deny/allow list over step names kept correct against 24
repositories' naming conventions indefinitely. A silent check with no moving parts is
cheap insurance; a silent check with a drifting heuristic is a liability that pays
nothing.

### Re-running it

The counts expire under hard rule 7; the method does not. For each live manifest
project take the newest completed run per workflow on the default branch, drop layers
1 and 2, fetch `/jobs` for what remains, and keep only runs whose failed step matches
the code allow-list. A full sweep is ~49 calls for 24 projects against a 15,000/hr
core budget, so cost is not the obstacle and never was.

Build it the day that returns a non-empty set for more than one or two projects — and
re-check the hard rule 3 collision first. There is deliberately no committed script,
for the reason given under "What the score actually measures".

## Why `refine` mode is implemented and not enabled

`refine` is the autonomous-fix path: plan, apply, test, branch, push, open a PR.
It is fully implemented (`agent/main.py`, `refine_project`) and has **never run in
production**, because `infrastructure/run-autorefine.sh` hard-codes
`--mode file-ideas`. Enabling it was considered on 2026-08-28 and **declined**.

The reason is not the one that looks obvious. It is not that agent PRs pile up
unmergeably — measured below, they merge fine. It is that **the only demonstrated
way they merge is a human merging past CI that never ran**, and `refine` would
scale exactly that.

### What was measured, 2026-08-28

Across the 25 live manifest projects, every PR authored by the Copilot coding
agent, and every workflow run on those PRs' head branches:

| | |
|---|---|
| Copilot-authored PRs, all states, all time | **5**, in 3 repos |
| …merged | **3** (`portaBaltica` #179, #180, #181, all that day) |
| Workflow runs on those branches | **22** |
| …at `run_attempt = 2` | **0** |
| `event=pull_request` runs among them | 16 |
| …that concluded `success` | **0** |
| …`action_required` (awaiting a human click) | 6 |
| …`failure` | 10 |

The 5 runs that did succeed are all `Running Copilot cloud agent`
(`event=dynamic`) — the agent building the branch, not CI judging it. **No
`pull_request` run on a Copilot branch has ever passed, once, anywhere in the
fleet.** All 6 `action_required` runs sit on `copilot/*` branches, so the
approval gate is real and it is aimed squarely at this traffic.

And yet three PRs merged, all by `samoletovs`, **14, 25 and 16 seconds after
opening** — past checks showing `action_required` and `failure`. The timing is
worth stating exactly, because it shows the gate was never reached rather than
overridden:

| | `#179` | `#180` | `#181` |
|---|---|---|---|
| PR created | 05:58:03 | 06:04:29 | 06:27:02 |
| merged by a human | 05:58:28 | 06:04:43 | 06:27:18 |
| the `action_required` CI run was queued | 05:58:31 | 06:04:46 | 06:27:20 |

**The blocked CI run starts 2–3 seconds *after* the merge it was meant to gate.**
It is queued against a branch that is already in `master`, and there it sits.
Those are three of the six `action_required` runs in the fleet: not a backlog
awaiting review, but residue of merges that had already happened.

**Do not say the human merged over red checks.** The other runs were *queued*
9–21 seconds before each merge, which is tempting to read that way, but none had
*concluded*: measured 2026-08-28, every run on all three PRs concluded 1–3
seconds **after** the merge, so **0 failures were visible at merge time**. The
merge did not override a red build — it happened before CI could report at all.
Queued-before and concluded-before are different claims and only the second would
support that reading.

`Auto-merge Copilot PRs` started 9–21 seconds before each merge and concluded
`failure` on all three. Whether it failed on its own merits or because a human
merged the PR out from under a run already in flight is **not determinable** from
the run data — but either way it did not perform the merge, and a human did.

This is the same defect as "An empty check rollup is not evidence that CI is
green", seen from the other end. That section explains why the PR-card sweep read
these PRs as ready: at the moment it looks, the rollup is `[]`, because a run held
at `action_required` has produced no check run. The timing above says where the
`[]` comes from — the run had not been queued yet when the PR was merged, and
never will produce a job. Both were verified on `portaBaltica#181`.

**The two measurements have different scopes and do not disagree.** They count
different populations, and the split is not marginal. Measured 2026-08-28 —
Copilot-authored pull requests, all states, across the non-archived org:

| Population | Copilot PRs |
|---|---|
| `mindVault` (private, **not in the manifest**) | 87 |
| `nauroLabs-github` (governance, **not in the manifest**) | 19 |
| manifest projects (`portaBaltica` 4, `atlas` 1, `era` 1) | 6 |
| `familyVault` | 2 |
| **org total** | **114** |

**108 of 114 agent pull requests in this org are invisible to a manifest-scoped
sweep**, and the single largest producer is a repo autoRefine has never heard of.
So a manifest-walking method is right about the fleet it ships and blind to where
the agent is actually used. When these numbers are re-run, say which population
was counted — it is the largest single source of apparent contradiction between
these two sections, and an earlier revision of this paragraph carried an
unsourced "17 org-wide" that reproduces under no definition tried.

`mindVault` is also the second-largest line in the Actions bill (§"What the sweep
actually costs"). It is not a manifest project, so nothing autoRefine measures —
score, cost rows, PR cards — sees any of it.

### Why that is a reason not to enable it

The queue is drainable. That is precisely the problem. The drain is a human
merging in about twenty seconds, before CI has even been queued, and it ran at
three PRs a day. `refine` adds a second producer feeding the same queue, and
nothing in the measurement suggests the *review* would scale with it — a
20-second lag is already too short to have read a diff.

So enabling `refine` does not risk a stalled queue. It risks a faster one,
draining the same way, with more in it. The thing to fix first is the merge path,
not the supply of PRs.

Two supporting conditions, both weaker than the above and both temporary:

- **The card/merge loop is switched off.** `AUTOREFINE_TIER=critical` skips
  `.github/workflows/pr-ready-cards.yml`; every scheduled run since 2026-08-22 is
  `skipped`, as is every run of the health scan.
- **August's Actions allowance is spent**: gross $64.56, discount $55.60, **net
  $8.97** overage on ~10,760 minutes. Read it with
  `gh api "/users/samoletovs/settings/billing/usage?year=2026&month=8"`, summing
  `grossAmount`/`discountAmount`/`netAmount` over `product == "actions"`; the
  older `/settings/billing/actions` endpoint now answers **HTTP 410**.

**Do not write that the brake was engaged because autoRefine is expensive.** It
is not and never was: autoRefine is public, public minutes are free, and its
entire August Actions cost is **$0.01** across 130 minutes. The brake bought
about one cent. The bill is elsewhere — `nauroLabs-github` at $2.95 (33% of net)
and `mindVault` at $2.03 (23%), the rest a long tail of `*-legacy` repositories.

### What would change the decision

A `pull_request` run on a `copilot/*` branch **in a manifest project** that
concludes `success` at `run_attempt = 1`. That single fact would mean the approval
gate has been configured away or satisfied, that CI is judging agent work rather
than being skipped past, and that a merge could rest on something. Until then,
more agent PRs buy more unreviewed merges.

**The scope qualifier is load-bearing.** `nauroLabs-github#191`
(`copilot/restart-nauro-ops-loop`) already meets the bare condition — measured
2026-08-28, its `Auto-review and merge` run is `success` at `run_attempt = 1`. So
without "in a manifest project" the criterion reads as already satisfied while
every project the fleet actually ships remains at zero. That repo is also the one
with a working `Auto-review and merge`, which is why it clears and the others do
not: **it is the example to copy, not evidence the problem is solved.**

Fixing `Auto-merge Copilot PRs` would also change it — but establish first whether
it is broken. It is the only automation that could drain this queue on evidence
rather than on patience, and nobody has yet read why it fails.

### Re-running the measurement

Take the live manifest, list every PR whose `user.login` is `Copilot`, and for
each fetch `/actions/runs?branch=<head>&per_page=100`; group by `event`,
`run_attempt` and `conclusion`, and read `merged_by` alongside them. Cross-check
the blocked set with `/actions/runs?status=action_required` per repo, which
answers directly and avoids paginating history. About 55 calls for 25 projects
against a 15,000/hr budget.

**The step that produced the sharpest finding is comparing each run's
`created_at` against the PR's `merged_at`.** Run-level conclusions alone say
"blocked"; the timestamps say the blocked run was queued *after* the merge, which
is a different fact and the one that matters. Do not skip it. There is
deliberately no committed script, for the reason given under "What the score
actually measures".

**`branch=` and `head_sha=` are different questions, and this document uses both.**
This recipe says `branch=`; "An empty check rollup is not evidence that CI is
green" says `head_sha=`. Both are correct for what they ask — `head_sha=` asks what
ran on the PR's *current* head, which is what a live readiness check needs;
`branch=` asks what ever ran on the branch, superseded commits included, which is
what a historical claim needs. They do not agree. Measured 2026-08-28 over the same
19 Copilot PRs in `nauroLabs-github`:

| Query | total runs | `pull_request` | `run_attempt` | `action_required` | successes |
|---|---|---|---|---|---|
| `head_sha=` | 24 | 21 | 14 × 1, 7 × 2 | 11 | 6 (1 at attempt 1) |
| `branch=` | 49 | 26 | 19 × 1, 7 × 2 | 13 | 7 (2 at attempt 1) |

Ten of the 19 PRs return nothing at all under `head_sha=`. So "say which population
you counted" has a second half — **say which question you asked the API** — and a
reader comparing counts across these two sections without it will conclude one of
them is wrong. Neither is.

Note what did *not* move: `7 × 2` is identical under both, and it is the figure the
argument rests on. **A conclusion that survives a change of query is worth more than
one that needs the right query to hold**, and re-running a count both ways is the
cheapest test available of whether a finding is real. Also paginate
`pulls?state=all`; a repo's Copilot PRs run back past one page, and truncating there
is what produced a wrong PR count the first time this was measured.

**Every number here expires under hard rule 7**, and this set is unusually
volatile: all three merges happened on the day of measurement, so the fleet had a
one-day history of merging agent PRs at all. The Actions billing figures moved
between two reads eight minutes apart (gross $64.39 → $64.56) because the meter
is live; `action_required` counts move too, the moment somebody clicks.

**The deploy trap applies if this is ever reversed.**
`infrastructure/run-autorefine.sh` is inlined into the ARM template by
`loadTextContent` at deploy time, so changing `--mode file-ideas` there does
nothing until a human runs `az deployment group create`. See "The entrypoint is
baked in; the Python is not".

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

**That 552:1 is a 2026-08-21 reading and is not current** — see "The ratio,
re-measured" below, which reads 58.5:1 from a different instrument.

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

### The ratio, re-measured

Measured 2026-08-28 from the only cost file that exists,
`samoletovs/nauroLabs-github` → `reports/cost/run-2026-08-28-0607.jsonl`, filtered to
the 10 genuine rows (see the contamination note under "Sizing the job ceiling"):
**prompt 500,232, completion 8,558 — 58.5:1**, over 6 projects in one sweep.

**Do not call that a 9× improvement on 552:1.** The two numbers come from different
instruments and are not comparable:

| | 552:1 | 58.5:1 |
|---|---|---|
| Source | Azure billing meter, month-to-date | `RunCompletionUsage`, per run |
| Scope | one **cached** SKU, aggregated over a month | 10 runs, one sweep, n=10 |
| Date | 2026-08-21 | 2026-08-28 |

If the cached SKU was a subset of total input rather than all of it, the old true
ratio was lower and any improvement is overstated. Nobody has checked which, and
until someone does the honest statement is "58.5:1 on 2026-08-28, n=10" with no
comparison attached.

**Do not attribute the change to the activity gate.** The gate changes how many runs
happen; this ratio is per-run. It moves the bill and can never move this number.
`truncation_strategy` is the plausible cause, and the `system.md` cache prefix may
contribute — but the mechanism is unconfirmed, and the next entry is why.

**58.5:1 is a total, not a description of a run.** The per-run spread is
min 10.8, median 61.8, max 84.7 — nearly 8×. Quote the aggregate for cost, never
for "what a run looks like".

#### An open contradiction, recorded rather than resolved

The driver table above says a plan run is ~74 rounds and input is O(rounds²).
Observed on the same 10 rows: `rounds` = 1, 4, 5, 5, 6, 6, 6, 7, 7, 8 — mean 5.5,
and **6.0 for `plan` alone**, which is the like-for-like comparison and makes the
gap wider, not narrower.

74 → 6.0 is a 12.3× drop. Squared, that predicts ~152× less input. The observed
ratio change is 9.4×. Something is wrong by more than an order of magnitude, and at
least one of these is the culprit:

- the O(rounds²) model — but `truncation_strategy` was *designed* to convert it to
  roughly O(rounds), so comparing both regimes under one model is itself suspect;
- the ~74 figure, which may never have been representative;
- the cross-instrument comparison in the table above, which is already known to be
  unsound.

**Truncation does not explain the round collapse.** It caps context *per round*; it
does not make a model ask for fewer rounds. Why rounds fell from ~74 to ~6 is
genuinely unexplained. Do not assert a cause — measure it when there are files to
measure.

#### `plan_captured` is not evidence about `submit_plan` — measured, and it is worse

This section previously read the cost rows as the evidence AGENTS.md asks for before
tightening the truncation window: *"Don't tighten the window below 12 without evidence
that runs still reach `submit_plan`."* **It is not that evidence, and the production
logs say the opposite.**

`plan_captured` is `plan_result is not None`, and `plan_result` is assigned in **two**
places: `foundry_agent.py:1110` when the model calls `submit_plan`, and `:1162` when it
did not and autoRefine scraped a plan out of its prose instead
(`_parse_plan_from_text`). The row cannot tell you which fired. Only the log line can.

Measured on the 2026-08-28 sweep — **9 `Parsed plan from text response (submit_plan not
called)` lines across the 10 genuine runs.** The model essentially never calls
`submit_plan`. Every run in that sweep terminated holding a plan, and almost none of
them terminated the way this document assumed.

**Confirmed on a second, independent sweep.** 2026-08-29: **11 fallback lines against
11 runs that captured a plan — `submit_plan` was called zero times all day.** Two
consecutive sweeps, 20 of 21 successful runs on the scraping path. This is not a
one-day artifact, and the second sweep is the cleaner sample: it is the first cost file
written after `_test_subprocess_env` landed, so it carries no test fixtures at all
(12 rows, 12 genuine, 0 fixtures — the contamination in the 08-28 file is described
under "What the sweep actually costs").

**That 9 is a count and not a floor, and the reason is load-bearing.** The log line sits
behind `if plan_result:`, so a fallback that set `plan_captured` without logging would
hide inside the remaining 1. It cannot: `_parse_plan_from_text` returns `None` on
unparseable text rather than a falsy dict — verified 2026-08-28 for unparseable prose,
for a bare `Score: 70`, and for the empty string — so the fallback cannot set
`plan_captured` without also logging. Change that function's failure return and this
number silently becomes a lower bound.

**That inverts the reassurance.** A run that stops calling tools and writes prose
instead is what a truncated run losing its thread looks like, so a high
`plan_captured` rate is as consistent with the hazard as with safety. Reading it as
safety is the `npm audit` shape again: a value that is present, correct, and means two
opposite things.

What the rows do support, and it is worth keeping: all 10 were
`RunStatus.COMPLETED` with `guard = None` at 1–8 rounds, so no run hit a loop guard and
none ended empty-handed. That rules out the worst case. It says nothing about the
window, because the rows record `rounds` and `tool_calls`, not messages —
`prime`/`file-ideas` at 1 round and 3 tool calls cannot have reached 12 messages, so
truncation never engaged on it at all.

Two questions this opens, neither answered:

- **Is `_parse_plan_from_text` load-bearing production code?** On this evidence it is
  the normal path, not a fallback, and it is named and documented as a fallback. Nobody
  has looked at whether the scraped plans are as good as submitted ones.
- **Did truncation cause this, or has the model always ignored the tool?** One sweep
  cannot say. The cheap test is the same grep against a pre-truncation sweep's logs.

The measurement is one `az monitor log-analytics query` for
`Parsed plan from text response` over a sweep's `ContainerGroupName_s`. Re-run it before
believing anything in this subsection; per hard rule 7 the count is from one day.

### The ratio across six clean files

The entry above was written on one file and said to wait for several. Measured
2026-09-03 over the six clean files (2026-08-29 … 09-03), **84 runs, 23 projects**:

| | |
|---|---|
| aggregate | prompt 3,330,075 / completion 59,232 — **56.2:1** |
| per-run spread | min 10.3, median 52.1, max 159.4 |
| rounds, `plan` | n=51, median 6.0, max 16 |
| rounds, `file-ideas` | n=33, median 5.0, max 10 |

Per-file ratios are 45.7 / 52.3 / 54.7 / 60.6 / 60.9 / 60.9 — a 1.33× spread, so the
aggregate is stable enough to quote **for cost**, and the 15× per-run spread still means
it never describes a run.

**It excludes the tripped runs, which is the one thing to remember about it.** The 11
`stuck_tool_loop` runs carry `prompt_tokens: null` (see "What the sweep actually costs"),
so they contribute nothing to the numerator and are not in the 84's token totals. The
true cost per sweep is higher than these figures by an unmeasured amount.

The `plan` median of 6.0 is unchanged from the single-file reading, which is worth more
than the ratio: it is the figure that contradicts the driver table's ~74, and it now
survives a 5× larger sample.

### Re-running the ratio measurement

Files from 2026-08-29 onward are clean (`_test_subprocess_env` and
`tests/conftest.py` now strip `AUTOREFINE_*` from child processes); 08-28 is not and
must be dropped or filtered.

Fetch each `reports/cost/*.jsonl` from `nauroLabs-github`, keep rows whose `run_id`
starts `run_`, and report: aggregate prompt/completion, the **per-run** ratio spread,
`rounds` by `mode`, and the `status`/`plan_captured`/`guard` counts. Split by `mode` —
`plan` and `file-ideas` differ — and treat any span across a config change as two
regimes, not one distribution. There is deliberately no committed script, for the
reason given under "What the score actually measures".

**Pair it with the run logs, not just the rows.** Count how many runs logged `Parsed
plan from text response (submit_plan not called)`. A `plan_captured` count read
without it cannot tell a run that submitted a plan from one that wrote prose and had
it scraped — see the previous entry, which is the whole reason that distinction
matters.

### What `reports/cost` does not measure

"Cost" there means **Foundry LLM tokens, and nothing else**. The row schema is
`project, mode, rounds, run_id, status, guard, plan_captured, prompt_tokens,
completion_tokens, total_tokens, tool_calls, duration_s, ts` — no field for GitHub
Actions minutes, and no field for money in any currency. The €5/month cap in hard
rule 6 is a cap on that one meter.

The fleet's other meter is real and entirely unmodelled. August 2026 Actions, read
2026-08-28: gross $64.56, discount $55.60, **net $8.97** on ~10,760 minutes. The
largest lines are `nauroLabs-github` at $2.95 (33% of net) and `mindVault` at $2.03
(23%) — and **`mindVault` is not in the workspace manifest at all**. So there are two
limitations stacked, and the first is the sharper: Actions spend is not in the schema,
so autoRefine cannot see it for any repo; and even if it were, the telemetry is
manifest-scoped, so `mindVault` would stay invisible.

Do not conclude the fleet costs whatever the cost log says. It reports one meter.

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

**The first cost file is partly fabricated. Filter it, don't avoid it.**
`nauroLabs-github`'s `reports/cost/run-2026-08-28-0607.jsonl` — the first one ever written,
so the tempting baseline — has 18 of its 28 rows fabricated. **Keep the rows whose `run_id`
starts `run_`** (a real Foundry id). That leaves 10 genuine rows, and they are good data:
"The ratio, re-measured" above is built on them.

This warning used to open with "do not read it as data", and that cost something: a reader
took it as "don't look" and nearly discarded every finding in that section. The remedy
belongs first — the story second.

The story: autoRefine is in its own manifest, so planning itself made the model call
`run_project_tests`, and pytest inherited the entrypoint's `AUTOREFINE_COST_LOG` and
appended `tests/test_foundry_agent.py`'s fixtures. Three markers catch all 18 —
`project: "demo"`, `run_id: "run-1"`, `duration_s: 0.0`, all inside 0.11s. A null
`prompt_tokens` looks like a fourth and **is not**: measured 2026-08-28 it holds for only
14 of the 18, so it leaks four fixtures into a set you believe is clean. Use `run_id`.

It did not merely pad the file, it **inverted its headline finding**. The fixtures carry 7
`stuck_tool_loop` trips and 2 `max_tool_rounds`, so the file reads as though the loop
guards fire constantly; across the 10 genuine rows the real count of both is **zero**.
Sum tokens over those 18 and the bill is wrong in the other direction too.

`_test_subprocess_env` strips every `AUTOREFINE_*` before the child starts and
`tests/conftest.py` does the same from the suite's side, so files from 2026-08-29 onward
are clean and need no filtering.

**Confirmed in production, 2026-08-29:** `run-2026-08-29-0607.jsonl` is 12 rows, 12
genuine, **0 fixtures**, against the previous day's 28/10/18.

That file is also the first honest record of a *failure*: one
`portaBaltica`/`file-ideas` run carries `RunStatus.FAILED` with `plan_captured=False`,
from a transient Foundry `server_error`, followed by a successful retry. The retry logic
worked and the telemetry said so — which the contaminated file could not have done,
since its 7 `stuck_tool_loop` and 2 `max_tool_rounds` trips were all fabricated.

**The sentence that used to end this paragraph said the real guard count "is zero, twice
over". Six clean files later it is 11, and hard rule 7 is why.** The measurement was
correct for the two sweeps it covered; the error was writing a two-sample count in a form
that reads as a property of the system. Measured 2026-09-03 over the six clean files:

| day | runs | `stuck_tool_loop` |
|---|---|---|
| 08-29 | 12 | 0 |
| 08-30 | 12 | 2 |
| 08-31 | 14 | 0 |
| 09-01 | 16 | 1 |
| 09-02 | 19 | 3 |
| 09-03 | 11 | **5** |
| **total** | **84** | **11 (13%)** |

Three things about it, in decreasing confidence:

- **The waste is total.** `plan_captured` is `False` on all 11. A tripped run produces
  nothing at all, so this is the one failure mode with no partial credit.
- **Its cost is invisible.** A tripped run ends `RunStatus.REQUIRES_ACTION`, so
  `_run_token_usage` finds no usage and the row carries `prompt_tokens: null`. Every
  token aggregate in this document therefore **excludes** these runs and understates the
  bill. Do not read 56.2:1 as covering the whole sweep.
- **The cause is not established, and one plausible story is already half-refuted.** In
  the one sequence traced from logs, the repeated batch was `run_project_tests`. The job
  image is `python:3.12` with no node (`infrastructure/main.bicep:141`, no install step
  in the entrypoint), so for any project with a `package.json` that tool can only ever
  return `Test runner 'npm' is not installed in this environment` — the `npm audit`
  shape again, one tool over. But 5 of 9 tripped projects have a `package.json` and so
  do 2 of 6 that completed cleanly, so it is at most a contributing factor. **Do not fix
  this on the strength of that correlation.**

The rising trend is six points and small; treat it as an observation to re-run, not an
established rate. Re-run with: fetch every `reports/cost/*.jsonl`, drop the contaminated
08-28 file, and count `guard` by day. Tool *results* are never logged — only calls — so
the return value that provokes a repeat cannot be read from Log Analytics, and
establishing the cause needs either a logged tool result or a reproduction.

### The cause, found the next day, and the fix

The trend continued: **2026-09-04 was 7 trips in 14 runs — 50%**, taking the corpus to
18 in 98. At that rate the sweep spends half its Foundry budget producing nothing, so the
"do not fix on a correlation" above was answered by getting better evidence rather than
by waiting.

**The `package.json` correlation is refuted, and `golazo` is what refutes it.** It ran
clean on 09-03 and tripped on 09-04 with the same manifest, so no property of the project
decides this. What decides it is what the model does with a particular tool result.

**Every one of the seven aborts on 09-04 ends with the same two calls:**
`run_project_tests({})`, immediately repeated. Read from the tool-call log, not inferred:

```
list_directory({}) / read_project_file(...) × 4
run_project_tests({})
run_project_tests({})          <- identical, three rounds running
--- Aborting (stuck_tool_loop) at round 5
```

The tool returned `Test runner 'npm' is not installed in this environment`. That is
**true**, and it is why the retry happens: it reads like a hiccup — something that might
differ next round. It cannot. The image is `python:3.12` with no Node.js and no install
step, so for a project carrying a `package.json` that call fails on every round of every
sweep forever. Two identical retries is all it takes to trip a guard set at three.

The fix is not to make the tool work — installing Node.js fleet-wide to run arbitrary
`npm test` is a much larger decision. It is to stop a permanent condition being reported
in language that invites a retry. `_terminal_tool_error` adds `retryable: False` and an
explicit instruction not to call again, and `shutil.which` checks up front so the answer
does not depend on catching an exception. **A timeout and an `OSError` deliberately keep
the old shape:** those really can differ next time, and marking them terminal would trade
this bug for a quieter one.

**This does raise fleet-wide output**, and that is the point rather than a side effect:
18 runs that produced nothing would have produced plans. It restores intended behaviour
rather than exceeding it, but expect ideas from projects that have been silently
skipped, and watch the first sweep after it lands.

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

## An empty check rollup is not evidence that CI is green

`_checks_green()` used to treat an empty `statusCheckRollup` as green, on the reasoning
that a repo with no CI has nothing to wait for. That reasoning is sound and the conclusion
was still wrong, because the rollup is not a list of *workflows* — it is a list of **check
runs**, and a workflow run that never produced a job produces no check run. Verified
2026-08-28 on `portaBaltica#181`: all four runs on its head SHA return
`total_count: 0` from `/actions/runs/{id}/jobs`, and its rollup is `[]`.

So an empty rollup means either "no CI ran" or "CI never started", and the sweep could not
tell them apart. This is the `npm audit` shape from "The dependency check" — a failure that
looks clean — **inverted and worse**: it did not look clean, it looked *ready to merge*.
The card it sends says "CI is green" and carries a 👍 that nauroBot turns into an
approve + squash-merge, so the bug's payload is merging code no test ever ran on.

The gate is `conclusion: action_required` on `/repos/{repo}/actions/runs?head_sha={sha}`,
which is GitHub holding a run until a human presses *Approve and run workflows*. This is
the **default state of a Copilot PR** in a repo with CI, not an edge case, which is why it
hits exactly the population this sweep exists to serve. It is a literal API value, so
unlike the CI-signal grounding declined above it needs no deny/allow list kept correct
across 24 repos as they rename their steps.

Measured 2026-08-28 across 17 Copilot PRs (every open one org-wide plus a sample of
closed):

| Shape | n | Old verdict |
|-------|---|-------------|
| rollup non-empty | 6 | correct — new code never runs |
| rollup `[]`, no runs at all | 4 | green, correct |
| rollup `[]`, only good runs | 1 | green, correct |
| rollup `[]`, `action_required` present | **6** | **green — wrong** |

Both open Copilot PRs org-wide were in the wrong row (`atlas#4`, `nauroLabs-github#213`),
and `nauroLabs-github#205` was merged in that state.

Four things are load-bearing:

- **`[]` and `None` are different answers.** `_workflow_runs` returns `[]` only for "the
  API said there are no runs" and `None` for every failure — absent `gh`, non-zero exit,
  malformed body, missing key, timeout, kill switch. Collapsing them would rebuild the
  exact bug one layer down. `TestNoFailureLooksClean` walks every path.
- **It fails closed, and the asymmetry is why.** An unknown CI state cards nothing. A
  missed card costs a day and is retried on the next sweep, because the PR stays
  unlabelled; a card sent wrongly cannot be recalled and may already have been tapped.
  This is the opposite choice from the activity gate, which fails *open* — there, being
  wrong costs one run, and here it can merge untested code.
- **The blocked card has no buttons and no `arfpr:` token.** nauroBot turns 👍 on an
  `arfpr:` card into a squash-merge. A card whose whole message is "CI has never run"
  must not offer to merge, or the fix hands over the very tap it exists to prevent. It is
  a nudge with a link, because the only thing that clears `action_required` is a human
  clicking in the GitHub UI.
- **Withholding alone was not enough.** `action_required` never clears on its own, so
  suppressing the false green would have converted it into a permanent silent stall of the
  idea → build → merge funnel, with nothing anywhere telling the human to click. A distinct
  card is what keeps the pipeline alive.

`AUTOREFINE_SKIP_RUN_CHECK=1` stops the new call, on the reasoning behind
`AUTOREFINE_SKIP_DEPENDABOT`. It degrades to "unknown", **never** to the old card-anyway
behaviour — the switch exists to stop a network call, not to re-enable a bug.

**Narrow would have sufficed; general was chosen anyway.** On all 17 sampled PRs every
false green carried an `action_required` run, so matching only that value would have given
identical results. `_runs_verdict` also applies `_GOOD_CONCLUSIONS` — the set already
applied to the rollup — to the runs the rollup could not see, so an invisible *failed* run
also blocks. That is the same rule on better data rather than a new one, it diverges on
nothing measured, and it exists because the invisible-run mechanism was measured directly
(8 zero-job `failure` runs across `portaBaltica#179/180/181`) rather than inferred.
`BLOCKED` beats `NOT_GREEN` when both are present, because until the gate opens no other
run's verdict is the whole picture.

**Why this was built while the sweep is switched off.** `AUTOREFINE_TIER=critical` makes
`pr-ready-cards.yml:52` skip scheduled runs, and all 6 scheduled runs since 2026-08-22 are
`skipped`, so this changes nothing today. It survives the dead-code test on the same
grounds `_abandoned_after_build` did, and fails the test the CI-signal grounding failed:

1. **Silent temporally, not structurally.** The brake is a repository variable one click
   clears. Measured 2026-08-28 it gates a *public* repo whose entire month of Actions came
   to 130 minutes, $0.78 gross and **$0.012 net** — public-repo minutes are free, so the
   brake is not load-bearing on cost here and may lift at any time. The real August bill is
   `nauroLabs-github` ($2.95) and `mindVault` ($2.03) of $8.97, both private and neither
   gated by `AUTOREFINE_TIER`.
2. **The signal is non-empty now.** `atlas#4` is a live false green, not a hypothetical.
3. **No heuristic.** One literal API value, with no names to maintain.

**Every count above expires under hard rule 7.** To re-measure: list open PRs per manifest
project with `--json headRefOid,statusCheckRollup`, keep the Copilot-authored ones with an
empty rollup, and read `/actions/runs?head_sha=…` for each. There is deliberately no
committed script, for the reason given under "What the score actually measures".

## Test

```bash
pytest tests/ -x -q
```
