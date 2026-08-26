"""Tests for test and CI detection — built from layouts that exist in the fleet.

The old `check_tests` looked only at four directories at the repository root.
Measured across the 24 live manifest projects on 2026-08-26, widening the
declaration gate would have filed six P0 "no tests" findings of which **three
were false**, because the repos keep their tests one level down:

    courier    -> app/tests/test_feedback.py            + a tests.yml workflow
    mindMe     -> harness/tests/test_*.py, pytest.ini   + a tests.yml workflow
    foundryLab -> agents/labMemoryAgent/src/smoke_test.py

Two of those have CI running the very suite the check said did not exist. A P0
titled "no test directory found" against such a repo is a demonstrably wrong
statement handed to a coding agent at 10-30 minutes a go.

The fixtures below are those real layouts rather than invented ones. The
false-negative direction is tested at least as hard, because a detector too
permissive to fail is just another check that can never fire — which is the
disease the rest of this module exists to cure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import ProjectConfig
from agent.tools.quality_tools import (
    LEGACY_TEST_DIRS,
    _has_tests,
    _is_test_file,
    measure_ci_cd,
    measure_tests,
)


@pytest.fixture
def config() -> ProjectConfig:
    return ProjectConfig(
        name="demo", purpose="p", users="u", stage="active",
        quality=["tests", "ci-cd"],
    )


def _write(root: Path, relative: str, content: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _flags(tmp_path: Path, config: ProjectConfig) -> bool:
    """True when the check reports a finding."""
    return bool(measure_tests(tmp_path, config).findings)


# ── Real fleet layouts ─────────────────────────────────────────────────────


class TestRealFleetLayouts:
    def test_root_tests_dir(self, tmp_path: Path, config: ProjectConfig) -> None:
        """autoRefine, golazo, tPlan — the layout the old rule already handled."""
        _write(tmp_path, "tests/test_quality.py", "def test_x(): pass")
        assert _flags(tmp_path, config) is False

    def test_nested_app_tests(self, tmp_path: Path, config: ProjectConfig) -> None:
        """courier. Had a tests.yml workflow running these when it was flagged."""
        _write(tmp_path, "app/tests/test_feedback.py", "def test_x(): pass")
        _write(tmp_path, "app/tests/test_recipients.py", "def test_y(): pass")
        assert _flags(tmp_path, config) is False

    def test_nested_harness_tests_with_pytest_ini(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """mindMe."""
        _write(tmp_path, "pytest.ini", "[pytest]\n")
        _write(tmp_path, "harness/tests/conftest.py", "")
        _write(tmp_path, "harness/tests/test_function_app.py", "def test_x(): pass")
        _write(tmp_path, "scripts/local/test_briefing_snapshot.py", "def test_z(): pass")
        assert _flags(tmp_path, config) is False

    def test_a_single_deep_smoke_test(self, tmp_path: Path, config: ProjectConfig) -> None:
        """foundryLab. One test is not a suite, but it is not "no tests" either."""
        _write(tmp_path, "agents/labMemoryAgent/src/smoke_test.py", "def test_x(): pass")
        assert _flags(tmp_path, config) is False

    def test_colocated_dot_test_files(self, tmp_path: Path, config: ProjectConfig) -> None:
        """glassBox — and the one repo whose *current* score this fix changes.

        It declares `tests`, keeps them beside the source as `.test.ts`, and has
        therefore been carrying a false P0 "no test directory found" while having
        a real suite. 70 -> 90.
        """
        _write(tmp_path, "src/authUrls.test.ts", "it('works', () => {})")
        _write(tmp_path, "src/ssPackage.test.ts", "it('works', () => {})")
        _write(tmp_path, "api/src/logic.test.ts", "it('works', () => {})")
        assert _flags(tmp_path, config) is False

    def test_a_bare_test_js_harness(self, tmp_path: Path, config: ProjectConfig) -> None:
        """playground.

        A judgement call, recorded deliberately: `test.js` is an ambiguous name,
        but playground's is 3.8 KB of `console.assert` cases. Filing "no tests"
        against it would be false, so a bare `test.<code suffix>` counts. Note the
        suffix requirement is what stops `test.yml` or `test_plan.md` counting.
        """
        _write(tmp_path, "test.js", "function testLang(){ console.assert(true); }")
        assert _flags(tmp_path, config) is False

    def test_genuinely_untested_still_flags(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """folio, payArc, playground — the three true positives."""
        _write(tmp_path, "app.py", "def main(): pass")
        _write(tmp_path, "README.md", "# folio")

        findings = measure_tests(tmp_path, config).findings
        assert len(findings) == 1
        assert findings[0].priority == "P0"
        assert findings[0].weight == 20


# ── The false-negative direction ───────────────────────────────────────────


class TestPermissivenessGuards:
    """A detector that cannot fail is worthless. These are the ways it must."""

    @pytest.mark.parametrize(
        "vendor",
        ["node_modules", "site-packages", "vendor", "dist", "build", "__pycache__"],
    )
    def test_a_vendored_test_certifies_nothing(
        self, tmp_path: Path, config: ProjectConfig, vendor: str
    ) -> None:
        _write(tmp_path, f"{vendor}/somelib/test_upstream.py", "def test_x(): pass")
        _write(tmp_path, "app.py", "def main(): pass")
        assert _flags(tmp_path, config) is True

    @pytest.mark.parametrize("hidden", [".venv", ".tox", ".git", ".pytest_cache"])
    def test_a_test_in_a_dot_directory_certifies_nothing(
        self, tmp_path: Path, config: ProjectConfig, hidden: str
    ) -> None:
        _write(tmp_path, f"{hidden}/lib/test_installed.py", "def test_x(): pass")
        assert _flags(tmp_path, config) is True

    def test_a_vendored_tests_directory_certifies_nothing(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        _write(tmp_path, "node_modules/pkg/tests/index.js", "// upstream suite")
        assert _flags(tmp_path, config) is True

    def test_a_runner_config_alone_is_not_a_test(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        """`pytest.ini` with nothing to run runs nothing.

        Config without tests is the declared-but-not-real pattern this whole
        module exists to expose. It must not certify.
        """
        _write(tmp_path, "pytest.ini", "[pytest]\n")
        _write(tmp_path, "tox.ini", "[tox]\n")
        assert _flags(tmp_path, config) is True

    @pytest.mark.parametrize(
        "name", ["test_plan.md", "testing.md", "latest_notes.txt", "contest.py", "attest.py"],
    )
    def test_names_that_only_look_like_tests(
        self, tmp_path: Path, config: ProjectConfig, name: str
    ) -> None:
        _write(tmp_path, name)
        assert _flags(tmp_path, config) is True

    def test_an_empty_repo_flags(self, tmp_path: Path, config: ProjectConfig) -> None:
        assert _flags(tmp_path, config) is True


# ── Naming conventions ─────────────────────────────────────────────────────


class TestIsTestFile:
    @pytest.mark.parametrize(
        "name",
        [
            "test_thing.py", "thing_test.py", "smoke_test.py", "conftest.py",
            "widget.test.ts", "widget.test.tsx", "widget.spec.js", "widget_spec.rb",
            "handler_test.go", "Thing.test.jsx", "TEST_UPPER.PY",
        ],
    )
    def test_recognised(self, name: str) -> None:
        assert _is_test_file(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "test_plan.md", "testing.md", "readme.txt", "contest.py",
            "manifest.json", "latest.py", "protest.py", "test.yml",
        ],
    )
    def test_not_recognised(self, name: str) -> None:
        assert _is_test_file(name) is False


class TestTestDirectoryNames:
    @pytest.mark.parametrize("name", ["tests", "test", "__tests__", "spec", "specs"])
    def test_nested_test_directories_count(
        self, tmp_path: Path, config: ProjectConfig, name: str
    ) -> None:
        _write(tmp_path, f"packages/core/{name}/anything.txt")
        assert _flags(tmp_path, config) is False


# ── The contained-change guarantee ─────────────────────────────────────────


class TestCanOnlyRemoveFindings:
    """Everything the old rule accepted must still be accepted.

    This is what makes the change contained rather than fleet-wide: it cannot
    create a finding, so it cannot create an idea, so it cannot spend money.
    """

    @pytest.mark.parametrize("legacy", LEGACY_TEST_DIRS)
    def test_every_legacy_directory_still_certifies(
        self, tmp_path: Path, config: ProjectConfig, legacy: str
    ) -> None:
        _write(tmp_path, f"{legacy}/anything.txt")
        assert _has_tests(tmp_path) is True

    @pytest.mark.parametrize("legacy", LEGACY_TEST_DIRS)
    def test_a_legacy_directory_holding_only_a_subdirectory_still_certifies(
        self, tmp_path: Path, config: ProjectConfig, legacy: str
    ) -> None:
        """The old rule used `any(iterdir())`, which a bare subdirectory satisfies."""
        (tmp_path / legacy / "unit").mkdir(parents=True)
        assert _has_tests(tmp_path) is True

    def test_the_check_is_still_gated_on_declaration(self, tmp_path: Path) -> None:
        """Detection changed; the gate did not. This PR does not open it."""
        undeclared = ProjectConfig(
            name="d", purpose="p", users="u", stage="active", quality=[]
        )
        result = measure_tests(tmp_path, undeclared)

        assert result.measured is False
        assert result.findings == []


# ── CI detection ───────────────────────────────────────────────────────────


class TestCiDetection:
    @pytest.mark.parametrize("filename", ["ci.yml", "ci.yaml"])
    def test_both_extensions_count(
        self, tmp_path: Path, config: ProjectConfig, filename: str
    ) -> None:
        """Actions accepts either. Globbing only `*.yml` was the same bug one
        field over — no repo in the fleet trips it today, which is exactly why
        it would have sat there unnoticed."""
        _write(tmp_path, f".github/workflows/{filename}", "name: CI")
        assert measure_ci_cd(tmp_path, config).findings == []

    def test_no_workflows_still_flags(self, tmp_path: Path, config: ProjectConfig) -> None:
        findings = measure_ci_cd(tmp_path, config).findings
        assert len(findings) == 1
        assert findings[0].priority == "P1"

    def test_an_empty_workflows_directory_still_flags(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        assert len(measure_ci_cd(tmp_path, config).findings) == 1

    def test_a_non_workflow_file_does_not_count(
        self, tmp_path: Path, config: ProjectConfig
    ) -> None:
        _write(tmp_path, ".github/workflows/README.md", "docs")
        assert len(measure_ci_cd(tmp_path, config).findings) == 1
