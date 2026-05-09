"""Telegram channel + heartbeat boot — owner-only dumb pipe to agent.respond."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import OWNER_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from core import agent, heartbeat

log = logging.getLogger("charles.telegram")

_VOICE_TMP = Path("/tmp")


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
    text_reply = reply or "(empty reply)"
    await update.message.reply_text(text_reply)

    # Also send voice reply (always, unless disabled)
    if reply:
        try:
            from core import speak as _speak
            ogg = await asyncio.to_thread(_speak.speak_to_ogg, reply)
            try:
                with ogg.open("rb") as fh:
                    await update.message.reply_voice(voice=fh)
            finally:
                ogg.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log.warning("voice-out failed (text reply already sent): %s", e)


async def _on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER_TELEGRAM_ID:
        log.warning("ignored voice from non-owner user_id=%s", user.id if user else None)
        return
    if not update.message:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    log.info("inbound voice: file_id=%s duration=%ss", voice.file_id, getattr(voice, "duration", "?"))
    await update.message.chat.send_action(action="typing")

    f = await ctx.bot.get_file(voice.file_id)
    tmp = _VOICE_TMP / f"charles_voice_{voice.file_unique_id}.oga"
    await f.download_to_drive(custom_path=str(tmp))

    # Transcribe (sync via mlx_whisper — wrap in to_thread)
    try:
        from core import transcribe as _transcribe
        text = await asyncio.to_thread(_transcribe.transcribe, str(tmp))
    except Exception as e:  # noqa: BLE001
        log.exception("transcription failed")
        await update.message.reply_text(f"[transcribe error] {type(e).__name__}: {e}")
        return
    finally:
        if tmp.exists():
            tmp.unlink()

    if not text:
        await update.message.reply_text("[empty transcript]")
        return

    log.info("transcript (%d chars): %r", len(text), text[:200])

    reply = await asyncio.to_thread(agent.respond, text, str(update.effective_chat.id))
    # Text reply with transcript echo (so user can spot mistranscriptions)
    combined = f"📝 \"{text}\"\n\n{reply or '(empty reply)'}"
    await update.message.reply_text(combined)

    # Voice-out: mirror the input mode. If they spoke, Charles speaks back too.
    if reply:
        try:
            from core import speak as _speak
            ogg = await asyncio.to_thread(_speak.speak_to_ogg, reply)
            try:
                with ogg.open("rb") as fh:
                    await update.message.reply_voice(voice=fh)
            finally:
                ogg.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log.warning("voice-out failed (text reply already sent): %s", e)


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
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))
    app.run_polling()
