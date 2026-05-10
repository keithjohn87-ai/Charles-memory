"""Deterministic guards that prevent Charles from re-trying things he's
already tried, re-reading files he just read, or using exec_shell as a
memory-query tool.

Built 2026-05-09 evening after a 500-turn forensic showed Charles:
  - re-reading the same source file 55 times in one goal,
  - retrying ResearchGate "Access denied" pages 48 times,
  - running 167 sqlite3 queries against his own memory.db via exec_shell,
  - emitting the same `[BLOCKED]` browse_url 13× across ticks.

Qwen's tool-call format is fine. The model is fine. The dispatcher just
had no enforcement of "don't repeat what already failed." This module
adds that enforcement, deterministically, without re-training anything.

State model:
  • _BLOCKED_URLS — per conv_id, set of URLs that returned a blocked-page
    signal. Persists across `respond()` calls (ticks) so a goal that
    spans dozens of ticks doesn't re-try the same dead URLs.
  • _IN_FLIGHT — per `respond()` call, set of (tool_name, args_signature)
    tuples seen so far. Reset at the start of each respond. Catches the
    "blast 8 browse_urls in one turn, then blast same 8 again" pattern.
  • _RECENT_READS — per `respond()` call, dict of file_path → content_hash
    so re-reads of the same file in the same chain return a short signal
    instead of re-dumping the file.

The guards are applied by `core/tools.dispatch()` before invoking the
handler. They short-circuit with `[error] ...` strings the model will
read in its next round.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from collections import defaultdict
from typing import Any

log = logging.getLogger("charles.tool_guards")

# ---------------------------------------------------------------------------
# Per-conversation state (persists across respond() calls within one process)
# ---------------------------------------------------------------------------

# conv_id → set of blocked URLs. Bounded by python's natural memory; if it
# grows unbounded for a single conv, that conv has its own bigger problems
# (the watchdog will trim repeating replies / cancel the goal).
_BLOCKED_URLS: dict[str, dict[str, str]] = defaultdict(dict)  # conv → {url: reason}

# ---------------------------------------------------------------------------
# Per-respond() state (reset every call). Lives in a contextvar so concurrent
# respond() calls (different conv_ids served in parallel) don't trample each
# other's in-flight tracking.
# ---------------------------------------------------------------------------

_in_flight: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "tool_guards_in_flight", default=None,
)
_recent_reads: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "tool_guards_recent_reads", default=None,
)
_current_conv: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_guards_current_conv", default=None,
)


def respond_started(conv_id: str | None) -> None:
    """Called by agent.respond() at the start of every call.

    Also rehydrates the per-conv URL block-list from persisted facts so a
    process restart doesn't lose the "this URL is dead" knowledge.
    """
    _in_flight.set({})
    _recent_reads.set({})
    _current_conv.set(conv_id)
    _recall_history.set([])
    if conv_id and conv_id not in _BLOCKED_URLS:
        try:
            _rehydrate_block_list(conv_id)
        except Exception as e:  # noqa: BLE001
            log.warning("block-list rehydrate failed for %s: %s", conv_id, e)


def respond_finished() -> None:
    """Called by agent.respond() in its finally block."""
    _in_flight.set(None)
    _recent_reads.set(None)
    _current_conv.set(None)
    _recall_history.set(None)


def current_conv_id() -> str | None:
    return _current_conv.get()


# ---------------------------------------------------------------------------
# Tool-call signature (used for both in-flight dedup and blocked-URL lookup)
# ---------------------------------------------------------------------------

def _signature(name: str, args: dict[str, Any]) -> str:
    """Stable hashable signature of (tool_name, args). Sorted keys so arg
    re-orderings don't fool the dedup."""
    try:
        return name + "|" + json.dumps(args, sort_keys=True, default=str)[:400]
    except Exception:  # noqa: BLE001
        return name + "|" + repr(sorted(args.items()))[:400]


# ---------------------------------------------------------------------------
# Pre-call guards: return an [error] string to short-circuit, or None to proceed
# ---------------------------------------------------------------------------

# exec_shell + sqlite3 against the agent's own memory.db is never the right
# tool — Charles has `recall()` and `search_facts()` for that. Detect and
# redirect.
_OWN_DB_PATTERN = re.compile(
    r"sqlite3\b.*?\bmemory\.db\b",
    re.IGNORECASE | re.DOTALL,
)

