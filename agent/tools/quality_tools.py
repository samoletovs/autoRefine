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
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from agent.config import ProjectConfig

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Dependabot alert reads. One or two calls per repo per sweep, against a 15,000/hr
# core budget — three orders of magnitude of headroom, measured 2026-08-26.
DEPENDABOT_PAGE_SIZE = 100
DEPENDABOT_MAX_PAGES = 10
DEPENDABOT_TIMEOUT_SECONDS = 20

# Set to "1" to stop the deps check making any network call. It then reports
# `tooling-unavailable` — never "clean" — so disabling it costs coverage rather
# than quietly manufacturing assurance. This exists because the check is the
# first thing in a previously offline module to call out to the network, twice a
# day across the fleet, and the alternative to a switch is an emergency PR.
DEPENDABOT_DISABLED_ENV = "AUTOREFINE_SKIP_DEPENDABOT"

# Same idea for the branch-protection read. Its own switch rather than a shared
# one, so an emergency here cannot silently take the dependency check with it.
BRANCH_PROTECTION_DISABLED_ENV = "AUTOREFINE_SKIP_BRANCH_PROTECTION"

# Failure types a GitHub read can raise that are *not* httpx.HTTPError, kept in
# one place because missing one is how an exception escapes a check and costs a
# project its entire evaluation rather than one dimension. `resp.json()` decodes
# raw bytes, so a non-UTF-8 body raises UnicodeDecodeError — a ValueError, but
# neither a JSON error nor an httpx one. httpx.InvalidURL is likewise not an
# httpx.HTTPError and is reachable from a malformed slug or Link cursor.
_GITHUB_READ_ERRORS = (httpx.HTTPError, httpx.InvalidURL, ValueError)

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


@dataclass(frozen=True)
class RepoContext:
    """Who this project is on GitHub, for checks that must ask GitHub.

    Optional throughout. A local run — ``scripts/eval_all.py`` walking sibling
    directories, or a test — has no slug and no token, and every check that needs
    one then reports ``tooling-unavailable`` rather than inventing an answer.
    """

    slug: str | None = None  # "owner/name"
    token: str | None = None

    @property
    def can_query_github(self) -> bool:
        return bool(self.slug) and bool(self.token)


class GitHubUnavailable(Exception):
    """A GitHub read did not complete.

    Deliberately an exception rather than a falsy return. "No alerts" and "no
    protection rule" are real answers; "we could not look" is not, and the whole
    class of bug these checks exist to remove is the two being indistinguishable.
    """


class DependabotUnavailable(GitHubUnavailable):
    """Open alerts could not be read.

    Deliberately an exception rather than an empty list. ``[]`` is a real answer
    meaning "no open critical or high alerts", and the entire reason this check
    was rewritten is that its predecessor could not tell that apart from "npm was
    never installed on the runner" — which is the state the production job has
    always been in. A caller cannot accidentally treat this as a clean bill of
    health, because there is no value here to mistake for one.
    """


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _describe_http_failure(resp: httpx.Response) -> str:
    """Status plus GitHub's own message, which is usually the actual diagnosis.

    A repo with the feature switched off answers 403 "Dependabot alerts are
    disabled for this repository." — worth repeating verbatim rather than
    flattening to "403".
    """
    detail = ""
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        detail = str(body.get("message", "")).strip()
    return f"HTTP {resp.status_code}" + (f" — {detail}" if detail else "")


def _next_link(resp: httpx.Response) -> str | None:
    """The ``rel="next"`` URL from the Link header, if there is one."""
    links = getattr(resp, "links", None)
    if not isinstance(links, Mapping):
        return None
    nxt = links.get("next")
    if not isinstance(nxt, Mapping):
        return None
    url = nxt.get("url")
    return str(url) if url else None


