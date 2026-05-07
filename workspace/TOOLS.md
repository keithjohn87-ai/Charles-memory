# TOOLS

## Currently available (M1 + M2 + M3)

- **`read_file(path)`** — read a UTF-8 text file. `~`-expansion supported. Cap: 64KB returned.
- **`write_file(path, content, append=False)`** — overwrite (default) or append; creates parent dirs automatically.
- **`exec_shell(command, timeout=60)`** — full zsh, no allowlist, no confirmation gate. stdout/stderr returned with exit code, output capped at 8KB.
- **`remember(fact, tags="")`** — save a fact to long-term memory (SQLite, persists across conversations).
- **`recall(query)`** — substring search over saved facts; up to 5 results.
- **`self_modify(path, new_content, reason)`** — overwrite one of my own files. Auto-backups to `workspace/self_modify_backups/`, syntax-checks `.py`, git-commits on success. Effect on NEXT restart.
- **`self_patch(path, find, replace, reason)`** — find-and-replace a unique snippet. Cheaper than `self_modify` for small edits. Same backup + commit guarantees.
- **`current_time()`** — return current local (EST) time as a formatted string.
- **`get_weather(location, format=...)`** — fetch current weather + short forecast via wttr.in. No API key.
- **`set_goal(description, advance_minutes=)`** — create a long-running open-ended goal. Heartbeat advances it every advance_minutes; you take one concrete step per tick, log notes, and complete when done.
- **`list_goals(status=)`** / **`append_goal_note(goal_id, note)`** / **`complete_goal(goal_id, summary)`** / **`cancel_goal(goal_id)`** — goal lifecycle.
- **`send_imessage(message, target=...)`** — send a native macOS iMessage. Defaults to John's number. Used for the alternate channel (Telegram is your primary; iMessage will eventually be your sole channel).
- **`recent_imessages(limit=, from_handle=)`** — read recent iMessage thread. Useful for catching up on what John has said when you wake up after time away.
- **`analyze_sentiment(text)`** — classify positive / neutral / negative with confidence. Use to read John's mood from his messages, or to vet your own draft replies before sending.
- **`browse_url(url, wait_for=)`** — open a URL in headless Chromium and return the visible page text. Handles JS-rendered pages that curl can't read.
- **`browser_screenshot(url, path=)`** — full-page screenshot to disk. Returns the path.
- **`schedule_task(description, in_seconds=, at_iso=, cadence_seconds=)`** — queue a future tick. `cadence_seconds` makes it recurring.
- **`list_scheduled_tasks(status="pending")`** — show scheduled tasks. Status: pending/running/done/failed/cancelled/all.
- **`cancel_scheduled_task(task_id)`** — cancel a pending task.
- **`notify_john(message)`** — proactively message John on Telegram. Use sparingly; silence is correct unless one of the SOUL.md criteria applies.

## How tools are loaded

The classifier in `core/tools.py` picks 0–3 tools per turn by matching trigger keywords against the inbound message. Only matched tools' full JSON schemas go into the prompt that turn — everything else is just a one-line summary. Result: lean default prompt, fast turns, room to grow toward dozens of tools without bloat.

If I think I need a tool but its schema isn't loaded this turn, I can either:
- Re-phrase using a clearer keyword and the classifier will catch it next turn, or
- Use `exec_shell` (loaded for most action verbs) to do the equivalent.

## M5+ landed (2026-05-07 morning)

- **iMessage** ✓ — `send_imessage` and `recent_imessages` via osascript trampoline
- **Whisper voice** ✓ — voice messages on Telegram auto-transcribed via mlx-whisper
- **Playwright browser** ✓ — `browse_url` and `browser_screenshot` (Chromium headless)
- **Sentiment** ✓ — `analyze_sentiment` via cardiffnlp twitter-roberta
- **Voice clone** ✓ — Chatterbox 4-bit cloning your Keith David reference at `workspace/voice_reference.wav`. Active for all voice replies via Telegram.

## Heartbeat (M4)

A 15-second background tick runs alongside the Telegram channel. Each tick:
1. Pulls any `pending` scheduled_tasks whose `due_at` has passed
2. Runs each task through the agent with a synthetic `[heartbeat task #N] {description}` prompt and conversation_id `heartbeat:N`
3. On success: marks `done` (or reschedules if `cadence_seconds` is set)
4. On failure: marks `failed` with the error

Heartbeat-fired turns are isolated from John's main Telegram conversation history — they have their own `conversation_id`. Facts saved via `remember` during a heartbeat tick are still globally visible.

## Self-modify safety net

Every `self_modify` / `self_patch` call:
1. Copies the existing file to `workspace/self_modify_backups/<name>.<timestamp>.bak`
2. Refuses on Python `SyntaxError` (won't write a broken `.py`)
3. Git-commits the change with the reason as the message — so `git log` is my audit trail and `git reset --hard <prev>` undoes any change

To roll back the most recent self-edit: `exec_shell("git reset --hard HEAD~1")` from the Charles root.

To inspect my current tool registry at any time:
```
read_file('/Users/home/charles/tools/__init__.py')
read_file('/Users/home/charles/tools/filesystem.py')
read_file('/Users/home/charles/tools/shell.py')
```

🌊
