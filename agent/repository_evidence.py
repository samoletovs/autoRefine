"""Bounded repository context for functional suggestions."""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent.config import ProjectConfig

_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs",
             ".html", ".md", ".vue", ".svelte"}
_EXCLUDED = {"node_modules", "vendor", "dist", "build", "coverage", "__pycache__", "venv"}
_STOP_WORDS = {"improve", "support", "create", "provide", "users", "using", "features",
               "feature", "product", "application", "project", "enable", "with", "from"}
_SENSITIVE = re.compile(
    r"(?i)(secret|password|credential|api[_-]?key|access[_-]?token|private[_-]?key|bearer\s)"
)


def repository_inventory(project_dir: Path, config: ProjectConfig) -> list[dict[str, str]]:
    """Return short matching source lines, not assertions that a gap exists."""
    terms = {
        word for text in [*getattr(config, "goals", []), *getattr(config, "similar", [])]
        for word in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(word) >= 4 and word not in _STOP_WORDS
    }
    if not terms:
        return []
    inventory: list[dict[str, str]] = []
    scanned = 0
    for visited, (root, dirs, files) in enumerate(os.walk(project_dir, followlinks=False), 1):
        if visited > 500:
            return inventory
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in _EXCLUDED
            and not (Path(root) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(root) / name
            if (name.startswith(".") or path.suffix.lower() not in _SUFFIXES
                    or _SENSITIVE.search(name) or path.is_symlink()):
                continue
            scanned += 1
            if scanned > 500:
                return inventory
            try:
                with path.open("rb") as source:
                    data = source.read(65537)
                if len(data) > 65536 or b"\0" in data:
                    continue
                lines = data.decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                excerpt = line.strip()
                words = set(re.findall(r"[a-z0-9]+", excerpt.lower()))
                if terms & words and excerpt and not _SENSITIVE.search(excerpt):
                    inventory.append({
                        "path": path.relative_to(project_dir).as_posix(),
                        "excerpt": excerpt[:200],
                    })
                    break
            if len(inventory) >= 8:
                return inventory
    return inventory


def matching_evidence(text: str, inventory: list[dict[str, str]]) -> list[dict[str, str]]:
    """Select inventory entries sharing meaningful terms with a goal or product."""
    terms = {
        word for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) >= 4 and word not in _STOP_WORDS
    }
    return [
        entry for entry in inventory
        if terms & set(re.findall(r"[a-z0-9]+", entry["excerpt"].lower()))
    ][:2]


def verify_plan_evidence(plan: dict, inventory: list[dict[str, str]]) -> None:
    """Replace model citations with inventory excerpts; missing citations stay unverified."""
    by_path = {entry["path"]: entry for entry in inventory}
    for improvement in plan.get("improvements", []):
        if not isinstance(improvement, dict):
            continue
        citations = improvement.get("evidence")
        if not isinstance(citations, list):
            citations = []
        paths = {
            entry.get("path") for entry in citations
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        evidence = [entry for path, entry in by_path.items() if path in paths]
        improvement["evidence"] = evidence
        improvement["evidence_status"] = "repository-context" if evidence else "unverified"