def fetch_dependabot_alerts(slug: str, token: str) -> list[dict]:
    """Open critical/high Dependabot alerts for *slug*.

    Raises :class:`DependabotUnavailable` on every path that is not a successful
    read. Only a 200 carrying a JSON list returns, and only then may the result
    be empty.

    Pagination follows the ``Link`` header. This endpoint paginates by **cursor**,
    not by page number, and passing ``page`` earns a flat rejection from the live
    API::

        HTTP 400 — Pagination using the `page` parameter is not supported.

    That fails closed rather than silently, so it would not have manufactured a
    clean bill of health — but it would have made the check dead on every repo,
    which is the state it is being rescued from. Mocked responses cannot catch
    this; it was found by running against the real API.

    Severity is filtered server-side *and* recounted locally: the query narrows
    the payload, and the recount means a silently ignored filter parameter cannot
    inflate the numbers.
    """
    headers = _github_headers(token)
    alerts: list[dict] = []
    url = f"{GITHUB_API}/repos/{slug}/dependabot/alerts"
    params: dict[str, Any] | None = {
        "state": "open",
        "severity": "critical,high",
        "per_page": DEPENDABOT_PAGE_SIZE,
    }

    try:
        with httpx.Client(headers=headers, timeout=DEPENDABOT_TIMEOUT_SECONDS) as client:
            for _ in range(DEPENDABOT_MAX_PAGES):
                resp = client.get(url, params=params)
                if resp.status_code != 200:
                    raise DependabotUnavailable(_describe_http_failure(resp))

                batch = resp.json()
                if not isinstance(batch, list):
                    raise DependabotUnavailable("response body was not a list of alerts")

                alerts.extend(batch)

                next_url = _next_link(resp)
                if not next_url:
                    return alerts
                # The cursor is already encoded in the next URL; re-sending the
                # original params would overwrite it.
                url, params = next_url, None
    except _GITHUB_READ_ERRORS as exc:
        # See _GITHUB_READ_ERRORS: ValueError covers json.JSONDecodeError and
        # UnicodeDecodeError; httpx.InvalidURL is not an httpx.HTTPError.
        raise DependabotUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # Ran out of pages with a cursor still pointing onwards. The read is
    # incomplete, and an incomplete read reported as a complete one is the exact
    # bug this check was rewritten to remove: if `severity` were ever ignored
    # server-side, a thousand low-severity alerts would fill the cap and the
    # critical ones beyond it would vanish into a measured-clean dimension.
    raise DependabotUnavailable(
        f"more than {DEPENDABOT_MAX_PAGES} pages of alerts — read is incomplete"
    )


def _count_severities(alerts: Iterable[Any]) -> tuple[int, int]:
    """``(critical, high)``, tolerating anything unexpected in the payload."""
    critical = high = 0
    for alert in alerts:
        if not isinstance(alert, Mapping):
            continue
        advisory = alert.get("security_advisory")
        severity = ""
        if isinstance(advisory, Mapping):
            severity = str(advisory.get("severity", "")).strip().lower()
        if severity == "critical":
            critical += 1
        elif severity == "high":
            high += 1
    return critical, high


def _measured(dimension: str, findings: list[QualityFinding]) -> DimensionResult:
    """A dimension that ran. An empty list here genuinely means "clean"."""
    return DimensionResult(dimension=dimension, findings=findings)


def _skipped(dimension: str, reason: str, detail: str) -> DimensionResult:
    """A dimension that never ran. It contributes no findings *and* no assurance."""
    return DimensionResult(dimension=dimension, skip_reason=reason, detail=detail)


# Test detection.
#
# The old rule looked only at four directories at the repository *root*, and it
# was wrong for half the fleet the moment it was used in anger. Measured across
# the 24 live manifest projects on 2026-08-26, widening the gate would have filed
# six P0 "no tests" findings, of which three were false:
#
#   courier    -> app/tests/test_feedback.py        + a tests.yml workflow
#   mindMe     -> harness/tests/test_*.py           + a tests.yml workflow
#   foundryLab -> agents/labMemoryAgent/src/smoke_test.py
#
# Filing "no test directory found" against a repo whose CI is running its suite
# is a demonstrably wrong statement handed to a coding agent at 10-30 minutes a
# go. So detection now covers nested directories and file-naming conventions.
#
# The other direction is the real hazard: a detector too permissive to fail turns
# this into another check that can never fire, which is the disease the rest of
# this file exists to cure. Hence three guards — vendor trees are pruned, a test
# file must carry a code suffix (`test_plan.md` proves nothing), and a runner
# config alone is not evidence (`pytest.ini` with no tests runs no tests).
LEGACY_TEST_DIRS = ("tests", "test", "__tests__", "src/__tests__")
TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})
TEST_FILE_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rb", ".rs", ".java", ".cs", ".php",
})
# Where a permissive glob finds its false comfort.
VENDOR_DIRS = frozenset({
    "node_modules", "site-packages", "vendor", "dist", "build",
    "venv", "env", "target", "out", "coverage", "__pycache__",
})


