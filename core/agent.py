"""Single-conversation reasoning with multi-round tool calls and persistent memory.

M2: every turn loads the last few user/assistant exchanges for the same
conversation_id from SQLite and prepends them to the prompt. Each user
message and final assistant reply is also persisted, so Charles is
continuous across Telegram messages — not a goldfish.
"""
from __future__ import annotations

import json
import logging
import re
import threading

import tools  # noqa: F401  — import side-effect: registers all tools

from core import memory
from core.inference import complete
from core.prompts import build_system_prompt
from core.tools import REGISTRY, dispatch  # select_tools still in core.tools, kept for future

log = logging.getLogger("charles.agent")

# In-flight cancellation registry — keyed by conversation_id. When a user
# clicks "Stop" in the WarRoom UI, the server calls request_stop(conv_id),
# which sets the Event. The respond() loop checks between tool rounds and
# exits cleanly with a "stopped by user" marker.
#
# Note: this can't kill an in-progress MLX generation mid-token — the current
# round's complete() call has to finish. But it WILL prevent the next round
# from running, so a runaway tool chain stops within one round (~5-30 sec).
_in_flight_stops: dict[str, threading.Event] = {}
_in_flight_lock = threading.Lock()


def request_stop(conversation_id: str) -> bool:
    """Signal the in-flight respond() for this conv to exit at the next checkpoint.
    Returns True if a respond() was registered to receive it."""
    with _in_flight_lock:
        ev = _in_flight_stops.get(conversation_id)
        if ev:
            ev.set()
            return True
    return False


def is_stop_pending(conversation_id: str | None) -> bool:
    if not conversation_id:
        return False
    with _in_flight_lock:
        ev = _in_flight_stops.get(conversation_id)
    return bool(ev and ev.is_set())

MAX_TOOL_ROUNDS = 25
HISTORY_CHAR_BUDGET = 4000

# Intra-call repetition guard: if the assistant emits substantially identical
# content (>= this ratio) in 2 of the last 3 rounds within a SINGLE respond()
# call, exit the loop early and surface a clear breakage marker. Catches the
# 2026-05-09 "**Test** (after ~5 min):" 108x intra-call loops where the
# between-call trim never fires because the model never returns to the user.
_INTRA_CALL_REPETITION_THRESHOLD = 0.85
_INTRA_CALL_REPETITION_WINDOW = 3


def _intra_call_loop_detected(recent_assistant_texts: list[str]) -> str | None:
    """Return a description of the loop if 2+ of last N assistant texts are near-identical."""
    if len(recent_assistant_texts) < _INTRA_CALL_REPETITION_WINDOW:
        return None
    window = recent_assistant_texts[-_INTRA_CALL_REPETITION_WINDOW:]
    pairs_above = 0
    for i in range(len(window)):
        for j in range(i + 1, len(window)):
            a, b = window[i].strip(), window[j].strip()
            if not a or not b:
                continue
            if a == b:
                pairs_above += 1
                continue
            # Cheap similarity: shared first-50-char prefix is the strong signal
            if a[:50] == b[:50]:
                pairs_above += 1
                continue
            # Fall back to set-of-words Jaccard on first 200 chars
            sa, sb = set(a[:200].lower().split()), set(b[:200].lower().split())
            if sa and sb and len(sa & sb) / max(len(sa | sb), 1) >= _INTRA_CALL_REPETITION_THRESHOLD:
                pairs_above += 1
    return f"intra-call repetition: {pairs_above}/{len(window)} pairs near-identical" if pairs_above >= 1 else None


def respond(message: str, conversation_id: str | None = None) -> str:
    # Register a stop event for this conv so request_stop() can cancel us
    stop_event: threading.Event | None = None
    if conversation_id:
        with _in_flight_lock:
            stop_event = threading.Event()
            _in_flight_stops[conversation_id] = stop_event
    # Tell the tool-call guards a fresh respond chain is starting — clears
    # the in-flight dedup set + recent-reads cache for this call. The
    # per-conv URL block-list persists across calls (a goal that retries
    # ResearchGate across 5 ticks should still hit the block-list).
    from core import tool_guards
    tool_guards.respond_started(conversation_id)
    try:
        return _respond_impl(message, conversation_id, stop_event)
    finally:
        tool_guards.respond_finished()
        # Clean up the stop registration so it doesn't leak across calls
        if conversation_id:
            with _in_flight_lock:
                if _in_flight_stops.get(conversation_id) is stop_event:
                    del _in_flight_stops[conversation_id]


