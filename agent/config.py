"""Configuration and project.yaml parsing."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class ProjectConfig:
    """Parsed project.yaml — the machine-readable project card."""

    name: str
    purpose: str
    users: str
    stage: str  # idea | research | mvp | active | complete | archived
    goals: list[str] = field(default_factory=list)
    similar: list[str] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "ProjectConfig":
        """Load a project.yaml file."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls(
            name=data.get("name", path.parent.name),
            purpose=data.get("purpose", ""),
            users=data.get("users", ""),
            stage=data.get("stage", "active"),
            goals=data.get("goals", []),
            similar=data.get("similar", []),
            quality=data.get("quality", []),
        )

    def to_context(self) -> str:
        """Format as a context string for the LLM."""
        lines = [
            f"# {self.name}",
            f"Purpose: {self.purpose}",
            f"Users: {self.users}",
            f"Stage: {self.stage}",
        ]
        if self.goals:
            lines.append("Goals:")
            for g in self.goals:
                lines.append(f"  - {g}")
        if self.similar:
            lines.append(f"Similar products: {', '.join(self.similar)}")
        if self.quality:
            lines.append(f"Quality traits: {', '.join(self.quality)}")
        return "\n".join(lines)


@dataclass
class AutoRefineConfig:
    """Top-level configuration for an autoRefine run."""

    repos: list[str]  # e.g., ["samoletovs/golazo", "samoletovs/era"]
    mode: str = "evaluate"  # evaluate | plan | refine
    model: str = "gpt-4o-mini"
    dry_run: bool = False
    workdir: Path = field(default_factory=lambda: Path("/tmp/autorefine"))
