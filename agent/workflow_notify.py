"""Send Telegram notification from the evaluate CI workflow.

Reads all data from env vars so the script file can be called without
inline code in the YAML (which causes YAML indentation parse errors).

Required env vars:
  NAURO_BOT_TOKEN, NAURO_CHAT_ID  — Telegram credentials (via agent.notify)

Optional env vars (populated by the Parse scores step):
  SCORES   — multi-line score summary
  TOTAL    — number of projects evaluated
  AVG      — average score
  MODE     — evaluate mode (file-ideas / plan / evaluate)
"""

import os
import sys

sys.path.insert(0, os.getcwd())

try:
    from agent.notify import send_telegram
except ImportError as exc:
    print(f"Could not import agent.notify: {exc}", file=sys.stderr)
    sys.exit(1)

scores = os.environ.get("SCORES", "No data")
total = os.environ.get("TOTAL", "?")
avg = os.environ.get("AVG", "?")
mode = os.environ.get("MODE", "file-ideas")

msg = f"🔧 <b>autoRefine</b> daily {mode}\n\n"
msg += f"📊 {total} projects, avg {avg}/100\n\n"
msg += scores

ok = send_telegram(msg, parse_mode="HTML")
print("Telegram notification sent" if ok else "Telegram notification skipped/failed")
