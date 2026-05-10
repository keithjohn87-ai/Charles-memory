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
    tags         TEXT    NOT NULL DEFAULT '',           -- secondary index (legacy + john-vocab tags)
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    topic        TEXT,                                  -- single primary topic (learning-tree axis)
    source       TEXT,                                  -- where this fact came from (URL / conv_id / tool result)
    confidence   REAL    DEFAULT 1.0,                   -- 0..1, how sure Charles is
    embedding    BLOB                                   -- packed float32 vector from MiniLM (384 dims)
);
CREATE INDEX IF NOT EXISTS idx_facts_tags  ON long_term_facts(tags);
CREATE INDEX IF NOT EXISTS idx_facts_topic ON long_term_facts(topic);

CREATE TABLE IF NOT EXISTS daily_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_daily_log_created ON daily_log(created_at);

-- Charles-authored tasks for John (added 2026-05-09 night). Distinct from
-- approval-pending facts (which are Tier-2 governance) and from open_requests
-- (which are time-tracked follow-ups). This is the general "I need you to do
-- X" surface that lands in the Tasks tab.
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    urgency      TEXT    NOT NULL DEFAULT 'normal',  -- low | normal | high | blocking
    status       TEXT    NOT NULL DEFAULT 'open',    -- open | done | dismissed
    source       TEXT    NOT NULL DEFAULT 'charles', -- charles | auto_extracted | john | watchdog
    source_conv  TEXT,                               -- conv_id where this came from (if any)
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id DESC);
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
        # Skip daily_log noise for progress rows — they're transient liveness pings
        if role != "progress":
            c.execute(
                "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
                (f"turn:{role}", f"[{conversation_id}] {content[:500]}"),
            )


def insert_progress(conversation_id: str, content: str) -> int:
    """Insert a fresh role='progress' row and return its id.

    The progress row is meant to be UPDATED (via update_progress) as work
    advances, so the UI sees one ticker line that mutates in place rather
    than a stack of new rows. Used by agent.respond.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO conversations (conversation_id, role, content) "
            "VALUES (?, 'progress', ?)",
            (conversation_id, content),
        )
        return cur.lastrowid or 0


def update_progress(row_id: int, content: str) -> bool:
    """Replace the content of an existing progress row.

    Returns True if the row was updated (i.e., it still exists and is
    a progress row). False if not found — caller should fall back to
    inserting a fresh row.
    """
    if not row_id:
        return False
    with _conn() as c:
        cur = c.execute(
            "UPDATE conversations SET content=? WHERE id=? AND role='progress'",
            (content, row_id),
        )
        return cur.rowcount > 0


def delete_progress(row_id: int) -> bool:
    """Remove a progress ticker row once the respond chain is done.
    Keeps the UI's conv view clean (no stale ticker lines after the reply)."""
    if not row_id:
        return False
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM conversations WHERE id=? AND role='progress'",
            (row_id,),
        )
        return cur.rowcount > 0


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
    Filters out role='progress' rows — those are UI-only liveness pings
    written by agent.respond after each tool round; they would confuse the
    OpenAI API (unknown role) and bloat the prompt with redundant info.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, tool_calls_json, tool_call_id "
            "FROM conversations WHERE conversation_id = ? AND role != 'progress' "
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


# ---------------- Behavioral health (loop detection) ----------------


