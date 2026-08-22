"""Tests for file-ideas mode helpers in agent.main."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from agent import main


def test_map_improvement_type() -> None:
    assert main._map_improvement_type("security") == "bugfix"
    assert main._map_improvement_type("quality") == "refactor"


def test_build_run_references_includes_commit_and_workflow_url(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_RUN_ID", "987")
    monkeypatch.setenv("GITHUB_REPOSITORY", "samoletovs/autoRefine")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")

    refs = main._build_run_references()

    assert "commit: `abc123`" in refs
    assert "https://github.com/samoletovs/autoRefine/actions/runs/987" in refs


def test_build_file_idea_command_uses_supported_options(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "_discover_file_idea_options",
        lambda _path: {
            "--repo",
            "--title",
            "--source",
            "--type",
            "--problem",
            "--approach",
            "--references",
            "--body",
            "--dry-run",
        },
    )

    cmd = main._build_file_idea_command(
        script_path=Path("/tmp/file-idea.py"),
        repo="samoletovs/era",
        improvement={
            "title": "Add tests",
            "description": "No tests exist for critical logic.",
            "priority": "P0",
            "category": "tests",
        },
        references="- commit: `abc`",
        dry_run=True,
    )

    assert "--repo" in cmd
    assert "samoletovs/era" in cmd
    assert "--source" in cmd
    assert "autorefine" in cmd
    assert "--type" in cmd
    assert "refactor" in cmd
    assert "--references" in cmd
    assert "--dry-run" in cmd


def _spec(title: str, priority: str, category: str) -> dict:
    """An improvement specified well enough to be filed.

    `approach` and `success_criteria` are required now: autoRefine used to invent
    them from the title so validation would pass, which put 81 unbuildable ideas
    into the approval queue.
    """
    return {
        "title": title,
        "priority": priority,
        "category": category,
        "approach": f"Edit src/{category}.ts and cover the new branch in tests/.",
        "success_criteria": "npm test exits 0 with 12 passing specs, up from 10.",
    }


def test_file_ideas_for_plan_filters_and_deduplicates(monkeypatch) -> None:
    monkeypatch.setattr(main, "_resolve_file_idea_script", lambda: Path("/tmp/fake-file-idea.py"))
    monkeypatch.setattr(main, "_build_run_references", lambda: "- commit: `abc`")
    monkeypatch.setattr(
        main,
        "_build_file_idea_command",
        lambda script_path, repo, improvement, references, dry_run: [
            "python",
            str(script_path),
            improvement["title"],
        ],
    )

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], *args, **kwargs) -> SimpleNamespace:
        # file_ideas_for_plan now probes open ideas first, to catch near-duplicates
        # across runs. That probe is not a filing call, so keep it out of `calls`.
        if cmd and cmd[0] == "gh":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", _fake_run)

    filed = main.file_ideas_for_plan(
        repo="samoletovs/era",
        plan={
            "improvements": [
                _spec("Critical fix", "P0", "security"),
                _spec("Critical fix", "P0", "security"),
                _spec("Important cleanup", "P1", "quality"),
                _spec("Later cleanup", "P2", "quality"),
            ],
        },
    )

    assert filed == 2
    assert len(calls) == 2
    assert calls[0][-1] == "Critical fix"
    assert calls[1][-1] == "Important cleanup"


def test_file_ideas_for_plan_skips_unspecified_improvements(monkeypatch) -> None:
    """An improvement with no real approach/criteria is dropped, not dressed up.

    This is the regression that mattered: the old code synthesized
    "Implement '<title>' (P0)" and "'<title>' is implemented and usable as
    described", which satisfied file-idea.py's schema check while telling the
    builder nothing.
    """
    monkeypatch.setattr(main, "_resolve_file_idea_script", lambda: Path("/tmp/fake-file-idea.py"))
    monkeypatch.setattr(main, "_build_run_references", lambda: "- commit: `abc`")
    monkeypatch.setattr(
        main,
        "_build_file_idea_command",
        lambda script_path, repo, improvement, references, dry_run: [
            "python", str(script_path), improvement["title"],
        ],
    )
    calls: list[list[str]] = []
    def _fake_run(cmd, *args, **kwargs):
        # file_ideas_for_plan now probes open ideas first, to catch near-duplicates
        # across runs. That probe is not a filing call, so keep it out of `calls`.
        if cmd and cmd[0] == "gh":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", _fake_run)

    filed = main.file_ideas_for_plan(
        repo="samoletovs/era",
        plan={
            "improvements": [
                # No approach or criteria at all.
                {"title": "Increase Test Coverage", "priority": "P0", "category": "quality"},
                # Present, but only restates the title — the old synthesized form.
                {
                    "title": "Add heartbeat system",
                    "priority": "P0",
                    "category": "quality",
                    "approach": "Implement 'Add heartbeat system' (P0).",
                    "success_criteria": (
                        "'Add heartbeat system' is implemented and usable as described, "
                        "with no regression to existing flows."
                    ),
                },
                _spec("Fix the retry backoff", "P0", "quality"),
            ],
        },
    )

    assert filed == 1
    assert [c[-1] for c in calls] == ["Fix the retry backoff"]


def test_is_specified_accepts_a_real_memo_and_rejects_filler() -> None:
    assert main.is_specified({
        "title": "Add unit tests for foundry_agent.py",
        "approach": "Add tests/test_foundry_agent.py covering build_plan_task and submit_plan.",
        "success_criteria": "pytest -q reports >=60 passing tests, up from 40.",
    })
    assert not main.is_specified({
        "title": "Increase Test Coverage",
        "approach": "Implement 'Increase Test Coverage' (P0).",
        "success_criteria": "'Increase Test Coverage' is implemented and usable as described.",
    })
    assert not main.is_specified({"title": "Increase Test Coverage"})


def test_all_unspecified_is_logged_as_an_error_not_silence(monkeypatch, caplog) -> None:
    """A model that stopped answering must not look like a healthy quiet project."""
    monkeypatch.setattr(main, "_resolve_file_idea_script", lambda: Path("/tmp/fake-file-idea.py"))
    monkeypatch.setattr(main, "_build_run_references", lambda: "- commit: `abc`")

    with caplog.at_level(logging.ERROR):
        filed = main.file_ideas_for_plan(
            repo="samoletovs/era",
            plan={
                "improvements": [
                    {"title": "Increase Test Coverage", "priority": "P0", "category": "quality"},
                    {"title": "Enhance Documentation", "priority": "P0", "category": "docs"},
                ],
            },
        )

    assert filed == 0
    assert any("not supplying" in record.getMessage() for record in caplog.records)


# ── Cross-run duplicate detection ───────────────────────────────────────────────
# Added 2026-08-22. autoRefine deduplicated only WITHIN a single plan (`seen_titles`,
# exact match), so the same idea reappeared on every run until somebody closed it by
# hand. The 2026-08-15 triage of the approval queue found 6 duplicate pairs among 81
# issues, and foundryLab#7 was closed with: "Closing as a duplicate of #5 ... The
# proposer does not currently check open issues for near-duplicates before filing."
#
# Each duplicate that reaches approval buys a second 10-30 minute Copilot run for
# work already in flight.


def test_verbatim_repeat_is_caught():
    assert main._is_near_duplicate("Adaptive Learning Paths", ["Adaptive Learning Paths"])


def test_reworded_repeat_is_caught():
    """The real failure mode - the proposer rarely repeats a title verbatim."""
    assert main._is_near_duplicate(
        "Enhance Educational Features", ["Enhanced Educational Features"])


def test_the_real_foundrylab_pair_is_caught():
    """foundryLab#7 vs #5, the pair a human had to close by hand."""
    assert main._is_near_duplicate(
        "Enhance README documentation", ["Enhance onboarding documentation"])


