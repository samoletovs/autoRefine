"""The test-runner tool must not leak autoRefine's own environment into a child.

Both defects here were found in production data rather than by reading the code, on the
first cost file the pipeline ever wrote (2026-08-28):

* 18 of its 28 rows were unit-test fixtures for a project named ``demo``, written inside
  0.11s. autoRefine is in its own manifest, so planning itself made the model call
  ``run_project_tests``; pytest reached ``tests/test_foundry_agent.py``, whose fixtures
  call ``run_agent`` without unsetting ``AUTOREFINE_COST_LOG``, and those rows landed in
  the log the entrypoint commits as production telemetry. The rows carried 7
  ``stuck_tool_loop`` trips and 2 ``max_tool_rounds``; the real count of both was zero.

* ``text=True`` with no encoding decodes with the locale and raises ``UnicodeDecodeError``
  on a byte that does not fit. That is a ``ValueError``, so it escapes all three handlers
  in ``_handle_run_tests`` and aborts the sweep — the one thing its docstring promises
  cannot happen. Test output is arbitrary bytes from someone else's project.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent import foundry_agent


def _python_project(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return tmp_path


class _Recorder:
    """Stands in for ``subprocess.run`` and behaves the way the real one is documented to.

    It decodes with whatever ``encoding``/``errors`` it is handed, so a caller that omits
    them fails here for the same reason it fails in production, and it exposes the ``env``
    it was given so a test can assert on what a child would actually see.
    """

    def __init__(self, stdout_bytes: bytes = b"ok") -> None:
        self.stdout_bytes = stdout_bytes
        self.env: dict[str, str] | None = None

    def __call__(self, _cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.env = kwargs.get("env")
        encoding = kwargs.get("encoding") or "ascii"
        errors = kwargs.get("errors") or "strict"
        text = self.stdout_bytes.decode(encoding, errors)
        return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout=text, stderr="")


class TestControlVariablesDoNotReachTheChild:
    def test_cost_log_is_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The variable that actually corrupted production telemetry."""
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/autorefine-cost.jsonl")
        rec = _Recorder()
        monkeypatch.setattr(subprocess, "run", rec)

        foundry_agent._handle_run_tests(_python_project(tmp_path), {})

        assert rec.env is not None, "an explicit env must be passed, not inherited"
        assert "AUTOREFINE_COST_LOG" not in rec.env

    def test_every_autorefine_variable_is_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stripping the prefix, not a hand-kept list, is what makes this stay fixed."""
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/x.jsonl")
        monkeypatch.setenv("AUTOREFINE_MAX_TOOL_ROUNDS", "5")
        monkeypatch.setenv("AUTOREFINE_STUCK_REPEATS", "2")
        monkeypatch.setenv("AUTOREFINE_A_VARIABLE_INVENTED_TOMORROW", "1")
        rec = _Recorder()
        monkeypatch.setattr(subprocess, "run", rec)

        foundry_agent._handle_run_tests(_python_project(tmp_path), {})

        assert rec.env is not None
        assert [k for k in rec.env if k.startswith("AUTOREFINE_")] == []

    def test_unrelated_environment_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scrubbing our own knobs must not amount to handing the child an empty env."""
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/x.jsonl")
        monkeypatch.setenv("PROJECT_NEEDS_THIS", "keep-me")
        rec = _Recorder()
        monkeypatch.setattr(subprocess, "run", rec)

        foundry_agent._handle_run_tests(_python_project(tmp_path), {})

        assert rec.env is not None
        assert rec.env.get("PROJECT_NEEDS_THIS") == "keep-me"
        assert "PATH" in rec.env


class TestUndecodableOutputCannotAbortTheSweep:
    def test_invalid_utf8_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A byte the locale cannot decode is a mangled character, never a lost run."""
        rec = _Recorder(stdout_bytes=b"caf\xe9 failed \xff\xfe")
        monkeypatch.setattr(subprocess, "run", rec)

        raw = foundry_agent._handle_run_tests(_python_project(tmp_path), {})

        payload = json.loads(raw)
        assert payload["passed"] is True
        assert "failed" in payload["output"]


class TestEnvHelper:
    def test_returns_a_copy_and_does_not_mutate_os_environ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/x.jsonl")

        env = foundry_agent._test_subprocess_env()

        assert "AUTOREFINE_COST_LOG" not in env
        import os

        assert os.environ["AUTOREFINE_COST_LOG"] == "/tmp/x.jsonl", (
            "the caller's own environment must be left alone"
        )
