"""Package installs must retain the runtime dependencies used by the deployed job."""

import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement


def test_runtime_dependencies_match_the_job_requirements() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime, separator, _dev = (root / "requirements.txt").read_text(
        encoding="utf-8"
    ).partition("# Dev tools")
    assert separator, "Keep the runtime/dev boundary explicit in requirements.txt"
    job_dependencies = {
        Requirement(line.strip())
        for line in runtime.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    package_dependencies = {
        Requirement(dependency) for dependency in metadata["project"]["dependencies"]
    }

    assert package_dependencies == job_dependencies


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_package_metadata_changes_run_ci(event: str) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    triggers = document.get("on") or document.get(True)

    assert "pyproject.toml" in triggers[event]["paths"]
