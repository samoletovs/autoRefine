"""Tests for agent/score_summary.py — the count, the average, and the delimiter.

The bug these lock down: ``total`` was computed by ``wc -l`` on the score list
*after* ``head -20`` had already truncated it, while ``avg`` was computed by jq
over the full set. The live manifest holds 25 projects, so every run reported
"20 projects, avg <over 25>/100". Neither number was checkable from Python,
because both lived in a shell pipeline inside the workflow YAML.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from agent.score_summary import (
    MAX_LISTED,
    Summary,
    main,
    render_github_output,
    summarise,
)

# The workspace manifest autoRefine sweeps. The count is what makes truncation
# reachable at all: at 20 or fewer this bug is invisible.
LIVE_MANIFEST_PROJECTS = 25


def _obj(name: str, score: float) -> dict:
    return {"project": name, "stage": "active", "findings": [], "score": score}


def _fleet(count: int) -> list[dict]:
    return [_obj(f"project-{i:02d}", 70 + (i % 30)) for i in range(count)]


class TestSummarise:
    def test_total_counts_every_project_not_just_the_listed_ones(self) -> None:
        """The regression. 25 in, 25 reported — not 20."""
        summary = summarise(_fleet(LIVE_MANIFEST_PROJECTS))
        assert summary.total == LIVE_MANIFEST_PROJECTS

    def test_average_and_total_share_a_denominator(self) -> None:
        """`avg` must be the mean of exactly the projects `total` counts."""
        objects = _fleet(LIVE_MANIFEST_PROJECTS)
        summary = summarise(objects)

        expected = math.floor(sum(o["score"] for o in objects) / len(objects))
        assert summary.avg == expected
        assert summary.total == len(objects)

    def test_truncated_list_says_how_many_it_dropped(self) -> None:
        objects = _fleet(LIVE_MANIFEST_PROJECTS)
        lines = summarise(objects).scores.splitlines()

        assert len(lines) == MAX_LISTED + 1
        assert lines[-1] == f"… and {LIVE_MANIFEST_PROJECTS - MAX_LISTED} more"

    def test_short_fleet_is_not_truncated_and_adds_no_footer(self) -> None:
        objects = _fleet(3)
        summary = summarise(objects)

        assert summary.total == 3
        assert summary.scores.splitlines() == [
            "project-00: 70/100",
            "project-01: 71/100",
            "project-02: 72/100",
        ]

    def test_exactly_max_listed_has_no_footer(self) -> None:
        """The off-by-one boundary: 20 projects fit, so nothing was dropped."""
        summary = summarise(_fleet(MAX_LISTED))
        assert summary.total == MAX_LISTED
        assert len(summary.scores.splitlines()) == MAX_LISTED
        assert "more" not in summary.scores

    def test_float_scores_render_and_average_like_jq_did(self) -> None:
        objects = [_obj("a", 72.5), _obj("b", 80.0)]
        summary = summarise(objects)

        assert "a: 72.5/100" in summary.scores
        assert summary.avg == 76  # floor(76.25)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            summarise([])


class TestRenderGithubOutput:
    def test_emits_every_key_the_workflow_reads(self) -> None:
        rendered = render_github_output(summarise(_fleet(2)))

        assert "total=2" in rendered
        assert "avg=" in rendered
        assert "score=" in rendered
        assert rendered.startswith("scores<<")

    def test_delimiter_is_not_a_literal_that_data_can_forge(self) -> None:
        """A project literally named EOF must not close the block early."""
        summary = Summary(scores="EOF\nreal: 90/100", total=2, avg=90)
        rendered = render_github_output(summary)

        delimiter = rendered.splitlines()[0].removeprefix("scores<<")
        assert delimiter not in summary.scores
        # The payload survives intact, so `total` is still parseable after it.
        assert "total=2" in rendered.split(delimiter)[-1]

    def test_delimiter_opens_and_closes_the_same_block(self) -> None:
        rendered = render_github_output(summarise(_fleet(2)))
        lines = rendered.splitlines()

        delimiter = lines[0].removeprefix("scores<<")
        assert lines.count(delimiter) == 1, "closing delimiter missing or duplicated"


class TestMain:
    def _write_report(self, tmp_path: Path, objects: list[dict]) -> Path:
        report = tmp_path / "report.json"
        report.write_text(
            "\n".join(json.dumps(o, indent=2) for o in objects), encoding="utf-8"
        )
        return report

    def test_writes_summary_to_github_output_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._write_report(tmp_path, _fleet(LIVE_MANIFEST_PROJECTS))
        output = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))

        assert main(["score_summary.py", str(report)]) == 0
        assert "total=25" in output.read_text(encoding="utf-8")

    def test_appends_rather_than_clobbering_earlier_outputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self._write_report(tmp_path, _fleet(2))
        output = tmp_path / "gh-output"
        output.write_text("earlier=kept\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))

        assert main(["score_summary.py", str(report)]) == 0
        assert "earlier=kept" in output.read_text(encoding="utf-8")

    def test_empty_report_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))

        assert main(["score_summary.py", str(report)]) == 1

    def test_missing_report_exits_nonzero_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed agent leaves no file at all; that must be a clean failure."""
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
        assert main(["score_summary.py", str(tmp_path / "absent.json")]) == 1

    def test_falls_back_to_stdout_when_not_in_actions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        report = self._write_report(tmp_path, _fleet(2))
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        assert main(["score_summary.py", str(report)]) == 0
        assert "total=2" in capsys.readouterr().out

    def test_no_arguments_is_a_usage_error(self) -> None:
        assert main(["score_summary.py"]) == 2
