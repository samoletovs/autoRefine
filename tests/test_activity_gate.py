"""Tests for the sweep's activity gate.

The daily manifest sweep re-planned every project whether or not it had changed.
Each project costs two Foundry planning runs, and a planning run re-sends the whole
accumulated thread on every tool round, so an unchanged project bought a repeat of
yesterday's ideas for hundreds of thousands of input tokens. These tests pin the two
things that matter: an idle project does not reach Foundry, and a project the gate
cannot positively establish is idle always does.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import main
from agent.config import AutoRefineConfig

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str, **env: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            **env,
        },
    )


def _repo_with_commit_at(tmp_path: Path, when: datetime, name: str = "proj") -> Path:
    """A real git repo whose only commit is dated ``when``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "project.yaml").write_text(
        "name: proj\npurpose: p\nusers: u\nstage: active\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    stamp = when.isoformat()
    _git(repo, "commit", "-q", "-m", "c", GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return repo


def _commit_file(repo: Path, rel: str, when: datetime, body: str = "x") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    stamp = when.isoformat()
    _git(repo, "commit", "-q", "-m", rel, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)


# ── housekeeping does not count as activity ──────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["LICENSE", ".gitignore", ".github/workflows/ci.yml", "scripts/audit-leaks.ps1"],
)
def test_lab_wide_housekeeping_does_not_count_as_activity(tmp_path: Path, path: str) -> None:
    """The gate has to survive the lab's own cross-repo sweeps.

    On 2026-08-21 three of them — a licence sweep, a synced security script and a
    governance workflow update — had touched 22 of 24 projects within 28 hours. A
    gate counting any commit as activity would have skipped two projects and planned
    the rest, saving nothing. Removing the pathspec filter fails this test.
    """
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=90))
    _commit_file(repo, path, NOW - timedelta(hours=2))

    age = main._hours_since_last_commit(repo, NOW)
    assert age is not None and age > 24 * 80, f"{path} was counted as substantive work"


def test_real_work_after_housekeeping_still_counts(tmp_path: Path) -> None:
    """The filter must not swallow genuine changes that follow a sweep."""
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=90))
    _commit_file(repo, "LICENSE", NOW - timedelta(hours=5))
    _commit_file(repo, "src/app.py", NOW - timedelta(hours=2))

    age = main._hours_since_last_commit(repo, NOW)
    assert age is not None and 1.9 < age < 2.1


def test_repo_of_pure_housekeeping_reads_as_idle(tmp_path: Path) -> None:
    """git succeeding with no match is an answer, not an error."""
    repo = tmp_path / "hk"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit_file(repo, "LICENSE", NOW - timedelta(hours=1))

    assert main._hours_since_last_commit(repo, NOW) == float("inf")


# ── the commit-age reader ────────────────────────────────────────────────────


def test_reads_commit_age_from_a_real_repo(tmp_path: Path) -> None:
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(hours=50))
    age = main._hours_since_last_commit(repo, NOW)
    assert age is not None
    assert 49.9 < age < 50.1


def test_commit_age_is_unknown_outside_a_repo(tmp_path: Path) -> None:
    assert main._hours_since_last_commit(tmp_path, NOW) is None


# ── the decision ─────────────────────────────────────────────────────────────


def test_recent_commit_is_planned(tmp_path: Path) -> None:
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(hours=3))
    planned, reason = main.should_plan_repo("samoletovs/proj", repo, now=NOW)
    assert planned
    assert "within" in reason


def test_idle_project_is_skipped(tmp_path: Path) -> None:
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=40))
    # Pick a date this repo's rotation slot does not fall on, so only the commit
    # age is under test here.
    day = NOW
    while main._rotation_slot_due("samoletovs/proj", day, main.DEFAULT_ROTATION_DAYS):
        day += timedelta(days=1)

    planned, reason = main.should_plan_repo("samoletovs/proj", repo, now=day)
    assert not planned
    assert "rotation" in reason


def test_unknown_commit_age_still_plans(tmp_path: Path) -> None:
    """Fail open. A git failure must never silently switch ideation off fleet-wide."""
    planned, reason = main.should_plan_repo("samoletovs/proj", tmp_path, now=NOW)
    assert planned
    assert "unknown" in reason


def test_lookback_of_zero_disables_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOREFINE_ACTIVITY_LOOKBACK_HOURS", "0")
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=400))
    planned, _reason = main.should_plan_repo("samoletovs/proj", repo, now=NOW)
    assert planned


