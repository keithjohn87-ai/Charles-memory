"""iMessage tools for Charles — send and read messages on John's Mac.

Uses osascript trampolines:
  - send: AppleScript `tell application "Messages" ... send`
  - read: `osascript do shell script "sqlite3 ~/Library/Messages/chat.db ..."`
        (direct sqlite3 from Charles's process chain hits a TCC translocation
         issue; routing through osascript inherits Messages.app's permission
         context and works.)

Charles runs alongside Claude Code during the build phase. iMessage will
eventually be Charles's primary channel; for now both are wired so the
transition is one config change away.
"""
from __future__ import annotations

import json
import shlex
import subprocess

from core.tools import tool

# John's number — pulled from grounding so Charles always knows where to write
DEFAULT_TARGET = "+16156637932"


def _osa_shell(cmd: str, timeout: int = 15) -> str:
    """Run a shell command via osascript do-shell-script trampoline. Returns stdout."""
    osa = f"do shell script {json.dumps(cmd)}"
    r = subprocess.run(
        ["osascript", "-e", osa],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def _osa_messages(applescript: str, timeout: int = 15) -> str:
    """Run an AppleScript snippet against Messages.app."""
    r = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


@tool(
    name="send_imessage",
    summary="Send an iMessage to John (or another phone/email). Native macOS Messages.app. Use this when John has explicitly said to use iMessage, or as a backup when Telegram is unavailable.",
    triggers=("imessage", "send imessage", "text john", "message john on imessage"),
    schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Message body to send. Plain text. Can include emoji.",
            },
            "target": {
                "type": "string",
                "description": "Phone (E.164 like +16156637932) or email. Defaults to John's number.",
                "default": DEFAULT_TARGET,
            },
        },
        "required": ["message"],
    },
)
def send_imessage(message: str, target: str = DEFAULT_TARGET) -> str:
    msg = (message or "").strip()
    if not msg:
        return "[error] empty message"
    # Escape for AppleScript: backslash + double quote
    msg_esc = msg.replace("\\", "\\\\").replace('"', '\\"')
    target_esc = target.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{target_esc}" of targetService
    send "{msg_esc}" to targetBuddy
end tell
'''
    try:
        _osa_messages(script)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"sent {len(msg)} chars to {target} via iMessage"


@tool(
    name="recent_imessages",
    summary="Read the most recent N messages from a contact's iMessage thread. Useful for catching up on what John has said when Charles wakes up after time away.",
    triggers=("recent imessages", "read imessages", "imessage history"),
    schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many recent messages to return. Default 20, max 100.",
                "default": 20,
            },
            "from_handle": {
                "type": "string",
                "description": "Phone or email to filter by. Defaults to John's number.",
                "default": DEFAULT_TARGET,
            },
        },
    },
)
def recent_imessages(limit: int = 20, from_handle: str = DEFAULT_TARGET) -> str:
    limit = max(1, min(int(limit), 100))
    handles = [from_handle, from_handle.lstrip("+"), "+1" + from_handle.lstrip("+1").lstrip("+")]
    handle_list = ",".join(f"'{h}'" for h in set(handles))
    sql = (
        "SELECT m.ROWID, "
        "datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime'), "
        "CASE WHEN m.is_from_me=1 THEN 'me' ELSE 'them' END, "
        "COALESCE(m.text, '') "
        "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
        f"WHERE h.id IN ({handle_list}) "
        f"ORDER BY m.ROWID DESC LIMIT {limit};"
    )
    sql_safe = sql.replace("'", "'\"'\"'")  # for inner shell quoting
    cmd = f"sqlite3 -separator '|' ~/Library/Messages/chat.db {shlex.quote(sql)}"
    try:
        out = _osa_shell(cmd, timeout=20)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    if not out:
        return f"(no iMessages found for {from_handle})"
    # Reverse for chronological order
    lines = list(reversed([l for l in out.split("\n") if l.strip()]))
    formatted = []
    for line in lines:
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        rowid, ts, who, text = parts
        if not text.strip():
            continue
        formatted.append(f"[{ts}] {who}: {text.strip()}")
    return "\n".join(formatted) if formatted else f"(no readable iMessages for {from_handle})"
