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
    # Assert — 'A' deduped, capped at FUNCTIONAL_IDEA_CAP (1 idea per project per day)
    assert [i["title"] for i in selected] == ["A"]
    assert len(selected) == m.FUNCTIONAL_IDEA_CAP


def test_select_functional_excludes_out_of_range_priority():
    plan = _plan(("low", "P3"), ("ok", "P1"))
    selected = m._select_functional_improvements(plan)
    assert [i["title"] for i in selected] == ["ok"]


def test_select_functional_normalizes_messy_priority():
    plan = {"improvements": [{"title": "Dashboard", "priority": "[P0 — Critical]", "category": "feature"}]}
    selected = m._select_functional_improvements(plan)
    assert [i["title"] for i in selected] == ["Dashboard"]
    assert selected[0]["priority"] == "P0"  # normalized for the card + memo


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
    # Assert — filed the capped selection (1 idea/project/day) with the functional priorities
    assert captured["titles"] == ["A"]
    assert captured["priorities"] == m.FUNCTIONAL_PRIORITIES
    assert len(result) == 1


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


# --- cards mode + reason-fed generation -------------------------------------


def test_functional_mode_accepts_cards(monkeypatch):
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "cards")
    assert m._functional_mode() == "cards"


def test_parse_issue_number_extracts_from_url():
    assert m._parse_issue_number("https://github.com/samoletovs/era/issues/42") == 42
    assert m._parse_issue_number("not a url") is None
    assert m._parse_issue_number("") is None


def test_format_avoid_context_empty_is_blank():
    assert m._format_avoid_context([]) == ""


def test_format_avoid_context_lists_declined():
    block = m._format_avoid_context(["CSV export — too niche", "Dark mode"])
    assert "DECLINED" in block
    assert "- CSV export — too niche" in block
    assert "- Dark mode" in block


def test_functional_task_appends_avoid_context():
    base = m._functional_task()
    with_ctx = m._functional_task(avoid_context="AVOID: foo")
    assert "AVOID: foo" in with_ctx
    assert "AVOID: foo" not in base


def test_file_and_card_files_and_cards_each(monkeypatch):
    # Avoid touching the filesystem / governance script.
    monkeypatch.setattr(m, "_resolve_file_idea_script", lambda: m.Path("file-idea.py"))
    monkeypatch.setattr(m, "_build_run_references", lambda: "refs")
    filed: list[str] = []
    carded: list[tuple] = []

    def fake_filer(imp):
        filed.append(imp["title"])
        return f"https://github.com/samoletovs/era/issues/{len(filed)}"

    def fake_carder(repo, number, imp):
        carded.append((repo, number, imp["title"]))
        return True

    plan = [
        {"title": "A", "priority": "P1", "category": "feature"},
        {"title": "A", "priority": "P1", "category": "feature"},  # duplicate
        {"title": "B", "priority": "P2", "category": "feature"},
    ]
    count = m.file_and_card_functional_ideas(
        "samoletovs/era", plan, filer=fake_filer, carder=fake_carder
    )
    assert count == 2  # A and B; duplicate skipped
    assert filed == ["A", "B"]
    assert [c[1] for c in carded] == [1, 2]


def test_file_and_card_skips_when_filing_fails(monkeypatch):
    monkeypatch.setattr(m, "_resolve_file_idea_script", lambda: m.Path("file-idea.py"))
    monkeypatch.setattr(m, "_build_run_references", lambda: "refs")
    carded: list = []
    count = m.file_and_card_functional_ideas(
        "samoletovs/era",
        [{"title": "A", "priority": "P1", "category": "feature"}],
        filer=lambda imp: None,  # filing failed → no card
        carder=lambda *a: carded.append(a) or True,
    )
    assert count == 0 and not carded


def test_handle_cards_routes_to_carder():
    plan = _plan(("A", "P1"), ("B", "P2"))
    calls: dict = {}

    def fake_carder(repo, selected, dry_run=False):
        calls["repo"] = repo
        calls["titles"] = [i["title"] for i in selected]
        return len(selected)

    result = m.handle_functional_ideas(
        "samoletovs/era", plan, mode="cards", carder=fake_carder, notifier=lambda s: None
    )
    assert calls["titles"] == ["A"]
    assert len(result) == 1


# --- plan_functional transient-failure retry --------------------------------


def _patch_functional_agent(monkeypatch, run_agent_impl):
    """Wire plan_functional's Foundry dependencies to fakes (no real API calls)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    fake_client = SimpleNamespace(delete_agent=MagicMock())
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("azure.ai.agents.AgentsClient", lambda **_k: fake_client)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.foundry_agent.create_agent", lambda *_a, **_k: "agent-1")
    monkeypatch.setattr("agent.foundry_agent.run_agent", run_agent_impl)
    return fake_client


def test_plan_functional_retries_on_transient_none(monkeypatch, tmp_path):
    """A transient Foundry failure (run_agent -> None) is retried before giving up."""
    from types import SimpleNamespace

    calls = {"n": 0}
    plan = {"improvements": [{"title": "X", "priority": "P1"}]}

    def flaky(*_a, **_k):
        calls["n"] += 1
        return None if calls["n"] == 1 else plan  # fail once, then succeed

    fake_client = _patch_functional_agent(monkeypatch, flaky)
    result = m.plan_functional(tmp_path, SimpleNamespace(name="era"))

    assert calls["n"] == 2  # retried once after the transient None
    assert result == plan
    fake_client.delete_agent.assert_called_once_with("agent-1")


def test_plan_functional_gives_up_after_all_attempts(monkeypatch, tmp_path):
    """When every attempt fails, plan_functional returns None and still cleans up."""
    from types import SimpleNamespace

    calls = {"n": 0}

    def always_none(*_a, **_k):
        calls["n"] += 1
        return None

    fake_client = _patch_functional_agent(monkeypatch, always_none)
    result = m.plan_functional(tmp_path, SimpleNamespace(name="era"))

    assert result is None
    assert calls["n"] == m.FUNCTIONAL_PLAN_ATTEMPTS  # exhausted all attempts
    fake_client.delete_agent.assert_called_once_with("agent-1")
