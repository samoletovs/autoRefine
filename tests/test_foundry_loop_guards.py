"""Cost guards on the Foundry tool-calling loop.

The loop in ``run_agent`` was unbounded: nothing stopped a model that kept
asking for tool calls, and every round re-sends the thread, so a run that had
stopped making progress kept billing. These tests pin the two guards that
bound it — a hard round ceiling and a stuck detector — and, just as
importantly, pin the failure semantics: an aborted run must never look like a
successful plan.

Hermetic: every Foundry interaction is a fake, following the fake-client
patterns already used in ``test_foundry_agent.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent import foundry_agent
from agent.config import ProjectConfig
from agent.foundry_agent import (
    FoundryRunAbortedError,
    FoundryRunIncompleteError,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


class _DummyFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _DummyToolCall:
    def __init__(self, tool_call_id: str, name: str, arguments: str) -> None:
        self.id = tool_call_id
        self.function = _DummyFunction(name, arguments)


class _DummyAction:
    def __init__(self, tool_calls: list[_DummyToolCall]) -> None:
        self.submit_tool_outputs = SimpleNamespace(tool_calls=tool_calls)


class _DummyToolOutput:
    def __init__(self, tool_call_id: str, output: str) -> None:
        self.tool_call_id = tool_call_id
        self.output = output


class _Runs:
    """The ``client.runs`` surface ``run_agent`` drives."""

    def __init__(self, client: _ToolLoopClient) -> None:
        self._client = client

    def create(
        self,
        *,
        thread_id: str,
        agent_id: str,
        max_prompt_tokens: int | None = None,
        truncation_strategy: object = None,
    ) -> SimpleNamespace:
        return self._client.next_run()

    def submit_tool_outputs(self, **_kwargs: object) -> SimpleNamespace:
        return self._client.next_run()

    def get(self, **_kwargs: object) -> SimpleNamespace:
        return self._client.next_run()

    def cancel(self, *, thread_id: str, run_id: str) -> None:
        self._client.cancelled.append(run_id)


class _ToolLoopClient:
    """Fake client that scripts one batch of tool calls per round.

    ``script`` receives the 1-based round number and returns that round's tool
    calls, or ``None`` to end the run as ``completed``. ``rounds`` records how
    many rounds the loop actually consumed, which is how these tests tell a
    guard that fired from one that did not.
    """

    def __init__(self, script: Callable[[int], list[_DummyToolCall] | None]) -> None:
        self._script = script
        self.rounds = 0
        self.cancelled: list[str] = []
        self.deleted_threads: list[str] = []
        self.threads = SimpleNamespace(
            create=lambda: SimpleNamespace(id="thread-1"),
            delete=self.deleted_threads.append,
        )
        self.messages = SimpleNamespace(
            create=lambda **_kwargs: None,
            list=lambda **_kwargs: [],
        )
        self.runs = _Runs(self)

    def next_run(self) -> SimpleNamespace:
        self.rounds += 1
        calls = self._script(self.rounds)
        if calls is None:
            return SimpleNamespace(
                id="run-1",
                status="completed",
                last_error=None,
                usage=SimpleNamespace(
                    prompt_tokens=1234,
                    completion_tokens=56,
                    total_tokens=1290,
                ),
            )
        return SimpleNamespace(
            id="run-1",
            status="requires_action",
            last_error=None,
            required_action=_DummyAction(calls),
        )


@pytest.fixture
def loop_dummies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module's isinstance checks at the fakes above."""
    monkeypatch.setattr(foundry_agent, "RequiredFunctionToolCall", _DummyToolCall)
    monkeypatch.setattr(foundry_agent, "SubmitToolOutputsAction", _DummyAction)
    monkeypatch.setattr(foundry_agent, "ToolOutput", _DummyToolOutput)


@pytest.fixture(autouse=True)
def clean_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit a developer's local overrides."""
    for name in (
        "AUTOREFINE_MAX_TOOL_ROUNDS",
        "AUTOREFINE_STUCK_REPEATS",
        "AUTOREFINE_MAX_PROMPT_TOKENS",
        "AUTOREFINE_TRUNCATION_LAST_MESSAGES",
    ):
        monkeypatch.delenv(name, raising=False)


