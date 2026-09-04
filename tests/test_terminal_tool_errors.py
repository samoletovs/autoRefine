"""A permanently-failing tool result must not read as a retryable one.

Measured 2026-08-29..09-04 across 98 production runs: 18 tripped ``stuck_tool_loop``,
rising 0/12 to 7/14, and all 18 captured no plan. Every one of the seven aborts traced
from Log Analytics ended with ``run_project_tests({})`` immediately repeated.

The old message — "Test runner 'npm' is not installed in this environment" — was true
and still provoked the retry, because it reads like something that could differ next
time. It cannot: the job image is ``python:3.12`` with no Node.js, so a project with a
``package.json`` can never run its tests there.

These tests pin the distinction the fix rests on: a condition that cannot change within
a run is marked terminal, and one that can is left an ordinary error.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent import foundry_agent


def _js_project(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    return tmp_path


def _py_project(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return tmp_path


def _no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the runner absent by both routes, so the assertion is about behaviour.

    ``shutil.which`` is patched by dotted name rather than through
    ``foundry_agent.shutil``: the latter only exists once this module imports it, so a
    test written that way fails with ``AttributeError`` against any revision that does
    not, which proves an import was added and nothing about what the tool returns.
    ``subprocess.run`` is made to raise ``FileNotFoundError`` as well, which is how a
    missing binary surfaces when nothing checked for it first.
    """
    monkeypatch.setattr("shutil.which", lambda _cmd: None)

    def _missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", _missing)


def _runner_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")


class TestPermanentFailuresAreMarkedTerminal:
    def test_missing_runner_is_not_retryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production case: a JS project in an image with no Node.js."""
        _no_runner(monkeypatch)

        payload = json.loads(foundry_agent._handle_run_tests(_js_project(tmp_path), {}))

        assert payload.get("retryable") is False
        assert payload["passed"] is False

    def test_missing_runner_tells_the_model_not_to_call_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prose carries the instruction; a model is not reading the schema."""
        _no_runner(monkeypatch)

        payload = json.loads(foundry_agent._handle_run_tests(_js_project(tmp_path), {}))

        assert "do not call run_project_tests again" in payload.get(
            "instruction", ""
        ).lower()

    def test_no_runner_detected_is_also_terminal(self, tmp_path: Path) -> None:
        """A project with no manifest at all cannot grow one mid-run either.

        This branch also used to omit ``passed`` entirely, so a caller reading it got
        ``None`` where every sibling returned a bool.
        """
        payload = json.loads(foundry_agent._handle_run_tests(tmp_path, {}))

        assert payload.get("retryable") is False
        assert payload.get("passed") is False

    def test_the_runner_is_never_invoked_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checked up front, so the answer does not depend on catching an exception."""
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        called = False

        def _spy(*_a: Any, **_k: Any) -> Any:
            nonlocal called
            called = True
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(subprocess, "run", _spy)

        foundry_agent._handle_run_tests(_js_project(tmp_path), {})

        assert called is False, "spawned a process for a binary known to be absent"


class TestTransientFailuresStayRetryable:
    """The other half. Marking these terminal would trade this bug for a quieter one."""

    def test_timeout_is_not_marked_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _runner_present(monkeypatch)

        def _timeout(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=300)

        monkeypatch.setattr(subprocess, "run", _timeout)

        payload = json.loads(foundry_agent._handle_run_tests(_py_project(tmp_path), {}))

        assert payload.get("retryable") is not False
        assert "timed out" in payload["error"]

    def test_oserror_is_not_marked_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _runner_present(monkeypatch)

        def _oserror(*_a: Any, **_k: Any) -> Any:
            raise OSError("resource temporarily unavailable")

        monkeypatch.setattr(subprocess, "run", _oserror)

        payload = json.loads(foundry_agent._handle_run_tests(_py_project(tmp_path), {}))

        assert payload.get("retryable") is not False


class TestAWorkingRunnerIsUnaffected:
    def test_a_passing_suite_still_reports_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix must not change the path that already worked."""
        _runner_present(monkeypatch)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=a[0] if a else [], returncode=0, stdout="3 passed", stderr=""),
        )

        payload = json.loads(foundry_agent._handle_run_tests(_py_project(tmp_path), {}))

        assert payload["passed"] is True
        assert "3 passed" in payload["output"]
        assert "retryable" not in payload
