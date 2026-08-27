"""Hardening guards for the workflows, and for the evaluate workflow in particular.

Three separate hazards, all of which the evaluate workflow shipped with:

1. **Script injection.** ``${{ github.event.inputs.repo }}`` was spliced straight
   into a ``run:`` block. Actions substitutes expressions *textually, before bash
   parses the line*, so the value is not an argument — it is syntax. Quoting at
   the call site cannot help, because the quotes are inserted after the payload
   is. The fix is to pass untrusted values through ``env:``, where they are only
   ever data. ``pr-merged-notify.yml`` already did it that way.

2. **Unbounded runtime.** The job had no ``timeout-minutes``, so it inherited
   GitHub's 6-hour default. This job's documented failure mode is hanging on a
   Foundry call, not crashing, and AGENTS.md records it as the fleet's largest
   consumer of Actions minutes at ~43 min/run.

3. **Unbounded concurrency.** No ``concurrency`` group, so two dispatches ran
   two full Foundry sweeps at once — double the bill for one answer.

4. **Authentication that only fails at runtime.** Both agent workflows read a
   ``creds:`` secret named ``AZURE_CREDENTIALS`` that had never been created.
   Actions substitutes a missing secret as the empty string rather than
   erroring, so ``azure/login`` fell through to service-principal auth with
   nothing to use and every dispatch died at the login step. Nothing in the
   repository could have caught that, because the mistake was a *valid* YAML
   reference to a name that did not exist. They now authenticate with OIDC,
   which has two preconditions that fail the same silent way — see
   ``AZURE_LOGIN_STEPS`` below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The jobs that must not be able to run for GitHub's 6-hour default.
#
# Listed, not derived from "runs the agent". autorefine-health-scan.yml runs the
# agent too and is deliberately absent: every one of its runs has been skipped
# since AUTOREFINE_TIER went to 'critical', so there is no observed duration to
# size a ceiling from, and a guessed one would risk killing a legitimate scan.
# That is a known gap awaiting data, not an oversight — add it here once a real
# run records a duration.
TIME_BOUNDED_WORKFLOWS = ("autorefine-evaluate.yml", "pr-ready-cards.yml")

# The one that holds a Foundry agent open for the whole run, so the one where two
# at once is two bills for one answer.
EVALUATE_WORKFLOW = WORKFLOW_DIR / "autorefine-evaluate.yml"

# The evaluate sweep is ~43 min (AGENTS.md); nothing here should approach this.
# Above it is not a slow day, it is a hang — and a hang is also how a Foundry
# agent gets orphaned, since a hard kill never reaches main.py's `finally`.
MAX_REASONABLE_TIMEOUT_MINUTES = 180

# Contexts an outsider (or a mistyped dispatch input) can put arbitrary text
# into. Sourced from GitHub's "Security hardening for GitHub Actions" guidance.
UNTRUSTED_EXPRESSION = re.compile(
    r"\$\{\{[^}]*\b(?:"
    r"github\.event\.inputs\.[\w.-]+"
    r"|inputs\.[\w.-]+"
    r"|github\.head_ref"
    r"|github\.event\.(?:issue|discussion)\.(?:title|body)"
    r"|github\.event\.pull_request\.(?:title|body)"
    r"|github\.event\.pull_request\.head\.(?:ref|label)"
    r"|github\.event\.(?:comment|review)\.body"
    r"|github\.event\.head_commit\.(?:message|author\.\w+)"
    r")\b[^}]*\}\}"
)


def workflow_files() -> list[Path]:
    return sorted(
        [*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")],
        key=lambda p: p.name,
    )


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_steps(document: dict):
    """Yield ``(job_name, step_label, run_script)`` for every shell step."""
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if isinstance(script, str):
                label = (step or {}).get("name") or f"step #{index + 1}"
                yield job_name, label, script


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_untrusted_input_is_never_interpolated_into_a_run_block(path: Path) -> None:
    offenders: list[str] = []

    for job_name, label, script in _run_steps(_load(path)):
        for match in UNTRUSTED_EXPRESSION.finditer(script):
            offenders.append(f"{job_name} / {label}: {match.group(0)}")

    assert not offenders, (
        f"{path.name} interpolates attacker-controllable values directly into a "
        "'run:' script, where they become shell syntax rather than data. Pass them "
        "through 'env:' and reference them as \"$VAR\" instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("workflow_name", TIME_BOUNDED_WORKFLOWS)
def test_job_is_time_bounded(workflow_name: str) -> None:
    """Without this the ceiling is GitHub's 6-hour default, not anything chosen."""
    path = WORKFLOW_DIR / workflow_name
    jobs = _load(path)["jobs"]

    for job_name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        assert timeout is not None, (
            f"job '{job_name}' in {workflow_name} has no 'timeout-minutes', "
            "so a hung call burns the 6-hour default before GitHub steps in."
        )
        assert isinstance(timeout, int) and 0 < timeout <= MAX_REASONABLE_TIMEOUT_MINUTES, (
            f"job '{job_name}' timeout of {timeout!r} is not a sane bound "
            f"(expected 1..{MAX_REASONABLE_TIMEOUT_MINUTES} minutes)."
        )


