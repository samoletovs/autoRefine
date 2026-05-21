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
  STATUS     — overall job/parse status: "success" | "failure" | anything else
  RUN_URL    — URL of the GitHub Actions run for the failure message
  ISSUE_URL  — URL of the tracking issue (when STATUS != "success")
"""

import os
import sys


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

    is_failure = status and status != "success"
    has_scores = bool(scores) and bool(total) and bool(avg)

    if is_failure or not has_scores:
        lines = [f"❌ <b>autoRefine</b> daily {mode} — <b>FAILED</b>"]
        if run_url:
            lines.append(f'🔗 <a href="{run_url}">View run</a>')
        if issue_url:
            lines.append(f'🐛 <a href="{issue_url}">Tracking issue</a>')
        lines.append("")
        lines.append("No scores parsed — Copilot has been notified to investigate.")
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
