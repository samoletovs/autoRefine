"""Feeding PR outcomes back into ideation — `_abandoned_after_build`.

The existing loop (`_recent_declined_reasons`) learns only from a 👎 on a Telegram card,
which costs one tap. These cover the other end of the funnel: an idea that was approved,
ran a 10-30 minute Copilot Coding Agent, opened a PR, and had that PR closed unmerged.

Fixtures are the real API shapes, captured from `gh api graphql` on 2026-08-27, not
invented ones. `era#1 -> PR #2` is the case that motivated the whole change; the payload
below is what the live query returned for it, with the issue state moved to closed because
that is the state in which the lesson applies (see
``test_an_idea_still_open_is_a_retry_request_not_a_lesson``).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from agent import main

# The owner's real closing comment on samoletovs/era#2, verbatim from the API (1,615
# characters there; trimmed here to the part that carries the lesson). It is the only
# rich failure reason the lab has ever produced, and it lives on a PR — which is exactly
# why reading issues alone could never find it.
ERA_PR2_CLOSING_COMMENT = (
    "Closing: this PR contains **no changes at all**.\n\n"
    "```\nchanged_files: 0    additions: 0    deletions: 0\n"
    'commits:       1    ("Initial plan")\n```\n\n'
    "`git compare main...copilot/add-enhanced-invoice-recognition` returns `files: 0`. "
    "The branch is one empty planning commit ahead of `main`.\n\n"
    "The description says otherwise — it details `accountCode` extraction, a fallback to "
    "`6350`, and upload-boundary schemas, complete with a TypeScript snippet. None of "
    "that code exists in the branch."
)


def _issue(
    title: str = "[idea] Enhanced Invoice Recognition",
    labels: tuple[str, ...] = (
        "approved", "area:frontend", "area:shared", "priority:medium",
        "idea", "feature", "autopilot", "build-notified",
    ),
    prs: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """One `issues.nodes[]` entry in the shape the live GraphQL query returns."""
    if prs is None:
        prs = (_pr(merged=False, comments=((("OWNER", ERA_PR2_CLOSING_COMMENT)),)),)
    return {
        "title": title,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "closedByPullRequestsReferences": {"nodes": list(prs)},
    }


def _pr(*, merged: bool, comments: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
    return {
        "merged": merged,
        "comments": {
            "nodes": [
                {"authorAssociation": association, "body": body}
                for association, body in comments
            ]
        },
    }


def _payload(*issues: dict[str, Any]) -> str:
    return json.dumps({"data": {"repository": {"issues": {"nodes": list(issues)}}}})


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch):
    """Stub `gh`, recording argv. Returns a setter for the stdout the next call sees."""
    state: dict[str, Any] = {"stdout": _payload(), "returncode": 0, "stderr": ""}
    calls: list[list[str]] = []

    def _fake_run(cmd, *args: Any, **kwargs: Any):
        calls.append(list(cmd))
        if isinstance(state["stdout"], BaseException):
            raise state["stdout"]
        return SimpleNamespace(
            returncode=state["returncode"], stdout=state["stdout"], stderr=state["stderr"]
        )

    monkeypatch.setattr(main.subprocess, "run", _fake_run)
    monkeypatch.delenv("AUTOREFINE_SKIP_PR_OUTCOMES", raising=False)
    return SimpleNamespace(state=state, calls=calls)


# ── The case that motivated this ─────────────────────────────────────────────


def test_an_abandoned_build_reaches_the_generator_with_its_reason(gh) -> None:
    """era#1: approved, built, PR #2 closed unmerged. Title *and* why."""
    gh.state["stdout"] = _payload(_issue())

    entries = main._abandoned_after_build("samoletovs/era")

    assert len(entries) == 1
    title, _, reason = entries[0].partition(" — ")
    assert title == "Enhanced Invoice Recognition"
    assert "no changes at all" in reason


def test_the_reason_is_capped_because_it_rides_the_prompt_every_run(gh) -> None:
    """A Telegram decline is a phrase; a human post-mortem is an essay.

    era#2's real comment is 1,615 characters. Uncapped, that is ~400 tokens added to
    every planning run for one entry, against a bill that is already 97% input tokens.
    """
    gh.state["stdout"] = _payload(_issue())

    reason = main._abandoned_after_build("samoletovs/era")[0].split(" — ", 1)[1]

    assert len(reason) <= main.AVOID_REASON_MAX_CHARS + 1  # +1 for the ellipsis
    assert reason.endswith("…")
    assert "\n" not in reason, "markdown fences are whitespace we would be charged for"


