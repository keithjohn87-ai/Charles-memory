"""Memory tools: remember(fact, tags) and recall(query)."""
from __future__ import annotations

import re

from core import memory
from core.tools import tool

# Patterns that indicate a credential is being saved. remember() refuses these
# outright — secrets belong in .env or workspace/*_secret.txt files, NOT in
# long_term_facts (which gets dumped into prompts on recall, can leak into
# logs, gets backed up to git, etc.). Added 2026-05-09 after Charles
# auto-saved a Stripe sk_live key John pasted in chat.
_CREDENTIAL_PATTERNS = [
    (r"\bsk_(live|test)_[A-Za-z0-9]{16,}", "Stripe secret key"),
    (r"\bpk_(live|test)_[A-Za-z0-9]{16,}", "Stripe publishable key"),
    (r"\brk_(live|test)_[A-Za-z0-9]{16,}", "Stripe restricted key"),
    (r"\bAKIA[0-9A-Z]{16}", "AWS access key"),
    (r"\bAIza[0-9A-Za-z_-]{35}", "Google API key"),
    (r"\bghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"\bgho_[A-Za-z0-9]{36}", "GitHub OAuth token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}", "JWT-shaped token"),
    (r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----", "private key"),
    (r"\b[A-Z0-9]{32,}\b", "long opaque token (could be API key)"),
]


def _detect_credential(text: str) -> str | None:
    for pattern, label in _CREDENTIAL_PATTERNS:
        if re.search(pattern, text):
            return label
    return None


@tool(
    name="remember",
    summary="Save a fact to long-term memory. Auto-embedded for semantic recall later. Use for things you want to recall in future conversations.",
    triggers=("remember", "note that", "save this", "memorize", "don't forget", "make a note"),
    schema={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact to remember, written as a complete sentence. (Aliases also accepted: content, text, message — Qwen sometimes calls with these.)"},
            "topic": {"type": "string", "description": "Optional primary topic (single string, e.g. 'cognitive_bias'). If omitted, derived from the first tag.", "default": ""},
            "source": {"type": "string", "description": "Optional source of the fact (URL, conv_id, tool result). Helps with provenance audits later.", "default": ""},
            "tags": {"type": "string", "description": "Optional comma-separated tags. Legacy secondary index — `topic` is the primary axis now.", "default": ""},
        },
    },
)
def remember(
    fact: str = "",
    topic: str = "",
    source: str = "",
    tags: str = "",
    content: str = "",
    text: str = "",
    message: str = "",
) -> str:
    # Qwen sometimes calls remember(content=...) / remember(text=...) instead
    # of the schema-correct remember(fact=...). Alias them.
    if not fact:
        fact = content or text or message
    if not fact:
        return "[error] remember() needs a 'fact' argument (the thing to save)."
    # Credential guardrail — facts get dumped into prompts on recall, backed
    # up to git, etc. Secrets belong in .env or workspace/*_secret.txt.
    leak = _detect_credential(fact)
    if leak:
        return (
            f"[REFUSED — credential detected: {leak}] "
            f"Secrets don't belong in long_term_facts (they get loaded into "
            f"future prompts and backed up to git). Save credentials to "
            f"~/charles/.env (gitignored) or a workspace/*_secret.txt file. "
            f"If the user just pasted a credential in chat, redact it from "
            f"your reply and remind them to rotate it. The fact was NOT saved."
        )
    fact_id = memory.add_fact(
        fact,
        tags=tags,
        topic=(topic or None),
        source=(source or None),
    )
    return f"remembered (id={fact_id}, topic={topic or '(auto)'}): {fact}"


@tool(
    name="recall",
    summary=(
        "Semantic search over long-term memory. Embeds your query, returns the "
        "top-5 facts ranked by meaning-similarity (not just keyword match). Works "
        "across vocabulary mismatch — querying 'how do brains screw up reasoning' "
        "still finds facts about cognitive biases."
    ),
    triggers=("recall", "what do you know about", "what did i tell you", "what was", "do you know"),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language question or topic. Substring matching is no longer required — describe what you're looking for."},
        },
        "required": ["query"],
    },
)
def recall(query: str) -> str:
    hits = memory.semantic_search(query, limit=5)
    if not hits:
        return f"(no facts found for {query!r})"
    lines = []
    for h in hits:
        date = (h["created_at"] or "")[:10]
        score = h.get("score", 0)
        topic = f"[{h['topic']}] " if h.get("topic") else ""
        lines.append(f"- ({score:.2f}) [{date}] {topic}{h['fact']}")
    return "\n".join(lines)


