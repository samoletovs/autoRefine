"""Foundry agent — the AI brain of autoRefine.

Uses Azure AI Agents SDK to create a Foundry-hosted agent with function-calling
tools. The agent reasons about project findings, compares against provided
similar products, creates improvement plans, and can execute changes.

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
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, TypeVar

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
from azure.core.exceptions import AzureError, HttpResponseError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from agent.config import ProjectConfig
from agent.tools.quality_tools import plannable_findings

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
# Keep system.md at or above ~1,150 tokens. Azure prompt caching only engages on
# a prefix of at least 1,024 identical tokens, so instructions shorter than that
# cache nothing and every tool round of every run pays full input rate. The run
# object exposes no cached-token count (RunCompletionUsage carries only
# prompt/completion/total), so falling under the cliff is invisible until the
# bill arrives — tests/test_prompt_cache_prefix.py is what makes it visible.
# Append durable guidance; never reorder or templatize, which would break the
# byte-identical prefix the cache matches on.

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

# ── Tool-round budget ────────────────────────────────────────────────────────
# Two *local* guards on the number of tool rounds, complementing the two
# service-side prompt guards above. They exist because the loop in
# ``run_agent`` is otherwise unbounded: nothing stops a model that keeps
# asking for tool calls, and every round re-sends the thread, so a run that
# has stopped making progress keeps billing for rounds that add no
# information.
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

    ``reason`` is ``"max_tool_rounds"`` or ``"stuck_tool_loop"``.
    """

    def __init__(self, run_id: str, reason: str, detail: str) -> None:
        self.run_id = run_id
        self.reason = reason
        # Bypasses the parent's message, which advises raising the
        # prompt-token budget — useless advice for a loop going nowhere.
        RuntimeError.__init__(self, f"Foundry run {run_id} aborted: {detail}")


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


def _handle_run_tests(project_dir: Path, _args: dict) -> str:
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
        return json.dumps({"error": "No test runner detected"})

    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_test_subprocess_env(),
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

def _is_sweepable_orphan(agent: Any, cutoff: datetime) -> bool:
    """Whether one listed agent is an autoRefine orphan old enough to delete.

    Total by construction: the client is generated and ``created_at`` is whatever
    the service put on the wire, so a single odd record must not be able to abort
    the sweep. A naive timestamp is read as UTC — Foundry sends UTC — because
    comparing it against the aware cutoff would otherwise raise ``TypeError`` and
    take the whole run down with it. ``_hours_since_last_commit`` normalises the
    same way for the same reason.
    """
    if getattr(agent, "name", None) != AGENT_NAME:
        return False

    created = getattr(agent, "created_at", None)
    if created is None:
        return False

    if created.tzinfo is None:
        created = created.replace(tzinfo=cutoff.tzinfo)

    return created < cutoff


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

    Fails open, like the activity gate: this is cleanup, and cleanup that cannot
    reach the service must never destroy the work the run came to do. Note the
    catch is ``AzureError``, not ``HttpResponseError`` — a connection reset or a
    read timeout raises ``ServiceRequestError``/``ServiceResponseError``, which
    are *siblings* of ``HttpResponseError`` under ``AzureError``, not subclasses
    of it. Catching only the narrower type let a transient network blip during
    housekeeping abort agent creation and fail the entire sweep.

    :return: number of orphans deleted.
    """
    cutoff = datetime.now(timezone.utc) - max_age

    try:
        orphans = [agent for agent in client.list_agents() if _is_sweepable_orphan(agent, cutoff)]
    except AzureError as exc:
        log.warning("Could not list agents to sweep orphans: %s", exc)
        return 0

    swept = 0
    for agent in orphans:
        try:
            client.delete_agent(agent.id)
        except AzureError as exc:
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
    # Housekeeping only — never allowed to block agent creation. sweep_orphaned_agents
    # already swallows AzureError, so this catches what is left: a generated client can
    # raise anything on a malformed response, and losing the run to a failed *cleanup*
    # would invert the point of the sweep.
    if hasattr(client, "list_agents"):
        try:
            sweep_orphaned_agents(client)
        except Exception as exc:  # noqa: BLE001 - cleanup must never fail the run
            log.warning("Orphan sweep failed, continuing: %s", exc)

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

    Fails open, like ``sweep_orphaned_agents``. Telemetry is strictly less
    important than the work it measures, and a bad path or a full disk must
    never cost a 116-minute sweep.
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
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:  # cost telemetry must never fail the run it measures
        log.warning("Could not append a cost row to %s", path, exc_info=True)


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
        function = getattr(call, "function", None)
        name = getattr(function, "name", "") or ""
        arguments = getattr(function, "arguments", "") or ""
        parts.append(f"{name}\x1f{arguments}")

    joined = "\x1e".join(sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _run_token_usage(run: Any) -> dict[str, Any]:
    """Best-effort token usage read off a run object.

    ``usage`` is not guaranteed: it is absent while a run is in flight, absent
    on a run we abandoned mid-flight, and shaped as either a model or a plain
    dict depending on service version. Every field is therefore probed rather
    than assumed, and a run without it reports ``None`` — this feeds a log
    line in a ``finally`` block, so it must never raise and mask a real error.
    """
    usage = getattr(run, "usage", None)
    if usage is None:
        return dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))

    def field(key: str) -> Any:
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    return {key: field(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


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
    how a run cut short is told apart from one that finished.
    """
    try:
        usage = _run_token_usage(run)
        log.info(
            "run_cost run_id=%s status=%s rounds=%d tool_calls=%d guard=%s "
            "plan_captured=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            getattr(run, "id", "unknown"),
            getattr(run, "status", "unknown"),
            rounds,
            tool_calls,
            guard or "none",
            plan_captured,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage["total_tokens"],
        )
    except Exception:  # observability must never fail a run
        log.warning("Could not emit the run cost line.", exc_info=True)