def test_evaluate_workflow_serialises_its_runs() -> None:
    concurrency = _load(EVALUATE_WORKFLOW).get("concurrency")

    assert concurrency, (
        f"{EVALUATE_WORKFLOW.name} has no 'concurrency' group, so two dispatches "
        "run two full Foundry sweeps at once — twice the spend for one answer."
    )
    assert concurrency.get("group"), "concurrency needs a 'group'"


def test_evaluate_workflow_does_not_cancel_runs_in_progress() -> None:
    """Cancelling here would trade an Actions minute for a leaked Foundry agent.

    ``agent/main.py`` deletes its ephemeral agent in a ``finally`` block. A
    cancelled job is a hard kill, so that block never runs and the agent survives
    until the next run's ``sweep_orphaned_agents`` clears it (AGENTS.md).
    """
    concurrency = _load(EVALUATE_WORKFLOW).get("concurrency") or {}

    assert concurrency.get("cancel-in-progress") is not True, (
        "cancel-in-progress must not be true: a cancelled evaluate run never "
        "reaches the 'finally' that deletes its Foundry agent."
    )


# ── Azure OIDC login ───────────────────────────────────────────────────────
#
# The action ref is matched case-insensitively because GitHub resolves it that
# way: ``Azure/login`` and ``azure/login`` are the same action, and a guard that
# saw only one spelling would silently stop covering a renamed step.
AZURE_LOGIN_ACTION = "azure/login"


def _azure_login_steps(document: dict):
    """Yield ``(job_name, step_label, step, effective_permissions)``.

    A job-level ``permissions`` block *replaces* the workflow-level one outright
    — GitHub does not merge the two — so the effective set is the job's if it
    declares one and the workflow's otherwise.
    """
    workflow_permissions = document.get("permissions")

    for job_name, job in (document.get("jobs") or {}).items():
        job = job or {}
        permissions = job.get("permissions", workflow_permissions)

        for index, step in enumerate(job.get("steps") or []):
            step = step or {}
            uses = str(step.get("uses") or "")
            if uses.split("@", 1)[0].lower() == AZURE_LOGIN_ACTION:
                label = step.get("name") or f"step #{index + 1}"
                yield job_name, label, step, permissions


def _discover_azure_login_steps() -> list[tuple]:
    return [
        (path, job_name, label, step, permissions)
        for path in workflow_files()
        for job_name, label, step, permissions in _azure_login_steps(_load(path))
    ]


AZURE_LOGIN_STEPS = _discover_azure_login_steps()


def _login_ids(entries: list[tuple]) -> list[str]:
    return [f"{path.name}::{job_name}::{label}" for path, job_name, label, _, _ in entries]