def test_entry_count_is_capped(gh) -> None:
    gh.state["stdout"] = _payload(*[_issue(title=f"[idea] Number {n}") for n in range(20)])

    assert len(main._abandoned_after_build("samoletovs/era")) == main.ABANDONED_CONTEXT_CAP


# ── What must NOT be fed back ────────────────────────────────────────────────


def test_a_merged_pr_is_a_success_not_a_lesson(gh) -> None:
    gh.state["stdout"] = _payload(
        _issue(prs=(_pr(merged=True, comments=(("OWNER", "Nice work, shipping."),)),))
    )

    assert main._abandoned_after_build("samoletovs/era") == []


def test_an_idea_with_no_pr_at_all_is_not_an_abandoned_build(gh) -> None:
    """Closed with nothing built is a decline, and `_recent_declined_reasons` owns it."""
    gh.state["stdout"] = _payload(_issue(prs=()))

    assert main._abandoned_after_build("samoletovs/era") == []


@pytest.mark.parametrize("label", sorted(main.SUPERSEDED_FAILURE_LABELS))
def test_a_defect_we_already_fixed_teaches_the_model_nothing(gh, label: str) -> None:
    """`unbuildable-memo` and `duplicate` are what is_specified() and _is_near_duplicate
    now prevent up front. Measured 2026-08-27: 20 of the fleet's 23 closed idea issues
    carry one. Replaying them would suppress ideas that file perfectly well today, and
    charge tokens for the privilege of re-punishing a fixed bug.
    """
    gh.state["stdout"] = _payload(_issue(labels=("idea", "autopilot", label)))

    assert main._abandoned_after_build("samoletovs/era") == []


def test_an_idea_still_open_is_a_retry_request_not_a_lesson(gh) -> None:
    """The exclusion that keeps this from inverting its one real example.

    era#2 was closed unmerged and the human wrote "#1 stays open and is ready to be
    picked up again" — the idea survived, only the build failed. Suppressing it would
    contradict them outright, and `_is_near_duplicate` already stops a re-proposal while
    the issue is open. GitHub does this filtering, so the query is what must carry it.
    """
    gh.state["stdout"] = _payload(_issue())
    main._abandoned_after_build("samoletovs/era")

    query = next(arg for arg in gh.calls[0] if arg.startswith("query="))
    assert "states: [CLOSED]" in query


def test_the_query_asks_for_closed_prs_or_it_can_never_see_one(gh) -> None:
    """`closedByPullRequestsReferences` defaults to merged PRs only.

    Verified against era#1 on 2026-08-27: `gh issue view --json
    closedByPullRequestsReferences` returns [] — it does not pass this argument — while
    the raw query returns PR #2 with `merged: false`. Dropping it makes the whole check
    dead on every repo while every test that mocks the response still passes, which is
    why this asserts on the query text rather than on a fixture.
    """
    gh.state["stdout"] = _payload(_issue())
    main._abandoned_after_build("samoletovs/era")

    query = next(arg for arg in gh.calls[0] if arg.startswith("query="))
    assert "includeClosedPrs: true" in query


def test_only_a_humans_words_are_replayed(gh) -> None:
    """A bot's status line costs tokens and teaches nothing."""
    gh.state["stdout"] = _payload(
        _issue(prs=(_pr(merged=False, comments=(
            ("OWNER", "Wrong approach — we already do this in the ingest worker."),
            ("NONE", "🤖 PR janitor: waiting on you for 19h."),
        )),))
    )

    reason = main._abandoned_after_build("samoletovs/era")[0].split(" — ", 1)[1]

    assert reason.startswith("Wrong approach")
    assert "janitor" not in reason


def test_a_bot_only_thread_still_yields_the_title(gh) -> None:
    """"Closed" with no human word teaches little, but that it was tried and thrown
    away is itself the lesson — so the entry survives without a reason."""
    gh.state["stdout"] = _payload(
        _issue(prs=(_pr(merged=False, comments=(("NONE", "Superseded by automation."),)),))
    )

    assert main._abandoned_after_build("samoletovs/era") == ["Enhanced Invoice Recognition"]


# ── Fail open ────────────────────────────────────────────────────────────────


