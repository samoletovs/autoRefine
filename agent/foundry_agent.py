"""Foundry agent — the AI brain of autoRefine.

Uses the Foundry Agent Service (``azure-ai-projects`` 2.x) to keep a persistent,
versioned prompt agent with function-calling tools, and drives it through the
project's OpenAI-compatible Responses API. The agent reasons about project
findings, compares against provided similar products, creates improvement
plans, and can execute changes.

Requires:
    FOUNDRY_PROJECT_ENDPOINT in .env
    DefaultAzureCredential (az login or managed identity)
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import httpx
import openai
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from agent.config import DEFAULT_CLASSIC_DEPLOYMENT, ProjectConfig, require_classic_deployment
from agent.plan_validation import plan_errors
from agent.sdk_boundary import SdkBoundary, SdkDeadlineExceeded, client_boundary
from agent.tools.quality_tools import plannable_findings

log = logging.getLogger(__name__)

DEFAULT_DEPLOYMENT = os.environ.get("FOUNDRY_DEFAULT_DEPLOYMENT", DEFAULT_CLASSIC_DEPLOYMENT)
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    # Global short-context USD/M: uncached input, output, cached input.
    "gpt-6-luna": (0.10, 0.50, 0.01),
    "gpt-6-sol": (2.00, 10.00, 0.20),
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
}
MAX_FOUNDRY_RETRY_ATTEMPTS = 5
RETRYABLE_FOUNDRY_STATUS_CODES = {429, 502, 503, 504}

# Persistent agent names are ``autorefine-plan`` and ``autorefine-refine``: one per
# tool set. Never widen this to a prefix match over the project — ``atlas-*`` and
# ``lab-memory`` share the Foundry project and belong to other repos.
AGENT_NAME = "autorefine"
AGENT_DEFINITION_METADATA_KEY = "autorefine_definition_sha256"
# How many recent versions to scan for one whose definition already matches. A
# --model override alternates definitions; scanning a few back reuses the older
# version instead of minting a new one on every flip.
AGENT_VERSION_SCAN = 20

# Back-compat alias. Older imports referenced DEPLOYMENT directly; keep it
# pointing at the env-resolved default so any cached imports still work.
DEPLOYMENT = DEFAULT_DEPLOYMENT

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
# Keep system.md at or above ~1,150 tokens. Azure prompt caching only engages on
# a prefix of at least 1,024 identical tokens, so instructions shorter than that
# cache nothing and every tool round of every run pays full input rate.
# tests/test_prompt_cache_prefix.py is what makes falling under the cliff visible.
# Append durable guidance; never reorder or templatize, which would break the
# byte-identical prefix the cache matches on.

# ── Prompt budget ────────────────────────────────────────────────────────────
# The classic Agents run carried two service-side levers that the Responses API
# does not have:
#
#   max_prompt_tokens / max_completion_tokens — RUN-WIDE and CUMULATIVE across
#                        every turn of a run. The service ended the run as
#                        ``incomplete`` once a sum crossed its cap. A response is
#                        one turn, so these are now enforced *locally*: usage is
#                        summed across every response in the tool loop and the
#                        loop stops (``FoundryRunIncompleteError``, same reasons)
#                        before issuing a request past either cap. Overshoot is
#                        at most one response. Each request also carries
#                        ``max_output_tokens`` = the completion budget left, so
#                        one response cannot spend more than the run has left.
#
#   truncation_strategy(last_messages) — the classic per-turn window. Responses
#                        has no last-N equivalent; ``truncation="auto"`` only
#                        drops items when the context window would overflow. With
#                        ``previous_response_id`` chaining the full history is
#                        re-sent each round, so input grows O(rounds^2) again —
#                        but as an append-only, byte-identical prefix, which is
#                        exactly what Azure prompt caching rewards. See AGENTS.md
#                        "What the sweep actually costs".
#
# The run-wide prompt cap remains a runaway guard, deliberately set high enough
# that a healthy run never trips it; a tighter cap is opt-in via the env var.
DEFAULT_MAX_PROMPT_TOKENS = 200_000
DEFAULT_MAX_COMPLETION_TOKENS = 16_000
DEEP_MAX_PROMPT_TOKENS = 40_000
DEEP_MAX_COMPLETION_TOKENS = 4_000
RESPONSES_TRUNCATION = "auto"
# The Responses API rejects max_output_tokens below 16.
MIN_OUTPUT_TOKENS = 16
# A run-wide ceiling below ~20k cannot survive more than a couple of turns and
# would kill plans before submit_plan, so reject it rather than accept a value
# that silently breaks the agent.
MIN_MAX_PROMPT_TOKENS = 20_000
# Classic-only knob, kept so a stale deployment setting is reported, not obeyed.
LEGACY_TRUNCATION_ENV = "AUTOREFINE_TRUNCATION_LAST_MESSAGES"

# ── Tool-round budget ────────────────────────────────────────────────────────
# Two *local* guards on the number of tool rounds, complementing the two
# prompt guards above. They exist because the loop in ``run_agent`` is
# otherwise unbounded: nothing stops a model that keeps asking for tool calls,
# and every round re-sends the conversation, so a run that has stopped making
# progress keeps billing for rounds that add no information.
#
#   max_tool_rounds — hard ceiling on rounds. A measured plan run is ~74
#                     rounds (AGENTS.md, "What the sweep actually costs"), so
#                     200 is ~2.7x headroom. That margin is deliberate: refine
#                     mode writes a file per round and legitimately runs
#                     longer than a plan, and the cost of one wasted run is
#                     far smaller than the cost of aborting healthy ones. This
#                     catches a runaway, not a long run.
#
#   stuck_repeats   — consecutive rounds requesting an identical batch of tool
#                     calls. Three in a row is not analysis, it is a loop: the
#                     second repeat already got back exactly what the first
#                     did, so the third cannot learn anything new. Kept
#                     deliberately narrow — consecutive *and* identical —
#                     because a false abort costs a whole project's ideation.
DEFAULT_MAX_TOOL_ROUNDS = 200
DEFAULT_STUCK_REPEATS = 3
# Floors in the same spirit as MIN_MAX_PROMPT_TOKENS: reject a value that
# would kill healthy runs rather than silently accept it. A ceiling at or
# below the observed 74-78 round band would abort plans before submit_plan.
MIN_MAX_TOOL_ROUNDS = 100
# Two identical rounds running is the smallest thing that is even a repeat;
# 1 would abort on the very first tool call.
MIN_STUCK_REPEATS = 2
DEFAULT_RUN_TIMEOUT_SECONDS = 1800
CLEANUP_TIMEOUT_SECONDS = 10
MAX_PLAN_REJECTIONS = 3
# Statuses a Responses object can still leave on the server side.
IN_FLIGHT_STATUSES = ("queued", "in_progress")

T = TypeVar("T")


class FoundryRunIncompleteError(RuntimeError):
    """A run stopped early (``status == "incomplete"``) instead of finishing.

    Raised so a truncated, partial result can never be mistaken for a
    successful plan. ``reason`` carries ``incomplete_details.reason`` or the
    run-wide budget that was exhausted, e.g. ``max_prompt_tokens``.
    """

    def __init__(self, run_id: str, reason: str | None) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(
            f"Foundry run {run_id} ended incomplete (reason={reason or 'unknown'}). "
            "Raise AUTOREFINE_MAX_PROMPT_TOKENS / AUTOREFINE_MAX_COMPLETION_TOKENS "
            "or reduce tool output if this recurs."
        )


class FoundryRunAbortedError(FoundryRunIncompleteError):
    """A local cost guard stopped the loop before the service ended the run.

    Deliberately a *subclass* of :class:`FoundryRunIncompleteError`. A run we
    abandoned for spinning, or for exhausting its round budget, is in exactly
    the state that error exists to describe — stopped early, with no plan we
    are entitled to trust — and callers already handle it that way:
    ``main.py``'s refine path catches ``FoundryRunIncompleteError`` to roll
    back half-applied edits before they can be committed. A fresh, unrelated
    exception type would slip past that handler and let a partial result reach
    a PR.

    ``reason`` names the round, stuck, elapsed-time or plan-validation guard.
    ``cancellation_unconfirmed`` is True when a request may still be executing
    server-side (the abort happened while one was in flight); a synchronous
    response that already returned leaves nothing running.
    """

    def __init__(
        self, run_id: str, reason: str, detail: str, *,
        cancellation_unconfirmed: bool = True,
    ) -> None:
        self.run_id = run_id
        self.reason = reason
        self.cancellation_unconfirmed = cancellation_unconfirmed
        # Bypasses the parent's message, which advises raising the
        # prompt-token budget — useless advice for a loop going nowhere.
        RuntimeError.__init__(self, f"Foundry run {run_id} aborted: {detail}")


class FoundryRunFailedError(FoundryRunIncompleteError):
    """A failed run that must not be replayed or publish partially applied edits."""

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        RuntimeError.__init__(self, f"Foundry run {run_id} failed (reason={reason}).")


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
    """Run-wide prompt-token ceiling. Override: ``AUTOREFINE_MAX_PROMPT_TOKENS``."""
    return _positive_int_from_env(
        "AUTOREFINE_MAX_PROMPT_TOKENS",
        DEFAULT_MAX_PROMPT_TOKENS,
        MIN_MAX_PROMPT_TOKENS,
    )


def resolve_max_completion_tokens() -> int:
    """A run-wide ceiling, including any reasoning the service performs."""
    value = _positive_int_from_env(
        "AUTOREFINE_MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION_TOKENS, MIN_OUTPUT_TOKENS,
    )
    if value > DEFAULT_MAX_COMPLETION_TOKENS:
        raise ValueError("AUTOREFINE_MAX_COMPLETION_TOKENS must be <= 16000")
    return value


def _deployment(model: str | None, mode: str) -> str:
    deployment = require_classic_deployment(model or DEFAULT_DEPLOYMENT)
    if deployment not in MODEL_PRICES:
        raise ValueError("Foundry deployment must name a supported actual model")
    if deployment == "gpt-6-sol" and (model is None or mode != "plan"):
        raise ValueError("Sol is restricted to an explicit on-demand plan model override")
    return deployment


def resolve_max_tool_rounds() -> int:
    """Tool-round ceiling for a run. Override: ``AUTOREFINE_MAX_TOOL_ROUNDS``."""
    return _positive_int_from_env(
        "AUTOREFINE_MAX_TOOL_ROUNDS",
        DEFAULT_MAX_TOOL_ROUNDS,
        MIN_MAX_TOOL_ROUNDS,
    )


def resolve_stuck_repeats() -> int:
    """Identical rounds tolerated before abort. Override: ``AUTOREFINE_STUCK_REPEATS``."""
    return _positive_int_from_env(
        "AUTOREFINE_STUCK_REPEATS",
        DEFAULT_STUCK_REPEATS,
        MIN_STUCK_REPEATS,
    )


def resolve_run_timeout_seconds() -> int:
    """Elapsed run budget, including status polls and transient retry waits."""
    return _positive_int_from_env(
        "AUTOREFINE_RUN_TIMEOUT_SECONDS", DEFAULT_RUN_TIMEOUT_SECONDS, 1
    )


def resolve_cost_log_path() -> Path | None:
    """Where to append per-run cost rows, or ``None`` when disabled.

    Unset means off. Override: ``AUTOREFINE_COST_LOG``. CI, the test suite and
    a developer's laptop therefore write nothing unless they ask for it; only
    the scheduled sweep sets it, and it is the sweep's entrypoint that commits
    the file afterwards.
    """
    raw = os.environ.get("AUTOREFINE_COST_LOG", "").strip()
    return Path(raw) if raw else None


class FoundryPromptBudgetUnsupportedError(RuntimeError):
    """The installed SDK cannot express the token budget on ``responses.create``.

    Fails closed. A ``**kwargs``-only signature is *not* evidence of support:
    a client that accepts arbitrary kwargs may drop unknown ones, which would
    leave runs unbounded while appearing to succeed.
    """


@dataclass(frozen=True)
class RunBudget:
    """Run-wide token caps, enforced locally across every response in a run."""

    max_prompt_tokens: int
    max_completion_tokens: int
    truncation: str = RESPONSES_TRUNCATION


def _run_budget(create: Callable[..., Any], model: str | None = None) -> RunBudget:
    """Resolve the run's token caps, requiring explicit SDK support for the bounds.

    ``truncation`` and ``max_output_tokens`` must appear as named parameters of
    the installed ``responses.create``; otherwise we raise rather than start an
    unbounded run, so a dependency bump can never silently restore the
    runaway-cost behaviour.
    """
    max_prompt_tokens = resolve_max_prompt_tokens()
    max_completion_tokens = resolve_max_completion_tokens()
    if model == "gpt-6-sol":
        max_prompt_tokens = min(max_prompt_tokens, DEEP_MAX_PROMPT_TOKENS)
        max_completion_tokens = min(max_completion_tokens, DEEP_MAX_COMPLETION_TOKENS)

    try:
        parameters = inspect.signature(create).parameters
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        raise FoundryPromptBudgetUnsupportedError(
            f"Cannot introspect {create!r} to confirm prompt-budget support."
        ) from exc

    missing = [
        name
        for name in ("truncation", "max_output_tokens", "previous_response_id")
        if name not in parameters
        or parameters[name].kind is inspect.Parameter.VAR_KEYWORD
    ]
    if missing:
        raise FoundryPromptBudgetUnsupportedError(
            "Installed openai SDK does not declare "
            f"{', '.join(missing)} as named parameter(s) of responses.create; "
            "refusing to start an unbounded run. Pin a supported SDK version."
        )

    if os.environ.get(LEGACY_TRUNCATION_ENV, "").strip():
        log.warning(
            "%s is ignored: the Responses API has no last-N message window; "
            "truncation=%s is used and the run-wide prompt cap bounds cost.",
            LEGACY_TRUNCATION_ENV, RESPONSES_TRUNCATION,
        )
    log.info(
        "Prompt budget: max_prompt_tokens=%d max_completion_tokens=%d (run-wide), "
        "truncation=%s",
        max_prompt_tokens, max_completion_tokens, RESPONSES_TRUNCATION,
    )
    return RunBudget(max_prompt_tokens, max_completion_tokens)


# Errors a service/transport call can raise that are expected rather than bugs.
# ``SdkDeadlineExceeded`` is a ``TimeoutError`` and therefore an ``OSError``.
_SERVICE_ERRORS: tuple[type[BaseException], ...] = (
    AzureError, OSError, httpx.HTTPError, openai.APIError,
)


def _is_retryable_foundry_exception(exception: BaseException) -> bool:
    """Return True only for transient Foundry/network errors worth retrying."""
    if isinstance(exception, HttpResponseError):
        status_code = getattr(exception, "status_code", None)
        return status_code in RETRYABLE_FOUNDRY_STATUS_CODES
    if isinstance(exception, openai.APIStatusError):
        return exception.status_code in RETRYABLE_FOUNDRY_STATUS_CODES
    if isinstance(exception, openai.APIConnectionError):  # includes APITimeoutError
        return True

    return isinstance(
        exception,
        (
            ServiceRequestError, ServiceResponseError,
            httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException,
        ),
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


_RunDeadlineExceeded = SdkDeadlineExceeded


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RunDeadlineExceeded("Foundry run elapsed-time budget exhausted")
    return remaining


_foundry_retry_wait = wait_random_exponential(multiplier=1, max=60)


def _deadline_retry_wait(retry_state: RetryCallState) -> float:
    delay = _foundry_retry_wait(retry_state)
    deadline = retry_state.kwargs.get("deadline")
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.monotonic()))
    return delay


@retry(
    retry=retry_if_exception(_is_retryable_foundry_exception),
    wait=_deadline_retry_wait,
    stop=stop_after_attempt(MAX_FOUNDRY_RETRY_ATTEMPTS),
    reraise=True,
    before_sleep=_log_retry_attempt,
)
def _call_foundry_with_retry(
    operation: str, fn: Callable[..., T], *args: Any,
    deadline: float | None = None, boundary: SdkBoundary | None = None,
    cleanup: bool = False, late_result: Callable[[T], None] | None = None, **kwargs: Any,
) -> T:
    """Retry transient failures only within the remaining elapsed budget.

    The worker boundary bounds the entire call, including auth and complete body
    consumption. Transport timeouts remain helpful inactivity limits, not the gate.
    Late-created IDs are handed only to cleanup, never back to a stopped run.
    ``fn`` receives azure-core transport kwargs; OpenAI methods are adapted by
    :func:`_openai_call`.
    """
    if deadline is not None:
        def invoke(call_deadline: float) -> T:
            remaining = _remaining_seconds(call_deadline)
            return fn(
                *args, **kwargs,
                connection_timeout=remaining / 2,
                read_timeout=remaining / 2,
                retry_total=0,
            )

        return (boundary or SdkBoundary()).call(
            invoke, deadline=deadline, cleanup=cleanup, late_result=late_result,
        )
    return fn(*args, **kwargs)


def _openai_call(method: Callable[..., T]) -> Callable[..., T]:
    """Translate the boundary's transport kwargs into an OpenAI per-request timeout.

    ``retry_total`` has no per-request OpenAI equivalent: SDK-internal retries are
    disabled where the client is built (:func:`open_foundry_clients`,
    ``max_retries=0``) so the bounded application retries above stay the only ones.
    """
    def call(
        *args: Any, connection_timeout: float | None = None,
        read_timeout: float | None = None, retry_total: int | None = None, **kwargs: Any,
    ) -> T:
        if read_timeout is not None:
            kwargs["timeout"] = httpx.Timeout(
                read_timeout, connect=connection_timeout or read_timeout,
            )
        return method(*args, **kwargs)

    return call


def open_foundry_clients(endpoint: str) -> tuple[Any, Any]:
    """Project client (agent versions) and its OpenAI client (responses), one endpoint.

    Same ``FOUNDRY_PROJECT_ENDPOINT`` and ``DefaultAzureCredential`` chain as the
    classic client; the OpenAI client authenticates for ``https://ai.azure.com``,
    the scope the workflows already pre-warm. Internal OpenAI retries are off so
    ``_call_foundry_with_retry`` owns every retry inside the elapsed deadline.
    """
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return project, project.get_openai_client(max_retries=0)


# ── Tool definitions (JSON schemas sent with the agent version) ──────────────

_PLAN_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "read_project_file",
        "description": (
            "Read a file from the project repository to inspect source code, configs, or docs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from project root, e.g. 'src/App.tsx' or 'package.json'"
                    ),
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default 200)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a project directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from project root. Use '.' for root.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_project_tests",
        "description": "Run the project's test suite. Returns pass/fail and output.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "submit_plan",
        "description": (
            "Submit a structured improvement plan after analyzing the project. Each "
            "improvement's `approach` must name the actual files/commands to change, not "
            "restate the title; `success_criteria` must be checkable by someone who did not "
            "do the work, e.g. 'pytest exits 0 with >=60 passing tests'. Use outcome "
            "'no_gap' with improvements [] only when no evidence-backed P0-P2 gap exists, "
            "citing files successfully read this run in no_gap_evidence."
        ),
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
                    "description": "Ordered list of recommended improvements.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "priority": {"type": "string", "description": "P0, P1, P2 or P3"},
                            "effort": {"type": "string", "description": "S, M or L"},
                            "category": {"type": "string"},
                            "approach": {"type": "string"},
                            "success_criteria": {"type": "string"},
                        },
                    },
                },
                "research_insights": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Insights from researching similar products",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["improvements", "no_gap"],
                    "description": (
                        "'improvements', or 'no_gap' if no evidence-backed P0-P2 gap exists."
                    ),
                },
                "no_gap_evidence": {
                    "type": "array",
                    "description": (
                        "For no_gap: files successfully read this run and what they showed."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "observation": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["score", "summary", "improvements"],
        },
    },
)

_REFINE_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "write_project_file",
        "description": (
            "Write or overwrite a file in the project repository. Use for applying improvements."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from project root, e.g. 'src/utils/helpers.ts'"
                    ),
                },
                "content": {"type": "string", "description": "The full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "apply_improvement",
        "description": (
            "Signal that an improvement has been applied. Call after writing all files for "
            "one improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string", "description": "Title of the improvement being applied",
                },
                "description": {
                    "type": "string", "description": "Brief description of what was changed",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths that were modified",
                },
            },
            "required": ["title", "description", "files_changed"],
        },
    },
)


def tool_definitions(mode: str = "plan") -> list[FunctionTool]:
    """Function tools for a mode; refine adds the write tools to the plan set."""
    schemas = _PLAN_TOOL_SCHEMAS + (_REFINE_TOOL_SCHEMAS if mode == "refine" else ())
    return [
        FunctionTool(
            name=schema["name"], description=schema["description"],
            parameters=schema["parameters"], strict=False,
        )
        for schema in schemas
    ]


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


CONTROL_ENV_PREFIX = "AUTOREFINE_"

TEST_ENV_PASSTHROUGH_ENV = "AUTOREFINE_TEST_ENV_PASSTHROUGH"

# What a project's test suite may inherit from us. Everything else is withheld.
#
# Compared case-insensitively, so each name appears once. Windows upper-cases every key
# in ``os.environ`` while POSIX does not, and POSIX tooling reads both ``HTTPS_PROXY``
# and ``https_proxy``; matching on case would mean listing several spellings of the same
# variable and still missing one. A case variant of a benign name is benign — the risk
# an allow-list controls is *which* variables, not how they are spelled.
TEST_ENV_ALLOWED: frozenset[str] = frozenset({
    # Finding and starting an interpreter at all. Without PATH there is no `python`,
    # no `npm` and no `git`; without PATHEXT, Windows cannot resolve `npm.cmd`.
    "PATH", "PATHEXT", "COMSPEC", "SHELL", "TERM",
    # Where a toolchain looks for its config and writes its caches. pip, npm and git
    # all resolve these before they do anything.
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "TMPDIR", "TEMP", "TMP",
    # Windows machinery. A child Python does not start without SYSTEMROOT, so these are
    # correctness on the machine this is developed on rather than convenience.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "OS", "DRIVERDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
    "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
    "COMPUTERNAME", "USERNAME", "USERDOMAIN", "LOGNAME", "USER", "HOSTNAME", "PWD",
    # Locale and time zone. Assertions on formatted dates and sorted text turn on these,
    # and a suite that passes under one locale can fail under another.
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES",
    "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TZ",
    # Python.
    "PYTHONPATH", "PYTHONHOME", "PYTHONHASHSEED", "PYTHONIOENCODING", "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONWARNINGS", "PYTHONBREAKPOINT",
    "PYTHONFAULTHANDLER", "PYTHONNOUSERSITE", "PYTHONPYCACHEPREFIX",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PIP_CACHE_DIR",
    # Node. Named one by one rather than by a NODE_ prefix: `NODE_AUTH_TOKEN` is the npm
    # registry credential `actions/setup-node` writes, so the obvious prefix would hand a
    # publish token to every suite. The same reasoning excludes `npm_config_*`, which
    # carries `npm_config__auth`.
    "NODE_ENV", "NODE_PATH", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_NO_WARNINGS",
    "NVM_DIR", "NVM_BIN",
    # Reaching the network from a proxied or custom-CA network at all.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "FTP_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # The marker a suite reads to know it is not on a developer's laptop. Measured in use
    # by one fleet project's tests.
    "CI",
})


def _test_env_passthrough() -> set[str]:
    """Extra names an operator has explicitly allowed, upper-cased."""
    raw = os.environ.get(TEST_ENV_PASSTHROUGH_ENV, "")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _test_subprocess_env() -> dict[str, str]:
    """Environment for a project's own test suite: an allow-list, not our whole process.

    A test suite is arbitrary code from someone else's repository, run as our child. It
    inherited everything we hold, and in production that is every credential the job has:
    ``GH_TOKEN`` and ``GITHUB_TOKEN`` (the same org-wide PAT), ``NAURO_BOT_TOKEN``, and —
    because Azure Container Apps injects them and nothing in this repository ever named
    them — ``IDENTITY_ENDPOINT`` and ``IDENTITY_HEADER``, which together mint tokens for
    the job's managed identity. That identity holds Key Vault Secrets User on the vault
    holding the PAT (``infrastructure/main.bicep``), so the pair is not one credential
    but a key to the rest.

    **An allow-list rather than a deny-list, for a reason this file already demonstrates.**
    The previous version of this function was a deny-list of one prefix, added because
    ``AUTOREFINE_COST_LOG`` — a variable introduced in #12 and not thought about here —
    leaked into children and corrupted the first cost file the pipeline ever wrote (18 of
    28 rows were fixtures for a project named ``demo``). A deny-list is a promise to
    remember every future variable; that promise had already been broken once before
    anyone noticed. The two Container Apps identity variables make the point sharper still:
    the most dangerous values in the production environment are ones no author of a
    deny-list here would think to list, because Azure sets them and this repository has
    never mentioned them.

    **The cost of getting an allow-list too narrow was measured, not assumed** (2026-08-28,
    all 25 live manifest projects, shallow-cloned and scanned for environment reads). A
    narrowing can only break a suite for a variable that is both read by that suite *and*
    present in our environment to begin with. Of 267 distinct names the fleet reads, 256
    are absent from ours — the child never received them under either rule. In test-scoped
    files the intersection is two: ``AUTOREFINE_COST_LOG`` (this repo's own suite, already
    withheld on purpose) and ``FOUNDRY_PROJECT_ENDPOINT`` (foundryLab, in
    ``agents/labMemoryAgent/src/smoke_test.py``, which the ``pytest tests/`` this module
    runs does not collect — foundryLab has no root ``tests/``). Re-measure rather than
    quote: AGENTS.md hard rule 7 applies to every number here.

    Withholding is also a correctness fix, not only a containment one. turgo's
    ``src/server/services/ai-dev.ts`` logs ``GITHUB_TOKEN not set, returning mock
    response`` — the branch its tests are written for. Today it finds a real org-wide PAT
    in our environment and takes the live path instead, so autoRefine's credential is
    spending someone else's rate limit inside their test run.

    ``AUTOREFINE_*`` is stripped unconditionally after the allow-list rather than left
    implicit. The allow-list already excludes it, but that is an accident of the list's
    contents: ``AUTOREFINE_TIER`` is exactly the sort of thing someone later adds because
    a project asks for it, and re-opening the path that corrupted production telemetry
    should take more than one plausible-looking edit.
    """
    allowed = TEST_ENV_ALLOWED | _test_env_passthrough()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed and not key.upper().startswith(CONTROL_ENV_PREFIX)
    }

    # Names only, never values: this line exists so a suite that broke because we took
    # something away can be diagnosed from a run's logs, which is the whole risk of
    # choosing an allow-list. Logging a value here would recreate the leak in the log.
    withheld = sorted(key for key in os.environ if key not in env)
    if withheld:
        log.debug(
            "test subprocess env: passing %d of %d variable(s); withheld %s",
            len(env), len(os.environ), ", ".join(withheld),
        )
    return env


def _terminal_tool_error(reason: str) -> str:
    """A tool result the model must not retry, said in terms a model will act on.

    Measured 2026-08-29..09-04 across 98 production runs: 18 tripped the
    ``stuck_tool_loop`` guard, rising from 0/12 on the first day to 7/14 on the last, and
    **all 18 captured no plan at all**. Every one of the seven aborts traced from logs
    ended with the same two calls — ``run_project_tests({})`` immediately repeated.

    The old message was accurate and still provoked the retry. *"Test runner 'npm' is not
    installed in this environment"* reads like a hiccup: something that might be different
    next time. It never is. The job image is ``python:3.12`` with no Node.js and no
    install step (``infrastructure/main.bicep``), so for any project carrying a
    ``package.json`` that call cannot succeed on any round — the same standing fact that
    made the old ``npm audit`` check dead.

    A model retrying a permanently-failing call twice is enough to trip a guard set at
    three identical rounds, and the run is then aborted holding nothing. So the fix is not
    to make the tool work; it is to say *permanent* in a way that leaves nothing to infer.
    ``retryable: False`` gives a machine-checkable field and the prose gives the
    instruction, because only the prose reliably reaches a model that is not reading
    schema.

    Reserved for conditions that cannot change within a run. A timeout and an ``OSError``
    stay ordinary errors: those really can differ next time, and telling a model never to
    retry them would trade this bug for a quieter one.
    """
    return json.dumps({
        "error": reason,
        "passed": False,
        "retryable": False,
        "instruction": (
            "Do not call run_project_tests again for this project — the result cannot "
            "change. Continue with the information you already have."
        ),
    })


def _handle_run_tests(
    project_dir: Path, _args: dict, *, timeout_seconds: float = 300,
) -> str:
    """Run the project's test suite.

    A tool the model calls must never be able to abort the run. Whichever runner is
    missing, times out, or explodes, this reports the failure back to the model as data so
    the remaining projects still get evaluated.

    The decode is pinned rather than left to the locale. ``text=True`` alone decodes with
    ``locale.getpreferredencoding()`` and raises ``UnicodeDecodeError`` on any byte that
    does not fit — a ``ValueError``, so it slips past all three handlers below and kills
    the sweep, which is precisely what the paragraph above promises cannot happen. Test
    output is arbitrary bytes from someone else's project; ``errors="replace"`` keeps a
    stray one a mangled character in a log rather than a lost run.
    """
    pkg_json = project_dir / "package.json"
    pyproject = project_dir / "pyproject.toml"

    if pkg_json.exists():
        cmd = ["npm", "test", "--", "--reporter=verbose"]
    elif pyproject.exists() or (project_dir / "requirements.txt").exists():
        cmd = ["python", "-m", "pytest", "tests/", "-x", "-q"]
    else:
        return _terminal_tool_error(
            "No test runner detected: this project has no package.json, pyproject.toml "
            "or requirements.txt."
        )

    if shutil.which(cmd[0]) is None:
        return _terminal_tool_error(
            f"This environment has no '{cmd[0]}' and cannot get one. The job image is "
            f"python:3.12, which ships no Node.js, so a JavaScript project's tests can "
            f"never run here. This is a property of the environment, not of the project."
        )

    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_test_subprocess_env(),
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return _terminal_tool_error(
            f"Test runner '{cmd[0]}' is not installed in this environment."
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"Test run timed out after {timeout_seconds:g}s", "passed": False,
        })
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
    improvements = _coerce_list(raw_improvements)
    decoded_array = isinstance(raw_improvements, list)
    # gpt-4o-mini sometimes passes improvements as a numbered free-text string that
    # isn't valid JSON; recover the structured items so ideas aren't dropped.
    if isinstance(raw_improvements, str) and raw_improvements.strip():
        try:
            decoded_array = isinstance(json.loads(raw_improvements), list)
        except (json.JSONDecodeError, ValueError):
            improvements = _parse_improvements_list(raw_improvements)
    # Missing/malformed output must not turn into a valid empty no-gap result.
    if raw_improvements is None or (
        not isinstance(raw_improvements, list)
        and not improvements
        and not decoded_array
    ):
        normalized["improvements"] = None
    else:
        normalized["improvements"] = improvements
    research_insights = args.get("research_insights")
    if isinstance(research_insights, str):
        normalized["research_insights"] = research_insights
    elif isinstance(research_insights, list):
        normalized["research_insights"] = research_insights
    else:
        normalized["research_insights"] = []
    return normalized


def _handle_submit_plan(
    _project_dir: Path, args: dict, *, read_paths: set[str] | None = None,
) -> str:
    """Validate before acknowledgement, while the model can still repair its memo."""
    if not isinstance(args, dict):
        return json.dumps({
            "status": "plan_rejected",
            "errors": [{"item": None, "field": "arguments", "error": "Supply a JSON object."}],
        })
    plan = _normalize_plan_args(args)
    errors = plan_errors(plan, read_paths=read_paths or set())
    if errors:
        return json.dumps({
            "status": "plan_rejected",
            "errors": errors,
            "instruction": "Repair the named fields and resubmit the complete plan. "
            "Use evidence-backed no_gap only when justified; never replace errors with filler.",
        })
    return json.dumps({
        "status": "plan_received", "improvements_count": len(plan["improvements"]),
    })


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


@dataclass(frozen=True)
class AgentVersion:
    """One pinned version of a persistent autoRefine prompt agent.

    The exact version is pinned in every ``agent_reference`` so a concurrent run
    that publishes a different definition (another ``--model``) cannot change
    the agent underneath a run already in progress.
    """

    name: str
    version: str
    model: str

    def reference(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "type": "agent_reference"}


def agent_name(mode: str) -> str:
    """One persistent agent per tool set: refine can write files, everything else cannot."""
    return f"{AGENT_NAME}-{'refine' if mode == 'refine' else 'plan'}"


def build_agent_definition(mode: str, deployment: str) -> PromptAgentDefinition:
    # Reasoning effort is not set: the gpt-6 models that would use it stay blocked
    # by the deployment gate, and a model that ignores it must not be told otherwise.
    sampling = {} if deployment.startswith("gpt-6-") else {"temperature": 0.3}
    return PromptAgentDefinition(
        model=deployment,
        instructions=SYSTEM_PROMPT,
        tools=tool_definitions(mode),
        **sampling,
    )


def definition_fingerprint(definition: PromptAgentDefinition) -> str:
    """Stable hash of everything that makes a version behave differently."""
    canonical = json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recent_agent_versions(project: Any, name: str, **kwargs: Any) -> list[Any]:
    """First page only, newest first. A missing agent simply has no versions yet.

    ``list_versions`` is lazy, so the page fetch happens here, inside the retry and
    deadline boundary, and the pager is recreated on retry.
    """
    try:
        pager = project.agents.list_versions(
            name, limit=AGENT_VERSION_SCAN, order="desc", **kwargs,
        )
        pages = getattr(pager, "by_page", None)
        return list(next(pages(), [])) if callable(pages) else list(pager)
    except ResourceNotFoundError:
        return []


def create_agent(
    project: Any,
    mode: str = "plan",
    model: str | None = None,
) -> AgentVersion:
    """Ensure the persistent agent version for this mode and model exists.

    Reuses the newest version whose stored definition fingerprint matches, and
    publishes a new version only when model, instructions, tools or sampling
    differ. Nothing is created per run, so nothing can be orphaned by a hard
    kill; this replaced the classic per-run create/delete and its orphan sweep.

    :param model: Foundry deployment name to use. Falls back to
        ``FOUNDRY_DEFAULT_DEPLOYMENT`` env var, then to ``gpt-4o-mini``.
        Luna/Sol are blocked until their separate service gate passes,
        including explicit manual overrides.
    """
    deployment = _deployment(model, mode)
    deadline = time.monotonic() + resolve_run_timeout_seconds()
    definition = build_agent_definition(mode, deployment)
    fingerprint = definition_fingerprint(definition)
    name = agent_name(mode)
    boundary = client_boundary(project)

    try:
        versions = _call_foundry_with_retry(
            "project.agents.list_versions", _recent_agent_versions, project, name,
            deadline=deadline, boundary=boundary,
        )
        match = next((
            version for version in versions
            if (getattr(version, "metadata", None) or {}).get(AGENT_DEFINITION_METADATA_KEY)
            == fingerprint
        ), None)
        if match is None:
            match = _call_foundry_with_retry(
                "project.agents.create_version", project.agents.create_version,
                deadline=deadline, boundary=boundary,
                agent_name=name,
                definition=definition,
                metadata={AGENT_DEFINITION_METADATA_KEY: fingerprint},
                description=f"autoRefine {mode} agent ({deployment}); managed by autoRefine.",
            )
            log.info(
                "Published agent %s version %s (mode=%s, model=%s)",
                name, match.version, mode, deployment,
            )
        else:
            log.info(
                "Reusing agent %s version %s (mode=%s, model=%s)",
                name, match.version, mode, deployment,
            )
    except _RunDeadlineExceeded as exc:
        raise FoundryRunAbortedError(
            "unknown", "run_deadline", "agent provisioning exceeded the caller deadline",
        ) from exc
    return AgentVersion(name=name, version=str(match.version), model=deployment)


class _RunSummary:
    """Aggregate of every response in one tool loop — the unit a cost row describes.

    ``id`` is the first response (the root of the ``previous_response_id`` chain),
    ``status``/``model`` come from the latest one, and usage is summed over all of
    them. Responses report usage per response; the classic run reported it
    run-wide, so summing keeps the row's ``prompt_tokens``/``completion_tokens``
    meaning the same thing.
    """

    def __init__(self) -> None:
        self.id: str | None = None
        self.status: str | None = None
        self.model: str | None = None
        self.last: Any = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cached_prompt_tokens = 0
        self._usage_seen = False

    def observe(self, response: Any) -> None:
        if self.id is None:
            self.id = getattr(response, "id", None)
        self.last = response
        self.status = _run_status(response)
        model = getattr(response, "model", None)
        if isinstance(model, str):
            self.model = model

    def add_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        read = _usage_counts(usage)
        self.prompt_tokens += read["prompt_tokens"] or 0
        self.completion_tokens += read["completion_tokens"] or 0
        self.total_tokens += read["total_tokens"] or 0
        self.cached_prompt_tokens += read["cached_prompt_tokens"] or 0
        self._usage_seen = True

    @property
    def usage(self) -> dict[str, int] | None:
        if not self._usage_seen:
            return None
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
        }


def _usage_counts(usage: Any) -> dict[str, Any]:
    """Read usage in either the Responses or the classic/summed shape."""
    def field(source: Any, key: str) -> Any:
        if isinstance(source, dict):
            return source.get(key)
        return getattr(source, key, None)

    prompt = field(usage, "prompt_tokens")
    if prompt is None:
        prompt = field(usage, "input_tokens")
    completion = field(usage, "completion_tokens")
    if completion is None:
        completion = field(usage, "output_tokens")
    total = field(usage, "total_tokens")
    if total is None and type(prompt) is int and type(completion) is int:
        total = prompt + completion
    cached = field(usage, "cached_prompt_tokens")
    if cached is None:
        details = field(usage, "input_tokens_details")
        cached = field(details, "cached_tokens") if details is not None else None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_prompt_tokens": cached,
    }


def _append_cost_row(
    run: Any,
    *,
    project: str,
    mode: str,
    rounds: int,
    tool_calls: int,
    guard: str | None,
    plan_captured: bool,
    duration_s: float,
) -> None:
    """Append one JSON line describing what this run cost. Never raises.

    The ``run_cost`` log line goes to stderr, and the sweep's own entrypoint
    documents that channel as unreliable — "Console-log ingestion drops lines,
    so counting the report objects is the only trustworthy measure"
    (``infrastructure/run-autorefine.sh``). A dropped line is fine for a human
    watching a run and useless for building a distribution, so the measurement
    goes to a file that the entrypoint commits once at the end of the sweep.

    ``mode`` is the field the file exists for: the round ceiling was chosen
    against a plan-run figure with no refine equivalent, and a row that cannot
    say which mode produced it cannot close that gap.

    The schema is unchanged by the Responses migration: ``run_id`` is the root
    response of the chain and the token fields are summed over every response.

    Fails open. Telemetry is strictly less important than the work it measures,
    and a bad path or a full disk must never cost a 116-minute sweep.
    """
    path = resolve_cost_log_path()
    if path is None:
        return

    try:
        status = getattr(run, "status", None)
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "project": project,
            "mode": mode,
            "run_id": getattr(run, "id", None),
            "status": str(status) if status is not None else None,
            "rounds": rounds,
            "tool_calls": tool_calls,
            "guard": guard,
            "plan_captured": plan_captured,
            "duration_s": round(duration_s, 1),
            **_run_token_usage(run),
            **_run_cost_estimate(run),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:  # cost telemetry must never fail the run it measures
        log.warning("Could not append a cost row to %s", path, exc_info=True)


def _call_name_and_arguments(call: Any) -> tuple[str, str]:
    """A function call's name and raw JSON arguments, from either item shape."""
    function = getattr(call, "function", None)
    name = getattr(call, "name", None) or getattr(function, "name", "") or ""
    arguments = getattr(call, "arguments", None)
    if arguments is None:
        arguments = getattr(function, "arguments", "")
    return str(name), arguments or ""