def test_azure_login_steps_are_discoverable() -> None:
    """Guard the guard: the tests below are parametrised over this list.

    If discovery stops matching — an action rename, a ref this helper does not
    split correctly — it collects nothing and every assertion below passes by
    finding no work to do. That is precisely the failure mode this module
    exists to prevent, so make it loud.
    """
    assert AZURE_LOGIN_STEPS, (
        f"no '{AZURE_LOGIN_ACTION}' steps found. Either the workflows stopped "
        "authenticating to Azure — in which case delete these tests deliberately "
        "— or _azure_login_steps() stopped recognising them."
    )


@pytest.mark.parametrize(
    ("path", "job_name", "label", "step", "permissions"),
    AZURE_LOGIN_STEPS,
    ids=_login_ids(AZURE_LOGIN_STEPS),
)
def test_azure_login_job_may_mint_an_oidc_token(
    path: Path, job_name: str, label: str, step: dict, permissions: object
) -> None:
    """OIDC needs ``id-token: write``, and says so only once the job is running.

    ``azure/login`` calls ``core.getIDToken()``, which needs the job to have been
    granted ``id-token: write``. Without it the run fails inside the action with
    "Failed to fetch federated token from GitHub" — long after the point where a
    test could have said so for free.
    """
    assert isinstance(permissions, dict), (
        f"{path.name} / job '{job_name}' declares permissions {permissions!r}. "
        "These jobs carry a deliberately restrictive block; keep it an explicit "
        "mapping and add 'id-token: write' to it rather than widening the lot."
    )
    assert permissions.get("id-token") == "write", (
        f"{path.name} / job '{job_name}' runs '{label}' but does not grant "
        "'id-token: write', so azure/login cannot fetch a federated token from "
        "GitHub and the step fails before it ever reaches Entra ID."
    )


@pytest.mark.parametrize(
    ("path", "job_name", "label", "step", "permissions"),
    AZURE_LOGIN_STEPS,
    ids=_login_ids(AZURE_LOGIN_STEPS),
)
def test_azure_login_supplies_its_identity_inputs(
    path: Path, job_name: str, label: str, step: dict, permissions: object
) -> None:
    """The shipped bug, in test form.

    ``creds: ${{ secrets.AZURE_CREDENTIALS }}`` named a secret that did not
    exist. Actions renders a missing secret as the empty string, so the action
    saw no credentials at all and raised "Using auth-type: SERVICE_PRINCIPAL.
    Not all values are present. Ensure 'client-id' and 'tenant-id' are
    supplied." Asserting the inputs are present catches that shape of mistake
    without a dispatch — though note that no test can tell whether the *secrets*
    behind them exist, which is why the workflow headers say so in prose.

    The subscription rule mirrors the action's own ``LoginConfig.validate()``:
    a subscription is required unless ``allow-no-subscriptions`` is set.
    """
    inputs = step.get("with") or {}

    assert "creds" not in inputs, (
        f"{path.name} / '{label}' passes 'creds:'. These workflows authenticate "
        "with OIDC federated credentials so that no long-lived secret exists to "
        "rotate or leak; going back to a credentials blob is a deliberate "
        "decision, not an edit that should pass quietly."
    )

    missing = [
        key for key in ("client-id", "tenant-id") if not str(inputs.get(key, "")).strip()
    ]
    assert not missing, (
        f"{path.name} / '{label}' is missing {missing!r}. azure/login fails with "
        "\"Ensure 'client-id' and 'tenant-id' are supplied\"."
    )

    if not str(inputs.get("subscription-id", "")).strip():
        assert str(inputs.get("allow-no-subscriptions", "")).lower() == "true", (
            f"{path.name} / '{label}' supplies neither 'subscription-id' nor "
            "'allow-no-subscriptions: true'; azure/login requires one of them."
        )


