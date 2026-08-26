"""Per-run cost rows: the measurement substrate for round/token distributions.

``run_agent`` already logs a ``run_cost`` line, but it goes to stderr and the
sweep's own entrypoint documents that channel as lossy — "Console-log ingestion
drops lines" (``infrastructure/run-autorefine.sh``). These rows go to a file the
entrypoint commits once at the end of a sweep instead.

Two properties matter more than the contents, and both are pinned here:

* **Off by default.** An unset ``AUTOREFINE_COST_LOG`` writes nothing, which is
  what makes this safe to merge before it has ever run.
* **Fail-open.** A telemetry failure must never fail the run it measures. A
  116-minute sweep is not worth losing to a bad path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent import foundry_agent
from agent.config import ProjectConfig
from tests.test_foundry_loop_guards import (  # reuse the loop-driving fakes
    _DummyToolCall,
    _ToolLoopClient,
    loop_dummies,  # noqa: F401 - fixture re-export
)


@pytest.fixture(autouse=True)
def clean_cost_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOREFINE_COST_LOG", raising=False)
    monkeypatch.delenv("AUTOREFINE_MAX_TOOL_ROUNDS", raising=False)
    monkeypatch.delenv("AUTOREFINE_STUCK_REPEATS", raising=False)


def _config(name: str = "demo") -> ProjectConfig:
    return ProjectConfig(name=name, purpose="", users="", stage="active")


def _plan_script(round_number: int) -> list[_DummyToolCall] | None:
    if round_number == 1:
        return [
            _DummyToolCall(
                "c1",
                "submit_plan",
                json.dumps({"score": 71, "summary": "ok", "improvements": []}),
            )
        ]
    return None


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── Off by default ───────────────────────────────────────────────────────────


def test_no_row_is_written_when_the_env_var_is_unset(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Unset means off, so CI and laptops are untouched."""
    assert foundry_agent.resolve_cost_log_path() is None

    foundry_agent.run_agent(_ToolLoopClient(_plan_script), "a1", tmp_path, _config(), "task")

    assert list(tmp_path.glob("*.jsonl")) == []


def test_blank_env_var_is_also_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_COST_LOG", "   ")
    assert foundry_agent.resolve_cost_log_path() is None


# ── The row ──────────────────────────────────────────────────────────────────


def test_row_records_mode_rounds_and_tokens(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode is the field this file exists for — refine has no measured distribution."""
    log_path = tmp_path / "cost" / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    class Recorder:
        def create_agent(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(id="agent-refine")

    # create_agent is what knows the mode; run_agent only ever sees an agent id.
    foundry_agent.create_agent(Recorder(), mode="refine")

    client = _ToolLoopClient(_plan_script)
    foundry_agent.run_agent(client, "agent-refine", tmp_path, _config("payArc"), "task")

    rows = _rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "refine", "a row that cannot say which mode produced it is useless"
    assert row["project"] == "payArc"
    assert row["rounds"] == 1
    assert row["tool_calls"] == 1
    assert row["guard"] is None
    assert row["plan_captured"] is True
    assert row["status"] == "completed"
    assert row["prompt_tokens"] == 1234
    assert row["total_tokens"] == 1290
    assert isinstance(row["duration_s"], float)
    assert row["ts"].endswith("+00:00")


def test_unknown_mode_when_the_agent_was_not_created_here(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id we never saw is reported honestly, not guessed at."""
    log_path = tmp_path / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    foundry_agent.run_agent(_ToolLoopClient(_plan_script), "never-seen", tmp_path, _config(), "t")

    assert _rows(log_path)[0]["mode"] == "unknown"


def test_rows_are_append_only(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep is many runs into one file; the second must not clobber the first."""
    log_path = tmp_path / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    for project in ("alpha", "beta", "gamma"):
        foundry_agent.run_agent(
            _ToolLoopClient(_plan_script), "a1", tmp_path, _config(project), "task"
        )

    assert [row["project"] for row in _rows(log_path)] == ["alpha", "beta", "gamma"]


def test_row_is_written_even_when_a_cost_guard_aborts_the_run(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expensive runs are exactly the ones worth measuring."""
    log_path = tmp_path / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    def spin(round_number: int) -> list[_DummyToolCall] | None:
        return None if round_number > 50 else [
            _DummyToolCall("c1", "read_project_file", json.dumps({"path": "R.md"}))
        ]

    with pytest.raises(foundry_agent.FoundryRunAbortedError):
        foundry_agent.run_agent(_ToolLoopClient(spin), "a1", tmp_path, _config(), "task")

    row = _rows(log_path)[0]
    assert row["guard"] == "stuck_tool_loop"
    assert row["rounds"] == 3
    assert row["plan_captured"] is False


# ── Fail-open ────────────────────────────────────────────────────────────────


def test_an_unwritable_path_does_not_fail_the_run(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry is worth less than the work it measures."""
    # A file standing where a directory needs to be: mkdir fails, so the append does.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(blocker / "nested" / "rows.jsonl"))

    result = foundry_agent.run_agent(
        _ToolLoopClient(_plan_script), "a1", tmp_path, _config(), "task"
    )

    assert result == {"score": 71, "summary": "ok", "improvements": [], "research_insights": []}
    assert "Could not append a cost row" in caplog.text


def test_append_helper_swallows_a_hostile_run_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(tmp_path / "rows.jsonl"))

    class Hostile:
        @property
        def status(self) -> str:
            raise RuntimeError("no status for you")

    foundry_agent._append_cost_row(
        Hostile(),
        project="p",
        mode="plan",
        rounds=1,
        tool_calls=1,
        guard=None,
        plan_captured=False,
        duration_s=1.0,
    )  # must not raise


# ── Mode registry ────────────────────────────────────────────────────────────


def test_agent_mode_registry_is_bounded() -> None:
    """A cache, not a ledger — it must not grow without limit in a long-lived host."""
    for index in range(foundry_agent._MAX_TRACKED_AGENT_MODES + 20):
        foundry_agent._remember_agent_mode(f"agent-{index}", "plan")

    assert len(foundry_agent._AGENT_MODES) <= foundry_agent._MAX_TRACKED_AGENT_MODES
    # The most recent survives; the oldest is what gets evicted.
    newest = f"agent-{foundry_agent._MAX_TRACKED_AGENT_MODES + 19}"
    assert foundry_agent._AGENT_MODES[newest] == "plan"
    assert "agent-0" not in foundry_agent._AGENT_MODES
