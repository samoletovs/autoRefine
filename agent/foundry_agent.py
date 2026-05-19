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
import time
from pathlib import Path
from typing import Any

import httpx
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    FunctionTool,
    ListSortOrder,
    MessageRole,
    RequiredFunctionToolCall,
    SubmitToolOutputsAction,
    ToolOutput,
)
from azure.core.exceptions import HttpResponseError
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

DEPLOYMENT = os.environ.get("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-4o-mini")
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")


# ── Tool definitions as typed Python functions (SDK inspects these) ──────────

def read_project_file(path: str, max_lines: int = 200) -> str:
    """Read a file from the project repository to inspect source code, configs, or docs.

    :param path: Relative path from project root, e.g. 'src/App.tsx' or 'package.json'
    :param max_lines: Maximum number of lines to return (default 200)
    """
    return ""  # Stub — actual execution in TOOL_HANDLERS


def list_directory(path: str = ".") -> str:
    """List files and subdirectories in a project directory.

    :param path: Relative path from project root. Use '.' for root.
    """
    return ""


def search_web(query: str) -> str:
    """Search the web for information about similar products, features, or best practices.

    :param query: Search query, e.g. 'Fitbod app key features 2026'
    """
    return ""


def run_project_tests() -> str:
    """Run the project's test suite. Returns pass/fail and output."""
    return ""


def submit_plan(
    score: int,
    summary: str,
    improvements: list,
    research_insights: list | None = None,
) -> str:
    """Submit a structured improvement plan after analyzing the project.

    :param score: Overall project quality score 0-100
    :param summary: 2-3 sentence executive summary of findings
    :param improvements: Ordered list of recommended improvements (dicts with title, description, priority, effort, category)
    :param research_insights: Insights from researching similar products
    """
    return ""


def write_project_file(path: str, content: str) -> str:
    """Write or overwrite a file in the project repository. Use for applying improvements.

    :param path: Relative path from project root, e.g. 'src/utils/helpers.ts'
    :param content: The full file content to write
    """
    return ""


def apply_improvement(title: str, description: str, files_changed: list) -> str:
    """Signal that an improvement has been applied. Call after writing all files for one improvement.

    :param title: Title of the improvement being applied
    :param description: Brief description of what was changed
    :param files_changed: List of file paths that were modified
    """
    return ""


# ── Tool implementations ─────────────────────────────────────────────────────

def _handle_read_project_file(project_dir: Path, args: dict) -> str:
    """Read a file from the project."""
    rel_path = args.get("path", "")
    max_lines = int(args.get("max_lines", 200))
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
    """Web search via DuckDuckGo HTML (no API key needed)."""
    import re as _re

    import httpx

    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "Empty query"})

    log.info("Web search: %s", query)

    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "autoRefine/1.0 (project improvement agent)"},
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()

        # Extract result snippets from DDG HTML
        results = []
        # DDG HTML results are in <a class="result__a"> and <a class="result__snippet">
        title_pattern = _re.compile(
            r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', _re.DOTALL
        )
        snippet_pattern = _re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', _re.DOTALL
        )
        url_pattern = _re.compile(
            r'<a[^>]*class="result__url"[^>]*href="([^"]*)"', _re.DOTALL
        )

        titles = title_pattern.findall(resp.text)
        snippets = snippet_pattern.findall(resp.text)
        urls = url_pattern.findall(resp.text)

        for i in range(min(5, len(titles))):
            # Strip HTML tags from snippets
            title = _re.sub(r"<[^>]+>", "", titles[i]).strip()
            snippet = _re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            url = urls[i] if i < len(urls) else ""
            results.append({"title": title, "snippet": snippet, "url": url})

        if not results:
            return json.dumps({
                "query": query,
                "results": [],
                "note": "No results found. Use your training knowledge instead.",
            })

        return json.dumps({"query": query, "results": results})

    except (httpx.HTTPError, httpx.TimeoutException) as e:
        log.warning("Web search failed: %s", e)
        return json.dumps({
            "query": query,
            "error": f"Search failed: {e}",
            "note": "Use your training knowledge about these products.",
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


def _handle_write_project_file(project_dir: Path, args: dict) -> str:
    """Write a file in the project (for refine mode)."""
    rel_path = args.get("path", "")
    content = args.get("content", "")

    if not rel_path:
        return json.dumps({"error": "No path specified"})

    target = project_dir / rel_path

    # Security: don't escape project directory
    try:
        target.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Path traversal blocked"})

    # Don't allow writing to dangerous paths
    dangerous = {".git", ".env", "node_modules", ".github/workflows"}
    for d in dangerous:
        if rel_path.startswith(d):
            return json.dumps({"error": f"Cannot write to {d}/ — protected path"})

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"status": "written", "path": rel_path, "bytes": len(content)})
    except OSError as e:
        return json.dumps({"error": str(e)})


