"""notify_john — send a proactive Telegram message to the owner.

Used by Charles when an autonomous tick (heartbeat task, scheduled work)
produces something John actually needs to see. Most heartbeat work should
NOT call this — silence is correct unless one of the SOUL.md criteria
applies.
"""
from __future__ import annotations

import logging

import httpx

from config import OWNER_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from core.tools import tool

log = logging.getLogger("charles.notify")


@tool(
    name="notify_john",
    summary="Send a proactive Telegram message to John. Use ONLY for: meaningful deliverable done, hard blocker needing his input, financial decision, or genuinely time-sensitive. Otherwise stay silent.",
    triggers=("notify john", "ping john", "tell john", "message john", "alert john"),
    schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send. Direct, factual, no preamble. Keep under 1000 chars.",
            },
        },
        "required": ["message"],
    },
)
def notify_john(message: str) -> str:
    text = message.strip()
    if not text:
        return "[error] empty message"
    if len(text) > 4000:
        text = text[:3990] + "…[truncated]"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={"chat_id": OWNER_TELEGRAM_ID, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        log.info("notify_john sent (%d chars)", len(text))
        return f"sent {len(text)} chars to John"
    except Exception as e:  # noqa: BLE001
        log.exception("notify_john failed")
        return f"[error] {type(e).__name__}: {e}"
