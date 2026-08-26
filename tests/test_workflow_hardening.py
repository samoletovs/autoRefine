"""Hardening guards for the workflows, and for the evaluate workflow in particular.

Three separate hazards, all of which the evaluate workflow shipped with:

1. **Script injection.** ``${{ github.event.inputs.repo }}`` was spliced straight
   into a ``run:`` block. Actions substitutes expressions *textually, before bash
   parses the line*, so the value is not an argument — it is syntax. Quoting at
   the call site cannot help, because the quotes are inserted after the payload
   is. The fix is to pass untrusted values through ``env:``, where they are only
   ever data. ``pr-merged-notify.yml`` already did it that way.

2. **Unbounded runtime.** The job had no ``timeout-minutes``, so it inherited
   GitHub's 6-hour default. This job's documented failure mode is hanging on a
   Foundry call, not crashing, and AGENTS.md records it as the fleet's largest
   consumer of Actions minutes at ~43 min/run.

3. **Unbounded concurrency.** No ``concurrency`` group, so two dispatches ran
   two full Foundry sweeps at once — double the bill for one answer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The jobs that must not be able to run for GitHub's 6-hour default.
#
# Listed, not derived from "runs the agent". autorefine-health-scan.yml runs the
# agent too and is deliberately absent: every one of its runs has been skipped
# since AUTOREFINE_TIER went to 'critical', so there is no observed duration to
# size a ceiling from, and a guessed one would risk killing a legitimate scan.
# That is a known gap awaiting data, not an oversight — add it here once a real
# run records a duration.
TIME_BOUNDED_WORKFLOWS = ("autorefine-evaluate.yml", "pr-ready-cards.yml")

# The one that holds a Foundry agent open for the whole run, so the one where two
# at once is two bills for one answer.
EVALUATE_WORKFLOW = WORKFLOW_DIR / "autorefine-evaluate.yml"

# The evaluate sweep is ~43 min (AGENTS.md); nothing here should approach this.
# Above it is not a slow day, it is a hang — and a hang is also how a Foundry
# agent gets orphaned, since a hard kill never reaches main.py's `finally`.
MAX_REASONABLE_TIMEOUT_MINUTES = 180

# Contexts an outsider (or a mistyped dispatch input) can put arbitrary text
# into. Sourced from GitHub's "Security hardening for GitHub Actions" guidance.
UNTRUSTED_EXPRESSION = re.compile(
    r"\$\{\{[^}]*\b(?:"
    r"github\.event\.inputs\.[\w.-]+"
    r"|inputs\.[\w.-]+"
    r"|github\.head_ref"
    r"|github\.event\.(?:issue|discussion)\.(?:title|body)"
    r"|github\.event\.pull_request\.(?:title|body)"
    r"|github\.event\.pull_request\.head\.(?:ref|label)"
    r"|github\.event\.(?:comment|review)\.body"
    r"|github\.event\.head_commit\.(?:message|author\.\w+)"
    r")\b[^}]*\}\}"
)


def workflow_files() -> list[Path]:
    return sorted(
        [*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")],
        key=lambda p: p.name,
    )


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_steps(document: dict):
    """Yield ``(job_name, step_label, run_script)`` for every shell step."""
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if isinstance(script, str):
                label = (step or {}).get("name") or f"step #{index + 1}"
                yield job_name, label, script


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_untrusted_input_is_never_interpolated_into_a_run_block(path: Path) -> None:
    offenders: list[str] = []

    for job_name, label, script in _run_steps(_load(path)):
        for match in UNTRUSTED_EXPRESSION.finditer(script):
            offenders.append(f"{job_name} / {label}: {match.group(0)}")

    assert not offenders, (
        f"{path.name} interpolates attacker-controllable values directly into a "
        "'run:' script, where they become shell syntax rather than data. Pass them "
        "through 'env:' and reference them as \"$VAR\" instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("workflow_name", TIME_BOUNDED_WORKFLOWS)
def test_job_is_time_bounded(workflow_name: str) -> None:
    """Without this the ceiling is GitHub's 6-hour default, not anything chosen."""
    path = WORKFLOW_DIR / workflow_name
    jobs = _load(path)["jobs"]

    for job_name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        assert timeout is not None, (
            f"job '{job_name}' in {workflow_name} has no 'timeout-minutes', "
            "so a hung call burns the 6-hour default before GitHub steps in."
        )
        assert isinstance(timeout, int) and 0 < timeout <= MAX_REASONABLE_TIMEOUT_MINUTES, (
            f"job '{job_name}' timeout of {timeout!r} is not a sane bound "
            f"(expected 1..{MAX_REASONABLE_TIMEOUT_MINUTES} minutes)."
        )


def test_evaluate_workflow_serialises_its_runs() -> None:
    concurrency = _load(EVALUATE_WORKFLOW).get("concurrency")

    assert concurrency, (
        f"{EVALUATE_WORKFLOW.name} has no 'concurrency' group, so two dispatches "
        "run two full Foundry sweeps at once — twice the spend for one answer."
    )
    assert concurrency.get("group"), "concurrency needs a 'group'"


def test_evaluate_workflow_does_not_cancel_runs_in_progress() -> None:
    """Cancelling here would trade an Actions minute for a leaked Foundry agent.

    ``agent/main.py`` deletes its ephemeral agent in a ``finally`` block. A
    cancelled job is a hard kill, so that block never runs and the agent survives
    until the next run's ``sweep_orphaned_agents`` clears it (AGENTS.md).
    """
    concurrency = _load(EVALUATE_WORKFLOW).get("concurrency") or {}

    assert concurrency.get("cancel-in-progress") is not True, (
        "cancel-in-progress must not be true: a cancelled evaluate run never "
        "reaches the 'finally' that deletes its Foundry agent."
    )

