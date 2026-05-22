"""Tests for autoRefine quality checks."""

from pathlib import Path

import pytest
import yaml

from agent.config import ProjectConfig
from agent.tools.quality_tools import (
    check_ci_cd,
    check_i18n,
    check_project_yaml,
    check_security_headers,
    check_tests,
    run_quality_checks,
)


@pytest.fixture
def sample_config() -> ProjectConfig:
    return ProjectConfig(
        name="test-project",
        purpose="A test project",
        users="Developers",
        stage="active",
        goals=["Build something", "Test it"],
        similar=["Similar App"],
        quality=["tests", "ci-cd", "responsive"],
    )


@pytest.fixture
def project_dir(tmp_path: Path, sample_config: ProjectConfig) -> Path:
    """Create a minimal project directory."""
    # Write project.yaml
    data = {
        "name": sample_config.name,
        "purpose": sample_config.purpose,
        "users": sample_config.users,
        "stage": sample_config.stage,
        "goals": sample_config.goals,
        "similar": sample_config.similar,
        "quality": sample_config.quality,
    }
    yaml_path = tmp_path / "project.yaml"
    yaml_path.write_text(yaml.dump(data), encoding="utf-8")
    return tmp_path


class TestCheckTests:
    def test_no_test_dir_reports_finding(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        findings = check_tests(project_dir, sample_config)
        assert len(findings) == 1
        assert findings[0].category == "tests"
        assert findings[0].priority == "P0"

    def test_existing_test_dir_is_clean(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        tests_dir = project_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_example.py").write_text("def test_one(): pass")
        findings = check_tests(project_dir, sample_config)
        assert len(findings) == 0

    def test_skips_when_not_in_quality(self, project_dir: Path) -> None:
        config = ProjectConfig(
            name="no-tests", purpose="", users="", stage="active", quality=[]
        )
        findings = check_tests(project_dir, config)
        assert len(findings) == 0


class TestCheckCiCd:
    def test_no_workflows_reports_finding(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        findings = check_ci_cd(project_dir, sample_config)
        assert len(findings) == 1
        assert findings[0].category == "ci-cd"

    def test_existing_workflows_is_clean(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        wf_dir = project_dir / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI")
        findings = check_ci_cd(project_dir, sample_config)
        assert len(findings) == 0


class TestCheckSecurityHeaders:
    def test_missing_headers(self, project_dir: Path, sample_config: ProjectConfig) -> None:
        swa = project_dir / "staticwebapp.config.json"
        swa.write_text('{"globalHeaders": {}}')
        findings = check_security_headers(project_dir, sample_config)
        assert len(findings) == 1
        assert "Missing security headers" in findings[0].description

    def test_no_swa_config_is_clean(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        findings = check_security_headers(project_dir, sample_config)
        assert len(findings) == 0


class TestCheckProjectYaml:
    def test_missing_yaml(self, tmp_path: Path, sample_config: ProjectConfig) -> None:
        findings = check_project_yaml(tmp_path, sample_config)
        assert len(findings) == 1
        assert findings[0].priority == "P0"

    def test_missing_purpose(self, project_dir: Path) -> None:
        config = ProjectConfig(
            name="test", purpose="", users="", stage="active",
        )
        findings = check_project_yaml(project_dir, config)
        assert any("purpose" in f.description for f in findings)


class TestCheckI18n:
    def _config_with_i18n(self) -> ProjectConfig:
        return ProjectConfig(
            name="test", purpose="p", users="u", stage="active",
            quality=["i18n"],
        )

    def test_no_i18n_declared_skips_check(self, tmp_path: Path) -> None:
        config = ProjectConfig(name="t", purpose="p", users="u", stage="active", quality=[])
        assert check_i18n(tmp_path, config) == []

    def test_missing_i18n_flags_finding(self, tmp_path: Path) -> None:
        findings = check_i18n(tmp_path, self._config_with_i18n())
        assert len(findings) == 1
        assert findings[0].category == "i18n"

    def test_locales_dir_satisfies(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "locales").mkdir(parents=True)
        assert check_i18n(tmp_path, self._config_with_i18n()) == []

    def test_package_json_i18n_dep_satisfies(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"react-i18next": "^13"}}')
        assert check_i18n(tmp_path, self._config_with_i18n()) == []

    def test_html_data_attr_switcher_satisfies(self, tmp_path: Path) -> None:
        # playground-style: data-en/data-ru/data-lv attributes
        (tmp_path / "index.html").write_text(
            '<h2 data-en="Snake" data-ru="Snake-RU" data-lv="Snake-LV">Snake</h2>',
            encoding="utf-8",
        )
        assert check_i18n(tmp_path, self._config_with_i18n()) == []

    def test_python_language_field_satisfies(self, tmp_path: Path) -> None:
        # agentMode-style: Python app with language= signal
        (tmp_path / "app.py").write_text('def reply(language="lv"): pass\n')
        assert check_i18n(tmp_path, self._config_with_i18n()) == []

    def test_python_locale_string_satisfies(self, tmp_path: Path) -> None:
        (tmp_path / "browser.py").write_text('context = browser.new_context(locale="lv-LV")\n')
        assert check_i18n(tmp_path, self._config_with_i18n()) == []


class TestRunQualityChecks:
    def test_returns_sorted_findings(
        self, project_dir: Path, sample_config: ProjectConfig
    ) -> None:
        findings = run_quality_checks(str(project_dir), sample_config)
        priorities = [f.priority for f in findings]
        assert priorities == sorted(
            priorities, key=lambda p: {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(p, 9)
        )
