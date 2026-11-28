"""Bounded repository context for functional suggestions."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from agent.config import ProjectConfig

_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs",
             ".html", ".md", ".vue", ".svelte"}
_EXCLUDED = {"node_modules", "vendor", "dist", "build", "coverage", "__pycache__", "venv"}
_STOP_WORDS = {"improve", "support", "create", "provide", "users", "using", "features",
               "feature", "product", "application", "project", "enable", "with", "from"}
_SENSITIVE = re.compile(
    r"(?i)(secret|password|credential|api[_-]?key|access[_-]?token|private[_-]?key|bearer\s|"
    r"[a-z][a-z0-9+.-]*://|[a-z0-9+/=_-]{32,})"
)
_DECLARATION = re.compile(
    r"^(?:(?:export|default|public|private|async|static|abstract)\s+)*"
    r"(?:def|class|function|func|fn|interface|struct|type|const|let|var)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*"
)
_METHOD = re.compile(
    r"^(?:(?:public|private|protected|internal|static|async|virtual|override|final)\s+)*"
    r"[A-Za-z_][A-Za-z0-9_<>,.?\[\]]*\s+[A-Za-z_][A-Za-z0-9_]*\s*\("
    r"|^func\s+\([A-Za-z_][A-Za-z0-9_]*\s+\*?[A-Za-z_][A-Za-z0-9_]*\)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*\s*\("
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
    try:
        root = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if Path(root).resolve() != project_dir.resolve():
            return []
        tracked = subprocess.run(
            ["git", "-C", str(project_dir), "ls-files", "-z", "--cached"],
            capture_output=True, timeout=5, check=True,
        ).stdout.decode("utf-8").split("\0")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return []
    tracked_paths = set(tracked)
    inventory: list[dict[str, str]] = []
    scanned = 0
    deadline = time.monotonic() + 10
    for visited, (root, dirs, files) in enumerate(os.walk(project_dir, followlinks=False), 1):
        if visited > 500:
            return inventory
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in _EXCLUDED
            and not (Path(root) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(root) / name
            relative = path.relative_to(project_dir).as_posix()
            if (name.startswith(".") or path.suffix.lower() not in _SUFFIXES
                    or _SENSITIVE.search(name) or path.is_symlink()
                    or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
                    or relative not in tracked_paths):
                continue
            scanned += 1
            if scanned > 500 or time.monotonic() >= deadline:
                return inventory
            try:
                remaining = max(0.01, deadline - time.monotonic())
                object_name = f"HEAD:{relative}"
                size = int(subprocess.run(
                    ["git", "-C", str(project_dir), "cat-file", "-s", object_name],
                    capture_output=True, timeout=remaining, check=True,
                ).stdout)
                if size > 65536:
                    continue
                remaining = max(0.01, deadline - time.monotonic())
                data = subprocess.run(
                    ["git", "-C", str(project_dir), "show", object_name],
                    capture_output=True, timeout=remaining, check=True,
                ).stdout
                if b"\0" in data:
                    continue
                lines = data.decode("utf-8").splitlines()
            except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
                continue
            for line in lines:
                match = _DECLARATION.match(line.strip()) or _METHOD.match(line.strip())
                if not match:
                    continue
                excerpt = match.group()
                words = set(re.findall(r"[a-z0-9]+", re.sub(
                    r"([a-z0-9])([A-Z])", r"\1 \2", excerpt
                ).lower()))
                if terms & words and not _SENSITIVE.search(excerpt):
                    inventory.append({
                        "path": relative,
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
        if terms & set(re.findall(
            r"[a-z0-9]+",
            re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", entry["excerpt"]).lower(),
        ))
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
