"""Tests for autoRefine quality checks."""

import json
from pathlib import Path

import pytest
import yaml

from agent.config import ProjectConfig
from agent.main import evaluate_project, suggest_feature_improvements
from agent.tools.quality_tools import (
    QualityCoverage,
    QualityFinding,
    check_ci_cd,
    check_i18n,
    check_project_yaml,
    check_security_headers,
    check_tests,
    measure_i18n,
    measure_project_yaml,
    measure_security_headers,
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


class TestSecurityHeaderEdgeCases:
    @pytest.mark.parametrize(
        "payload",
        [
            "[]",
            '"headers"',
            "0",
            "true",
            "null",
        ],
    )
    def test_non_object_config_is_reported_instead_of_raising(
        self, tmp_path: Path, sample_config: ProjectConfig, payload: str
    ) -> None:
        (tmp_path / "staticwebapp.config.json").write_text(payload, encoding="utf-8")

        findings = measure_security_headers(tmp_path, sample_config).findings

        assert len(findings) == 1
        assert findings[0].category == "security"
        assert findings[0].priority == "P0"
        assert "invalid structure" in findings[0].description

    @pytest.mark.parametrize("headers", ["null", "[]", '"headers"', "0", "true"])
    def test_non_object_global_headers_are_reported_instead_of_raising(
        self, tmp_path: Path, sample_config: ProjectConfig, headers: str
    ) -> None:
        (tmp_path / "staticwebapp.config.json").write_text(
            f'{{"globalHeaders": {headers}}}', encoding="utf-8"
        )

        findings = measure_security_headers(tmp_path, sample_config).findings

        assert len(findings) == 1
        assert findings[0].category == "security"
        assert findings[0].priority == "P0"
        assert "invalid structure" in findings[0].description

    @pytest.mark.parametrize("payload", ["{", '{"globalHeaders":', "not-json", "[1,"])
    def test_invalid_json_is_reported_as_a_security_finding(
        self, tmp_path: Path, sample_config: ProjectConfig, payload: str
    ) -> None:
        (tmp_path / "staticwebapp.config.json").write_text(payload, encoding="utf-8")

        findings = measure_security_headers(tmp_path, sample_config).findings

        assert len(findings) == 1
        assert findings[0].category == "security"
        assert "invalid JSON" in findings[0].description

    @pytest.mark.parametrize(
        "missing",
        [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
            "Strict-Transport-Security",
        ],
    )
    def test_each_required_header_is_checked_independently(
        self, tmp_path: Path, sample_config: ProjectConfig, missing: str
    ) -> None:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Strict-Transport-Security": "max-age=31536000",
        }
        del headers[missing]
        (tmp_path / "staticwebapp.config.json").write_text(
            json.dumps({"globalHeaders": headers}), encoding="utf-8"
        )

        findings = measure_security_headers(tmp_path, sample_config).findings

        assert len(findings) == 1
        assert missing in findings[0].description

    def test_all_required_headers_are_clean(
        self, tmp_path: Path, sample_config: ProjectConfig
    ) -> None:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Strict-Transport-Security": "max-age=31536000",
        }
        (tmp_path / "staticwebapp.config.json").write_text(
            json.dumps({"globalHeaders": headers}), encoding="utf-8"
        )

        assert measure_security_headers(tmp_path, sample_config).findings == []

    def test_missing_global_headers_checks_all_required_headers(
        self, tmp_path: Path, sample_config: ProjectConfig
    ) -> None:
        (tmp_path / "staticwebapp.config.json").write_text("{}", encoding="utf-8")

        findings = measure_security_headers(tmp_path, sample_config).findings

        assert len(findings) == 1
        assert findings[0].priority == "P1"
        assert "Missing security headers" in findings[0].description


class TestI18nEdgeCases:
    @pytest.mark.parametrize(
        "relative",
        [
            "src/i18n",
            "src/locales",
            "public/locales",
            "locales",
            "i18n",
        ],
    )
    def test_each_standard_locale_directory_counts(
        self, tmp_path: Path, relative: str
    ) -> None:
        (tmp_path / relative).mkdir(parents=True)
        config = ProjectConfig(
            name="demo", purpose="p", users="u", stage="active", quality=["i18n"]
        )

        result = measure_i18n(tmp_path, config)

        assert result.measured is True
        assert result.findings == []

    @pytest.mark.parametrize("attribute", ["data-en", "data-ru", "data-lv"])
    def test_each_supported_html_language_attribute_counts(
        self, tmp_path: Path, attribute: str
    ) -> None:
        (tmp_path / "index.html").write_text(
            f'<h1 {attribute}="Hello">Hello</h1>', encoding="utf-8"
        )
        config = ProjectConfig(
            name="demo", purpose="p", users="u", stage="active", quality=["i18n"]
        )

        assert measure_i18n(tmp_path, config).findings == []

    @pytest.mark.parametrize(
        "signal",
        ['language="lv"', 'locale="lv-LV"', 'locale="ru-RU"', "lv-LV", "ru-RU"],
    )
    def test_each_supported_python_language_signal_counts(
        self, tmp_path: Path, signal: str
    ) -> None:
        (tmp_path / "app.py").write_text(f'config = "{signal}"\n', encoding="utf-8")
        config = ProjectConfig(
            name="demo", purpose="p", users="u", stage="active", quality=["i18n"]
        )

        assert measure_i18n(tmp_path, config).findings == []

    @pytest.mark.parametrize("vendor", [".venv", "node_modules", "site-packages"])
    def test_python_language_signal_in_vendor_tree_does_not_count(
        self, tmp_path: Path, vendor: str
    ) -> None:
        vendor_file = tmp_path / vendor / "app.py"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text('language="lv"\n', encoding="utf-8")
        config = ProjectConfig(
            name="demo", purpose="p", users="u", stage="active", quality=["i18n"]
        )

        findings = measure_i18n(tmp_path, config).findings

        assert len(findings) == 1
        assert findings[0].category == "i18n"


