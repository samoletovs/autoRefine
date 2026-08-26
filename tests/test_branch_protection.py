"""Tests for the branch-protection check — the first advisory finding.

AGENTS.md rule 1 is "never push directly to main/master". Measured across the
fleet on 2026-08-26: **31 of 31 non-archived repositories have no classic branch
protection and zero rulesets**, so the rule is enforced by convention alone and a
direct push goes straight around the merge gate.

Enabling protection is a repository setting, not a commit. No pull request can
deliver it, so the finding is `advisory`: it scores and it reports, but it is
withheld from the planner. An idea filed from it would buy a 10-30 minute
coding-agent run guaranteed to produce nothing — 31 of them.

This is the first real producer of the flag added in #11, so
`TestAdvisoryInvariantEndToEnd` is the point where that mechanism stops being
inert and gets exercised against a finding a real check actually emits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import httpx
import pytest
import yaml

from agent.config import ProjectConfig
from agent.foundry_agent import build_plan_task
from agent.main import evaluate_project
from agent.tools.quality_tools import (
    BRANCH_PROTECTION_DISABLED_ENV,
    SKIP_NOT_APPLICABLE,
    SKIP_TOOLING_UNAVAILABLE,
    GitHubUnavailable,
    RepoContext,
    branch_is_protected,
    fetch_repo_facts,
    measure_branch_protection,
)

REPO = RepoContext(slug="samoletovs/era", token="t0ken")

REPO_URL = "/repos/samoletovs/era"
PROTECTION_URL = "/branches/main/protection"
RULES_URL = "/rules/branches/main"

REPO_OK = {"default_branch": "main", "archived": False}
NOT_PROTECTED = {"message": "Branch not protected"}


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.links: dict = {}

    def json(self) -> Any:
        return self._payload


class _Router:
    """Fake ``httpx.Client`` answering by URL fragment.

    Routing rather than replaying a sequence, because this check talks to three
    different endpoints and a positional fake would hide which one was called.
    """

    def __init__(self, routes: dict[str, _FakeResponse], error: Exception | None = None) -> None:
        self._routes = routes
        self._error = error
        self.urls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        # Exact suffix match. Substring matching made routing depend on dict
        # order ("/repos/owner/name" is inside every other URL), and ordering by
        # fragment length only swapped one wrong proxy for another — the repo URL
        # is longer than "/rules/branches/main". A suffix is unambiguous.
        for fragment, response in self._routes.items():
            if url.endswith(fragment):
                return response
        raise AssertionError(f"no route for {url}")


def _patch(router: _Router):
    return patch("agent.tools.quality_tools.httpx.Client", return_value=router)


def _unprotected_router() -> _Router:
    return _Router({
        PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
        RULES_URL: _FakeResponse(200, []),
        REPO_URL: _FakeResponse(200, REPO_OK),
    })


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(
        yaml.dump({
            "name": root.name, "purpose": "p", "users": "u", "stage": "active",
            "goals": ["g"], "similar": ["s"],
        }),
        encoding="utf-8",
    )
    return root


def _config(project: Path) -> ProjectConfig:
    return ProjectConfig.from_yaml(project / "project.yaml")


# ── fetch_repo_facts ───────────────────────────────────────────────────────


class TestRepoFacts:
    def test_reads_the_default_branch_and_archived_flag(self) -> None:
        router = _Router({REPO_URL: _FakeResponse(200, {"default_branch": "master", "archived": True})})
        with _patch(router):
            facts = fetch_repo_facts("samoletovs/era", "tok")

        assert facts.default_branch == "master"
        assert facts.archived is True

    def test_the_branch_is_read_not_assumed(self) -> None:
        """The fleet is split between `main` and `master`; assuming one is wrong."""
        for branch in ("main", "master"):
            router = _Router({REPO_URL: _FakeResponse(200, {"default_branch": branch})})
            with _patch(router):
                assert fetch_repo_facts("samoletovs/era", "tok").default_branch == branch

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_any_non_200_raises(self, status: int) -> None:
        router = _Router({REPO_URL: _FakeResponse(status, {"message": "no"})})
        with _patch(router), pytest.raises(GitHubUnavailable):
            fetch_repo_facts("samoletovs/era", "tok")

    def test_a_non_object_body_raises(self) -> None:
        router = _Router({REPO_URL: _FakeResponse(200, ["surprise"])})
        with _patch(router), pytest.raises(GitHubUnavailable):
            fetch_repo_facts("samoletovs/era", "tok")

    def test_a_missing_default_branch_is_empty_not_a_crash(self) -> None:
        router = _Router({REPO_URL: _FakeResponse(200, {})})
        with _patch(router):
            assert fetch_repo_facts("samoletovs/era", "tok").default_branch == ""

    def test_transport_errors_raise(self) -> None:
        router = _Router({}, error=httpx.ConnectTimeout("slow"))
        with _patch(router), pytest.raises(GitHubUnavailable):
            fetch_repo_facts("samoletovs/era", "tok")


# ── branch_is_protected ────────────────────────────────────────────────────


class TestBranchIsProtected:
    def test_classic_protection_counts(self) -> None:
        router = _Router({PROTECTION_URL: _FakeResponse(200, {"required_pull_request_reviews": {}})})
        with _patch(router):
            assert branch_is_protected("samoletovs/era", "main", "tok") is True

    def test_a_ruleset_counts_too(self) -> None:
        """The load-bearing case.

        Rulesets are the modern way to protect a branch. A check blind to them
        would keep reporting a repo that had just been fixed — and a finding that
        cannot clear is a permanent penalty, which AGENTS.md says never to weight.
        """
        router = _Router({
            PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
            RULES_URL: _FakeResponse(200, [{"type": "pull_request"}]),
        })
        with _patch(router):
            assert branch_is_protected("samoletovs/era", "main", "tok") is True

    def test_neither_means_unprotected(self) -> None:
        router = _Router({
            PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
            RULES_URL: _FakeResponse(200, []),
        })
        with _patch(router):
            assert branch_is_protected("samoletovs/era", "main", "tok") is False

    def test_a_404_that_is_not_about_protection_raises(self) -> None:
        """"Not Found" is not an answer about protection — it is a failure to look."""
        router = _Router({PROTECTION_URL: _FakeResponse(404, {"message": "Not Found"})})
        with _patch(router), pytest.raises(GitHubUnavailable):
            branch_is_protected("samoletovs/era", "main", "tok")

    def test_403_raises_rather_than_reading_as_unprotected(self) -> None:
        """Reading protection needs admin. Without it we did not look."""
        router = _Router({PROTECTION_URL: _FakeResponse(403, {"message": "Must have admin rights"})})
        with _patch(router), pytest.raises(GitHubUnavailable):
            branch_is_protected("samoletovs/era", "main", "tok")

    @pytest.mark.parametrize("payload,status", [(None, 500), ({"message": "x"}, 403)])
    def test_an_unreadable_ruleset_response_raises(self, payload: Any, status: int) -> None:
        router = _Router({
            PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
            RULES_URL: _FakeResponse(status, payload),
        })
        with _patch(router), pytest.raises(GitHubUnavailable):
            branch_is_protected("samoletovs/era", "main", "tok")

    def test_a_non_list_ruleset_response_raises(self) -> None:
        router = _Router({
            PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
            RULES_URL: _FakeResponse(200, {"unexpected": True}),
        })
        with _patch(router), pytest.raises(GitHubUnavailable):
            branch_is_protected("samoletovs/era", "main", "tok")


# ── The check ──────────────────────────────────────────────────────────────


class TestMeasureBranchProtection:
    def _measure(self, tmp_path: Path, router: _Router | None = None, **kwargs: Any):
        project = _project(tmp_path)
        repo = kwargs.pop("repo", REPO)
        if router is None:
            return measure_branch_protection(project, _config(project), repo)
        with _patch(router):
            return measure_branch_protection(project, _config(project), repo)

    def test_an_unprotected_branch_is_one_advisory_p1_finding(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, _unprotected_router())

        assert result.measured is True
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.category == "branch-protection"
        assert finding.priority == "P1"
        assert finding.weight == 10
        assert finding.advisory is True
        assert "main" in finding.description

    def test_a_protected_branch_is_measured_and_clean(self, tmp_path: Path) -> None:
        router = _Router({
            REPO_URL: _FakeResponse(200, REPO_OK),
            PROTECTION_URL: _FakeResponse(200, {}),
        })
        result = self._measure(tmp_path, router)

        assert result.measured is True
        assert result.findings == []

    def test_a_ruleset_clears_the_finding(self, tmp_path: Path) -> None:
        """Self-resolving in the modern way, not only the classic one."""
        router = _Router({
            REPO_URL: _FakeResponse(200, REPO_OK),
            PROTECTION_URL: _FakeResponse(404, NOT_PROTECTED),
            RULES_URL: _FakeResponse(200, [{"type": "pull_request"}]),
        })
        assert self._measure(tmp_path, router).findings == []

    def test_the_finding_names_the_actual_default_branch(self, tmp_path: Path) -> None:
        router = _Router({
            REPO_URL: _FakeResponse(200, {"default_branch": "master", "archived": False}),
            "/branches/master/protection": _FakeResponse(404, NOT_PROTECTED),
            "/rules/branches/master": _FakeResponse(200, []),
        })
        result = self._measure(tmp_path, router)
        assert "'master'" in result.findings[0].description


class TestNeverPenaliseWhatCannotBeBought:
    def _measure(self, tmp_path: Path, router: _Router):
        project = _project(tmp_path)
        with _patch(router):
            return measure_branch_protection(project, _config(project), REPO)

    def test_an_archived_repo_is_not_applicable(self, tmp_path: Path) -> None:
        """Its settings are frozen, so it cannot buy the fix."""
        router = _Router({REPO_URL: _FakeResponse(200, {"default_branch": "main", "archived": True})})
        result = self._measure(tmp_path, router)

        assert result.measured is False
        assert result.skip_reason == SKIP_NOT_APPLICABLE
        assert result.findings == []

    def test_a_repo_with_no_default_branch_is_not_applicable(self, tmp_path: Path) -> None:
        router = _Router({REPO_URL: _FakeResponse(200, {"default_branch": "", "archived": False})})
        result = self._measure(tmp_path, router)

        assert result.skip_reason == SKIP_NOT_APPLICABLE
        assert result.findings == []


class TestNoFailureLooksClean:
    """The same guarantee the dependency check carries, for the same reason."""

    def _measure(self, tmp_path: Path, **kwargs: Any):
        project = _project(tmp_path)
        return measure_branch_protection(project, _config(project), **kwargs)

    @pytest.mark.parametrize(
        "repo",
        [None, RepoContext(), RepoContext(slug="o/r"), RepoContext(token="tok")],
    )
    def test_missing_identity_is_unmeasured(self, tmp_path: Path, repo) -> None:
        result = self._measure(tmp_path, repo=repo)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert result.findings == []

    def test_the_kill_switch_makes_no_call_and_reports_unmeasured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(BRANCH_PROTECTION_DISABLED_ENV, "1")

        with patch("agent.tools.quality_tools.httpx.Client") as client:
            result = self._measure(tmp_path, repo=REPO)

        assert client.call_count == 0
        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE

    def test_an_unreadable_repo_is_unmeasured_not_unprotected(self, tmp_path: Path) -> None:
        """A 403 must never be reported as "no protection" — we did not look."""
        router = _Router({REPO_URL: _FakeResponse(403, {"message": "Forbidden"})})
        with _patch(router):
            result = self._measure(tmp_path, repo=REPO)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert result.findings == []

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("unforeseen"), TypeError("httpx changed shape"),
         UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid")],
    )
    def test_an_unforeseen_exception_costs_one_dimension_not_the_report(
        self, tmp_path: Path, error: Exception
    ) -> None:
        with patch("agent.tools.quality_tools.fetch_repo_facts", side_effect=error):
            result = self._measure(tmp_path, repo=REPO)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert type(error).__name__ in result.detail


# ── The advisory invariant, first live exercise ────────────────────────────


class TestAdvisoryInvariantEndToEnd:
    """#11 added the mechanism with no producer. This is the producer.

    The chain asserted here is the whole argument: a finding a real check emits,
    carried into the report and the score, and absent from the prompt that turns
    findings into filed GitHub issues.
    """

    def _report(self, tmp_path: Path) -> tuple[dict, ProjectConfig]:
        project = _project(tmp_path)
        config = _config(project)
        with _patch(_unprotected_router()):
            return evaluate_project(project, config, REPO), config

    def test_the_finding_reaches_the_report(self, tmp_path: Path) -> None:
        report, _ = self._report(tmp_path)

        protection = [
            f for f in report["findings"] if f["category"] == "branch-protection"
        ]
        assert len(protection) == 1
        assert protection[0]["advisory"] is True
        assert protection[0]["priority"] == "P1"

    def test_the_finding_costs_the_score(self, tmp_path: Path) -> None:
        """Weighted, per AGENTS.md: the drop is self-resolving, so it is earned."""
        report, _ = self._report(tmp_path)
        assert report["score"] == 90

    def test_the_finding_never_reaches_the_planner(self, tmp_path: Path) -> None:
        """The invariant #11 exists for, exercised against a real producer."""
        report, config = self._report(tmp_path)
        task = build_plan_task(report["findings"], config)

        assert "branch-protection" not in task
        assert "no protection rule" not in task
        assert "## Quality check findings" not in task

    def test_a_plannable_finding_alongside_it_still_gets_through(
        self, tmp_path: Path
    ) -> None:
        """Withholding must be surgical, not a blanket mute."""
        report, config = self._report(tmp_path)
        report["findings"].append(
            {
                "category": "tests",
                "description": "No test directory found",
                "priority": "P0",
                "advisory": False,
            }
        )
        task = build_plan_task(report["findings"], config)

        assert "No test directory found" in task
        assert "no protection rule" not in task

    def test_the_report_still_serialises(self, tmp_path: Path) -> None:
        report, _ = self._report(tmp_path)
        restored = json.loads(json.dumps(report, indent=2))
        assert restored["coverage"]["total"] == 7