def _respond_impl(message: str, conversation_id: str | None, stop_event: threading.Event | None) -> str:
    system = build_system_prompt()
    history: list[dict] = [{"role": "system", "content": system}]

    if conversation_id:
        # Behavioral pre-flight: check the tail of the conversation for a
        # response loop (last 3 assistant turns near-identical). If found,
        # nuke the poisoned tail BEFORE loading history. Prevents the
        # 2026-05-09 "**Test**" loop from re-occurring in any conv.
        try:
            trimmed = memory.trim_repeating_replies(conversation_id)
            if trimmed:
                log.warning("loop-recovery: trimmed %d turns from conv=%s before this run", trimmed, conversation_id)
        except Exception as e:  # noqa: BLE001
            log.exception("loop-recovery check failed (continuing): %s", e)

        prior = memory.recent_history(conversation_id, max_chars=HISTORY_CHAR_BUDGET)
        history.extend(prior)
        log.info("loaded %d prior turns for conv=%s", len(prior), conversation_id)

    # Auto-recall: before letting the model see the user message, run a
    # cheap keyword search over long_term_facts. If past sessions saved
    # relevant findings, MERGE them into the leading system prompt so
    # Charles doesn't have to be reminded by John (the "I already told
    # you where the file is" frustration). Cannot insert a separate
    # system message later in history — MLX-LM rejects "system message
    # must be at the beginning" if any non-system message comes first.
    if conversation_id and not conversation_id.startswith(("goal:", "heartbeat:")):
        try:
            auto_recall_note = _build_auto_recall_note(message)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-recall failed (non-fatal): %s", e)
            auto_recall_note = ""
        if auto_recall_note:
            # Merge into the leading system prompt (history[0])
            history[0]["content"] = history[0]["content"] + "\n\n" + auto_recall_note

    history.append({"role": "user", "content": message})

    progress_id: int = 0  # row id of the single ticker line — updated as work advances
    if conversation_id:
        memory.log_turn(conversation_id, "user", message)
        # Single ticker row that gets UPDATED in place each tool round, so the
        # UI shows one mutating line ("Browsing wikipedia.org…" → "Reading
        # foo.txt…") instead of a stack. Inserted now with a generic
        # "thinking…" placeholder that the first tool round will overwrite.
        try:
            progress_id = memory.insert_progress(conversation_id, "*thinking…*")
        except Exception:  # noqa: BLE001
            pass

    # Send all registered tool schemas every turn. Total schema cost at M2 is
    # ~200 tokens — worth it to eliminate the "tool present but not loaded"
    # failure mode where the model narrates a call as text instead of emitting
    # a real tool_call. When the toolset grows past ~10, reintroduce
    # select_tools gating.
    api_tools = [t.openai_schema() for t in REGISTRY.values()] or None

    total_chars = sum(len(m.get("content") or "") for m in history)
    log.info(
        "respond start: prompt_chars=%d turns_in_prompt=%d tools=%s",
        total_chars,
        len(history) - 1,
        [t.name for t in REGISTRY.values()],
    )

    final_text = ""
    recent_assistant_texts: list[str] = []  # for intra-call loop detection
    for round_n in range(MAX_TOOL_ROUNDS):
        # User-initiated stop check — fires between rounds (covers multi-tool chains).
        if stop_event and stop_event.is_set():
            log.warning("respond() stopped by user request at round %d (conv=%s)", round_n, conversation_id)
            final_text = "(stopped by you)"
            if conversation_id:
                memory.log_turn(conversation_id, "assistant", final_text)
            return final_text
        # max_tokens budget: keep the FIRST round modest so a glitchy
        # whitespace runaway can't burn ~100 seconds of MLX time and trip
        # the UI's HTTP timeout. Forensic 2026-05-09 evening: 6/105 calls
        # hit 4000-token cap with zero tool_calls and empty stripped text
        # — that's the timeout pattern John was hitting.
        # Subsequent rounds get more headroom because tool_call arguments
        # (e.g., a long write_file content) legitimately need it.
        round_max_tokens = 1500 if round_n == 0 else 4000
        text, msg, usage = complete(history, tools=api_tools, max_tokens=round_max_tokens)
        log.info(
            "round=%d usage=%s tool_calls=%d",
            round_n,
            usage,
            len(msg.tool_calls or []),
        )

        # Post-complete stop check — catches Stop button clicked DURING the
        # LLM generation. MLX call already finished (can't kill mid-token),
        # but we discard the result so the user gets a clean stop marker
        # rather than the unwanted reply.
        if stop_event and stop_event.is_set():
            log.warning("stop fired during round %d's complete() — discarding generated text", round_n)
            partial = (text or "").strip()[:120]
            final_text = (
                f"(stopped by you — partial reply discarded: \"{partial}…\")"
                if partial else "(stopped by you)"
            )
            if conversation_id:
                memory.log_turn(conversation_id, "assistant", final_text)
            return final_text

        # Track each round's assistant text for intra-call repetition guard
        round_text = (msg.content or "").strip()
        if round_text:
            recent_assistant_texts.append(round_text)

        if not msg.tool_calls:
            final_text = text
            break

        # Intra-call loop guard — abort early before logging dozens of
        # identical turns. The forensic showed Charles emitting "**Test**
        # (after ~5 min):" 108 times in one tool chain on 2026-05-09.
        loop_reason = _intra_call_loop_detected(recent_assistant_texts)
        if loop_reason and round_n >= _INTRA_CALL_REPETITION_WINDOW - 1:
            log.warning("intra-call loop ABORTED at round %d (%s)", round_n, loop_reason)
            final_text = (
                f"(loop-detected at round {round_n}: {loop_reason} — "
                f"breaking out so the next tick starts fresh)"
            )
            if conversation_id:
                memory.log_turn(conversation_id, "assistant", final_text)
                # Also save an audit fact so we can see how often this fires
                try:
                    memory.add_fact(
                        f"Intra-call loop guard fired in conv {conversation_id} at round {round_n}: "
                        f"{loop_reason}. Last text: {round_text[:200]}",
                        tags="incident,intra_call_loop,auto",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return final_text

        tool_calls_payload = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
        history.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": tool_calls_payload,
        })
        if conversation_id:
            memory.log_assistant_tool_calls(conversation_id, msg.content or "", tool_calls_payload)

        for tc in msg.tool_calls:
            # Update the ticker BEFORE dispatch fires so John sees what
            # Charles is about to do (in present tense) rather than only
            # learning after each finishes.
            if progress_id:
                try:
                    memory.update_progress(
                        progress_id,
                        _format_progress(tc.function.name, tc.function.arguments, None),
                    )
                except Exception:  # noqa: BLE001
                    pass

            result = dispatch(tc.function.name, tc.function.arguments)
            log.info(
                "tool=%s args=%r result_chars=%d",
                tc.function.name,
                tc.function.arguments[:200],
                len(result),
            )
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
            if conversation_id:
                memory.log_tool_result(conversation_id, tc.id, result)
                # Update the ticker with the OUTCOME so the next round can
                # overwrite with its own present-tense action.
                if progress_id:
                    try:
                        memory.update_progress(
                            progress_id,
                            _format_progress(tc.function.name, tc.function.arguments, result),
                        )
                    except Exception:  # noqa: BLE001
                        pass
    else:
        # Hit MAX_TOOL_ROUNDS without breaking — model never settled into a
        # final text reply. Force one more round with tools=None so the model
        # MUST emit text. This gives John a real summary every time instead
        # of leaving the conv hanging mid-tool-chain. Also caps max_tokens
        # tightly since we just want a synthesis, not more action.
        log.warning("hit MAX_TOOL_ROUNDS=%d, forcing final summary", MAX_TOOL_ROUNDS)
        history.append({
            "role": "user",
            "content": (
                "You've used your tool budget for this tick. Stop calling tools "
                "and write a SHORT summary (under 200 words) of what you found "
                "and what you'd recommend doing next. Past tense. No more tool "
                "calls — just plain text."
            ),
        })
        try:
            forced_text, _msg, _usage = complete(history, tools=None, max_tokens=600)
            final_text = (forced_text or "").strip() or (
                text or "(tool budget used; couldn't synthesize a summary)"
            )
        except Exception as e:  # noqa: BLE001
            log.exception("forced summary failed: %s", e)
            final_text = text or "(this tick used the full tool budget — work continues next tick)"

    # If the model emitted whitespace-only content + zero tool_calls (the
    # "4000-token runaway" Qwen failure mode), final_text ends up empty
    # after .strip(). Don't silently drop — return a marker so the user
    # sees SOMETHING in their UI and the DB has a row to anchor history
    # against. Forensic 2026-05-09 showed this pattern caused UI timeouts
    # because empty replies weren't being recorded and the HTTP call
    # dragged on through MLX's full max_tokens generation.
    if not (final_text or "").strip():
        final_text = (
            "(I drew a blank on that one — the model generated empty text. "
            "Try rephrasing or asking again.)"
        )
        log.warning("respond() emitted empty content for conv=%s — using fallback marker", conversation_id)

    # Narration-stall recovery: if the chain ended on a "let me X" / "now I'll
    # Y" / "let me save..." final reply (no tool calls + intent-only text),
    # the model bailed on actually doing the work. Force one more round with
    # tools=None to extract a real synthesis. Forensic 2026-05-09 22:45:
    # Charles emitted "Now let me save... continue to Part 2" as final after
    # 19 rounds of real scraping, leaving the work mid-thought.
    elif _is_narration_stall(final_text):
        log.warning(
            "respond() ended on narration stall for conv=%s — forcing synthesis. text=%r",
            conversation_id, final_text[:120],
        )
        history.append({"role": "user", "content": (
            "You ended your last reply with a 'let me X' / 'now I'll Y' "
            "intent statement instead of actually doing or summarizing. Stop "
            "narrating intent. Either:\n"
            "  (a) write a SHORT summary (under 200 words, past tense) of what "
            "you actually accomplished in this chain, OR\n"
            "  (b) write the file/data you said you'd save (use write_file).\n"
            "No more 'let me' phrases. No more tool-only turns — emit text or "
            "a single concrete tool call."
        )})
        try:
            recovered_text, _msg, _usage = complete(history, tools=api_tools, max_tokens=600)
            recovered = (recovered_text or "").strip()
            if recovered and not _is_narration_stall(recovered):
                final_text = recovered
        except Exception as e:  # noqa: BLE001
            log.warning("narration-stall recovery failed: %s", e)

    if conversation_id:
        memory.log_turn(conversation_id, "assistant", final_text)
        # Drop the progress ticker now that we have a real reply — keeps the
        # UI's conv view clean (one user → one assistant per exchange).
        if progress_id:
            try:
                memory.delete_progress(progress_id)
            except Exception:  # noqa: BLE001
                pass
        # Auto-extract tasks from the reply so they surface in the Tasks tab.
        # Only fires for human-conv replies (not goal: or heartbeat: ticks).
        try:
            _autoextract_tasks(final_text, conversation_id)
        except Exception as e:  # noqa: BLE001
            log.warning("task auto-extract failed (non-fatal): %s", e)
        # Auto-remember substantive findings as facts so future user-message
        # auto-recall surfaces them. Closes the loop on memory continuity.
        try:
            n = _autoremember_findings(final_text, conversation_id)
            if n:
                log.info("auto-remembered %d finding(s) from reply in conv=%s", n, conversation_id)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-remember failed (non-fatal): %s", e)

    return final_text


