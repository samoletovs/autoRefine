"""Smoke tests for autoRefine's adapted can-auto-merge.py predicate.

The script lives at scripts/can-auto-merge.py and is a copy of the
governance gate with autoRefine-specific patterns: agent/ and scripts/
appear in BOTH LOW_RISK and HIGH_RISK so that Copilot's source-code PRs
hit the deep-review path instead of being escalated as 'file-tier'.

These tests guard the pattern definitions themselves — gh and HTTP I/O
are not exercised here. Network-level tests live alongside the
governance copy.
"""

from __future__ import annotations

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