def _tool_call_signature(tool_calls: Sequence[Any]) -> str:
    """Fingerprint one round's requested tool calls, for stuck detection.

    Covers every call in the round, not just the first: the service can
    request several in parallel, and a round only repeats the previous one if
    the whole batch matches. Reading three *different* files in one round is
    progress; asking for the same three again is not.

    Arguments are compared as the raw JSON string the service sent, so this
    costs no parsing and cannot fail on a payload we could not decode. Hashing
    keeps the retained state a fixed 64 bytes however large the arguments are
    — ``write_project_file`` carries whole file bodies.
    """
    parts = []
    for call in tool_calls:
        name, arguments = _call_name_and_arguments(call)
        parts.append(f"{name}\x1f{arguments}")

    joined = "\x1e".join(sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _run_token_usage(run: Any) -> dict[str, Any]:
    """Best-effort token usage read off a run summary or a single response.

    ``usage`` is not guaranteed: it is absent until a response finishes, absent
    on a run we abandoned mid-flight, and shaped as either a model or a plain
    dict. Every field is therefore probed rather than assumed, and a run
    without it reports ``None`` — this feeds a log line in a ``finally`` block,
    so it must never raise and mask a real error.
    """
    usage = getattr(run, "usage", None)
    if usage is None:
        return dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))

    read = _usage_counts(usage)
    return {key: read[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _run_cost_estimate(run: Any) -> dict[str, Any]:
    reported = getattr(run, "model", None)
    model = next((
        known for known in MODEL_PRICES
        if isinstance(reported, str) and (reported == known or reported.startswith(known + "-"))
    ), None)
    usage = _run_token_usage(run)
    prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
    cost = None
    if model is not None and type(prompt) is int and type(completion) is int and min(prompt, completion) >= 0:
        input_rate, output_rate, _cached_rate = MODEL_PRICES[model]
        cost = (prompt * input_rate + completion * output_rate) / 1_000_000
    else:
        log.warning("Run USD estimate unavailable: missing/unknown model or token usage")
    return {
        "model": reported if isinstance(reported, str) else None,
        "estimated_usd_uncached": cost,
        "cost_basis": "uncached_input_upper_bound" if cost is not None else "unavailable",
    }


def _log_run_cost(
    run: Any,
    *,
    rounds: int,
    tool_calls: int,
    guard: str | None,
    plan_captured: bool,
) -> None:
    """Emit the one structured cost line every run ends with.

    A single greppable ``key=value`` line so a month of runs can be summed
    from logs, instead of the by-hand Azure meter forensics AGENTS.md
    describes. ``guard`` names the guard that fired, or ``none`` — which is
    how a run cut short is told apart from one that finished. Responses report
    cached input, so the line also carries ``cached_prompt_tokens`` (a subset of
    ``prompt_tokens``); the cost-row schema is deliberately left unchanged.
    """
    try:
        usage = _run_token_usage(run)
        cached = _usage_counts(getattr(run, "usage", None) or {})["cached_prompt_tokens"]
        log.info(
            "run_cost run_id=%s status=%s rounds=%d tool_calls=%d guard=%s "
            "plan_captured=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s "
            "cached_prompt_tokens=%s",
            getattr(run, "id", "unknown"),
            getattr(run, "status", "unknown"),
            rounds,
            tool_calls,
            guard or "none",
            plan_captured,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage["total_tokens"],
            cached,
        )
    except Exception:  # observability must never fail a run
        log.warning("Could not emit the run cost line.", exc_info=True)


def _abort_run(
    response: Any,
    reason: str,
    detail: str,
    *,
    in_flight: bool = False,
) -> NoReturn:
    """Stop issuing requests for a run a guard has given up on, then raise.

    There is nothing to cancel for a synchronous response: once it has returned,
    no work continues server-side, and simply not sending the tool outputs back
    ends the run. Only a request still in flight when the guard fired — an
    abandoned call at the deadline, or a response left ``queued``/``in_progress``
    — may still be billing, and that is reported as ``cancellation_unconfirmed``.
    """
    run_id = getattr(response, "id", None) or "unknown"
    log.error("Aborting Foundry run %s (%s): %s", run_id, reason, detail)

    unconfirmed = (
        in_flight or response is None or _run_status(response) in IN_FLIGHT_STATUSES
    )
    if unconfirmed:
        log.warning("cancellation_unconfirmed run_id=%s reason=request_in_flight", run_id)
    else:
        log.info("no_server_side_work run_id=%s", run_id)
    raise FoundryRunAbortedError(
        run_id, reason, detail, cancellation_unconfirmed=unconfirmed,
    )


def _cleanup_responses(client: Any, response_ids: Sequence[str]) -> bool:
    """Delete a run's stored responses; the Responses analogue of thread deletion.

    Each delete has its own bounded caller grace. Fails open on the first
    expected service/OS failure — the service expires stored responses on its
    own — but unexpected programming errors still propagate.
    """
    delete = getattr(getattr(client, "responses", None), "delete", None)
    if not callable(delete):
        return False
    for response_id in response_ids:
        try:
            _call_foundry_with_retry(
                "client.responses.delete", _openai_call(delete), response_id,
                deadline=time.monotonic() + CLEANUP_TIMEOUT_SECONDS,
                boundary=client_boundary(client), cleanup=True,
            )
        except _SERVICE_ERRORS as exc:
            log.warning("Could not delete response %s: %s", response_id, exc)
            return False
    return True


def _run_status(run: Any) -> str:
    status = getattr(run, "status", None)
    return str(getattr(status, "value", status) or "unknown").lower()


def _error_code(error: Any) -> str:
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    return str(getattr(code, "value", code) or "unknown")


def _status_error_code(exc: openai.APIStatusError) -> str:
    """The failure code for an HTTP error from ``responses.create``.

    The HTTP status decides transience first: Azure bodies carry their own codes
    (``"429"``, ``"InternalServerError"``, ``"ServiceUnavailable"``) that would never
    match ``TRANSIENT_FAILURE_CODES``, turning a throttling blip into a hard failure.
    """
    if exc.status_code == 429:
        return "rate_limit_exceeded"
    if exc.status_code >= 500:
        return "server_error"
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        code = nested.get("code")
        if isinstance(code, str) and code:
            return code
    return f"http_{exc.status_code}"


def _function_calls(response: Any) -> list[Any]:
    return [
        item for item in (getattr(response, "output", None) or [])
        if getattr(item, "type", None) == "function_call"
    ]


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return text
    parts = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            value = getattr(content, "text", None)
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


TRANSIENT_FAILURE_CODES = {"server_error", "rate_limit_exceeded"}


def run_agent(
    client: Any,
    agent: AgentVersion,
    project_dir: Path,
    config: ProjectConfig,
    task: str,
    *,
    mode: str = "unknown",
    model: str | None = None,
) -> dict | None:
    """Run the agent with a task message. Returns the parsed plan or None.

    ``client`` is the project's OpenAI client (``responses``); ``agent`` is the
    exact version to drive, pinned in every request's ``agent_reference``. Tool
    calls come back as ``function_call`` output items; their results are sent
    as ``function_call_output`` items chained with ``previous_response_id``.

    Local guards bound tool rounds, repeated calls, rejected submissions,
    run-wide tokens and elapsed time (``AUTOREFINE_RUN_TIMEOUT_SECONDS``),
    including status polling. Any guard firing raises
    :class:`FoundryRunAbortedError` (or :class:`FoundryRunIncompleteError` for a
    token budget) rather than returning ``None`` — callers read ``None`` as "the
    model declined to plan" and retry it, which would pay for a spinning run two
    more times. ``None`` is returned only for a transient service failure
    outside refine mode.

    :param mode: What this run is for, recorded on the cost row so a round and
        token distribution can be read per mode. This is the *run's* purpose,
        not the agent's tool set: functional ideation builds a plan-mode agent
        but is the daily sweep, and conflating the two would hide the thing
        the rows exist to show. Defaults to ``"unknown"`` rather than guessing,
        so a caller that says nothing is visible as such in the data.
    """
    deployment = _deployment(model, mode)
    if agent.model != deployment:
        raise ValueError(
            f"Agent {agent.name} v{agent.version} is pinned to {agent.model}, "
            f"not the requested {deployment}"
        )
    budget = _run_budget(client.responses.create, deployment)
    max_tool_rounds = resolve_max_tool_rounds()
    stuck_repeats = resolve_stuck_repeats()
    timeout_seconds = resolve_run_timeout_seconds()
    started = time.monotonic()
    deadline = started + timeout_seconds
    boundary = client_boundary(client)
    agent_reference = {"agent_reference": agent.reference()}

    summary = _RunSummary()
    response: Any = None
    created: list[str] = []
    in_flight = False
    plan_result: dict | None = None
    rounds = 0
    tool_calls = 0
    guard_fired: str | None = None
    last_signature: str | None = None
    repeat_streak = 0
    plan_rejections = 0
    read_paths: set[str] = set()

    def late_response(late: Any) -> None:
        late_id = getattr(late, "id", None)
        if late_id:
            _cleanup_responses(client, [late_id])

    def stop_for_budget(reason: str) -> NoReturn:
        nonlocal guard_fired
        guard_fired = reason
        log.error("Run %s stopped: run-wide %s budget exhausted", summary.id, reason)
        raise FoundryRunIncompleteError(getattr(response, "id", None) or "unknown", reason)

    try:
        context = config.to_context()
        pending_input: list[dict[str, Any]] = [{
            "role": "user",
            "content": f"""## Project context
{context}

## Task
{task}""",
        }]
        previous_id: str | None = None

        while True:
            _remaining_seconds(deadline)
            if summary.prompt_tokens >= budget.max_prompt_tokens:
                stop_for_budget("max_prompt_tokens")
            output_budget = budget.max_completion_tokens - summary.completion_tokens
            if output_budget < MIN_OUTPUT_TOKENS:
                stop_for_budget("max_completion_tokens")

            request: dict[str, Any] = {
                "input": pending_input,
                "extra_body": agent_reference,
                "store": True,
                "truncation": budget.truncation,
                "max_output_tokens": output_budget,
            }
            if previous_id is not None:
                request["previous_response_id"] = previous_id

            in_flight = True
            try:
                response = _call_foundry_with_retry(
                    "client.responses.create", _openai_call(client.responses.create),
                    deadline=deadline, boundary=boundary, late_result=late_response,
                    **request,
                )
            except openai.APIStatusError as exc:
                if time.monotonic() >= deadline:
                    raise
                in_flight = False
                code = _status_error_code(exc)
                failed_id = getattr(response, "id", None) or "unknown"
                log.error("Response request failed (%s): %s", code, exc)
                if code in TRANSIENT_FAILURE_CODES and mode != "refine":
                    return None
                raise FoundryRunFailedError(failed_id, code) from exc

            created.append(response.id)
            summary.observe(response)
            if previous_id is None:
                log.info("Run started: %s (agent %s v%s)", response.id, agent.name, agent.version)
            while _run_status(response) in IN_FLIGHT_STATUSES:
                time.sleep(min(1, _remaining_seconds(deadline)))
                response = _call_foundry_with_retry(
                    "client.responses.retrieve", _openai_call(client.responses.retrieve),
                    response.id, deadline=deadline, boundary=boundary,
                )
                summary.observe(response)
            in_flight = False
            summary.add_usage(response)

            if _run_status(response) != "completed":
                break
            batch = _function_calls(response)
            if not batch:
                break

            rounds += 1
            if rounds > max_tool_rounds:
                guard_fired = "max_tool_rounds"
                _abort_run(
                    response,
                    guard_fired,
                    f"exhausted its {max_tool_rounds}-round tool budget without "
                    "reaching submit_plan",
                )

            signature = _tool_call_signature(batch)
            repeat_streak = repeat_streak + 1 if signature == last_signature else 1
            last_signature = signature
            plan_batch = all(
                _call_name_and_arguments(call)[0] == "submit_plan" for call in batch
            )
            repairing_plan = plan_batch and plan_rejections > 0 and plan_result is None
            if repeat_streak >= stuck_repeats and not repairing_plan:
                guard_fired = "stuck_tool_loop"
                _abort_run(
                    response,
                    guard_fired,
                    f"asked for an identical batch of tool calls {repeat_streak} "
                    f"rounds running (round {rounds}) — it has stopped making progress",
                )

            tool_outputs: list[dict[str, Any]] = []
            for tool_call in batch:
                _remaining_seconds(deadline)
                tool_calls += 1
                fn_name, raw_arguments = _call_name_and_arguments(tool_call)
                try:
                    fn_args = json.loads(raw_arguments)
                except (json.JSONDecodeError, TypeError):
                    if fn_name != "submit_plan":
                        raise
                    fn_args = None
                log.info("Tool call: %s(%s)", fn_name, fn_args)

                handler = TOOL_HANDLERS.get(fn_name)
                if handler:
                    if fn_name == "submit_plan":
                        plan_result = None
                        output = _handle_submit_plan(
                            project_dir, fn_args, read_paths=read_paths,
                        )
                        feedback = json.loads(output)
                        if feedback["status"] == "plan_received":
                            plan_result = _normalize_plan_args(fn_args)
                        else:
                            plan_rejections += 1
                            if plan_rejections >= MAX_PLAN_REJECTIONS:
                                guard_fired = "invalid_plan"
                                _abort_run(
                                    response, guard_fired,
                                    f"plan rejected {plan_rejections} times: "
                                    f"{feedback['errors']}",
                                )
                            feedback["repairs_remaining"] = (
                                MAX_PLAN_REJECTIONS - plan_rejections
                            )
                            output = json.dumps(feedback)
                    elif fn_name == "run_project_tests":
                        output = handler(
                            project_dir, fn_args,
                            timeout_seconds=min(300, _remaining_seconds(deadline)),
                        )
                    else:
                        output = handler(project_dir, fn_args)
                    _remaining_seconds(deadline)
                    if fn_name == "read_project_file":
                        read = json.loads(output)
                        if "error" not in read and read.get("content"):
                            read_paths.add(read["path"])
                else:
                    output = json.dumps({"error": f"Unknown tool: {fn_name}"})

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": getattr(tool_call, "call_id", None),
                    "output": output,
                })

            pending_input = tool_outputs
            previous_id = response.id

        _remaining_seconds(deadline)
        status = _run_status(response)
        if status == "failed":
            code = _error_code(getattr(response, "error", None))
            log.error("Response %s failed: %s", response.id, getattr(response, "error", None))
            if code in TRANSIENT_FAILURE_CODES and mode != "refine":
                return None
            raise FoundryRunFailedError(response.id, code)

        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = (
                details.get("reason") if isinstance(details, dict)
                else getattr(details, "reason", None)
            )
            log.error("Response %s incomplete (reason=%s)", response.id, reason)
            raise FoundryRunIncompleteError(response.id, str(reason) if reason is not None else None)

        if status != "completed":
            log.error("Response %s ended without completing (status=%s)", response.id, status)
            raise FoundryRunFailedError(response.id, status)

        agent_text = _response_text(response)
        if agent_text:
            log.info("Agent response:\n%s", agent_text)
            # Fallback: if agent didn't call submit_plan, parse from text
            if plan_result is None and "Score:" in agent_text:
                candidate = _parse_plan_from_text(agent_text)
                if candidate and not plan_errors(candidate, read_paths=read_paths):
                    plan_result = candidate
                    log.info("Parsed plan from text response (submit_plan not called)")

        _remaining_seconds(deadline)
        if plan_result is None and plan_rejections:
            guard_fired = "invalid_plan"
            _abort_run(
                response, guard_fired, "run completed without repairing the rejected plan",
            )
        return plan_result
    except _SERVICE_ERRORS as exc:
        if not isinstance(exc, _RunDeadlineExceeded) and time.monotonic() < deadline:
            raise
        guard_fired = "run_deadline"
        plan_result = None
        _abort_run(
            response, guard_fired,
            f"exhausted its {timeout_seconds}s elapsed budget (status={_run_status(response)})",
            in_flight=in_flight,
        )
    finally:
        if summary.id is not None:
            _log_run_cost(
                summary,
                rounds=rounds,
                tool_calls=tool_calls,
                guard=guard_fired,
                plan_captured=plan_result is not None,
            )
            _append_cost_row(
                summary,
                project=config.name,
                mode=mode,
                rounds=rounds,
                tool_calls=tool_calls,
                guard=guard_fired,
                plan_captured=plan_result is not None,
                duration_s=time.monotonic() - started,
            )
        if created:
            _cleanup_responses(client, created)


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
    """Build the task prompt for plan mode.

    Advisory findings are dropped here rather than at the call sites. This is the
    single point at which a finding becomes prompt text, so it is the only place
    the exclusion can be *enforced* rather than merely observed: a future caller
    that forgets to filter still cannot leak one, because there is nowhere else
    for a finding to enter a prompt.
    """
    plannable = plannable_findings(findings)

    withheld = len(findings) - len(plannable)
    if withheld:
        log.info(
            "Withholding %d advisory finding(s) from the plan prompt for %s — no "
            "pull request can repair them, so an idea filed from one would buy a "
            "coding-agent run that cannot succeed",
            withheld, config.name,
        )

    findings_text = ""
    if plannable:
        findings_text = "\n## Quality check findings\n"
        for f in plannable:
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
