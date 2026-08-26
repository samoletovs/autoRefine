"""Tests for score coverage — the denominator the 0-100 score never had.

The bug these lock down: the score does not measure quality, it measures how
much a project has declared about itself. Nearly every check in
``quality_tools`` early-returns unless the project's own ``project.yaml``
``quality:`` list names the trait, and an early return deducts nothing. So two
projects that are byte-for-byte identical — a Python app with no tests and no
CI — score 100 and 70 depending only on whether they were honest about their
own standards. The score rewards silence and penalises candour, and it is
reported to Telegram and the dashboard as if it meant quality.

These tests do **not** change what gets flagged: making every check
unconditional would drop scores across the whole fleet at once and set
``file-ideas`` generating a burst of P0 "no tests" ideas, each approvable into a
10-30 minute Copilot Coding Agent run, against a 5 EUR/month cap. They pin the
scores exactly as they are and assert that the gap is now *visible*: a check
that did not run is no longer indistinguishable from a check that passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent.config import ProjectConfig
from agent.main import evaluate_project
from agent.tools.quality_tools import (
    SKIP_NOT_APPLICABLE,
    SKIP_NOT_DECLARED,
    SKIP_TOOLING_UNAVAILABLE,
    QualityCoverage,
    RepoContext,
    check_ci_cd,
    check_tests,
    measure_dependencies,
    measure_security_headers,
    measure_tests,
    run_quality_checks,
    run_quality_checks_with_coverage,
)

# Every dimension the score is built from, in report order. Pinned here so a new
# check cannot be added to the findings without also entering the denominator.
ALL_DIMENSIONS = ["metadata", "tests", "ci-cd", "security", "deps", "i18n"]


def _write_python_app(root: Path, quality: list[str]) -> Path:
    """A Python app with no tests and no CI. The only variable is what it declares."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    (root / "project.yaml").write_text(
        yaml.dump(
            {
                "name": root.name,
                "purpose": "Demonstrate the scoring asymmetry",
                "users": "Maintainers",
                "stage": "active",
                "goals": ["Ship something"],
                "similar": ["CompetitorX"],
                "quality": quality,
            }
        ),
        encoding="utf-8",
    )
    return root


def _config(project: Path) -> ProjectConfig:
    return ProjectConfig.from_yaml(project / "project.yaml")


def _score(findings: list) -> int:
    """The same arithmetic ``evaluate_project`` uses."""
    return max(0, 100 - sum(f.weight for f in findings))


# ── The regression ─────────────────────────────────────────────────────────


class TestDeclarationAsymmetry:
    """Two identical projects, two scores, and now a visible reason why."""

    def test_silence_scores_100_and_candour_scores_70(self, tmp_path: Path) -> None:
        """The asymmetry itself, pinned. Neither number may move."""
        silent = _write_python_app(tmp_path / "silent", quality=[])
        honest = _write_python_app(tmp_path / "honest", quality=["tests", "ci-cd"])

        silent_findings, _ = run_quality_checks_with_coverage(str(silent), _config(silent))
        honest_findings, _ = run_quality_checks_with_coverage(str(honest), _config(honest))

        assert _score(silent_findings) == 100
        assert silent_findings == []

        assert _score(honest_findings) == 70
        assert [(f.priority, f.category) for f in honest_findings] == [
            ("P0", "tests"),
            ("P1", "ci-cd"),
        ]

    def test_the_perfect_score_is_the_one_that_measured_least(self, tmp_path: Path) -> None:
        """The point of the change: 100/100 now arrives with its denominator."""
        silent = _write_python_app(tmp_path / "silent", quality=[])
        honest = _write_python_app(tmp_path / "honest", quality=["tests", "ci-cd"])

        _, silent_coverage = run_quality_checks_with_coverage(str(silent), _config(silent))
        _, honest_coverage = run_quality_checks_with_coverage(str(honest), _config(honest))

        assert silent_coverage.measured == ["metadata"]
        assert silent_coverage.summary() == "1/6 measured"

        assert honest_coverage.measured == ["metadata", "tests", "ci-cd"]
        assert honest_coverage.summary() == "3/6 measured"

        # The project that scored higher was inspected less. That is the finding.
        assert len(silent_coverage.measured) < len(honest_coverage.measured)

    def test_skip_reasons_separate_undeclared_from_inapplicable(self, tmp_path: Path) -> None:
        """"Not declared" is a choice the project made; "not applicable" is not."""
        silent = _write_python_app(tmp_path / "silent", quality=[])
        _, coverage = run_quality_checks_with_coverage(str(silent), _config(silent))

        reasons = {r.dimension: r.skip_reason for r in coverage.results}
        assert reasons["tests"] == SKIP_NOT_DECLARED
        assert reasons["ci-cd"] == SKIP_NOT_DECLARED
        assert reasons["i18n"] == SKIP_NOT_DECLARED
        assert reasons["security"] == SKIP_NOT_APPLICABLE
        assert reasons["metadata"] is None
        # deps needs GitHub identity it was not given here, which is a third
        # thing again: not the project's silence, and not an inapplicable stack.
        assert reasons["deps"] == SKIP_TOOLING_UNAVAILABLE

        details = {r.dimension: r.detail for r in coverage.results}
        assert "project.yaml" in details["tests"]
        assert "repo identity" in details["deps"]


