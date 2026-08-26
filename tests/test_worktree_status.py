"""Tests for the porcelain status parser in ``agent.main``.

``_worktree_status`` is the single parser behind both the rollback guarantee
and the success-path change list. It replaced two divergent ones:

* ``_status_path`` — ``line[3:]``, split on ``" -> "``, ``strip('"')``
* an inline ``line.strip().split(maxsplit=1)[-1]`` on the success path

Both parsed the *default* porcelain format, which quotes and C-escapes any
non-ASCII path. Everything here runs against a real git repository, because
the bugs being fixed are disagreements with git's actual output format rather
than logic errors.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent import main as agent_main

ACCENTED = "café.py"  # C-escaped by git as "caf\303\251.py" without -z


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


# ── the parser itself ──────────────────────────────────────────────────────
def test_clean_worktree_is_empty(repo: Path) -> None:
    assert agent_main._worktree_status(repo) == {}


def test_modified_and_untracked_are_reported(repo: Path) -> None:
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.py").write_text("x\n", encoding="utf-8")

    status = agent_main._worktree_status(repo)

    assert status == {"tracked.txt": " M", "new.py": "??"}


def test_staged_and_unstaged_share_one_path_entry(repo: Path) -> None:
    """The code changes when a file is staged; the path does not."""
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    assert agent_main._worktree_status(repo)["tracked.txt"] == " M"

    _git(repo, "add", "tracked.txt")

    assert agent_main._worktree_status(repo)["tracked.txt"] == "M "
    assert set(agent_main._worktree_status(repo)) == {"tracked.txt"}


# ── the non-ASCII regression: the case the old parser got wrong ────────────
def test_accented_path_is_returned_verbatim(repo: Path) -> None:
    """Without -z git emits ``?? "caf\\303\\251.py"``, which names no real file."""
    (repo / ACCENTED).write_text("y\n", encoding="utf-8")

    status = agent_main._worktree_status(repo)

    assert ACCENTED in status
    assert status[ACCENTED] == "??"
    assert "\\303" not in "".join(status)


def test_accented_path_round_trips_to_the_filesystem(repo: Path) -> None:
    """The parsed path must stat, or the rollback silently skips the file."""
    (repo / ACCENTED).write_text("y\n", encoding="utf-8")

    path = next(iter(agent_main._worktree_status(repo)))

    assert (repo / path).is_file()


def test_accented_path_round_trips_back_to_git(repo: Path) -> None:
    """The parsed path must also be a pathspec git accepts.

    The old parser produced ``caf\\303\\251.py``, and
    ``git checkout -- 'caf\\303\\251.py'`` exits 128 with "did not match any
    files" — which is why the rollback could not restore such a file.
    """
    (repo / ACCENTED).write_text("y\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add accented")
    (repo / ACCENTED).write_text("modified\n", encoding="utf-8")

    path = next(iter(agent_main._worktree_status(repo)))
    result = subprocess.run(
        ["git", "checkout", "--", path],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / ACCENTED).read_text(encoding="utf-8") == "y\n"


def test_path_with_spaces_is_unquoted(repo: Path) -> None:
    (repo / "with space.py").write_text("y\n", encoding="utf-8")

    status = agent_main._worktree_status(repo)

    assert "with space.py" in status
    assert '"with space.py"' not in status


# ── the rename regression: -z splits the record across two fields ──────────
def test_rename_reports_the_new_path_only(repo: Path) -> None:
    """Under -z a rename is ``R  new\\0old\\0``, so the origin field must be consumed.

    The old success-path parser did ``split(maxsplit=1)[-1]`` on
    ``R  old.py -> new.py`` and produced the literal ``"old.py -> new.py"``.
    """
    (repo / "old.py").write_text("content worth detecting as a rename\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add old")
    _git(repo, "mv", "old.py", "new.py")

    status = agent_main._worktree_status(repo)

    assert "new.py" in status
    assert status["new.py"].startswith("R")
    assert "old.py" not in status  # not a phantom entry from the origin field
    assert not any("->" in p for p in status)


def test_rename_does_not_swallow_the_following_entry(repo: Path) -> None:
    """Consuming two fields must not skip an unrelated file reported after it."""
    (repo / "old.py").write_text("content worth detecting as a rename\n", encoding="utf-8")
    (repo / "zzz_other.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add both")
    _git(repo, "mv", "old.py", "new.py")
    (repo / "zzz_other.txt").write_text("second\n", encoding="utf-8")

    status = agent_main._worktree_status(repo)

    assert "new.py" in status
    assert status["zzz_other.txt"] == " M"


# ── the properties the rollback depends on ─────────────────────────────────
def test_snapshot_returns_paths_not_status_lines(repo: Path) -> None:
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    assert agent_main._worktree_snapshot(repo) == {"tracked.txt"}


def test_staging_a_baseline_file_does_not_make_it_look_agent_touched(
    repo: Path,
) -> None:
    """The reason the snapshot holds paths rather than lines.

    A pre-existing edit that gets staged mid-run moves from ``" M"`` to
    ``"M "``. Line-based comparison would stop recognising it as pre-existing
    and the rollback would revert the user's own work.
    """
    (repo / "tracked.txt").write_text("user work in progress\n", encoding="utf-8")
    baseline = agent_main._worktree_snapshot(repo)
    _git(repo, "add", "tracked.txt")

    reverted = agent_main._rollback_agent_changes(repo, baseline)

    assert reverted == []
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "user work in progress\n"


def test_rollback_removes_an_accented_agent_file(repo: Path) -> None:
    """The live hole this PR closes: the file used to be left behind."""
    baseline = agent_main._worktree_snapshot(repo)
    (repo / ACCENTED).write_text("# half-written\n", encoding="utf-8")

    reverted = agent_main._rollback_agent_changes(repo, baseline)

    assert reverted == [ACCENTED]
    assert not (repo / ACCENTED).exists()
    assert agent_main._worktree_status(repo) == {}


def test_rollback_restores_an_accented_tracked_file(repo: Path) -> None:
    (repo / ACCENTED).write_text("original\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add accented")
    baseline = agent_main._worktree_snapshot(repo)
    (repo / ACCENTED).write_text("agent partial edit\n", encoding="utf-8")

    reverted = agent_main._rollback_agent_changes(repo, baseline)

    assert reverted == [ACCENTED]
    assert (repo / ACCENTED).read_text(encoding="utf-8") == "original\n"
