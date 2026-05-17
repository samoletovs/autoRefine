# autoRefine

> Projects that improve themselves.

An AI agent powered by [Microsoft Foundry](https://learn.microsoft.com/en-us/azure/ai-foundry/) that continuously evaluates and improves software projects. Goes beyond linting and dependency updates — autoRefine understands your project's vision, researches what similar products do, identifies gaps, creates improvement plans, and executes changes with tests.

## How it works

```
project.yaml → Discover → Research → Evaluate → Plan → [Ask?] → Execute → Test → PR
```

1. **Discover** — reads `project.yaml` + code structure to understand what the project is
2. **Research** — web searches for similar products, compares features
3. **Evaluate** — technical quality checks + functional gap analysis
4. **Plan** — creates a prioritized improvement plan
5. **Ask** — when confidence is low, asks the user before proceeding
6. **Execute** — makes changes to the codebase
7. **Test** — runs the project's test suite to validate changes
8. **PR** — creates a pull request with the improvements

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

# Full cycle (evaluate + plan + execute)
python -m agent.main --repo owner/repo --mode refine

# All projects in a manifest
python -m agent.main --manifest config/workspace-manifest.json --mode refine
```

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

- **Model**: `gpt-4o-mini` (upgrades to `gpt-4o` only for complex code analysis)
- **Tools**: GitHub API, web search, file system, test runner, quality checkers
- **Human-in-the-loop**: agent asks for confirmation on risky or uncertain changes
- **Safety**: all changes are made on branches, tested, and submitted as PRs — never direct to main

## Stack

- Python 3.11+
- Microsoft Foundry (Azure AI Projects SDK)
- Azure OpenAI (`gpt-4o-mini` default)
- GitHub API (`gh` CLI + REST)
- PyYAML for `project.yaml` parsing

## Cost

Target: < €5/month on Azure consumption plan.