# ── Skipped is not passed ──────────────────────────────────────────────────


class TestSkippedIsNotPassed:
    def test_a_check_that_did_not_run_is_not_a_check_that_passed(
        self, tmp_path: Path
    ) -> None:
        """Both produce zero findings. Only ``measured`` tells them apart."""
        declared = _write_python_app(tmp_path / "declared", quality=["tests"])
        (declared / "tests").mkdir()
        (declared / "tests" / "test_smoke.py").write_text(
            "def test_smoke() -> None: pass\n", encoding="utf-8"
        )
        silent = _write_python_app(tmp_path / "silent", quality=[])

        passed = measure_tests(declared, _config(declared))
        skipped = measure_tests(silent, _config(silent))

        assert passed.findings == []
        assert skipped.findings == []  # indistinguishable on findings alone

        assert passed.measured is True
        assert passed.skip_reason is None

        assert skipped.measured is False
        assert skipped.skip_reason == SKIP_NOT_DECLARED

    def test_measured_never_disagrees_with_skip_reason(self, tmp_path: Path) -> None:
        project = _write_python_app(tmp_path, quality=["tests"])
        _, coverage = run_quality_checks_with_coverage(str(project), _config(project))

        for result in coverage.results:
            assert result.measured == (result.skip_reason is None)

    def test_no_token_reads_as_unmeasured_not_as_a_clean_tree(
        self, tmp_path: Path
    ) -> None:
        """Without a token nothing was audited; that is not a clean dependency tree."""
        project = _write_python_app(tmp_path, quality=[])

        result = measure_dependencies(
            project, _config(project), RepoContext(slug="owner/name", token="")
        )

        assert result.findings == []
        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE

    def test_a_successful_read_counts_as_measured(self, tmp_path: Path) -> None:
        project = _write_python_app(tmp_path, quality=[])

        with patch(
            "agent.tools.quality_tools.fetch_dependabot_alerts", return_value=[]
        ):
            result = measure_dependencies(
                project, _config(project), RepoContext(slug="owner/name", token="t")
            )

        assert result.measured is True
        assert result.findings == []

    def test_missing_swa_config_is_not_applicable_rather_than_secure(
        self, tmp_path: Path
    ) -> None:
        project = _write_python_app(tmp_path, quality=[])
        result = measure_security_headers(project, _config(project))

        assert result.findings == []
        assert result.measured is False
        assert result.skip_reason == SKIP_NOT_APPLICABLE


# ── The score must not move ────────────────────────────────────────────────


