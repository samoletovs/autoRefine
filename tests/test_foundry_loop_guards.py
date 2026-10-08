"""Cost guards on the Foundry tool-calling loop.

The loop in ``run_agent`` was unbounded: nothing stopped a model that kept
asking for tool calls, and every round re-sends the conversation, so a run that
had stopped making progress kept billing. These tests pin the two guards that
bound it — a hard round ceiling and a stuck detector — and, just as
importantly, pin the failure semantics: an aborted run must never look like a
successful plan.

Hermetic: every Foundry interaction is a fake of the OpenAI Responses surface
(``client.responses.create/retrieve/delete``) that ``run_agent`` drives.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import AzureError

from agent import foundry_agent
from agent.config import ProjectConfig
from agent.foundry_agent import (
    FoundryRunAbortedError,
    FoundryRunIncompleteError,
)
from tests.plan_fixtures import valid_plan

# ── Fakes ────────────────────────────────────────────────────────────────────

AGENT = foundry_agent.AgentVersion(name="autorefine-plan", version="7", model="gpt-4o-mini")


def agent_for(model: str = "gpt-4o-mini", mode: str = "plan") -> foundry_agent.AgentVersion:
    return foundry_agent.AgentVersion(foundry_agent.agent_name(mode), "7", model)


class _DummyToolCall:
    """A Responses ``function_call`` output item."""

    type = "function_call"

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = f"fc_{call_id}"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=1000,
        output_tokens=50,
        total_tokens=1050,
        input_tokens_details=SimpleNamespace(cached_tokens=600),
    )


def _response(
    response_id: str,
    status: str = "completed",
    calls: list[_DummyToolCall] | None = None,
    *,
    text: str = "",
    usage: Any = None,
    **extra: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id,
        status=status,
        model="gpt-4o-mini",
        output=list(calls or []),
        output_text=text,
        error=None,
        incomplete_details=None,
        usage=usage,
        **extra,
    )


class _Responses:
    """The ``client.responses`` surface ``run_agent`` drives."""

    def __init__(self, client: _ToolLoopClient) -> None:
        self._client = client

    def create(
        self,
        *,
        input: list,  # noqa: A002 - mirrors the SDK keyword
        extra_body: dict | None = None,
        store: bool | None = None,
        truncation: str | None = None,
        max_output_tokens: int | None = None,
        previous_response_id: str | None = None,
        timeout: object = None,
        **_kwargs: object,
    ) -> SimpleNamespace:
        self._client.requests.append({
            "input": input,
            "extra_body": extra_body,
            "store": store,
            "truncation": truncation,
            "max_output_tokens": max_output_tokens,
            "previous_response_id": previous_response_id,
            "timeout": timeout,
        })
        response = self._client.next_response()
        self._client.created.append(response.id)
        return response

    def retrieve(self, response_id: str, **_kwargs: object) -> SimpleNamespace:
        return self._client.next_response()

    def delete(self, response_id: str, **_kwargs: object) -> None:
        self._client.deleted.append(response_id)


class _ToolLoopClient:
    """Fake OpenAI client that scripts one batch of tool calls per response.

    ``script`` receives the 1-based response number and returns that response's
    function calls, or ``None`` to end the run with a completed text response.
    ``rounds`` records how many responses the loop consumed, which is how these
    tests tell a guard that fired from one that did not. Every response carries
    the same usage, so totals are a multiple of it.
    """

    def __init__(self, script: Callable[[int], list[_DummyToolCall] | None]) -> None:
        self._script = script
        self.rounds = 0
        self.requests: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.final_text = ""
        self.responses = _Responses(self)

    def next_response(self) -> SimpleNamespace:
        self.rounds += 1
        calls = self._script(self.rounds)
        return _response(
            f"resp-{self.rounds}", "completed", calls,
            text=self.final_text if calls is None else "", usage=_usage(),
        )


@pytest.fixture
def loop_dummies() -> None:
    """Kept so modules importing it keep working; the fakes need no patching now."""


@pytest.fixture(autouse=True)
def clean_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit a developer's local overrides."""
    for name in (
        "AUTOREFINE_MAX_TOOL_ROUNDS",
        "AUTOREFINE_STUCK_REPEATS",
        "AUTOREFINE_MAX_PROMPT_TOKENS",
        "AUTOREFINE_MAX_COMPLETION_TOKENS",
        "AUTOREFINE_TRUNCATION_LAST_MESSAGES",
    ):
        monkeypatch.delenv(name, raising=False)


def _config() -> ProjectConfig:
    return ProjectConfig(name="demo", purpose="", users="", stage="active")


def _read_call(path: str) -> list[_DummyToolCall]:
    return [_DummyToolCall("call-1", "read_project_file", json.dumps({"path": path}))]


def _run(client: _ToolLoopClient, project_dir: Path) -> dict | None:
    return foundry_agent.run_agent(client, AGENT, project_dir, _config(), "task")


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
    assert excinfo.value.run_id == "resp-3"
    # Default is 3 repeats: the third identical round is the one that aborts.
    assert client.rounds == foundry_agent.DEFAULT_STUCK_REPEATS == 3
    # The tool outputs are simply never sent back; nothing was left running.
    assert excinfo.value.cancellation_unconfirmed is False
    assert len(client.requests) == 3
    # Every stored response of the run is cleaned up, like the classic thread.
    assert client.deleted == client.created == ["resp-1", "resp-2", "resp-3"]


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
    assert client.deleted == client.created


