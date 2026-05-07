"""Autonomous heartbeat loop.

Two responsibilities every tick:
  1. Fire any due `scheduled_tasks` (one-shot or recurring time-based work).
  2. Advance one ripe `goal` (open-ended long-burn objectives).

Both produce a synthetic prompt routed through the full agent. Charles
decides what concrete action to take and only notifies John when warranted.

Conversation ids:
  - scheduled task firings → `heartbeat:<task_id>`
  - goal advancements      → `goal:<goal_id>`  (stable across ticks for that goal)
"""
from __future__ import annotations

import asyncio
import logging

from core import goals, scheduler

log = logging.getLogger("charles.heartbeat")

DEFAULT_TICK_SECONDS = 15


async def _run_blocking(prompt: str, conv_id: str) -> tuple[bool, str]:
    from core import agent  # late import — avoids circular at module load

    try:
        reply = await asyncio.to_thread(agent.respond, prompt, conv_id)
        return True, reply or ""
    except Exception as e:  # noqa: BLE001
        log.exception("conv=%s errored", conv_id)
        return False, f"{type(e).__name__}: {e}"


async def _fire_due_tasks() -> None:
    due = scheduler.due_tasks()
    if not due:
        return
    log.info("tick: firing %d task(s)", len(due))
    for task in due:
        scheduler.mark_running(task["id"])
        prompt = (
            f"[heartbeat task #{task['id']}] {task['description']}\n\n"
            f"This is an autonomous tick — not John talking. Decide if this "
            f"requires action. Use notify_john ONLY if John actually needs to know."
        )
        ok, result = await _run_blocking(prompt, f"heartbeat:{task['id']}")
        if ok:
            scheduler.mark_done(task["id"], result, task.get("cadence_seconds"))
        else:
            scheduler.mark_failed(task["id"], result)


_NARRATION_PHRASES = (
    "let me", "i'll", "i will", "i need to", "i'm going to",
    "going to write", "going to create", "going to start",
    "now i need", "now i'll", "writing the", "creating the",
)


def _count_narration_loop(notes: str) -> int:
    """Count how many recent notes look like 'I'll do X' without action.

    Only checks notes from the LAST 6 entries — older history doesn't matter.
    Used to detect when Charles is stuck saying 'let me write the file' over
    and over without ever calling write_file.
    """
    if not notes:
        return 0
    lines = [ln for ln in notes.split("\n") if ln.strip().startswith("[")]
    recent = lines[-6:]
    count = 0
    for line in recent:
        lower = line.lower()
        if any(phrase in lower for phrase in _NARRATION_PHRASES):
            count += 1
    return count


async def _advance_one_goal() -> None:
    ripe = goals.ripe_goals(limit=1)
    if not ripe:
        return
    goal = ripe[0]
    log.info("tick: advancing goal #%d (%s)", goal["id"], goal["description"][:60])
    notes_block = goal["notes"] or "(no notes yet — this is the first advance)"
    narration_count = _count_narration_loop(goal["notes"] or "")

    base_prompt = (
        f"[goal advance #{goal['id']}] {goal['description']}\n\n"
        f"## Progress so far\n{notes_block}\n\n"
        f"## Your job this tick\n"
    )

    if narration_count >= 3:
        # Charles is stuck saying "let me X" without doing X. Force the issue.
        log.warning("goal #%d narration loop detected (count=%d) — injecting strong-action prompt",
                    goal["id"], narration_count)
        action_prompt = (
            f"⚠️ NARRATION LOOP DETECTED: your last {narration_count} notes are all "
            f"'I'll do X' or 'let me write Y' WITHOUT actually doing it. This is the "
            f"failure mode John warned about. THREE OPTIONS — pick one this tick:\n"
            f"  1. ACTUALLY DO IT NOW: call write_file/exec_shell/etc with the real content. "
            f"     If you have the content in your head, write it. If you don't, you're not "
            f"     ready to write — go to option 2 or 3.\n"
            f"  2. RESEARCH FIRST: call search_web, browse_url, or read_file ONCE, then "
            f"     summarize what you found in your final reply. NO 'let me' / 'I'll' phrases.\n"
            f"  3. CANCEL THE GOAL: call cancel_goal(goal_id={goal['id']}) — you don't have "
            f"     the runway for it right now.\n"
            f"Words like 'let me', 'I will', 'writing the', 'going to' are FORBIDDEN in your "
            f"reply this tick. Past-tense only ('I wrote', 'I read', 'I found') OR direct "
            f"action verbs in tool_calls. No more declarations of intent."
        )
    else:
        action_prompt = (
            f"Take ONE concrete step toward this goal right now: read a file, write a file, "
            f"schedule a subtask, save a fact, anything actionable. Your final plain-text reply "
            f"will be AUTO-LOGGED as the next progress note for this goal — so write it as ONE "
            f"sentence describing what you DID this tick (past tense) and what the next concrete "
            f"step is. If the goal is fully complete, call `complete_goal(goal_id={goal['id']}, "
            f"summary=...)` instead. Do NOT call notify_john unless the goal actually finished — "
            f"silent ticks are correct."
        )

    prompt = base_prompt + action_prompt
    goals.mark_advanced(goal["id"])  # mark before running so a slow run doesn't double-fire
    ok, reply = await _run_blocking(prompt, f"goal:{goal['id']}")

    # Auto-append the final reply as a progress note so progress survives even if
    # Charles forgets to call append_goal_note. Skip if the goal got completed/cancelled
    # this turn (status flipped) — its notes already got the completion summary.
    if ok and reply.strip():
        latest = goals.get_goal(goal["id"])
        if latest and latest["status"] == "active":
            note = reply.strip()
            if len(note) > 500:
                note = note[:500] + "…"
            goals.append_note(goal["id"], note)


async def _tick() -> None:
    await _fire_due_tasks()
    await _advance_one_goal()


async def loop(period_seconds: int = DEFAULT_TICK_SECONDS) -> None:
    log.info("heartbeat starting; period=%ds", period_seconds)
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            log.exception("heartbeat tick failed")
        await asyncio.sleep(period_seconds)