class TestProjectYamlEdgeCases:
    @pytest.mark.parametrize(
        ("purpose", "goals", "similar", "missing"),
        [
            ("", ["goal"], ["product"], "purpose"),
            ("purpose", [], ["product"], "goals"),
            ("purpose", ["goal"], [], "similar"),
        ],
    )
    def test_each_missing_project_detail_is_reported(
        self,
        tmp_path: Path,
        purpose: str,
        goals: list[str],
        similar: list[str],
        missing: str,
    ) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n", encoding="utf-8")
        config = ProjectConfig(
            name="demo",
            purpose=purpose,
            users="u",
            stage="active",
            goals=goals,
            similar=similar,
        )

        findings = measure_project_yaml(tmp_path, config).findings

        assert len(findings) == 1
        assert missing in findings[0].description

    @pytest.mark.parametrize(
        ("purpose", "goals", "similar", "missing"),
        [
            (" ", ["goal"], ["product"], "purpose"),
            ("purpose", [" "], ["product"], "goals"),
            ("purpose", ["goal"], [" "], "similar"),
        ],
    )
    def test_whitespace_only_project_details_are_missing(
        self,
        tmp_path: Path,
        purpose: str,
        goals: list[str],
        similar: list[str],
        missing: str,
    ) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n", encoding="utf-8")
        config = ProjectConfig(
            name="demo",
            purpose=purpose,
            users="u",
            stage="active",
            goals=goals,
            similar=similar,
        )

        findings = measure_project_yaml(tmp_path, config).findings

        assert len(findings) == 1
        assert missing in findings[0].description

    def test_all_missing_project_details_are_reported_together(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n", encoding="utf-8")
        config = ProjectConfig(name="demo", purpose="", users="", stage="active")

        findings = measure_project_yaml(tmp_path, config).findings

        assert len(findings) == 3
        assert {finding.priority for finding in findings} == {"P1", "P2"}

    def test_all_whitespace_project_details_are_reported_together(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n", encoding="utf-8")
        config = ProjectConfig(
            name="demo",
            purpose=" ",
            users="",
            stage="active",
            goals=[" "],
            similar=[""],
        )

        findings = measure_project_yaml(tmp_path, config).findings

        assert len(findings) == 3

    @pytest.mark.parametrize("purpose", [None, 0])
    def test_non_string_purpose_is_reported_instead_of_raising(
        self, tmp_path: Path, purpose: object
    ) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n", encoding="utf-8")
        config = ProjectConfig(
            name="demo",
            purpose=purpose,
            users="u",
            stage="active",
            goals=["goal"],
            similar=["product"],
        )

        findings = measure_project_yaml(tmp_path, config).findings

        assert len(findings) == 1
        assert "purpose" in findings[0].description


class TestEvaluationEdgeCases:
    @pytest.mark.parametrize(
        ("goals", "similar", "expected_titles"),
        [
            ([], [], []),
            ([""], [], []),
            (["   "], [], []),
            (["goal"], [], ["Goal-aligned capability: goal"]),
            (["first", "second"], [], [
                "Goal-aligned capability: first",
                "Goal-aligned capability: second",
            ]),
            (["first", "second", "third"], [], [
                "Goal-aligned capability: first",
                "Goal-aligned capability: second",
            ]),
            ([], ["competitor"], ["Feature parity review with competitor"]),
            ([], ["", "  ", "competitor"], []),
            (["goal"], ["competitor"], [
                "Goal-aligned capability: goal",
                "Feature parity review with competitor",
            ]),
            (["g1", "g2"], ["c1", "c2"], [
                "Goal-aligned capability: g1",
                "Goal-aligned capability: g2",
                "Feature parity review with c1",
            ]),
            ([], ["one", "two", "three"], [
                "Feature parity review with one",
                "Feature parity review with two",
            ]),
            (["", "goal"], ["competitor"], [
                "Goal-aligned capability: goal",
                "Feature parity review with competitor",
            ]),
            (["", "second", "third"], [], ["Goal-aligned capability: second"]),
        ],
    )
    def test_suggestions_handle_empty_whitespace_and_capacity_edges(
        self, goals: list[str], similar: list[str], expected_titles: list[str]
    ) -> None:
        config = ProjectConfig(
            name="demo",
            purpose="p",
            users="u",
            stage="active",
            goals=goals,
            similar=similar,
        )

        suggestions = suggest_feature_improvements(config)

        assert [suggestion["title"] for suggestion in suggestions] == expected_titles
        assert len(suggestions) <= 3

    def test_evaluation_clamps_excessive_deductions_at_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        finding = QualityFinding(
            category="tests", description="missing tests", priority="P0", weight=101
        )
        monkeypatch.setattr(
            "agent.main.run_quality_checks_with_coverage",
            lambda _path, _config, _repo=None: ([finding], QualityCoverage()),
        )
        config = ProjectConfig(name="demo", purpose="p", users="u", stage="active")

        report = evaluate_project(tmp_path, config)

        assert report["score"] == 0
        assert report["findings"][0]["category"] == "tests"
        assert report["coverage"]["summary"] == "0/0 measured"
