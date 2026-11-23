from __future__ import annotations

import json
import subprocess

from agent.config import ProjectConfig
from agent.dashboard import _render_feature_suggestions
from agent.main import evaluate_project, handle_functional_ideas, plan_functional
from agent.repository_evidence import repository_inventory, verify_plan_evidence
from agent.tools.quality_tools import QualityCoverage, RepoContext


def _config() -> ProjectConfig:
    return ProjectConfig(
        name="demo", purpose="Help users", users="users", stage="active",
        goals=["Improve onboarding"], similar=["Competitor"],
    )


def _track(root, *paths):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *paths], check=True)


def test_evaluation_suggestions_include_matching_repository_context(tmp_path, monkeypatch):
    source = tmp_path / "onboarding.py"
    source.write_text('def onboarding():\n    return "Welcome users"\n', encoding="utf-8")
    _track(tmp_path, "onboarding.py")
    monkeypatch.setattr(
        "agent.main.run_quality_checks_with_coverage",
        lambda *_args: ([], QualityCoverage()),
    )
    report = evaluate_project(tmp_path, _config(), RepoContext(slug="owner/demo"))
    suggestion, parity = report["feature_suggestions"]
    assert suggestion["evidence"] == [{"path": "onboarding.py", "excerpt": "def onboarding():"}]
    assert suggestion["evidence_status"] == "repository-context"
    assert parity["evidence_status"] == "unverified"
    html = _render_feature_suggestions(
        [{**suggestion, "repo": report["repo"]}, parity], verified_evidence=True,
    )
    assert 'href="https://github.com/owner/demo/blob/HEAD/onboarding.py"' in html
    assert "def onboarding():" in html
    assert "unverified" in html


def test_inventory_excludes_secrets_hidden_directories_and_symlinks(tmp_path):
    (tmp_path / "app.py").write_text(
        'def onboarding_password(): pass\ndef onboarding(): pass\n', encoding="utf-8"
    )
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "hidden.py").write_text("onboarding hidden", encoding="utf-8")
    (tmp_path / "secret.py").write_text("onboarding secret", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "app.py")
    (tmp_path / "local.py").write_text("def onboarding_local(): pass\n", encoding="utf-8")
    _track(tmp_path, "app.py", ".private/hidden.py", "secret.py", "link.py")
    assert repository_inventory(tmp_path, _config()) == [
        {"path": "app.py", "excerpt": "def onboarding(): pass"}
    ]


def test_functional_plan_passes_inventory_and_canonicalizes_citations(tmp_path, monkeypatch):
    (tmp_path / "onboarding.py").write_text("def onboarding(): pass\n", encoding="utf-8")
    _track(tmp_path, "onboarding.py")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setattr("agent.foundry_agent.open_foundry_clients", lambda _: (object(), object()))
    monkeypatch.setattr("agent.foundry_agent.create_agent", lambda *a, **k: object())
    monkeypatch.setattr("agent.main._extract_relevant_wiki_insights", lambda _: "")
    captured = {}

    def run(_client, _agent, _dir, _config, task, **_kwargs):
        captured["task"] = task
        return {"improvements": [
            {"title": "Welcome", "evidence": [{"path": "onboarding.py", "excerpt": "invented"}]},
            {"title": "Unsupported", "evidence": [{"path": "../outside"}]},
        ]}

    monkeypatch.setattr("agent.foundry_agent.run_agent", run)
    plan = plan_functional(tmp_path, _config())
    assert '"path": "onboarding.py"' in captured["task"]
    assert plan["improvements"][0]["evidence"] == [
        {"path": "onboarding.py", "excerpt": "def onboarding(): pass"}
    ]
    assert plan["improvements"][1]["evidence_status"] == "unverified"
    assert json.dumps(plan["improvements"][1]["evidence"]) == "[]"


def test_evidence_renderer_escapes_content_and_rejects_unsafe_paths():
    html = _render_feature_suggestions([{
        "title": "Test", "repo": "owner/demo",
        "evidence": [{"path": "../<script>", "excerpt": "<img src=x onerror=alert(1)>"}],
    }], verified_evidence=True)
    assert "href=" not in html
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;img" in html


def test_untrusted_model_evidence_is_never_rendered_as_verified():
    html = _render_feature_suggestions([{
        "title": "Test", "repo": "owner/demo",
        "evidence": [{"path": "app.py", "excerpt": "invented"}],
    }])
    assert "invented" not in html
    assert "unverified" in html


def test_malformed_citations_are_unverified():
    plan = {"improvements": [{"evidence": [{"path": []}]}, {"evidence": "invalid"}]}
    verify_plan_evidence(plan, [{"path": "app.py", "excerpt": "onboarding"}])
    assert all(item["evidence_status"] == "unverified" for item in plan["improvements"])


def test_unverified_functional_ideas_are_not_filed():
    filed = []
    selected = handle_functional_ideas(
        "owner/demo",
        {"improvements": [{"title": "Missing", "priority": "P1",
                           "evidence_status": "unverified"}]},
        mode="file", filer=lambda *args, **kwargs: filed.append(args),
    )
    assert selected == []
    assert not filed
