"""Telegram channel + heartbeat boot — owner-only dumb pipe to agent.respond."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import OWNER_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from core import agent, heartbeat

log = logging.getLogger("charles.telegram")


async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER_TELEGRAM_ID:
        log.warning("ignored non-owner user_id=%s", user.id if user else None)
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text
    log.info("inbound: %r", text[:200])
    await update.message.chat.send_action(action="typing")
    reply = await asyncio.to_thread(agent.respond, text, str(update.effective_chat.id))
    await update.message.reply_text(reply or "(empty reply)")


async def _post_init(app: Application) -> None:
    """Spawn the heartbeat loop on the same asyncio loop as Telegram polling."""
    asyncio.create_task(heartbeat.loop())
    log.info("heartbeat task spawned")


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("Charles Telegram channel starting (owner=%s)", OWNER_TELEGRAM_ID)
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.run_polling()
