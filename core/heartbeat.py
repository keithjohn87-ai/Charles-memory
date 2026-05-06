"""Autonomous heartbeat loop.

Runs alongside the Telegram channel on the same asyncio event loop. Every
tick: pull any scheduled tasks whose due_at has passed and run each through
the full agent. Charles reasons about each fired task like a normal turn —
including deciding whether to notify John (via the notify_john tool).

Heartbeat-fired turns use a synthetic conversation_id of `heartbeat:<task_id>`
so they don't pollute John's main Telegram conversation history.
"""
from __future__ import annotations

import asyncio
import logging

from core import scheduler

log = logging.getLogger("charles.heartbeat")

DEFAULT_TICK_SECONDS = 15


async def _run_task_blocking(task: dict) -> tuple[bool, str]:
    """Run a single task through the agent. Sync agent code via to_thread."""
    from core import agent  # late import — avoids circular at module load

    conv_id = f"heartbeat:{task['id']}"
    synthetic_user = (
        f"[heartbeat task #{task['id']}] {task['description']}\n\n"
        f"This is an autonomous tick — not John talking. Decide if this "
        f"requires action. Use notify_john ONLY if John actually needs to know."
    )
    try:
        reply = await asyncio.to_thread(agent.respond, synthetic_user, conv_id)
        return True, reply or ""
    except Exception as e:  # noqa: BLE001
        log.exception("task #%d errored", task["id"])
        return False, f"{type(e).__name__}: {e}"


async def _tick() -> None:
    due = scheduler.due_tasks()
    if not due:
        return
    log.info("tick: firing %d task(s)", len(due))
    for task in due:
        scheduler.mark_running(task["id"])
        ok, result = await _run_task_blocking(task)
        if ok:
            scheduler.mark_done(task["id"], result, task.get("cadence_seconds"))
        else:
            scheduler.mark_failed(task["id"], result)


async def loop(period_seconds: int = DEFAULT_TICK_SECONDS) -> None:
    log.info("heartbeat starting; period=%ds", period_seconds)
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            log.exception("heartbeat tick failed")
        await asyncio.sleep(period_seconds)
