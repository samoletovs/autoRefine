"""A partial evaluation must not become an all-clear because some scores exist."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent import main as agent_main
from agent.config import ProjectConfig
from agent.parse_scores import extract_score_objects


@pytest.mark.parametrize("failed_stage", ["clone", "evaluation", "planning", None])
def test_sweep_finishes_other_repos_and_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_stage: str | None,
) -> None:
    repos = ["owner/first", "owner/broken", "owner/last"]
    visited: list[str] = []
    monkeypatch.setattr(agent_main, "load_repos_from_manifest", lambda _path: repos)
    monkeypatch.setattr(agent_main, "load_priorities_from_manifest", lambda _path: {})
    monkeypatch.setattr(agent_main, "should_plan_repo", lambda *_args, **_kw: (True, "due"))
    monkeypatch.setattr(
        agent_main, "load_config",
        lambda path: ProjectConfig(name=path.name, purpose="", users="", stage="active"),
    )
    monkeypatch.setenv("AUTOREFINE_FUNCTIONAL_MODE", "off")
    monkeypatch.setattr(agent_main, "file_ideas_for_plan", lambda *_args, **_kw: 0)

    def clone(repo: str, _target: Path) -> bool:
        visited.append(repo)
        return not (repo == "owner/broken" and failed_stage == "clone")

    def evaluate(path: Path, *_args: object) -> dict:
        if path.name == "broken" and failed_stage == "evaluation":
            raise RuntimeError("evaluation failed")
        return {"project": path.name, "score": 80, "findings": []}

    def plan(path: Path, *_args: object, **_kwargs: object) -> dict:
        if path.name == "broken" and failed_stage == "planning":
            raise RuntimeError("planning failed")
        return {"score": 80, "improvements": []}

    monkeypatch.setattr(agent_main, "clone_repo", clone)
    monkeypatch.setattr(agent_main, "evaluate_project", evaluate)
    monkeypatch.setattr(agent_main, "plan_project", plan)
    monkeypatch.setattr(
        "sys.argv",
        ["autorefine", "--manifest", "unused.json", "--mode", "file-ideas",
         "--workdir", str(tmp_path / "work")],
    )

    if failed_stage:
        with pytest.raises(SystemExit) as error:
            agent_main.main()
        assert error.value.code == 1
    else:
        agent_main.main()

    assert visited == repos
    output = capsys.readouterr().out
    report = tmp_path / "report.json"
    report.write_text(output, encoding="utf-8")
    scores = extract_score_objects(str(report))
    assert {score["project"] for score in scores} >= {"first", "last"}
    if failed_stage:
        decoder = json.JSONDecoder()
        objects = []
        while output.strip():
            obj, end = decoder.raw_decode(output.lstrip())
            objects.append(obj)
            output = output.lstrip()[end:]
        assert objects[-1] == {"run_status": "failed", "failed_repos": ["owner/broken"]}


def _evaluate_steps() -> list[dict]:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    document = yaml.safe_load((workflow / "autorefine-evaluate.yml").read_text("utf-8"))
    return document["jobs"]["evaluate"]["steps"]


@pytest.mark.parametrize("returncode", [0, 17])
def test_evaluate_workflow_preserves_exit_after_printing_report(
    tmp_path: Path, returncode: int,
) -> None:
    shell = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        git_bash = Path(git).parent.parent / "bin" / "bash.exe" if git else None
        if git_bash and git_bash.exists():
            shell = str(git_bash)
    if shell is None:
        pytest.skip("bash unavailable; required on the Ubuntu CI runner")
    script = next(step["run"] for step in _evaluate_steps() if step["name"] == "Run autoRefine")
    script = script.replace("/tmp/autorefine-report.json", "report.json")
    script = script.replace("/tmp/autorefine-logs.txt", "logs.txt")
    stub = f"python() {{ echo partial-report; echo failure-detail >&2; return {returncode}; }}\n"

    result = subprocess.run(
        [shell, "-e", "-o", "pipefail", "-c", stub + script],
        cwd=tmp_path,
        env={**os.environ, "MODE": "file-ideas", "REPO_INPUT": ""},
        capture_output=True, text=True, timeout=10, check=False,
    )

    assert result.returncode == returncode
    assert "partial-report" in result.stdout
    assert "failure-detail" in result.stdout


def test_partial_failure_reaches_notification_and_job_verdict_without_auto_assignment() -> None:
    steps = _evaluate_steps()
    run = next(step for step in steps if step["name"] == "Run autoRefine")
    parse = next(step for step in steps if step["name"] == "Parse scores")
    notify = next(step for step in steps if step["name"] == "Notify Telegram")
    issue = next(step for step in steps if step["name"] == "Create or update failure issue")
    final = steps[-1]

    assert run["id"] == "run-autorefine"
    assert run["continue-on-error"] is True
    assert parse["if"] == "steps.run-autorefine.outcome != 'skipped' && !cancelled()"
    assert notify["env"]["RUN_STATUS"] == "${{ steps.run-autorefine.outcome }}"
    assert "steps.run-autorefine.outcome == 'failure'" in final["if"]
    assert "steps.parse-scores.outcome == 'failure'" in final["if"]
    assert issue["if"] == "steps.parse-scores.outcome == 'failure'"
