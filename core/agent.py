"""Single-conversation reasoning with multi-round tool calls and persistent memory.

M2: every turn loads the last few user/assistant exchanges for the same
conversation_id from SQLite and prepends them to the prompt. Each user
message and final assistant reply is also persisted, so Charles is
continuous across Telegram messages — not a goldfish.
"""
from __future__ import annotations

import logging

import tools  # noqa: F401  — import side-effect: registers all tools

from core import memory
from core.inference import complete
from core.prompts import build_system_prompt
from core.tools import REGISTRY, dispatch  # select_tools still in core.tools, kept for future

log = logging.getLogger("charles.agent")

MAX_TOOL_ROUNDS = 25
HISTORY_CHAR_BUDGET = 4000


def respond(message: str, conversation_id: str | None = None) -> str:
    system = build_system_prompt()
    history: list[dict] = [{"role": "system", "content": system}]

    if conversation_id:
        prior = memory.recent_history(conversation_id, max_chars=HISTORY_CHAR_BUDGET)
        history.extend(prior)
        log.info("loaded %d prior turns for conv=%s", len(prior), conversation_id)

    history.append({"role": "user", "content": message})

    if conversation_id:
        memory.log_turn(conversation_id, "user", message)

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
    for round_n in range(MAX_TOOL_ROUNDS):
        # max_tokens=4000: tool_call args can carry long write_file content
        # (e.g., a multi-page analysis). 800 was truncating Charles mid-write.
        text, msg, usage = complete(history, tools=api_tools, max_tokens=4000)
        log.info(
            "round=%d usage=%s tool_calls=%d",
            round_n,
            usage,
            len(msg.tool_calls or []),
        )

        if not msg.tool_calls:
            final_text = text
            break

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
    else:
        final_text = text or "(max tool rounds reached)"

    if conversation_id and final_text:
        memory.log_turn(conversation_id, "assistant", final_text)

    return final_text
