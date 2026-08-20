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

## Test

```bash
pytest tests/ -x -q
```
