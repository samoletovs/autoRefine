"""Unit tests for ``agent.tools.github_tools``.

These functions are the repo's write path to GitHub: they create the branch,
make the commit and open the PR that autoRefine's refine mode produces. Every
existing test patches them out, so none of them had been executed before.

The tests run against a real, throwaway git repository created in ``tmp_path``
— fast, hermetic, and it means assertions are about what git actually does
rather than what we assume it does. Only the two operations that would reach
the network are mocked: ``git push`` and the ``gh`` CLI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.tools import github_tools

# Captured before any patching, so fakes can still shell out to real git.
_REAL_RUN = subprocess.run


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _REAL_RUN(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit on ``main`` and no remote."""
    _REAL_RUN(
        ["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True
    )
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "autoRefine Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial commit")
    return tmp_path


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _log_subjects(repo: Path) -> list[str]:
    out = _git(repo, "log", "--format=%s").stdout
    return [line for line in out.splitlines() if line]


def _offline_run(recorder: list[Sequence[str]]) -> Callable[..., Any]:
    """A ``subprocess.run`` stand-in that runs git for real but never pushes."""

    def fake(argv: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        recorder.append(list(argv))
        if "push" in argv:
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return _REAL_RUN(argv, *args, **kwargs)

    return fake


# ── create_branch ──────────────────────────────────────────────────────────
def test_create_branch_switches_to_a_fresh_branch(repo: Path) -> None:
    assert github_tools.create_branch(repo, "autorefine/improve-2026-05-17") is True
    assert _current_branch(repo) == "autorefine/improve-2026-05-17"


def test_create_branch_returns_false_when_branch_exists(repo: Path) -> None:
    """git refuses ``checkout -b`` onto an existing ref, so we get False.

    ``run_refine`` relies on this: it retries with a ``-2`` suffix when the
    first name is taken.
    """
    assert github_tools.create_branch(repo, "already-there") is True
    _git(repo, "checkout", "main")

    assert github_tools.create_branch(repo, "already-there") is False
    assert _current_branch(repo) == "main"


def test_create_branch_returns_false_for_an_invalid_name(repo: Path) -> None:
    assert github_tools.create_branch(repo, "has spaces") is False
    assert _current_branch(repo) == "main"


# ── commit_and_push ────────────────────────────────────────────────────────
def test_commit_and_push_returns_false_with_nothing_to_commit(repo: Path) -> None:
    """The guard that stops autoRefine opening empty PRs.

    Also asserts we never reach the network: an empty commit must not push.
    """
    github_tools.create_branch(repo, "feature")
    calls: list[Sequence[str]] = []

    with patch.object(subprocess, "run", _offline_run(calls)):
        assert github_tools.commit_and_push(repo, "nothing here", "feature") is False

    assert not any("push" in call for call in calls)
    assert _log_subjects(repo) == ["initial commit"]


def test_commit_and_push_commits_and_pushes_changes(repo: Path) -> None:
    github_tools.create_branch(repo, "feature")
    (repo / "README.md").write_text("# fixture\nchanged\n", encoding="utf-8")
    calls: list[Sequence[str]] = []

    with patch.object(subprocess, "run", _offline_run(calls)):
        assert github_tools.commit_and_push(repo, "feat: change it", "feature") is True

    assert _log_subjects(repo) == ["feat: change it", "initial commit"]
    assert ["git", "push", "-u", "origin", "feature"] in calls


def test_commit_and_push_pushes_the_branch_it_was_given(repo: Path) -> None:
    """The branch argument, not the checked-out branch, decides the refspec."""
    github_tools.create_branch(repo, "actually-checked-out")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    calls: list[Sequence[str]] = []

    with patch.object(subprocess, "run", _offline_run(calls)):
        github_tools.commit_and_push(repo, "feat: x", "the-name-passed-in")

    push = next(call for call in calls if "push" in call)
    assert push == ["git", "push", "-u", "origin", "the-name-passed-in"]


def test_commit_and_push_returns_false_when_push_fails(repo: Path) -> None:
    """A rejected push is a failure even though the commit landed locally."""
    github_tools.create_branch(repo, "feature")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")

    def fake(argv: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        if "push" in argv:
            return subprocess.CompletedProcess(list(argv), 1, "", "rejected")
        return _REAL_RUN(argv, *args, **kwargs)

    with patch.object(subprocess, "run", fake):
        assert github_tools.commit_and_push(repo, "feat: x", "feature") is False

    assert _log_subjects(repo)[0] == "feat: x"


def test_commit_and_push_stages_untracked_files(repo: Path) -> None:
    """KNOWN HAZARD: ``git add -A`` sweeps in anything the agent left behind.

    Nothing here is scoped to the files the refine run edited, so an untracked
    ``.env`` written into the workspace is committed and pushed along with the
    change. Only ``.gitignore`` stands between a stray secret and a public PR
    (see ``test_commit_and_push_respects_gitignore``).
    """
    github_tools.create_branch(repo, "feature")
    (repo / "README.md").write_text("# fixture\nedited\n", encoding="utf-8")
    (repo / ".env").write_text("AZURE_OPENAI_KEY=super-secret\n", encoding="utf-8")
    calls: list[Sequence[str]] = []

    with patch.object(subprocess, "run", _offline_run(calls)):
        assert github_tools.commit_and_push(repo, "feat: x", "feature") is True

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert ".env" in committed


def test_commit_and_push_respects_gitignore(repo: Path) -> None:
    """The mitigation for the hazard above: an ignored file is not staged."""
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "chore: ignore env")
    github_tools.create_branch(repo, "feature")
    (repo / "README.md").write_text("# fixture\nedited\n", encoding="utf-8")
    (repo / ".env").write_text("AZURE_OPENAI_KEY=super-secret\n", encoding="utf-8")

    with patch.object(subprocess, "run", _offline_run([])):
        assert github_tools.commit_and_push(repo, "feat: x", "feature") is True

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert ".env" not in committed
    assert "README.md" in committed


# ── read_file_safe ─────────────────────────────────────────────────────────
def test_read_file_safe_truncates_at_max_lines(tmp_path: Path) -> None:
    target = tmp_path / "long.txt"
    target.write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")

    result = github_tools.read_file_safe(target, max_lines=10)

    assert result.splitlines() == [str(i) for i in range(10)]


def test_read_file_safe_defaults_to_200_lines(tmp_path: Path) -> None:
    target = tmp_path / "long.txt"
    target.write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")

    assert len(github_tools.read_file_safe(target).splitlines()) == 200


def test_read_file_safe_returns_whole_file_when_shorter(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    assert github_tools.read_file_safe(target, max_lines=10) == "a\nb"


def test_read_file_safe_returns_empty_string_for_missing_file(tmp_path: Path) -> None:
    assert github_tools.read_file_safe(tmp_path / "nope.txt") == ""


def test_read_file_safe_returns_empty_string_for_a_directory(tmp_path: Path) -> None:
    assert github_tools.read_file_safe(tmp_path) == ""


def test_read_file_safe_ignores_undecodable_bytes(tmp_path: Path) -> None:
    target = tmp_path / "binary.bin"
    target.write_bytes(b"ok\n\xff\xfe\nmore\n")

    assert "ok" in github_tools.read_file_safe(target)


# ── read_project_yaml ──────────────────────────────────────────────────────
def test_read_project_yaml_returns_none_when_absent(tmp_path: Path) -> None:
    assert github_tools.read_project_yaml(tmp_path) is None


def test_read_project_yaml_parses_a_valid_card(tmp_path: Path) -> None:
    (tmp_path / "project.yaml").write_text(
        "name: era\npurpose: test things\nusers: devs\nstage: mvp\ngoals:\n  - ship\n",
        encoding="utf-8",
    )

    config = github_tools.read_project_yaml(tmp_path)

    assert config is not None
    assert config.name == "era"
    assert config.goals == ["ship"]


def test_read_project_yaml_returns_none_on_malformed_yaml(tmp_path: Path) -> None:
    """One unparseable card must cost that card, not the whole run."""
    (tmp_path / "project.yaml").write_text("name: era\n  bad: [unclosed\n", encoding="utf-8")

    assert github_tools.read_project_yaml(tmp_path) is None


def test_read_project_yaml_returns_none_when_yaml_is_not_a_mapping(
    tmp_path: Path,
) -> None:
    """Valid YAML that parses to a scalar has no ``.get`` — must not raise."""
    (tmp_path / "project.yaml").write_text("just a string\n", encoding="utf-8")

    assert github_tools.read_project_yaml(tmp_path) is None


# ── create_pr ──────────────────────────────────────────────────────────────
def _gh_result(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], code, "https://github.invalid/pr/1", "")


def test_create_pr_passes_the_requested_base_branch(tmp_path: Path) -> None:
    """The base must be whatever the caller resolved — this repo is ``master``."""
    with patch.object(subprocess, "run", return_value=_gh_result(0)) as run:
        assert (
            github_tools.create_pr(
                tmp_path,
                "samoletovs/autoRefine",
                title="feat: x",
                body="why",
                branch="autorefine/improve",
                base="master",
            )
            is True
        )

    argv = run.call_args_list[0].args[0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert argv[argv.index("--base") + 1] == "master"
    assert argv[argv.index("--repo") + 1] == "samoletovs/autoRefine"
    assert argv[argv.index("--head") + 1] == "autorefine/improve"
    assert argv[argv.index("--title") + 1] == "feat: x"
    assert argv[argv.index("--body") + 1] == "why"
    assert run.call_args_list[0].kwargs["cwd"] == str(tmp_path)


def test_create_pr_requires_an_explicit_base(tmp_path: Path) -> None:
    """No default base: this repo's default branch is ``master``, not ``main``.

    A silent ``main`` default would open PRs against a branch that does not
    exist here. Omitting it is now a TypeError at the call site instead.
    """
    with (
        patch.object(subprocess, "run", return_value=_gh_result(0)),
        pytest.raises(TypeError),
    ):
        github_tools.create_pr(  # type: ignore[call-arg]
            tmp_path, "samoletovs/autoRefine", title="t", body="b", branch="br"
        )


def test_create_pr_enables_auto_merge_on_success(tmp_path: Path) -> None:
    with patch.object(subprocess, "run", return_value=_gh_result(0)) as run:
        assert (
            github_tools.create_pr(
                tmp_path, "o/r", title="t", body="b", branch="br", base="master"
            )
            is True
        )

    assert run.call_count == 2
    merge_argv = run.call_args_list[1].args[0]
    assert merge_argv[:3] == ["gh", "pr", "merge"]
    assert "--squash" in merge_argv
    assert "--auto" in merge_argv


def test_create_pr_returns_false_when_gh_fails(tmp_path: Path) -> None:
    """A failed ``gh pr create`` must not go on to try auto-merge."""
    with patch.object(subprocess, "run", return_value=_gh_result(1)) as run:
        assert (
            github_tools.create_pr(
                tmp_path, "o/r", title="t", body="b", branch="br", base="master"
            )
            is False
        )

    assert run.call_count == 1


def test_create_pr_succeeds_even_if_auto_merge_fails(tmp_path: Path) -> None:
    """Auto-merge is best-effort; a branch-protection refusal must not lose the PR."""
    results = [_gh_result(0), _gh_result(1)]

    with patch.object(subprocess, "run", side_effect=results):
        assert (
            github_tools.create_pr(
                tmp_path, "o/r", title="t", body="b", branch="br", base="master"
            )
            is True
        )


# ── clone_repo ─────────────────────────────────────────────────────────────
def test_clone_repo_pulls_when_the_directory_exists(tmp_path: Path) -> None:
    existing = tmp_path / "era"
    existing.mkdir()
    run = MagicMock(return_value=subprocess.CompletedProcess(["git"], 0, "", ""))

    with patch.object(subprocess, "run", run):
        assert github_tools.clone_repo("samoletovs/era", existing) is True

    assert run.call_args.args[0] == ["git", "pull", "--rebase"]


def test_clone_repo_clones_when_the_directory_is_absent(tmp_path: Path) -> None:
    target = tmp_path / "era"
    run = MagicMock(return_value=subprocess.CompletedProcess(["gh"], 0, "", ""))

    with patch.object(subprocess, "run", run):
        assert github_tools.clone_repo("samoletovs/era", target) is True

    assert run.call_args.args[0] == ["gh", "repo", "clone", "samoletovs/era", str(target)]


def test_clone_repo_returns_false_on_failure(tmp_path: Path) -> None:
    run = MagicMock(return_value=subprocess.CompletedProcess(["gh"], 1, "", "boom"))

    with patch.object(subprocess, "run", run):
        assert github_tools.clone_repo("samoletovs/era", tmp_path / "era") is False