# Steps that take minutes rather than seconds. Under OIDC the login step starts a
# clock that these would burn: GitHub's identity token expires 5 minutes after it
# is minted, and the Azure CLI stores that token verbatim and cannot mint another
# (Azure/azure-cli#28708, open since 2024-04-08). `az login` itself only caches an
# ARM token, so the *first* request for any other scope — the Foundry/AI scope the
# agent needs — must land inside those 5 minutes or it fails with AADSTS700024.
#
# The pre-warm step now takes those tokens up front, so the window has to survive
# only until the step right after the login rather than until the agent's first
# call. Both guards still matter: this one keeps the window short, and
# ``test_azure_login_is_followed_by_a_prewarm`` keeps the pre-warm there at all.
SLOW_SETUP_ACTIONS = ("actions/checkout", "actions/setup-python", "actions/setup-node")
SLOW_SETUP_COMMANDS = ("pip install", "npm install", "npm ci", "apt-get install")


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_no_slow_setup_runs_after_azure_login(path: Path) -> None:
    """Keep the slow steps before the login, not after it.

    Moving ``pip install`` below ``azure/login`` would not fail here, or in
    review, or in any run that happens to be quick. It would fail intermittently
    and much later, inside the agent, with an Entra error naming a clock rather
    than a workflow edit.
    """
    document = _load(path)
    offenders: list[str] = []

    for job_name, job in (document.get("jobs") or {}).items():
        seen_login = False

        for index, step in enumerate((job or {}).get("steps") or []):
            step = step or {}
            uses = str(step.get("uses") or "")
            action = uses.split("@", 1)[0].lower()
            label = step.get("name") or f"step #{index + 1}"

            if action == AZURE_LOGIN_ACTION:
                seen_login = True
                continue
            if not seen_login:
                continue

            script = step.get("run") if isinstance(step.get("run"), str) else ""
            slow = action in SLOW_SETUP_ACTIONS or any(
                command in script for command in SLOW_SETUP_COMMANDS
            )
            if slow:
                offenders.append(f"{job_name} / {label}")

    assert not offenders, (
        f"{path.name} runs slow setup work after 'azure/login', which spends the "
        "5-minute federated-token window before the agent asks for its first "
        "non-ARM token (Azure/azure-cli#28708). Move these above the login:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("path", workflow_files(), ids=_ids(workflow_files()))
def test_azure_login_is_followed_by_a_prewarm(path: Path) -> None:
    """Every ``azure/login`` must be followed straight away by a token pre-warm.

    Deleting the pre-warm looks harmless: the evaluate sweep would still pass,
    because its agent starts seconds later and lands inside the window anyway.
    It would keep passing until a dependency install got slower or a step moved,
    and then fail as an Entra error deep inside a 43-minute run.

    What this cannot check is whether the *list of resources* is complete. A
    client's scope is usually an SDK default rather than a string in our source
    — ``AgentsClient``'s ``https://ai.azure.com`` is not written down anywhere in
    this repository — so a guard that tried to derive the list would be guessing,
    and a guard that hard-coded it would just be the workflow again. Adding a new
    Azure service means adding its resource by hand.
    """
    document = _load(path)
    offenders: list[str] = []

    for job_name, job in (document.get("jobs") or {}).items():
        steps = (job or {}).get("steps") or []

        for index, step in enumerate(steps):
            uses = str((step or {}).get("uses") or "")
            if uses.split("@", 1)[0].lower() != AZURE_LOGIN_ACTION:
                continue

            follower = steps[index + 1] if index + 1 < len(steps) else {}
            script = (follower or {}).get("run")
            script = script if isinstance(script, str) else ""
            label = (step or {}).get("name") or f"step #{index + 1}"

            if "get-access-token" not in script:
                offenders.append(
                    f"{job_name} / after '{label}': no pre-warm step follows the login"
                )
                continue
            if "--output none" not in script:
                offenders.append(
                    f"{job_name} / after '{label}': pre-warm must pass '--output none', "
                    "or it prints access tokens into the run log"
                )
            fails_open = "||" in script or (follower or {}).get("continue-on-error") is True
            if not fails_open:
                offenders.append(
                    f"{job_name} / after '{label}': pre-warm can fail the job. It is a "
                    "diagnostic; it must not kill the work it protects"
                )

    assert not offenders, (
        f"{path.name} has an azure/login whose token pre-warm is missing or unsafe "
        "(Azure/azure-cli#28708):\n  " + "\n  ".join(offenders)
    )

