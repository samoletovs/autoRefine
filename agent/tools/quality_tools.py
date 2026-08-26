"""Quality tools — deterministic technical quality checks.

Each check reports two independent things, and conflating them is what made the
0-100 score dishonest: *findings* (what is wrong) and *coverage* (whether the
check ran at all). Most checks are gated on the project's self-declared
``project.yaml`` ``quality:`` list or on a stack artefact such as
``package.json``, so "no findings" has always meant either "clean" or "never
looked", with no way to tell which. A project that declares nothing scores
100/100.

:class:`DimensionResult` separates the two. :func:`run_quality_checks` keeps its
old findings-only contract; :func:`run_quality_checks_with_coverage` returns the
denominator alongside it. No weight, finding or score changes as a result —
this makes the gap measurable, it does not close it.
"""

import json
import logging
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

REQUIRED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000",
}

# Why a dimension produced no findings.
#
# Most checks here are gated: they early-return unless the project's own
# `project.yaml` lists the trait, or unless the stack happens to carry the file
# they read. An empty finding list therefore means one of two opposite things —
# "we looked and it was fine" or "we never looked" — and the 0-100 score cannot
# tell them apart, because both deduct nothing. A project that declares nothing
# scores 100 for it. These reasons are what make the difference legible, and
# they change no finding and no score: they only say what the number covers.
SKIP_NOT_DECLARED = "not-declared"
SKIP_NOT_APPLICABLE = "not-applicable"
SKIP_TOOLING_UNAVAILABLE = "tooling-unavailable"


@dataclass
class QualityFinding:
    """A single quality finding."""

    category: str  # tests, security, deps, ci-cd, a11y, i18n, functional
    description: str
    priority: str  # P0, P1, P2, P3
    weight: int  # points deducted from score (P0=20, P1=10, P2=5, P3=2)
    fixable: bool = False  # can autoRefine fix this automatically?
    # True when *no pull request can repair this* — the remedy is a repository
    # setting, an org policy, or infrastructure outside the repo. Such a finding
    # is reported and scored like any other but is withheld from the planning
    # prompt, because an idea filed from it buys a 10-30 minute coding-agent run
    # that is guaranteed to produce nothing. See `plannable_findings`.
    #
    # NOT the same as `fixable`, and confusing the two would be expensive.
    # `fixable` means autoRefine's own deterministic fixer can repair it without
    # an LLM; almost every finding is `fixable=False` yet perfectly repairable by
    # a coding agent, so reusing that field here would silently starve the model
    # of nearly every finding it currently sees.
    advisory: bool = False


@dataclass
class DimensionResult:
    """The outcome of one quality dimension — including "did not run".

    ``measured`` is derived from ``skip_reason`` rather than stored beside it,
    so the two can never drift into disagreeing about whether the check ran.
    """

    dimension: str  # metadata, tests, ci-cd, security, deps, i18n
    findings: list[QualityFinding] = field(default_factory=list)
    skip_reason: str | None = None  # one of the SKIP_* constants
    detail: str = ""  # human-readable, shown next to the score

    @property
    def measured(self) -> bool:
        """True when the check actually inspected the project."""
        return self.skip_reason is None