def test_garbage_env_falls_back_to_the_default_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOREFINE_ACTIVITY_LOOKBACK_HOURS", "soon")
    assert main._activity_lookback_hours() == float(main.DEFAULT_ACTIVITY_LOOKBACK_HOURS)
    monkeypatch.setenv("AUTOREFINE_ROTATION_DAYS", "never")
    assert main._rotation_days() == main.DEFAULT_ROTATION_DAYS


# ── the staleness floor ──────────────────────────────────────────────────────


def test_every_project_gets_a_slot_within_the_rotation() -> None:
    """No project can go unplanned indefinitely just by staying quiet."""
    repos = [f"samoletovs/p{i}" for i in range(40)]
    for repo in repos:
        days = [
            d
            for d in range(main.DEFAULT_ROTATION_DAYS)
            if main._rotation_slot_due(repo, NOW + timedelta(days=d), main.DEFAULT_ROTATION_DAYS)
        ]
        assert len(days) == 1, f"{repo} has slots on {days}"


def test_rotation_spreads_projects_across_the_days() -> None:
    """A floor that fired for everything on the same day would rebuild the old bill."""
    repos = [f"samoletovs/p{i}" for i in range(40)]
    per_day = [
        sum(
            1
            for r in repos
            if main._rotation_slot_due(r, NOW + timedelta(days=d), main.DEFAULT_ROTATION_DAYS)
        )
        for d in range(main.DEFAULT_ROTATION_DAYS)
    ]
    assert sum(per_day) == len(repos)
    assert max(per_day) <= len(repos) // 2


def test_rotation_slot_is_stable_across_weeks() -> None:
    repo = "samoletovs/era"
    base = main._rotation_slot_due(repo, NOW, main.DEFAULT_ROTATION_DAYS)
    assert main._rotation_slot_due(repo, NOW + timedelta(days=7), 7) == base
    assert main._rotation_slot_due(repo, NOW + timedelta(days=70), 7) == base


# ── end to end through _process_repo ─────────────────────────────────────────


def _sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path, **kw: object) -> list[str]:
    """Run _process_repo in file-ideas mode against ``repo``; return Foundry calls made."""
    calls: list[str] = []
    monkeypatch.setattr(main, "clone_repo", lambda _repo, _dir: True)
    monkeypatch.setattr(
        main,
        "load_config",
        lambda _dir: SimpleNamespace(name="proj", stage="active", to_context=lambda: ""),
    )
    monkeypatch.setattr(
        main, "evaluate_project", lambda _dir, _cfg, _repo=None: {"findings": [], "score": 50}
    )
    monkeypatch.setattr(
        main, "plan_project", lambda *a, **k: calls.append("plan_project") or None
    )
    monkeypatch.setattr(
        main, "plan_functional", lambda *a, **k: calls.append("plan_functional") or None
    )
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "propose")

    config = AutoRefineConfig(
        repos=["samoletovs/proj"], mode="file-ideas", workdir=repo.parent, **kw
    )
    main._process_repo("samoletovs/proj", config)
    return calls


def test_sweep_makes_no_foundry_call_for_an_idle_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The behaviour the change exists for: an idle project costs zero tokens."""
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=400))
    monkeypatch.setenv("AUTOREFINE_ROTATION_DAYS", "100000")  # never today

    calls = _sweep(tmp_path, monkeypatch, repo, gate_on_activity=True)

    assert calls == []
    # The deterministic report is still emitted, so scores stay fleet-wide.
    assert '"score": 50' in capsys.readouterr().out


def test_sweep_plans_a_project_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_commit_at(tmp_path, datetime.now(timezone.utc) - timedelta(hours=2))
    calls = _sweep(tmp_path, monkeypatch, repo, gate_on_activity=True)
    assert calls == ["plan_project", "plan_functional"]


def test_explicit_single_repo_is_never_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--repo is a human asking for this project now, however old the last commit."""
    repo = _repo_with_commit_at(tmp_path, NOW - timedelta(days=400))
    monkeypatch.setenv("AUTOREFINE_ROTATION_DAYS", "100000")

    calls = _sweep(tmp_path, monkeypatch, repo, gate_on_activity=False)

    assert calls == ["plan_project", "plan_functional"]


# ── Priority-scaled rotation ────────────────────────────────────────────────────
# Added 2026-08-21. Before this, every one of 25 experiments got the same guaranteed
# rotation slot, so `payArc` (stage: idea, untouched since spring) cost the same
# Foundry planning and filed ideas at the same rate as `era`, the flagship Q1
# experiment. In a lab whose binding constraint is budget that is a real allocation
# decision, and it was being made by omission.
#
# These pin the behaviour, not the storage: a `priority` field nothing acts on would
# pass a shape test and change nothing.


