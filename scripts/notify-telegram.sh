#!/usr/bin/env bash
# notify-telegram.sh — single shared sender for all Telegram notifications.
#
# Why this exists: workflows used to call the Telegram API directly, some with
# parse_mode=HTML and some without, and a couple emitted literal "\u2705"
# escape sequences instead of real UTF-8 emoji. That made notifications render
# inconsistently (and sometimes as raw escapes) in the chat.
#
# Usage:
#   scripts/notify-telegram.sh [--html] [--plain] "message text"
#   echo "message text" | scripts/notify-telegram.sh [--html] [--plain]
#
# Flags:
#   --html    Send with parse_mode=HTML. Caller is responsible for HTML-escaping
#             any user-controlled substring it interpolates into the message.
#             Default mode.
#   --plain   Send with no parse_mode. Use for messages that contain raw text
#             with characters like '<', '>', '&' that should NOT be parsed.
#
# Environment:
#   NAURO_BOT_TOKEN   Telegram bot token. If unset, the script exits 0 (no-op).
#   NAURO_CHAT_ID     Telegram chat id.    If unset, the script exits 0 (no-op).
#
# Exit codes:
#   0  Message sent OR credentials not configured (silent skip, by design — we
#      don't want CI to fail just because the bot secret is missing in a fork).
#   2  curl invocation failed (network / Telegram API rejection).
#
# Notes:
#   - We use --data-urlencode so embedded newlines, '&', '=' etc. are safe.
#   - We deliberately do NOT use 'set -e' inside this helper; the caller decides
#     whether a Telegram failure should fail the step (most use '|| true').

set -u

mode="html"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --html)   mode="html";  shift ;;
    --plain)  mode="plain"; shift ;;
    --)       shift; break ;;
    -*)
      echo "notify-telegram: unknown flag: $1" >&2
      exit 64
      ;;
    *) break ;;
  esac
done

if [ "$#" -gt 0 ]; then
  message="$*"
else
  # Read from stdin if no positional message was given.
  message="$(cat)"
fi

if [ -z "${NAURO_BOT_TOKEN:-}" ] || [ -z "${NAURO_CHAT_ID:-}" ]; then
  echo "notify-telegram: NAURO_BOT_TOKEN or NAURO_CHAT_ID not set; skipping" >&2
  exit 0
fi

if [ -z "${message:-}" ]; then
  echo "notify-telegram: empty message; skipping" >&2
  exit 0
fi

curl_args=(
  -sf -X POST
  "https://api.telegram.org/bot${NAURO_BOT_TOKEN}/sendMessage"
  --data-urlencode "chat_id=${NAURO_CHAT_ID}"
  --data-urlencode "text=${message}"
  --data-urlencode "disable_web_page_preview=true"
)
if [ "$mode" = "html" ]; then
  curl_args+=(--data-urlencode "parse_mode=HTML")
fi

if ! curl "${curl_args[@]}" >/dev/null; then
  echo "notify-telegram: send failed" >&2
  exit 2
fi
