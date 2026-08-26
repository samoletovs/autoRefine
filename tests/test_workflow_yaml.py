"""The workflow files must stay loadable by GitHub Actions.

Nine branches once carried a copy of ``autorefine-evaluate.yml`` whose "Parse
scores" step inlined a ``python3 -c "..."`` script at column 0. Inside a
``run: |`` block scalar that dedent *ends the block*, so everything after it
was parsed as top-level YAML and the file stopped being a workflow. GitHub
cannot report that as a step failure — there is no job to fail — so each push
produced a jobless run marked "failure", and the workflow's registered name
degraded to its own path.

Nothing caught it: ``tests.yml`` filtered on ``agent/**`` and friends, so
editing a workflow ran no checks at all. These tests are that missing check.
Keep them cheap and dependency-free — they are the only thing standing between
a YAML slip and a fleet of jobless red runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# PyYAML resolves a bare ``on`` key to the boolean ``True`` (YAML 1.1), so the
# trigger block shows up as ``True`` rather than ``"on"``.
ON_KEY = True

# https://docs.github.com/actions/reference/workflow-syntax-for-github-actions
ALLOWED_TOP_LEVEL_KEYS = {
    "name",
    "run-name",
    ON_KEY,
    "permissions",
    "env",
    "defaults",
    "concurrency",
    "jobs",
}


def workflow_files() -> list[Path]:
    return sorted(
        [*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")],
        key=lambda p: p.name,
    )


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def test_workflow_directory_is_not_empty() -> None:
    """Guard the guard: a bad glob would make every test below vacuously pass."""
    assert workflow_files(), f"no workflow files found under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_workflow_is_valid_yaml(path: Path) -> None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - message is the point
        pytest.fail(f"{path.name} is not valid YAML, so GitHub cannot run it:\n{exc}")

    assert isinstance(document, dict), f"{path.name} must parse to a mapping"


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_workflow_has_only_known_top_level_keys(path: Path) -> None:
    """A dedented heredoc can still parse — as junk keys beside ``jobs``.

    Valid YAML is necessary but not sufficient: ``import: sys, json`` at column
    0 parses fine and silently becomes a top-level key. GitHub rejects the
    workflow either way, so assert the shape, not just the syntax.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    unexpected = set(document) - ALLOWED_TOP_LEVEL_KEYS

    assert not unexpected, (
        f"{path.name} has unexpected top-level keys {sorted(map(str, unexpected))!r}. "
        "This usually means a line inside a 'run: |' block was dedented out of it."
    )


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_workflow_declares_name_triggers_and_jobs(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert document.get("name"), f"{path.name} has no 'name'"
    assert document.get(ON_KEY), f"{path.name} has no 'on:' triggers"

    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} defines no jobs"


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_run_steps_keep_their_indentation(path: Path) -> None:
    """Catch the original bug at its source, with the file's own line numbers.

    The YAML-level tests above fire only once the damage changes the document.
    This one reads the raw text, so it names the offending line directly.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders: list[str] = []

    run_indent: int | None = None
    for number, line in enumerate(lines, start=1):
        stripped = line.lstrip(" ")

        if run_indent is not None:
            if not stripped:
                continue
            indent = len(line) - len(stripped)
            if indent > run_indent:
                continue  # still inside the block scalar
            run_indent = None  # dedented back out; fall through and re-examine

        if stripped.startswith(("run: |", "run: >")):
            run_indent = len(line) - len(stripped)
            continue

        if line and not line.startswith(" ") and not line.startswith("#"):
            # A column-0 line is legal only for a top-level key.
            key = line.split(":", 1)[0]
            if ":" not in line or key not in {
                "name",
                "run-name",
                "on",
                "permissions",
                "env",
                "defaults",
                "concurrency",
                "jobs",
            }:
                offenders.append(f"line {number}: {line!r}")

    assert not offenders, (
        f"{path.name} has column-0 lines that are not top-level workflow keys — "
        "a 'run: |' block scalar is almost certainly broken here:\n  "
        + "\n  ".join(offenders)
    )
