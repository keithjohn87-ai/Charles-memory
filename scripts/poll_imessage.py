#!/usr/bin/env python3
"""Poll macOS iMessage chat.db for new messages from John.

Emits one stdout line per event. Used as a Monitor source so I (Claude Code)
get notifications when John iMessages me. Charles continues to handle Telegram
on his own.

Reads chat.db via `osascript do shell script` trampoline because direct sqlite3
from this process chain hits a TCC translocation issue, while osascript inherits
Messages.app's permission context and works.

State: last-seen ROWID stored at /tmp/imessage_lastrowid.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

STATE = Path("/tmp/imessage_lastrowid")
POLL_SECONDS = 600  # 10 min

JOHN_HANDLES = ("+16156637932", "6156637932", "+1 615-663-7932", "16156637932")

_fda_warned = False


def _query(sql: str) -> str:
    """Run a SQL query against chat.db via osascript trampoline."""
    inner = f'sqlite3 ~/Library/Messages/chat.db {shlex.quote(sql)}'
    osa = f'do shell script {json.dumps(inner)}'
    result = subprocess.run(
        ["osascript", "-e", osa],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def _get_last_rowid() -> int:
    if STATE.exists():
        try:
            return int(STATE.read_text().strip() or 0)
        except ValueError:
            return 0
    return 0


def _save_last_rowid(rowid: int) -> None:
    STATE.write_text(str(rowid))


def poll() -> None:
    global _fda_warned
    try:
        last = _get_last_rowid()
        if last == 0:
            max_row = int(_query("SELECT COALESCE(MAX(ROWID), 0) FROM message;") or 0)
            _save_last_rowid(max_row)
            print(f"[INIT] baseline rowid={max_row}", flush=True)
            return

        handle_list = ",".join(f"'{h}'" for h in JOHN_HANDLES)
        sql = (
            "SELECT m.ROWID || '|||' || COALESCE(m.text, '') "
            "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
            f"WHERE m.ROWID > {last} AND m.is_from_me = 0 AND h.id IN ({handle_list}) "
            "ORDER BY m.ROWID;"
        )
        out = _query(sql)
        if not out:
            return
        for line in out.split("\n"):
            if "|||" not in line:
                continue
            rowid_s, text = line.split("|||", 1)
            try:
                rowid = int(rowid_s)
            except ValueError:
                continue
            text_clean = text.replace("\r", " ").replace("\n", " ").strip()
            if text_clean:
                print(f"[FROM_JOHN] rowid={rowid} text={text_clean!r}", flush=True)
            _save_last_rowid(rowid)
        _fda_warned = False
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "authorization denied" in msg or "operation not permitted" in msg.lower():
            if not _fda_warned:
                print(f"[FDA_NEEDED] {msg}", flush=True)
                _fda_warned = True
        else:
            print(f"[ERROR] {type(e).__name__}: {msg}", flush=True)


def main() -> None:
    print(f"[START] iMessage poller — every {POLL_SECONDS}s, watching {JOHN_HANDLES}", flush=True)
    while True:
        poll()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
