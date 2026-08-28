"""A project's test suite must not inherit autoRefine's credentials.

``_handle_run_tests`` runs arbitrary code from someone else's repository as our child.
Until this module existed that child received our entire environment minus one prefix,
which in production is every credential the job holds: ``GH_TOKEN`` and ``GITHUB_TOKEN``
(one org-wide PAT under two names), ``NAURO_BOT_TOKEN``, and the Container Apps managed
identity pair ``IDENTITY_ENDPOINT`` / ``IDENTITY_HEADER``. That pair is the sharpest
case. Azure injects it, no file in this repository has ever named it, and per Microsoft's
own documentation it is sufficient to mint tokens for the job's identity -- which holds
Key Vault Secrets User on the vault storing the PAT. A deny-list cannot withhold a
variable nobody knows exists; an allow-list withholds it without knowing.

Values here are synthetic. These tests assert on the presence or absence of a *key* and
never on a value, because a test that printed one on failure would be the same leak in a
different channel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import foundry_agent

# Named exactly as production sets them. The first three come from the container's `env`
# block in infrastructure/main.bicep; GH_TOKEN and GITHUB_TOKEN are the same Key Vault
# secret under two names. The next two are injected by Azure Container Apps itself and
# appear in no file in this repository. MSI_SECRET is the older App Service spelling of
# the same idea, pinned defensively rather than because this platform sets it.
PRODUCTION_SECRETS = [
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "NAURO_BOT_TOKEN",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "MSI_SECRET",
]
# Not credentials, but still ours: three from main.bicep and one exported by
# infrastructure/run-autorefine.sh.
PRODUCTION_NON_SECRET_BUT_OURS = [
    "NAURO_CHAT_ID",
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_SUBSCRIPTION_ID",
    "NAURO_GOVERNANCE_PATH",
]


class _Recorder:
    """Stands in for ``subprocess.run`` and exposes the ``env`` a child would receive."""

    def __init__(self) -> None:
        self.env: dict[str, str] | None = None

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.env = kwargs.get("env")  # type: ignore[assignment]
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")


def _python_project(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return tmp_path


class TestCredentialsDoNotReachTheChild:
    """The mutation check. Every assertion here fails against the previous deny-list."""

    @pytest.mark.parametrize("name", PRODUCTION_SECRETS)
    def test_each_production_credential_is_withheld(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "synthetic-not-a-real-credential")

        env = foundry_agent._test_subprocess_env()

        assert name not in env, f"{name} reached a project's test suite"

    @pytest.mark.parametrize("name", PRODUCTION_NON_SECRET_BUT_OURS)
    def test_our_own_configuration_is_withheld_too(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not secret, but ours: a project's suite has no business reading our wiring.

        ``FOUNDRY_PROJECT_ENDPOINT`` is the one with a measured reader in the fleet
        (foundryLab), which is why it is pinned rather than assumed harmless.
        """
        monkeypatch.setenv(name, "synthetic-value")

        assert name not in foundry_agent._test_subprocess_env()

    def test_an_unknown_credential_shaped_variable_is_withheld(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property a deny-list cannot have: safe by default for the next variable.

        Nothing in this repository knows this name, which is precisely the situation the
        Container Apps identity pair was already in.
        """
        monkeypatch.setenv("SOME_FUTURE_SERVICE_TOKEN", "synthetic")

        assert "SOME_FUTURE_SERVICE_TOKEN" not in foundry_agent._test_subprocess_env()

    def test_the_child_reaches_it_through_the_real_tool_entrypoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards the wiring, not just the helper: the tool must use the narrowed env."""
        monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-real-credential")
        rec = _Recorder()
        monkeypatch.setattr(subprocess, "run", rec)

        foundry_agent._handle_run_tests(_python_project(tmp_path), {})

        assert rec.env is not None, "an explicit env must be passed, not inherited"
        assert "GH_TOKEN" not in rec.env


class TestTheChildCanStillRun:
    """An allow-list that is too narrow breaks every suite, which is the real risk."""

    def test_the_interpreter_can_be_found(self) -> None:
        env = foundry_agent._test_subprocess_env()
        assert "PATH" in env, "without PATH there is no python, no npm and no git"

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only start-up requirements")
    def test_windows_machinery_survives(self) -> None:
        """A child Python does not start on Windows without these.

        The production job is ``python:3.12`` on Linux and this is a Windows box, so a
        list that satisfied only production would pass review and fail on the machine the
        suite is run on.
        """
        env = foundry_agent._test_subprocess_env()
        for name in ("SYSTEMROOT", "PATHEXT"):
            assert name in env, f"{name} is required to start a child process on Windows"

    def test_locale_and_proxy_names_are_matched_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX tooling reads ``https_proxy`` as readily as ``HTTPS_PROXY``."""
        monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")

        env = foundry_agent._test_subprocess_env()

        assert any(k.upper() == "HTTPS_PROXY" for k in env)

    def test_the_allow_list_is_not_vacuously_empty(self) -> None:
        """Anti-vacuity: an emptied list would make every test above pass for free."""
        assert len(foundry_agent.TEST_ENV_ALLOWED) > 20

    @pytest.mark.parametrize("name", ["NODE_AUTH_TOKEN", "npm_config__auth"])
    def test_token_shaped_toolchain_variables_are_not_swept_in(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the Node entries are named one by one instead of by a ``NODE_`` prefix."""
        monkeypatch.setenv(name, "synthetic")

        assert name not in foundry_agent._test_subprocess_env()


class TestControlVariablesStayStripped:
    """The bug this function was originally written for must not regress."""

    def test_autorefine_prefix_is_still_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/x.jsonl")
        monkeypatch.setenv("AUTOREFINE_TIER", "high")

        env = foundry_agent._test_subprocess_env()

        assert [k for k in env if k.upper().startswith("AUTOREFINE_")] == []

    def test_the_passthrough_hatch_cannot_re_admit_a_control_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The belt-and-braces strip earns its place here rather than being redundant.

        ``AUTOREFINE_TIER`` is exactly what someone adds when a project asks for it. The
        unconditional strip means that edit cannot silently reopen the path that
        corrupted the first cost file the pipeline ever wrote.
        """
        monkeypatch.setenv("AUTOREFINE_COST_LOG", "/tmp/x.jsonl")
        monkeypatch.setenv(
            foundry_agent.TEST_ENV_PASSTHROUGH_ENV, "AUTOREFINE_COST_LOG,AUTOREFINE_TIER"
        )

        env = foundry_agent._test_subprocess_env()

        assert "AUTOREFINE_COST_LOG" not in env
        assert "AUTOREFINE_TIER" not in env


class TestOperatorEscapeHatch:
    """A suite broken by an over-narrow list must be fixable without changing this code."""

    def test_a_named_variable_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NEEDS_THIS", "keep-me")
        monkeypatch.setenv(foundry_agent.TEST_ENV_PASSTHROUGH_ENV, "PROJECT_NEEDS_THIS")

        assert "PROJECT_NEEDS_THIS" in foundry_agent._test_subprocess_env()

    def test_without_the_hatch_the_same_variable_is_withheld(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NEEDS_THIS", "keep-me")

        assert "PROJECT_NEEDS_THIS" not in foundry_agent._test_subprocess_env()

    def test_whitespace_and_empty_entries_are_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NEEDS_THIS", "keep-me")
        monkeypatch.setenv(
            foundry_agent.TEST_ENV_PASSTHROUGH_ENV, " , PROJECT_NEEDS_THIS , "
        )

        assert "PROJECT_NEEDS_THIS" in foundry_agent._test_subprocess_env()


class TestCallerEnvironmentIsUntouched:
    def test_os_environ_is_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-real-credential")

        foundry_agent._test_subprocess_env()

        assert "GH_TOKEN" in os.environ, "the caller's own environment must be left alone"


class TestAgainstARealChildProcess:
    """Mocks have agreed with a false belief in this repository before.

    ``_Recorder`` proves what env we *pass*. It cannot prove a real interpreter starts
    with it, and "the allow-list is too narrow to run anything" is the failure mode that
    matters most. So this spawns a genuine ``python -m pytest`` through the real tool
    handler, against a real project layout, and has the child report its own environment
    back through a file.
    """

    @staticmethod
    def _project_that_reports_its_env(tmp_path: Path) -> tuple[Path, Path]:
        project = tmp_path / "someone-elses-project"
        (project / "tests").mkdir(parents=True)
        (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        # Pins rootdir so the child cannot discover config from outside the fixture.
        (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        dump = project / "child-env-keys.json"
        (project / "tests" / "test_report_env.py").write_text(
            "import json, os, pathlib\n"
            "def test_report():\n"
            "    pathlib.Path(__file__).parent.parent.joinpath('child-env-keys.json')"
            ".write_text(json.dumps(sorted(os.environ)), encoding='utf-8')\n",
            encoding="utf-8",
        )
        return project, dump

    def test_a_real_suite_runs_and_sees_no_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-real-credential")
        monkeypatch.setenv("NAURO_BOT_TOKEN", "synthetic-not-a-real-credential")
        monkeypatch.setenv("IDENTITY_HEADER", "synthetic-not-a-real-credential")
        # The interpreter running this suite, not whichever one PATH happens to find.
        monkeypatch.setattr(
            foundry_agent.subprocess,
            "run",
            _pinned_interpreter(foundry_agent.subprocess.run),
        )
        project, dump = self._project_that_reports_its_env(tmp_path)

        payload = json.loads(foundry_agent._handle_run_tests(project, {}))

        assert payload.get("passed") is True, (
            "the narrowed environment must still be able to run a real suite; "
            f"output was: {payload.get('output') or payload.get('error')}"
        )
        assert dump.exists(), "the child never ran, so it proves nothing about the env"
        child_keys = {k.upper() for k in json.loads(dump.read_text(encoding="utf-8"))}
        assert "PATH" in child_keys
        for name in ("GH_TOKEN", "NAURO_BOT_TOKEN", "IDENTITY_HEADER"):
            assert name not in child_keys, f"{name} reached a real child process"


def _pinned_interpreter(real_run: object):
    """Rewrite a bare ``python`` argv[0] to this interpreter.

    ``_handle_run_tests`` runs ``python -m pytest``, which resolves through PATH. On a
    machine whose ``python`` is a different install -- or the Windows Store shim -- the
    child would fail for a reason unrelated to the environment under test, turning a real
    check into a flaky one. Only argv[0] is touched; the env, cwd and decode settings
    stay exactly as production builds them.
    """

    def run(cmd: list[str], **kwargs: object):
        if cmd and cmd[0] == "python":
            cmd = [sys.executable, *cmd[1:]]
        return real_run(cmd, **kwargs)  # type: ignore[operator]

    return run
