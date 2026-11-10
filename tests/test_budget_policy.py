"""The scheduled Foundry boundary must reject absent or unsafe budget evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.budget_policy import BudgetPolicyError, load_budget_policy

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


def _write_policy(tmp_path: Path, **changes: object) -> Path:
    policy: dict[str, object] = {
        "generated_at": NOW.isoformat(),
        "tier": "normal",
        "cadence_per_day": 1,
        "run_deep_pass": False,
    }
    policy.update(changes)
    path = tmp_path / "azure-budget.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_fresh_allowed_policy_permits_the_scheduled_run(tmp_path: Path) -> None:
    policy = load_budget_policy(_write_policy(tmp_path), now=NOW)

    assert policy.permits_run
    assert policy.cadence_per_day == 1


def test_zero_cadence_policy_deliberately_stops_the_scheduled_run(tmp_path: Path) -> None:
    policy = load_budget_policy(
        _write_policy(tmp_path, tier="critical", cadence_per_day=0),
        now=NOW,
    )

    assert not policy.permits_run


def test_unmeasured_fallback_policy_is_a_stop_not_permission_to_spend(tmp_path: Path) -> None:
    policy = load_budget_policy(
        _write_policy(
            tmp_path,
            tier="critical",
            cadence_per_day=0,
            run_deep_pass=False,
            fallback_reason="cost source unavailable",
        ),
        now=NOW,
    )

    assert policy.has_fallback_reason
    assert not policy.permits_run


@pytest.mark.parametrize(
    "changes",
    [
        {"generated_at": (NOW - timedelta(hours=37)).isoformat()},
        {"generated_at": (NOW + timedelta(minutes=6)).isoformat()},
        {"generated_at": "not-a-timestamp"},
        {"generated_at": "2026-11-10T05:30:00"},
        {"tier": "critical", "cadence_per_day": 1},
        {"tier": "normal", "cadence_per_day": 0},
        {"tier": ["normal"]},
        {"cadence_per_day": True},
        {"cadence_per_day": 2},
        {"run_deep_pass": "false"},
        {"tier": "critical", "cadence_per_day": 1, "fallback_reason": "unavailable"},
    ],
)
def test_invalid_or_untrustworthy_policy_fails_closed(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(BudgetPolicyError):
        load_budget_policy(_write_policy(tmp_path, **changes), now=NOW)


def test_missing_policy_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BudgetPolicyError, match="missing"):
        load_budget_policy(tmp_path / "missing.json", now=NOW)


def test_cli_returns_distinct_status_for_stop_and_invalid_policy(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    stop_dir = tmp_path / "stop"
    invalid_dir = tmp_path / "invalid"
    stop_dir.mkdir()
    invalid_dir.mkdir()
    stop_path = _write_policy(
        stop_dir,
        generated_at=now.isoformat(),
        tier="critical",
        cadence_per_day=0,
    )
    invalid_path = _write_policy(
        invalid_dir,
        generated_at=(now - timedelta(hours=37)).isoformat(),
    )
    module_root = Path(__file__).resolve().parents[1]

    stopped = subprocess.run(
        [sys.executable, "-m", "agent.budget_policy", "--policy", str(stop_path)],
        cwd=module_root,
        capture_output=True,
        text=True,
        check=False,
    )
    invalid = subprocess.run(
        [sys.executable, "-m", "agent.budget_policy", "--policy", str(invalid_path)],
        cwd=module_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert stopped.returncode == 3
    assert "BUDGET POLICY STOP" in stopped.stderr
    assert invalid.returncode == 2
    assert "BUDGET POLICY INVALID" in invalid.stderr
