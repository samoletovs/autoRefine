#!/usr/bin/env python3
"""claude-deep-review.py — Phase 1c LLM deep-review for cloud-agent PRs.

Per plan §8.6: when a cloud-agent PR touches high-risk paths (`src/`, `api/`,
`infrastructure/`, `*.bicep`, files matching `auth|secret|credential`), an LLM
grades the diff against the linked idea memo and returns APPROVE /
REQUEST_CHANGES / ESCALATE.

Backend: GitHub Models (https://github.com/marketplace/models) via the
OpenAI-compatible endpoint at `https://models.inference.ai.azure.com`.
Authenticated with `GH_TOKEN` (the workflow GITHUB_TOKEN or GH_PAT).
No extra API keys or subscriptions required.

Fail-safe: if `GH_TOKEN` is missing OR the API call errors OR the
verdict can't be parsed, this script exits with verdict=SKIPPED, degraded=true.
The caller (workflow) labels the PR `review-degraded` and routes to
`needs-human` via the morning digest.

Inputs (CLI flags):
  --repo OWNER/NAME       Required.
  --pr NUMBER             Required.
  --format json|github    Default json. `github` writes `name=value` lines
                          plus a `verdict_comment<<EOF`-style HEREDOC for
                          the multi-line review comment, suitable for piping
                          into $GITHUB_OUTPUT.
  --model NAME            Primary model (default: claude-opus-4, or
                          $DEEP_REVIEW_MODEL if set in the environment).
                          Any model on GitHub Models works. Swap to
                          claude-opus-4.7 once it appears by setting
                          repo variable DEEP_REVIEW_MODEL=claude-opus-4.7.
  --fallback-model NAME   Fallback if primary returns 404/400. Repeatable.
                          Defaults: [claude-sonnet-4, gpt-4o]. The actually-
                          answering model is reported in the comment header
                          and the JSON ``model`` field.
  --max-tokens N          Cap output tokens (default: 2048).
  --max-diff-bytes N      Truncate the diff above this many bytes (default
                          200_000). Truncation is signalled in the prompt.

Auth: relies on `gh` CLI (authenticated via `GH_TOKEN` — typically
`secrets.GITHUB_TOKEN` in the workflow) for PR + issue fetches, and
`GITHUB_MODELS_TOKEN` (typically `secrets.GH_PAT`) for the GitHub Models
API call. Falls back to `GH_TOKEN` for the model call if
`GITHUB_MODELS_TOKEN` is not set. Both are populated by the workflow.

Exit codes:
  0  predicate succeeded (regardless of verdict / degraded)
  2  CLI usage error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

# Windows stdout defaults to cp1252; force UTF-8 so emoji-laden review bodies print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES", "ESCALATE", "SKIPPED")

LINKED_ISSUE_PATTERN = re.compile(
    r"(?i)\b(?:fixes|closes|resolves)\s+#(\d+)"
)


# ----- result type -----------------------------------------------------------

@dataclass
class ReviewResult:
    verdict: str
    degraded: bool
    reason: str
    comment_body: str
    linked_issue: int | None = None
    diff_truncated: bool = False
    model: str = ""


# ----- gh fetches ------------------------------------------------------------

def _gh_run(args: list[str], *, capture: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (exit {exc.returncode}): {exc.stderr}"
        ) from exc
    return proc.stdout


def fetch_pr(repo: str, pr_number: int) -> dict:
    raw = _gh_run([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "number,title,body,headRefName,baseRefName,files,labels,url",
    ])
    return json.loads(raw)


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    return _gh_run(["pr", "diff", str(pr_number), "--repo", repo])


def extract_linked_issue(body: str | None) -> int | None:
    if not body:
        return None
    match = LINKED_ISSUE_PATTERN.search(body)
    return int(match.group(1)) if match else None


def fetch_issue_memo(repo: str, issue_number: int) -> str | None:
    try:
        raw = _gh_run([
            "issue", "view", str(issue_number),
            "--repo", repo,
            "--json", "title,body,labels",
        ])
    except RuntimeError:
        return None
    data = json.loads(raw)
    title = data.get("title", "")
    body = data.get("body") or ""
    labels = ", ".join(lbl["name"] for lbl in data.get("labels", []))
    return f"# {title}\n\nLabels: {labels}\n\n{body}"


# ----- prompt construction ---------------------------------------------------

SYSTEM_PROMPT = """\
You are the **deep-reviewer** in the NauroLabs closed evaluator-optimizer loop.
A cloud-coding-agent (GitHub Copilot SWE) opened the PR you are about to grade.

