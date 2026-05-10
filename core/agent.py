"""Single-conversation reasoning with multi-round tool calls and persistent memory.

M2: every turn loads the last few user/assistant exchanges for the same
conversation_id from SQLite and prepends them to the prompt. Each user
message and final assistant reply is also persisted, so Charles is
continuous across Telegram messages — not a goldfish.

INVARIANT (cemented 2026-05-10): JOHN_CHARLES is a clean dialog channel.
Only `role='user'` and `role='assistant'` (final replies) persist there.
Tool calls, tool results, and mid-chain "let me X" assistant turns are
NOT logged in JOHN_CHARLES — only in CHARLES_LOG. The chain's in-memory
`history` keeps full plumbing for the model. See:
  - memory: feedback_john_charles_clean_dialog.md
  - architecture: project_two_channel_architecture.md
JOHN_CHARLES tool-round budget is also tighter (5 vs 25) — relational
chat doesn't need 25 tool rounds to answer "how's it going?".
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import tools  # noqa: F401  — import side-effect: registers all tools

from core import channels, memory
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

# JOHN_CHARLES (the relational chat) caps tool rounds tighter than the
# autonomous side — answering "how's it going?" should not take 20 tool
# rounds. Charles's autonomous goal work runs in CHARLES_LOG and has the
# full 25-round budget for actual heavy work.
MAX_TOOL_ROUNDS_RELATIONAL = 5
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
    # Auto-recall fires on the relational channel (JOHN_CHARLES) only.
    # On CHARLES_LOG, the synthetic tick message already carries enough
    # context (goal_id, task_id, etc.) and the goals.notes column gives
    # per-goal continuity — running auto-recall there would just bloat the
    # prompt with off-topic facts.
    if conversation_id == channels.JOHN_CHARLES:
        try:
            auto_recall_note = _build_auto_recall_note(message)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-recall failed (non-fatal): %s", e)
            auto_recall_note = ""
        if auto_recall_note:
            # Merge into the leading system prompt (history[0])
            history[0]["content"] = history[0]["content"] + "\n\n" + auto_recall_note

        # Cross-channel context: surface what Charles has been doing in
        # CHARLES_LOG since John last spoke, so when John pings him he
        # has fresh awareness of his own autonomous activity. Capped tight.
        try:
            log_note = _build_charles_log_summary()
        except Exception as e:  # noqa: BLE001
            log.warning("charles_log summary failed (non-fatal): %s", e)
            log_note = ""
        if log_note:
            history[0]["content"] = history[0]["content"] + "\n\n" + log_note

    history.append({"role": "user", "content": message})

    progress_id: int = 0  # row id of the single ticker line — updated as work advances
    if conversation_id:
        memory.log_turn(conversation_id, "user", message)
        # Single ticker row that gets UPDATED in place each tool round, so the
        # UI shows one mutating line ("Browsing wikipedia.org…" → "Reading
        # foo.txt…") instead of a stack. Inserted now with a generic
        # "thinking…" placeholder that the first tool round will overwrite.
        try:
            progress_id = memory.insert_progress(conversation_id, "*workin' on it…*")
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
    max_rounds = (
        MAX_TOOL_ROUNDS_RELATIONAL
        if conversation_id == channels.JOHN_CHARLES
        else MAX_TOOL_ROUNDS
    )
    for round_n in range(max_rounds):
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
        # Persistence policy: in JOHN_CHARLES (the relational chat), the user
        # only wants to see his own messages and Charles's FINAL reply — not
        # the tool plumbing or "let me X" intermediate narration. So skip
        # logging mid-chain assistant turns + tool calls for that channel.
        # The chain's in-memory `history` still has them for the model.
        # CHARLES_LOG keeps the full record (it's the operational stream).
        if conversation_id and conversation_id != channels.JOHN_CHARLES:
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
            # Same persistence policy as the assistant-turn log above: don't
            # pollute JOHN_CHARLES with tool result rows.
            if conversation_id and conversation_id != channels.JOHN_CHARLES:
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
        log.warning("hit max_rounds=%d, forcing final summary", max_rounds)
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

    # Welcome-back prefix — first reply after a >6h gap from John gets a
    # brief "Mornin', John." / "Afternoon, John." / "Evenin', John." prefix.
    # Computed BEFORE we log the assistant reply (else the prefix double-logs
    # if anything fails downstream). Human convs only — never on goal:/
    # heartbeat:/sunday_test_ ticks.
    try:
        prefix = _welcome_back_prefix(conversation_id)
        if prefix and final_text and not final_text.startswith("(stopped"):
            final_text = prefix + final_text
    except Exception as e:  # noqa: BLE001
        log.warning("welcome-back prefix failed (non-fatal): %s", e)

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
    # John's conversational fillers (observed 2026-05-10) — common short
    # verbs/adverbs that aren't actually distinctive
    "all", "through", "few", "minutes", "minute", "any", "some", "more",
    "really", "actually", "kind", "sort", "stuff", "thing", "things",
    "give", "tell", "show", "ask", "see", "got", "getting", "doing",
    "started", "start", "ready", "good", "great", "fine", "fire", "still",
    "back", "much", "many", "very", "much", "even", "well", "right",
    "every", "ever", "next", "last", "first", "best", "better", "less",
    "lot", "lots", "way", "ways", "around", "above", "below", "before",
    "after", "since", "while", "during", "until", "yet", "between",
    "without", "within", "off", "over", "under", "than", "much",
    "ys", "sir", "yo", "hey", "hi", "hello", "lol", "haha",
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

    Fires on BOTH channels (JOHN_CHARLES + CHARLES_LOG) — both produce
    findings worth remembering (Charles's replies to John often contain
    URL/path/decision content; goal-tick narration in CHARLES_LOG produces
    most of the autonomous research findings).

    Skips legacy `heartbeat:` / `sunday_test_` rows that may still arrive
    while migration is in flight.
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
                    tags=_with_john_vocab_tags("auto_finding,extracted_from_reply"),
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
# Voice: subtle g-dropping + casual phrasing, in line with Charles's
# Southern-Black-blue-collar-sophisticated-with-whiskey-warmth doctrine.
# Light flavor, not minstrel — the goal is "feels like Charles", not caricature.
_TOOL_VERBS = {
    "browse_url": "Pullin' up",
    "browser_screenshot": "Snappin' a pic of",
    "read_file": "Crackin' open",
    "write_file": "Layin' down",
    "exec_shell": "Runnin' shell:",
    "search_web": "Hittin' the web for",
    "search_facts": "Diggin' through memory for",
    "recall": "Pullin' from memory:",
    "remember": "Tuckin' away",
    "send_imessage": "Textin' John:",
    "send_email": "Sendin' an email:",
    "list_emails": "Checkin' the inbox",
    "read_email": "Readin' an email",
    "archive_email": "Filin' that one away",
    "set_goal": "Lockin' in a goal:",
    "list_goals": "Lookin' over my goals",
    "append_goal_note": "Loggin' a note",
    "complete_goal": "Wrappin' up:",
    "cancel_goal": "Killin' that goal:",
    "update_goal_status": "Flippin' that goal's status:",
    "schedule_task": "Pencilin' in a task:",
    "list_scheduled_tasks": "Checkin' the schedule",
    "current_time": "Glancin' at the clock",
    "get_weather": "Checkin' the weather for",
    "system_status": "Pulse check on the box",
    "self_modify": "Tweakin' my own code:",
    "self_patch": "Patchin' myself:",
    "analyze_sentiment": "Readin' the room:",
    "request_approval": "Askin' John:",
    "resolve_approval": "Closin' an approval",
    "notify_john": "Pingin' John:",
    "add_task": "Pinnin' a task:",
    "list_open_tasks": "Checkin' open tasks",
    "reset_my_conversation": "Wipin' the slate",
    "solve_recaptcha": "Crackin' a captcha",
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
    if name in ("complete_goal", "cancel_goal", "set_goal", "update_goal_status"):
        return str(args.get("description") or args.get("summary") or args.get("goal_id", ""))[:48]
    if args:
        first_key, first_val = next(iter(args.items()))
        return f"{first_key}={str(first_val)[:40]}"
    return ""


def _format_progress(tool_name: str, args_json: str, result: str | None = None) -> str:
    """Build a single-line italic action note. If `result` is None, this is
    the IN-PROGRESS form ('Pullin' up wikipedia.org…'). If `result` is
    given, this is the JUST-FINISHED form (briefly shows outcome before
    next round overwrites it). Voice-aligned with Charles's doctrine."""
    verb = _TOOL_VERBS.get(tool_name, tool_name)
    target = _short_target(tool_name, args_json)
    base = f"{verb} {target}".strip().rstrip(":")

    # In-progress (no result yet)
    if result is None:
        return f"*{base}…*"

    # Finished — append a short outcome with light Charles flavor
    head = result[:140] if result else ""
    if head.startswith("[BLOCKED"):
        m = re.search(r"reason=(\S+)", head)
        reason = (m.group(1) if m else "blocked").replace("_", " ")
        return f"*{base} → {reason}, on to the next*"
    if head.startswith("[error]"):
        if "STOP. You have now called" in head:
            label = "got the stop signal — pivotin'"
        elif "you already called" in head:
            label = "been there — movin' on"
        elif "you already tried this URL" in head:
            label = "URL's dead — skippin'"
        elif "missing required argument" in head:
            label = "swung at that one wrong, retryin'"
        elif "your own memory database" in head:
            label = "redirected to recall()"
        elif "you've made" in head and "recall" in head:
            label = "wrong tag schema — broadenin' the query"
        else:
            label = "hit an error"
        return f"*{base} → {label}*"
    if head.startswith("[cached"):
        return f"*{base} → already got that one*"
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
# Convs where auto-extract is allowed: JOHN_CHARLES only. Charles's autonomous
# narration in CHARLES_LOG shouldn't auto-create tasks for John — those are
# meant to be Charles's own work, not John's todo list.
def _autoextract_tasks(reply_text: str, conversation_id: str) -> int:
    """Scan a final assistant reply for task-language; create tasks for matches.
    Returns the count of tasks created. JOHN_CHARLES only."""
    if conversation_id != channels.JOHN_CHARLES:
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


# ---------------------------------------------------------------------------
# Welcome-back greeting — first reply after a >6h gap from John gets a brief
# "Mornin'/Afternoon/Evenin', John." prefix in voice.
#
# "First reply only" is enforced naturally: by the second reply in a session,
# the gap between successive user turns will be small (seconds to minutes),
# so the prefix won't fire again.
# ---------------------------------------------------------------------------

_WELCOME_BACK_GAP_HOURS = 6.0


def _welcome_back_prefix(conversation_id: str | None) -> str:
    """Return 'Mornin'/Afternoon/Evenin', John. ' if it's been >6h since the
    PREVIOUS user turn in this conv. Otherwise empty string.

    JOHN_CHARLES channel only — never on operational ticks.
    """
    if conversation_id != channels.JOHN_CHARLES:
        return ""

    # Pull the two most-recent user turns in this conv. The most-recent is
    # the one we just logged at respond-start; the second is the previous
    # user message — that's the one we measure the gap from. If only one
    # exists (first message ever in this conv), no prefix.
    with memory._conn() as c:
        rows = c.execute(
            "SELECT created_at FROM conversations "
            "WHERE conversation_id=? AND role='user' "
            "ORDER BY id DESC LIMIT 2",
            (conversation_id,),
        ).fetchall()
    if len(rows) < 2:
        return ""

    prev_at = rows[1]["created_at"]
    try:
        prev = datetime.fromisoformat(prev_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    hours = (now - prev).total_seconds() / 3600
    if hours < _WELCOME_BACK_GAP_HOURS:
        return ""

    # Time-of-day in John's wall clock (America/New_York). EST label is
    # forced via memory rules elsewhere — for the greeting we just need
    # local hour to pick which form.
    local = datetime.now(ZoneInfo("America/New_York"))
    h = local.hour
    if 4 <= h < 12:
        greeting = "Mornin', John."
    elif 12 <= h < 18:
        greeting = "Afternoon, John."
    else:
        greeting = "Evenin', John."
    return greeting + " "


# ---------------------------------------------------------------------------
# Cross-channel context — when John speaks in JOHN_CHARLES, surface a tight
# summary of what Charles has been doing autonomously in CHARLES_LOG since
# John's previous turn. Closes the "what were you up to?" gap without forcing
# John to ask.
# ---------------------------------------------------------------------------

_CHARLES_LOG_SUMMARY_MAX_LINES = 8
_CHARLES_LOG_SUMMARY_MAX_CHARS = 1200
_CHARLES_LOG_LOOKBACK_HOURS = 24


def _build_charles_log_summary() -> str:
    """Return a system-note string with recent CHARLES_LOG activity, or empty.

    Pulls assistant turns from CHARLES_LOG within the last 24h, dedups
    near-duplicates, caps to a small bullet list. Designed to merge into the
    leading system prompt when John speaks, so Charles has fresh awareness
    of his own autonomous work without re-reading the full log.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_CHARLES_LOG_LOOKBACK_HOURS)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with memory._conn() as c:
        rows = c.execute(
            "SELECT content FROM conversations "
            "WHERE conversation_id=? AND role='assistant' AND created_at >= ? "
            "ORDER BY id DESC LIMIT 25",
            (channels.CHARLES_LOG, cutoff),
        ).fetchall()
    if not rows:
        return ""

    seen_keys: set[str] = set()
    bullets: list[str] = []
    for row in rows:
        content = (row["content"] or "").strip()
        if not content or content.startswith("(stopped"):
            continue
        # Take just the first sentence/line of each assistant turn — the goal
        # tick narration is structured "I did X. Next step is Y." Keep the
        # past-tense action, drop the forward-looking part.
        first = content.split("\n", 1)[0].strip()
        first = first.split(". ", 1)[0].strip().rstrip(".") + "."
        if len(first) < 12 or len(first) > 220:
            continue
        # Dedup on lowercased prefix — goal ticks often repeat themes
        key = first.lower()[:60]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        bullets.append(f"- {first}")
        if len(bullets) >= _CHARLES_LOG_SUMMARY_MAX_LINES:
            break

    if not bullets:
        return ""

    # IMPORTANT framing: John is asking you a question NOW. The bullets below
    # are background context about what your autonomous side (CHARLES_LOG)
    # has been doing — they are NOT instructions to continue that work.
    # Answer John's actual message. If he asks for a status, ONE plain-text
    # reply summarizing what you've done. Don't run 20 rounds of tool calls
    # to "verify" or "continue" — your goal-tick chain is doing that work
    # in the background. This conversation is the relational thread.
    out = (
        "Background — what your autonomous side has been doing in CHARLES_LOG since "
        "John last messaged. Reference only; do NOT continue this work in this "
        "reply. Answer John's actual message in one direct response.\n"
        + "\n".join(bullets)
    )
    if len(out) > _CHARLES_LOG_SUMMARY_MAX_CHARS:
        out = out[: _CHARLES_LOG_SUMMARY_MAX_CHARS - 1] + "…"
    return out


# ---------------------------------------------------------------------------
# John-vocab tagging — the recall index is built around how JOHN talks, not
# how Charles labels things. Every time a fact is auto-saved, we look at
# John's most recent message in JOHN_CHARLES, extract simple keywords, and
# attach them as `john:<kw>` tags so future recall pulls the fact when John
# uses the same phrasing — even if Charles internally tagged it differently.
#
# John's register: blue-collar, simple, non-tech, direct. Three months in,
# beating it into submission. The vocab index reflects that — short
# concrete nouns and verbs, not jargon.
# ---------------------------------------------------------------------------

_JOHN_VOCAB_MAX_KEYWORDS = 5
_JOHN_VOCAB_LOOKBACK = 3  # how many recent John messages to pool keywords from


def _recent_john_keywords() -> list[str]:
    """Pull keywords from John's most recent N messages in JOHN_CHARLES.

    Used to build the John-vocab tag set for facts being saved RIGHT NOW.
    Pooling several recent messages helps when John's first message kicks
    off work and the keyword-relevant phrasing was a few turns back.
    """
    try:
        with memory._conn() as c:
            rows = c.execute(
                "SELECT content FROM conversations "
                "WHERE conversation_id=? AND role='user' "
                "ORDER BY id DESC LIMIT ?",
                (channels.JOHN_CHARLES, _JOHN_VOCAB_LOOKBACK),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    pool: list[str] = []
    seen: set[str] = set()
    for row in rows:
        content = (row["content"] or "").strip()
        if not content:
            continue
        for kw in _extract_keywords(content, max_kw=_JOHN_VOCAB_MAX_KEYWORDS):
            if kw not in seen:
                seen.add(kw)
                pool.append(kw)
    return pool[:_JOHN_VOCAB_MAX_KEYWORDS]


def _with_john_vocab_tags(base_tags: str) -> str:
    """Append `john:<kw>` tags to a base tag string. Returns base_tags
    unchanged if no John keywords are available right now (cold start, or
    John hasn't said anything yet)."""
    kws = _recent_john_keywords()
    if not kws:
        return base_tags
    extra = ",".join(f"john:{kw}" for kw in kws)
    return f"{base_tags},{extra}" if base_tags else extra