class TestNeverBlocksIdeation:
    """Every failure returns [] — a GitHub hiccup must not stop the proposer.

    `[]` is also what "nothing to avoid" returns, which is unavoidable here: unlike a
    scored check, an empty avoid-block is a legitimate prompt. What is not acceptable is
    the two being indistinguishable to an operator, so failure logs at WARNING and
    emptiness at DEBUG.
    """

    @pytest.mark.parametrize(
        "stdout, returncode",
        [
            ("", 1),                                  # gh non-zero (403, rate limit)
            ("not json at all", 0),                    # malformed body
            ('{"data": {"repository": null}}', 0),     # repo 404s: GraphQL 200 + null
            ('{"errors": [{"message": "x"}]}', 0),     # GraphQL error, no data
            ("{}", 0),
            ("", 0),
        ],
    )
    def test_a_bad_response_is_empty_not_an_exception(self, gh, stdout, returncode) -> None:
        gh.state["stdout"] = stdout
        gh.state["returncode"] = returncode

        assert main._abandoned_after_build("samoletovs/era") == []

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("gh: command not found"),
            subprocess.TimeoutExpired(cmd="gh", timeout=60),
            subprocess.SubprocessError("boom"),
        ],
    )
    def test_a_raising_gh_is_empty(self, gh, exc: BaseException) -> None:
        gh.state["stdout"] = exc

        assert main._abandoned_after_build("samoletovs/era") == []

    def test_a_malformed_slug_never_reaches_the_network(self, gh) -> None:
        assert main._abandoned_after_build("not-a-slug") == []
        assert gh.calls == []

    def test_the_kill_switch_stops_the_call_entirely(
        self, gh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new fleet-wide network call in the ideation hot path needs an off switch,
        for the same reason AUTOREFINE_SKIP_DEPENDABOT does."""
        monkeypatch.setenv("AUTOREFINE_SKIP_PR_OUTCOMES", "1")
        gh.state["stdout"] = _payload(_issue())

        assert main._abandoned_after_build("samoletovs/era") == []
        assert gh.calls == []

    def test_failure_and_emptiness_are_told_apart_in_the_log(
        self, gh, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="autorefine"):
            gh.state["stdout"] = _payload()          # a real, empty answer
            main._abandoned_after_build("samoletovs/era")
            assert not [r for r in caplog.records if r.levelname == "WARNING"]

            caplog.clear()
            gh.state["returncode"] = 1               # could not tell
            main._abandoned_after_build("samoletovs/era")
            assert [r for r in caplog.records if r.levelname == "WARNING"]


# ── Rendering ────────────────────────────────────────────────────────────────


def test_declined_and_abandoned_are_separate_blocks() -> None:
    """They are not the same lesson.

    A declined idea was refused on its face, so the answer is to propose something
    different in kind. An abandoned one passed the human filter and died in the build —
    the area was wanted. One shared header cannot say both.
    """
    block = main._format_avoid_context(
        ["CSV export — too niche"],
        ["Enhanced Invoice Recognition — the PR contained no changes at all"],
    )

    assert "DECLINED" in block
    assert "THROWN AWAY" in block
    assert block.index("DECLINED") < block.index("THROWN AWAY")
    assert "- CSV export — too niche" in block
    assert "- Enhanced Invoice Recognition — the PR contained no changes at all" in block


def test_either_block_stands_alone() -> None:
    assert "THROWN AWAY" not in main._format_avoid_context(["Dark mode"])
    assert "DECLINED" not in main._format_avoid_context([], ["Something — why"])
    assert main._format_avoid_context([], []) == ""
    assert main._format_avoid_context([]) == ""


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_the_sweep_actually_asks_for_pr_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring test, not the helper test.

    `_abandoned_after_build` can be perfect and change nothing if the one place that
    builds the prompt never calls it — the failure this repo keeps repeating. Removing
    the call from the cards branch of `_process_repo` must fail THIS test.
    """
    import inspect

    source = inspect.getsource(main._process_repo)
    assert "_abandoned_after_build(repo)" in source, (
        "the cards branch no longer feeds PR outcomes into _format_avoid_context"
    )
    # ...and it must stay behind the same cards gate as the declined reasons, so a
    # `propose`-mode sweep does not start paying for a call it will not use.
    cards_branch = source.split("_format_avoid_context", 1)[1].split("else", 1)[0]
    assert "_recent_declined_reasons(repo)" in cards_branch
    assert "_abandoned_after_build(repo)" in cards_branch
