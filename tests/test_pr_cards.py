"""Unit tests for agent.pr_cards (the PR approval-card sweep)."""
from __future__ import annotations

import json
import subprocess

import pytest

from agent import pr_cards

# --- Real fixtures ----------------------------------------------------------
#
# Captured live on 2026-08-28, not invented. Both are Copilot PRs that the pre-fix sweep
# would have carded as "CI is green" while no CI had run at all.
#
#   gh pr list --repo samoletovs/atlas --state open --limit 50 \
#     --json number,title,isDraft,author,labels,url,statusCheckRollup,headRefOid
#   gh api 'repos/samoletovs/atlas/actions/runs?head_sha=b5e0a4ac...&per_page=100'

ATLAS_4_SHA = "b5e0a4ac1641e205a1eaa9f2986edfec7d88d7fe"

ATLAS_4_PR = {
    "author": {"is_bot": True, "login": "app/copilot-swe-agent"},
    "headRefOid": ATLAS_4_SHA,
    "isDraft": False,
    "labels": [],
    "number": 4,
    "statusCheckRollup": [],
    "title": "Extract adaptive-learning scoring logic into a testable module",
    "url": "https://github.com/samoletovs/atlas/pull/4",
}

# All three have zero jobs — verified via /actions/runs/{id}/jobs — which is precisely why
# they contribute nothing to statusCheckRollup.
ATLAS_4_RUNS = [
    {"id": 32633411771, "name": "Azure Static Web Apps CI/CD", "event": "pull_request",
     "status": "completed", "conclusion": "action_required"},
    {"id": 32633411813, "name": "Auto-merge Dependabot PRs", "event": "pull_request",
     "status": "completed", "conclusion": "action_required"},
    {"id": 32633406941, "name": "Azure Static Web Apps CI/CD", "event": "pull_request",
     "status": "completed", "conclusion": "action_required"},
]

# samoletovs/portaBaltica#181 @ 805f6aea — the same empty rollup, but with three genuinely
# failed runs alongside the gated one. All four have total_count: 0 jobs.
PORTABALTICA_181_RUNS = [
    {"id": 33147970314, "name": "CI/CD", "event": "pull_request",
     "status": "completed", "conclusion": "action_required"},
    {"id": 33147958199, "name": "Auto-merge Dependabot PRs", "event": "pull_request",
     "status": "completed", "conclusion": "failure"},
    {"id": 33147958203, "name": "Auto-merge Copilot PRs", "event": "pull_request",
     "status": "completed", "conclusion": "failure"},
    {"id": 33147958201, "name": "CI/CD", "event": "pull_request",
     "status": "completed", "conclusion": "failure"},
]

# samoletovs/era#2 — empty rollup and one successful run (Copilot's own `dynamic` session
# run, which attaches no check run to the PR). This is the honest "nothing to wait for"
# case and must stay cardable.
ERA_2_RUNS = [
    {"id": 32591557074, "name": "Running Copilot cloud agent", "event": "dynamic",
     "status": "completed", "conclusion": "success"},
]


# --- _checks_green ----------------------------------------------------------


def test_empty_rollup_is_green() -> None:
    # Still True in isolation — but callers must not read it as evidence; see
    # test_blocked_pr_is_not_carded_as_green.
    assert pr_cards._checks_green([]) is True


def test_all_success_is_green() -> None:
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "state": "SUCCESS"},
    ]
    assert pr_cards._checks_green(rollup) is True


def test_running_check_is_not_green() -> None:
    assert pr_cards._checks_green([{"status": "IN_PROGRESS", "conclusion": ""}]) is False


def test_failed_conclusion_is_not_green() -> None:
    assert pr_cards._checks_green([{"status": "COMPLETED", "conclusion": "FAILURE"}]) is False


def test_pending_status_state_is_not_green() -> None:
    assert pr_cards._checks_green([{"state": "PENDING"}]) is False


def test_neutral_and_skipped_count_as_green() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert pr_cards._checks_green(rollup) is True


# --- _runs_verdict ----------------------------------------------------------