def _similarity(a: str, b: str) -> float:
    """Quick similarity ratio without importing difflib (which is slow on long strs)."""
    if not a or not b:
        return 0.0
    a_norm = a.strip().lower()
    b_norm = b.strip().lower()
    if a_norm == b_norm:
        return 1.0
    # First-50-chars match is a strong duplicate signal in our context
    if a_norm[:50] == b_norm[:50] and a_norm[:50]:
        return 0.95
    # Fall back to set-of-words Jaccard — cheap, good enough for "Charles repeated himself"
    sa, sb = set(a_norm.split()), set(b_norm.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def trim_repeating_replies(
    conversation_id: str,
    n_check: int = 3,
    threshold: float = 0.7,
) -> int:
    """Detect & delete a poisoned tail of near-identical assistant turns.

    Looks at the last `n_check` assistant turns in the conversation. If they're
    all >= `threshold` similar to each other, deletes them (plus any user/tool
    turns interleaved between them) so the next prompt isn't loaded with a
    pattern-locking history. Returns the number of rows deleted.

    Called by agent.respond() before adding a new user turn. Conservative on
    purpose — only nukes if the tail is actually broken.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT id, role, content FROM conversations "
            "WHERE conversation_id = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, n_check),
        ).fetchall()
        if len(rows) < n_check:
            return 0
        # All pairwise similarities must clear threshold
        contents = [r["content"] or "" for r in rows]
        for i in range(len(contents)):
            for j in range(i + 1, len(contents)):
                if _similarity(contents[i], contents[j]) < threshold:
                    return 0
        # Find the oldest poisoned id, delete everything from there forward in this conv
        oldest_poisoned_id = min(r["id"] for r in rows)
        deleted = c.execute(
            "DELETE FROM conversations WHERE conversation_id = ? AND id >= ?",
            (conversation_id, oldest_poisoned_id),
        ).rowcount
        log.warning(
            "loop detected in conv=%s — deleted %d turns starting at id=%d (last 3 assistant similarity above %.2f)",
            conversation_id, deleted, oldest_poisoned_id, threshold,
        )
        # Save audit trail
        c.execute(
            "INSERT INTO long_term_facts (fact, tags) VALUES (?, ?)",
            (
                f"Auto-recovery from response loop in conv {conversation_id}: "
                f"deleted {deleted} turns from id {oldest_poisoned_id} onward. "
                f"Pattern locked into a {len(rows)}-turn near-identical reply tail.",
                "incident,loop_recovery,auto",
            ),
        )
        return deleted


def reset_conversation(conversation_id: str, keep_last_user_turn: bool = True) -> int:
    """Manual nuclear option: wipe a conversation's recent tail.

    If keep_last_user_turn=True, leaves only the most recent user turn (so the
    next agent.respond reads it fresh). Otherwise deletes everything.
    Returns rows deleted.
    """
    with _conn() as c:
        if keep_last_user_turn:
            row = c.execute(
                "SELECT id FROM conversations WHERE conversation_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            keep_id = row["id"] if row else 0
            deleted = c.execute(
                "DELETE FROM conversations WHERE conversation_id=? AND id != ?",
                (conversation_id, keep_id),
            ).rowcount
        else:
            deleted = c.execute(
                "DELETE FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).rowcount
        log.warning("manual reset of conv=%s — deleted %d turns", conversation_id, deleted)
        c.execute(
            "INSERT INTO long_term_facts (fact, tags) VALUES (?, ?)",
            (
                f"Manual conversation reset on {conversation_id}: deleted {deleted} turns "
                f"(keep_last_user={keep_last_user_turn}).",
                "incident,manual_reset",
            ),
        )
    # Drop the URL block-list for this conv so a fresh start really IS fresh.
    try:
        from core import tool_guards
        tool_guards.clear_blocked_urls(conversation_id)
    except Exception:  # noqa: BLE001 — never fail a reset for a guard cleanup issue
        pass
    return deleted


# ---------------- Long-term facts ----------------


def add_fact(
    fact: str,
    tags: str = "",
    topic: str | None = None,
    source: str | None = None,
    confidence: float = 1.0,
) -> int:
    """Save a fact. Embeds it once (via core.embeddings) so semantic recall
    works immediately. If topic is None, defaults to the first tag (legacy
    behavior for code paths that haven't been updated yet)."""
    fact = fact.strip()
    if not fact:
        raise ValueError("empty fact")
    # Topic defaults to first tag if not supplied — back-compat for callers
    # that still pass only tags.
    if topic is None:
        first_tag = (tags.split(",") or [""])[0].strip()
        topic = first_tag or None
    # Embed inline. Failure (e.g. model not yet loaded) shouldn't block the
    # save — fall through with embedding=None and the next migration sweep
    # can backfill.
    embedding_bytes: bytes | None = None
    try:
        from core import embeddings as _embed
        embedding_bytes = _embed.encode(fact)
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed for new fact (saving without): %s", e)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO long_term_facts (fact, tags, topic, source, confidence, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fact, tags.strip(), topic, source, float(confidence), embedding_bytes),
        )
        c.execute(
            "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
            ("remembered", fact[:500]),
        )
        return cur.lastrowid or 0


def search_facts(query: str, limit: int = 5) -> list[dict]:
    """LEGACY substring search over fact text and tags. Kept as fallback /
    exact-string lookup. Prefer `semantic_search` for the relational recall
    path — it does cosine similarity instead of LIKE match.
    """
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


def semantic_search(
    query: str,
    limit: int = 5,
    recency_weight: float = 0.10,
    exclude_tags: tuple[str, ...] = (
        "superseded", "intervention,auto", "prune,auto", "credential_scrub", "blocked_url",
    ),
) -> list[dict]:
    """Semantic top-k retrieval. Embeds the query, cosine-compares to every
    fact's stored embedding, optionally blends in a small recency bonus.

    Returns dicts with id, fact, tags, topic, source, confidence, created_at,
    and a `score` field (higher is better). Updates last_used_at on hits so
    we can prune cold facts later.

    `exclude_tags`: substrings to filter out from the candidate pool — these
    are housekeeping rows that shouldn't surface in recall.
    """
    from core import embeddings as _embed
    q = (query or "").strip()
    if not q:
        return []
    qvec = _embed.unpack(_embed.encode(q))

    with _conn() as c:
        # Pull all facts with embeddings — at 1k rows this is cheap. Filter
        # housekeeping rows out at SQL level rather than after retrieval so
        # the top-k pool is clean.
        clauses = ["embedding IS NOT NULL"]
        params: list = []
        for t in exclude_tags:
            clauses.append("tags NOT LIKE ?")
            params.append(f"%{t}%")
        sql = (
            "SELECT id, fact, tags, topic, source, confidence, created_at, last_used_at, embedding "
            f"FROM long_term_facts WHERE {' AND '.join(clauses)}"
        )
        rows = c.execute(sql, params).fetchall()
        if not rows:
            return []

        # Vectorized cosine
        candidates = [(r["id"], r["embedding"]) for r in rows]
        scored = _embed.topk_by_cosine(qvec, candidates, k=max(limit * 3, limit))

        # Build a fast lookup
        row_by_id = {r["id"]: r for r in rows}

        # Optional small recency tweak — sort by score, but bump scores by a
        # tiny amount based on how recent the fact is. Keeps fresh findings
        # competitive with older topical hits without dominating.
        import time
        now_t = time.time()
        adjusted: list[tuple[int, float]] = []
        for fid, sim in scored:
            row = row_by_id[fid]
            recency_bonus = 0.0
            if recency_weight > 0 and row["created_at"]:
                # crude age bonus: facts in last 24h get up to +recency_weight,
                # decaying to 0 over ~30 days
                try:
                    age_sec = now_t - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
                    days = age_sec / 86400.0
                    decay = max(0.0, 1.0 - (days / 30.0))
                    recency_bonus = recency_weight * decay
                except Exception:  # noqa: BLE001
                    pass
            adjusted.append((fid, sim + recency_bonus))

        adjusted.sort(key=lambda t: -t[1])
        top = adjusted[:limit]

        # Touch last_used_at for surfaced facts
        if top:
            ids = ",".join(str(fid) for fid, _ in top)
            c.execute(
                f"UPDATE long_term_facts SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE id IN ({ids})"
            )

        results: list[dict] = []
        for fid, score in top:
            r = row_by_id[fid]
            results.append({
                "id": r["id"],
                "fact": r["fact"],
                "tags": r["tags"],
                "topic": r["topic"],
                "source": r["source"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
                "score": round(score, 4),
            })
        return results


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


# ---------------- Tasks (Charles → John, surfaces in Tasks tab) ----------------


def add_task(
    title: str,
    description: str = "",
    urgency: str = "normal",
    source: str = "charles",
    source_conv: str | None = None,
) -> int:
    """Create a task — appears in WarRoom 'Tasks' tab + iOS Tasks badge.

    `urgency`: low | normal | high | blocking
    `source`: charles (he made it) | auto_extracted (from his chat reply) |
              john (added in UI) | watchdog (the immune system flagged it)
    Returns the new task id.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("task title required")
    if urgency not in ("low", "normal", "high", "blocking"):
        urgency = "normal"
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (title, description, urgency, source, source_conv) VALUES (?, ?, ?, ?, ?)",
            (title, description.strip(), urgency, source, source_conv),
        )
        c.execute(
            "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
            ("task_added", f"[{urgency}] {title}"),
        )
        return cur.lastrowid or 0


def list_tasks(status: str | None = "open", limit: int = 50) -> list[dict]:
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT id, title, description, urgency, status, source, source_conv, "
                "       created_at, completed_at "
                "FROM tasks WHERE status=? ORDER BY "
                "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 "
                "               WHEN 'normal' THEN 2 ELSE 3 END, id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, title, description, urgency, status, source, source_conv, "
                "       created_at, completed_at FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def complete_task(task_id: int, note: str = "") -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE tasks SET status='done', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id=? AND status='open'",
            (task_id,),
        )
        if note and cur.rowcount:
            c.execute(
                "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
                ("task_done", f"#{task_id}: {note[:200]}"),
            )
        return cur.rowcount > 0


def dismiss_task(task_id: int, reason: str = "") -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE tasks SET status='dismissed', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id=? AND status='open'",
            (task_id,),
        )
        if cur.rowcount:
            c.execute(
                "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
                ("task_dismissed", f"#{task_id}: {reason[:200] or '(no reason given)'}"),
            )
        return cur.rowcount > 0


# ---------------- Lifecycle ----------------

init_db()