def _config() -> ProjectConfig:
    return ProjectConfig(name="demo", purpose="", users="", stage="active")


def _read_call(path: str) -> list[_DummyToolCall]:
    return [_DummyToolCall("call-1", "read_project_file", json.dumps({"path": path}))]


def _run(client: _ToolLoopClient, project_dir: Path) -> dict | None:
    return foundry_agent.run_agent(client, "agent-1", project_dir, _config(), "task")


# ── Stuck detection ──────────────────────────────────────────────────────────


def test_spinning_loop_is_cut_short(loop_dummies: None, tmp_path: Path) -> None:
    """A model repeating one identical call is stopped, not indulged for 50 rounds."""

    def script(round_number: int) -> list[_DummyToolCall] | None:
        # Would happily spin for 50 rounds before finishing on its own.
        return None if round_number > 50 else _read_call("README.md")

    client = _ToolLoopClient(script)

    with pytest.raises(FoundryRunAbortedError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "stuck_tool_loop"
    assert excinfo.value.run_id == "run-1"
    # Default is 3 repeats: the third identical round is the one that aborts.
    assert client.rounds == foundry_agent.DEFAULT_STUCK_REPEATS == 3
    # The run is torn down, not left holding a thread open for tool outputs
    # that are never coming.
    assert client.cancelled == ["run-1"]
    assert client.deleted_threads == ["thread-1"]


def test_stuck_detector_compares_whole_parallel_batches(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    """Re-reading the same three files is a repeat; reading three new ones is not."""

    def batch(suffix: str) -> list[_DummyToolCall]:
        return [
            _DummyToolCall(f"call-{i}", "read_project_file", json.dumps({"path": f"{i}{suffix}"}))
            for i in range(3)
        ]

    def script(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 20 else batch(".md")

    client = _ToolLoopClient(script)
    with pytest.raises(FoundryRunAbortedError):
        _run(client, tmp_path)
    assert client.rounds == 3

    def progressing(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 20 else batch(f"-{round_number}.md")

    moving = _ToolLoopClient(progressing)
    assert _run(moving, tmp_path) is None
    assert moving.rounds == 21, "a run doing new work each round must not be aborted"


def test_alternating_calls_do_not_trip_the_detector(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    """The heuristic is consecutive-identical only, and deliberately so.

    An A/B/A/B oscillation is not caught. That is the documented cost of
    keeping false aborts near zero; the round ceiling is the backstop.
    """

    def script(round_number: int) -> list[_DummyToolCall] | None:
        if round_number > 12:
            return None
        return _read_call("a.md" if round_number % 2 else "b.md")

    client = _ToolLoopClient(script)
    assert _run(client, tmp_path) is None
    assert client.rounds == 13


def test_stuck_repeats_is_configurable(
    loop_dummies: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOREFINE_STUCK_REPEATS", "2")

    def script(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 50 else _read_call("README.md")

    client = _ToolLoopClient(script)
    with pytest.raises(FoundryRunAbortedError):
        _run(client, tmp_path)
    assert client.rounds == 2


# ── Round budget ─────────────────────────────────────────────────────────────


def test_round_budget_is_enforced(
    loop_dummies: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run making 'progress' forever still stops at the ceiling."""
    monkeypatch.setenv("AUTOREFINE_MAX_TOOL_ROUNDS", "100")

    def script(round_number: int) -> list[_DummyToolCall] | None:
        # A distinct call every round, so stuck detection can never fire and
        # only the ceiling is under test.
        return None if round_number > 400 else _read_call(f"file-{round_number}.md")

    client = _ToolLoopClient(script)

    with pytest.raises(FoundryRunAbortedError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "max_tool_rounds"
    # 100 rounds are served; the 101st request is refused.
    assert client.rounds == 101
    assert client.deleted_threads == ["thread-1"]


def test_round_budget_counts_unservable_required_actions(
    loop_dummies: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``requires_action`` we cannot service must still consume budget.

    That branch falls through without calling the service or sleeping, so an
    uncounted round would busy-spin on an unchanged run forever.
    """
    monkeypatch.setenv("AUTOREFINE_MAX_TOOL_ROUNDS", "100")
    unservable = SimpleNamespace(
        id="run-1",
        status="requires_action",
        last_error=None,
        required_action=SimpleNamespace(kind="something-we-do-not-handle"),
    )

    class StuckRuns(_Runs):
        def create(
            self,
            *,
            thread_id: str,
            agent_id: str,
            max_prompt_tokens: int | None = None,
            truncation_strategy: object = None,
        ) -> SimpleNamespace:
            return unservable

    client = _ToolLoopClient(lambda _round: None)
    client.runs = StuckRuns(client)

    with pytest.raises(FoundryRunAbortedError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "max_tool_rounds"


def test_default_round_ceiling_clears_a_measured_plan_run() -> None:
    """AGENTS.md measures ~74 rounds per plan run; the default must not bite."""
    measured_rounds = 78
    assert foundry_agent.DEFAULT_MAX_TOOL_ROUNDS == 200
    assert foundry_agent.DEFAULT_MAX_TOOL_ROUNDS > measured_rounds * 2
    # The floor must reject a ceiling that would abort healthy plans.
    assert foundry_agent.MIN_MAX_TOOL_ROUNDS > measured_rounds


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AUTOREFINE_MAX_TOOL_ROUNDS", "50"),
        ("AUTOREFINE_MAX_TOOL_ROUNDS", "not-a-number"),
        ("AUTOREFINE_STUCK_REPEATS", "1"),
        ("AUTOREFINE_STUCK_REPEATS", "0"),
    ],
)
def test_guard_env_overrides_are_validated(
    name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(name, value)
    resolver = (
        foundry_agent.resolve_max_tool_rounds
        if name == "AUTOREFINE_MAX_TOOL_ROUNDS"
        else foundry_agent.resolve_stuck_repeats
    )
    with pytest.raises(ValueError, match=name):
        resolver()


# ── Healthy run: neither guard fires ─────────────────────────────────────────


def _healthy_script(round_number: int) -> list[_DummyToolCall] | None:
    if round_number == 1:
        return [_DummyToolCall("c1", "list_directory", json.dumps({"path": "."}))]
    if round_number == 2:
        return _read_call("README.md")
    if round_number == 3:
        return [
            _DummyToolCall(
                "c3",
                "submit_plan",
                json.dumps({"score": 72, "summary": "ok", "improvements": []}),
            )
        ]
    return None


def test_healthy_run_reaching_submit_plan_is_untouched(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    client = _ToolLoopClient(_healthy_script)

    result = _run(client, tmp_path)

    assert result == {"score": 72, "summary": "ok", "improvements": [], "research_insights": []}
    assert client.rounds == 4
    assert client.cancelled == [], "a healthy run must never be cancelled"
    assert client.deleted_threads == ["thread-1"]


# ── Failure semantics ────────────────────────────────────────────────────────


def test_abort_is_caught_as_an_incomplete_run(loop_dummies: None, tmp_path: Path) -> None:
    """``main.py``'s refine path catches ``FoundryRunIncompleteError`` to roll
    back half-applied edits. An abort must be caught by that same handler, or a
    partial refine reaches a PR.
    """
    assert issubclass(FoundryRunAbortedError, FoundryRunIncompleteError)

    def script(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 50 else _read_call("README.md")

    client = _ToolLoopClient(script)

    caught: FoundryRunIncompleteError | None = None
    try:
        _run(client, tmp_path)
    except FoundryRunIncompleteError as exc:  # exactly main.py's handler
        caught = exc

    assert isinstance(caught, FoundryRunAbortedError)
    assert caught.reason == "stuck_tool_loop"
    # The parent's "raise your prompt-token budget" advice would be wrong here.
    assert "max_prompt_tokens" not in str(caught)


def test_abort_never_returns_a_plan(loop_dummies: None, tmp_path: Path) -> None:
    """A run that spins after submitting a plan still fails.

    ``None`` would be read by ``main.py``'s functional path as "the model
    declined to plan" and retried, paying for the spin twice more.
    """

    def script(round_number: int) -> list[_DummyToolCall] | None:
        if round_number == 1:
            return [
                _DummyToolCall(
                    "c1",
                    "submit_plan",
                    json.dumps({"score": 51, "summary": "ok", "improvements": []}),
                )
            ]
        return None if round_number > 50 else _read_call("README.md")

    client = _ToolLoopClient(script)

    with pytest.raises(FoundryRunAbortedError):
        _run(client, tmp_path)


def test_cleanup_failure_does_not_mask_the_abort(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    """The caller must learn why the run was abandoned, not how tidy-up broke."""

    def script(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 50 else _read_call("README.md")

    client = _ToolLoopClient(script)

    def explode(_thread_id: str) -> None:
        raise RuntimeError("thread delete exploded")

    client.threads = SimpleNamespace(
        create=lambda: SimpleNamespace(id="thread-1"),
        delete=explode,
    )

    with pytest.raises(FoundryRunAbortedError) as excinfo:
        _run(client, tmp_path)
    assert excinfo.value.reason == "stuck_tool_loop"


# ── Observability ────────────────────────────────────────────────────────────


def test_cost_line_reports_a_healthy_run(
    loop_dummies: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agent.foundry_agent")
    _run(_ToolLoopClient(_healthy_script), tmp_path)

    line = _cost_line(caplog)
    assert "rounds=3" in line
    assert "tool_calls=3" in line
    assert "guard=none" in line
    assert "plan_captured=True" in line
    assert "prompt_tokens=1234" in line
    assert "completion_tokens=56" in line
    assert "total_tokens=1290" in line


def test_cost_line_names_the_guard_that_fired(
    loop_dummies: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agent.foundry_agent")

    def script(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 50 else _read_call("README.md")

    with pytest.raises(FoundryRunAbortedError):
        _run(_ToolLoopClient(script), tmp_path)

    line = _cost_line(caplog)
    assert "guard=stuck_tool_loop" in line
    assert "rounds=3" in line
    assert "plan_captured=False" in line


def _cost_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("run_cost ")]
    assert len(lines) == 1, f"expected exactly one cost line, got {lines}"
    return lines[0]


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15},
        SimpleNamespace(prompt_tokens=7, completion_tokens=8, total_tokens=15),
    ],
)
def test_token_usage_is_probed_not_assumed(usage: Any) -> None:
    """``usage`` is absent mid-flight and shaped differently by service version."""
    run = SimpleNamespace(id="run-1", status="completed")
    if usage is not None:
        run.usage = usage

    read = foundry_agent._run_token_usage(run)

    assert set(read) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert read["total_tokens"] == (None if usage is None else 15)


def test_cost_line_survives_an_unreadable_run(caplog: pytest.LogCaptureFixture) -> None:
    """Logging runs in a ``finally``; a logging bug must not replace a real error."""

    class Hostile:
        @property
        def usage(self) -> Any:
            raise RuntimeError("no usage for you")

    caplog.set_level(logging.WARNING, logger="agent.foundry_agent")
    foundry_agent._log_run_cost(
        Hostile(), rounds=1, tool_calls=1, guard=None, plan_captured=False
    )
    assert "Could not emit the run cost line." in caplog.text


# ── Signature hashing ────────────────────────────────────────────────────────


def test_signature_is_stable_order_insensitive_and_argument_sensitive() -> None:
    a = _DummyToolCall("1", "read_project_file", '{"path": "a.md"}')
    b = _DummyToolCall("2", "read_project_file", '{"path": "b.md"}')

    sign = foundry_agent._tool_call_signature
    assert sign([a, b]) == sign([b, a]), "parallel calls must not depend on order"
    assert sign([a]) != sign([b]), "different arguments must not collide"
    assert sign([a]) != sign([a, b])
    # Tool-call ids change every round and must not defeat the comparison.
    assert sign([a]) == sign([_DummyToolCall("99", "read_project_file", '{"path": "a.md"}')])


def test_signature_survives_a_malformed_tool_call() -> None:
    """Stuck detection must not be the thing that crashes a run."""
    assert foundry_agent._tool_call_signature([SimpleNamespace()])
    assert foundry_agent._tool_call_signature([]) == foundry_agent._tool_call_signature([])