def test_no_runs_at_all_is_green() -> None:
    # samoletovs/nauroLabs-github#217 shape: empty rollup, zero runs. Genuinely no CI.
    assert pr_cards._runs_verdict([]) == (pr_cards.GREEN, [])


def test_action_required_runs_are_blocked() -> None:
    verdict, names = pr_cards._runs_verdict(ATLAS_4_RUNS)
    assert verdict == pr_cards.BLOCKED
    assert sorted(set(names)) == ["Auto-merge Dependabot PRs", "Azure Static Web Apps CI/CD"]


def test_blocked_wins_over_failure() -> None:
    verdict, names = pr_cards._runs_verdict(PORTABALTICA_181_RUNS)
    assert verdict == pr_cards.BLOCKED
    assert "CI/CD" in names


def test_successful_invisible_run_stays_green() -> None:
    assert pr_cards._runs_verdict(ERA_2_RUNS) == (pr_cards.GREEN, [])


def test_failed_invisible_run_is_not_green() -> None:
    runs = [r for r in PORTABALTICA_181_RUNS if r["conclusion"] == "failure"]
    assert pr_cards._runs_verdict(runs) == (pr_cards.NOT_GREEN, [])


def test_unfinished_run_is_not_green() -> None:
    assert pr_cards._runs_verdict(
        [{"name": "CI", "status": "in_progress", "conclusion": None}]
    ) == (pr_cards.NOT_GREEN, [])


def test_none_is_unknown_not_green() -> None:
    # The whole point: "could not tell" must never collapse into "fine".
    assert pr_cards._runs_verdict(None) == (pr_cards.UNKNOWN, [])


# --- _workflow_runs: no failure may look clean ------------------------------


class TestNoFailureLooksClean:
    """Every way of failing must return None, never [] — [] is the real answer 'no runs'."""

    def test_kill_switch_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTOREFINE_SKIP_RUN_CHECK", "1")
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    @pytest.mark.parametrize(("repo", "sha"), [("", ATLAS_4_SHA), ("samoletovs/atlas", "")])
    def test_missing_repo_or_sha_returns_none(
        self, repo: str, sha: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
        assert pr_cards._workflow_runs(repo, sha) is None

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
        monkeypatch.setattr(
            pr_cards.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "HTTP 403"),
        )
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    def test_malformed_body_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
        monkeypatch.setattr(
            pr_cards.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "not json", ""),
        )
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    def test_missing_workflow_runs_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
        monkeypatch.setattr(
            pr_cards.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"total_count": 0}', ""),
        )
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)

        def _boom(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired("gh", 60)

        monkeypatch.setattr(pr_cards.subprocess, "run", _boom)
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    def test_gh_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)

        def _boom(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(pr_cards.subprocess, "run", _boom)
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) is None

    def test_empty_list_is_a_real_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
        monkeypatch.setattr(
            pr_cards.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a, 0, '{"total_count": 0, "workflow_runs": []}', ""
            ),
        )
        assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) == []


def test_workflow_runs_queries_the_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL must carry the head SHA — a repo-wide query would read someone else's CI."""
    seen: list[list[str]] = []

    def _fake(cmd: list[str], **_k: object) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"workflow_runs": ATLAS_4_RUNS}), ""
        )

    monkeypatch.delenv("AUTOREFINE_SKIP_RUN_CHECK", raising=False)
    monkeypatch.setattr(pr_cards.subprocess, "run", _fake)
    assert pr_cards._workflow_runs("samoletovs/atlas", ATLAS_4_SHA) == ATLAS_4_RUNS
    assert seen[0][:2] == ["gh", "api"]
    assert f"head_sha={ATLAS_4_SHA}" in seen[0][2]
    assert "repos/samoletovs/atlas/actions/runs" in seen[0][2]


# --- _is_copilot / _already_carded -----------------------------------------


@pytest.mark.parametrize(
    "login", ["Copilot", "copilot-swe-agent[bot]", "app/copilot-swe-agent", "github-copilot[bot]"]
)
def test_is_copilot_matches_agent_logins(login: str) -> None:
    assert pr_cards._is_copilot({"author": {"login": login}}) is True