def _abort_run(
    client: AgentsClient,
    thread_id: str,
    run: Any,
    reason: str,
    detail: str,
) -> NoReturn:
    """Tear down a run a cost guard has given up on, then raise.

    Cancelling first stops the service holding a run open waiting for tool
    outputs that are never coming; deleting the thread mirrors the
    ``incomplete`` path. Both are best-effort and swallow their own failures:
    the caller needs to see *why* the run was abandoned, not a connection
    error from the tidy-up. ``cancel`` is probed because the fakes in the test
    suite — and older SDKs — do not expose it.
    """
    run_id = getattr(run, "id", "unknown")
    log.error("Aborting Foundry run %s (%s): %s", run_id, reason, detail)

    cancel = getattr(getattr(client, "runs", None), "cancel", None)
    if callable(cancel):
        try:
            cancel(thread_id=thread_id, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the abort
            log.warning("Could not cancel aborted run %s: %s", run_id, exc)

    try:
        _call_foundry_with_retry("client.threads.delete", client.threads.delete, thread_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the abort
        log.warning("Could not delete thread %s after abort: %s", thread_id, exc)

    raise FoundryRunAbortedError(run_id, reason, detail)


def run_agent(
    client: AgentsClient,
    agent_id: str,
    project_dir: Path,
    config: ProjectConfig,
    task: str,
    *,
    mode: str = "unknown",
) -> dict | None:
    """Run the agent with a task message. Returns the parsed plan or None.

    Two local cost guards bound the tool-calling loop, which is otherwise
    unbounded: a hard round ceiling (``AUTOREFINE_MAX_TOOL_ROUNDS``) and a
    stuck detector (``AUTOREFINE_STUCK_REPEATS``). Either firing raises
    :class:`FoundryRunAbortedError` rather than returning ``None`` — callers
    read ``None`` as "the model declined to plan" and retry it, which would
    pay for a spinning run two more times.

    :param mode: What this run is for, recorded on the cost row so a round and
        token distribution can be read per mode. This is the *run's* purpose,
        not the agent's tool set: functional ideation builds a plan-mode agent
        but is the daily sweep, and conflating the two would hide the thing
        the rows exist to show. Defaults to ``"unknown"`` rather than guessing,
        so a caller that says nothing is visible as such in the data.
    """
    max_tool_rounds = resolve_max_tool_rounds()
    stuck_repeats = resolve_stuck_repeats()
    started = time.monotonic()

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
    rounds = 0
    tool_calls = 0
    guard_fired: str | None = None
    last_signature: str | None = None
    repeat_streak = 0

    try:
        while run.status in ("queued", "in_progress", "requires_action"):
            if run.status == "requires_action":
                # Counted before the isinstance check below on purpose. A
                # required action we cannot service falls through to
                # `continue` without touching the service or sleeping, so
                # without this the loop would spin on an unchanged run
                # forever — the exact failure the ceiling exists to stop.
                rounds += 1
                if rounds > max_tool_rounds:
                    guard_fired = "max_tool_rounds"
                    _abort_run(
                        client,
                        thread.id,
                        run,
                        guard_fired,
                        f"exhausted its {max_tool_rounds}-round tool budget without "
                        "reaching submit_plan",
                    )

                action = run.required_action
                if isinstance(action, SubmitToolOutputsAction):
                    batch = list(action.submit_tool_outputs.tool_calls)

                    signature = _tool_call_signature(batch)
                    repeat_streak = repeat_streak + 1 if signature == last_signature else 1
                    last_signature = signature
                    if repeat_streak >= stuck_repeats:
                        guard_fired = "stuck_tool_loop"
                        _abort_run(
                            client,
                            thread.id,
                            run,
                            guard_fired,
                            f"asked for an identical batch of tool calls {repeat_streak} "
                            f"rounds running (round {rounds}) — it has stopped making "
                            "progress",
                        )

                    tool_outputs = []
                    for tool_call in batch:
                        if isinstance(tool_call, RequiredFunctionToolCall):
                            tool_calls += 1
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
    finally:
        _log_run_cost(
            run,
            rounds=rounds,
            tool_calls=tool_calls,
            guard=guard_fired,
            plan_captured=plan_result is not None,
        )
        _append_cost_row(
            run,
            project=config.name,
            mode=mode,
            rounds=rounds,
            tool_calls=tool_calls,
            guard=guard_fired,
            plan_captured=plan_result is not None,
            duration_s=time.monotonic() - started,
        )


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