def test_distinct_ideas_are_not_caught():
    """A check that flags everything is a check somebody turns off."""
    assert main._is_near_duplicate(
        "Adaptive Learning Paths", ["Fix the Cosmos connection timeout"]) is None
    assert main._is_near_duplicate(
        "Add rate limiting to the public API", ["Improve the onboarding email copy"]) is None


def test_a_near_miss_is_accepted_as_the_cheap_error():
    """Documents a KNOWN false positive rather than hiding it.

    "Improve invoice recognition" and "Improve receipt recognition" share exactly
    one meaningful word of two - the same lexical distance as the real foundryLab
    pair a human closed as a duplicate. No threshold separates them, so the
    threshold is set to make the cheap error: this is flagged, logged with its
    match, and a human can override. The expensive error would be buying a second
    Copilot run for work already queued.
    """
    assert main._is_near_duplicate(
        "Improve invoice recognition", ["Improve receipt recognition"]) is not None


def test_filler_alone_is_not_similarity():
    """Two titles sharing only 'add'/'the'/'implement' are not duplicates."""
    assert main._is_near_duplicate(
        "Add the invoice parser", ["Add the payment webhook"]) is None


def test_the_match_is_returned_so_it_can_be_named():
    # "skipped, duplicates X" is actionable; a bare "skipped" invites someone to
    # disable the check.
    m = main._is_near_duplicate("Enhanced Educational Features", ["Educational Features Enhancement"])
    assert m == "Educational Features Enhancement"