def test_is_copilot_rejects_humans() -> None:
    assert pr_cards._is_copilot({"author": {"login": "samoletovs"}}) is False


def test_already_carded_detects_label() -> None:
    pr = {"labels": [{"name": "pr-card-sent"}, {"name": "idea"}]}
    assert pr_cards._already_carded(pr) is True


def test_already_carded_false_when_absent() -> None:
    assert pr_cards._already_carded({"labels": [{"name": "idea"}]}) is False


def test_blocked_label_is_distinct_from_carded_label() -> None:
    # A nudged PR must still be eligible for a real card once a human unblocks it.
    nudged = {"labels": [{"name": pr_cards.PR_BLOCKED_CARD_SENT_LABEL}]}
    assert pr_cards._already_nudged(nudged) is True
    assert pr_cards._already_carded(nudged) is False


# --- sweep_pr_cards ---------------------------------------------------------


def _pr(number, *, login="Copilot", draft=False, labels=None, rollup=None, title="T") -> dict:
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "author": {"login": login},
        "labels": labels or [],
        "url": f"https://github.com/samoletovs/era/pull/{number}",
        "statusCheckRollup": rollup if rollup is not None else [],
        "headRefOid": "0" * 40,
    }


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch):
    """Wire the sweep to in-memory doubles and record what it sent.

    ``raising=False`` on ``_workflow_runs`` is deliberate: with the source change reverted
    that attribute does not exist, and the test must then fail on the *behaviour* (a green
    card was sent for a blocked PR) rather than on a missing attribute.
    """
    sent: dict[str, list] = {"green": [], "blocked": [], "labels": []}

    def _install(prs: list[dict], runs: list[dict] | None) -> dict[str, list]:
        monkeypatch.setattr(pr_cards, "_list_open_prs", lambda repo: prs)
        monkeypatch.setattr(pr_cards, "_workflow_runs", lambda repo, sha: runs, raising=False)
        monkeypatch.setattr(
            pr_cards, "send_pr_card",
            lambda repo, num, title, **kw: sent["green"].append(num) or True,
        )
        monkeypatch.setattr(
            pr_cards, "send_pr_blocked_card",
            lambda repo, num, title, **kw: sent["blocked"].append((num, kw.get("workflows"))) or True,
            raising=False,
        )
        monkeypatch.setattr(
            pr_cards, "_mark_carded",
            lambda repo, num, label=pr_cards.PR_CARD_SENT_LABEL: sent["labels"].append((num, label)),
        )
        return sent

    return _install


def test_blocked_pr_is_not_carded_as_green(sweep) -> None:
    """THE REGRESSION TEST.

    samoletovs/atlas#4 verbatim: an empty rollup with three ``action_required`` runs. The
    pre-fix sweep read the empty rollup as green and sent "CI is green" for a PR whose CI
    had never started, where one 👍 squash-merges it.
    """
    sent = sweep([ATLAS_4_PR], ATLAS_4_RUNS)

    carded = pr_cards.sweep_pr_cards(["samoletovs/atlas"])

    assert sent["green"] == [], "sent a CI-is-green card for a PR whose CI never ran"
    assert carded == 0
    assert [n for n, _ in sent["blocked"]] == [4]
    assert sent["labels"] == [(4, pr_cards.PR_BLOCKED_CARD_SENT_LABEL)]


def test_blocked_card_names_the_waiting_workflows(sweep) -> None:
    sent = sweep([ATLAS_4_PR], ATLAS_4_RUNS)
    pr_cards.sweep_pr_cards(["samoletovs/atlas"])
    assert "Azure Static Web Apps CI/CD" in sent["blocked"][0][1]


def test_blocked_pr_is_nudged_only_once(sweep) -> None:
    already = {**ATLAS_4_PR, "labels": [{"name": pr_cards.PR_BLOCKED_CARD_SENT_LABEL}]}
    sent = sweep([already], ATLAS_4_RUNS)
    pr_cards.sweep_pr_cards(["samoletovs/atlas"])
    assert sent["blocked"] == []
    assert sent["green"] == []


