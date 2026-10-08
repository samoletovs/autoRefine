"""The classic Foundry Agents surface must not come back.

Azure retires the classic Agent Service APIs (``azure-ai-agents``: threads, runs,
``create_agent``) on 2027-03-31. autoRefine moved to versioned prompt agents driven
through the Responses API; these guards fail if any code imports the old package
again or a dependency manifest starts installing it.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
CLASSIC_MODULE = "azure.ai.agents"
CLASSIC_PACKAGE = "azure-ai-agents"


def _python_sources() -> list[Path]:
    return sorted((ROOT / "agent").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_sources_are_scanned_at_all() -> None:
    sources = _python_sources()
    assert ROOT / "agent" / "foundry_agent.py" in sources
    assert ROOT / "agent" / "main.py" in sources


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_source_imports_the_classic_agents_sdk(path: Path) -> None:
    classic = {
        module for module in _imported_modules(path)
        if module == CLASSIC_MODULE or module.startswith(CLASSIC_MODULE + ".")
    }
    assert not classic, f"{path.name} imports the retired classic SDK: {sorted(classic)}"
    # Also catches importlib / monkeypatch-by-string style references.
    assert CLASSIC_MODULE not in path.read_text(encoding="utf-8")


def _requirement_names(lines: list[str]) -> dict[str, Requirement]:
    requirements = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            requirement = Requirement(stripped)
            requirements[requirement.name.lower()] = requirement
    return requirements


def _manifests() -> dict[str, dict[str, Requirement]]:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return {
        "pyproject.toml": _requirement_names(metadata["project"]["dependencies"]),
        "requirements.txt": _requirement_names(requirements),
    }


@pytest.mark.parametrize("manifest", ["pyproject.toml", "requirements.txt"])
def test_manifests_install_the_new_agent_service_sdk_not_the_classic_one(manifest: str) -> None:
    requirements = _manifests()[manifest]

    assert CLASSIC_PACKAGE not in requirements
    projects = requirements["azure-ai-projects"]
    assert projects.specifier.contains("2.1.0")
    assert not projects.specifier.contains("1.0.0")
    assert not projects.specifier.contains("3.0.0")
    openai = requirements["openai"]
    assert openai.specifier.contains("2.0.0")
    assert not openai.specifier.contains("1.99.0")


def test_installed_sdk_offers_the_agent_version_api_this_code_drives() -> None:
    from azure.ai.projects.operations import AgentsOperations

    for method in ("create_version", "list_versions"):
        assert hasattr(AgentsOperations, method)
