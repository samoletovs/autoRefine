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

    def _fake_run(cmd: list[str], capture_output: bool, text: bool) -> SimpleNamespace:
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
    monkeypatch.setattr(
        main.subprocess, "run",
        lambda cmd, capture_output, text: (
            calls.append(cmd), SimpleNamespace(returncode=0, stdout="", stderr="")
        )[1],
    )

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