def test_unknown_ci_state_cards_nothing(sweep) -> None:
    """Fail closed. A missed card is retried next sweep; a wrong card cannot be recalled."""
    sent = sweep([ATLAS_4_PR], None)
    assert pr_cards.sweep_pr_cards(["samoletovs/atlas"]) == 0
    assert sent["green"] == []
    assert sent["blocked"] == []


def test_repo_with_no_ci_is_still_carded(sweep) -> None:
    """The honest empty-rollup case must keep working — this fix must not silence the sweep."""
    sent = sweep([_pr(1)], [])
    assert pr_cards.sweep_pr_cards(["samoletovs/era"]) == 1
    assert sent["green"] == [1]


def test_invisible_successful_run_is_still_carded(sweep) -> None:
    sent = sweep([_pr(1)], ERA_2_RUNS)
    assert pr_cards.sweep_pr_cards(["samoletovs/era"]) == 1
    assert sent["green"] == [1]


def test_invisible_failed_run_is_not_carded(sweep) -> None:
    runs = [r for r in PORTABALTICA_181_RUNS if r["conclusion"] == "failure"]
    sent = sweep([_pr(1)], runs)
    assert pr_cards.sweep_pr_cards(["samoletovs/portaBaltica"]) == 0
    assert sent["green"] == []


def test_non_empty_rollup_skips_the_runs_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost guard: a PR with real checks must not pay for an extra API call."""
    calls: list[str] = []
    monkeypatch.setattr(
        pr_cards, "_list_open_prs",
        lambda repo: [_pr(1, rollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}])],
    )
    monkeypatch.setattr(
        pr_cards, "_workflow_runs",
        lambda repo, sha: calls.append(sha) or [], raising=False,
    )
    monkeypatch.setattr(pr_cards, "send_pr_card", lambda *a, **k: True)
    monkeypatch.setattr(pr_cards, "_mark_carded", lambda *a, **k: None)

    assert pr_cards.sweep_pr_cards(["samoletovs/era"]) == 1
    assert calls == []


def test_sweep_cards_only_eligible_prs(sweep) -> None:
    prs = [
        _pr(1),                                     # eligible: copilot, ready, green, not carded
        _pr(2, draft=True),                          # draft → skip
        _pr(3, login="samoletovs"),                  # human → skip
        _pr(4, labels=[{"name": "pr-card-sent"}]),   # already carded → skip
        _pr(5, rollup=[{"status": "IN_PROGRESS"}]),  # CI not green → skip
    ]
    sent = sweep(prs, [])

    count = pr_cards.sweep_pr_cards(["samoletovs/era"])
    assert count == 1
    assert sent["green"] == [1]


def test_sweep_dry_run_counts_but_sends_nothing(sweep) -> None:
    sent = sweep([_pr(1)], [])
    count = pr_cards.sweep_pr_cards(["samoletovs/era"], dry_run=True)
    assert count == 1
    assert sent["green"] == []


def test_sweep_dry_run_nudges_nothing(sweep) -> None:
    sent = sweep([ATLAS_4_PR], ATLAS_4_RUNS)
    assert pr_cards.sweep_pr_cards(["samoletovs/atlas"], dry_run=True) == 0
    assert sent["blocked"] == []
    assert sent["labels"] == []


def test_sweep_marks_carded_after_send(sweep) -> None:
    sent = sweep([_pr(7)], [])
    pr_cards.sweep_pr_cards(["samoletovs/era"])
    assert sent["labels"] == [(7, pr_cards.PR_CARD_SENT_LABEL)]


def test_list_open_prs_requests_the_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without headRefOid there is no SHA to ask the Actions API about."""
    seen: list[list[str]] = []

    def _fake(cmd: list[str], **_k: object) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(pr_cards.subprocess, "run", _fake)
    pr_cards._list_open_prs("samoletovs/atlas")
    assert "headRefOid" in seen[0][seen[0].index("--json") + 1]

