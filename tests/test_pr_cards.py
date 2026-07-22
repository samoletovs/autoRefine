"""Unit tests for agent.pr_cards (the PR approval-card sweep)."""
from __future__ import annotations

import pytest

from agent import pr_cards


# --- _checks_green ----------------------------------------------------------


def test_empty_rollup_is_green() -> None:
    # A repo with no CI has nothing to wait for.
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
    }


def test_sweep_cards_only_eligible_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    prs = [
        _pr(1),                                     # eligible: copilot, ready, green, not carded
        _pr(2, draft=True),                          # draft → skip
        _pr(3, login="samoletovs"),                  # human → skip
        _pr(4, labels=[{"name": "pr-card-sent"}]),   # already carded → skip
        _pr(5, rollup=[{"status": "IN_PROGRESS"}]),  # CI not green → skip
    ]
    monkeypatch.setattr(pr_cards, "_list_open_prs", lambda repo: prs)
    carded: list[int] = []
    monkeypatch.setattr(
        pr_cards, "send_pr_card", lambda repo, num, title, **kw: carded.append(num) or True
    )
    monkeypatch.setattr(pr_cards, "_mark_carded", lambda repo, num: None)

    count = pr_cards.sweep_pr_cards(["samoletovs/era"])
    assert count == 1
    assert carded == [1]


def test_sweep_dry_run_counts_but_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_cards, "_list_open_prs", lambda repo: [_pr(1)])
    sent: list[int] = []
    monkeypatch.setattr(pr_cards, "send_pr_card", lambda *a, **k: sent.append(1) or True)
    monkeypatch.setattr(pr_cards, "_mark_carded", lambda repo, num: None)

    count = pr_cards.sweep_pr_cards(["samoletovs/era"], dry_run=True)
    assert count == 1
    assert sent == []


def test_sweep_marks_carded_after_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_cards, "_list_open_prs", lambda repo: [_pr(7)])
    monkeypatch.setattr(pr_cards, "send_pr_card", lambda *a, **k: True)
    marked: list[tuple[str, int]] = []
    monkeypatch.setattr(pr_cards, "_mark_carded", lambda repo, num: marked.append((repo, num)))

    pr_cards.sweep_pr_cards(["samoletovs/era"])
    assert marked == [("samoletovs/era", 7)]