def test_empty_and_filler_only_titles_do_not_match_everything():
    assert main._is_near_duplicate("", ["Anything"]) is None
    assert main._is_near_duplicate("Implement the feature", [""]) is None


def test_open_idea_titles_fails_open(monkeypatch):
    """A GitHub hiccup must not stop the run filing legitimate ideas. The cost of
    failing open is one duplicate; failing closed silently halts the proposer."""
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(main.subprocess, "run", boom)
    assert main._open_idea_titles("samoletovs/era") == []


def test_open_idea_titles_returns_titles(monkeypatch):
    class R:
        returncode = 0
        stdout = "Adaptive Learning Paths\nEnhanced Invoice Recognition\n\n"
        stderr = ""
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: R())
    assert main._open_idea_titles("samoletovs/era") == [
        "Adaptive Learning Paths", "Enhanced Invoice Recognition"]


def test_file_ideas_for_plan_actually_skips_an_already_open_duplicate(monkeypatch, tmp_path):
    """The wiring test, not the helper test.

    _is_near_duplicate can be perfect and still change nothing if the filing path
    never calls it - which is the failure this repo keeps repeating (a Copilot
    assignment that assigned nobody, a leak installer that installed nothing, a
    budget guard that guarded nothing). Disabling the call in file_ideas_for_plan
    must fail THIS test.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "gh":
            # One idea is already open, worded slightly differently.
            return SimpleNamespace(
                returncode=0, stdout="Enhanced Educational Features\n", stderr="")
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    script = tmp_path / "file-idea.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(main, "_resolve_file_idea_script", lambda: script)
    monkeypatch.setattr(main, "_build_run_references", lambda: "- commit: `abc`")
    monkeypatch.setattr(main, "_discover_file_idea_options", lambda p: set())
    monkeypatch.setattr(main.subprocess, "run", _fake_run)

    filed = main.file_ideas_for_plan(
        repo="samoletovs/era",
        plan={"improvements": [
            {   # duplicates the open issue - must be skipped
                "title": "Enhance Educational Features",
                "priority": "P1", "category": "feature",
                "approach": "Add a per-topic recall table and weight lesson choice by it.",
                "success_criteria": "A learner below 70% on topic X sees X in the next 5 lessons.",
            },
            {   # genuinely new - must be filed
                "title": "Retry the Cosmos connection on 429",
                "priority": "P1", "category": "bugfix",
                "approach": "Wrap the Cosmos client in an exponential backoff on 429 responses.",
                "success_criteria": "A simulated 429 is retried three times before surfacing an error.",
            },
        ]},
        dry_run=False,
    )

    assert filed == 1, "the duplicate must not be filed"
    titles = [c[-1] for c in calls]
    assert not any("Educational" in t for t in titles), \
        "file_ideas_for_plan did not consult the open-issue list"
