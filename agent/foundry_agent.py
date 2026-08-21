"""Foundry agent — the AI brain of autoRefine.

Uses Azure AI Agents SDK to create a Foundry-hosted agent with function-calling
tools. The agent reasons about project findings, compares against provided
similar products, creates improvement plans, and can execute changes.

Requires:
    FOUNDRY_PROJECT_ENDPOINT in .env
    DefaultAzureCredential (az login or managed identity)
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import httpx
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    FunctionTool,
    ListSortOrder,
    MessageRole,
    RequiredFunctionToolCall,
    SubmitToolOutputsAction,
    ToolOutput,
    TruncationObject,
    TruncationStrategy,
)
from azure.core.exceptions import HttpResponseError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

DEFAULT_DEPLOYMENT = os.environ.get("FOUNDRY_DEFAULT_DEPLOYMENT", "gpt-4o-mini")
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
MAX_FOUNDRY_RETRY_ATTEMPTS = 5
RETRYABLE_FOUNDRY_STATUS_CODES = {429, 502, 503, 504}

AGENT_NAME = "autorefine"
# Only sweep agents older than a full run. A run is ~43 min today; 6h leaves
# generous headroom so a concurrent run's live agent is never collected.
ORPHAN_AGENT_MAX_AGE = timedelta(hours=6)

# Back-compat alias. Older imports referenced DEPLOYMENT directly; keep it
# pointing at the env-resolved default so any cached imports still work.
DEPLOYMENT = DEFAULT_DEPLOYMENT

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

# ── Prompt budget ────────────────────────────────────────────────────────────
# Two distinct levers, easy to conflate:
#
#   max_prompt_tokens  — RUN-WIDE and CUMULATIVE across every turn of a run,
#                        not a per-call limit. The service ends the run as
#                        ``incomplete`` once the sum of prompt tokens over all
#                        turns crosses it. Measured traffic averages ~11.5k
#                        input tokens per call (23M/day over ~2,000 calls), so
#                        a plan doing a dozen tool rounds legitimately spends
#                        well over 100k run-wide. This is therefore only a
#                        runaway guard, deliberately set high enough that a
#                        healthy run never trips it. We have no per-run
#                        distribution yet, so the default is safety-first: a
#                        tighter cap is opt-in via the env var below, and
#                        should only be lowered with quality evidence that
#                        runs still reach submit_plan.
#
#   truncation_strategy — the actual PER-TURN cost lever. ``last_messages``
#                        bounds how much accumulated thread history is re-sent
#                        on each round, which is what turns a run's input cost
#                        from O(rounds^2) into roughly O(rounds).
DEFAULT_MAX_PROMPT_TOKENS = 200_000
DEFAULT_TRUNCATION_LAST_MESSAGES = 12
# A run-wide ceiling below ~20k cannot survive more than a couple of turns and
# would kill plans before submit_plan, so reject it rather than accept a value
# that silently breaks the agent.
MIN_MAX_PROMPT_TOKENS = 20_000
MIN_TRUNCATION_LAST_MESSAGES = 2

T = TypeVar("T")


class FoundryRunIncompleteError(RuntimeError):
    """A run stopped early (``status == "incomplete"``) instead of finishing.

    Raised so a truncated, partial result can never be mistaken for a
    successful plan. ``reason`` carries ``incomplete_details.reason``, e.g.
    ``max_prompt_tokens``.
    """

    def __init__(self, run_id: str, reason: str | None) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(
            f"Foundry run {run_id} ended incomplete (reason={reason or 'unknown'}). "
            "Raise AUTOREFINE_MAX_PROMPT_TOKENS / AUTOREFINE_TRUNCATION_LAST_MESSAGES "
            "or reduce tool output if this recurs."
        )


def _positive_int_from_env(name: str, default: int, minimum: int) -> int:
    """Read a positive-int knob from the environment, validating explicitly.

    An unset variable uses ``default``. A value that is not an integer, or is
    below ``minimum``, is rejected loudly rather than silently coerced.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def resolve_max_prompt_tokens() -> int:
    """Prompt-token ceiling for a run. Override: ``AUTOREFINE_MAX_PROMPT_TOKENS``."""
    return _positive_int_from_env(
        "AUTOREFINE_MAX_PROMPT_TOKENS",
        DEFAULT_MAX_PROMPT_TOKENS,
        MIN_MAX_PROMPT_TOKENS,
    )


