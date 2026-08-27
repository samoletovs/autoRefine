"""The entrypoint drift check, exercised against a real ``/proc/1/cmdline`` layout.

``infrastructure/main.bicep`` inlines ``run-autorefine.sh`` with
``loadTextContent()``, so production runs the copy the last deploy captured
while the Python it invokes is cloned fresh from master. When those two fell out
of step nothing failed: #12's cost-telemetry block sat merged and inert for days
and the only symptom was a file that never appeared.

The job is the one place where both copies exist at once — the running text in
``/proc/1/cmdline``, master's in the clone at ``/app`` — so the comparison costs
nothing and rides a run that was happening anyway.

The parsing is the fiddly part, and it is not reasoned about here. These tests
build the real byte layout, ``/bin/sh\\0-c\\0<script>\\0``, from the actual
current script and run the actual block extracted from it, so the thing under
test is the shipped code with two paths substituted rather than a reimplementation
that can drift from it.

**Silence is a result.** Every way of failing to establish an answer — wrong argv
shape, unreadable ``/proc``, a PID 1 that is not this script, a missing clone —
must print nothing at all, because a drift warning that fires when the check
itself is broken is an alarm nobody reads. And nothing here may exit non-zero:
``set -eu`` is on and ``replicaRetryLimit: 1`` would re-run the whole ~2h sweep
because a diagnostic failed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "infrastructure" / "run-autorefine.sh"

BEGIN = "# drift:begin"
END = "# drift:end"

# The literal path assignments the block opens with, and which these tests swap
# for fixtures. Substituting the assignments rather than every mention of the
# paths keeps the rest of the block byte-identical to what ships.
CMDLINE_ASSIGNMENT = "_drift_cmdline=/proc/1/cmdline"
REPO_COPY_ASSIGNMENT = "_drift_repo_copy=/app/infrastructure/run-autorefine.sh"

MATCHED = "entrypoint matches master"
DRIFTED = "ENTRYPOINT DRIFT"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(
    SH is None,
    reason=(
        "no POSIX sh on this machine. CI runs on ubuntu-latest, where sh always "
        "exists, so this never silently disables the suite there."
    ),
)


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _block() -> str:
    """The shipped drift block, verbatim, between its markers."""
    text = _script_text()
    assert BEGIN in text and END in text, (
        f"{SCRIPT.name} no longer carries the {BEGIN!r}/{END!r} markers, so these "
        "tests cannot find the code they exist to verify. If the drift check was "
        "removed, remove these tests deliberately rather than letting them rot."
    )
    body = _script_text().split(BEGIN, 1)[1].split(END, 1)[0]
    return body.split("\n", 1)[1]


def _cmdline(
    script: str,
    *,
    trailing_nul: bool = True,
    argv: tuple[str, ...] = ("/bin/sh", "-c"),
) -> bytes:
    """The byte layout the kernel exposes: NUL-separated argv, usually NUL-terminated."""
    blob = b"\0".join(part.encode("utf-8") for part in (*argv, script))
    return blob + b"\0" if trailing_nul else blob


def _run(
    tmp_path: Path,
    cmdline: bytes | None,
    master: str | None,
) -> subprocess.CompletedProcess[str]:
    cmdline_path = tmp_path / "cmdline"
    if cmdline is not None:
        cmdline_path.write_bytes(cmdline)

    master_path = tmp_path / "master.sh"
    if master is not None:
        master_path.write_bytes(master.encode("utf-8"))

    body = _block()
    assert CMDLINE_ASSIGNMENT in body and REPO_COPY_ASSIGNMENT in body, (
        "the drift block no longer opens with the path assignments these tests "
        "substitute; update CMDLINE_ASSIGNMENT / REPO_COPY_ASSIGNMENT."
    )
    body = body.replace(CMDLINE_ASSIGNMENT, f"_drift_cmdline='{cmdline_path.as_posix()}'")
    body = body.replace(
        REPO_COPY_ASSIGNMENT, f"_drift_repo_copy='{master_path.as_posix()}'"
    )
    assert "/proc/1/cmdline" not in body and "/app/" not in body, (
        "substitution did not take: the block would read the real production "
        "paths and the test would prove nothing."
    )

    # set -eu is what the block actually runs under, and is the whole reason it
    # is wrapped in `|| true`. Testing it without that would test a weaker thing.
    runner = tmp_path / "block.sh"
    runner.write_bytes(("set -eu\n" + body).encode("utf-8"))

    assert SH is not None
    # check=False deliberately: the return code is one of the things under test.
    return subprocess.run(
        [SH, runner.as_posix()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --------------------------------------------------------------------------- #
# It agrees when the copies agree, including where the byte layouts differ.
# --------------------------------------------------------------------------- #


@needs_sh
def test_reports_a_match_when_the_running_copy_is_master(tmp_path: Path) -> None:
    live = _script_text()
    result = _run(tmp_path, _cmdline(live), live)

    assert MATCHED in result.stdout, result
    assert DRIFTED not in result.stdout, result


@needs_sh
def test_a_missing_trailing_nul_is_still_a_match(tmp_path: Path) -> None:
    """The normalisation, stated as a test rather than as a claim.

    ``/proc/PID/cmdline`` is normally NUL-terminated, so the running side gains a
    trailing newline the file does not have. Command substitution strips trailing
    newlines from both sides; this pins that the result does not depend on whether
    the terminator is there.
    """
    live = _script_text()
    result = _run(tmp_path, _cmdline(live, trailing_nul=False), live)

    assert MATCHED in result.stdout, result


@needs_sh
def test_extra_trailing_newlines_on_either_side_are_not_drift(tmp_path: Path) -> None:
    live = _script_text()
    result = _run(tmp_path, _cmdline(live), live + "\n\n\n")

    assert MATCHED in result.stdout, result


# --------------------------------------------------------------------------- #
# It notices the failure it was built for.
# --------------------------------------------------------------------------- #


@needs_sh
def test_reports_drift_when_master_has_moved_on(tmp_path: Path) -> None:
    """The shape of #12: a block appended to master, never deployed."""
    live = _script_text()
    moved_on = live + '\necho "==> a change that was merged but not deployed"\n'

    result = _run(tmp_path, _cmdline(live), moved_on)

    assert DRIFTED in result.stdout, result
    assert MATCHED not in result.stdout, result


