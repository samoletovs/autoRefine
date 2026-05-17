# AGENTS.md — autoRefine

## What this repo is

autoRefine is a Foundry-powered AI agent that evaluates and improves software projects.
It reads `project.yaml` from each project, understands vision + tech stack, researches
competitors, identifies gaps, creates improvement plans, and executes with tests + PRs.

## Hard rules

1. **Never push directly to main/master.** Always create a branch and PR.
2. **Never delete files** without explicit user confirmation.
3. **Never modify secrets or auth config.** Report findings, don't fix.
4. **Always run tests** after making changes. Revert if tests fail.
5. **Ask the user** when confidence is below 70% on any change.
6. **Cost discipline.** Default to `gpt-4o-mini`. Budget cap: €5/month.

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
- Async for I/O (GitHub API, Foundry calls, web requests)
- `logging` module — never `print()` in production
- `pytest` + `pytest-asyncio` for tests
- `ruff` for linting

## Build & run

```bash
pip install -r requirements.txt
python -m agent.main --repo owner/repo --mode evaluate
```

## Test

```bash
pytest tests/ -x -q
```
