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
from agent.main import main, run_health_scan_mode


@pytest.fixture
def scan_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    mocks: dict[str, Mock] = {}
    for name, result in {
        "scan_github": {},
        "scan_azure_costs": {"total": 0},
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


@pytest.mark.parametrize("analysis_failed", [False, True])
def test_health_scan_cli_dry_run_never_writes_or_sends(
    scan_io: dict[str, Mock], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], analysis_failed: bool,
) -> None:
    planned = [{"repo": "repo", "title": "Investigate", "body": "A proposed finding"}]
    scan_io["analyze_with_ai"].return_value = (
        {"error": "model unavailable"} if analysis_failed
        else {"alerts": [], "issues_to_create": planned}
    )
    monkeypatch.setattr(
        "sys.argv", ["autorefine", "--mode", "health-scan", "--repo", "owner/repo", "--dry-run"],
    )

    if analysis_failed:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
    else:
        main()

    for name in ("commit_report", "enforce_report_retention", "create_github_issues", "send_telegram"):
        scan_io[name].assert_not_called()
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True
    assert summary["report_path"] is None
    assert summary["created_issues"] == []
    assert summary["planned_issues"] == ([] if analysis_failed else planned)
    assert summary["failed_stages"] == (["analysis"] if analysis_failed else [])
    assert "DRY RUN" in summary["telegram_summary"]
    assert summary["report"]
    scan_io["analyze_with_ai"].assert_called_once()


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
@pytest.mark.parametrize("filename", ["autorefine-health-scan.yml", "autorefine-evaluate.yml"])
def test_oidc_prerequisite_check_names_missing_inputs_without_leaking_values(
    tmp_path: Path, bash_shell: str, missing: tuple[str, ...], filename: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    data = yaml.safe_load((workflow / filename).read_text("utf-8"))
    job = next(iter(data["jobs"].values()))
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
    output_path = tmp_path / "outputs"
    env["GITHUB_OUTPUT"] = str(output_path)

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
    assert output_path.read_text("utf-8").strip() == f"missing={' '.join(missing)}"


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


def test_failure_notification_keeps_run_link_without_preview(
    tmp_path: Path, bash_shell: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-ready-cards.yml"
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


@pytest.mark.parametrize(
    ("oidc", "login", "scan", "sweep", "missing", "expected"),
    [
        ("failure", "skipped", "skipped", "success", "AZURE_CLIENT_ID AZURE_TENANT_ID",
         ("Missing Azure OIDC settings: AZURE_CLIENT_ID AZURE_TENANT_ID",
          "Health scan: not run", "PR-card sweep: succeeded")),
        ("success", "failure", "skipped", "success", "",
         ("Azure login failed", "Health scan: not run", "PR-card sweep: succeeded")),
        ("success", "success", "failure", "success", "",
         ("Health scan: failed", "PR-card sweep: succeeded")),
        ("success", "success", "success", "failure", "",
         ("Health scan: succeeded", "PR-card sweep: failed")),
        ("skipped", "skipped", "skipped", "failure", "",
         ("Setup failed before Azure login", "Health scan: not run", "PR-card sweep: failed")),
    ],
)
def test_health_failure_notification_identifies_actual_stages(
    tmp_path: Path, bash_shell: str, oidc: str, login: str, scan: str, sweep: str,
    missing: str, expected: tuple[str, ...],
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    steps = yaml.safe_load((workflow / "autorefine-health-scan.yml").read_text("utf-8"))[
        "jobs"
    ]["health-scan"]["steps"]
    step = next(step for step in steps if step["name"] == "Failure notification")
    bindings = {
        "OIDC_STATUS": "${{ steps.azure-prerequisites.outcome }}",
        "AZURE_LOGIN_STATUS": "${{ steps.azure-login.outcome }}",
        "SCAN_STATUS": "${{ steps.health-scan.outcome }}",
        "PR_SWEEP_STATUS": "${{ steps.pr-sweep.outcome }}",
        "AZURE_OIDC_MISSING": "${{ steps.azure-prerequisites.outputs.missing }}",
        "RUN_URL": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
    }
    assert step.get("env") == bindings
    for step_id in ("azure-prerequisites", "azure-login", "health-scan", "pr-sweep"):
        assert any(candidate.get("id") == step_id for candidate in steps)
    sweep_step = next(candidate for candidate in steps if candidate.get("id") == "pr-sweep")
    assert sweep_step["if"] == "always()"
    env = {
        **os.environ, "NAURO_BOT_TOKEN": "test-token", "NAURO_CHAT_ID": "test-chat",
        "OIDC_STATUS": oidc, "AZURE_LOGIN_STATUS": login, "SCAN_STATUS": scan,
        "PR_SWEEP_STATUS": sweep, "AZURE_OIDC_MISSING": missing,
        "RUN_URL": "https://github.com/owner/repo/actions/runs/123",
    }
    for sender_status in (0, 2):
        proc = subprocess.run(
            [bash_shell, "-e", "-c",
             f"bash() {{ printf '%s\\n' \"$@\"; return {sender_status}; }}\n" + step["run"]],
            cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert proc.returncode == sender_status
        assert "scripts/notify-telegram.sh" in proc.stdout
        assert "--plain" in proc.stdout
        for text in expected:
            assert text in proc.stdout
        assert env["RUN_URL"] in proc.stdout
        assert "test-token" not in proc.stdout
    assert step["if"] == (
        "failure() && !(github.event_name == 'workflow_dispatch' && inputs.dry_run)"
    )


@pytest.mark.parametrize("dry_run", ["true", "false"])
def test_workflow_dry_run_reaches_scan_and_pr_sweep(
    tmp_path: Path, bash_shell: str, dry_run: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    data = yaml.safe_load((workflow / "autorefine-health-scan.yml").read_text("utf-8"))
    trigger = data.get("on", data.get(True))
    assert trigger["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"
    assert trigger["workflow_dispatch"]["inputs"]["dry_run"]["default"] is False
    job = data["jobs"]["health-scan"]
    assert job["env"]["AUTOREFINE_DRY_RUN"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}"
    )
    for name in ("Run health scan", "Sweep and card ready Copilot PRs"):
        script = next(step["run"] for step in job["steps"] if step["name"] == name)
        proc = subprocess.run(
            [bash_shell, "-e", "-c", "python() { printf '%s\\n' \"$@\"; }\n" + script],
            cwd=tmp_path, env={**os.environ, "AUTOREFINE_DRY_RUN": dry_run},
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        assert ("--dry-run" in proc.stdout.splitlines()) == (dry_run == "true")


@pytest.mark.parametrize("missing", ["NAURO_BOT_TOKEN", "NAURO_CHAT_ID"])
def test_health_failure_notification_does_not_claim_delivery_without_settings(
    tmp_path: Path, bash_shell: str, missing: str,
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    steps = yaml.safe_load((workflow / "autorefine-health-scan.yml").read_text("utf-8"))[
        "jobs"
    ]["health-scan"]["steps"]
    step = next(step for step in steps if step["name"] == "Failure notification")
    env = {**os.environ, "NAURO_BOT_TOKEN": "test-token", "NAURO_CHAT_ID": "test-chat", missing: ""}
    proc = subprocess.run(
        [bash_shell, "-e", "-c", "bash() { echo sender-was-called; }\n" + step["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "sender-was-called" not in proc.stdout


@pytest.mark.parametrize("sender_status", [0, 22])
def test_shared_sender_disables_previews_and_propagates_delivery_errors(
    tmp_path: Path, bash_shell: str, sender_status: int,
) -> None:
    sender = Path(__file__).resolve().parents[1] / "scripts" / "notify-telegram.sh"
    captured = tmp_path / "curl-arguments"
    stub = (
        'curl() { printf "%s\\n" "$@" > "$CAPTURED_ARGS"; '
        f'return {sender_status}; }}\n'
    )
    proc = subprocess.run(
        [bash_shell, "-c", stub + sender.read_text("utf-8"), "--", "--plain", "stage <unknown>"],
        cwd=tmp_path,
        env={
            **os.environ, "NAURO_BOT_TOKEN": "test-token", "NAURO_CHAT_ID": "test-chat",
            "CAPTURED_ARGS": str(captured),
        },
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == (2 if sender_status else 0)
    arguments = captured.read_text("utf-8").splitlines()
    assert "disable_web_page_preview=true" in arguments
    assert "text=stage <unknown>" in arguments
    assert not any("parse_mode" in argument for argument in arguments)
    assert "test-token" not in proc.stdout + proc.stderr