@tool(
    name="recall_keyword",
    summary=(
        "LEGACY exact-string fallback for recall. Use when you need substring "
        "matching (e.g., looking for a specific URL or filename). Prefer `recall` "
        "for natural-language questions — it does semantic similarity."
    ),
    triggers=("find exact", "search for string", "substring match"),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Substring to match against fact text and tags."},
        },
        "required": ["query"],
    },
)
def recall_keyword(query: str) -> str:
    hits = memory.search_facts(query, limit=5)
    if not hits:
        return f"(no facts match {query!r})"
    lines = [f"- [{h['created_at'][:10]}] {h['fact']}" + (f"  ({h['tags']})" if h["tags"] else "") for h in hits]
    return "\n".join(lines)


_MASTERY_LEVELS = ("entry", "moderate", "expert")


@tool(
    name="set_mastery",
    summary=(
        "Tag a topic with your current mastery level (entry / moderate / expert) per "
        "MOM §11. ENTRY = first exposure, surface knowledge. MODERATE = practical working "
        "knowledge. EXPERT = deep mastery, can teach or apply broadly. Promoting to expert "
        "auto-prunes earlier entry-level facts on the same topic — that's the MOM's "
        "'pruning rule: when Charles reaches expert tier on a domain, entry-level info "
        "gets pruned'. Topic should match the slug used by `triangulate` (e.g. 'tennessee_river_property')."
    ),
    triggers=("set mastery", "promote mastery", "mastery level", "i mastered", "i learned enough about"),
    schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Topic slug — lowercase, underscored. Should match the topic: tag used when storing facts.",
            },
            "level": {
                "type": "string",
                "description": "entry | moderate | expert",
                "enum": list(_MASTERY_LEVELS),
            },
            "evidence": {
                "type": "string",
                "description": "One-line note on why you're claiming this mastery level (what you can do now / what you tested).",
                "default": "",
            },
        },
        "required": ["topic", "level"],
    },
)
def set_mastery(topic: str, level: str, evidence: str = "") -> str:
    topic = re.sub(r"[^a-z0-9_]+", "_", topic.lower()).strip("_")
    if not topic:
        return "[error] topic slug is empty after normalization"
    if level not in _MASTERY_LEVELS:
        return f"[error] level must be one of {_MASTERY_LEVELS}, got {level!r}"

    summary = (
        f"MASTERY {level.upper()}: topic={topic}. "
        f"Evidence: {evidence or '(none provided)'}."
    )
    fact_id = memory.add_fact(summary, tags=f"mastery,mastery:{level},topic:{topic}")

    pruned = 0
    if level == "expert":
        # Per MOM: "When Charles reaches expert tier on a domain, entry-level info gets pruned"
        # We don't actually delete — we tag superseded so audit/rollback survives.
        pruned = _supersede_lower_mastery(topic, current_level="expert")
    elif level == "moderate":
        pruned = _supersede_lower_mastery(topic, current_level="moderate")

    suffix = f" Pruned {pruned} stale lower-mastery facts." if pruned else ""
    return f"mastery set: topic={topic} level={level} (fact #{fact_id}).{suffix}"


def _supersede_lower_mastery(topic: str, current_level: str) -> int:
    """Tag earlier facts on this topic with mastery:<lower> as 'superseded'.

    Returns count of facts marked. Does NOT delete — preservation matters for
    rollback per MOM §11 ('Old versions archived to separate file in case
    rollback needed').
    """
    levels_to_supersede = {"moderate": ["entry"], "expert": ["entry", "moderate"]}.get(current_level, [])
    if not levels_to_supersede:
        return 0
    import sqlite3
    from pathlib import Path
    db = Path("/Users/home/charles/workspace/memory.db")
    if not db.exists():
        return 0
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    pruned = 0
    for old_level in levels_to_supersede:
        rows = cur.execute(
            "SELECT id, tags FROM long_term_facts "
            "WHERE tags LIKE ? AND tags LIKE ? AND tags NOT LIKE '%superseded%'",
            (f"%topic:{topic}%", f"%mastery:{old_level}%"),
        ).fetchall()
        for r in rows:
            new_tags = f"{r['tags']},superseded,superseded_by:mastery_{current_level}"
            cur.execute("UPDATE long_term_facts SET tags=? WHERE id=?", (new_tags, r["id"]))
            pruned += 1
    con.commit()
    con.close()
    return pruned


@tool(
    name="reset_my_conversation",
    summary=(
        "Nuclear option: wipe the recent rolling history of the current conversation, "
        "keeping only the most recent user turn. Use when you notice you're stuck in a "
        "response loop or when the user explicitly says 'reset' / 'start fresh' / "
        "'forget the last thing'. Does NOT delete long-term facts or goals — just the "
        "conversation tail. Always followed by a fresh response on the next turn."
    ),
    triggers=("reset conversation", "start fresh", "forget the last", "wipe the last", "clean slate"),
    schema={
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "The conversation to reset. Use the same ID you're currently in.",
            },
        },
        "required": ["conversation_id"],
    },
)
def reset_my_conversation(conversation_id: str) -> str:
    deleted = memory.reset_conversation(conversation_id, keep_last_user_turn=True)
    return f"reset conv={conversation_id}: deleted {deleted} turns, kept last user message"
