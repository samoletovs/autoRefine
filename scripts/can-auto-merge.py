#!/usr/bin/env python3
"""can-auto-merge.py — single-source-of-truth predicate for the auto-review gates.

Per plan §8.7 + §8.9. Mirrors the inline logic in .github/workflows/auto-review.yml
so that other callers (Sam's CLI, future workflows, other agents) can ask the same
question: "is this PR mergeable by automation right now?".

Inputs (CLI flags):
  --repo OWNER/NAME      Required.
  --pr   NUMBER          Required.
  --format json|github   json (default) prints one JSON line; github emits
                         `name=value` lines suitable for GitHub Actions outputs.

Behavior:
  1. Fetches the PR, its files, labels, reviews, and check runs via `gh`.
  2. Evaluates all gates in plan §8.7 and returns a structured result.

Exit codes:
  0  predicate succeeded (regardless of can_merge true/false)
  2  CLI usage error
  3  upstream API error (gh failed, malformed response, etc.)

Auth: relies on `gh` being authenticated. In a workflow, set GH_TOKEN to the
GITHUB_TOKEN of the run.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Iterable

# Windows stdout defaults to cp1252; force UTF-8 so JSON / emoji output works.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----- gate configuration ----------------------------------------------------
#
# Repo-specific patterns: autoRefine source code lives in `agent/` and
# `scripts/` rather than the governance repo's `src/`. We include them in
# BOTH LOW_RISK (so the file-tier gate doesn't escalate) and HIGH_RISK
# (so the closed loop runs Claude deep-review before merging). The result
# is: every agent/scripts change requires a deep-review APPROVE verdict,
# while plain *.md / test files auto-merge with CI alone.
#
# Future work: lift these into config/auto-review-patterns.json so every
# repo can ship its own allowlist without forking the script.

LOW_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.md$", re.IGNORECASE),
    re.compile(r"\.json$", re.IGNORECASE),
    re.compile(r"\.yml$", re.IGNORECASE),
    re.compile(r"\.yaml$", re.IGNORECASE),
    re.compile(r"^tests/"),
    re.compile(r"^docs/"),
    re.compile(r"^reports/"),
    re.compile(r"^agent/"),
    re.compile(r"^scripts/"),
    re.compile(r"^\.github/"),
    re.compile(r"(^|/)\.gitkeep$"),
    re.compile(r"(^|/)\.env\.example$"),
    re.compile(r"(^|/)requirements.*\.txt$", re.IGNORECASE),
]

HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^infrastructure/"),
    re.compile(r"\.bicep$", re.IGNORECASE),
    re.compile(r"^agent/"),
    re.compile(r"^scripts/"),
    re.compile(r"auth", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
]

RISKY_LABELS: frozenset[str] = frozenset({
    "bicep", "secret", "dns", "major-bump", "auth", "breaking-change",
})

DAILY_CAP: int = 5
ORG_DAILY_CAP: int = 40
AUTO_MERGED_LABEL: str = "auto-merged"
REQUIRED_CHECK_OK_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})


# ----- result type -----------------------------------------------------------

@dataclass
class GateResult:
    can_merge: bool
    gate_failed: str | None
    reason: str
    low_risk_only: bool
    has_risky_label: bool
    under_daily_cap: bool
    under_org_daily_cap: bool
    needs_deep_review: bool
    merged_today: int
    merged_today_org: int
    files_count: int
    risky_labels_found: list[str] = field(default_factory=list)
    high_risk_files: list[str] = field(default_factory=list)
    non_low_risk_files: list[str] = field(default_factory=list)


def review_and_ci_gates_pass(
    *,
    verdict: str,
    degraded: bool,
    labels: Iterable[str],
    required_check_conclusions: Iterable[str],
) -> bool:
    """Return True when review + CI-related gates allow auto-merge."""
    normalized_labels = {label.strip().lower() for label in labels}
    if verdict != "APPROVE":
        return False
    if degraded:
        return False
    if "needs-human" in normalized_labels:
        return False
    if any(label.startswith("risky-") for label in normalized_labels):
        return False
    return all(
        (conclusion or "").strip().lower() in REQUIRED_CHECK_OK_CONCLUSIONS
        for conclusion in required_check_conclusions
    )


# ----- gh helpers ------------------------------------------------------------

def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse its stdout as JSON.

    Raises SystemExit(3) on failure so the caller (workflow) sees a non-zero
    exit code without us needing to redefine an exception class.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print("error: `gh` CLI not found on PATH", file=sys.stderr)
        sys.exit(3)
    except subprocess.CalledProcessError as exc:
        print(
            f"error: gh {' '.join(args)} failed (exit {exc.returncode}):\n"
            f"{exc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"error: gh stdout was not JSON: {exc}", file=sys.stderr)
        sys.exit(3)


def _paginate(url: str, per_page: int = 100) -> list[dict]:
    """Page through a GitHub REST endpoint and concatenate the arrays."""
    sep = "&" if "?" in url else "?"
    result: list[dict] = []
    page = 1
    while True:
        chunk = _gh_json(["api", f"{url}{sep}per_page={per_page}&page={page}"])
        if not isinstance(chunk, list):
            print(f"error: expected list from {url}, got {type(chunk).__name__}",
                  file=sys.stderr)
            sys.exit(3)
        result.extend(chunk)
        if len(chunk) < per_page:
            return result


def _count_org_auto_merges_today(owner: str, today: str) -> int:
    """Count org-wide merged PRs labeled `auto-merged` on the given UTC date."""
    query = f"org:{owner} is:pr is:merged label:{AUTO_MERGED_LABEL} merged:{today}"
    encoded_query = urllib.parse.quote_plus(query)
    payload = _gh_json(["api", f"/search/issues?q={encoded_query}&per_page=1"])
    if not isinstance(payload, dict):
        print("error: expected dict from search/issues", file=sys.stderr)
        sys.exit(3)
    total_count = payload.get("total_count", 0)
    try:
        return int(total_count)
    except (TypeError, ValueError):
        print(f"error: unexpected total_count from search/issues: {total_count}",
              file=sys.stderr)
        sys.exit(3)
        page += 1
        if page > 50:  # hard safety; we don't expect repos this large in canary
            return result


# ----- gate evaluation -------------------------------------------------------

def _any_match(filename: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(p.search(filename) for p in patterns)


def evaluate(repo: str, pr_number: int) -> GateResult:
    pr_obj = _gh_json(["api", f"/repos/{repo}/pulls/{pr_number}"])
    assert isinstance(pr_obj, dict)

    label_names: list[str] = [lbl["name"] for lbl in pr_obj.get("labels", [])]
    risky_labels_found = [name for name in label_names if name in RISKY_LABELS]
    has_risky_label = bool(risky_labels_found)

    files = _paginate(f"/repos/{repo}/pulls/{pr_number}/files")
    filenames: list[str] = [f["filename"] for f in files]

    if filenames:
        non_low_risk = [f for f in filenames if not _any_match(f, LOW_RISK_PATTERNS)]
        high_risk = [f for f in filenames if _any_match(f, HIGH_RISK_PATTERNS)]
    else:
        # An empty PR (no file changes) is suspicious; treat as not-low-risk.
        non_low_risk = []
        high_risk = []
    low_risk_only = bool(filenames) and not non_low_risk
    needs_deep_review = bool(high_risk)

    # Daily cap: count auto-merged PRs closed today.
    today = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
    closed = _paginate(
        f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
    )
    merged_today = 0
    for pr in closed:
        if not pr.get("merged_at"):
            # stop early; once we hit a PR not merged today and not updated
            # today, older PRs are guaranteed out of window
            if (pr.get("updated_at") or "")[:10] < today:
                break
            continue
        if pr["merged_at"][:10] != today:
            # Updated today but merged earlier — skip but keep scanning.
            if pr.get("updated_at", "")[:10] < today:
                break
            continue
        if any(lbl["name"] == AUTO_MERGED_LABEL for lbl in pr.get("labels", [])):
            merged_today += 1
    owner = repo.split("/", 1)[0]
    merged_today_org = _count_org_auto_merges_today(owner, today)
    under_repo_daily_cap = merged_today < DAILY_CAP
    under_org_daily_cap = merged_today_org < ORG_DAILY_CAP
    under_daily_cap = under_repo_daily_cap and under_org_daily_cap

    # Compose verdict in plan §8.7 order.
    if not filenames:
        return GateResult(
            can_merge=False,
            gate_failed="empty-pr",
            reason="PR has no file changes",
            low_risk_only=False,
            has_risky_label=has_risky_label,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=False,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=0,
            risky_labels_found=risky_labels_found,
        )
    if has_risky_label:
        return GateResult(
            can_merge=False,
            gate_failed="risky-label",
            reason=f"risky label(s) applied: {', '.join(risky_labels_found)}",
            low_risk_only=low_risk_only,
            has_risky_label=True,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            risky_labels_found=risky_labels_found,
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if not low_risk_only:
        return GateResult(
            can_merge=False,
            gate_failed="file-tier",
            reason=(
                f"{len(non_low_risk)} file(s) outside low-risk allowlist: "
                f"{', '.join(non_low_risk[:3])}"
                + (" …" if len(non_low_risk) > 3 else "")
            ),
            low_risk_only=False,
            has_risky_label=False,
            under_daily_cap=under_daily_cap,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
            high_risk_files=high_risk,
            non_low_risk_files=non_low_risk,
        )
    if not under_daily_cap:
        return GateResult(
            can_merge=False,
            gate_failed="daily-cap",
            reason=(
                ", ".join(
                    part for part in [
                        (
                            f"repo cap reached: {merged_today}/{DAILY_CAP}"
                            if not under_repo_daily_cap else ""
                        ),
                        (
                            f"org cap reached: {merged_today_org}/{ORG_DAILY_CAP}"
                            if not under_org_daily_cap else ""
                        ),
                    ] if part
                )
            ),
            low_risk_only=True,
            has_risky_label=False,
            under_daily_cap=False,
            under_org_daily_cap=under_org_daily_cap,
            needs_deep_review=needs_deep_review,
            merged_today=merged_today,
            merged_today_org=merged_today_org,
            files_count=len(filenames),
        )
    return GateResult(
        can_merge=True,
        gate_failed=None,
        reason="all pre-flight gates passed",
        low_risk_only=True,
        has_risky_label=False,
        under_daily_cap=True,
        under_org_daily_cap=True,
        needs_deep_review=needs_deep_review,
        merged_today=merged_today,
        merged_today_org=merged_today_org,
        files_count=len(filenames),
    )


# ----- output formatting -----------------------------------------------------

def _emit_json(result: GateResult) -> None:
    json.dump(asdict(result), sys.stdout)
    sys.stdout.write("\n")


def _emit_github(result: GateResult) -> None:
    """Emit `name=value` lines for the GITHUB_OUTPUT file."""
    payload = asdict(result)
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, list):
            rendered = ",".join(str(v) for v in value)
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        # Multi-line strings would need a HEREDOC; our values are all single-line.
        sys.stdout.write(f"{key}={rendered}\n")


# ----- entrypoint ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="OWNER/NAME of the repo")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument(
        "--format",
        choices=("json", "github"),
        default="json",
        help="Output shape (default: json)",
    )
    args = parser.parse_args(argv)

    if "/" not in args.repo:
        parser.error("--repo must be OWNER/NAME")

    result = evaluate(args.repo, args.pr)
    if args.format == "github":
        _emit_github(result)
    else:
        _emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
