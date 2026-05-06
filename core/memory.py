"""SQLite-backed memory.

Three tables, one file at workspace/memory.db:

  conversations      append-only log of (conversation_id, role, content). Used to
                     replay recent context into the prompt on each turn so Charles
                     stays continuous across Telegram messages.

  long_term_facts    facts Charles chooses to remember (name, place, decision,
                     habit). Added via the `remember` tool, queried via `recall`.
                     NOT auto-injected into every prompt — pulled on demand.

  daily_log          structured event log (kind, text). Used for daily summaries
                     and audits. Charles can query today's entries via tools.

Memory is QUERIED into prompts on demand, never dumped wholesale.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from config import WORKSPACE

# Tool results in history get truncated to this size — full result still goes
# to the live turn that generated it; this is just for replay.
TOOL_RESULT_LOG_CAP = 2000

DB_PATH = WORKSPACE / "memory.db"

log = logging.getLogger("charles.memory")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    tool_calls_json TEXT,
    tool_call_id    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_cid ON conversations(conversation_id, id);

CREATE TABLE IF NOT EXISTS long_term_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fact         TEXT    NOT NULL,
    tags         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_tags ON long_term_facts(tags);

CREATE TABLE IF NOT EXISTS daily_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_daily_log_created ON daily_log(created_at);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Forward-only migrations for already-existing DBs
        cols = {row["name"] for row in c.execute("PRAGMA table_info(conversations)")}
        if "tool_calls_json" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN tool_calls_json TEXT")
        if "tool_call_id" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN tool_call_id TEXT")


# ---------------- Conversations ----------------


def log_turn(conversation_id: str, role: str, content: str) -> None:
    """Persist a user or final-assistant message (no tool calls)."""
    if not content.strip():
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO conversations (conversation_id, role, content) VALUES (?, ?, ?)",
            (conversation_id, role, content),
        )
        c.execute(
            "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
            (f"turn:{role}", f"[{conversation_id}] {content[:500]}"),
        )


def log_assistant_tool_calls(
    conversation_id: str, content: str, tool_calls: list[dict]
) -> None:
    """Persist an assistant turn that emitted tool_calls (may have empty content)."""
    with _conn() as c:
        c.execute(
            "INSERT INTO conversations (conversation_id, role, content, tool_calls_json) "
            "VALUES (?, 'assistant', ?, ?)",
            (conversation_id, content or "", json.dumps(tool_calls)),
        )


def log_tool_result(conversation_id: str, tool_call_id: str, content: str) -> None:
    """Persist a tool result in conversation history (truncated for replay)."""
    truncated = content
    if len(content) > TOOL_RESULT_LOG_CAP:
        truncated = (
            content[:TOOL_RESULT_LOG_CAP]
            + f"\n...[+{len(content) - TOOL_RESULT_LOG_CAP} chars truncated]"
        )
    with _conn() as c:
        c.execute(
            "INSERT INTO conversations (conversation_id, role, content, tool_call_id) "
            "VALUES (?, 'tool', ?, ?)",
            (conversation_id, truncated, tool_call_id),
        )


def recent_history(conversation_id: str, max_chars: int = 4000, max_turns: int = 100) -> list[dict]:
    """Return recent turns (oldest first), trimmed to a char budget.

    Reconstructs full OpenAI format including tool_calls and role=tool rows
    so the model sees the actual cause-effect of past tool use.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, tool_calls_json, tool_call_id "
            "FROM conversations WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, max_turns),
        ).fetchall()

    out: list[dict] = []
    total = 0
    for r in rows:  # newest first
        size = len(r["content"]) + (len(r["tool_calls_json"]) if r["tool_calls_json"] else 0)
        if total + size > max_chars:
            break
        msg: dict = {"role": r["role"], "content": r["content"]}
        if r["tool_calls_json"]:
            msg["tool_calls"] = json.loads(r["tool_calls_json"])
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        out.append(msg)
        total += size
    out.reverse()
    return out


# ---------------- Long-term facts ----------------


def add_fact(fact: str, tags: str = "") -> int:
    fact = fact.strip()
    if not fact:
        raise ValueError("empty fact")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO long_term_facts (fact, tags) VALUES (?, ?)",
            (fact, tags.strip()),
        )
        c.execute(
            "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
            ("remembered", fact[:500]),
        )
        return cur.lastrowid or 0


def search_facts(query: str, limit: int = 5) -> list[dict]:
    """Substring search over fact text and tags. Newest first on ties."""
    q = f"%{query.strip()}%"
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fact, tags, created_at FROM long_term_facts "
            "WHERE fact LIKE ? OR tags LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (q, q, limit),
        ).fetchall()
        if rows:
            ids = ",".join(str(r["id"]) for r in rows)
            c.execute(
                f"UPDATE long_term_facts SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE id IN ({ids})"
            )
    return [dict(r) for r in rows]


def all_facts(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fact, tags, created_at FROM long_term_facts "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- Daily log ----------------


def log_event(kind: str, text: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO daily_log (kind, text) VALUES (?, ?)", (kind, text))


def daily_log_for(date_iso: str | None = None) -> list[dict]:
    """All entries for a UTC date (YYYY-MM-DD). Defaults to today UTC."""
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            "SELECT id, kind, text, created_at FROM daily_log "
            "WHERE substr(created_at, 1, 10) = ? "
            "ORDER BY id ASC",
            (date_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- Lifecycle ----------------

init_db()
