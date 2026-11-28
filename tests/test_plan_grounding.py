"""Memos must name paths and test commands that exist in the repo they target."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import foundry_agent as f
from agent.plan_validation import is_specified, plan_errors, specificity_errors


def _plan(approach: str, criteria: str) -> dict:
    return {
        "score": 70,
        "summary": "A promised export is missing.",
        "improvements": [{
            "title": "Export the visible rows",
            "description": "Users cannot download the rows currently displayed.",
            "priority": "P1",
            "effort": "S",
            "approach": approach,
            "success_criteria": criteria,
        }],
    }


def _errors(root: Path, approach: str, criteria: str) -> list[dict]:
    return plan_errors(_plan(approach, criteria), read_paths=set(), project_dir=root)


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "export.py").write_text("import csv\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_export.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "export.ts").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "build": "vite build"},
        "devDependencies": {"vitest": "^1.0.0"},
    }), encoding="utf-8")
    return tmp_path


def test_a_grounded_memo_passes_in_both_ecosystems(python_repo: Path, node_repo: Path) -> None:
    assert _errors(
        python_repo,
        "Stream rows through csv.writer in app/export.py instead of joining strings.",
        "pytest tests/test_export.py passes with a new quoted-cell case.",
    ) == []
    assert _errors(
        node_repo,
        "Stream rows through a CSV encoder in src/export.ts and add a download button.",
        "npm test passes and npm run build exits 0.",
    ) == []


@pytest.mark.parametrize("approach", [
    "Create app/csv_writer.py with serialize_rows and call it from app/export.py.",
    "Move serialize_rows from app/export.py into app/csv_writer.py.",
    "Write app/csv_writer.py and app/csv_reader.py, then wire them into app/export.py.",
    "Put serialize_rows in app/csv_writer.py (new) and call it from app/export.py.",
    "Read a column manifest (e.g. app/columns.json) from app/export.py.",
])
def test_files_the_memo_says_it_creates_need_not_exist(python_repo: Path, approach: str) -> None:
    assert _errors(python_repo, approach, "pytest tests/test_export.py passes.") == []


@pytest.mark.parametrize("approach", [
    "Update src/export.py to stream rows through csv.writer.",
    "Add a streaming writer to app/exporter.py using csv.writer.",
    "Create app/csv_writer.py and update src/export.py to call it.",
])
def test_a_path_missing_from_the_repo_is_rejected_by_name(python_repo: Path, approach: str) -> None:
    errors = _errors(python_repo, approach, "pytest tests/test_export.py passes.")
    assert [(e["item"], e["field"]) for e in errors] == [(1, "approach")]
    missing = "app/exporter.py" if "exporter" in approach else "src/export.py"
    assert missing in errors[0]["error"]
    assert "list_directory" in errors[0]["error"]
    assert "app/csv_writer.py" not in errors[0]["error"]


@pytest.mark.parametrize(("criteria", "needle"), [
    ("npm run e2e passes against the dev server.", "'e2e' script"),
    ("pytest tests/test_export.py passes.", "needs pytest"),
    ("npx jest exits 0 with the new export spec.", "needs jest"),
    ("go test ./... passes for the exporter.", "go.mod"),
])
def test_a_test_command_the_repo_does_not_define_is_rejected(
    node_repo: Path, criteria: str, needle: str,
) -> None:
    errors = _errors(node_repo, "Stream rows through a CSV encoder in src/export.ts.", criteria)
    assert [(e["item"], e["field"]) for e in errors] == [(1, "success_criteria")]
    assert needle in errors[0]["error"]
    if "e2e" in criteria:
        assert "build, test" in errors[0]["error"], "names the scripts that do exist"


def test_a_command_the_memo_sets_up_is_accepted(node_repo: Path) -> None:
    assert _errors(
        node_repo,
        "Stream rows through src/export.ts. Add an e2e script to package.json for playwright.",
        "npm run e2e passes against the dev server.",
    ) == []


def test_npm_init_placeholder_is_not_a_test_script(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
    }), encoding="utf-8")
    errors = _errors(tmp_path, "Export rows as CSV from index.js using a stream.", "npm test passes.")
    assert [e["field"] for e in errors] == ["success_criteria"]


@pytest.mark.parametrize("criteria", [
    "Users find the export faster and the code is cleaner.",
    "Error handling around the export is more robust and maintainable.",
])
def test_success_criteria_without_a_measurable_check_is_rejected(criteria: str) -> None:
    memo = _plan("Stream rows through csv.writer in app/export.py.", criteria)["improvements"][0]
    assert not is_specified(memo)
    assert "measurable" in specificity_errors(memo)["success_criteria"]


@pytest.mark.parametrize("criteria", [
    "Exporting 10k rows takes under 2 seconds, down from 9.",
    "Clicking Export downloads a CSV whose header row matches the visible columns.",
    "The export test fails before the fix and passes after it.",
    "axe-core reports no critical accessibility violations on the export page.",
])
def test_measurable_success_criteria_is_accepted(criteria: str) -> None:
    memo = _plan("Stream rows through csv.writer in app/export.py.", criteria)["improvements"][0]
    assert is_specified(memo)


def test_submit_plan_feedback_names_the_ungrounded_field_until_repaired(python_repo: Path) -> None:
    hallucinated = _plan("Update src/export.py to stream rows.", "npm test passes.")
    rejected = json.loads(f._handle_submit_plan(python_repo, hallucinated, read_paths=set()))
    assert rejected["status"] == "plan_rejected"
    assert [e["field"] for e in rejected["errors"]] == ["approach", "success_criteria"]

    repaired = _plan("Update app/export.py to stream rows.", "pytest tests/test_export.py passes.")
    received = json.loads(f._handle_submit_plan(python_repo, repaired, read_paths=set()))
    assert received["status"] == "plan_received"
