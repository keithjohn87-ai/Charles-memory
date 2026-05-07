"""Goals — long-running open-ended objectives Charles works on across heartbeat ticks.

The difference between a goal and a scheduled_task:
  - scheduled_task: fire at a specific time, do a thing, done (or repeat).
  - goal: an open-ended objective ("review the MOM and build missing tools")
          that gets advanced periodically by the heartbeat until Charles
          marks it done. Each "advance" is a turn through the agent where
          Charles takes one concrete step.

Heartbeat polls `ripe_goals()` every tick (see core/heartbeat.py) and fires
the oldest-ripe goal as a synthetic [goal advance] prompt. Charles takes one
step (read, write, schedule subtask, save fact) and records what he did
in the goal's notes column. Next tick, he picks up where he left off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.memory import _conn

log = logging.getLogger("charles.goals")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    description      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    notes            TEXT NOT NULL DEFAULT '',
    advance_seconds  INTEGER NOT NULL DEFAULT 300,
    last_advanced_at TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_status_lastadv ON goals(status, last_advanced_at);
"""


def init_schema() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _now_iso() -> str:
    # SQLite's strftime('%f') produces SS.SSS (seconds.milliseconds). Python's
    # strftime('%f') produces microseconds only (6 digits, no seconds prefix).
    # We must match SQLite's format so julianday() comparisons work correctly.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def add_goal(description: str, advance_seconds: int = 300) -> dict:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO goals (description, advance_seconds) VALUES (?, ?)",
            (description.strip(), advance_seconds),
        )
        gid = cur.lastrowid or 0
    log.info("goal #%d added: %r (every %ds)", gid, description[:80], advance_seconds)
    return {"id": gid, "description": description, "advance_seconds": advance_seconds}


def list_goals(status: str | None = "active") -> list[dict]:
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT id, description, status, notes, advance_seconds, last_advanced_at, created_at, completed_at "
                "FROM goals WHERE status = ? ORDER BY id ASC",
                (status,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, description, status, notes, advance_seconds, last_advanced_at, created_at, completed_at "
                "FROM goals ORDER BY id ASC",
            ).fetchall()
    return [dict(r) for r in rows]


def get_goal(goal_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, description, status, notes, advance_seconds, last_advanced_at, created_at, completed_at "
            "FROM goals WHERE id = ?",
            (goal_id,),
        ).fetchone()
    return dict(row) if row else None


def append_note(goal_id: int, note: str) -> None:
    """Append a progress note to the goal."""
    stamp = _now_iso()[:19]
    line = f"[{stamp}] {note.strip()}"
    with _conn() as c:
        c.execute(
            "UPDATE goals SET notes = CASE WHEN notes = '' THEN ? "
            "ELSE notes || char(10) || ? END WHERE id = ?",
            (line, line, goal_id),
        )


def mark_advanced(goal_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE goals SET last_advanced_at = ? WHERE id = ?",
            (_now_iso(), goal_id),
        )


def complete(goal_id: int, summary: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE goals SET status='done', completed_at=?, notes = notes || char(10) || char(10) || ? "
            "WHERE id = ? AND status='active'",
            (_now_iso(), f"DONE: {summary.strip()}", goal_id),
        )
        return cur.rowcount > 0


def cancel(goal_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE goals SET status='cancelled' WHERE id = ? AND status='active'",
            (goal_id,),
        )
        return cur.rowcount > 0


def ripe_goals(now_iso: str | None = None, limit: int = 5) -> list[dict]:
    """Return active goals whose last_advanced_at is older than advance_seconds (or null).

    Returned in order: never-advanced first, then oldest-advanced.
    """
    now = now_iso or _now_iso()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, description, status, notes, advance_seconds, last_advanced_at "
            "FROM goals WHERE status='active' "
            "  AND (last_advanced_at IS NULL "
            "       OR (julianday(?) - julianday(last_advanced_at)) * 86400 >= advance_seconds) "
            "ORDER BY (last_advanced_at IS NULL) DESC, last_advanced_at ASC "
            "LIMIT ?",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


init_schema()
