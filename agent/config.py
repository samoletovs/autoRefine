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
            goals=cls._str_list(data.get("goals"), path, "goals"),
            similar=cls._str_list(data.get("similar"), path, "similar"),
            quality=cls._str_list(data.get("quality"), path, "quality"),
        )

    @staticmethod
    def _str_list(value: object, path: Path, key: str) -> list[str]:
        """Coerce a project.yaml list field to plain strings.

        These cards are hand-written, so a mistyped entry is a matter of time: a stray
        `- i18n: false` parses as a dict, and joining that raised a TypeError that killed
        the whole run mid-way through the projects. One malformed card should cost that
        card its detail, not the remaining projects, so anything unexpected is stringified
        and flagged rather than raised.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            log.warning("%s: '%s' should be a list, got %s — ignoring", path, key, type(value).__name__)
            return []
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            else:
                log.warning("%s: '%s' entry %r is not a string — coercing", path, key, item)
                out.append(str(item))
        return out

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
    # Default model: cheap Foundry tier for daily scans of 11 repos.
    # Override via CLI --model or FOUNDRY_DEFAULT_DEPLOYMENT env var.
    # See AGENTS.md "Model strategy" for the tiered plan.
    model: str = "gpt-4o-mini"
    dry_run: bool = False
    workdir: Path = field(default_factory=lambda: Path("/tmp/autorefine"))
    # True only for a manifest-driven sweep of every project. Such a sweep re-plans
    # projects that have not changed since the last one, which is the bulk of the
    # Foundry bill; see should_plan_repo in main.py. An explicit --repo is a human
    # asking for this project now, so it is never gated.
    gate_on_activity: bool = False
    # {repo_slug: "top"|"normal"|"low"} from the workspace manifest. Scales the
    # rotation floor in should_plan_repo, so flagship projects come round sooner
    # than experiments nobody has touched since spring. Empty means everything is
    # treated as `normal` — a missing manifest must never silently demote a project.
    priorities: dict[str, str] = field(default_factory=dict)