def resolve_truncation_last_messages() -> int:
    """Thread window size. Override: ``AUTOREFINE_TRUNCATION_LAST_MESSAGES``."""
    return _positive_int_from_env(
        "AUTOREFINE_TRUNCATION_LAST_MESSAGES",
        DEFAULT_TRUNCATION_LAST_MESSAGES,
        MIN_TRUNCATION_LAST_MESSAGES,
    )


class FoundryPromptBudgetUnsupportedError(RuntimeError):
    """The installed SDK cannot express the prompt budget on ``runs.create``.

    Fails closed. A ``**kwargs``-only signature is *not* evidence of support:
    the generated clients accept arbitrary kwargs and silently drop unknown
    ones, which would leave runs unbounded while appearing to succeed.
    """


def _prompt_budget_kwargs(create: Callable[..., Any]) -> dict[str, Any]:
    """Build the prompt-bounding kwargs, requiring explicit SDK support.

    ``max_prompt_tokens`` and ``truncation_strategy`` must appear as named
    parameters of the pinned ``azure-ai-agents`` ``RunsOperations.create``. If
    they do not, we raise rather than fall back to an unbounded run, so a
    dependency bump can never silently restore the runaway-cost behaviour.
    """
    max_prompt_tokens = resolve_max_prompt_tokens()
    last_messages = resolve_truncation_last_messages()

    try:
        parameters = inspect.signature(create).parameters
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        raise FoundryPromptBudgetUnsupportedError(
            f"Cannot introspect {create!r} to confirm prompt-budget support."
        ) from exc

    missing = [
        name
        for name in ("max_prompt_tokens", "truncation_strategy")
        if name not in parameters
        or parameters[name].kind is inspect.Parameter.VAR_KEYWORD
    ]
    if missing:
        raise FoundryPromptBudgetUnsupportedError(
            "Installed azure-ai-agents SDK does not declare "
            f"{', '.join(missing)} as named parameter(s) of runs.create; "
            "refusing to start an unbounded run. Pin a supported SDK version."
        )

    log.info(
        "Prompt budget: max_prompt_tokens=%d (run-wide), truncation=last_messages(%d)",
        max_prompt_tokens,
        last_messages,
    )
    return {
        "max_prompt_tokens": max_prompt_tokens,
        "truncation_strategy": TruncationObject(
            type=TruncationStrategy.LAST_MESSAGES,
            last_messages=last_messages,
        ),
    }


def _is_retryable_foundry_exception(exception: BaseException) -> bool:
    """Return True only for transient Foundry/network errors worth retrying."""
    if isinstance(exception, HttpResponseError):
        status_code = getattr(exception, "status_code", None)
        return status_code in RETRYABLE_FOUNDRY_STATUS_CODES

    return isinstance(
        exception,
        (httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException),
    )


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    """Emit retry diagnostics for transient Foundry failures."""
    if retry_state.outcome is None or not retry_state.outcome.failed:
        return
    exception = retry_state.outcome.exception()
    if exception is None:
        return
    operation = str(retry_state.args[0]) if retry_state.args else "Foundry call"
    log.warning(
        "Retrying %s after attempt %d/%d due to %s: %s",
        operation,
        retry_state.attempt_number,
        MAX_FOUNDRY_RETRY_ATTEMPTS,
        type(exception).__name__,
        exception,
    )


