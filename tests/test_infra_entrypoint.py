"""Guards for the job entrypoint that ``main.bicep`` bakes into the ARM template.

``infrastructure/main.bicep`` builds the job's command with
``loadTextContent('run-autorefine.sh')``. That inlines the file **at deploy
time**, so production runs the copy captured by the last deployment — while the
Python that copy invokes really is fresh on every run, because the script
git-clones autoRefine at start-up.

Nobody noticed the asymmetry, and the script's own header asserted the opposite
("it always runs whatever is on master"). The cost-telemetry block added in #12
was written, reviewed and merged, and never executed once: the deployed job kept
running the 2,918-character pre-#12 script while the same run pulled the new
Python from master. Nothing failed, no output was missing from anywhere anyone
was looking, and the only symptom was a file that never appeared.

No hermetic test can catch *that*. "Is it deployed" is a fact about Azure, not
about this repository, and the only honest check for it has to call ARM — which
in CI would mean credentials, and without them a skip, and a test that skips in
the one place it matters is the kind this repo has spent effort deleting.

So these pin the properties around the mechanism that *are* checkable, and each
one can fail:

1. every inlined file exists and says in prose that editing it requires a
   redeploy — which catches a second baked-in file added without the warning,
   and catches the warning being tidied away;
2. every inlined file is pinned to LF, because it is inlined verbatim and then
   run by ``/bin/sh`` in a Linux container, and these deployments are issued
   from Windows;
3. CI actually runs on ``infrastructure/`` changes, without which every
   assertion above is decorative — it was missing when this was written.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infrastructure"
BICEP = INFRA_DIR / "main.bicep"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# The sentence a baked-in file has to carry. Deliberately prose rather than a
# marker comment: it has to land on whoever is editing the file, and the person
# who shipped #12 was editing that exact header when they missed it.
REDEPLOY_MARKER = "requires a redeploy to take effect"

# Only a call with a literal argument. Bicep has no other way to name the file,
# and requiring the quotes keeps prose mentions of loadTextContent() — of which
# main.bicep now has one — from being read as a reference to a real path.
LOAD_TEXT_CONTENT = re.compile(r"loadTextContent\(\s*'([^']+)'\s*\)")


def _bicep_code() -> str:
    """``main.bicep`` with whole-line ``//`` comments dropped.

    Only whole-line comments: a trailing ``//`` cannot be stripped safely
    because the template contains ``https://`` inside string literals.
    """
    lines = BICEP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def inlined_files() -> list[str]:
    return sorted(set(LOAD_TEXT_CONTENT.findall(_bicep_code())))


def _gitattributes_rules() -> list[tuple[str, list[str]]]:
    rules: list[tuple[str, list[str]]] = []
    for raw in GITATTRIBUTES.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *attributes = line.split()
        rules.append((pattern, attributes))
    return rules


def _pattern_matches(pattern: str, relative_path: str) -> bool:
    """Approximate gitattributes matching: no slash means match at any depth."""
    target = relative_path if "/" in pattern else relative_path.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(target, pattern)


def _workflow_triggers(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads a bare `on:` key as the boolean True, not the string.
    return document.get("on") or document.get(True) or {}


def test_the_template_still_bakes_something_in() -> None:
    """Without this the parametrised tests below collect nothing and pass.

    If the entrypoint ever stops being inlined — a bootstrap that clones and
    execs the real script would do it — that is a deliberate change to the
    deployment model, and it should arrive here rather than quietly emptying
    every guard in this file.
    """
    assert inlined_files(), (
        "no loadTextContent('...') call found in infrastructure/main.bicep, so "
        "every test in this module would silently guard nothing. If the job "
        "entrypoint is no longer baked into the template, delete or rewrite "
        "these guards on purpose."
    )


@pytest.mark.parametrize("name", inlined_files(), ids=inlined_files())
def test_inlined_file_exists(name: str) -> None:
    assert (INFRA_DIR / name).is_file(), (
        f"main.bicep inlines '{name}', but infrastructure/{name} does not exist. "
        "loadTextContent resolves at compile time, so this fails the deployment "
        "rather than the job."
    )


@pytest.mark.parametrize("name", inlined_files(), ids=inlined_files())
def test_inlined_file_says_it_is_baked_at_deploy_time(name: str) -> None:
    path = INFRA_DIR / name
    if not path.is_file():
        pytest.fail(f"infrastructure/{name} is missing; see test_inlined_file_exists")

    assert REDEPLOY_MARKER in path.read_text(encoding="utf-8"), (
        f"infrastructure/{name} is inlined into the ARM template by main.bicep, "
        f"so production runs the copy from the last deployment — but the file "
        f"never says so. It must contain the phrase {REDEPLOY_MARKER!r}.\n\n"
        "This is not bookkeeping: the previous header claimed the opposite, and "
        "a cost-telemetry block was merged and left inert in production for days "
        "because its author believed it."
    )


@pytest.mark.parametrize("name", inlined_files(), ids=inlined_files())
def test_inlined_file_is_pinned_to_lf(name: str) -> None:
    relative_path = f"{INFRA_DIR.name}/{name}"

    eol = None
    for pattern, attributes in _gitattributes_rules():
        if not _pattern_matches(pattern, relative_path):
            continue
        for attribute in attributes:
            if attribute.startswith("eol="):
                eol = attribute  # last matching rule wins, as git does it

    assert eol == "eol=lf", (
        f"{relative_path} is inlined verbatim into the ARM template and then run "
        f"by /bin/sh in a Linux container, but .gitattributes resolves its line "
        f"endings to {eol or 'nothing in particular'}. A CRLF checkout — the "
        "default on the Windows machines these deployments are issued from — "
        "would put a carriage return on the end of every line and fail the job "
        "in a way that looks like anything but a line-ending problem."
    )


@pytest.mark.parametrize("event", ("push", "pull_request"))
def test_ci_runs_on_infrastructure_changes(event: str) -> None:
    """The path filter is what makes every other test in this file real.

    tests.yml is path-filtered because the repo is private and its minutes come
    out of the monthly allowance. ``infrastructure/**`` was not in that filter,
    so a PR touching only the bicep and the entrypoint ran no tests at all.
    """
    paths = (_workflow_triggers(TESTS_WORKFLOW).get(event) or {}).get("paths") or []

    assert any(entry.startswith(f"{INFRA_DIR.name}/") for entry in paths), (
        f"tests.yml does not run on '{event}' for {INFRA_DIR.name}/ changes, so "
        "the guards in this module never execute on a PR that only touches the "
        f"deployment template. Add '{INFRA_DIR.name}/**' to that path filter.\n"
        f"  current paths: {paths}"
    )