# ---------------------------------------------------------------------------
# Auto-recall — prepend relevant past findings as context.
#
# Built 2026-05-09 night after John's frustration: "I had to point to the
# specific file and what part of the file. That's annoying." Charles had
# found the answer 30 min earlier but didn't remember it because the
# previous reply lived in a different conv (channel fragmentation) AND
# even within one conv, his rolling history is short (4000 chars). This
# function makes memory continuity automatic: every user message
# triggers a quick keyword search over long_term_facts and any matches
# get injected as a system note before the model sees the user message.
# ---------------------------------------------------------------------------

# Stopwords removed before keyword extraction. Tuned for John's actual
# message style (terse, action-oriented) — not a general English list.
_RECALL_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "did", "do", "does", "for", "from", "get", "go", "had", "has", "have",
    "he", "her", "him", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "just", "like", "me", "my", "no", "not", "of", "on", "one", "or",
    "our", "out", "she", "so", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "to", "up", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "you", "your", "charles", "use", "run", "make", "find",
    "please", "again", "thanks", "thank", "now", "okay", "ok", "yes",
    "yeah", "no", "should", "would", "could", "want", "need", "ill",
    "let", "lets", "us", "lemme", "gonna", "going",
}


def _extract_keywords(text: str, max_kw: int = 5) -> list[str]:
    """Pull 3-5 distinctive keywords from a user message for fact lookup."""
    if not text:
        return []
    # Lowercase, split on non-word chars, drop stopwords + 1-2 char tokens
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in raw:
        if w in _RECALL_STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= max_kw:
            break
    return out


