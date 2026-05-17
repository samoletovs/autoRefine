"""Quality tools — deterministic technical quality checks."""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

REQUIRED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000",
}


@dataclass
class QualityFinding:
    """A single quality finding."""

    category: str  # tests, security, deps, ci-cd, a11y, i18n, functional
    description: str
    priority: str  # P0, P1, P2, P3
    weight: int  # points deducted from score (P0=20, P1=10, P2=5, P3=2)
    fixable: bool = False  # can autoRefine fix this automatically?


def check_tests(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Check if the project has tests."""
    findings: list[QualityFinding] = []

    if "tests" not in config.quality:
        return findings  # project doesn't claim to have tests

    test_dirs = [
        project_dir / "tests",
        project_dir / "test",
        project_dir / "__tests__",
        project_dir / "src" / "__tests__",
    ]

    has_tests = any(d.exists() and any(d.iterdir()) for d in test_dirs)

    if not has_tests:
        findings.append(QualityFinding(
            category="tests",
            description="No test directory found despite 'tests' in quality traits",
            priority="P0",
            weight=20,
        ))

    return findings


def check_ci_cd(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Check CI/CD pipeline presence."""
    findings: list[QualityFinding] = []

    if "ci-cd" not in config.quality:
        return findings

    workflows = project_dir / ".github" / "workflows"
    if not workflows.exists() or not list(workflows.glob("*.yml")):
        findings.append(QualityFinding(
            category="ci-cd",
            description="No GitHub Actions workflows found despite 'ci-cd' in quality traits",
            priority="P1",
            weight=10,
        ))

    return findings


def check_security_headers(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Check SWA security headers."""
    findings: list[QualityFinding] = []
    swa_config = project_dir / "staticwebapp.config.json"

    if not swa_config.exists():
        return findings

    try:
        data = json.loads(swa_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        findings.append(QualityFinding(
            category="security",
            description="staticwebapp.config.json has invalid JSON",
            priority="P0",
            weight=20,
        ))
        return findings

    headers = data.get("globalHeaders", {})
    missing = [h for h in REQUIRED_SECURITY_HEADERS if h not in headers]

    if missing:
        findings.append(QualityFinding(
            category="security",
            description=f"Missing security headers: {', '.join(missing)}",
            priority="P1",
            weight=10,
            fixable=True,
        ))

    return findings


def check_dependencies(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Check for outdated or vulnerable dependencies."""
    findings: list[QualityFinding] = []
    pkg_json = project_dir / "package.json"

    if not pkg_json.exists():
        return findings

    # Check for npm audit vulnerabilities
    try:
        result = subprocess.run(
            ["npm", "audit", "--json"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        audit = json.loads(result.stdout)
        vulns = audit.get("metadata", {}).get("vulnerabilities", {})
        critical = vulns.get("critical", 0)
        high = vulns.get("high", 0)

        if critical > 0:
            findings.append(QualityFinding(
                category="deps",
                description=f"{critical} critical npm vulnerabilities",
                priority="P0",
                weight=20,
                fixable=True,
            ))
        elif high > 0:
            findings.append(QualityFinding(
                category="deps",
                description=f"{high} high npm vulnerabilities",
                priority="P1",
                weight=10,
                fixable=True,
            ))
    except (json.JSONDecodeError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return findings


def check_project_yaml(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Check project.yaml completeness."""
    findings: list[QualityFinding] = []
    yaml_path = project_dir / "project.yaml"

    if not yaml_path.exists():
        findings.append(QualityFinding(
            category="metadata",
            description="Missing project.yaml — autoRefine cannot understand this project",
            priority="P0",
            weight=25,
        ))
        return findings

    if not _config.purpose:
        findings.append(QualityFinding(
            category="metadata",
            description="project.yaml has no 'purpose' field",
            priority="P1",
            weight=10,
        ))

    if not _config.goals:
        findings.append(QualityFinding(
            category="metadata",
            description="project.yaml has no 'goals' — agent cannot evaluate feature completeness",
            priority="P1",
            weight=10,
        ))

    if not _config.similar:
        findings.append(QualityFinding(
            category="metadata",
            description="project.yaml has no 'similar' — agent cannot research competition",
            priority="P2",
            weight=5,
        ))

    return findings


def check_i18n(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Check internationalization if declared."""
    findings: list[QualityFinding] = []

    if "i18n" not in config.quality:
        return findings

    # Look for i18n config files
    i18n_indicators = [
        project_dir / "src" / "i18n",
        project_dir / "src" / "locales",
        project_dir / "public" / "locales",
    ]

    has_i18n = any(d.exists() for d in i18n_indicators)

    if not has_i18n:
        # Check package.json for i18n deps
        pkg = project_dir / "package.json"
        if pkg.exists():
            content = pkg.read_text(encoding="utf-8", errors="ignore")
            if "i18n" in content or "intl" in content:
                has_i18n = True

    if not has_i18n:
        findings.append(QualityFinding(
            category="i18n",
            description="'i18n' declared in quality traits but no i18n setup found",
            priority="P2",
            weight=5,
        ))

    return findings


def run_quality_checks(project_dir: str, config: ProjectConfig) -> list[QualityFinding]:
    """Run all quality checks on a project."""
    path = Path(project_dir)
    findings: list[QualityFinding] = []

    findings.extend(check_project_yaml(path, config))
    findings.extend(check_tests(path, config))
    findings.extend(check_ci_cd(path, config))
    findings.extend(check_security_headers(path, config))
    findings.extend(check_dependencies(path, config))
    findings.extend(check_i18n(path, config))

    # Sort by priority
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda f: priority_order.get(f.priority, 9))

    return findings
