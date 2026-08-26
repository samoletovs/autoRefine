"""Send Telegram notification from the evaluate CI workflow.

Reads all data from env vars so the script file can be called without
inline code in the YAML (which causes YAML indentation parse errors).

Required env vars:
  NAURO_BOT_TOKEN, NAURO_CHAT_ID  — Telegram credentials (via agent.notify)

Optional env vars (populated by the workflow):
  SCORES     — multi-line score summary
  TOTAL      — number of projects evaluated
  AVG        — average score
  MODE       — evaluate mode (file-ideas / plan / evaluate)
  STATUS     — ``steps.parse-scores.outcome``: "success" | "failure" | "skipped"
  RUN_URL    — URL of the GitHub Actions run for the failure message
  ISSUE_URL  — URL of the tracking issue, empty when no issue was filed
"""

import os
import sys

# GitHub marks a step "skipped" when an *earlier* step failed, so the parse
# never ran and the sweep never reached a project. That is an infrastructure
# fault — a credential, a checkout, a dependency install — and it is a
# different problem from parsing a real report and finding no scores in it.
# The two need different things from whoever reads the message, so they must
# not render the same. "cancelled" lands here for the same reason.
_INFRASTRUCTURE_STATUSES = frozenset({"skipped", "cancelled"})


def _failure_kind(status: str, has_scores: bool) -> str | None:
    """Classify the failure, or ``None`` when the run genuinely succeeded."""
    if status in _INFRASTRUCTURE_STATUSES:
        return "infrastructure"
    if status and status != "success":
        return "parse"
    if not has_scores:
        return "parse"
    return None


def _failure_detail(kind: str, has_issue: bool) -> str:
    """The closing sentence, which must never claim more than is true.

    This line used to read "Copilot has been notified to investigate"
    unconditionally. On an infrastructure failure the issue-filing step is
    skipped, so no issue exists and nobody has been notified — and a human
    told the problem is already being handled stops looking at it. A false
    reassurance is worse than silence, because silence at least prompts a
    question.
    """
    if kind == "infrastructure":
        base = (
            "The run stopped before any project was evaluated, so there are no "
            "scores to report — the failure is in the workflow itself."
        )
    else:
        base = "The run finished, but no scores could be parsed from its report."

    if has_issue:
        return f"{base} Copilot has been notified to investigate."
    return f"{base} No issue was filed, so nobody has been notified — this needs a human."


def build_message() -> str:
    """Build the Telegram message body based on env vars.

    Kept pure so it can be unit-tested without touching the network.
    """
    scores = os.environ.get("SCORES", "").strip()
    total = os.environ.get("TOTAL", "").strip()
    avg = os.environ.get("AVG", "").strip()
    mode = os.environ.get("MODE", "file-ideas").strip() or "file-ideas"
    status = os.environ.get("STATUS", "").strip().lower()
    run_url = os.environ.get("RUN_URL", "").strip()
    issue_url = os.environ.get("ISSUE_URL", "").strip()

    has_scores = bool(scores) and bool(total) and bool(avg)
    kind = _failure_kind(status, has_scores)

    if kind is not None:
        headline = "FAILED before any project was scored" if kind == "infrastructure" else "FAILED"
        lines = [f"❌ <b>autoRefine</b> daily {mode} — <b>{headline}</b>"]
        if run_url:
            lines.append(f'🔗 <a href="{run_url}">View run</a>')
        if issue_url:
            lines.append(f'🐛 <a href="{issue_url}">Tracking issue</a>')
        lines.append("")
        lines.append(_failure_detail(kind, bool(issue_url)))
        return "\n".join(lines)

    msg = f"🔧 <b>autoRefine</b> daily {mode}\n\n"
    msg += f"📊 {total} projects, avg {avg}/100\n\n"
    msg += scores
    return msg


def main() -> int:
    sys.path.insert(0, os.getcwd())
    try:
        from agent.notify import send_telegram
    except ImportError as exc:
        print(f"Could not import agent.notify: {exc}", file=sys.stderr)
        return 1

    msg = build_message()
    ok = send_telegram(msg, parse_mode="HTML")
    print("Telegram notification sent" if ok else "Telegram notification skipped/failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