# Detects search-shaped shell commands so the dispatcher can nudge Charles
# away from grep/find iteration loops (the "Beginner Coding URLs" pattern
# from 2026-05-09 night).
_SEARCH_CMD_PATTERN = re.compile(
    r"^\s*(grep|find|rg|ag|locate)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_search_command(cmd: str) -> bool:
    """True if the command starts with a content-search tool. False otherwise."""
    return bool(_SEARCH_CMD_PATTERN.match(cmd or ""))


def _count_search_commands(in_flight: dict[str, int]) -> int:
    """How many distinct search-shaped exec_shell calls are tracked in this
    respond chain. Used to throttle keyword-fishing loops."""
    n = 0
    for sig in in_flight:
        if not sig.startswith("exec_shell|"):
            continue
        try:
            args = json.loads(sig.split("|", 1)[1])
            if _looks_like_search_command(args.get("command", "")):
                n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def check_pre_call(name: str, args: dict[str, Any]) -> str | None:
    """Return a short-circuit error string, or None to let the call proceed."""

    # 1) Self-querying memory via shell — redirect.
    if name == "exec_shell":
        cmd = (args.get("command") or "")
        if _OWN_DB_PATTERN.search(cmd):
            return (
                "[error] this is your own memory database. NEVER query "
                "workspace/memory.db via shell — it's slow, error-prone, and "
                "the schema can change. Use the dedicated tools instead:\n"
                "  - recall(query='...') for fact lookups\n"
                "  - search_facts(query='...') for keyword search across facts\n"
                "  - list_goals() / append_goal_note() for goal state\n"
                "Re-emit your tool_call with one of those instead of sqlite3."
            )

        # 1a) Search-loop nudge: if Charles has run 4+ exec_shell with grep/find
        # in this respond chain, he's probably keyword-fishing instead of
        # reading the source. Nudge him to pivot.
        if _looks_like_search_command(cmd):
            in_flight = _in_flight.get()
            if in_flight is not None:
                search_count = _count_search_commands(in_flight)
                if search_count >= 4:
                    return (
                        "[error] you've run "
                        f"{search_count} grep/find commands in this "
                        "response chain. If they're not finding what you "
                        "want, your KEYWORDS are probably wrong, not the "
                        "PATHS. Stop iterating searches — instead:\n"
                        "  1. read_file the most likely source directly "
                        "(check long_term_facts via recall() for known paths)\n"
                        "  2. OR ask the user to clarify what they're "
                        "looking for\n"
                        "Do NOT run another grep/find. Pivot now."
                    )

    # 2) URL block-list (browse_url, browser_screenshot).
    conv_id = current_conv_id()
    if conv_id and name in ("browse_url", "browser_screenshot"):
        url = (args.get("url") or "").strip()
        if url:
            blocked = _BLOCKED_URLS.get(conv_id, {})
            reason = blocked.get(url)
            if reason:
                return (
                    f"[error] you already tried this URL earlier in this "
                    f"conversation and it failed: reason={reason}, url={url}. "
                    f"Move on — pick a different source or skip this item. "
                    f"Do NOT retry it; the result will be the same."
                )

    # 3) In-flight duplicate (same tool + same args within ONE respond chain).
    # Count how many times we've seen this exact call so we can escalate the
    # error message — Qwen sometimes ignores the first "you already called"
    # error and retries the same call. After 2 blocks we make it impossible
    # to misread.
    in_flight = _in_flight.get()
    if in_flight is not None:
        sig = _signature(name, args)
        prior_attempts = in_flight.get(sig, 0)
        if prior_attempts >= 1:
            # Track this attempt too so the escalation count keeps climbing
            in_flight[sig] = prior_attempts + 1
            attempt_n = prior_attempts + 1  # this is now the Nth attempt
            if attempt_n >= 3:
                return (
                    f"[error] STOP. You have now called {name}() with these "
                    f"exact arguments {attempt_n} TIMES in this response chain. "
                    f"The result will not change. You are looping — pick a "
                    f"DIFFERENT tool or call complete_goal/cancel_goal if you're "
                    f"done. Do NOT call {name}() with these arguments again."
                )
            return (
                f"[error] you already called {name}() with these exact "
                f"arguments earlier in this same response chain (attempt #{attempt_n}). "
                f"Calling it again won't change the result. Use the result "
                f"you already have, or call a DIFFERENT tool with DIFFERENT args. "
                f"Continuing to retry this same call will exhaust your tool budget."
            )
        # Will mark this signature as seen AFTER pre-checks pass — see mark_in_flight().

    # 3a) Fuzzy-recall nudge — if 4+ recall calls in this chain returned
    # short results (<100 chars), the model is iterating tag-pattern guesses
    # against a schema that doesn't match. Pivot to broad recall or
    # search_facts. Catches the "recall(url:1)..recall(url:22)" loop pattern.
    if name == "recall":
        history = _recall_history.get()
        if history is not None:
            short_results = sum(1 for _, l in history if l < _RECALL_SHORT_RESULT_LEN)
            if short_results >= _RECALL_NUDGE_THRESHOLD:
                last_queries = [q for q, _ in history[-_RECALL_NUDGE_THRESHOLD:]]
                return (
                    f"[error] you've made {short_results} recall() calls in "
                    f"this chain that all returned <100 chars (essentially "
                    f"empty). Your tag schema assumption is wrong. Recent "
                    f"queries: {last_queries}\n"
                    f"PIVOT NOW:\n"
                    f"  - Try recall(query='<broad keyword>') without "
                    f"sub-pattern guesses (e.g., 'url_corpus' alone, not "
                    f"'url_corpus url:1').\n"
                    f"  - OR call search_facts(query='...') for substring "
                    f"matching across fact text.\n"
                    f"  - OR call list_goals(status='all') to see your prior "
                    f"goal notes which often have what you're looking for.\n"
                    f"Do NOT make another narrow recall — your schema "
                    f"assumption is wrong."
                )

    # 4) read_file de-dup within a respond chain — return cached content
    #    fingerprint instead of the full file.
    if name == "read_file":
        path = (args.get("path") or "").strip()
        recent = _recent_reads.get()
        if path and recent is not None and path in recent:
            cached_hash = recent[path]
            return (
                f"[cached read_file] you read {path!r} earlier in this same "
                f"response chain — content hash sha256={cached_hash[:12]}. "
                f"It hasn't changed since you read it 2 seconds ago. Use the "
                f"content you already have in context. If you genuinely need "
                f"to re-read because you suspect the file changed, call "
                f"exec_shell with `stat {path!r}` first to verify the mtime."
            )

    return None


def mark_in_flight(name: str, args: dict[str, Any]) -> None:
    """Record a successful pre-check pass so the next call with same sig short-circuits."""
    in_flight = _in_flight.get()
    if in_flight is not None:
        sig = _signature(name, args)
        in_flight[sig] = in_flight.get(sig, 0) + 1


# ---------------------------------------------------------------------------
# Post-call hooks: read tool results and update the block-lists / cache
# ---------------------------------------------------------------------------

# When browse_url returns a "page is blocked / dead" result, we want to record
# the URL so future calls short-circuit. The browse_url tool itself emits a
# structured `[BLOCKED reason=... url=...]` header (see tools/browser.py) that
# we parse here. Falls back to substring matching for results from older
# code paths.
_BLOCKED_HEADER_RE = re.compile(
    r"\[BLOCKED\s+reason=([a-z_0-9]+)\s+url=(\S+?)\]",
    re.IGNORECASE,
)
_LEGACY_BLOCK_PHRASES = (
    ("Access denied", "access_denied"),
    ("Access Denied", "access_denied"),
    ("Forbidden", "forbidden"),
    ("Just a moment", "cloudflare_block"),
    ("Temporarily Unavailable", "site_unavailable"),
    ("Page not found", "404"),
    ("Page Not Found", "404"),
    ("404 Error", "404"),
    ("403 ERROR", "forbidden"),
    ("Sorry, the page you requested was not found", "404"),
    ("File Not Found", "404"),
)


def _classify_legacy_blocked(result: str) -> str | None:
    """Best-effort: scan the raw page text for known block-page phrases."""
    snippet = result[:1500]  # only the head matters
    for phrase, reason in _LEGACY_BLOCK_PHRASES:
        if phrase in snippet:
            return reason
    return None


def post_call(name: str, args: dict[str, Any], result: str) -> None:
    """Update guards' state from the tool's result. Never raises."""
    try:
        conv_id = current_conv_id()

        # Update URL block-list from browse_url / browser_screenshot results.
        if conv_id and name in ("browse_url", "browser_screenshot"):
            url = (args.get("url") or "").strip()
            if url:
                m = _BLOCKED_HEADER_RE.search(result[:300])
                reason = None
                if m:
                    reason = m.group(1).lower()
                else:
                    reason = _classify_legacy_blocked(result)
                if reason:
                    _BLOCKED_URLS[conv_id][url] = reason
                    log.info("URL blocked for conv=%s: %s (%s)", conv_id, url, reason)
                    # Persist so process restarts don't forget which URLs are
                    # dead. Auto-recall filter excludes blocked_url tag from
                    # user-message context to keep prompts clean.
                    _persist_blocked_url(conv_id, url, reason)

        # Track recall calls for fuzzy-recall nudge (see check_pre_call).
        if name == "recall":
            _track_recall_result(args.get("query") or "", result)

        # Cache successful read_file by path (only if it didn't error).
        if name == "read_file":
            path = (args.get("path") or "").strip()
            recent = _recent_reads.get()
            if path and recent is not None and not result.startswith("[error]") and not result.startswith("[cached"):
                recent[path] = hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest()
    except Exception as e:  # noqa: BLE001 — guards must never break the dispatcher
        log.warning("post_call hook failed for %s: %s", name, e)


# ---------------------------------------------------------------------------
# URL block-list persistence — survives process restarts
# ---------------------------------------------------------------------------

def _persist_blocked_url(conv_id: str, url: str, reason: str) -> None:
    """Save a blocked-URL fact tagged 'blocked_url,<reason>,<conv_short>'.
    Auto-recall filter (in agent._build_auto_recall_note) excludes this tag
    so it doesn't pollute user-message context. Loaded by _rehydrate_block_list
    on respond_started so a fresh process boots with the prior knowledge."""
    try:
        from core import memory as _mem
        # Dedup: don't write the same blocked URL more than once
        existing = _mem.search_facts(f"blocked_url {url}", limit=1)
        if existing:
            return
        _mem.add_fact(
            f"BLOCKED_URL conv={conv_id} url={url} reason={reason}",
            tags=f"blocked_url,blocked_url:{reason},conv:{conv_id[:30]}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("persist_blocked_url failed: %s", e)


def _rehydrate_block_list(conv_id: str) -> None:
    """On respond_started, load any persisted blocked URLs for this conv from
    long_term_facts back into _BLOCKED_URLS so the in-memory check_pre_call
    short-circuits work after a process restart."""
    from core import memory as _mem
    facts = _mem.search_facts(f"conv:{conv_id[:30]}", limit=100)
    n = 0
    for f in facts:
        tags = (f.get("tags") or "").lower()
        if "blocked_url" not in tags:
            continue
        # Parse "BLOCKED_URL conv=X url=Y reason=Z" from fact text
        text = f.get("fact") or ""
        m = re.match(r"BLOCKED_URL conv=(\S+) url=(\S+) reason=(\S+)", text)
        if not m:
            continue
        url = m.group(2)
        reason = m.group(3)
        _BLOCKED_URLS[conv_id][url] = reason
        n += 1
    if n:
        log.info("rehydrated %d blocked URLs for conv=%s", n, conv_id)


# ---------------------------------------------------------------------------
# Fuzzy-recall nudge — detect recall iteration with empty results
# ---------------------------------------------------------------------------

# Per-respond tracker: list of (query, result_len) for each recall call.
_recall_history: contextvars.ContextVar[list[tuple[str, int]] | None] = contextvars.ContextVar(
    "tool_guards_recall_history", default=None,
)
_RECALL_NUDGE_THRESHOLD = 4   # 4 recalls returning <100 chars = pattern misuse
_RECALL_SHORT_RESULT_LEN = 100


def _track_recall_result(query: str, result: str) -> None:
    """Record this recall call's outcome. Reset by respond_started."""
    history = _recall_history.get()
    if history is None:
        history = []
        _recall_history.set(history)
    history.append((query, len(result)))


# ---------------------------------------------------------------------------
# Inspection / debug helpers (for tests + watchdog visibility)
# ---------------------------------------------------------------------------

def blocked_urls_for(conv_id: str) -> dict[str, str]:
    """Return a copy of {url: reason} for a conv (empty dict if none)."""
    return dict(_BLOCKED_URLS.get(conv_id, {}))


def clear_blocked_urls(conv_id: str) -> int:
    """Drop the block-list for a conv (e.g. on reset_conversation)."""
    n = len(_BLOCKED_URLS.get(conv_id, {}))
    _BLOCKED_URLS.pop(conv_id, None)
    return n


def reset_all() -> None:
    """For tests."""
    _BLOCKED_URLS.clear()
    _in_flight.set(None)
    _recent_reads.set(None)
    _current_conv.set(None)
