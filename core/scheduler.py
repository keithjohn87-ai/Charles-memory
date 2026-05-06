"""Scheduled task storage. SQLite-backed, append-only with status updates.

A scheduled task = (description, due_at, optional cadence). The heartbeat loop
in core/heartbeat.py polls `due_tasks()` every tick and runs anything overdue
through the full agent so Charles can reason about it just like a normal turn.

Cadence_seconds means "after firing, reschedule for now+cadence." None = one-shot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.memory import _conn  # share the same DB connection helper

log = logging.getLogger("charles.scheduler")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    description     TEXT    NOT NULL,
    due_at          TEXT    NOT NULL,
    cadence_seconds INTEGER,
    status          TEXT    NOT NULL DEFAULT 'pending',
    last_run_at     TEXT,
    last_result     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_sched_status_due ON scheduled_tasks(status, due_at);
"""


def init_schema() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def schedule(
    description: str,
    *,
    in_seconds: float | None = None,
    at_iso: str | None = None,
    cadence_seconds: int | None = None,
) -> dict:
    """Insert a task. Exactly one of in_seconds or at_iso is required."""
    if (in_seconds is None) == (at_iso is None):
        raise ValueError("provide exactly one of in_seconds or at_iso")
    if in_seconds is not None:
        due = datetime.now(timezone.utc) + timedelta(seconds=float(in_seconds))
        due_at = due.strftime("%Y-%m-%dT%H:%M:%fZ")
    else:
        # Accept naive ISO; assume UTC if no offset
        due_at = at_iso  # type: ignore[assignment]
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled_tasks (description, due_at, cadence_seconds) "
            "VALUES (?, ?, ?)",
            (description.strip(), due_at, cadence_seconds),
        )
        tid = cur.lastrowid or 0
    log.info("scheduled task #%d at %s: %r", tid, due_at, description[:60])
    return {"id": tid, "description": description, "due_at": due_at, "cadence_seconds": cadence_seconds}


def due_tasks(now_iso: str | None = None) -> list[dict]:
    now = now_iso or _now_iso()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, description, due_at, cadence_seconds "
            "FROM scheduled_tasks WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at ASC LIMIT 10",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_running(task_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_tasks SET status='running', last_run_at=? WHERE id=?",
            (_now_iso(), task_id),
        )


def mark_done(task_id: int, result: str, cadence_seconds: int | None) -> None:
    """Mark complete. If cadence_seconds, reset to pending for next interval."""
    with _conn() as c:
        if cadence_seconds:
            new_due = (
                datetime.now(timezone.utc) + timedelta(seconds=cadence_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%fZ")
            c.execute(
                "UPDATE scheduled_tasks SET status='pending', due_at=?, last_result=? WHERE id=?",
                (new_due, result[:2000], task_id),
            )
        else:
            c.execute(
                "UPDATE scheduled_tasks SET status='done', last_result=? WHERE id=?",
                (result[:2000], task_id),
            )


def mark_failed(task_id: int, err: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_tasks SET status='failed', last_result=? WHERE id=?",
            (err[:2000], task_id),
        )


def list_tasks(status: str | None = "pending", limit: int = 50) -> list[dict]:
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT id, description, due_at, cadence_seconds, status, last_run_at, last_result "
                "FROM scheduled_tasks WHERE status = ? ORDER BY due_at ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, description, due_at, cadence_seconds, status, last_run_at, last_result "
                "FROM scheduled_tasks ORDER BY due_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def cancel(task_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE scheduled_tasks SET status='cancelled' WHERE id=? AND status='pending'",
            (task_id,),
        )
        return cur.rowcount > 0


# Initialize on import — same pattern as core.memory
init_schema()
