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
import re
from pathlib import Path
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

    client = _ToolLoopClient(_plan_script)
    foundry_agent.run_agent(
        client, "agent-1", tmp_path, _config("payArc"), "task", mode="refine"
    )

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


def test_mode_describes_the_run_not_the_agents_tool_set(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Functional ideation builds a plan-mode agent but is the daily sweep.

    ``main.py``'s ``plan_functional`` calls ``create_agent(mode="plan")`` and
    then labels the run ``file-ideas``. Those two must be tellable apart in the
    data, because one is the fleet-wide daily cost and the other is a one-off.
    """
    log_path = tmp_path / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    for mode in ("plan", "file-ideas"):
        foundry_agent.run_agent(
            _ToolLoopClient(_plan_script), "agent-1", tmp_path, _config(), "task", mode=mode
        )

    assert [row["mode"] for row in _rows(log_path)] == ["plan", "file-ideas"]


def test_mode_defaults_to_unknown_rather_than_a_guess(
    loop_dummies: None,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that says nothing is visible as such, not silently mislabelled."""
    log_path = tmp_path / "rows.jsonl"
    monkeypatch.setenv("AUTOREFINE_COST_LOG", str(log_path))

    foundry_agent.run_agent(_ToolLoopClient(_plan_script), "a1", tmp_path, _config(), "t")

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


# ── Call-site coverage ───────────────────────────────────────────────────────


def test_every_run_agent_call_site_names_its_mode() -> None:
    """The default exists for safety, not for production to rely on.

    An unlabelled row is a row that cannot answer the question the file was
    added for, so a new call site that forgets ``mode=`` should fail here
    rather than quietly emit ``"unknown"`` for a fortnight.
    """
    source = (Path(__file__).resolve().parents[1] / "agent" / "main.py").read_text(
        encoding="utf-8"
    )
    calls = re.findall(r"run_agent\((?:[^()]|\([^()]*\))*\)", source)

    assert calls, "expected agent/main.py to call run_agent"
    unlabelled = [call for call in calls if "mode=" not in call]
    assert not unlabelled, f"run_agent call(s) without an explicit mode: {unlabelled}"