def _build_auto_recall_note(user_message: str) -> str:
    """Search long_term_facts for keywords from the user's message and return
    a system-note string with the top matches. Empty string if nothing
    relevant was found.

    Caps: 3 facts max, 200 chars per fact (was 5 facts × 300 chars). With
    a 360+ fact store, the broader cap pulled 30K+ chars into the prompt
    and bloated context — observed 39K total prompt size on simple "Good
    Morning" greetings. Tightened to keep the recall block under ~1500 chars.
    """
    keywords = _extract_keywords(user_message)
    if not keywords:
        return ""

    # Search facts for each keyword; collect top hits, dedup by id
    seen_ids: set[int] = set()
    hits: list[dict] = []
    for kw in keywords:
        try:
            results = memory.search_facts(kw, limit=3)
        except Exception:  # noqa: BLE001
            continue
        for r in results:
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            # Skip noisy auto-generated facts that wouldn't help John
            tags = (r.get("tags") or "").lower()
            if any(t in tags for t in ("superseded", "intervention,auto", "prune,auto", "credential_scrub", "blocked_url")):
                continue
            hits.append(r)
            if len(hits) >= 3:
                break
        if len(hits) >= 3:
            break

    if not hits:
        return ""

    lines = [
        "## Relevant memory from past sessions (auto-recalled):",
        "Search keywords: " + ", ".join(keywords),
        "",
    ]
    for r in hits:
        fact = (r["fact"] or "").strip()[:200]
        tags = (r.get("tags") or "").strip()[:80]
        when = (r.get("created_at") or "")[:10]
        lines.append(f"- [{when}] {fact}" + (f"  _({tags})_" if tags else ""))
    lines.append("")
    lines.append("If any of the above is relevant to your task, USE IT — don't re-do work already done.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-remember — extract substantive findings from final replies and persist
# them as facts. Closes the loop with auto-recall: every confirmed finding
# becomes searchable in future sessions.
# ---------------------------------------------------------------------------

# Patterns that signal "Charles is reporting a finding worth remembering".
# Permissive on purpose — better to over-extract findings than miss them.
# The auto-recall side filters by tag relevance + recency anyway.
_FINDING_PATTERNS = (
    # Verb-led: found / located / saved / wrote / stored
    re.compile(r"[Ff]ound\s+(?:the\s+|that\s+|it[:\s]+)?([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Ll]ocated\s+(?:at\s+|in\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Ss]aved\s+(?:to\s+|at\s+|the\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Ww]rote\s+(?:to\s+|the\s+|\d+\s+chars?\s+to\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Ss]tored\s+(?:in\s+|at\s+|to\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Cc]reated\s+(?:the\s+)?(?:file\s+|directory\s+)?([^\.!\?\n]{8,250})", re.MULTILINE),
    # Existence: "the X is at / are in / lives in"
    re.compile(r"[Tt]he\s+\w[\w\s]{0,30}\s+(?:is|are|lives|sits)\s+(?:at\s+|in\s+|on\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    re.compile(r"[Ii]t.s\s+(?:at\s+|in\s+|on\s+|located\s+at\s+)([^\.!\?\n]{8,250})", re.MULTILINE),
    # Any sentence containing a substantial absolute path is worth remembering
    re.compile(r"([^\.!\?\n]*?(?:/Users/|~/|/tmp/|/var/|/etc/|/opt/)[^\s\)`'\"]{4,}[^\.!\?\n]{0,150})", re.MULTILINE),
)


def _autoremember_findings(reply_text: str, conversation_id: str) -> int:
    """Scan a final assistant reply for finding-shaped statements; persist
    each as a fact tagged 'auto_finding' so future auto-recall surfaces
    them. Returns count of facts saved.

    Fires on user-channel convs AND goal-tick convs (goal: prefix) since
    autonomous goal work also produces findings worth remembering. Skips
    only heartbeat/sunday_test which are scheduled-task / harness convs.
    """
    if any(conversation_id.startswith(p) for p in ("heartbeat:", "sunday_test_")):
        return 0
    if not reply_text or len(reply_text) < 30:
        return 0

    saved = 0
    seen: set[str] = set()
    for pattern in _FINDING_PATTERNS:
        for match in pattern.finditer(reply_text):
            phrase = match.group(0).strip().rstrip(",;:.")
            # Dedup by first 50 chars (catches near-identical findings from
            # multiple patterns matching the same sentence)
            key = phrase.lower()[:50]
            if len(phrase) < 12 or key in seen:
                continue
            seen.add(key)
            try:
                memory.add_fact(
                    f"FINDING (auto-extracted from conv {conversation_id}): {phrase[:280]}",
                    tags="auto_finding,extracted_from_reply",
                )
                saved += 1
            except Exception:  # noqa: BLE001
                pass
            if saved >= 4:
                return saved
    return saved


# ---------------------------------------------------------------------------
# Progress notes — the "Charles is still working" liveness indicator.
#
# At respond start, agent.respond inserts a SINGLE role='progress' row in the
# conversation. As Charles works through tool rounds, that row's content gets
# UPDATED in place ("*Browsing wikipedia.org…*" → "*Reading file foo.txt…*").
# The UI sees one ticker line that mutates rather than a stack of new rows —
# matching the "Editing tool_guards.py" style John has in his Claude Code
# session. Filtered out of memory.recent_history so it never enters the prompt.
# ---------------------------------------------------------------------------

# Map each tool to a present-tense verb phrase. The action shows what Charles
# is *currently* doing — UI-renderable in a single italic line.
_TOOL_VERBS = {
    "browse_url": "Browsing",
    "browser_screenshot": "Screenshotting",
    "read_file": "Reading",
    "write_file": "Writing",
    "exec_shell": "Running shell:",
    "search_web": "Searching web:",
    "search_facts": "Searching memory:",
    "recall": "Recalling:",
    "remember": "Remembering",
    "send_imessage": "Texting John:",
    "send_email": "Sending email:",
    "list_emails": "Checking email",
    "read_email": "Reading email",
    "archive_email": "Archiving email",
    "set_goal": "Setting goal:",
    "list_goals": "Reviewing goals",
    "append_goal_note": "Logging goal note",
    "complete_goal": "Completing goal:",
    "cancel_goal": "Cancelling goal:",
    "schedule_task": "Scheduling task:",
    "list_scheduled_tasks": "Reviewing schedule",
    "current_time": "Checking the clock",
    "get_weather": "Checking weather:",
    "system_status": "Checking system status",
    "self_modify": "Modifying my own code:",
    "self_patch": "Patching my own code:",
    "analyze_sentiment": "Reading the room:",
    "request_approval": "Asking John:",
    "resolve_approval": "Resolving approval",
    "notify_john": "Pinging John:",
    "add_task": "Adding a task:",
    "list_open_tasks": "Reviewing open tasks",
    "reset_my_conversation": "Resetting context",
    "solve_recaptcha": "Solving captcha",
}


def _short_target(name: str, args_json: str) -> str:
    """Extract the most user-meaningful arg as a single short string."""
    try:
        args = json.loads(args_json)
    except Exception:  # noqa: BLE001
        return ""
    if name in ("browse_url", "browser_screenshot"):
        url = args.get("url", "")
        m = re.match(r"https?://([^/]+)(/[^?]*)?", url)
        if m:
            host = m.group(1).replace("www.", "")
            path = (m.group(2) or "").rstrip("/")
            tail = path.rsplit("/", 1)[-1][:36] if path else ""
            return f"{host}{('/' + tail) if tail else ''}"
        return url[:50]
    if name == "exec_shell":
        cmd = (args.get("command") or "").strip().replace("\n", " ")
        return cmd[:60] + ("…" if len(cmd) > 60 else "")
    if name in ("read_file", "write_file"):
        path = args.get("path", "")
        return path.rsplit("/", 1)[-1][:48] or path[:48]
    if name in ("recall", "search_facts", "search_web"):
        return (args.get("query") or "")[:48]
    if name in ("remember", "append_goal_note"):
        text = args.get("fact") or args.get("note") or args.get("content", "")
        return (str(text)[:48] + "…") if len(str(text)) > 48 else str(text)
    if name in ("send_imessage", "send_email", "notify_john"):
        msg = args.get("message") or args.get("body") or args.get("text", "")
        return (str(msg)[:40] + "…") if len(str(msg)) > 40 else str(msg)
    if name in ("complete_goal", "cancel_goal", "set_goal"):
        return str(args.get("description") or args.get("summary") or args.get("goal_id", ""))[:48]
    if args:
        first_key, first_val = next(iter(args.items()))
        return f"{first_key}={str(first_val)[:40]}"
    return ""


def _format_progress(tool_name: str, args_json: str, result: str | None = None) -> str:
    """Build a single-line italic action note. If `result` is None, this is
    the IN-PROGRESS form ('Browsing wikipedia.org…'). If `result` is given,
    this is the JUST-FINISHED form (briefly shows outcome before next round
    overwrites it)."""
    verb = _TOOL_VERBS.get(tool_name, tool_name)
    target = _short_target(tool_name, args_json)
    base = f"{verb} {target}".strip().rstrip(":")

    # In-progress (no result yet)
    if result is None:
        return f"*{base}…*"

    # Finished — append a short outcome
    head = result[:140] if result else ""
    if head.startswith("[BLOCKED"):
        m = re.search(r"reason=(\S+)", head)
        reason = (m.group(1) if m else "blocked").replace("_", " ")
        return f"*{base} → {reason}, moving on*"
    if head.startswith("[error]"):
        if "STOP. You have now called" in head:
            label = "looping — stop signal sent"
        elif "you already called" in head:
            label = "already tried — moving on"
        elif "you already tried this URL" in head:
            label = "URL on block-list — skipping"
        elif "missing required argument" in head:
            label = "bad call shape — retrying"
        elif "your own memory database" in head:
            label = "redirected to recall()"
        else:
            label = "error"
        return f"*{base} → {label}*"
    if head.startswith("[cached"):
        return f"*{base} → cached, skipping re-read*"
    return f"*{base} → {len(result):,} chars*"


# Phrases that mean "I'm declaring intent, not actually doing it." When a
# final reply (chain end, no tool calls) consists primarily of these, the
# narration-stall recovery fires. Tuned to be specific enough not to
# false-positive on legitimate replies that contain incidental "let me"
# or "I'll" mentions.
_NARRATION_STALL_LEADS = (
    "now let me",
    "let me save",
    "let me continue",
    "let me start",
    "let me get",
    "let me check",
    "now i'll",
    "now i will",
    "i'll save",
    "i'll continue",
    "i'll start",
)


def _is_narration_stall(text: str) -> bool:
    """True if a final reply is just intent narration without action.
    Catches the post-reasoning-leak pattern where Charles announces what
    he'll do next instead of summarizing what he did."""
    if not text:
        return False
    s = text.strip().lower()
    if len(s) > 400:
        # Long replies usually contain real content; only catch short narration
        return False
    starts = any(s.startswith(p) for p in _NARRATION_STALL_LEADS)
    if not starts:
        return False
    # Must NOT contain a real path, code block, or list — those are content
    if any(marker in s for marker in ("```", "/users/", "1.", "**1**", "##")):
        return False
    return True


# Patterns that indicate Charles is asking John to do something concrete.
# These run on his FINAL reply only (not tool-chain rounds), so we don't
# spam tasks for every "let me think about that" intermediate turn.
_TASK_PATTERNS = [
    # "I need you to X" — strong direct ask
    re.compile(r"(?:^|[\.!\?\n])\s*[Ii]\s+need\s+you\s+to\s+([^\.!\?\n]{6,140})", re.MULTILINE),
    # "You'll need to X" / "You need to X"
    re.compile(r"(?:^|[\.!\?\n])\s*[Yy]ou(?:'ll)?\s+need\s+to\s+([^\.!\?\n]{6,140})", re.MULTILINE),
    # "Please X" — start-of-sentence verb
    re.compile(r"(?:^|[\.!\?\n])\s*[Pp]lease\s+([a-z][^\.!\?\n]{6,140})", re.MULTILINE),
    # "Can you X?" — request form
    re.compile(r"(?:^|[\.!\?\n])\s*[Cc]an\s+you\s+([^\?\.\n]{6,140})\?", re.MULTILINE),
    # "Could you X?" — softer request
    re.compile(r"(?:^|[\.!\?\n])\s*[Cc]ould\s+you\s+([^\?\.\n]{6,140})\?", re.MULTILINE),
    # "Waiting on you to X" / "Waiting for you to X"
    re.compile(r"[Ww]aiting\s+(?:on|for)\s+you\s+(?:to\s+)?([^\.!\?\n]{6,140})", re.MULTILINE),
]
# Convs where auto-extract is allowed. Heuristic: human-named conv ids (numeric
# Telegram IDs, or anything not starting with goal:/heartbeat:/sunday_test_/
# warroom-).
_AUTOEXTRACT_SKIP_PREFIXES = ("goal:", "heartbeat:", "sunday_test_", "warroom-", "stress_", "smoketest", "post_patch")


def _autoextract_tasks(reply_text: str, conversation_id: str) -> int:
    """Scan a final assistant reply for task-language; create tasks for matches.
    Returns the count of tasks created. No-ops on goal/heartbeat conv_ids."""
    if any(conversation_id.startswith(p) for p in _AUTOEXTRACT_SKIP_PREFIXES):
        return 0
    if not reply_text or len(reply_text) < 6:
        return 0
    seen: set[str] = set()
    created = 0
    for pattern in _TASK_PATTERNS:
        for match in pattern.finditer(reply_text):
            phrase = match.group(1).strip().rstrip(",;:")
            phrase_key = phrase.lower()[:80]
            if phrase_key in seen:
                continue
            seen.add(phrase_key)
            # Drop trivially short or punctuation-only matches
            if len(phrase) < 6 or not any(c.isalpha() for c in phrase):
                continue
            title = phrase[:120].rstrip()
            try:
                memory.add_task(
                    title=title,
                    description=f"Auto-extracted from Charles's reply in conv {conversation_id}.",
                    urgency="normal",
                    source="auto_extracted",
                    source_conv=conversation_id,
                )
                created += 1
            except Exception as e:  # noqa: BLE001
                log.warning("add_task failed for phrase %r: %s", title, e)
    if created:
        log.info("auto-extracted %d task(s) from reply in conv=%s", created, conversation_id)
    return created
