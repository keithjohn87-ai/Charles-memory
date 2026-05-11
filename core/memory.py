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

-- Learning-tree Phase 2 (2026-05-10): per-project structured state.
-- Replaces the "ask Charles to count" anti-pattern with a single source
-- of truth that returns the same number every time. The URL corpus
-- (goal #10003) is the proving ground but the schema is generic — any
-- long-running initiative with item-level status can use it.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    UNIQUE NOT NULL,                      -- URL-safe identifier
    title       TEXT    NOT NULL,
    description TEXT,
    status      TEXT    NOT NULL DEFAULT 'active',            -- active | paused | done | cancelled
    goal_id     INTEGER,                                       -- optional link to goals(id)
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS project_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,                          -- order within project
    item_key        TEXT    NOT NULL,                          -- URL / file path / unique identifier
    title           TEXT,                                      -- human-readable label
    item_type       TEXT,                                      -- url | file | task | etc.
    status          TEXT    NOT NULL DEFAULT 'pending',        -- pending | in_progress | done | blocked | skipped | paywalled | 404
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    fact_count      INTEGER NOT NULL DEFAULT 0,                -- # facts derived from this item
    last_attempt_at TEXT,
    last_error      TEXT,                                      -- short reason for blocked/404/skipped
    notes           TEXT,                                      -- free-text for human/Charles annotations
    UNIQUE(project_id, position),
    UNIQUE(project_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_project_items_status ON project_items(project_id, status);
CREATE INDEX IF NOT EXISTS idx_project_items_key    ON project_items(item_key);

-- Learning-tree Phase 3 (2026-05-10): topic hierarchy + cached summaries.
-- The `topic` column on long_term_facts is the foreign key (by name). This
-- table holds the topic's metadata: human-readable title, parent for
-- hierarchy, fact_count cache, and the cached one-paragraph summary that
-- recall_topic() returns.
CREATE TABLE IF NOT EXISTS topics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    UNIQUE NOT NULL,                  -- slug-like, matches long_term_facts.topic
    title           TEXT,                                      -- human-readable
    description     TEXT,
    summary         TEXT,                                      -- cached one-paragraph summary
    parent_topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    fact_count      INTEGER NOT NULL DEFAULT 0,
    summary_updated_at TEXT,
    last_fact_at    TEXT,                                      -- when most recent fact under this topic was added
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_topics_parent ON topics(parent_topic_id);

-- Learning-tree Phase 4 (2026-05-10): skill registry.
-- Tracks what Charles has learned to DO (not just know). Tool patterns,
-- recovery procedures, behavioral habits. Levels: novice / practiced /
-- expert. `set_mastery` tool writes here; future tool-call sites can
-- consult this to know what Charles can confidently do without
-- re-learning. Promotions are evidence-based — a skill goes from
-- novice → practiced after N successful demonstrations.
CREATE TABLE IF NOT EXISTS skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    UNIQUE NOT NULL,                     -- slug-like
    title        TEXT,                                         -- human-readable
    description  TEXT,
    level        TEXT    NOT NULL DEFAULT 'novice',           -- novice | practiced | expert
    evidence     TEXT,                                         -- last evidence statement
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_skills_level ON skills(level);

-- Learning-tree Phase 5 (2026-05-10): John-preferences ledger.
-- John's settled doctrine in queryable form. Claude Code's memory files
-- (`feedback_*.md`) live in ~/.claude/projects/.../memory/ and Charles
-- can't read them. This table makes that doctrine accessible to Charles
-- so he can ask "what does John think about X?" and get a deterministic
-- answer. Update on every "from now on..." / "always do X" / "never do Y"
-- John directive.
CREATE TABLE IF NOT EXISTS john_prefs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT    NOT NULL,                            -- comms | autonomy | technical | personal | scheduling | tooling
    rule         TEXT    NOT NULL,                            -- the actual rule, imperative form
    why          TEXT,                                         -- context / reasoning
    how_to_apply TEXT,                                         -- when this rule fires
    source       TEXT,                                         -- where it came from (memory file / session date)
    learned_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_john_prefs_category ON john_prefs(category);
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
        # Save audit trail — route through canonical taxonomy.
        # Topic 'loop_recovery' is a canonical leaf under system_health.
        _audit_fact = (
            f"Auto-recovery from response loop in conv {conversation_id}: "
            f"deleted {deleted} turns from id {oldest_poisoned_id} onward. "
            f"Pattern locked into a {len(rows)}-turn near-identical reply tail."
        )
        _audit_embed = None
        try:
            from core import embeddings as _embed
            _audit_embed = _embed.encode(_audit_fact)
        except Exception:  # noqa: BLE001
            pass
        c.execute(
            "INSERT INTO long_term_facts (fact, tags, topic, embedding) VALUES (?, ?, ?, ?)",
            (_audit_fact, "incident,loop_recovery,auto", "loop_recovery", _audit_embed),
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
        _audit_fact = (
            f"Manual conversation reset on {conversation_id}: deleted {deleted} turns "
            f"(keep_last_user={keep_last_user_turn})."
        )
        _audit_embed = None
        try:
            from core import embeddings as _embed
            _audit_embed = _embed.encode(_audit_fact)
        except Exception:  # noqa: BLE001
            pass
        c.execute(
            "INSERT INTO long_term_facts (fact, tags, topic, embedding) VALUES (?, ?, ?, ?)",
            (_audit_fact, "incident,manual_reset", "manual_reset", _audit_embed),
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
    """Save a fact. Embeds it once (via core.embeddings). Topic is matched
    against the canonical taxonomy (topics table). If topic isn't in the
    canonical list, semantic-matches against existing leaf topics and
    auto-routes to the closest. If no good match, parks under
    'uncategorized'.

    INGESTION GATE (2026-05-10 evening, per John): topics are NOT auto-created
    from free-text tags anymore. The taxonomy is John's curated tree. New
    topics only land via explicit topic_upsert by Charles or John.
    """
    fact = fact.strip()
    if not fact:
        raise ValueError("empty fact")

    # Embed inline. Failure (e.g. model not yet loaded) shouldn't block the
    # save — fall through with embedding=None.
    embedding_bytes: bytes | None = None
    try:
        from core import embeddings as _embed
        embedding_bytes = _embed.encode(fact)
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed for new fact (saving without): %s", e)

    # INGESTION GATE — match topic against canonical taxonomy
    canonical_topic = _route_to_canonical_topic(topic, tags, fact, embedding_bytes)

    with _conn() as c:
        cur = c.execute(
            "INSERT INTO long_term_facts (fact, tags, topic, source, confidence, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fact, tags.strip(), canonical_topic, source, float(confidence), embedding_bytes),
        )
        c.execute(
            "INSERT INTO daily_log (kind, text) VALUES (?, ?)",
            ("remembered", fact[:500]),
        )
        return cur.lastrowid or 0


def _route_to_canonical_topic(
    requested_topic: str | None,
    tags: str,
    fact_text: str,
    embedding_bytes: bytes | None,
) -> str:
    """Route a fact to a canonical topic. Order of preference:
    1. requested_topic if it exists in the topics table
    2. Any tag that matches an existing topic name
    3. Semantic-match against leaf topics' descriptions (best cosine)
    4. 'uncategorized' as final fallback

    Returns the canonical topic name to write into long_term_facts.topic.
    """
    # Normalize the requested topic to slug form
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_").replace("/", "_")

    with _conn() as c:
        # Build the canonical leaf list (topics that have a parent — these are
        # the buckets we want to route facts into, not the parent containers).
        leaves = c.execute(
            "SELECT name, title, description FROM topics WHERE parent_topic_id IS NOT NULL"
        ).fetchall()
        leaf_names = {r["name"] for r in leaves}
        # Also include parents themselves as valid topics — sometimes a fact
        # doesn't fit any leaf and the parent is the right bucket.
        all_topics = c.execute("SELECT name FROM topics").fetchall()
        all_topic_names = {r["name"] for r in all_topics}

    # 1. requested topic, if canonical
    if requested_topic:
        slug = _slug(requested_topic)
        if slug in all_topic_names:
            return slug

    # 2. any tag that matches a canonical topic
    if tags:
        for t in tags.split(","):
            slug = _slug(t)
            if slug in all_topic_names:
                return slug

    # 3. semantic match against ALL topics (parents + leaves), prefer leaves
    if embedding_bytes:
        try:
            from core import embeddings as _embed
            qvec = _embed.unpack(embedding_bytes)
            with _conn() as c:
                all_rows = c.execute(
                    "SELECT name, title, description, parent_topic_id FROM topics"
                ).fetchall()
            if all_rows:
                candidates = []
                for r in all_rows:
                    desc = f"{r['title']}. {r['description'] or ''}"
                    blob = _embed.encode(desc)
                    candidates.append((r["name"], blob))
                scored = _embed.topk_by_cosine(qvec, candidates, k=3)
                # Prefer leaves over parents — if top hit is a parent and any
                # leaf is within 0.05 below it, take the leaf instead
                row_by_name = {r["name"]: r for r in all_rows}
                top_name, top_score = scored[0]
                if top_score >= 0.12:
                    # Check if top hit is a parent and a close-enough leaf exists
                    top_row = row_by_name[top_name]
                    if top_row["parent_topic_id"] is None:
                        for cand_name, cand_score in scored[1:]:
                            cand_row = row_by_name[cand_name]
                            if cand_row["parent_topic_id"] is not None and cand_score >= top_score - 0.05:
                                return cand_name
                    return top_name
        except Exception as e:  # noqa: BLE001
            log.warning("semantic topic-route failed (falling back): %s", e)

    # 4. fallback
    # Make sure 'uncategorized' exists in the topics table so the FK-like
    # relationship still holds
    with _conn() as c:
        existing = c.execute("SELECT id FROM topics WHERE name='uncategorized'").fetchone()
        if not existing:
            c.execute(
                "INSERT INTO topics (name, title, description) VALUES (?, ?, ?)",
                ("uncategorized", "Uncategorized", "Facts that didn't match any canonical topic. Review and re-route periodically."),
            )
    return "uncategorized"


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


# ---------------- Projects (learning-tree Phase 2) ----------------
#
# Per-project structured state. Replaces the "ask Charles to count" pattern.
# Every long-running initiative gets a `projects` row + `project_items` rows.
# Counting and status are deterministic SQL queries — same answer every time.


def project_create(
    slug: str,
    title: str,
    description: str = "",
    goal_id: int | None = None,
) -> int:
    """Create a project. Idempotent on slug (returns existing id if present)."""
    slug = slug.strip()
    if not slug:
        raise ValueError("project slug required")
    with _conn() as c:
        existing = c.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if existing:
            return existing["id"]
        cur = c.execute(
            "INSERT INTO projects (slug, title, description, goal_id) VALUES (?, ?, ?, ?)",
            (slug, title.strip(), description.strip(), goal_id),
        )
        return cur.lastrowid or 0


def project_get(slug: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def project_register_items(slug: str, items: list[dict]) -> int:
    """Bulk-insert items into a project. Each item dict: {key, title?, type?,
    position?}. `key` must be unique within the project. Returns count
    inserted. Idempotent on (project, key): existing items are skipped, not
    re-inserted (so re-running this is safe)."""
    proj = project_get(slug)
    if not proj:
        raise ValueError(f"project {slug!r} not found — create it first")
    inserted = 0
    with _conn() as c:
        # Find the highest existing position so new items append after
        max_pos_row = c.execute(
            "SELECT COALESCE(MAX(position), -1) AS p FROM project_items WHERE project_id=?",
            (proj["id"],),
        ).fetchone()
        next_pos = (max_pos_row["p"] if max_pos_row else -1) + 1
        for item in items:
            key = (item.get("key") or "").strip()
            if not key:
                continue
            # Skip if already present
            exists = c.execute(
                "SELECT 1 FROM project_items WHERE project_id=? AND item_key=?",
                (proj["id"], key),
            ).fetchone()
            if exists:
                continue
            pos = item.get("position")
            if pos is None:
                pos = next_pos
                next_pos += 1
            c.execute(
                "INSERT INTO project_items (project_id, position, item_key, title, item_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (proj["id"], pos, key, item.get("title", ""), item.get("type", "")),
            )
            inserted += 1
    return inserted


def project_mark_item(
    slug: str,
    item_key: str,
    status: str | None = None,
    last_error: str | None = None,
    fact_count_delta: int = 0,
    notes: str | None = None,
    increment_attempts: bool = True,
) -> bool:
    """Update a single item's state. Returns True if a row was updated.

    - `status`: pending | in_progress | done | blocked | skipped | paywalled | 404
    - `last_error`: short reason for non-success statuses
    - `fact_count_delta`: how many new facts were derived this attempt
    - `notes`: append to existing notes column (newline-joined)
    - `increment_attempts`: bump attempt_count (default True)
    """
    proj = project_get(slug)
    if not proj:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM project_items WHERE project_id=? AND item_key=?",
            (proj["id"], item_key),
        ).fetchone()
        if not row:
            return False
        new_status = status if status else row["status"]
        new_error = last_error if last_error is not None else row["last_error"]
        new_facts = (row["fact_count"] or 0) + max(0, fact_count_delta)
        new_attempts = (row["attempt_count"] or 0) + (1 if increment_attempts else 0)
        new_notes = row["notes"] or ""
        if notes:
            new_notes = (new_notes + ("\n" if new_notes else "") + notes.strip())[:4000]
        c.execute(
            "UPDATE project_items "
            "SET status=?, last_error=?, fact_count=?, attempt_count=?, "
            "    last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), notes=? "
            "WHERE id=?",
            (new_status, new_error, new_facts, new_attempts, new_notes, row["id"]),
        )
        c.execute(
            "UPDATE projects SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (proj["id"],),
        )
    return True


def project_status(slug: str) -> dict:
    """Return aggregate counts + recent activity for a project.

    Output shape:
    {
        slug, title, status (project-level),
        total, by_status: {pending: N, done: N, blocked: N, ...},
        progress_pct: float,
        last_attempt_at: ISO,
        recent_done: [item_key, ...],          # last 5 done items
        recent_blocked: [item_key, ...],       # last 5 blocked items
    }
    """
    proj = project_get(slug)
    if not proj:
        return {}
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM project_items WHERE project_id=? GROUP BY status",
            (proj["id"],),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        last_attempt = c.execute(
            "SELECT MAX(last_attempt_at) AS t FROM project_items WHERE project_id=?",
            (proj["id"],),
        ).fetchone()
        recent_done = c.execute(
            "SELECT item_key FROM project_items WHERE project_id=? AND status='done' "
            "ORDER BY last_attempt_at DESC LIMIT 5",
            (proj["id"],),
        ).fetchall()
        recent_blocked = c.execute(
            "SELECT item_key FROM project_items "
            "WHERE project_id=? AND status IN ('blocked','paywalled','404') "
            "ORDER BY last_attempt_at DESC LIMIT 5",
            (proj["id"],),
        ).fetchall()
    done = by_status.get("done", 0)
    progress = (100.0 * done / total) if total else 0.0
    return {
        "slug": proj["slug"],
        "title": proj["title"],
        "status": proj["status"],
        "total": total,
        "by_status": by_status,
        "progress_pct": round(progress, 1),
        "last_attempt_at": last_attempt["t"] if last_attempt else None,
        "recent_done": [r["item_key"] for r in recent_done],
        "recent_blocked": [r["item_key"] for r in recent_blocked],
    }


def project_next_pending(slug: str) -> dict | None:
    """Return the next pending item (lowest position). None if all done."""
    proj = project_get(slug)
    if not proj:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM project_items "
            "WHERE project_id=? AND status='pending' "
            "ORDER BY position ASC LIMIT 1",
            (proj["id"],),
        ).fetchone()
    return dict(row) if row else None


def project_list_items(slug: str, status: str | None = None, limit: int = 100) -> list[dict]:
    """List items in a project, optionally filtered by status. Position-ordered."""
    proj = project_get(slug)
    if not proj:
        return []
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM project_items WHERE project_id=? AND status=? "
                "ORDER BY position ASC LIMIT ?",
                (proj["id"], status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM project_items WHERE project_id=? "
                "ORDER BY position ASC LIMIT ?",
                (proj["id"], limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------- Topics (learning-tree Phase 3) ----------------
#
# Topic = a named bucket of facts. The `long_term_facts.topic` column is the
# foreign key (by name, not id, for stability across reseeds). Each unique
# topic name gets a row in `topics` with metadata + a cached summary.
#
# Hierarchy: optional via `parent_topic_id`. Currently unset for backfilled
# topics; Charles can promote later (e.g. group "kahneman_tversky" under
# parent "cognitive_psychology").


def topic_upsert(name: str, title: str | None = None, description: str | None = None) -> int:
    """Insert or update a topic row. Returns id."""
    name = name.strip().lower().replace(" ", "_")
    if not name:
        raise ValueError("topic name required")
    with _conn() as c:
        existing = c.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()
        if existing:
            if title or description:
                c.execute(
                    "UPDATE topics SET title=COALESCE(?, title), description=COALESCE(?, description) WHERE id=?",
                    (title, description, existing["id"]),
                )
            return existing["id"]
        cur = c.execute(
            "INSERT INTO topics (name, title, description) VALUES (?, ?, ?)",
            (name, title or name.replace("_", " ").title(), description),
        )
        return cur.lastrowid or 0


def topic_recount() -> int:
    """Rebuild fact_count and last_fact_at columns for all topics by scanning
    long_term_facts. Also upserts any topic referenced in facts that doesn't
    have a row in the topics table yet. Returns number of topic rows touched."""
    with _conn() as c:
        # Find all distinct topics used by facts
        used = c.execute(
            "SELECT topic, COUNT(*) AS n, MAX(created_at) AS last "
            "FROM long_term_facts WHERE topic IS NOT NULL AND topic != '' "
            "GROUP BY topic"
        ).fetchall()
        touched = 0
        for row in used:
            tname = row["topic"]
            n = row["n"]
            last = row["last"]
            # Upsert the topic
            existing = c.execute("SELECT id FROM topics WHERE name=?", (tname,)).fetchone()
            if existing:
                c.execute(
                    "UPDATE topics SET fact_count=?, last_fact_at=? WHERE id=?",
                    (n, last, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO topics (name, title, fact_count, last_fact_at) VALUES (?, ?, ?, ?)",
                    (tname, tname.replace("_", " ").title(), n, last),
                )
            touched += 1
    return touched


def topic_list(limit: int = 50, min_facts: int = 1) -> list[dict]:
    """List topics ordered by fact_count desc."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, title, summary, parent_topic_id, fact_count, last_fact_at, summary_updated_at "
            "FROM topics WHERE fact_count >= ? ORDER BY fact_count DESC LIMIT ?",
            (min_facts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def topic_facts(name: str, limit: int = 10) -> list[dict]:
    """Return facts under a topic, newest first."""
    name = (name or "").strip().lower().replace(" ", "_")
    if not name:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT id, fact, tags, topic, source, confidence, created_at "
            "FROM long_term_facts WHERE topic=? "
            "AND tags NOT LIKE '%superseded%' AND tags NOT LIKE '%intervention,auto%' "
            "ORDER BY id DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def topic_set_parent(child_name: str, parent_name: str | None) -> bool:
    """Wire a topic into the hierarchy. parent_name=None unsets. Returns True
    if the child topic was found."""
    child_name = (child_name or "").strip().lower().replace(" ", "_")
    with _conn() as c:
        child = c.execute("SELECT id FROM topics WHERE name=?", (child_name,)).fetchone()
        if not child:
            return False
        if parent_name is None:
            c.execute("UPDATE topics SET parent_topic_id=NULL WHERE id=?", (child["id"],))
            return True
        parent_name = parent_name.strip().lower().replace(" ", "_")
        parent = c.execute("SELECT id FROM topics WHERE name=?", (parent_name,)).fetchone()
        if not parent:
            # Auto-create parent topic with no facts
            cur = c.execute(
                "INSERT INTO topics (name, title) VALUES (?, ?)",
                (parent_name, parent_name.replace("_", " ").title()),
            )
            pid = cur.lastrowid or 0
        else:
            pid = parent["id"]
        if pid == child["id"]:
            return False
        c.execute("UPDATE topics SET parent_topic_id=? WHERE id=?", (pid, child["id"]))
    return True


def topic_set_summary(name: str, summary: str) -> bool:
    """Cache a summary string on a topic. Updates summary_updated_at."""
    name = (name or "").strip().lower().replace(" ", "_")
    with _conn() as c:
        existing = c.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()
        if not existing:
            return False
        c.execute(
            "UPDATE topics SET summary=?, summary_updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (summary.strip(), existing["id"]),
        )
    return True


def topic_tree() -> list[dict]:
    """Return all topics with children populated as nested 'children' lists.
    Only roots (parent_topic_id IS NULL) are at the top level."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, title, parent_topic_id, fact_count FROM topics ORDER BY name"
        ).fetchall()
    nodes = {r["id"]: dict(r, children=[]) for r in rows}
    roots = []
    for r in rows:
        parent = r["parent_topic_id"]
        if parent and parent in nodes:
            nodes[parent]["children"].append(nodes[r["id"]])
        else:
            roots.append(nodes[r["id"]])
    return roots


# ---------------- Skills (learning-tree Phase 4) ----------------
#
# Per-skill registry. A skill is something Charles has learned to DO —
# a tool pattern, recovery procedure, behavioral habit. Tracks level
# (novice/practiced/expert), evidence, and a success/failure ledger.


_SKILL_LEVELS = ("novice", "practiced", "expert")


def skill_upsert(name: str, title: str = "", description: str = "") -> int:
    """Create or update a skill row. Returns id."""
    name = name.strip().lower().replace(" ", "_")
    if not name:
        raise ValueError("skill name required")
    with _conn() as c:
        existing = c.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        if existing:
            if title or description:
                c.execute(
                    "UPDATE skills SET title=COALESCE(?,title), description=COALESCE(?,description) WHERE id=?",
                    (title or None, description or None, existing["id"]),
                )
            return existing["id"]
        cur = c.execute(
            "INSERT INTO skills (name, title, description) VALUES (?, ?, ?)",
            (name, title or name.replace("_", " ").title(), description),
        )
        return cur.lastrowid or 0


def skill_set_level(name: str, level: str, evidence: str = "") -> bool:
    """Set a skill's level. Returns True if the row was updated."""
    name = name.strip().lower().replace(" ", "_")
    level = level.strip().lower()
    if level not in _SKILL_LEVELS:
        raise ValueError(f"level must be one of {_SKILL_LEVELS}")
    with _conn() as c:
        row = c.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        if not row:
            # Auto-upsert when promoting an unknown skill
            cur = c.execute(
                "INSERT INTO skills (name, title, level, evidence) VALUES (?, ?, ?, ?)",
                (name, name.replace("_", " ").title(), level, evidence),
            )
            return bool(cur.lastrowid)
        c.execute(
            "UPDATE skills SET level=?, evidence=?, last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (level, evidence, row["id"]),
        )
    return True


def skill_record_attempt(name: str, success: bool, evidence: str = "") -> bool:
    """Log a single use of a skill — success or failure. Auto-promotes
    novice→practiced after 3 successes, practiced→expert after 10. Returns
    True if a row was touched."""
    name = name.strip().lower().replace(" ", "_")
    with _conn() as c:
        row = c.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if not row:
            # Auto-create at novice with first attempt logged
            level = "novice"
            cur = c.execute(
                "INSERT INTO skills (name, title, level, success_count, failure_count, evidence, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (name, name.replace("_", " ").title(), level,
                 1 if success else 0, 0 if success else 1, evidence),
            )
            return bool(cur.lastrowid)
        succ = (row["success_count"] or 0) + (1 if success else 0)
        fail = (row["failure_count"] or 0) + (0 if success else 1)
        # Auto-promote based on success count
        new_level = row["level"]
        if new_level == "novice" and succ >= 3:
            new_level = "practiced"
        if new_level == "practiced" and succ >= 10:
            new_level = "expert"
        c.execute(
            "UPDATE skills SET success_count=?, failure_count=?, level=?, "
            "evidence=COALESCE(?, evidence), last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (succ, fail, new_level, evidence or None, row["id"]),
        )
    return True


def skill_list(level: str | None = None, limit: int = 50) -> list[dict]:
    with _conn() as c:
        if level:
            rows = c.execute(
                "SELECT * FROM skills WHERE level=? ORDER BY success_count DESC, last_used_at DESC LIMIT ?",
                (level, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM skills ORDER BY success_count DESC, last_used_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def skill_get(name: str) -> dict | None:
    name = name.strip().lower().replace(" ", "_")
    with _conn() as c:
        row = c.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


# ---------------- John Preferences (learning-tree Phase 5) ----------------
#
# Settled John doctrine in queryable form. Charles asks "what does John
# think about comms?" → gets the rules without reading scattered memory files.


def john_pref_add(
    category: str,
    rule: str,
    why: str = "",
    how_to_apply: str = "",
    source: str = "",
) -> int:
    """Add a John preference rule. Returns id."""
    category = category.strip().lower()
    rule = rule.strip()
    if not category or not rule:
        raise ValueError("category and rule required")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO john_prefs (category, rule, why, how_to_apply, source) VALUES (?, ?, ?, ?, ?)",
            (category, rule, why, how_to_apply, source),
        )
        return cur.lastrowid or 0


def john_prefs_by_category(category: str | None = None) -> list[dict]:
    with _conn() as c:
        if category:
            rows = c.execute(
                "SELECT * FROM john_prefs WHERE category=? ORDER BY id DESC",
                (category.strip().lower(),),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM john_prefs ORDER BY category, id DESC").fetchall()
    return [dict(r) for r in rows]


def john_pref_categories() -> list[tuple[str, int]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT category, COUNT(*) AS n FROM john_prefs GROUP BY category ORDER BY n DESC"
        ).fetchall()
    return [(r["category"], r["n"]) for r in rows]


# ---------------- Reflection (learning-tree Phase 6) ----------------
#
# Daily self-review. Aggregates the last 24h of fact ingestion by topic,
# rebuilds summaries for topics with new content, writes a digestible
# "today's reflection" fact tagged `reflection,daily`, and identifies
# topics/skills that have gone cold.


def reflect_daily() -> dict:
    """Run a daily reflection pass. Returns a structured digest:
    {
        date: ISO date,
        new_facts_total: N,
        new_facts_by_topic: {topic_name: count, ...},
        topics_with_new_summaries: [name, ...],
        thin_topics: [name, ...],         # topics with <3 facts
        cold_topics: [name, ...],         # topics not touched in 14+ days
        cold_facts_marked: N,             # facts auto-superseded for staleness
    }
    Also: persists the digest as a fact tagged `reflection,daily,system`.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    two_weeks_ago = (now - timedelta(days=14)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    thirty_days_ago = (now - timedelta(days=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    with _conn() as c:
        # New facts in last 24h, grouped by topic
        rows = c.execute(
            "SELECT topic, COUNT(*) AS n FROM long_term_facts "
            "WHERE created_at >= ? AND topic IS NOT NULL AND topic != '' "
            "AND tags NOT LIKE '%superseded%' AND tags NOT LIKE '%intervention,auto%' "
            "GROUP BY topic ORDER BY n DESC",
            (day_ago,),
        ).fetchall()
        new_by_topic = {r["topic"]: r["n"] for r in rows}
        new_total = sum(new_by_topic.values())

        # Cold topics — last_fact_at older than 14 days
        cold_rows = c.execute(
            "SELECT name FROM topics WHERE last_fact_at IS NOT NULL AND last_fact_at < ? "
            "ORDER BY last_fact_at ASC LIMIT 10",
            (two_weeks_ago,),
        ).fetchall()
        cold_topics = [r["name"] for r in cold_rows]

        # Thin topics (less than 3 facts) — gaps to potentially fill
        thin_rows = c.execute(
            "SELECT name FROM topics WHERE fact_count BETWEEN 1 AND 2 ORDER BY fact_count ASC LIMIT 15"
        ).fetchall()
        thin_topics = [r["name"] for r in thin_rows]

        # Auto-supersede facts not used in 30+ days AND created over 30 days ago
        cold_facts_marked = c.execute(
            "UPDATE long_term_facts SET tags = tags || ',superseded,superseded_by:age_30d' "
            "WHERE (last_used_at IS NULL OR last_used_at < ?) "
            "AND created_at < ? "
            "AND tags NOT LIKE '%superseded%' "
            "AND topic NOT IN (SELECT name FROM topics WHERE parent_topic_id IS NOT NULL "
            "                  OR fact_count > 50)",  # don't mark facts under big or hierarchical topics
            (thirty_days_ago, thirty_days_ago),
        ).rowcount

    # Recompute summaries for topics with > 3 new facts
    refreshed = []
    for topic, n in new_by_topic.items():
        if n >= 3:
            # Just refresh the cached summary from current facts
            facts = topic_facts(topic, limit=10)
            if not facts:
                continue
            bullets = []
            for f in facts[:8]:
                first = f["fact"].split(". ", 1)[0]
                if len(first) > 200:
                    first = first[:197] + "..."
                bullets.append(f"- {first}.")
            summary = (
                f"Top facts under '{topic}' (refreshed {now.strftime('%Y-%m-%d')}, "
                f"{len(facts)} sampled):\n" + "\n".join(bullets)
            )
            topic_set_summary(topic, summary)
            refreshed.append(topic)

    digest = {
        "date": now.strftime("%Y-%m-%d"),
        "new_facts_total": new_total,
        "new_facts_by_topic": new_by_topic,
        "topics_with_new_summaries": refreshed,
        "thin_topics": thin_topics,
        "cold_topics": cold_topics,
        "cold_facts_marked": cold_facts_marked,
    }

    # Persist as a fact
    summary_text = (
        f"Daily reflection {digest['date']}: ingested {new_total} new facts across "
        f"{len(new_by_topic)} topics. "
        f"Refreshed summaries: {', '.join(refreshed) if refreshed else 'none'}. "
        f"Cold (14d) topics: {', '.join(cold_topics[:5]) if cold_topics else 'none'}. "
        f"Thin topics needing more content: {', '.join(thin_topics[:5]) if thin_topics else 'none'}. "
        f"Auto-superseded {cold_facts_marked} stale facts."
    )
    add_fact(summary_text, tags="reflection,daily,system", topic="reflection")

    return digest


# ---------------- Lifecycle ----------------

init_db()
