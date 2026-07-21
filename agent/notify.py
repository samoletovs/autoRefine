"""Telegram notification helper for the NauroLabs ops bot.

Sends one-way messages to the lab's dedicated Telegram bot via the
public Bot API. No webhook, no Azure Function — just two env vars:

    NAURO_BOT_TOKEN  - bot token from @BotFather
    NAURO_CHAT_ID    - chat id to send to (Sam's user id or a channel id)

If either is missing, send_telegram() is a no-op so local dev and CI
without secrets stay silent rather than failing.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096


def _truncate(text: str, limit: int = TELEGRAM_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n…[truncated]"
    return text[: limit - len(suffix)] + suffix


def send_telegram(
    text: str,
    *,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Send a text message to the NauroLabs ops bot.

    Returns True if delivered, False if skipped or failed. Never raises —
    notifications must not break the pipeline that produced them.

    Reads NAURO_BOT_TOKEN and NAURO_CHAT_ID from env when the matching
    keyword args are not provided.
    """
    token = bot_token or os.environ.get("NAURO_BOT_TOKEN", "")
    chat = chat_id or os.environ.get("NAURO_CHAT_ID", "")

    if not token or not chat:
        log.warning("NAURO_BOT_TOKEN or NAURO_CHAT_ID missing — skipping Telegram send")
        return False

    payload = {
        "chat_id": chat,
        "text": _truncate(text),
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            # One retry on 5xx
            if 500 <= resp.status_code < 600:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
            log.warning(
                "Telegram send failed: HTTP %d %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
    except httpx.HTTPError as e:
        log.warning("Telegram send error: %s", e)
        return False


def send_idea_card(
    repo: str,
    issue_number: int,
    title: str,
    *,
    priority: str = "P2",
    description: str = "",
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Send an idea approval card with 👍 Build / 👎 Not now buttons.

    The buttons carry ``arf:<repo>:<num>:y|n`` and the text echoes ``arf:<repo>:<num>`` so a
    text reply to the card can be attributed back to the issue by nauroBot. ``repo`` may be
    ``OWNER/NAME`` or a bare name — only the bare name is encoded (nauroBot prepends the
    owner). Returns True if delivered, False if skipped or failed. Never raises.
    """
    token = bot_token or os.environ.get("NAURO_BOT_TOKEN", "")
    chat = chat_id or os.environ.get("NAURO_CHAT_ID", "")

    if not token or not chat:
        log.warning("NAURO_BOT_TOKEN or NAURO_CHAT_ID missing — skipping idea card")
        return False

    name = repo.split("/")[-1]
    lines = [f"💡 [{priority}] {name} — {title}"]
    if description:
        lines.append(description)
    lines.append("Proposed by autoRefine · reply with a reason if you decline")
    lines.append(f"arf:{name}:{issue_number}")

    payload = {
        "chat_id": chat,
        "text": _truncate("\n".join(lines)),
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "👍 Build", "callback_data": f"arf:{name}:{issue_number}:y"},
                {"text": "👎 Not now", "callback_data": f"arf:{name}:{issue_number}:n"},
            ]],
        },
    }

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            if 500 <= resp.status_code < 600:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
            log.warning(
                "Idea card send failed: HTTP %d %s", resp.status_code, resp.text[:200]
            )
            return False
    except httpx.HTTPError as e:
        log.warning("Idea card send error: %s", e)
        return False
