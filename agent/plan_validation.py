"""Shared memo-quality gate for tool feedback and fail-closed idea filing."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
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

# Test commands a memo may name as its pass/fail check. Only forms whose meaning is
# unambiguous are recognised; anything else is left unchecked rather than guessed at.
_BOUNDARY = r"(?<![\w.\-/])"
_SCRIPT_COMMAND = re.compile(
    _BOUNDARY + r"(npm|pnpm|yarn|bun)\s+(?:run(?:-script)?\s+([\w:.\-@/]+)|(test|t|start)\b)",
    re.IGNORECASE,
)
_RUNNER_COMMAND = re.compile(
    _BOUNDARY + r"(?:npx|pnpm\s+exec|yarn\s+exec)\s+(?:-{1,2}[\w-]+\s+)*([@\w][\w@/.\-]*)",
    re.IGNORECASE,
)
_PYTEST_COMMAND = re.compile(
    _BOUNDARY + r"(?:python3?\s+-m\s+)?(?:pytest|py\.test)(?![\w\-]|\.\w)", re.IGNORECASE,
)
_TOOLCHAIN_COMMAND = re.compile(_BOUNDARY + r"(go|cargo|dotnet)\s+test\b", re.IGNORECASE)
# Package names that provide each runner reachable through npx.
_NODE_RUNNERS = {
    "jest": ("jest",),
    "vitest": ("vitest",),
    "mocha": ("mocha",),
    "ava": ("ava",),
    "cypress": ("cypress",),
    "playwright": ("@playwright/test", "playwright"),
    "@playwright/test": ("@playwright/test",),
}

# A threshold, a before/after value, or an observable pass/fail outcome.
_OUTCOME_VERBS = """
pass fail exit succeed return respond reject raise throw assert match equal contain show
display render download redirect appear block emit save print disable enable hide prevent
skip retry warn notify navigate click compile deploy send receive persist refuse deny accept
""".split()
_MEASURABLE = re.compile(
    r"(?<![A-Za-z_])\d|[<>≤≥]"
    r"|\b(?:at\s+(?:least|most)|no\s+(?:more|fewer|less)\s+than"
    r"|(?:no|without)\s+(?:[\w-]+\s+){0,3}(?:errors?|warnings?|violations?|failures?"
    r"|alerts?|instances?|occurrences?|vulnerabilit(?:y|ies)|findings?|crash(?:es)?)"
    r"|(?:fewer|less|more|greater|lower|higher)\s+than|under|below|above|within|exactly"
    r"|up\s+from|down\s+from|no\s+longer|zero|once|twice|none|half|double|visible"
    r"|verif(?:y|ies|ied)|(?:" + "|".join(_OUTCOME_VERBS) + r")(?:s|es|e?d|ing|n|ted|ged|ped)?)\b",
    re.IGNORECASE,
)


def _names_test_command(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (_SCRIPT_COMMAND, _RUNNER_COMMAND, _PYTEST_COMMAND, _TOOLCHAIN_COMMAND)
    )


def is_measurable(criteria: str) -> bool:
    """A threshold, before/after value or pass/fail check, not an impression."""
    return bool(_MEASURABLE.search(criteria)) or _names_test_command(criteria)


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
        elif field == "success_criteria" and not is_measurable(section):
            errors[field] = (
                "No measurable check. Give a threshold (e.g. 'p95 under 300 ms'), a "
                "before/after value (e.g. 'from 4 clicks to 1') or a pass/fail result a "
                "reviewer can run (e.g. 'npm test passes with a new export test')."
            )
    return errors


def is_specified(improvement: dict) -> bool:
    """Reject unspecified memos rather than fabricating approach/acceptance fields."""
    return not specificity_errors(improvement)


# ── Grounding: named paths and test commands must exist in the target repo ──────

_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
_MAX_SCAN_ENTRIES = 50_000
# Build outputs are usually gitignored, so their absence from a clone proves nothing.
_UNCHECKED_ROOTS = _SCAN_SKIP_DIRS | {
    "dist", "build", "out", "coverage", ".next", "target", "bin", "obj",
}
_PATH_EXTENSIONS = frozenset("""
py pyi js mjs cjs ts mts cts tsx jsx json jsonc md mdx yml yaml toml ini cfg css scss
sass less html htm vue svelte astro go rs cs fs csproj fsproj sln java kt kts swift rb php
sh bash ps1 psm1 sql bicep tf tfvars xml svg gradle c h cpp hpp ipynb txt
""".split())
# Library names written like files in prose ("next.js", "chart.js").
_LIBRARY_STEMS = frozenset("""
node next nuxt vue react angular express nest chart d3 three p5 alpine ember backbone
socket moment day leaflet mapbox pixi phaser matter babylon anime htmx solid svelte
""".split())
_CREATE_MARKERS = frozenset("""
create creates created creating add adds adding new introduce introduces introducing
scaffold scaffolds generate generates generated write writes writing produce produces
emit emits
""".split())
_ABSENT_MARKERS = frozenset("no not without missing lack lacks lacking absent instead".split())
_EXISTING_MARKERS = frozenset("""
existing current currently update updates updating modify modifies modifying edit edits
editing extend extends extending refactor refactors fix fixes fixing change changes
wire wires patch patches replace replaces
""".split())
_ADD_MARKERS = frozenset({"add", "adds", "adding"})
_MOVE_MARKERS = frozenset("""
split splits extract extracts move moves rename renames copy copies
""".split())
_LIST_GLUE = frozenset({"and", "or", "plus"})
_ARTICLES = frozenset({"the", "a", "an"})
_INSTALL_MARKERS = frozenset("""
add adds adding install installs installing introduce introduces adopt adopts
configure configures setup set
""".split())
_SENTENCE_SPLIT = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.;!?])\s+|\n+")
# An illustrative path ("for example src/manifest.json") is not a claim that it exists.
_EXAMPLE_MARKER = re.compile(r"\be\.g\.|\bfor\s+(?:example|instance)\b", re.IGNORECASE)
_TOKEN = re.compile(r"[^\s`'\"()\[\]{}<>,;]+")
_PATH_SEGMENT = re.compile(r"[\w.\-@+]+")


@dataclass(frozen=True)
class RepoTree:
    """What a memo may reference: files, directories, scripts and test runners."""

    paths: frozenset[str]
    root_entries: frozenset[str]
    basenames: frozenset[str]
    scripts: frozenset[str]
    packages: frozenset[str]
    runners: frozenset[str]
    complete: bool

    def has(self, path: str) -> bool:
        """Case-insensitive; a path may also match below a monorepo package root."""
        path = path.strip("/").lower()
        if not path:
            return True
        if "/" not in path and path in self.basenames:
            return True
        if path in self.paths:
            return True
        suffix = "/" + path
        return any(entry.endswith(suffix) for entry in self.paths)


def _read_text(path: Path, limit: int = 1_000_000) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def scan_repo(root: str | os.PathLike[str]) -> RepoTree:
    """Index a checkout once so every memo in a plan is checked against the same tree."""
    root = Path(root)
    paths: set[str] = set()
    root_entries: set[str] = set()
    basenames: set[str] = set()
    scripts: set[str] = set()
    packages: set[str] = set()
    runners: set[str] = set()
    complete = True
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _SCAN_SKIP_DIRS]
        rel = Path(current).relative_to(root).as_posix()
        prefix = "" if rel == "." else rel.lower() + "/"
        for name in (*dirs, *files):
            paths.add(prefix + name.lower())
            if not prefix:
                root_entries.add(name.lower())
        for name in files:
            lower = name.lower()
            basenames.add(lower)
            if (
                re.fullmatch(r"test_.*\.py|.*_test\.py|conftest\.py|pytest\.ini", lower)
                or (
                    re.fullmatch(r"pyproject\.toml|setup\.cfg|tox\.ini|requirements.*\.txt", lower)
                    and "pytest" in _read_text(Path(current) / name).lower()
                )
            ):
                runners.add("pytest")
            elif lower == "go.mod":
                runners.add("go")
            elif lower == "cargo.toml":
                runners.add("cargo")
            elif lower.endswith((".csproj", ".fsproj", ".sln")):
                runners.add("dotnet")
            elif lower == "package.json":
                try:
                    manifest = json.loads(_read_text(Path(current) / name))
                except ValueError:
                    continue
                if not isinstance(manifest, dict):
                    continue
                declared = manifest.get("scripts")
                for script, command in (declared.items() if isinstance(declared, dict) else ()):
                    # npm init's placeholder only prints an error and exits 1.
                    if not (script == "test" and "no test specified" in str(command)):
                        scripts.add(str(script).lower())
                for key in (
                    "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
                ):
                    if isinstance(manifest.get(key), dict):
                        packages.update(str(name).lower() for name in manifest[key])
        if len(paths) > _MAX_SCAN_ENTRIES:
            complete = False
            break
    return RepoTree(
        frozenset(paths), frozenset(root_entries), frozenset(basenames),
        frozenset(scripts), frozenset(packages), frozenset(runners), complete,
    )


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _normalize_path(token: str) -> str:
    if "://" in token:
        return ""
    token = token.split("::")[0].split("#")[0]
    token = re.sub(r":\d+(?::\d+)?$", "", token).rstrip(".:!?")
    while token.startswith("./"):
        token = token[2:]
    return token


def _path_candidate(token: str, tree: RepoTree) -> str | None:
    """Return the path a token names, or None for prose, URLs, globs and identifiers."""
    if not token or token[0] in "/~@$-#" or any(char in token for char in "*?=|\\%"):
        return None
    token = _normalize_path(token)
    if ":" in token:
        token = token.split(":")[0]
    segments = [segment for segment in token.split("/") if segment and segment != "."]
    if not segments or any(
        segment == ".." or not _PATH_SEGMENT.fullmatch(segment) for segment in segments
    ):
        return None
    first = segments[0].lower()
    if first in _UNCHECKED_ROOTS or "." in first[1:] and len(segments) > 1:
        return None  # build output, dependency tree, or a domain name
    last = segments[-1]
    stem, _, extension = last.rpartition(".")
    extension = extension.lower() if stem else ""
    if len(segments) == 1:
        if token.endswith("/"):
            return token
        if extension not in _PATH_EXTENSIONS or stem in ("", "."):
            return None
        if extension == "js" and (stem != stem.lower() or stem in _LIBRARY_STEMS):
            return None
        return token
    if extension in _PATH_EXTENSIONS or token.endswith("/") or first in tree.root_entries:
        return token
    return None


def _named_paths(text: str, tree: RepoTree) -> list[tuple[str, bool]]:
    """Each path a field names, with whether the memo says it will not exist yet."""
    found: list[tuple[str, bool]] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        previous_end, previous_declared = 0, None
        for match in _TOKEN.finditer(sentence):
            path = _path_candidate(match.group(), tree)
            if path is None:
                continue
            segment = sentence[previous_end:match.start()]
            words = _words(segment)
            tail = list(words)
            while tail and tail[-1] in _ARTICLES:
                tail.pop()
            following = re.match(r"\s*\(\s*new\b[^)]*\)?", sentence[match.end():], re.IGNORECASE)
            if following or _EXAMPLE_MARKER.search(segment):
                declared = True
            elif previous_declared is not None and set(words) <= _LIST_GLUE and not re.search(
                r"[^\s,&a-z]", segment.lower(),
            ):
                declared = previous_declared
            elif tail and tail[-1] in ("into", "to", "as") and _MOVE_MARKERS & set(
                _words(sentence[:match.start()]),
            ):
                declared = True
            else:
                markers = [
                    index for index, word in enumerate(words)
                    if word in _CREATE_MARKERS or word in _ABSENT_MARKERS
                ]
                declared = bool(markers) and not any(
                    word in _EXISTING_MARKERS for word in words[markers[-1] + 1:]
                )
                # "Add X to <path>" names an existing target, not a new file.
                if declared and tail and tail[-1] in ("to", "into") and _ADD_MARKERS & set(words):
                    declared = False
            found.append((path, declared))
            previous_end = match.end() + (following.end() if following else 0)
            previous_declared = declared
    return found


def _sentences_with(text: str, required: frozenset[str], *words: str) -> bool:
    """A sentence pairs one of *required* with every one of *words*."""
    for sentence in _SENTENCE_SPLIT.split(text):
        tokens = {
            token.rstrip(".:") for token in re.findall(r"[\w@/:.\-]+", sentence.lower())
        }
        if required & tokens and all(word in tokens for word in words):
            return True
    return False


def _command_errors(
    criteria: str, tree: RepoTree, memo_text: str, declared_paths: set[str],
) -> list[str]:
    """Each named test command must be runnable in this repo, or set up by the memo."""
    errors: list[str] = []
    known_scripts = ", ".join(sorted(tree.scripts)[:12])
    for match in _SCRIPT_COMMAND.finditer(criteria):
        tool = match.group(1).lower()
        if tool == "bun" and match.group(3):
            continue  # bun's built-in runner, not a package.json script
        script = (match.group(2) or match.group(3)).rstrip(".:").lower()
        script = "test" if script == "t" else script
        if script in tree.scripts or (script == "start" and tree.has("server.js")):
            continue
        if _sentences_with(memo_text, _CREATE_MARKERS | {"define", "defines"}, script, "script"):
            continue
        defined = f"defined scripts: {known_scripts}" if known_scripts else (
            "the repo defines no package.json scripts"
        )
        errors.append(
            f"'{match.group().strip()}' runs the '{script}' script, but no package.json in "
            f"the repo defines it ({defined}). Use a command the repo already has, or state "
            f"in approach that you add the '{script}' script to package.json."
        )
    for match in _RUNNER_COMMAND.finditer(criteria):
        tool = match.group(1).rstrip(".:").lower()
        providers = _NODE_RUNNERS.get(tool)
        if providers is None or tree.packages & set(providers):
            continue
        if any(_sentences_with(memo_text, _INSTALL_MARKERS, name) for name in {tool, *providers}):
            continue
        errors.append(
            f"'{match.group().strip()}' needs {providers[0]}, but no package.json in the repo "
            f"lists it as a dependency. Use the repo's existing test command, or state in "
            f"approach that you add {providers[0]}."
        )
    for match in _PYTEST_COMMAND.finditer(criteria):
        new_tests = any(
            re.fullmatch(r"test_.*\.py|.*_test\.py", path.rsplit("/", 1)[-1])
            for path in declared_paths
        )
        if not (
            "pytest" in tree.runners or new_tests
            or _sentences_with(memo_text, _INSTALL_MARKERS, "pytest")
        ):
            alternative = " Use the repo's own test command, e.g. 'npm test'." if (
                "test" in tree.scripts
            ) else " Name the new test_*.py file in approach if you add pytest tests."
            errors.append(
                f"'{match.group().strip()}' needs pytest, but the repo has no test_*.py, "
                f"conftest.py, pytest.ini or pytest dependency.{alternative}"
            )
            continue
        for argument in criteria[match.end():].split():
            argument = argument.strip("`'\"),;").rstrip(".:")
            if argument.startswith("-"):
                continue
            if not ("/" in argument or argument.endswith(".py") or "::" in argument):
                break
            path = _normalize_path(argument)
            parent, _, name = path.rstrip("/").rpartition("/")
            # A new test file in an existing test directory is how most fixes are proven.
            new_test_file = bool(re.fullmatch(r"test_.*\.py|.*_test\.py", name.lower())) and (
                not parent or tree.has(parent)
            )
            if (
                path and not new_test_file and not tree.has(path)
                and path.lower() not in declared_paths
            ):
                errors.append(
                    f"'{match.group().strip()} {argument}' targets {path}, which does not "
                    f"exist in the repo. Point it at an existing test file or directory, or "
                    f"name the new file in approach (e.g. 'create {path}')."
                )
    for match in _TOOLCHAIN_COMMAND.finditer(criteria):
        tool = match.group(1).lower()
        if tool not in tree.runners:
            manifest = {"go": "go.mod", "cargo": "Cargo.toml", "dotnet": ".csproj or .sln"}[tool]
            errors.append(
                f"'{match.group().strip()}' needs a {manifest} in the repo, and there is none. "
                f"Use the repo's existing test command."
            )
    return list(dict.fromkeys(errors))


def grounding_errors(improvement: dict, tree: RepoTree) -> dict[str, list[str]]:
    """Name paths and test commands in a memo that the target repo does not have."""
    approach = improvement.get("approach")
    criteria = improvement.get("success_criteria")
    approach = approach if isinstance(approach, str) else ""
    criteria = criteria if isinstance(criteria, str) else ""
    memo_text = f"{approach}\n{criteria}"
    approach_paths = _named_paths(approach, tree)
    declared = {
        path.lower().strip("/")
        for path, is_new in approach_paths + _named_paths(criteria, tree) if is_new
    }
    errors: dict[str, list[str]] = {}
    if not tree.complete:
        return errors  # a truncated index cannot prove anything is missing
    missing = list(dict.fromkeys(
        path for path, is_new in approach_paths
        if not is_new and path.lower().strip("/") not in declared and not tree.has(path)
    ))
    if missing:
        errors["approach"] = [(
            f"These paths do not exist in the repo: {', '.join(missing[:8])}. Use "
            f"list_directory or read_project_file to find the real files, or mark a file "
            f"you will create explicitly (e.g. 'create {missing[0]}' or "
            f"'{missing[0]} (new)')."
        )]
    command_errors = _command_errors(criteria, tree, memo_text, declared)
    if command_errors:
        errors["success_criteria"] = command_errors
    return errors


def plan_errors(
    plan: dict,
    *,
    read_paths: set[str] | None = None,
    project_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate a whole submission; never turn discarded/missing items into a no-gap.

    With *project_dir*, each memo's paths and test commands are also checked
    against that checkout.
    """
    errors: list[dict[str, Any]] = []
    tree: RepoTree | None = None

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
        specificity = specificity_errors(improvement)
        for field, message in specificity.items():
            reject(field, message, index)
        if project_dir is None or not Path(project_dir).is_dir():
            continue
        if tree is None:
            tree = scan_repo(project_dir)
        for field, messages in grounding_errors(improvement, tree).items():
            if field not in specificity:
                for message in messages:
                    reject(field, message, index)
    return errors