def _is_test_file(name: str) -> bool:
    """True for a file whose name follows a test convention *and* is code."""
    lower = name.lower()
    if not any(lower.endswith(suffix) for suffix in TEST_FILE_SUFFIXES):
        return False
    if lower == "conftest.py":
        return True
    if lower.startswith(("test_", "test.")):
        return True
    return any(marker in lower for marker in ("_test.", ".test.", "_spec.", ".spec."))


def _walk_source(project_dir: Path) -> Iterable[tuple[Path, list[str], list[str]]]:
    """Walk the tree, pruning vendor and dot directories in place.

    Pruning rather than filtering afterwards: it is what keeps this fast on a
    repo with a populated ``node_modules``, and it is also what stops a vendored
    test suite certifying a project that has none of its own.
    """
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in VENDOR_DIRS and not d.startswith(".")
        ]
        yield Path(dirpath), dirnames, filenames


def _has_tests(project_dir: Path) -> bool:
    """Whether the project has anything that looks like a test suite.

    Deliberately structured as "the old rule, then additions". Every path the old
    rule accepted is still accepted first and unchanged, so this can only ever
    *remove* a finding, never create one — which is what makes it a contained
    change rather than a fleet-wide behaviour change needing a budget decision.
    """
    for relative in LEGACY_TEST_DIRS:
        candidate = project_dir / relative
        if candidate.exists() and any(candidate.iterdir()):
            return True

    for dirpath, dirnames, filenames in _walk_source(project_dir):
        is_test_dir = dirpath != project_dir and dirpath.name.lower() in TEST_DIR_NAMES
        if is_test_dir and (filenames or dirnames):
            return True
        if any(_is_test_file(name) for name in filenames):
            return True

    return False


def measure_tests(
    project_dir: Path, config: ProjectConfig, _repo: RepoContext | None = None,
) -> DimensionResult:
    """Check if the project has tests — only where it claims to have them."""
    findings: list[QualityFinding] = []

    if "tests" not in config.quality:
        return _skipped(
            "tests",
            SKIP_NOT_DECLARED,
            "'tests' not listed in project.yaml quality:",
        )

    if not _has_tests(project_dir):
        findings.append(QualityFinding(
            category="tests",
            description=(
                "No tests found despite 'tests' in quality traits — no test "
                "directory and no test-named source files"
            ),
            priority="P0",
            weight=20,
        ))

    return _measured("tests", findings)


