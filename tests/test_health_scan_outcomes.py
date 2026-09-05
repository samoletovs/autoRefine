"""Failed scans must remain failures after reports and notifications are attempted."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from agent import health_scan, notify
from agent.main import run_health_scan_mode


@pytest.fixture
def scan_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    mocks: dict[str, Mock] = {}
    for name, result in {
        "scan_github": {},
        "scan_azure_costs": {},
        "scan_app_insights": {},
        "check_deployed_urls": {},
        "analyze_with_ai": {"alerts": [], "issues_to_create": []},
        "commit_report": "reports/run/test.md",
        "enforce_report_retention": None,
        "create_github_issues": [],
    }.items():
        mocks[name] = Mock(return_value=result)
        monkeypatch.setattr(health_scan, name, mocks[name])
    mocks["send_telegram"] = Mock(return_value=True)
    monkeypatch.setattr(notify, "send_telegram", mocks["send_telegram"])
    return mocks


@pytest.mark.parametrize(
    ("stage", "result", "failure"),
    [
        ("analyze_with_ai", {"error": "model unavailable"}, "analysis"),
        ("commit_report", None, "report"),
        ("send_telegram", False, "telegram"),
    ],
)
def test_failed_stage_exits_nonzero_after_notification(
    scan_io: dict[str, Mock], capsys: pytest.CaptureFixture[str],
    stage: str, result: object, failure: str,
) -> None:
    scan_io[stage].return_value = result

    with pytest.raises(SystemExit) as error:
        run_health_scan_mode(["owner/repo"], assign_copilot=False)

    assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert failure in summary["failed_stages"]
    scan_io["send_telegram"].assert_called_once()


def test_successful_scan_with_no_findings_exits_normally(
    scan_io: dict[str, Mock], capsys: pytest.CaptureFixture[str],
) -> None:
    run_health_scan_mode(["owner/repo"], assign_copilot=False)

    assert json.loads(capsys.readouterr().out)["failed_stages"] == []
    scan_io["send_telegram"].assert_called_once()


def test_report_network_failure_still_attempts_notification(
    scan_io: dict[str, Mock], capsys: pytest.CaptureFixture[str],
) -> None:
    scan_io["commit_report"].side_effect = httpx.ConnectError("offline")

    with pytest.raises(SystemExit):
        run_health_scan_mode(["owner/repo"], assign_copilot=False)

    assert "report" in json.loads(capsys.readouterr().out)["failed_stages"]
    scan_io["send_telegram"].assert_called_once()


@pytest.fixture
def bash_shell() -> str:
    shell = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        git_bash = Path(git).parent.parent / "bin" / "bash.exe" if git else None
        shell = str(git_bash) if git_bash and git_bash.exists() else None
    if shell is None:
        pytest.skip("bash unavailable; required on the Ubuntu CI runner")
    return shell


@pytest.mark.parametrize(
    "missing",
    [
        (),
        ("AZURE_CLIENT_ID",),
        ("AZURE_TENANT_ID",),
        ("AZURE_SUBSCRIPTION_ID",),
        ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"),
    ],
)
def test_oidc_prerequisite_check_names_missing_inputs_without_leaking_values(
    tmp_path: Path, bash_shell: str, missing: tuple[str, ...],
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    data = yaml.safe_load((workflow / "autorefine-health-scan.yml").read_text("utf-8"))
    job = data["jobs"]["health-scan"]
    steps = job["steps"]
    guard = next(
        (step for step in steps if step["name"] == "Validate Azure OIDC prerequisites"), None,
    )
    assert guard is not None, "Missing an explicit OIDC prerequisite check before azure/login"
    login = next(step for step in steps if step.get("uses", "").lower().startswith("azure/login@"))
    assert steps.index(guard) < steps.index(login)
    assert not guard.get("continue-on-error")
    assert guard.get("if", "success()") == "success()"
    assert login.get("if", "success()") == "success()"
    bindings = {**job.get("env", {}), **guard.get("env", {})}
    values = {
        "AZURE_CLIENT_ID": "private-client-value",
        "AZURE_TENANT_ID": "private-tenant-value",
        "AZURE_SUBSCRIPTION_ID": "private-subscription-value",
    }
    for name, login_input in (
        ("AZURE_CLIENT_ID", "client-id"),
        ("AZURE_TENANT_ID", "tenant-id"),
        ("AZURE_SUBSCRIPTION_ID", "subscription-id"),
    ):
        assert bindings[name] == login["with"][login_input] == "${{ secrets." + name + " }}"
    env = {**os.environ, **values}
    env.update({name: "" for name in missing})

    proc = subprocess.run(
        [bash_shell, "-e", "-o", "pipefail", "-c", guard["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )

    assert proc.returncode == (1 if missing else 0)
    output = proc.stdout + proc.stderr
    for name in missing:
        assert name in output
    for name in values.keys() - set(missing):
        assert name not in output
    for value in values.values():
        assert value not in output
    if missing:
        assert "::error::" in output


@pytest.mark.parametrize("returncode", [0, 17])
def test_workflow_preserves_scan_exit_and_prints_its_log(
    tmp_path: Path, returncode: int, bash_shell: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    data = yaml.safe_load((workflow / "autorefine-health-scan.yml").read_text("utf-8"))
    steps = data["jobs"]["health-scan"]["steps"]
    script = next(step["run"] for step in steps if step["name"] == "Run health scan")
    script = script.replace("/tmp/autorefine-health.json", "autorefine-health.json")
    stub = f"python() {{ echo scan-output; return {returncode}; }}\n"
    proc = subprocess.run(
        [bash_shell, "-e", "-o", "pipefail", "-c", stub + script],
        cwd=tmp_path, env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True, text=True, timeout=10,
    )

    assert proc.returncode == returncode
    assert "scan-output" in proc.stdout


@pytest.mark.parametrize("filename", ["autorefine-health-scan.yml", "pr-ready-cards.yml"])
def test_failure_notification_keeps_run_link_without_preview(
    tmp_path: Path, bash_shell: str, filename: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / filename
    data = yaml.safe_load(workflow.read_text("utf-8"))
    job = next(iter(data["jobs"].values()))
    step = next(step for step in job["steps"] if step["name"] == "Failure notification")
    script = step["run"]
    for name, value in {
        "github.server_url": "https://github.com",
        "github.repository": "owner/repo",
        "github.run_id": "123",
    }.items():
        script = script.replace("${{ " + name + " }}", value)
    proc = subprocess.run(
        [bash_shell, "-e", "-c", "curl() { printf '%s\\n' \"$@\"; }\n" + script],
        cwd=tmp_path,
        env={**os.environ, "NAURO_BOT_TOKEN": "test-token", "NAURO_CHAT_ID": "test-chat"},
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )

    assert proc.returncode == 0
    assert "disable_web_page_preview=true" in proc.stdout.splitlines()
    assert "https://github.com/owner/repo/actions/runs/123" in proc.stdout
    assert "failed" in proc.stdout
    assert step["if"] == "failure()"
