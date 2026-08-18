"""Smoke tests for autoRefine's copy of the shared can-auto-merge.py gate.

The script at scripts/can-auto-merge.py is now **byte-identical** to the
governance repo's copy, synced by nauroLabs-github's
scripts/install-shared-review.ps1. What makes it autoRefine's gate is
config/auto-review-patterns.json: agent/ and scripts/ appear in BOTH low_risk
and high_risk so Copilot's source PRs take the deep-review path rather than
being escalated as 'file-tier'.

That split exists because the two repos previously each carried a full fork of
the script for the sake of that allowlist, and by 2026-08-18 they had drifted
37 lines apart while 152 lines of gate tests lived on only one side. A fix
landing on one copy and not the other is the failure being designed out, so
test_script_has_not_been_forked_again below is the load-bearing test here.

These tests guard the pattern wiring — gh and HTTP I/O are not exercised.
Network-level tests live alongside the governance copy.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import pytest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "can-auto-merge.py"


@pytest.fixture(scope="module")
def gate_module():
    spec = importlib.util.spec_from_file_location("can_auto_merge", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["can_auto_merge"] = mod
    spec.loader.exec_module(mod)
    return mod


def _match_any(patterns, path: str) -> bool:
    return any(p.search(path) for p in patterns)


def test_agent_paths_are_both_low_and_high_risk(gate_module):
    # agent/foundry_agent.py must match LOW_RISK (so file-tier gate passes)
    # AND HIGH_RISK (so the deep-review job runs and gates the merge).
    path = "agent/foundry_agent.py"
    assert _match_any(gate_module.LOW_RISK_PATTERNS, path), (
        "agent/ must be in LOW_RISK_PATTERNS or PRs touching the source "
        "code will be escalated as 'file-tier' and never auto-merge"
    )
    assert _match_any(gate_module.HIGH_RISK_PATTERNS, path), (
        "agent/ must be in HIGH_RISK_PATTERNS so the closed-loop Claude "
        "deep-reviewer gates every source-code change"
    )


def test_scripts_paths_are_both_low_and_high_risk(gate_module):
    path = "scripts/eval_all.py"
    assert _match_any(gate_module.LOW_RISK_PATTERNS, path)
    assert _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_markdown_is_low_risk_only(gate_module):
    path = "README.md"
    assert _match_any(gate_module.LOW_RISK_PATTERNS, path)
    assert not _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_workflow_yaml_is_low_risk_only(gate_module):
    path = ".github/workflows/auto-review.yml"
    assert _match_any(gate_module.LOW_RISK_PATTERNS, path)
    # Workflow files don't trigger deep review unless they match
    # auth/secret/credential by substring.
    assert not _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_test_files_are_low_risk_only(gate_module):
    path = "tests/test_foundry_agent.py"
    assert _match_any(gate_module.LOW_RISK_PATTERNS, path)
    assert not _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_path_with_auth_substring_triggers_high_risk(gate_module):
    # Defence-in-depth: anything containing "auth", "secret", "credential"
    # in its path must hit deep review even when it's also low-risk.
    path = "agent/auth_helpers.py"
    assert _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_bicep_files_are_high_risk(gate_module):
    path = "infrastructure/main.bicep"
    assert _match_any(gate_module.HIGH_RISK_PATTERNS, path)


def test_random_unmatched_path_is_neither(gate_module):
    # Belt-and-braces: a file outside the allowlist should still cause
    # file-tier escalation. If someone later opens a PR touching a brand-
    # new top-level folder we haven't approved, the gate must catch it.
    path = "vendor/some-thirdparty/file.go"
    assert not _match_any(gate_module.LOW_RISK_PATTERNS, path)
    assert not _match_any(gate_module.HIGH_RISK_PATTERNS, path)


# --- the reason the config split exists -------------------------------------


def _governance_copy() -> pathlib.Path | None:
    """The central source, when the workspace has it checked out beside us."""
    candidate = (pathlib.Path(__file__).resolve().parents[2]
                 / ".github" / "scripts" / "can-auto-merge.py")
    return candidate if candidate.exists() else None


def test_script_has_not_been_forked_again():
    """This copy must stay byte-identical to the governance source.

    Editing the script here instead of its config is exactly how the two repos
    drifted 37 lines apart last time. Re-sync with:

        pwsh -File ../.github/scripts/install-shared-review.ps1 -Project .

    Skipped when the governance repo is not checked out beside this one, which
    is the case in CI — this is a workspace guard, not a build gate.
    """
    source = _governance_copy()
    if source is None:
        pytest.skip("governance repo not present beside this one")
    mine = hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()
    theirs = hashlib.sha256(source.read_bytes()).hexdigest()
    assert mine == theirs, (
        "scripts/can-auto-merge.py has diverged from the governance copy. "
        "Repo-specific behaviour belongs in config/auto-review-patterns.json; "
        "re-sync the script with install-shared-review.ps1."
    )


def test_the_repo_ships_its_own_allowlist(gate_module):
    """The config is what makes this autoRefine's gate rather than the lab's."""
    config = pathlib.Path(__file__).resolve().parents[1] / "config" / "auto-review-patterns.json"
    assert config.exists(), "config/auto-review-patterns.json is missing"
    assert gate_module.PATTERNS_PATH.resolve() == config.resolve()


def test_governance_only_paths_are_not_low_risk_here(gate_module):
    """The allowlists are genuinely different, not a copy of the lab's.

    skills/ and wiki/ are governance-repo concepts, granted there as whole
    directories. Non-markdown files under them are the honest test: any *.md is
    low-risk in both repos, so a path like wiki/ideas.md proves nothing about
    which config is loaded.
    """
    for path in ("skills/foo/run.sh", "wiki/data.csv"):
        assert not _match_any(gate_module.LOW_RISK_PATTERNS, path)


def test_autorefine_only_paths_are_low_risk_here(gate_module):
    """The mirror of the above: this repo's own layout is granted."""
    for path in ("agent/main.py", "docs/reference/api.txt"):
        assert _match_any(gate_module.LOW_RISK_PATTERNS, path)