def check_tests(project_dir: Path, config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_tests`."""
    return measure_tests(project_dir, config).findings


def measure_ci_cd(
    project_dir: Path, config: ProjectConfig, _repo: RepoContext | None = None,
) -> DimensionResult:
    """Check CI/CD pipeline presence."""
    findings: list[QualityFinding] = []

    if "ci-cd" not in config.quality:
        return _skipped(
            "ci-cd",
            SKIP_NOT_DECLARED,
            "'ci-cd' not listed in project.yaml quality:",
        )

    workflows = project_dir / ".github" / "workflows"
    # Both extensions. Actions accepts either, and globbing only `*.yml` was the
    # same bug as the root-only test detection one field over: no repo in the
    # fleet trips it today, which is exactly why it would have sat there.
    has_workflow = workflows.exists() and (
        any(workflows.glob("*.yml")) or any(workflows.glob("*.yaml"))
    )
    if not has_workflow:
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


def measure_security_headers(
    project_dir: Path, _config: ProjectConfig, _repo: RepoContext | None = None,
) -> DimensionResult:
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


def measure_dependencies(
    project_dir: Path, _config: ProjectConfig, repo: RepoContext | None = None,
) -> DimensionResult:
    """Check for vulnerable dependencies, via Dependabot's alerts.

    This used to shell out to ``npm audit``. The production Container Apps job
    runs ``image: 'python:3.12'`` (``infrastructure/main.bicep:91``) and installs
    no node, so the check has **never produced a finding in production** — and
    someone once burned a debugging session on an OOM theory that turned out to
    be a missing npm (``main.bicep:96``). It was gated on ``package.json`` too, so
    it never looked at a Python project's dependencies at all.

    Dependabot's alerts cover every ecosystem, need no local tooling, and are
    already enabled fleet-wide. The cost is that this check now needs repo
    identity and a token, which is why they are threaded down here.

    Every failure — no token, no slug, 403, 404, timeout, malformed body — is
    ``tooling-unavailable``. None of them is silence dressed as a clean tree.
    """
    if os.environ.get(DEPENDABOT_DISABLED_ENV) == "1":
        return _skipped(
            "deps",
            SKIP_TOOLING_UNAVAILABLE,
            f"disabled by {DEPENDABOT_DISABLED_ENV}=1",
        )

    repo = repo or RepoContext()
    if not repo.slug:
        return _skipped(
            "deps",
            SKIP_TOOLING_UNAVAILABLE,
            "no repo identity — Dependabot alerts are per-repository",
        )
    if not repo.token:
        return _skipped(
            "deps",
            SKIP_TOOLING_UNAVAILABLE,
            "no GitHub token — cannot read Dependabot alerts",
        )

    try:
        alerts = fetch_dependabot_alerts(repo.slug, repo.token)
    except DependabotUnavailable as exc:
        log.warning("Dependabot alerts unavailable for %s: %s", repo.slug, exc)
        return _skipped("deps", SKIP_TOOLING_UNAVAILABLE, f"Dependabot alerts: {exc}")
    except Exception as exc:
        # Deliberately blind, and the same reasoning as the sweep loop in
        # main.py: this is the only check that leaves the machine, and an
        # unforeseen exception from the HTTP stack would otherwise propagate out
        # of evaluate_project and cost the project its *entire* evaluation —
        # every other dimension included — rather than just this one. Losing one
        # dimension is the honest failure; losing the report is not.
        log.exception("Unexpected failure reading Dependabot alerts for %s", repo.slug)
        return _skipped(
            "deps", SKIP_TOOLING_UNAVAILABLE, f"Dependabot alerts: {type(exc).__name__}",
        )

    critical, high = _count_severities(alerts)
    findings: list[QualityFinding] = []

    if critical > 0:
        findings.append(QualityFinding(
            category="deps",
            description=(
                f"{critical} open Dependabot alert(s) at critical severity — "
                "upgrade the affected dependencies"
            ),
            priority="P0",
            weight=20,
        ))
    elif high > 0:
        findings.append(QualityFinding(
            category="deps",
            description=(
                f"{high} open Dependabot alert(s) at high severity — "
                "upgrade the affected dependencies"
            ),
            priority="P1",
            weight=10,
        ))

    return _measured("deps", findings)


def check_dependencies(project_dir: Path, _config: ProjectConfig) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_dependencies`.

    Has no repo identity to give, so it always reports nothing. That is the same
    empty list it has returned in production since the beginning — the difference
    is that the coverage now says why.
    """
    return measure_dependencies(project_dir, _config).findings


def measure_project_yaml(
    project_dir: Path, _config: ProjectConfig, _repo: RepoContext | None = None,
) -> DimensionResult:
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


def measure_i18n(
    project_dir: Path, config: ProjectConfig, _repo: RepoContext | None = None,
) -> DimensionResult:
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


@dataclass(frozen=True)
class RepoFacts:
    """The repository metadata a settings check needs before it can ask anything."""

    default_branch: str
    archived: bool


def fetch_repo_facts(slug: str, token: str) -> RepoFacts:
    """Read the default branch and archived flag.

    The default branch is taken from the API rather than from the local clone.
    The fleet is split between ``main`` and ``master``, so it cannot be assumed,
    and reading it from a clone would make the answer depend on local tooling
    state — which is the class of bug that left the dependency check dead for the
    life of the project. One extra call per repo per sweep is not worth that.
    """
    try:
        with httpx.Client(
            headers=_github_headers(token), timeout=DEPENDABOT_TIMEOUT_SECONDS
        ) as client:
            resp = client.get(f"{GITHUB_API}/repos/{slug}")
            if resp.status_code != 200:
                raise GitHubUnavailable(_describe_http_failure(resp))
            body = resp.json()
    except _GITHUB_READ_ERRORS as exc:
        raise GitHubUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(body, Mapping):
        raise GitHubUnavailable("repository response was not an object")

    return RepoFacts(
        default_branch=str(body.get("default_branch") or ""),
        archived=bool(body.get("archived", False)),
    )


def branch_is_protected(slug: str, branch: str, token: str) -> bool:
    """True when *branch* is covered by classic protection **or** by a ruleset.

    Both are checked, and that is load-bearing rather than thorough. The case for
    scoring this finding at all is that it is self-resolving: it clears the moment
    somebody changes the setting. Rulesets are the modern way to change it, so a
    check blind to them would keep reporting a repo that had just been fixed —
    and a finding that cannot clear is a permanent penalty, which is exactly what
    AGENTS.md says never to weight.
    """
    try:
        with httpx.Client(
            headers=_github_headers(token), timeout=DEPENDABOT_TIMEOUT_SECONDS
        ) as client:
            classic = client.get(
                f"{GITHUB_API}/repos/{slug}/branches/{branch}/protection"
            )
            if classic.status_code == 200:
                return True
            if classic.status_code != 404:
                raise GitHubUnavailable(_describe_http_failure(classic))

            # 404 here is ambiguous: GitHub says "Branch not protected" for an
            # unprotected branch and also 404s a branch or repo it cannot see.
            # Only the first is an answer. Anything else is treated as unreadable,
            # which errs towards reporting nothing rather than towards a finding
            # nobody can act on.
            detail = _describe_http_failure(classic)
            if "not protected" not in detail.lower():
                raise GitHubUnavailable(detail)

            rules = client.get(f"{GITHUB_API}/repos/{slug}/rules/branches/{branch}")
            if rules.status_code != 200:
                raise GitHubUnavailable(_describe_http_failure(rules))
            active = rules.json()
            if not isinstance(active, list):
                raise GitHubUnavailable("branch rules response was not a list")
            return bool(active)
    except _GITHUB_READ_ERRORS as exc:
        raise GitHubUnavailable(f"{type(exc).__name__}: {exc}") from exc


def measure_branch_protection(
    project_dir: Path, _config: ProjectConfig, repo: RepoContext | None = None,
) -> DimensionResult:
    """Check that the default branch cannot be pushed to directly.

    AGENTS.md rule 1 is "never push directly to main/master", and the merge gate
    that enforces review lives in a workflow a direct push simply goes around. A
    convention enforced by nothing is a convention, not a control.

    The finding is **advisory**: enabling protection is a repository setting, not
    a commit, so no pull request can deliver it. It scores and it reports, but it
    is withheld from the planner — an idea filed from it would buy a 10-30 minute
    coding-agent run that cannot possibly succeed. See AGENTS.md, "Advisory
    findings".
    """
    if os.environ.get(BRANCH_PROTECTION_DISABLED_ENV) == "1":
        return _skipped(
            "branch-protection",
            SKIP_TOOLING_UNAVAILABLE,
            f"disabled by {BRANCH_PROTECTION_DISABLED_ENV}=1",
        )

    repo = repo or RepoContext()
    if not repo.slug:
        return _skipped(
            "branch-protection",
            SKIP_TOOLING_UNAVAILABLE,
            "no repo identity — protection is a per-repository setting",
        )
    if not repo.token:
        return _skipped(
            "branch-protection",
            SKIP_TOOLING_UNAVAILABLE,
            "no GitHub token — cannot read branch protection",
        )

    try:
        facts = fetch_repo_facts(repo.slug, repo.token)

        if facts.archived:
            # Never penalise a project for something it cannot buy: an archived
            # repository's settings are frozen.
            return _skipped(
                "branch-protection",
                SKIP_NOT_APPLICABLE,
                "repository is archived — its settings cannot be changed",
            )
        if not facts.default_branch:
            return _skipped(
                "branch-protection",
                SKIP_NOT_APPLICABLE,
                "repository has no default branch to protect",
            )

        protected = branch_is_protected(repo.slug, facts.default_branch, repo.token)
    except GitHubUnavailable as exc:
        log.warning("Branch protection unreadable for %s: %s", repo.slug, exc)
        return _skipped(
            "branch-protection", SKIP_TOOLING_UNAVAILABLE, f"branch protection: {exc}",
        )
    except Exception as exc:
        # Deliberately blind, for the same reason as measure_dependencies: an
        # unforeseen failure in the HTTP stack must cost this dimension, not the
        # project's entire evaluation.
        log.exception("Unexpected failure reading branch protection for %s", repo.slug)
        return _skipped(
            "branch-protection",
            SKIP_TOOLING_UNAVAILABLE,
            f"branch protection: {type(exc).__name__}",
        )

    findings: list[QualityFinding] = []
    if not protected:
        findings.append(QualityFinding(
            category="branch-protection",
            description=(
                f"Default branch '{facts.default_branch}' has no protection rule or "
                "ruleset — a direct push bypasses the review gate entirely"
            ),
            priority="P1",
            weight=10,
            advisory=True,
        ))

    return _measured("branch-protection", findings)


def check_branch_protection(
    project_dir: Path, _config: ProjectConfig
) -> list[QualityFinding]:
    """Findings-only view of :func:`measure_branch_protection`."""
    return measure_branch_protection(project_dir, _config).findings


# Every dimension the score is built from, in report order. Adding a check here
# is what puts it in the denominator; a check that is not listed is invisible to
# both the findings and the coverage, which is the failure mode this whole file
# now exists to make impossible.
#
# The uniform third parameter is what keeps this table trustworthy: every
# measurer takes a RepoContext whether or not it uses one, so a check that needs
# repo identity can be added without splitting the dispatch — and a check that
# splits the dispatch is a check that can fall out of the denominator.
_MEASURERS = (
    measure_project_yaml,
    measure_tests,
    measure_ci_cd,
    measure_security_headers,
    measure_dependencies,
    measure_branch_protection,
    measure_i18n,
)


def run_quality_checks_with_coverage(
    project_dir: str, config: ProjectConfig, repo: RepoContext | None = None,
) -> tuple[list[QualityFinding], QualityCoverage]:
    """Run all quality checks, returning findings *and* what they cover.

    The findings half keeps the contract :func:`run_quality_checks` has always
    had — same checks, same order, same weights. The coverage half is purely
    descriptive: it says which of those checks actually looked at anything.

    *repo* is optional. Without it the checks that must ask GitHub report
    ``tooling-unavailable``, which is what a local run or a test should see.
    """
    path = Path(project_dir)
    results = [measure(path, config, repo) for measure in _MEASURERS]

    findings = [f for result in results for f in result.findings]

    # Sort by priority. `list.sort` is stable, so findings of equal priority
    # keep the dimension order above — the order callers already depend on.
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda f: priority_order.get(f.priority, 9))

    return findings, QualityCoverage(results=results)


def run_quality_checks(
    project_dir: str, config: ProjectConfig, repo: RepoContext | None = None,
) -> list[QualityFinding]:
    """Run all quality checks on a project.

    Unchanged contract: priority-sorted findings, nothing else. Callers that
    want to know what the score covers should use
    :func:`run_quality_checks_with_coverage`.
    """
    findings, _coverage = run_quality_checks_with_coverage(project_dir, config, repo)
    return findings