def _handle_apply_improvement(_project_dir: Path, args: dict) -> str:
    """Acknowledge an improvement was applied."""
    return json.dumps({
        "status": "improvement_applied",
        "title": args.get("title", ""),
        "files_changed": args.get("files_changed", []),
    })


TOOL_HANDLERS = {
    "read_project_file": _handle_read_project_file,
    "list_directory": _handle_list_directory,
    "search_web": _handle_search_web,
    "run_project_tests": _handle_run_tests,
    "submit_plan": _handle_submit_plan,
    "write_project_file": _handle_write_project_file,
    "apply_improvement": _handle_apply_improvement,
}


# ── Agent orchestration ──────────────────────────────────────────────────────

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
MAX_RETRY_ATTEMPTS = 5


def _is_retryable_foundry_error(exc: BaseException) -> bool:
    if isinstance(exc, HttpResponseError):
        status_code = getattr(exc, "status_code", None)
        if status_code is None and getattr(exc, "response", None) is not None:
            status_code = getattr(exc.response, "status_code", None)
        return status_code in RETRYABLE_STATUS_CODES

    return isinstance(
        exc,
        (httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException),
    )


def _sleep_for_retry(seconds: float) -> None:
    time.sleep(seconds)


def _log_retry_before_sleep(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    log.warning(
        "Retrying Foundry call %s (attempt %d/%d) after error: %s",
        retry_state.fn.__name__,
        retry_state.attempt_number,
        MAX_RETRY_ATTEMPTS,
        exc,
    )


def _foundry_retry() -> Any:
    return retry(
        reraise=True,
        stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
        wait=wait_exponential_jitter(initial=1, max=60),
        retry=retry_if_exception(_is_retryable_foundry_error),
        before_sleep=_log_retry_before_sleep,
        sleep=_sleep_for_retry,
    )


@_foundry_retry()
def _create_run(client: AgentsClient, thread_id: str, agent_id: str) -> Any:
    return client.runs.create(thread_id=thread_id, agent_id=agent_id)


@_foundry_retry()
def _submit_tool_outputs(
    client: AgentsClient,
    thread_id: str,
    run_id: str,
    tool_outputs: list[ToolOutput],
) -> Any:
    return client.runs.submit_tool_outputs(
        thread_id=thread_id,
        run_id=run_id,
        tool_outputs=tool_outputs,
    )


@_foundry_retry()
def _get_run(client: AgentsClient, thread_id: str, run_id: str) -> Any:
    return client.runs.get(thread_id=thread_id, run_id=run_id)


@_foundry_retry()
def _delete_thread(client: AgentsClient, thread_id: str) -> None:
    client.threads.delete(thread_id)


def create_agent(client: AgentsClient, mode: str = "plan") -> str:
    """Create the autoRefine Foundry agent. In refine mode, includes write tools."""
    tool_functions = {
        read_project_file,
        list_directory,
        search_web,
        run_project_tests,
        submit_plan,
    }

    if mode == "refine":
        tool_functions.add(write_project_file)
        tool_functions.add(apply_improvement)

    tools = FunctionTool(functions=tool_functions)

    agent = client.create_agent(
        model=DEPLOYMENT,
        name="autorefine",
        instructions=SYSTEM_PROMPT,
        tools=tools.definitions,
        temperature=0.3,
    )
    log.info("Created agent: %s (mode=%s)", agent.id, mode)
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
    run = _create_run(client, thread.id, agent_id)
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

                run = _submit_tool_outputs(client, thread.id, run.id, tool_outputs)
            continue

        # Poll
        time.sleep(1)
        run = _get_run(client, thread.id, run.id)

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
                agent_text = block.text.value
                log.info("Agent response:\n%s", agent_text)

                # Fallback: if agent didn't call submit_plan, parse from text
                if plan_result is None and "Score:" in agent_text:
                    plan_result = _parse_plan_from_text(agent_text)
                    if plan_result:
                        log.info("Parsed plan from text response (submit_plan not called)")

    # Clean up
    _delete_thread(client, thread.id)

    return plan_result


def _parse_plan_from_text(text: str) -> dict | None:
    """Fallback parser: extract a plan from the agent's text response."""
    import re as _re

    plan: dict = {"score": 50, "summary": "", "improvements": [], "research_insights": []}

    # Extract score
    score_match = _re.search(r"Score:\s*(\d+)/100", text)
    if score_match:
        plan["score"] = int(score_match.group(1))

    # Extract summary from the first paragraph after "Findings"
    lines = text.splitlines()

    # Extract improvements from numbered items with priority tags
    current_title = ""
    current_desc = ""
    current_priority = "P2"
    for line in lines:
        # Match lines like: 1. **Title** — description or 1. [P0] **Title**: description
        imp_match = _re.match(
            r"\d+\.\s+(?:\[P(\d)\]\s+)?\*\*(.+?)\*\*\s*[ù—:\-]+\s*(.*)", line
        )
        if imp_match:
            if current_title:
                plan["improvements"].append({
                    "title": current_title,
                    "description": current_desc,
                    "priority": current_priority,
                    "effort": "M",
                    "category": "quality",
                })
            p = imp_match.group(1)
            current_priority = f"P{p}" if p else "P2"
            current_title = imp_match.group(2).strip()
            current_desc = imp_match.group(3).strip()

    # Append last improvement
    if current_title:
        plan["improvements"].append({
            "title": current_title,
            "description": current_desc,
            "priority": current_priority,
            "effort": "M",
            "category": "quality",
        })

    if not plan["improvements"]:
        return None

    plan["summary"] = f"Score {plan['score']}/100 with {len(plan['improvements'])} improvements identified."
    return plan


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


def build_refine_task(plan: dict, config: ProjectConfig) -> str:
    """Build the task prompt for refine mode — execute auto-fixable improvements."""
    improvements = plan.get("improvements", [])

    items_text = ""
    for i, imp in enumerate(improvements, 1):
        items_text += f"{i}. [{imp.get('priority', 'P2')}] {imp.get('title', '')}: {imp.get('description', '')}\n"

    return f"""You have an improvement plan for this project. Your job is to EXECUTE the improvements.

## Improvements to apply
{items_text}

## Instructions
1. Read the relevant source files to understand the current code.
2. For each improvement you can confidently implement:
   a. Use write_project_file to create or modify files.
   b. Call apply_improvement when done with each improvement.
3. After all changes, run the test suite to verify nothing broke.
4. If tests fail, read the output and fix the issue.
5. Finally, call submit_plan with an updated score reflecting the improvements.

## Rules
- Only implement improvements you are confident about (>80% certainty).
- Skip improvements that require domain expertise you don't have.
- Never modify .env, .git, node_modules, or workflow files.
- Keep changes minimal and focused — don't refactor unrelated code.
- If a test fails after your changes, revert that specific change."""
