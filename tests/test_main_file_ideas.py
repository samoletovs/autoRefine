"""Tests for file-ideas mode helpers in agent.main."""

from __future__ import annotations

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
                {"title": "Critical fix", "priority": "P0", "category": "security"},
                {"title": "Critical fix", "priority": "P0", "category": "security"},
                {"title": "Important cleanup", "priority": "P1", "category": "quality"},
                {"title": "Later cleanup", "priority": "P2", "category": "quality"},
            ],
        },
    )

    assert filed == 2
    assert len(calls) == 2
    assert calls[0][-1] == "Critical fix"
    assert calls[1][-1] == "Important cleanup"