@needs_sh
def test_the_drift_message_names_the_fix(tmp_path: Path) -> None:
    """"These differ" is a fact; "redeploy main.bicep" is something to do."""
    live = _script_text()
    result = _run(tmp_path, _cmdline(live), live + "\necho changed\n")

    assert "redeploy infrastructure/main.bicep" in result.stdout, result


# --------------------------------------------------------------------------- #
# Everything it cannot establish, it keeps quiet about.
# --------------------------------------------------------------------------- #


@needs_sh
@pytest.mark.parametrize(
    ("case", "make_cmdline", "master"),
    [
        pytest.param(
            "argv is not `sh -c`",
            lambda live: _cmdline(live, argv=("/usr/bin/python3", "-m")),
            "LIVE",
            id="foreign-argv",
        ),
        pytest.param(
            "PID 1 is a different script",
            lambda live: _cmdline("#!/bin/bash\necho something else\n"),
            "LIVE",
            id="foreign-pid1",
        ),
        pytest.param("cmdline is empty", lambda live: b"", "LIVE", id="empty-cmdline"),
        pytest.param(
            "cmdline is unreadable", lambda live: None, "LIVE", id="absent-cmdline"
        ),
        pytest.param(
            "the clone is missing", lambda live: _cmdline(live), None, id="absent-clone"
        ),
    ],
)
def test_says_nothing_when_it_cannot_establish_an_answer(
    tmp_path: Path, case: str, make_cmdline, master: str | None
) -> None:
    live = _script_text()
    result = _run(tmp_path, make_cmdline(live), live if master == "LIVE" else None)

    assert result.stdout.strip() == "", (
        f"{case}: the check could not establish whether the entrypoint had "
        f"drifted, and said something anyway:\n{result.stdout}\n"
        "A warning that fires when the check itself is broken is an alarm nobody "
        "reads, and silence here is the signal that it could not run."
    )


# --------------------------------------------------------------------------- #
# It can never cost a second sweep.
# --------------------------------------------------------------------------- #


@needs_sh
@pytest.mark.parametrize(
    ("case", "cmdline_kind", "master_kind"),
    [
        ("copies agree", "live", "live"),
        ("copies differ", "live", "moved-on"),
        ("cmdline absent", "absent", "live"),
        ("clone absent", "live", "absent"),
        ("cmdline is rubbish", "rubbish", "live"),
    ],
)
def test_never_exits_non_zero(
    tmp_path: Path, case: str, cmdline_kind: str, master_kind: str
) -> None:
    """``set -e`` is on and ``replicaRetryLimit: 1`` re-runs the entire sweep.

    A diagnostic that can fail the job would buy a second ~2h Foundry pass
    because a comparison went wrong — the same inversion the cost-commit block
    at the bottom of the script is wrapped against.
    """
    live = _script_text()
    cmdline = {
        "live": _cmdline(live),
        "absent": None,
        "rubbish": b"\0\0not an argv at all\0\0",
    }[cmdline_kind]
    master = {"live": live, "moved-on": live + "\necho changed\n", "absent": None}[
        master_kind
    ]

    result = _run(tmp_path, cmdline, master)

    assert result.returncode == 0, (
        f"{case}: the drift block exited {result.returncode}. Under set -e that "
        f"ends the job, and replicaRetryLimit re-runs the whole sweep.\n"
        f"stderr: {result.stderr}"
    )


# --------------------------------------------------------------------------- #
# Guards on the guard. These need no shell.
# --------------------------------------------------------------------------- #


def test_the_shebang_guard_matches_the_real_first_line() -> None:
    """Otherwise the check disables itself, silently and permanently.

    The block refuses to compare unless the running text opens with this exact
    line, which is what stops it shouting when PID 1 turns out to be some other
    ``sh -c``. Change the script's shebang without changing the guard and the
    drift check stops working with no symptom whatsoever — the precise failure
    mode it exists to catch, one level up.
    """
    text = _script_text()
    first_line = text.split("\n", 1)[0]

    assert f"_drift_shebang='{first_line}'" in text, (
        f"the drift check only compares when the running text starts with the "
        f"line in _drift_shebang, but {SCRIPT.name} now starts with "
        f"{first_line!r}. The guard would never match, so the check would go "
        "permanently and silently quiet."
    )


def test_the_check_runs_before_the_sweep() -> None:
    """Reporting a stale entrypoint after the run it invalidated is no use."""
    text = _script_text()

    assert text.index(END) < text.index("python -m agent.main"), (
        "the drift block now sits after the agent invocation, so a stale "
        "entrypoint would be reported at the end of a ~2h sweep it had already "
        "spent. Keep it next to the clone that gives it something to compare."
    )