class TestScoreIsUnchanged:
    @pytest.mark.parametrize(
        "quality,expected_score",
        [
            ([], 100),
            (["tests"], 80),
            (["ci-cd"], 90),
            (["tests", "ci-cd"], 70),
            (["tests", "ci-cd", "i18n"], 65),
            (["responsive"], 100),  # a trait no check knows about deducts nothing
        ],
    )
    def test_scores_are_pinned_exactly_where_they_were(
        self, tmp_path: Path, quality: list[str], expected_score: int
    ) -> None:
        project = _write_python_app(tmp_path, quality=quality)
        findings = run_quality_checks(str(project), _config(project))
        assert _score(findings) == expected_score

    def test_missing_project_yaml_still_costs_25(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        config = ProjectConfig(name="bare", purpose="p", users="u", stage="active")

        findings = run_quality_checks(str(bare), config)
        assert _score(findings) == 75
        assert findings[0].category == "metadata"

    def test_findings_only_entry_point_matches_the_coverage_one(
        self, tmp_path: Path
    ) -> None:
        """``run_quality_checks`` keeps its contract: same findings, same order."""
        project = _write_python_app(tmp_path, quality=["tests", "ci-cd", "i18n"])
        config = _config(project)

        legacy = run_quality_checks(str(project), config)
        findings, _ = run_quality_checks_with_coverage(str(project), config)

        assert [(f.category, f.priority, f.weight) for f in legacy] == [
            (f.category, f.priority, f.weight) for f in findings
        ]
        assert [f.priority for f in legacy] == ["P0", "P1", "P2"]

    def test_check_wrappers_still_return_plain_finding_lists(
        self, tmp_path: Path
    ) -> None:
        """Callers of the old ``check_*`` helpers see no change at all."""
        project = _write_python_app(tmp_path, quality=["tests", "ci-cd"])
        config = _config(project)

        assert isinstance(check_tests(project, config), list)
        assert check_tests(project, config)[0].category == "tests"
        assert check_ci_cd(project, config)[0].category == "ci-cd"

        silent = _write_python_app(tmp_path / "silent", quality=[])
        assert check_tests(silent, _config(silent)) == []


# ── Coverage shape ─────────────────────────────────────────────────────────


class TestCoverageShape:
    def test_every_check_appears_in_the_denominator(self, tmp_path: Path) -> None:
        project = _write_python_app(tmp_path, quality=[])
        _, coverage = run_quality_checks_with_coverage(str(project), _config(project))

        assert [r.dimension for r in coverage.results] == ALL_DIMENSIONS
        assert coverage.total == len(ALL_DIMENSIONS)
        assert sorted(coverage.measured + coverage.skipped) == sorted(ALL_DIMENSIONS)

    def test_as_dict_survives_a_json_round_trip(self, tmp_path: Path) -> None:
        """It rides in the report that the CI workflow parses as JSON."""
        project = _write_python_app(tmp_path, quality=["tests"])
        _, coverage = run_quality_checks_with_coverage(str(project), _config(project))

        restored = json.loads(json.dumps(coverage.as_dict()))
        assert restored["measured"] == 2
        assert restored["total"] == 6
        assert restored["summary"] == "2/6 measured"
        assert {d["dimension"] for d in restored["dimensions"]} == set(ALL_DIMENSIONS)

    def test_empty_coverage_does_not_divide_by_zero(self) -> None:
        assert QualityCoverage().summary() == "0/0 measured"
        assert QualityCoverage().as_dict()["total"] == 0


# ── The report ─────────────────────────────────────────────────────────────


class TestEvaluateProjectReport:
    def test_score_travels_with_its_denominator(self, tmp_path: Path) -> None:
        project = _write_python_app(tmp_path, quality=[])
        report = evaluate_project(project, _config(project))

        assert report["score"] == 100
        assert report["coverage"]["measured"] == 1
        assert report["coverage"]["total"] == 6
        assert report["coverage"]["summary"] == "1/6 measured"

    def test_report_still_holds_everything_it_used_to(self, tmp_path: Path) -> None:
        project = _write_python_app(tmp_path, quality=["tests", "ci-cd"])
        report = evaluate_project(project, _config(project))

        assert report["project"] == project.name
        assert report["stage"] == "active"
        assert report["score"] == 70
        assert [f["priority"] for f in report["findings"]] == ["P0", "P1"]
        assert len(report["feature_suggestions"]) == 2

    def test_report_is_json_serialisable(self, tmp_path: Path) -> None:
        """``parse_scores`` reads the report with a raw JSON decoder."""
        project = _write_python_app(tmp_path, quality=["tests"])
        report = evaluate_project(project, _config(project))

        restored = json.loads(json.dumps(report, indent=2))
        assert restored["coverage"]["summary"] == "2/6 measured"
