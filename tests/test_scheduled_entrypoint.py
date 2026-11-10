"""Regression tests for the baked autoRefine job's status handling."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "infrastructure" / "run-autorefine.sh"
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh is required for entrypoint tests")


def _block(start: str, end: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert start in text and end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def _policy_text(**changes: object) -> str:
    policy: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tier": "low",
        "cadence_per_day": 1,
        "run_deep_pass": False,
    }
    policy.update(changes)
    return json.dumps(policy)


def _stale_policy() -> str:
    return _policy_text(generated_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())


# (id, policy file contents or None for a missing file, script exit status, sweep starts, message)
GATE_CASES: list[tuple[str, Callable[[], str | None], int, bool, str]] = [
    ("fresh-permit", _policy_text, 0, True, "scheduled inference is permitted"),
    (
        "stop",
        lambda: _policy_text(tier="critical", cadence_per_day=0),
        0,
        False,
        "scheduled inference skipped by the budget policy",
    ),
    (
        "unmeasured-fallback",
        lambda: _policy_text(
            tier="critical",
            cadence_per_day=0,
            fallback_reason="Azure CLI failed: Too Many Requests",
        ),
        0,
        False,
        "scheduled inference skipped by the budget policy",
    ),
    ("stale", _stale_policy, 2, False, "budget policy could not be validated"),
    ("malformed", lambda: "{not json", 2, False, "budget policy could not be validated"),
    ("missing", lambda: None, 2, False, "budget policy could not be validated"),
]


@needs_sh
@pytest.mark.parametrize(
    ("make_policy", "expected", "sweep_starts", "message"),
    [
        pytest.param(make, status, starts, message, id=name)
        for name, make, status, starts, message in GATE_CASES
    ],
)
def test_budget_gate_decides_whether_the_sweep_can_start(
    tmp_path: Path,
    make_policy: Callable[[], str | None],
    expected: int,
    sweep_starts: bool,
    message: str,
) -> None:
    gate = _block("# budget-gate:begin", "# budget-gate:end")
    policy_path = tmp_path / "azure-budget.json"
    contents = make_policy()
    if contents is not None:
        policy_path.write_text(contents, encoding="utf-8")
    gate, replacements = re.subn(
        r"/gov/config/azure-budget\.json", lambda _: policy_path.as_posix(), gate, count=1
    )
    assert replacements == 1, "the test must point the real gate at its own policy file"

    runner = tmp_path / "gate.sh"
    runner.write_text(
        "set -eu\n"
        'python() { "$PYTHON_BIN" "$@"; }\n'
        + gate
        + "\necho SWEEP_STARTED\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [SH, str(runner)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )

    output = result.stdout + result.stderr
    assert result.returncode == expected, output
    assert ("SWEEP_STARTED" in result.stdout) is sweep_starts, output
    assert message in result.stdout, output


@needs_sh
@pytest.mark.parametrize(("exit_code", "expected"), [(0, 0), (42, 42)])
def test_agent_failure_status_survives_the_report_boundary(
    tmp_path: Path, exit_code: int, expected: int
) -> None:
    run = _block("# agent-run:begin", "# agent-run:end")
    status = _block("# agent-status:begin", "# agent-status:end")
    command = re.compile(
        r"if python -m agent\.main \\\n(?:[^\n]*\\\n)+\s*> \"\$\{REPORT_PATH\}\"; then"
    )
    run, replacements = command.subn(
        "if sh -c 'exit \"$AGENT_EXIT_CODE\"' > \"${REPORT_PATH}\"; then",
        run,
        count=1,
    )
    assert replacements == 1, "the test must replace the real agent invocation"

    runner = tmp_path / "status.sh"
    runner.write_text(
        "set -eu\n"
        + run
        + "\n"
        + status
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [SH, str(runner)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "AGENT_EXIT_CODE": str(exit_code),
            "REPORT_PATH": (tmp_path / "report.json").as_posix(),
        },
    )

    assert result.returncode == expected
    if exit_code:
        assert f"agent.main exited non-zero: {exit_code}" in result.stdout
        assert f"report and cost persistence (exit {exit_code})" in result.stdout
