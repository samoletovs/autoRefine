"""Shared memo-quality gate for tool feedback and fail-closed idea filing."""

from __future__ import annotations

import re
from typing import Any


# Shared with title deduplication; filler cannot make a memo specific.
FILLER_WORDS = frozenset("""
implement implemented implementing implementation add added adding usable used
as per this that describe described description work works working correct
correctly proper properly success successful successfully expected regression
regressions existing current flow flows feature features functionality
change changes update updates ensure ensures make makes should must will can
no not any all and or but with without for from into the a an of to in on at
by is are be been being was were do does done p0 p1 p2 p3
enhance enhanced enhancing enhancement improve improved improving improvement
better optimize optimized optimizing optimization support new
""".split())


def specificity_errors(improvement: dict) -> dict[str, str]:
    """Name fields that do not add two substantive words beyond the title."""
    title_words = set(
        re.sub(r"[^0-9a-zA-Z]+", " ", str(improvement.get("title", ""))).lower().split()
    )
    errors = {}
    if "category" in improvement and not isinstance(improvement["category"], str):
        errors["category"] = "Supply a category string, e.g. 'feature', or omit it for 'quality'."
    for field, guidance in (
        ("approach", "Name the actual files, functions or commands to change."),
        ("success_criteria", "Give an observable pass/fail check a reviewer can run."),
    ):
        section = improvement.get(field)
        if not isinstance(section, str) or not section.strip():
            errors[field] = f"A nonempty text field is required. {guidance}"
            continue
        words = set(re.sub(r"[^0-9a-zA-Z]+", " ", section).lower().split())
        if len(words - title_words - FILLER_WORDS) < 2:
            errors[field] = f"Add at least two substantive words beyond the title. {guidance}"
    return errors


def is_specified(improvement: dict) -> bool:
    """Reject unspecified memos rather than fabricating approach/acceptance fields."""
    return not specificity_errors(improvement)


def plan_errors(plan: dict, *, read_paths: set[str] | None = None) -> list[dict[str, Any]]:
    """Validate a whole submission; never turn discarded/missing items into a no-gap."""
    errors: list[dict[str, Any]] = []

    def reject(field: str, message: str, item: int | None = None) -> None:
        errors.append({"item": item, "field": field, "error": message})

    improvements = plan.get("improvements")
    if not isinstance(improvements, list):
        reject("improvements", "Supply a JSON array of improvement objects.")
        return errors
    outcome = plan.get("outcome", "improvements")
    if outcome not in ("improvements", "no_gap"):
        reject("outcome", "Use 'improvements' or 'no_gap'.")
    if outcome == "no_gap":
        if improvements:
            reject("improvements", "A no_gap result must have an empty improvements array.")
        if not isinstance(plan.get("summary"), str) or not plan["summary"].strip():
            reject("summary", "Explain why the reviewed goals have no justified P0-P2 gap.")
        evidence = plan.get("no_gap_evidence")
        if not isinstance(evidence, list) or not evidence:
            reject("no_gap_evidence", "List checked files with path and observation fields.")
        else:
            for index, entry in enumerate(evidence, 1):
                if not isinstance(entry, dict):
                    reject("no_gap_evidence", "Each entry needs path and observation.", index)
                    continue
                path, observation = entry.get("path"), entry.get("observation")
                if not isinstance(path, str) or not path.strip():
                    reject("no_gap_evidence.path", "Name a file you read this run.", index)
                elif read_paths is not None and path not in read_paths:
                    reject("no_gap_evidence.path", "Read this file successfully before citing it.", index)
                if not isinstance(observation, str) or not observation.strip():
                    reject("no_gap_evidence.observation", "Describe the capability verified there.", index)
        return errors
    if not improvements:
        reject(
            "outcome",
            "An empty plan requires outcome='no_gap', an explanation in summary, and "
            "no_gap_evidence from files read this run. Do not invent filler ideas.",
        )
    for index, improvement in enumerate(improvements, 1):
        if not isinstance(improvement, dict):
            reject("improvements", "Each improvement must be an object.", index)
            continue
        for field in ("title", "description"):
            if not isinstance(improvement.get(field), str) or not improvement[field].strip():
                reject(field, "Supply nonempty text describing this specific improvement.", index)
        for field, message in specificity_errors(improvement).items():
            reject(field, message, index)
    return errors
