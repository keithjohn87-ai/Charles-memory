"""Scheduling tools: schedule_task / list_scheduled_tasks / cancel_scheduled_task."""
from __future__ import annotations

import json

from core import scheduler
from core.tools import tool


@tool(
    name="schedule_task",
    summary="Schedule a future task to fire on the heartbeat. Use in_seconds for relative timing or at_iso for absolute. Optional cadence_seconds repeats after firing.",
    triggers=("schedule", "in 5", "in an hour", "at 9", "remind me", "later", "every"),
    schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What you should do when the task fires. Be specific — this is the prompt your future self will receive.",
            },
            "in_seconds": {
                "type": "number",
                "description": "Fire this many seconds from now. Use this for relative timing.",
            },
            "at_iso": {
                "type": "string",
                "description": "Fire at this UTC ISO timestamp (e.g. 2026-05-07T13:00:00Z). Use only if you have a specific clock time in mind.",
            },
            "cadence_seconds": {
                "type": "integer",
                "description": "If set, after firing, reschedule for now+cadence_seconds. Makes the task recurring.",
            },
        },
        "required": ["description"],
    },
)
def schedule_task(
    description: str,
    in_seconds: float | None = None,
    at_iso: str | None = None,
    cadence_seconds: int | None = None,
) -> str:
    if in_seconds is None and at_iso is None:
        return "[error] provide either in_seconds or at_iso"
    try:
        info = scheduler.schedule(
            description,
            in_seconds=in_seconds,
            at_iso=at_iso,
            cadence_seconds=cadence_seconds,
        )
    except ValueError as e:
        return f"[error] {e}"
    return f"scheduled task #{info['id']} for {info['due_at']}: {description}" + (
        f" (recurring every {cadence_seconds}s)" if cadence_seconds else ""
    )


@tool(
    name="list_scheduled_tasks",
    summary="List scheduled tasks. Filter by status (pending/running/done/failed/cancelled) or pass nothing for pending.",
    triggers=("list scheduled", "what's scheduled", "what tasks", "scheduled tasks", "pending tasks"),
    schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "pending|running|done|failed|cancelled, or 'all' for everything.",
                "default": "pending",
            },
        },
    },
)
def list_scheduled_tasks(status: str = "pending") -> str:
    rows = scheduler.list_tasks(None if status == "all" else status, limit=20)
    if not rows:
        return f"(no tasks with status={status!r})"
    out = []
    for r in rows:
        line = f"#{r['id']} [{r['status']}] due {r['due_at']}: {r['description']}"
        if r.get("cadence_seconds"):
            line += f" (every {r['cadence_seconds']}s)"
        out.append(line)
    return "\n".join(out)


@tool(
    name="cancel_scheduled_task",
    summary="Cancel a pending scheduled task by id.",
    triggers=("cancel task", "cancel scheduled", "remove task", "drop task"),
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "The id from schedule_task or list_scheduled_tasks."},
        },
        "required": ["task_id"],
    },
)
def cancel_scheduled_task(task_id: int) -> str:
    if scheduler.cancel(task_id):
        return f"cancelled task #{task_id}"
    return f"[error] task #{task_id} not found, not pending, or already cancelled"