@retry(
    retry=retry_if_exception(_is_retryable_foundry_exception),
    wait=wait_random_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(MAX_FOUNDRY_RETRY_ATTEMPTS),
    reraise=True,
    before_sleep=_log_retry_attempt,
)
def _call_foundry_with_retry(operation: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a Foundry SDK operation with transient-failure retries."""
    return fn(*args, **kwargs)


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
    :param improvements: Ordered list of recommended improvements. Each is a dict with
        title, description, priority, effort, category, approach, success_criteria.
        `approach` must name the actual files/commands to change — not a restatement
        of the title. `success_criteria` must be checkable by someone who did not do
        the work, e.g. "pytest exits 0 with >=60 passing tests", not "it is implemented".
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


def _handle_run_tests(project_dir: Path, _args: dict) -> str:
    """Run the project's test suite.

    A tool the model calls must never be able to abort the run. Whichever runner is
    missing, times out, or explodes, this reports the failure back to the model as data so
    the remaining projects still get evaluated.
    """
    pkg_json = project_dir / "package.json"
    pyproject = project_dir / "pyproject.toml"

    if pkg_json.exists():
        cmd = ["npm", "test", "--", "--reporter=verbose"]
    elif pyproject.exists() or (project_dir / "requirements.txt").exists():
        cmd = ["python", "-m", "pytest", "tests/", "-x", "-q"]
    else:
        return json.dumps({"error": "No test runner detected"})

    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return json.dumps({
            "error": f"Test runner '{cmd[0]}' is not installed in this environment",
            "passed": False,
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Test run timed out after 300s", "passed": False})
    except OSError as exc:
        return json.dumps({"error": f"Could not run tests: {exc}", "passed": False})

    output = (result.stdout + "\n" + result.stderr)[-2000:]  # cap output
    return json.dumps({
        "passed": result.returncode == 0,
        "output": output,
    })


def _coerce_int(value: Any, default: int = 0) -> int:
    """Coerce a value (possibly a stringified number) into an int."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except ValueError:
                return default
    return default


def _coerce_list(value: Any) -> list:
    """Coerce a value (possibly a JSON-encoded string) into a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
        return []
    return []


def _parse_improvements_list(text: str) -> list[dict]:
    """Recover a structured improvements list from a numbered free-text block.

    gpt-4o-mini sometimes serializes submit_plan's ``improvements`` as a numbered
    string instead of a JSON array, e.g.::

        1. Title \u2014 description \u2014 priority: P1, effort: M, category: feature
        2. Title \u2014 description \u2014 priority: P2, effort: L, category: onboarding

    Without this fallback ``_coerce_list`` returns [] and every idea is silently
    dropped. Splits on the leading item number, pulls the ``priority``/``effort``/
    ``category`` metadata wherever it appears, then separates title from description.
    """
    items: list[dict] = []
    for block in re.split(r"(?m)^\s*\d+[.)]\s+", text):
        block = block.strip()
        if not block:
            continue
        priority = re.search(r"priority\s*[:=]\s*P\s*(\d)", block, re.IGNORECASE)
        effort = re.search(r"effort\s*[:=]\s*([SML])\b", block, re.IGNORECASE)
        category = re.search(r"category\s*[:=]\s*([A-Za-z][\w-]*)", block, re.IGNORECASE)
        # Drop the trailing "[\u2014] priority: \u2026, effort: \u2026, category: \u2026" metadata clause.
        core = re.split(
            r"[\u2013\u2014\u00b7\-]?\s*priority\s*[:=]", block, maxsplit=1, flags=re.IGNORECASE
        )[0]
        core = core.strip().strip("*").strip(" \u2013\u2014\u00b7-:").strip()
        # Title is the text before the first separator; the remainder is the description.
        parts = re.split(r"\s+[\u2013\u2014\u00b7]\s+|\s+-\s+|:\s+", core, maxsplit=1)
        title = parts[0].strip().strip("*").strip()
        description = parts[1].strip() if len(parts) > 1 else ""
        if not title:
            continue
        item: dict = {"title": title, "description": description, "effort": "M", "category": "quality"}
        if priority:
            item["priority"] = f"P{priority.group(1)}"
        if effort:
            item["effort"] = effort.group(1).upper()
        if category:
            item["category"] = category.group(1).lower()
        items.append(item)
    return items


def _normalize_plan_args(args: dict) -> dict:
    """Normalize submit_plan tool arguments: LLMs sometimes serialize ints as strings
    and lists as JSON-encoded strings."""
    normalized: dict[str, Any] = dict(args)
    normalized["score"] = _coerce_int(args.get("score"), default=0)
    normalized["summary"] = str(args.get("summary", "") or "")
    raw_improvements = args.get("improvements")
    improvements = [item for item in _coerce_list(raw_improvements) if isinstance(item, dict)]
    # gpt-4o-mini sometimes passes improvements as a numbered free-text string that
    # isn't valid JSON; recover the structured items so ideas aren't dropped.
    if not improvements and isinstance(raw_improvements, str) and raw_improvements.strip():
        improvements = _parse_improvements_list(raw_improvements)
    normalized["improvements"] = improvements
    research_insights = args.get("research_insights")
    if isinstance(research_insights, str):
        normalized["research_insights"] = research_insights
    elif isinstance(research_insights, list):
        normalized["research_insights"] = research_insights
    else:
        normalized["research_insights"] = []
    return normalized


def _handle_submit_plan(_project_dir: Path, args: dict) -> str:
    """Receive the structured plan from the agent. Just acknowledge — main.py processes it."""
    improvements = _coerce_list(args.get("improvements"))
    return json.dumps({"status": "plan_received", "improvements_count": len(improvements)})


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
    "run_project_tests": _handle_run_tests,
    "submit_plan": _handle_submit_plan,
    "write_project_file": _handle_write_project_file,
    "apply_improvement": _handle_apply_improvement,
}


# ── Agent orchestration ──────────────────────────────────────────────────────

def sweep_orphaned_agents(
    client: AgentsClient,
    max_age: timedelta = ORPHAN_AGENT_MAX_AGE,
) -> int:
    """Delete ``autorefine`` agents stranded by runs that died before cleanup.

    Every run creates an ephemeral agent and deletes it in a ``finally`` block,
    but a hard kill (CI timeout, OOM, container eviction) never reaches that
    block and leaves the agent behind in the Foundry project. Sweeping on
    startup makes the leak self-healing: the next run collects it.

    Agents younger than ``max_age`` are left alone so a run happening in
    parallel never has its live agent deleted out from under it.

    :return: number of orphans deleted.
    """
    cutoff = datetime.now(timezone.utc) - max_age

    try:
        orphans = [
            agent
            for agent in client.list_agents()
            if agent.name == AGENT_NAME and agent.created_at and agent.created_at < cutoff
        ]
    except HttpResponseError as exc:
        log.warning("Could not list agents to sweep orphans: %s", exc)
        return 0

    swept = 0
    for agent in orphans:
        try:
            client.delete_agent(agent.id)
        except HttpResponseError as exc:
            log.warning("Could not delete orphaned agent %s: %s", agent.id, exc)
            continue
        swept += 1
        log.info("Swept orphaned agent %s (created %s)", agent.id, agent.created_at)

    if swept:
        log.info("Swept %d orphaned %r agent(s) from previous runs.", swept, AGENT_NAME)
    return swept


def create_agent(
    client: AgentsClient,
    mode: str = "plan",
    model: str | None = None,
) -> str:
    """Create the autoRefine Foundry agent. In refine mode, includes write tools.

    :param model: Foundry deployment name to use. Falls back to
        ``FOUNDRY_DEFAULT_DEPLOYMENT`` env var, then to ``gpt-4o-mini``.
        Pass a deployment name like ``gpt-5-mini`` (cheap tier) or
        ``gpt-5`` (deep reasoning) — the CLI ``--model`` arg threads
        through to here so callers can pick per-run.
    """
    # Housekeeping only — never allowed to block agent creation.
    if hasattr(client, "list_agents"):
        sweep_orphaned_agents(client)

    tool_functions = {
        read_project_file,
        list_directory,
        run_project_tests,
        submit_plan,
    }

    if mode == "refine":
        tool_functions.add(write_project_file)
        tool_functions.add(apply_improvement)

    tools = FunctionTool(functions=tool_functions)
    deployment = model or DEFAULT_DEPLOYMENT

    agent = client.create_agent(
        model=deployment,
        name=AGENT_NAME,
        instructions=SYSTEM_PROMPT,
        tools=tools.definitions,
        temperature=0.3,
    )
    log.info("Created agent: %s (mode=%s, model=%s)", agent.id, mode, deployment)
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

    # Run the agent with a bounded prompt so accumulated tool output cannot be
    # re-sent in full on every round.
    run = _call_foundry_with_retry(
        "client.runs.create",
        client.runs.create,
        thread_id=thread.id,
        agent_id=agent_id,
        **_prompt_budget_kwargs(client.runs.create),
    )
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
                                plan_result = _normalize_plan_args(fn_args)
                        else:
                            output = json.dumps({"error": f"Unknown tool: {fn_name}"})

                        tool_outputs.append(ToolOutput(
                            tool_call_id=tool_call.id,
                            output=output,
                        ))

                run = _call_foundry_with_retry(
                    "client.runs.submit_tool_outputs",
                    client.runs.submit_tool_outputs,
                    thread_id=thread.id,
                    run_id=run.id,
                    tool_outputs=tool_outputs,
                )
            continue

        # Poll
        import time
        time.sleep(1)
        run = _call_foundry_with_retry(
            "client.runs.get",
            client.runs.get,
            thread_id=thread.id,
            run_id=run.id,
        )

    if run.status == "failed":
        log.error("Run failed: %s", run.last_error)
        return None

    if run.status == "incomplete":
        details = getattr(run, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        log.error("Run %s incomplete (reason=%s)", run.id, reason)
        _call_foundry_with_retry("client.threads.delete", client.threads.delete, thread.id)
        raise FoundryRunIncompleteError(run.id, str(reason) if reason is not None else None)

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
    _call_foundry_with_retry("client.threads.delete", client.threads.delete, thread.id)

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
        # Separator class accepts em-dash, en-dash, hyphen, colon, and middle-dot.
        # Note: previously contained a mojibake "ù" (U+00F9) here that prevented
        # lines using Unicode separators (·) from being captured.
        imp_match = _re.match(
            r"\d+\.\s+(?:\[P(\d)\]\s+)?\*\*(.+?)\*\*\s*[\u2013\u2014\u00b7:\-]+\s*(.*)",
            line,
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
        similar_text = f"\n## Similar products context\n{', '.join(config.similar)}\n"

    return f"""Evaluate this project and create an improvement plan.

1. First, list the project directory to understand its structure.
2. Read key files: README.md, project.yaml, package.json or pyproject.toml.
3. Read a few source files to understand code quality and architecture.
4. Consider the project's goals and what similar products offer.
5. Run the test suite to check current health.
6. Submit a structured improvement plan via submit_plan.

Focus on actionable, specific improvements — not generic advice.

## Every improvement must be buildable from the memo alone

Each improvement becomes a GitHub issue, and the coding agent that implements it
sees ONLY that issue. It cannot ask you what you meant. So for each one give:

- `approach` — the actual steps: which files, which functions, which commands.
  NOT a restatement of the title. "Implement 'Increase Test Coverage'" is useless.
  "Add tests/test_router.ts covering the 4 error branches in router.ts:88-140" is not.
- `success_criteria` — something a reviewer can check without having done the work.
  It must be falsifiable. Prefer a number, a command and its expected output, or a
  concrete observable state.
    GOOD: "pytest -q reports >=60 passing tests, up from 40"
    GOOD: "no occurrence of `grep -oP` remains in .github/workflows/*.yml"
    GOOD: "GET /api/health returns 200 within 500ms"
    BAD:  "'X' is implemented and usable as described"
    BAD:  "the feature works correctly"

If you cannot state a checkable success criterion for an improvement, you do not
understand it well enough yet — read more of the code, or drop it from the plan.
A short plan of specified work beats a long list of vague intentions.
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