def test_round_budget_counts_every_response_that_asks_for_tools(
    loop_dummies: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls to a tool we do not have still consume budget.

    An unknown tool gets an error output and the loop continues, so an uncounted
    round would let a model that keeps asking for it bill forever.
    """
    monkeypatch.setenv("AUTOREFINE_MAX_TOOL_ROUNDS", "100")

    def script(round_number: int) -> list[_DummyToolCall]:
        return [_DummyToolCall(f"c{round_number}", "no_such_tool", f'{{"n": {round_number}}}')]

    client = _ToolLoopClient(script)

    with pytest.raises(FoundryRunAbortedError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "max_tool_rounds"
    error = json.loads(client.requests[1]["input"][0]["output"])
    assert error == {"error": "Unknown tool: no_such_tool"}


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
                json.dumps(valid_plan()),
            )
        ]
    return None


def test_healthy_run_reaching_submit_plan_is_untouched(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    client = _ToolLoopClient(_healthy_script)

    result = _run(client, tmp_path)

    assert result == {**valid_plan(), "research_insights": []}
    assert client.rounds == 4
    assert client.deleted == client.created


def test_tool_outputs_are_sent_back_on_the_pinned_agent_chain(
    loop_dummies: None,
    tmp_path: Path,
) -> None:
    """The function_call -> function_call_output loop, end to end.

    Each follow-up request must carry the outputs for exactly the calls the
    previous response asked for, chained with ``previous_response_id`` and the
    same pinned ``agent_reference``. Dropping any of those would either lose the
    tool results or run a different agent version mid-run.
    """
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    client = _ToolLoopClient(_healthy_script)

    _run(client, tmp_path)

    reference = {"agent_reference": {
        "name": "autorefine-plan", "version": "7", "type": "agent_reference",
    }}
    assert [r["extra_body"] for r in client.requests] == [reference] * 4
    assert [r["previous_response_id"] for r in client.requests] == [
        None, "resp-1", "resp-2", "resp-3",
    ]
    first = client.requests[0]["input"]
    assert first[0]["role"] == "user" and "## Task\ntask" in first[0]["content"]
    for request, call_id in zip(client.requests[1:], ("c1", "call-1", "c3"), strict=True):
        (item,) = request["input"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == call_id
        json.loads(item["output"])
    assert json.loads(client.requests[2]["input"][0]["output"])["content"] == "# demo"
    assert json.loads(client.requests[3]["input"][0]["output"])["status"] == "plan_received"
    assert all(r["store"] is True and r["truncation"] == "auto" for r in client.requests)


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
                    json.dumps(valid_plan(51)),
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

    def explode(_response_id: str, **_kwargs: object) -> None:
        raise AzureError("response delete failed")

    client.responses.delete = explode

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
    assert "run_id=resp-1" in line, "the row names the root of the response chain"
    assert "rounds=3" in line
    assert "tool_calls=3" in line
    assert "guard=none" in line
    assert "plan_captured=True" in line
    # Usage is summed over all four responses, not read off the last one.
    assert "prompt_tokens=4000" in line
    assert "completion_tokens=200" in line
    assert "total_tokens=4200" in line
    assert "cached_prompt_tokens=2400" in line


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
        SimpleNamespace(input_tokens=7, output_tokens=8, total_tokens=15),
        {"input_tokens": 7, "output_tokens": 8},
    ],
)
def test_token_usage_is_probed_not_assumed(usage: Any) -> None:
    """``usage`` is absent mid-flight and shaped differently by API and version."""
    run = SimpleNamespace(id="run-1", status="completed")
    if usage is not None:
        run.usage = usage

    read = foundry_agent._run_token_usage(run)

    assert set(read) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert read["total_tokens"] == (None if usage is None else 15)


def test_run_wide_prompt_budget_stops_before_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classic service-side run cap is now summed locally across responses."""
    monkeypatch.setenv("AUTOREFINE_MAX_PROMPT_TOKENS", "20000")
    client = _ToolLoopClient(lambda n: _read_call(f"file-{n}.md"))

    with pytest.raises(FoundryRunIncompleteError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "max_prompt_tokens"
    assert not isinstance(excinfo.value, FoundryRunAbortedError)
    # 20 responses x 1000 input tokens reaches the cap; no 21st request is sent.
    assert client.rounds == 20


def test_each_request_may_spend_only_the_completion_budget_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOREFINE_MAX_COMPLETION_TOKENS", "120")
    client = _ToolLoopClient(lambda n: _read_call(f"file-{n}.md"))

    with pytest.raises(FoundryRunIncompleteError) as excinfo:
        _run(client, tmp_path)

    assert excinfo.value.reason == "max_completion_tokens"
    assert [r["max_output_tokens"] for r in client.requests] == [120, 70, 20]


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
    # Call ids change every round and must not defeat the comparison.
    assert sign([a]) == sign([_DummyToolCall("99", "read_project_file", '{"path": "a.md"}')])


def test_signature_survives_a_malformed_tool_call() -> None:
    """Stuck detection must not be the thing that crashes a run."""
    assert foundry_agent._tool_call_signature([SimpleNamespace()])
    assert foundry_agent._tool_call_signature([]) == foundry_agent._tool_call_signature([])