You are NOT a style reviewer (the native bot handles style). You are a
correctness, security, and "does this match the spec" reviewer for changes
that touch high-risk paths (src/, api/, infrastructure/, bicep, auth, secrets).

Your verdict MUST be exactly one of: APPROVE, REQUEST_CHANGES, ESCALATE.

- APPROVE: the diff implements every memo success-criterion, has no security
  red flags (hard-coded secrets, missing input validation, broken authz), and
  does not introduce obvious regressions.
- REQUEST_CHANGES: the diff is fixable in 1-2 more iterations. List the
  specific problems with file:line refs.
- ESCALATE: the diff has architectural problems, the memo is the wrong shape
  for the requested change, or the security implications need a human decision.

Constraints:
- Treat the memo as the spec. If something in the memo isn't implemented,
  that's grounds for REQUEST_CHANGES, not APPROVE.
- If you see a secret-shaped string in the diff (anything matching
  /(api[-_ ]?key|secret|password|token)\\s*[:=]\\s*['\"][^'\"]{8,}/i), verdict
  is ESCALATE regardless of the rest of the diff.
- If the diff modifies infrastructure (Bicep, Terraform, GitHub Actions
  workflows) and the memo doesn't explicitly authorize infra changes, that's
  ESCALATE.
- If the diff is larger than the memo described (e.g. memo says "small refactor"
  but diff is 800+ lines), that's REQUEST_CHANGES or ESCALATE.
- Be concise. The whole comment body should be under 1500 characters.

Output format (mandatory):
```
VERDICT: <APPROVE|REQUEST_CHANGES|ESCALATE>
SUMMARY: <one sentence>

FINDINGS:
- <finding 1 with file:line if possible>
- <finding 2>
...

NEXT STEPS:
<one paragraph; what the Builder should do — or, for APPROVE, why this is
safe to merge>
```
"""

USER_PROMPT_TEMPLATE = """\
# PR under review

Repo: {repo}
PR: #{pr_number} — {pr_title}
URL: {pr_url}
Base: {base_ref}  ←  Head: {head_ref}
Labels: {labels}

# Linked idea memo
{memo_section}

# Diff
{diff_section}
"""


def build_user_prompt(
    *,
    repo: str,
    pr: dict,
    diff: str,
    memo: str | None,
    diff_truncated: bool,
) -> str:
    memo_section = (
        memo if memo else "_(no linked idea memo found — review against PR body only)_"
    )
    if not memo and pr.get("body"):
        memo_section = (
            "_(no linked idea memo; PR body shown instead)_\n\n" + pr["body"]
        )
    truncation_note = (
        "\n\n_(diff truncated to fit token budget; trailing changes elided)_"
        if diff_truncated else ""
    )
    labels = ", ".join(lbl["name"] for lbl in pr.get("labels", [])) or "_(none)_"
    return USER_PROMPT_TEMPLATE.format(
        repo=repo,
        pr_number=pr["number"],
        pr_title=pr["title"],
        pr_url=pr.get("url", ""),
        base_ref=pr.get("baseRefName", "?"),
        head_ref=pr.get("headRefName", "?"),
        labels=labels,
        memo_section=memo_section,
        diff_section=f"```diff\n{diff}\n```{truncation_note}",
    )


# ----- GitHub Models API (OpenAI-compatible) ----------------------------------

GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"


def call_github_models(
    *,
    token: str,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call the GitHub Models OpenAI-compatible chat completions endpoint."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GITHUB_MODELS_ENDPOINT,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    choices = parsed.get("choices", [])
    if not choices:
        raise RuntimeError(
            f"GitHub Models response has no choices: {body[:200]}"
        )
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(
            f"GitHub Models response missing content: {body[:200]}"
        )
    return content.strip()


def call_github_models_with_retry(
    *,
    token: str,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_prompt: str,
    fallback_models: list[str] | None = None,
    retries: int = 1,
    backoff_seconds: float = 3.0,
) -> tuple[str, str]:
    """Call GitHub Models with retry for 5xx and fallback for model-not-available.

    Returns ``(content, model_used)`` so the caller can report which candidate
    actually answered. ``fallback_models`` is tried in order whenever the
    primary (or a prior fallback) returns HTTP 404 (model_not_found) or 400
    (model_unavailable). Other HTTP errors (401/403/429/5xx) surface to the
    caller so the existing fail-safe ``review-degraded`` path stays intact.
    """
    candidates = [model] + list(fallback_models or [])
    last_exc: BaseException | None = None
    for candidate in candidates:
        for attempt in range(retries + 1):
            try:
                content = call_github_models(
                    token=token,
                    model=candidate,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                return content, candidate
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (400, 404):
                    # Model unavailable on this account; try the next candidate.
                    break
                is_retryable = 500 <= exc.code < 600
                can_retry = attempt < retries
                if not (is_retryable and can_retry):
                    raise
                time.sleep(backoff_seconds * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable retry loop state")


# ----- response parsing ------------------------------------------------------

_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(APPROVE|REQUEST_CHANGES|ESCALATE)",
                         re.MULTILINE | re.IGNORECASE)


def parse_verdict(text: str) -> tuple[str, str]:
    """Return (verdict, full_comment_body)."""
    match = _VERDICT_RE.search(text)
    if not match:
        raise RuntimeError(
            "could not find VERDICT: line in model response — first 200 chars:\n"
            + text[:200]
        )
    verdict = match.group(1).upper()
    if verdict not in {"APPROVE", "REQUEST_CHANGES", "ESCALATE"}:
        raise RuntimeError(f"verdict '{verdict}' not in allowed set")
    return verdict, text.strip()


# ----- entrypoint ------------------------------------------------------------

def review(repo: str, pr_number: int, *, model: str, fallback_models: list[str],
           max_tokens: int, max_diff_bytes: int) -> ReviewResult:
    # GITHUB_MODELS_TOKEN (typically a PAT) is preferred for the Models API;
    # fall back to GH_TOKEN which also works if it's a PAT.
    token = (
        os.environ.get("GITHUB_MODELS_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
    )
    if not token:
        return ReviewResult(
            verdict="SKIPPED",
            degraded=True,
            reason="Neither GITHUB_MODELS_TOKEN nor GH_TOKEN env var is set",
            comment_body=(
                "🟡 Deep-review skipped: no token configured for "
                "GitHub Models. PR routed to `needs-human`."
            ),
            model=model,
        )

    try:
        pr = fetch_pr(repo, pr_number)
    except RuntimeError as exc:
        return ReviewResult(
            verdict="SKIPPED",
            degraded=True,
            reason=f"failed to fetch PR: {exc}",
            comment_body=f"🟡 Claude deep-review skipped: {exc}",
            model=model,
        )

    linked_issue = extract_linked_issue(pr.get("body") or "")
    memo = fetch_issue_memo(repo, linked_issue) if linked_issue else None

    try:
        diff = fetch_pr_diff(repo, pr_number)
    except RuntimeError as exc:
        return ReviewResult(
            verdict="SKIPPED",
            degraded=True,
            reason=f"failed to fetch diff: {exc}",
            comment_body=f"🟡 Claude deep-review skipped: {exc}",
            linked_issue=linked_issue,
            model=model,
        )

    diff_truncated = False
    if len(diff.encode("utf-8")) > max_diff_bytes:
        diff = diff.encode("utf-8")[:max_diff_bytes].decode(
            "utf-8", errors="ignore"
        )
        diff_truncated = True

    user_prompt = build_user_prompt(
        repo=repo, pr=pr, diff=diff, memo=memo, diff_truncated=diff_truncated,
    )

    try:
        response, model_used = call_github_models_with_retry(
            token=token,
            model=model,
            fallback_models=fallback_models,
            max_tokens=max_tokens,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except urllib.error.HTTPError as exc:
        return ReviewResult(
            verdict="SKIPPED",
            degraded=True,
            reason=f"GitHub Models HTTP {exc.code}: {exc.reason}",
            comment_body=(
                f"🟡 Deep-review degraded (HTTP {exc.code}). "
                "PR routed to `needs-human`."
            ),
            linked_issue=linked_issue,
            diff_truncated=diff_truncated,
            model=model,
        )
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        return ReviewResult(
            verdict="SKIPPED",
            degraded=True,
            reason=f"GitHub Models call failed: {exc}",
            comment_body=(
                f"🟡 Deep-review degraded ({exc}). "
                "PR routed to `needs-human`."
            ),
            linked_issue=linked_issue,
            diff_truncated=diff_truncated,
            model=model,
        )

    try:
        verdict, body = parse_verdict(response)
    except RuntimeError as exc:
        return ReviewResult(
            verdict="ESCALATE",
            degraded=True,
            reason=f"unparseable verdict: {exc}",
            comment_body=(
                f"🚨 Deep-review returned an unparseable verdict; "
                f"escalating.\n\n```\n{response[:1500]}\n```"
            ),
            linked_issue=linked_issue,
            diff_truncated=diff_truncated,
            model=model,
        )

    header = (
        f"🤖 **Deep-review** (`{model_used}` via GitHub Models)\n"
        + (f"Linked memo: #{linked_issue}\n" if linked_issue else "")
        + "\n"
    )
    return ReviewResult(
        verdict=verdict,
        degraded=False,
        reason="model returned a valid verdict",
        comment_body=header + body,
        linked_issue=linked_issue,
        diff_truncated=diff_truncated,
        model=model_used,
    )


def _emit_github(result: ReviewResult) -> None:
    payload = asdict(result)
    for key, value in payload.items():
        if key == "comment_body":
            # Multi-line — use HEREDOC syntax for $GITHUB_OUTPUT.
            sys.stdout.write(f"comment_body<<EOF_COMMENT\n{value}\nEOF_COMMENT\n")
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        sys.stdout.write(f"{key}={rendered}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--format", choices=("json", "github"), default="json")
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEP_REVIEW_MODEL", "").strip() or "claude-opus-4",
        help=(
            "Primary model on GitHub Models. Defaults to $DEEP_REVIEW_MODEL "
            "or claude-opus-4. To swap to claude-opus-4.7 once available, set "
            "the repo variable DEEP_REVIEW_MODEL=claude-opus-4.7 — no code "
            "change required."
        ),
    )
    parser.add_argument(
        "--fallback-model",
        action="append",
        default=None,
        help=(
            "Model to try if the primary returns 404 (model_not_found) or "
            "400 (model_unavailable). Repeatable. If omitted, defaults to "
            "[claude-sonnet-4, gpt-4o]."
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-diff-bytes", type=int, default=200_000)
    args = parser.parse_args(argv)

    if "/" not in args.repo:
        parser.error("--repo must be OWNER/NAME")

    fallback_models = (
        args.fallback_model if args.fallback_model is not None
        else ["claude-sonnet-4", "gpt-4o"]
    )

    result = review(
        args.repo,
        args.pr,
        model=args.model,
        fallback_models=fallback_models,
        max_tokens=args.max_tokens,
        max_diff_bytes=args.max_diff_bytes,
    )
    if args.format == "github":
        _emit_github(result)
    else:
        json.dump(asdict(result), sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
