"""Tests for the dependency check — the one that has never fired in production.

`check_dependencies` shelled out to `npm audit`. The production Container Apps job
runs `image: 'python:3.12'` (`infrastructure/main.bicep:91`) and installs no node,
so every run raised `FileNotFoundError`, which the old code swallowed and returned
no findings for — indistinguishable downstream from a clean dependency tree. A
comment at `main.bicep:96` records someone debugging that as an OOM. On top of
that it was gated on `package.json`, so it never looked at a Python project at all.

Dependabot alerts replace it: every ecosystem, no local tooling, already enabled
fleet-wide. The whole risk of the rewrite is reintroducing the original disease in
a new costume — a 403, a timeout or an absent token quietly reading as "clean". So
the central test here is `TestNoFailureLooksClean`, which walks every failure mode
there is and asserts none of them produces a measured result.

Measured against the live fleet on 2026-08-26: 30 non-archived repos, 0 with open
critical or high alerts, so this produces no findings today. `samoletovs/.github`
answers 403 "Dependabot alerts are disabled for this repository." — the one real
degradation case, reproduced verbatim below.
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
from agent.main import evaluate_project, github_token
from agent.tools.quality_tools import (
    DEPENDABOT_DISABLED_ENV,
    DEPENDABOT_MAX_PAGES,
    DEPENDABOT_PAGE_SIZE,
    SKIP_TOOLING_UNAVAILABLE,
    DependabotUnavailable,
    RepoContext,
    fetch_dependabot_alerts,
    measure_dependencies,
)

REPO = RepoContext(slug="samoletovs/era", token="t0ken")


def _alert(severity: str) -> dict:
    return {"security_advisory": {"severity": severity}}


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        bad_json: bool = False,
        next_url: str | None = None,
        decode_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json
        self._decode_error = decode_error
        self.links = {"next": {"url": next_url, "rel": "next"}} if next_url else {}

    def json(self) -> Any:
        if self._decode_error:
            # What httpx raises for a body that is not valid UTF-8.
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        if self._bad_json:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._payload


class _FakeClient:
    """Stands in for ``httpx.Client`` as a context manager."""

    def __init__(self, responses: list[Any], error: Exception | None = None) -> None:
        self._responses = list(responses)
        self._error = error
        self.calls: list[dict] = []
        self.init_kwargs: dict = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, params: dict | None = None) -> Any:
        self.calls.append({"url": url, "params": params or {}})
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)

def _patch_client(client: _FakeClient):
    def factory(**kwargs: Any) -> _FakeClient:
        client.init_kwargs = kwargs
        return client

    return patch("agent.tools.quality_tools.httpx.Client", side_effect=factory)


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(
        yaml.dump({"name": root.name, "purpose": "p", "users": "u", "stage": "active"}),
        encoding="utf-8",
    )
    return root


def _config(project: Path) -> ProjectConfig:
    return ProjectConfig.from_yaml(project / "project.yaml")


# ── fetch_dependabot_alerts ────────────────────────────────────────────────


class TestFetch:
    def test_returns_the_alerts_on_a_clean_read(self) -> None:
        client = _FakeClient([_FakeResponse(200, [_alert("critical")])])
        with _patch_client(client):
            assert fetch_dependabot_alerts("o/r", "tok") == [_alert("critical")]

    def test_asks_only_for_open_critical_and_high(self) -> None:
        client = _FakeClient([_FakeResponse(200, [])])
        with _patch_client(client):
            fetch_dependabot_alerts("o/r", "tok")

        params = client.calls[0]["params"]
        assert params["state"] == "open"
        assert params["severity"] == "critical,high"
        assert params["per_page"] == DEPENDABOT_PAGE_SIZE
        assert client.calls[0]["url"].endswith("/repos/o/r/dependabot/alerts")

    def test_never_sends_a_page_parameter(self) -> None:
        """The live endpoint rejects it outright:

            HTTP 400 — Pagination using the `page` parameter is not supported.

        It paginates by cursor. Sending `page` made the check dead on every repo,
        and no mocked response could have shown that — this test exists because
        running it against the real API did.
        """
        client = _FakeClient([_FakeResponse(200, [])])
        with _patch_client(client):
            fetch_dependabot_alerts("o/r", "tok")

        assert "page" not in client.calls[0]["params"]

    def test_authenticates_and_pins_the_api_version(self) -> None:
        client = _FakeClient([_FakeResponse(200, [])])
        with _patch_client(client):
            fetch_dependabot_alerts("o/r", "tok")

        headers = client.init_kwargs["headers"]
        assert headers["Authorization"] == "token tok"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert client.init_kwargs["timeout"] > 0

    def test_follows_the_link_header_cursor(self) -> None:
        """Stopping at the first page would undercount — the same quiet wrongness."""
        cursor = "https://api.github.com/repos/o/r/dependabot/alerts?after=CURSOR"
        client = _FakeClient([
            _FakeResponse(200, [_alert("high")], next_url=cursor),
            _FakeResponse(200, [_alert("critical")]),
        ])
        with _patch_client(client):
            alerts = fetch_dependabot_alerts("o/r", "tok")

        assert len(alerts) == 2
        assert client.calls[1]["url"] == cursor
        # The cursor carries its own query string; resending the originals would
        # overwrite it and re-fetch page one forever.
        assert client.calls[1]["params"] == {}

    def test_pagination_exhaustion_is_a_failure_not_a_short_answer(self) -> None:
        """A cursor that never terminates means the read is incomplete.

        Returning what was collected would report a partial count as a complete
        one — the exact shape this check exists to remove. If `severity` were ever
        ignored server-side, low-severity alerts would fill the cap and the
        critical ones beyond it would vanish into a measured-clean dimension.
        """
        cursor = "https://api.github.com/next"
        pages = [
            _FakeResponse(200, [_alert("moderate")] * DEPENDABOT_PAGE_SIZE, next_url=cursor)
            for _ in range(DEPENDABOT_MAX_PAGES + 5)
        ]
        client = _FakeClient(pages)
        with _patch_client(client), pytest.raises(DependabotUnavailable) as exc:
            fetch_dependabot_alerts("o/r", "tok")

        assert len(client.calls) == DEPENDABOT_MAX_PAGES, "must stop, not spin"
        assert "incomplete" in str(exc.value)

    def test_a_non_utf8_body_raises_rather_than_escaping(self) -> None:
        """`resp.json()` decodes bytes; invalid UTF-8 raises UnicodeDecodeError.

        That is a ValueError but neither a JSON error nor an httpx one, so a
        narrow handler lets it escape — and an escape here costs the project its
        whole evaluation, not just this dimension.
        """
        client = _FakeClient([_FakeResponse(200, None, decode_error=True)])
        with _patch_client(client), pytest.raises(DependabotUnavailable) as exc:
            fetch_dependabot_alerts("o/r", "tok")
        assert "UnicodeDecodeError" in str(exc.value)

    def test_an_invalid_url_raises_rather_than_escaping(self) -> None:
        """httpx.InvalidURL is not an httpx.HTTPError, and a bad cursor reaches it."""
        client = _FakeClient([], error=httpx.InvalidURL("no host"))
        with _patch_client(client), pytest.raises(DependabotUnavailable):
            fetch_dependabot_alerts("o/r", "tok")

    def test_disabled_repository_raises_with_githubs_own_words(self) -> None:
        """The live case: samoletovs/.github answers exactly this."""
        client = _FakeClient([
            _FakeResponse(
                403, {"message": "Dependabot alerts are disabled for this repository."}
            )
        ])
        with _patch_client(client), pytest.raises(DependabotUnavailable) as exc:
            fetch_dependabot_alerts("samoletovs/.github", "tok")

        assert "403" in str(exc.value)
        assert "disabled for this repository" in str(exc.value)

    @pytest.mark.parametrize("status", [401, 403, 404, 422, 500, 502])
    def test_any_non_200_raises(self, status: int) -> None:
        client = _FakeClient([_FakeResponse(status, {"message": "nope"})])
        with _patch_client(client), pytest.raises(DependabotUnavailable):
            fetch_dependabot_alerts("o/r", "tok")

    def test_a_non_list_body_raises(self) -> None:
        client = _FakeClient([_FakeResponse(200, {"message": "surprise"})])
        with _patch_client(client), pytest.raises(DependabotUnavailable):
            fetch_dependabot_alerts("o/r", "tok")

    def test_malformed_json_raises(self) -> None:
        client = _FakeClient([_FakeResponse(200, None, bad_json=True)])
        with _patch_client(client), pytest.raises(DependabotUnavailable):
            fetch_dependabot_alerts("o/r", "tok")

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused"),
         httpx.ReadTimeout("slow")],
    )
    def test_transport_errors_raise(self, error: Exception) -> None:
        client = _FakeClient([], error=error)
        with _patch_client(client), pytest.raises(DependabotUnavailable):
            fetch_dependabot_alerts("o/r", "tok")

    def test_an_unreadable_error_body_still_produces_a_status(self) -> None:
        client = _FakeClient([_FakeResponse(500, None, bad_json=True)])
        with _patch_client(client), pytest.raises(DependabotUnavailable) as exc:
            fetch_dependabot_alerts("o/r", "tok")
        assert "HTTP 500" in str(exc.value)


# ── The disease must not come back ─────────────────────────────────────────


class TestNoFailureLooksClean:
    """Every way this can fail, asserted to be unmeasured rather than clean.

    This is the whole point of the rewrite. The old check returned ``[]`` when npm
    was missing, and ``[]`` is what a healthy project returns too.
    """

    def _run(self, tmp_path: Path, **kwargs: Any):
        project = _project(tmp_path)
        return measure_dependencies(project, _config(project), **kwargs)

    @pytest.mark.parametrize(
        "repo",
        [
            None,
            RepoContext(),
            RepoContext(slug="o/r"),           # no token
            RepoContext(token="tok"),          # no slug
            RepoContext(slug="", token=""),
        ],
    )
    def test_missing_identity_is_unmeasured(self, tmp_path: Path, repo) -> None:
        result = self._run(tmp_path, repo=repo)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert result.findings == []

    @pytest.mark.parametrize(
        "reason",
        ["HTTP 403 — Dependabot alerts are disabled for this repository.",
         "HTTP 404", "ConnectTimeout: timed out", "malformed JSON"],
    )
    def test_every_fetch_failure_is_unmeasured(self, tmp_path: Path, reason: str) -> None:
        with patch(
            "agent.tools.quality_tools.fetch_dependabot_alerts",
            side_effect=DependabotUnavailable(reason),
        ):
            result = self._run(tmp_path, repo=REPO)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert result.findings == []
        assert reason in result.detail  # the diagnosis survives to the report

    def test_the_kill_switch_is_unmeasured_not_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEPENDABOT_DISABLED_ENV, "1")

        with patch("agent.tools.quality_tools.httpx.Client") as client:
            result = self._run(tmp_path, repo=REPO)

        assert client.call_count == 0, "kill switch must prevent the network call"
        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE

    def test_the_kill_switch_is_off_unless_set_to_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEPENDABOT_DISABLED_ENV, "0")

        with patch("agent.tools.quality_tools.fetch_dependabot_alerts", return_value=[]):
            result = self._run(tmp_path, repo=REPO)

        assert result.measured is True

    def test_no_network_call_happens_without_a_token(self, tmp_path: Path) -> None:
        """Cheap as well as honest: a tokenless run makes no request at all."""
        with patch("agent.tools.quality_tools.httpx.Client") as client:
            self._run(tmp_path, repo=RepoContext(slug="o/r"))
        assert client.call_count == 0

    @pytest.mark.parametrize(
        "error",
        [
            UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid"),
            RuntimeError("something nobody foresaw"),
            TypeError("an httpx internal changed shape"),
        ],
    )
    def test_an_unforeseen_exception_costs_one_dimension_not_the_report(
        self, tmp_path: Path, error: Exception
    ) -> None:
        """This is the only check that leaves the machine.

        An exception escaping here propagates through evaluate_project and the
        project loses its *entire* evaluation — every other dimension with it.
        Losing one dimension is the honest failure; losing the report is not.
        """
        with patch(
            "agent.tools.quality_tools.fetch_dependabot_alerts", side_effect=error
        ):
            result = self._run(tmp_path, repo=REPO)

        assert result.measured is False
        assert result.skip_reason == SKIP_TOOLING_UNAVAILABLE
        assert result.findings == []
        assert type(error).__name__ in result.detail

    def test_an_unforeseen_exception_leaves_the_rest_of_the_report_intact(
        self, tmp_path: Path
    ) -> None:
        """The consequence of the above, asserted end to end."""
        project = _project(tmp_path)
        with patch(
            "agent.tools.quality_tools.fetch_dependabot_alerts",
            side_effect=RuntimeError("boom"),
        ):
            report = evaluate_project(project, _config(project), REPO)

        assert report["coverage"]["total"] == 7
        assert "metadata" in [
            d["dimension"] for d in report["coverage"]["dimensions"] if d["measured"]
        ]


# ── Findings ───────────────────────────────────────────────────────────────


class TestFindings:
    def _measure(self, tmp_path: Path, alerts: list[dict]):
        project = _project(tmp_path)
        with patch("agent.tools.quality_tools.fetch_dependabot_alerts", return_value=alerts):
            return measure_dependencies(project, _config(project), REPO)

    def test_no_alerts_is_measured_and_clean(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, [])
        assert result.measured is True
        assert result.findings == []

    def test_critical_alerts_are_p0_worth_20(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, [_alert("critical"), _alert("critical")])

        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.category == "deps"
        assert finding.priority == "P0"
        assert finding.weight == 20
        assert "2 open Dependabot alert(s) at critical severity" in finding.description

    def test_high_alerts_are_p1_worth_10(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, [_alert("high")])

        assert result.findings[0].priority == "P1"
        assert result.findings[0].weight == 10

    def test_critical_supersedes_high_rather_than_stacking(self, tmp_path: Path) -> None:
        """The old check reported one or the other; the weights depend on it."""
        result = self._measure(tmp_path, [_alert("critical"), _alert("high")])

        assert len(result.findings) == 1
        assert result.findings[0].priority == "P0"

    def test_lower_severities_are_ignored(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, [_alert("moderate"), _alert("low")])
        assert result.findings == []
        assert result.measured is True

    @pytest.mark.parametrize(
        "alerts",
        [
            ["not a dict"],
            [{}],
            [{"security_advisory": None}],
            [{"security_advisory": "critical"}],
            [{"security_advisory": {}}],
            [{"security_advisory": {"severity": None}}],
        ],
    )
    def test_a_surprising_payload_does_not_crash_the_sweep(
        self, tmp_path: Path, alerts: list
    ) -> None:
        """One odd alert must not cost the other 24 projects their evaluation."""
        result = self._measure(tmp_path, alerts)
        assert result.measured is True
        assert result.findings == []

    def test_severity_is_recounted_locally_not_trusted(self, tmp_path: Path) -> None:
        """If the server ignored `severity`, the local count still governs."""
        result = self._measure(tmp_path, [_alert("LOW"), _alert("Critical")])
        assert result.findings[0].priority == "P0"
        assert "1 open" in result.findings[0].description

    def test_dependency_alerts_are_not_advisory(self, tmp_path: Path) -> None:
        """A coding agent *can* bump a dependency, so these must reach the planner."""
        result = self._measure(tmp_path, [_alert("critical")])
        assert result.findings[0].advisory is False


# ── Plumbing ───────────────────────────────────────────────────────────────


class TestRepoIdentityReachesTheCheck:
    def test_evaluate_project_passes_the_repo_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        seen: dict = {}

        def fake_fetch(slug: str, token: str) -> list[dict]:
            seen["slug"], seen["token"] = slug, token
            return [_alert("critical")]

        monkeypatch.setattr("agent.tools.quality_tools.fetch_dependabot_alerts", fake_fetch)
        report = evaluate_project(project, _config(project), REPO)

        assert seen == {"slug": "samoletovs/era", "token": "t0ken"}
        assert report["score"] == 100 - 5 - 10 - 20  # no goals, no similar, critical deps
        assert any(f["category"] == "deps" for f in report["findings"])

    def test_evaluate_project_without_a_repo_still_works(self, tmp_path: Path) -> None:
        """A local evaluation has no GitHub identity and must not require one."""
        project = _project(tmp_path)
        report = evaluate_project(project, _config(project))

        deps = [
            d for d in report["coverage"]["dimensions"] if d["dimension"] == "deps"
        ]
        assert deps[0]["measured"] is False
        assert deps[0]["skip_reason"] == SKIP_TOOLING_UNAVAILABLE


class TestRepoContext:
    @pytest.mark.parametrize(
        "repo,expected",
        [
            (RepoContext(slug="o/r", token="t"), True),
            (RepoContext(slug="o/r"), False),
            (RepoContext(token="t"), False),
            (RepoContext(), False),
            (RepoContext(slug="", token="t"), False),
        ],
    )
    def test_can_query_github(self, repo: RepoContext, expected: bool) -> None:
        assert repo.can_query_github is expected


class TestGithubToken:
    def test_prefers_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "pat")
        monkeypatch.setenv("GITHUB_TOKEN", "actions")
        assert github_token() == "pat"

    def test_falls_back_to_github_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "actions")
        assert github_token() == "actions"

    def test_empty_when_neither_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert github_token() == ""