def test_top_priority_rotates_sooner_than_normal():
    assert main._rotation_days_for("top") < main._rotation_days_for("normal")


def test_low_priority_rotates_later_than_normal():
    assert main._rotation_days_for("low") > main._rotation_days_for("normal")


@pytest.mark.parametrize("value", ["banana", None, "", "TOP-ish"])
def test_untriaged_priority_is_treated_as_normal(value):
    """A project nobody has triaged must never silently drop to the 21-day cadence."""
    if value == "TOP-ish":
        assert main._rotation_days_for(value) == main._rotation_days_for("normal")
    else:
        assert main._rotation_days_for(value) == main._rotation_days_for("normal")


def test_case_is_ignored():
    assert main._rotation_days_for("TOP") == main._rotation_days_for("top")


def test_rotation_never_drops_below_one_day(monkeypatch):
    monkeypatch.setenv("AUTOREFINE_ROTATION_DAYS", "1")
    assert main._rotation_days_for("top") >= 1


def test_activity_still_beats_priority(tmp_path, monkeypatch):
    """A burst of work on a `low` project must never be ignored.

    Priority scales the *staleness floor*; it must not become a way to stop looking
    at a project that is actively being worked on.
    """
    monkeypatch.setattr(main, "_hours_since_last_commit", lambda *a, **k: 1.0)
    due, reason = main.should_plan_repo("samoletovs/x", tmp_path, now=NOW, priority="low")
    assert due
    assert "within" in reason


def test_skip_reason_names_the_priority(tmp_path, monkeypatch):
    """A skip nobody can explain is a skip somebody disables."""
    monkeypatch.setattr(main, "_hours_since_last_commit", lambda *a, **k: 10_000.0)
    monkeypatch.setattr(main, "_rotation_slot_due", lambda *a, **k: False)
    due, reason = main.should_plan_repo("samoletovs/x", tmp_path, now=NOW, priority="low")
    assert not due
    assert "priority=low" in reason


def _manifest(tmp_path, projects):
    import json
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return p


def test_repos_are_ordered_top_priority_first(tmp_path):
    # Ordering matters when a run is cut short by a timeout, a budget brake or a
    # crash: whatever it did reach should be what the owner cares about most.
    m = _manifest(tmp_path, [
        {"repo": "o/zzz-low", "status": "active", "priority": "low"},
        {"repo": "o/aaa-normal", "status": "active", "priority": "normal"},
        {"repo": "o/mmm-top", "status": "active", "priority": "top"},
    ])
    repos = main.load_repos_from_manifest(m)
    assert repos[0] == "o/mmm-top"
    assert repos[-1] == "o/zzz-low"


def test_untriaged_project_sorts_as_normal_not_last(tmp_path):
    m = _manifest(tmp_path, [
        {"repo": "o/low", "status": "active", "priority": "low"},
        {"repo": "o/untriaged", "status": "active"},
    ])
    assert main.load_repos_from_manifest(m)[0] == "o/untriaged"


def test_priorities_are_loaded_by_repo_slug(tmp_path):
    m = _manifest(tmp_path, [
        {"repo": "o/a", "status": "active", "priority": "TOP"},
        {"repo": "o/b", "status": "active"},
    ])
    pri = main.load_priorities_from_manifest(m)
    assert pri["o/a"] == "top"
    assert pri["o/b"] == "normal"


def test_unreadable_manifest_yields_no_priorities_rather_than_raising(tmp_path):
    """Fails open to `normal`: a broken manifest must not demote the whole fleet."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main.load_priorities_from_manifest(bad) == {}


def test_the_real_manifest_marks_the_flagships_top():
    """Pins the actual allocation, so a manifest edit that quietly demotes a flagship
    fails here rather than surfacing as silence weeks later."""
    real = Path(__file__).parent.parent.parent / ".github" / "config" / "workspace-manifest.json"
    if not real.exists():
        pytest.skip("workspace manifest not available in this checkout")
    by_name = {r.split("/")[-1]: p for r, p in main.load_priorities_from_manifest(real).items()}
    for flagship in ("era", "turgo", "atlas", "tPlan", "golazo", "autoRefine", "folio"):
        assert by_name.get(flagship) == "top", f"{flagship} should be top priority"