@dataclass
class QualityCoverage:
    """Which dimensions the score is built from, and why the rest are absent.

    This is the denominator the 0-100 score has always been missing. It carries
    no weight and never alters a finding — reporting only.
    """

    results: list[DimensionResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def measured(self) -> list[str]:
        return [r.dimension for r in self.results if r.measured]

    @property
    def skipped(self) -> list[str]:
        return [r.dimension for r in self.results if not r.measured]

    def summary(self) -> str:
        """A terse "2/6 measured" for Telegram, where every character costs."""
        return f"{len(self.measured)}/{self.total} measured"

    def as_dict(self) -> dict[str, object]:
        """JSON-serialisable form for the evaluation report."""
        return {
            "measured": len(self.measured),
            "total": self.total,
            "summary": self.summary(),
            "dimensions": [
                {
                    "dimension": r.dimension,
                    "measured": r.measured,
                    "skip_reason": r.skip_reason,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


def is_advisory(finding: Mapping[str, Any]) -> bool:
    """True when *no pull request can repair* this finding.

    Operates on the plain-dict form because that is the shape a finding has by
    the time it reaches the planning prompt (``evaluate_project`` flattens
    :class:`QualityFinding` into the report).

    Non-boolean values are ignored rather than coerced, and loudly. ``bool("false")``
    is ``True``, and a single such typo would silently withhold every finding from
    the model — a far worse failure than the one this guards against. Absent means
    plannable, which is exactly today's behaviour for every existing finding.
    """
    value = finding.get("advisory", False)
    if isinstance(value, bool):
        return value
    log.warning(
        "Ignoring non-boolean 'advisory' value %r on finding %r — treating it as "
        "plannable", value, finding.get("description", "?"),
    )
    return False


def plannable_findings(findings: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The findings a coding agent could actually act on.

    Advisory findings are dropped. They still score, still appear in the report,
    and still reach humans through Telegram and the dashboard — they are withheld
    only from the LLM, because an improvement generated from one becomes a GitHub
    issue that, if approved, buys a 10-30 minute coding-agent run against a defect
    no commit can fix.

    Priority is deliberately *not* the mechanism. Filing is gated on
    ``DEFAULT_IDEA_PRIORITIES = {"P0", "P1"}``, but a P2 finding still enters the
    prompt and the model is free to answer it with a P1 improvement. Only removing
    it from the prompt actually closes the path.
    """
    return [f for f in findings if not is_advisory(f)]


def _measured(dimension: str, findings: list[QualityFinding]) -> DimensionResult:
    """A dimension that ran. An empty list here genuinely means "clean"."""
    return DimensionResult(dimension=dimension, findings=findings)


def _skipped(dimension: str, reason: str, detail: str) -> DimensionResult:
    """A dimension that never ran. It contributes no findings *and* no assurance."""
    return DimensionResult(dimension=dimension, skip_reason=reason, detail=detail)


def measure_tests(project_dir: Path, config: ProjectConfig) -> DimensionResult:
    """Check if the project has tests — only where it claims to have them."""
    findings: list[QualityFinding] = []

    if "tests" not in config.quality:
        return _skipped(
            "tests",
            SKIP_NOT_DECLARED,
            "'tests' not listed in project.yaml quality:",
        )

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

    return _measured("tests", findings)


def check_tests(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_tests`."""
    return measure_tests(project_dir, config).findings


def measure_ci_cd(project_dir: Path, config: ProjectConfig) -> DimensionResult:
    """Check CI/CD pipeline presence."""
    findings: list[QualityFinding] = []

    if "ci-cd" not in config.quality:
        return _skipped(
            "ci-cd",
            SKIP_NOT_DECLARED,
            "'ci-cd' not listed in project.yaml quality:",
        )

    workflows = project_dir / ".github" / "workflows"
    if not workflows.exists() or not list(workflows.glob("*.yml")):
        findings.append(QualityFinding(
            category="ci-cd",
            description="No GitHub Actions workflows found despite 'ci-cd' in quality traits",
            priority="P1",
            weight=10,
        ))

    return _measured("ci-cd", findings)


def check_ci_cd(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_ci_cd`."""
    return measure_ci_cd(project_dir, config).findings


def measure_security_headers(project_dir: Path, _config: ProjectConfig) -> DimensionResult:
    """Check SWA security headers."""
    findings: list[QualityFinding] = []
    swa_config = project_dir / "staticwebapp.config.json"

    if not swa_config.exists():
        return _skipped(
            "security",
            SKIP_NOT_APPLICABLE,
            "no staticwebapp.config.json — headers are not this project's to set",
        )

    try:
        data = json.loads(swa_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        findings.append(QualityFinding(
            category="security",
            description="staticwebapp.config.json has invalid JSON",
            priority="P0",
            weight=20,
        ))
        return _measured("security", findings)

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

    return _measured("security", findings)


def check_security_headers(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_security_headers`."""
    return measure_security_headers(project_dir, _config).findings


def measure_dependencies(project_dir: Path, _config: ProjectConfig) -> DimensionResult:
    """Check for outdated or vulnerable dependencies."""
    findings: list[QualityFinding] = []
    pkg_json = project_dir / "package.json"

    if not pkg_json.exists():
        return _skipped(
            "deps",
            SKIP_NOT_APPLICABLE,
            "no package.json — nothing for npm audit to read",
        )

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
    except (json.JSONDecodeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # npm missing from the runner, or the audit timed out. The old code
        # swallowed this and returned no findings, which reads downstream as a
        # clean dependency tree. It is not one — nothing was ever audited.
        log.debug("npm audit did not complete in %s: %s", project_dir, exc)
        return _skipped(
            "deps",
            SKIP_TOOLING_UNAVAILABLE,
            f"npm audit did not complete ({type(exc).__name__})",
        )

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

    return _measured("deps", findings)


def check_dependencies(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_dependencies`."""
    return measure_dependencies(project_dir, _config).findings


def measure_project_yaml(project_dir: Path, _config: ProjectConfig) -> DimensionResult:
    """Check project.yaml completeness. The one check that always runs."""
    findings: list[QualityFinding] = []
    yaml_path = project_dir / "project.yaml"

    if not yaml_path.exists():
        findings.append(QualityFinding(
            category="metadata",
            description="Missing project.yaml — autoRefine cannot understand this project",
            priority="P0",
            weight=25,
        ))
        return _measured("metadata", findings)

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

    return _measured("metadata", findings)


def check_project_yaml(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_project_yaml`."""
    return measure_project_yaml(project_dir, _config).findings


def measure_i18n(project_dir: Path, config: ProjectConfig) -> DimensionResult:
    """Check internationalization if declared."""
    findings: list[QualityFinding] = []

    if "i18n" not in config.quality:
        return _skipped(
            "i18n",
            SKIP_NOT_DECLARED,
            "'i18n' not listed in project.yaml quality:",
        )

    # Look for i18n config files / directories (standard layouts)
    i18n_indicators = [
        project_dir / "src" / "i18n",
        project_dir / "src" / "locales",
        project_dir / "public" / "locales",
        project_dir / "locales",
        project_dir / "i18n",
    ]

    has_i18n = any(d.exists() for d in i18n_indicators)

    # Check package.json for i18n deps
    if not has_i18n:
        pkg = project_dir / "package.json"
        if pkg.exists():
            content = pkg.read_text(encoding="utf-8", errors="ignore")
            if "i18n" in content or "intl" in content:
                has_i18n = True

    # NauroLabs-style i18n: data-en/data-ru/data-lv attributes on HTML elements
    # (used by playground for in-place language switching without a framework)
    if not has_i18n:
        for html_file in project_dir.glob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "data-en=" in content or "data-ru=" in content or "data-lv=" in content:
                has_i18n = True
                break

    # NauroLabs-style i18n: Python apps that branch behavior on a `language`
    # field (e.g. agentMode user profiles + LLM prompt switching)
    if not has_i18n:
        python_signals = ("language=", "locale=\"lv", "locale=\"ru", "lv-LV", "ru-RU")
        for py_file in list(project_dir.rglob("*.py"))[:200]:
            # Skip vendor/venv dirs
            parts = set(py_file.parts)
            if ".venv" in parts or "node_modules" in parts or "site-packages" in parts:
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(sig in content for sig in python_signals):
                has_i18n = True
                break

    if not has_i18n:
        findings.append(QualityFinding(
            category="i18n",
            description="'i18n' declared in quality traits but no i18n setup found",
            priority="P2",
            weight=5,
        ))

    return _measured("i18n", findings)


def check_i18n(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_i18n`."""
    return measure_i18n(project_dir, config).findings


# Every dimension the score is built from, in report order. Adding a check here
# is what puts it in the denominator; a check that is not listed is invisible to
# both the findings and the coverage, which is the failure mode this whole file
# now exists to make impossible.
_MEASURERS = (
    measure_project_yaml,
    measure_tests,
    measure_ci_cd,
    measure_security_headers,
    measure_dependencies,
    measure_i18n,
)


def run_quality_checks_with_coverage(
    project_dir: str, config: ProjectConfig
) -> tuple[list[QualityFinding], QualityCoverage]:
    """Run all quality checks, returning findings *and* what they cover.

    The findings half is byte-identical to what :func:`run_quality_checks` has
    always returned — same checks, same order, same weights, same score. The
    coverage half is new and purely descriptive: it says which of those checks
    actually looked at anything.
    """
    path = Path(project_dir)
    results = [measure(path, config) for measure in _MEASURERS]

    findings = [f for result in results for f in result.findings]

    # Sort by priority. `list.sort` is stable, so findings of equal priority
    # keep the dimension order above — the order callers already depend on.
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda f: priority_order.get(f.priority, 9))

    return findings, QualityCoverage(results=results)


def run_quality_checks(project_dir: str, config: ProjectConfig) -> list[QualityFinding]:
    """Run all quality checks on a project.

    Unchanged contract: priority-sorted findings, nothing else. Callers that
    want to know what the score covers should use
    :func:`run_quality_checks_with_coverage`.
    """
    findings, _coverage = run_quality_checks_with_coverage(project_dir, config)
    return findings
