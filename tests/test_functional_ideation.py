"""Unit tests for autoRefine's observe-first functional ideation.

Pure logic only — the Foundry agent, GitHub filing, and Telegram are all mocked or
injected, per the testing convention "mock external dependencies — never call real
API services".
"""

from __future__ import annotations

import agent.main as m


def _plan(*titles_pri: tuple[str, str]) -> dict:
    return {
        "improvements": [
            {"title": t, "priority": p, "category": "feature"} for t, p in titles_pri
        ]
    }


# --- _map_improvement_type --------------------------------------------------


def test_map_improvement_type_routes_bug_to_bugfix():
    assert m._map_improvement_type("bug") == "bugfix"
    assert m._map_improvement_type("security") == "bugfix"


def test_map_improvement_type_routes_functional_to_feature():
    assert m._map_improvement_type("feature") == "feature"
    assert m._map_improvement_type("UX") == "feature"
    assert m._map_improvement_type("feature-parity") == "feature"


def test_map_improvement_type_defaults_to_refactor():
    assert m._map_improvement_type("performance") == "refactor"


# --- _functional_mode gate --------------------------------------------------


def test_functional_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("AUTOREFINE_FUNCTIONAL_MODE", raising=False)
    assert m._functional_mode() == "off"


def test_functional_mode_reads_valid_values(monkeypatch):
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "propose")
    assert m._functional_mode() == "propose"
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "FILE")
    assert m._functional_mode() == "file"


def test_functional_mode_rejects_unknown(monkeypatch):
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "yolo")
    assert m._functional_mode() == "off"


# --- _select_functional_improvements ----------------------------------------


def test_select_functional_caps_and_dedupes():
    # Arrange — duplicate 'A' and more than the cap
    plan = _plan(("A", "P1"), ("A", "P1"), ("B", "P2"), ("C", "P1"))
    # Act
    selected = m._select_functional_improvements(plan)
    # Assert — 'A' deduped, capped at FUNCTIONAL_IDEA_CAP (2)
    assert [i["title"] for i in selected] == ["A", "B"]
    assert len(selected) == m.FUNCTIONAL_IDEA_CAP


def test_select_functional_excludes_out_of_range_priority():
    plan = _plan(("low", "P3"), ("ok", "P1"))
    selected = m._select_functional_improvements(plan)
    assert [i["title"] for i in selected] == ["ok"]


# --- _format_functional_summary ---------------------------------------------


def test_format_summary_lists_titles_and_enable_hint():
    plan = _plan(("Add CSV export", "P1"))
    summary = m._format_functional_summary(
        "samoletovs/era", m._select_functional_improvements(plan)
    )
    assert "Add CSV export" in summary
    assert "AUTOREFINE_FUNCTIONAL_MODE=file" in summary


# --- handle_functional_ideas (observe-first routing) ------------------------


def test_handle_propose_notifies_and_does_not_file():
    # Arrange
    plan = _plan(("Add CSV export", "P1"))
    notified: list[str] = []
    filed: list[tuple] = []
    # Act
    result = m.handle_functional_ideas(
        "samoletovs/era",
        plan,
        mode="propose",
        notifier=lambda s: notified.append(s),
        filer=lambda *a, **k: filed.append(a),
    )
    # Assert — proposed and notified, nothing filed
    assert len(result) == 1
    assert notified and not filed


def test_handle_file_files_capped_selection_with_priorities():
    # Arrange
    plan = _plan(("A", "P1"), ("B", "P2"), ("C", "P1"))
    captured: dict = {}

    def fake_filer(repo, plan_arg, dry_run=False, allowed_priorities=None):
        captured["titles"] = [i["title"] for i in plan_arg["improvements"]]
        captured["priorities"] = allowed_priorities

    # Act
    result = m.handle_functional_ideas(
        "samoletovs/era", plan, mode="file", filer=fake_filer, notifier=lambda s: None
    )
    # Assert — filed the capped selection with the functional priority set
    assert captured["titles"] == ["A", "B"]
    assert captured["priorities"] == m.FUNCTIONAL_PRIORITIES
    assert len(result) == 2


def test_handle_empty_selection_is_a_noop():
    calls: list = []
    result = m.handle_functional_ideas(
        "r",
        {"improvements": []},
        mode="propose",
        notifier=lambda s: calls.append(s),
        filer=lambda *a, **k: calls.append(a),
    )
    assert result == [] and not calls
