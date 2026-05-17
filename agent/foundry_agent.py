"""Foundry agent — the AI brain of autoRefine.

Uses Azure AI Agents SDK to create a Foundry-hosted agent with function-calling
tools. The agent reasons about project findings, researches competitors, creates
improvement plans, and can execute changes.

Requires:
    FOUNDRY_PROJECT_ENDPOINT in .env
    DefaultAzureCredential (az login or managed identity)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    FunctionTool,
    ListSortOrder,
    MessageRole,
    RequiredFunctionToolCall,
    SubmitToolOutputsAction,
    ToolOutput,
)
from azure.identity import DefaultAzureCredential

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

DEPLOYMENT = os.environ.get("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-4o-mini")
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

# ── Tool definitions (JSON schema for function-calling) ──────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": "Read a file from the project repository. Use to inspect source code, configs, docs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from project root, e.g. 'src/App.tsx' or 'package.json'",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default 200)",
                        "default": 200,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from project root. Use '.' for root.",
                        "default": ".",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information about similar products, features, or best practices. Use to research competitors listed in project.yaml.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'Fitbod app key features 2026'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_project_tests",
            "description": "Run the project's test suite. Returns pass/fail and output.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "Submit a structured improvement plan. Call this when you have analyzed the project and are ready to recommend improvements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "score": {
                        "type": "integer",
                        "description": "Overall project quality score 0-100",
                    },
                    "summary": {
                        "type": "string",
                        "description": "2-3 sentence executive summary of findings",
                    },
                    "improvements": {
                        "type": "array",
                        "description": "Ordered list of recommended improvements",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "priority": {
                                    "type": "string",
                                    "enum": ["P0", "P1", "P2", "P3"],
                                },
                                "effort": {
                                    "type": "string",
                                    "enum": ["S", "M", "L"],
                                },
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "feature",
                                        "quality",
                                        "security",
                                        "performance",
                                        "ux",
                                        "docs",
                                        "tests",
                                        "deps",
                                    ],
                                },
                                "auto_fixable": {"type": "boolean"},
                            },
                            "required": [
                                "title",
                                "description",
                                "priority",
                                "effort",
                                "category",
                            ],
                        },
                    },
                    "research_insights": {
                        "type": "array",
                        "description": "Insights from researching similar products",
                        "items": {"type": "string"},
                    },
                },
                "required": ["score", "summary", "improvements"],
            },
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

def _handle_read_project_file(project_dir: Path, args: dict) -> str:
    """Read a file from the project."""
    rel_path = args.get("path", "")
    max_lines = args.get("max_lines", 200)
    target = project_dir / rel_path

    if not target.exists():
        return json.dumps({"error": f"File not found: {rel_path}"})
    if not target.is_file():
        return json.dumps({"error": f"Not a file: {rel_path}"})

    # Security: don't escape project directory
    try:
        target.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Path traversal blocked"})

    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        content = "\n".join(lines[:max_lines])
        truncated = len(lines) > max_lines
        return json.dumps({
            "path": rel_path,
            "content": content,
            "lines": len(lines),
            "truncated": truncated,
        })
    except OSError as e:
        return json.dumps({"error": str(e)})


def _handle_list_directory(project_dir: Path, args: dict) -> str:
    """List a project directory."""
    rel_path = args.get("path", ".")
    target = project_dir / rel_path

    if not target.exists() or not target.is_dir():
        return json.dumps({"error": f"Directory not found: {rel_path}"})

    try:
        target.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Path traversal blocked"})

    entries = []
    skip = {".git", "node_modules", "__pycache__", ".venv", "dist", "coverage"}
    for item in sorted(target.iterdir()):
        if item.name in skip:
            continue
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })

    return json.dumps({"path": rel_path, "entries": entries})


def _handle_search_web(_project_dir: Path, args: dict) -> str:
    """Web search via a lightweight approach (httpx + search API or fallback)."""
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "Empty query"})

    # Use gh CLI's copilot search as a fallback if no search API key
    # For now, return a stub that the agent can work with
    log.info("Web search: %s", query)
    return json.dumps({
        "query": query,
        "note": "Web search not yet connected. Use your training knowledge about these products.",
        "suggestion": "Analyze based on common knowledge of the similar products listed in project.yaml.",
    })


def _handle_run_tests(project_dir: Path, _args: dict) -> str:
    """Run the project's test suite."""
    pkg_json = project_dir / "package.json"
    pyproject = project_dir / "pyproject.toml"

    if pkg_json.exists():
        # Node project
        result = subprocess.run(
            ["npm", "test", "--", "--reporter=verbose"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
    elif pyproject.exists() or (project_dir / "requirements.txt").exists():
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-x", "-q"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
    else:
        return json.dumps({"error": "No test runner detected"})

    output = (result.stdout + "\n" + result.stderr)[-2000:]  # cap output
    return json.dumps({
        "passed": result.returncode == 0,
        "output": output,
    })


def _handle_submit_plan(_project_dir: Path, args: dict) -> str:
    """Receive the structured plan from the agent. Just acknowledge — main.py processes it."""
    return json.dumps({"status": "plan_received", "improvements_count": len(args.get("improvements", []))})


TOOL_HANDLERS = {
    "read_project_file": _handle_read_project_file,
    "list_directory": _handle_list_directory,
    "search_web": _handle_search_web,
    "run_project_tests": _handle_run_tests,
    "submit_plan": _handle_submit_plan,
}


# ── Agent orchestration ──────────────────────────────────────────────────────

def create_agent(client: AgentsClient) -> str:
    """Create (or reuse) the autoRefine Foundry agent."""
    tools = [FunctionTool(functions=TOOL_DEFINITIONS)]

    agent = client.create_agent(
        model=DEPLOYMENT,
        name="autorefine",
        instructions=SYSTEM_PROMPT,
        tools=tools,
        temperature=0.3,
    )
    log.info("Created agent: %s", agent.id)
    return agent.id


def run_agent(
    client: AgentsClient,
    agent_id: str,
    project_dir: Path,
    config: ProjectConfig,
    task: str,
) -> dict | None:
    """Run the agent with a task message. Returns the parsed plan or None."""
    # Create a thread
    thread = client.threads.create()
    log.info("Thread: %s", thread.id)

    # Build the user message with project context
    context = config.to_context()
    message_text = f"""## Project context
{context}

## Task
{task}"""

    client.messages.create(
        thread_id=thread.id,
        role=MessageRole.USER,
        content=message_text,
    )

    # Run the agent
    run = client.runs.create(thread_id=thread.id, agent_id=agent_id)
    log.info("Run started: %s", run.id)

    # Poll for completion, handling tool calls
    plan_result: dict | None = None

    while run.status in ("queued", "in_progress", "requires_action"):
        if run.status == "requires_action":
            action = run.required_action
            if isinstance(action, SubmitToolOutputsAction):
                tool_outputs = []
                for tool_call in action.submit_tool_outputs.tool_calls:
                    if isinstance(tool_call, RequiredFunctionToolCall):
                        fn_name = tool_call.function.name
                        fn_args = json.loads(tool_call.function.arguments)
                        log.info("Tool call: %s(%s)", fn_name, fn_args)

                        handler = TOOL_HANDLERS.get(fn_name)
                        if handler:
                            output = handler(project_dir, fn_args)

                            # Capture plan if this is submit_plan
                            if fn_name == "submit_plan":
                                plan_result = fn_args
                        else:
                            output = json.dumps({"error": f"Unknown tool: {fn_name}"})

                        tool_outputs.append(ToolOutput(
                            tool_call_id=tool_call.id,
                            output=output,
                        ))

                run = client.runs.submit_tool_outputs(
                    thread_id=thread.id,
                    run_id=run.id,
                    tool_outputs=tool_outputs,
                )
            continue

        # Poll
        import time
        time.sleep(1)
        run = client.runs.get(thread_id=thread.id, run_id=run.id)

    if run.status == "failed":
        log.error("Run failed: %s", run.last_error)
        return None

    # Get the final message
    messages = client.messages.list(
        thread_id=thread.id,
        order=ListSortOrder.DESCENDING,
        limit=1,
    )
    for msg in messages:
        if msg.role == MessageRole.AGENT:
            for block in msg.text_messages:
                log.info("Agent response:\n%s", block.text.value)

    # Clean up
    client.threads.delete(thread.id)

    return plan_result


def build_plan_task(findings: list[dict], config: ProjectConfig) -> str:
    """Build the task prompt for plan mode."""
    findings_text = ""
    if findings:
        findings_text = "\n## Quality check findings\n"
        for f in findings:
            findings_text += f"- [{f['priority']}] {f['category']}: {f['description']}\n"

    similar_text = ""
    if config.similar:
        similar_text = f"\n## Similar products to research\n{', '.join(config.similar)}\n"

    return f"""Evaluate this project and create an improvement plan.

1. First, list the project directory to understand its structure.
2. Read key files: README.md, project.yaml, package.json or pyproject.toml.
3. Read a few source files to understand code quality and architecture.
4. Consider the project's goals and what similar products offer.
5. Run the test suite to check current health.
6. Submit a structured improvement plan via submit_plan.

Focus on actionable, specific improvements — not generic advice.
{findings_text}{similar_text}"""
