"""Regression tests for the baked autoRefine job's status handling."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "infrastructure" / "run-autorefine.sh"
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh is required for entrypoint tests")


def _block(start: str, end: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert start in text and end in text
    return text.split(start, 1)[1].split(end, 1)[0]


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
