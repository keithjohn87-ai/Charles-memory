#!/usr/bin/env python3
"""Charles Behavioral Watchdog — the immune system.

Where `scripts/watchdog.py` is a process-liveness check (pid alive? log fresh?
kickstart if not), THIS watchdog is a behavioral peer to Charles. It polls
memory.db every 30s and detects + remediates the failure modes that the
2026-05-09 forensic exposed:

  - Response loops (intra-call AND between-call)
  - Narration stalls ("let me X" 3+ times without a tool result)
  - Hallucinations (Tesla / Bugatti / etc. — known-bad term list)
  - Goal idleness (active goal not advancing for 3x its cadence)
  - Tool error storms (5+ [error] results in 5 min on one conv)
  - System health (MLX unreachable, disk low, .env missing, agent log stale)
  - Hung respond() (heartbeat log not advancing while agent pid alive)

It intervenes proactively — trim the poisoned tail, cancel the spinning goal,
reset the conv, request stop on hung calls, kickstart Charles only when soft
remediation has failed N times in a row. Every action is logged as a fact
tagged "intervention,auto,<kind>" so John has an audit trail.

It also actively prunes — keeps the runway clear (per John's directive
2026-05-09: "his self pruning needs to be actionable. We don't want a pile
of garbage clogging up the runway"):

  - Conv history per conv_id: cap at 200 rows, drop oldest
  - Daily log: drop > 7 days
  - Goal notes: cap last 10 per goal; cancelled/done → wipe notes
  - Self-modify backups: drop > 14 days
  - Audio temps (/tmp/warroom_audio): drop > 1 hour
  - Conversation table hard ceiling: 50K rows
  - Each prune logged as fact tagged "prune,auto,<kind>"

Reports to John via iMessage when it had to intervene OR when something needs
human eyes (system resource crisis, kickstart failed, security flag).
Stays silent on routine self-heals.

Itself supervised: runs under LaunchAgent com.charles.behavior_watchdog with
KeepAlive. NO LLM calls of its own — deterministic Python only.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Stand alone — but reuse the project's modules where they exist.
sys.path.insert(0, "/Users/home/charles")

from config import LOGS, WORKSPACE  # noqa: E402
from core import goals as goals_mod  # noqa: E402
from core import memory as memory_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CHECK_SECONDS = 30                       # detection tick cadence
PRUNE_EVERY_SECONDS = 600                # full prune sweep every 10 min
SCAN_WINDOW_SECONDS = 1800               # detection looks at last 30 min of activity

# Loop detection
LOOP_MIN_REPEATS = 3                     # 3+ near-identical assistant turns = loop
LOOP_SIMILARITY_THRESHOLD = 0.7
LOOP_PREFIX_CHARS = 50

# Narration stall
NARRATION_MIN_HITS = 3
NARRATION_PHRASES = (
    "let me", "i'll", "i will", "i need to", "i'm going to",
    "now i need", "now i'll", "going to write", "going to create",
    "let me check", "let me think", "let me try", "let me start",
    "let me get", "let me extract", "let me write",
)

# Hallucination terms — fabricated entities Charles invented in 2026-05-09
# loop episodes. Tuned to be SPECIFIC enough not to false-positive on
# legitimate research mentions. "ford" + "luxury car" + "company about page"
# were dropped on 2026-05-10 morning after a false positive on legitimate
# psychology/business references. If Tesla/Bugatti/Rivian show up
# legitimately too, narrow further or require 2+ terms to co-occur.
HALLUCINATION_TERMS = (
    "tesla", "bugatti", "rivian", "larsonjuis", "larson juis",
)
GUARD_NOTICE_MARKER = "<<GUARD_NOTICE>>"  # heartbeat-issued lines, ignore

# Tool error storm
ERROR_STORM_MIN = 5
ERROR_STORM_WINDOW_SECONDS = 300

# Goal idleness
GOAL_IDLE_MULTIPLIER = 3                 # last_advanced > 3x advance_seconds = idle
GOAL_IDLE_FLOOR_SECONDS = 1800           # never flag earlier than 30 min

# Hung respond()
HEARTBEAT_LOG_STALE_SECONDS = 600        # log unchanged 10+ min while pid alive

# Pruning policy
CONV_HISTORY_CAP_PER_ID = 200            # rows per conversation_id
DAILY_LOG_RETENTION_DAYS = 7
GOAL_NOTES_LINE_CAP = 10                 # keep most recent N notes per goal
SELF_MODIFY_BACKUP_RETENTION_DAYS = 14
AUDIO_TMP_RETENTION_HOURS = 1
CONV_TABLE_HARD_CEILING = 50_000         # global row count
LONG_TERM_FACTS_CONSOLIDATE_THRESHOLD = 500

# Restart escalation
KICKSTART_AFTER_N_LOOPS = 3              # 3 consecutive ticks with intervention → kickstart
KICKSTART_COOLDOWN_SECONDS = 1800        # don't kickstart more than once per 30 min

# iMessage rate limit
IMSG_COOLDOWN_SECONDS = 1800             # alerts to John ≤ once per 30 min (per category)

# Paths
DB_PATH = WORKSPACE / "memory.db"
ENV_PATH = Path("/Users/home/charles/.env")
AGENT_LOG = Path("/Users/home/charles/logs/charles.launchd.err.log")
SELF_MODIFY_BACKUPS = WORKSPACE / "self_modify_backups"
AUDIO_TMP = Path("/tmp/warroom_audio")
STATE_PATH = Path("/tmp/charles_behavior_watchdog_state.json")
WATCHDOG_LOG = LOGS / "behavior_watchdog.log"

CHARLES_LABEL = "com.charles.agent"
JOHN_IMESSAGE = "+16156637932"

# Disk threshold (workspace fs)
DISK_FREE_MIN_GB = 5
MLX_BASE_URL = os.environ.get("MLX_BASE_URL", "http://127.0.0.1:8080/v1")

# Conversations excluded from "behavioral" loop intervention because they're
# diagnostic / synthetic (the stress test seeds these on purpose).
CONV_PREFIX_SKIP = ("stress_", "smoketest", "sunday_test_")

# CHARLES_LOG aggregates all goal-tick + heartbeat work into one conv. Tool
# errors get spread across multiple goals there, so the per-conv error-storm
# threshold is meaningless. The in-process tool_guards module already
# dedupes same-call retries and blocks dead URLs — the storm detector's
# redundant signal here just generates false positives every tick.
ERROR_STORM_SKIP_CONV_IDS = ("charles_log",)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(WATCHDOG_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("behavior_watchdog")


# ---------------------------------------------------------------------------
# State (consecutive loop counter, alert cooldowns, last prune timestamp)
# ---------------------------------------------------------------------------

# In-memory cooldown for "skipped intervention" log spam suppression.
# Not persisted — process-local. tick() races us on the state file so this
# avoids the lost-write hazard and we're OK losing a 10-min cooldown across
# restarts (worst case: one extra warning logged after a restart).
_LOOP_SKIP_COOLDOWNS: dict[str, float] = {}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state))
    except Exception as e:  # noqa: BLE001
        log.warning("could not write state: %s", e)


# ---------------------------------------------------------------------------
# DB helpers (read-only path; writes go via memory_mod / goals_mod for audit)
# ---------------------------------------------------------------------------

def _ro_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    """Cheap similarity — same heuristic as core/memory.py:_similarity."""
    if not a or not b:
        return 0.0
    a_n, b_n = a.strip().lower(), b.strip().lower()
    if a_n == b_n:
        return 1.0
    if a_n[:LOOP_PREFIX_CHARS] == b_n[:LOOP_PREFIX_CHARS] and a_n[:LOOP_PREFIX_CHARS]:
        return 0.95
    sa, sb = set(a_n.split()), set(b_n.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def detect_response_loops() -> list[dict]:
    """Per-conv response loops in the last SCAN_WINDOW_SECONDS.

    Per conversation_id, look at recent assistant turns. If 3+ are pairwise
    >= LOOP_SIMILARITY_THRESHOLD similar, flag the conv. Skips diagnostic
    conv prefixes (stress_, smoketest, sunday_test_).
    """
    cutoff = _ago_iso(SCAN_WINDOW_SECONDS)
    incidents: list[dict] = []
    with _ro_conn() as c:
        rows = c.execute(
            "SELECT conversation_id, content FROM conversations "
            "WHERE role='assistant' AND created_at >= ? "
            "ORDER BY conversation_id, id DESC",
            (cutoff,),
        ).fetchall()

    by_conv: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_conv[r["conversation_id"]].append(r["content"] or "")

    for conv_id, contents in by_conv.items():
        if any(conv_id.startswith(p) for p in CONV_PREFIX_SKIP):
            continue
        # Only look at the most recent window of turns
        window = contents[:6]
        if len(window) < LOOP_MIN_REPEATS:
            continue
        # Count near-identical pairs in the window
        pairs_above = 0
        for i in range(len(window)):
            for j in range(i + 1, len(window)):
                if _similarity(window[i], window[j]) >= LOOP_SIMILARITY_THRESHOLD:
                    pairs_above += 1
        # 3+ turns near-identical means at least 3 pairs above threshold
        if pairs_above >= LOOP_MIN_REPEATS:
            incidents.append({
                "kind": "response_loop",
                "conv_id": conv_id,
                "pairs_above": pairs_above,
                "sample": window[0][:120],
            })
    return incidents


def detect_narration_stalls() -> list[dict]:
    """Active goals whose last 6 notes are mostly 'let me X' / 'I'll X'."""
    incidents: list[dict] = []
    try:
        active = goals_mod.list_goals(status="active")
    except Exception as e:  # noqa: BLE001
        log.exception("goals.list_goals failed: %s", e)
        return incidents

    for g in active:
        notes = g.get("notes") or ""
        if not notes:
            continue
        lines = [
            ln for ln in notes.split("\n")
            if ln.strip().startswith("[") and GUARD_NOTICE_MARKER not in ln
        ][-6:]
        hits = 0
        for line in lines:
            lower = line.lower()
            if any(p in lower for p in NARRATION_PHRASES):
                hits += 1
        if hits >= NARRATION_MIN_HITS:
            incidents.append({
                "kind": "narration_stall",
                "goal_id": g["id"],
                "hits": hits,
                "description": g["description"][:80],
            })
    return incidents


def detect_hallucinations() -> list[dict]:
    """Hallucinated terms in active goal notes (excluding guard's own warnings)."""
    incidents: list[dict] = []
    try:
        active = goals_mod.list_goals(status="active")
    except Exception as e:  # noqa: BLE001
        log.exception("goals.list_goals failed: %s", e)
        return incidents

    for g in active:
        notes = g.get("notes") or ""
        if not notes:
            continue
        # Skip lines the guard itself wrote
        clean = "\n".join(
            ln for ln in notes.split("\n") if GUARD_NOTICE_MARKER not in ln
        ).lower()
        for term in HALLUCINATION_TERMS:
            if term in clean:
                incidents.append({
                    "kind": "hallucination",
                    "goal_id": g["id"],
                    "term": term,
                    "description": g["description"][:80],
                })
                break  # one report per goal is enough
    return incidents


def detect_goal_idleness() -> list[dict]:
    """Active goals whose last_advanced_at is older than 3x cadence (and at least
    GOAL_IDLE_FLOOR_SECONDS) — and the agent IS supposed to be running.
    """
    if not _agent_loaded():
        return []  # if Charles is down on purpose, idle goals are expected
    incidents: list[dict] = []
    try:
        active = goals_mod.list_goals(status="active")
    except Exception as e:  # noqa: BLE001
        log.exception("goals.list_goals failed: %s", e)
        return incidents

    now = datetime.now(timezone.utc)
    for g in active:
        last = g.get("last_advanced_at")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        cadence = max(int(g.get("advance_seconds") or 300), 60)
        threshold = max(cadence * GOAL_IDLE_MULTIPLIER, GOAL_IDLE_FLOOR_SECONDS)
        if (now - last_dt).total_seconds() >= threshold:
            incidents.append({
                "kind": "goal_idle",
                "goal_id": g["id"],
                "idle_seconds": int((now - last_dt).total_seconds()),
                "description": g["description"][:80],
            })
    return incidents


_TOOL_ERROR_RE = re.compile(r"^\[error\]", re.IGNORECASE)
# Errors emitted by the dispatcher's own guards — these aren't a real
# "storm" of model misbehavior, they're the guards working as designed.
# Filter them out of detect_tool_error_storms.
_GUARD_ERROR_PHRASES = (
    "you already called",
    "you already tried this URL",
    "your own memory database",
    "STOP. You have now called",
    "Re-emit the tool_call",        # missing-arg dispatcher message (actual wording)
    "Re-emit your tool_call",       # legacy phrasing — kept for backward compat
    "missing required argument",    # generic missing-arg dispatcher errors (Qwen schema mismatches)
    "you've run 4 grep/find",       # search-loop nudge (legacy phrasing)
    "grep/find commands in this response",
    "[cached read_file]",            # read_file dedup signal
    "you've made",                   # fuzzy-recall nudge ("you've made N recall() calls...")
    "Your tag schema assumption",    # ditto
)


def detect_tool_error_storms() -> list[dict]:
    """Conversations with >= 5 tool [error] results in 5 minutes.

    Excludes guard-issued errors (already-called dedup, blocked-URL retries,
    sqlite3-redirect) since those are the guards working correctly, not
    Charles malfunctioning.
    """
    cutoff = _ago_iso(ERROR_STORM_WINDOW_SECONDS)
    incidents: list[dict] = []
    with _ro_conn() as c:
        rows = c.execute(
            "SELECT conversation_id, content FROM conversations "
            "WHERE role='tool' AND created_at >= ?",
            (cutoff,),
        ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    samples: dict[str, str] = {}
    for r in rows:
        content = (r["content"] or "").strip()
        if not _TOOL_ERROR_RE.match(content):
            continue
        # Skip guard-issued errors — those are by design
        if any(phrase in content[:300] for phrase in _GUARD_ERROR_PHRASES):
            continue
        cid = r["conversation_id"]
        counts[cid] += 1
        if cid not in samples:
            samples[cid] = content[:160]
    for cid, n in counts.items():
        if n < ERROR_STORM_MIN:
            continue
        if any(cid.startswith(p) for p in CONV_PREFIX_SKIP):
            continue
        if cid in ERROR_STORM_SKIP_CONV_IDS:
            continue
        incidents.append({
            "kind": "tool_error_storm",
            "conv_id": cid,
            "count": n,
            "sample": samples.get(cid, ""),
        })
    return incidents


def detect_system_health() -> list[dict]:
    """Crises that need John's eyes (or hard remediation): MLX down, .env missing,
    disk free below threshold, conversation table size blowing up.
    """
    incidents: list[dict] = []

    # .env present
    if not ENV_PATH.exists():
        incidents.append({"kind": "env_missing", "path": str(ENV_PATH)})

    # disk free
    try:
        usage = shutil.disk_usage(str(WORKSPACE))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < DISK_FREE_MIN_GB:
            incidents.append({"kind": "disk_low", "free_gb": round(free_gb, 2)})
    except Exception as e:  # noqa: BLE001
        log.warning("disk_usage failed: %s", e)

    # MLX server reachable (only if agent is loaded — pointless to flag if Charles is down)
    if _agent_loaded():
        try:
            import urllib.request
            with urllib.request.urlopen(MLX_BASE_URL.replace("/v1", "/health"), timeout=3) as resp:
                if resp.status >= 500:
                    incidents.append({"kind": "mlx_unreachable", "status": resp.status})
        except Exception:  # noqa: BLE001
            # Try base URL as a fallback ping
            try:
                import urllib.request
                with urllib.request.urlopen(MLX_BASE_URL, timeout=3):
                    pass
            except Exception as e:  # noqa: BLE001
                incidents.append({"kind": "mlx_unreachable", "error": str(e)[:120]})

    # Conversation table approaching ceiling (warn at 80%)
    try:
        with _ro_conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        if n >= int(CONV_TABLE_HARD_CEILING * 0.8):
            incidents.append({"kind": "conv_table_pressure", "count": n})
    except Exception as e:  # noqa: BLE001
        log.warning("conv count failed: %s", e)

    return incidents


def detect_hung_respond() -> list[dict]:
    """Agent pid is alive but log file hasn't moved in HEARTBEAT_LOG_STALE_SECONDS."""
    incidents: list[dict] = []
    pid = _charles_pid()
    if not pid:
        return incidents  # not running — different problem (or intentional)
    if not AGENT_LOG.exists():
        return incidents
    age = time.time() - AGENT_LOG.stat().st_mtime
    if age >= HEARTBEAT_LOG_STALE_SECONDS:
        incidents.append({
            "kind": "hung_respond",
            "pid": pid,
            "log_stale_seconds": int(age),
        })
    return incidents


# ---------------------------------------------------------------------------
# Process / launchctl helpers
# ---------------------------------------------------------------------------

def _agent_loaded() -> bool:
    """True if com.charles.agent is loaded in launchctl (ignores enabled state)."""
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=5,
        )
        return any(CHARLES_LABEL in line for line in out.splitlines())
    except Exception:  # noqa: BLE001
        return False


def _charles_pid() -> int | None:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "python.*charles.py"], text=True, timeout=5,
        ).strip()
        if out:
            return int(out.split("\n")[0])
    except subprocess.CalledProcessError:
        return None
    except Exception:  # noqa: BLE001
        return None
    return None


def _kickstart_charles() -> bool:
    try:
        uid = os.getuid()
        # 45s timeout: kickstart -k blocks until the new instance is up,
        # and Charles's startup imports MLX/telegram/etc which can take
        # 20-30s on a cold disk cache. 15s was too tight and false-failed.
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{CHARLES_LABEL}"],
            check=True, capture_output=True, timeout=45,
        )
        log.warning("kickstarted %s", CHARLES_LABEL)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("kickstart failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Intervention
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """Two-faced intervention record: technical for logs, friendly for John.

    `technical` ends up in long_term_facts (audit) + watchdog log — preserves
    every detail a debugger would want. `friendly` is the iMessage to John
    in plain English, no jargon.
    """
    technical: str
    friendly: str


def _audit_fact(text: str, tags: str) -> None:
    """Log the action to long_term_facts for audit. Never raises."""
    try:
        memory_mod.add_fact(text, tags=tags)
    except Exception as e:  # noqa: BLE001
        log.warning("audit fact failed: %s", e)


def _conv_friendly(conv_id: str) -> str:
    """Translate a conv_id into something John recognises."""
    if conv_id == "8455750177":
        return "your Telegram chat"
    if conv_id.startswith("goal:"):
        try:
            gid = int(conv_id.split(":", 1)[1])
            g = goals_mod.get_goal(gid)
            if g:
                desc = (g.get("description") or "").strip()
                if desc:
                    return f"his work on goal '{desc[:60]}'"
            return f"his work on goal #{gid}"
        except Exception:  # noqa: BLE001
            return f"his work on goal {conv_id.split(':', 1)[1]}"
    if conv_id.startswith("heartbeat:"):
        return "a scheduled task"
    if conv_id.startswith("warroom-"):
        return "your War Room session"
    return "one of his conversations"


def _humanize_seconds(s: int) -> str:
    if s < 90:
        return f"{s} seconds"
    if s < 3600:
        return f"{s // 60} minutes"
    if s < 86400:
        h = s // 3600
        return "1 hour" if h == 1 else f"{h} hours"
    d = s // 86400
    return "1 day" if d == 1 else f"{d} days"


def intervene_response_loop(incident: dict) -> Action | None:
    """Trim a real loop — but ONLY if memory.trim_repeating_replies confirms it.

    Earlier versions fell through to memory.reset_conversation when the trim
    found nothing. That was destructive: if the watchdog's loose detector
    flagged a conv but the strict trim disagreed, we'd nuke the entire conv
    history. Lost 199 turns of John's Telegram conv this way once. Never
    again — disagreement means "no action, log warning."
    """
    conv_id = incident["conv_id"]
    try:
        deleted = memory_mod.trim_repeating_replies(conv_id)
    except Exception as e:  # noqa: BLE001
        log.exception("trim_repeating_replies failed for %s: %s", conv_id, e)
        return None
    if not deleted:
        # Detector and strict trim disagreed — likely a borderline case.
        # Skip the intervention. Log only ONCE per conv per 10 min so the
        # 30-min sliding-window detector doesn't spam the log every tick
        # while borderline rows age out.
        # Uses an in-memory cooldown dict (not the state file) because
        # tick() races us on state saves — its later _save_state overwrites
        # whatever we wrote here. Process-local is fine; on restart we
        # log once per conv then go quiet again.
        last_skip = _LOOP_SKIP_COOLDOWNS.get(conv_id, 0.0)
        now = time.time()
        if now - last_skip >= 600:
            log.warning(
                "response_loop incident on conv=%s but trim_repeating_replies "
                "found nothing to trim — skipping intervention. Sample: %r",
                conv_id, (incident.get("sample") or "")[:120],
            )
            _LOOP_SKIP_COOLDOWNS[conv_id] = now
        return None

    where = _conv_friendly(conv_id)
    technical = f"loop in conv={conv_id}: trim_repeating_replies removed {deleted} turn(s)"
    friendly = (
        f"Caught Charles loopin' again, boss — sayin' the same shit over and "
        f"over in {where}. Slapped him out of it. Back to normal."
    )
    _audit_fact(
        f"Behavioral watchdog intervention: {technical}. Sample of looped text: "
        f"{incident.get('sample', '')[:200]}",
        tags="intervention,auto,response_loop,trim_repeating_replies",
    )
    log.warning(technical)
    return Action(technical=technical, friendly=friendly)


def intervene_narration_stall(incident: dict) -> Action | None:
    gid = incident["goal_id"]
    desc = incident.get("description") or f"goal #{gid}"
    state = _load_state()
    seen = state.setdefault("narration_stalls", {})
    seen_count = int(seen.get(str(gid), 0)) + 1
    seen[str(gid)] = seen_count
    _save_state(state)

    if seen_count == 1:
        # First hit — inject a directive note so heartbeat sees it next tick.
        try:
            goals_mod.append_note(
                gid,
                f"{GUARD_NOTICE_MARKER} BEHAVIORAL WATCHDOG: narration stall "
                f"detected ({incident['hits']}/6 'let me X' notes without action). "
                f"Next tick MUST execute a real tool call (read_file, write_file, "
                f"exec_shell, etc.) or call cancel_goal. No more 'let me' / 'I'll' "
                f"phrases — past-tense action verbs only.",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("append_note failed for goal %d: %s", gid, e)
            return None
        technical = f"goal #{gid} narration stall: directive injected (1st hit)"
        friendly = (
            f"Charles is jawin' about the work instead of doin' it on '{desc}'. "
            f"Told him to either move on it or kill the goal. Watchin' him close."
        )
    else:
        # Repeat offender — cancel the goal.
        try:
            ok = goals_mod.cancel(gid)
        except Exception as e:  # noqa: BLE001
            log.exception("cancel goal %d failed: %s", gid, e)
            return None
        if not ok:
            return None
        technical = f"goal #{gid} narration stall: CANCELLED ({seen_count}x stall)"
        friendly = (
            f"Charles wouldn't shut up and start workin' on '{desc}' "
            f"even after I warned him once. Killed the goal. Re-add with "
            f"tighter scope if you still want it done."
        )
        try:
            memory_mod.add_task(
                title=f"Re-plan cancelled goal #{gid}",
                description=(
                    f"Watchdog cancelled goal #{gid} ({desc}) "
                    f"after {seen_count} narration-stall sweeps. Decide whether "
                    f"to recreate it with tighter scope or drop it."
                ),
                urgency="normal",
                source="watchdog",
            )
        except Exception:  # noqa: BLE001
            pass
        seen.pop(str(gid), None)
        _save_state(state)

    _audit_fact(
        f"Behavioral watchdog intervention: {technical}. Goal: {desc}",
        tags="intervention,auto,narration_stall",
    )
    log.warning(technical)
    return Action(technical=technical, friendly=friendly)


def intervene_hallucination(incident: dict) -> Action | None:
    gid = incident["goal_id"]
    term = incident["term"]
    desc = incident.get("description") or f"goal #{gid}"
    g = goals_mod.get_goal(gid)
    if not g or g.get("status") != "active":
        return None
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute(
                "UPDATE goals SET notes = ? WHERE id = ?",
                (
                    f"[{_now_iso()[:19]}] {GUARD_NOTICE_MARKER} Notes wiped by "
                    f"behavioral watchdog: contained hallucinated term {term!r}. "
                    f"Restart with a real read_file of the source.",
                    gid,
                ),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("wipe goal %d notes failed: %s", gid, e)
        return None
    technical = f"goal #{gid} hallucination ({term!r}): notes wiped"
    friendly = (
        f"Charles is just makin' shit up — pulled \"{term}\" out of his ass "
        f"while workin' on '{desc}'. Wiped his notes. He's startin' from real "
        f"sources next tick or I'm pullin' the goal."
    )
    _audit_fact(
        f"Behavioral watchdog intervention: {technical}. Goal: {desc}",
        tags="intervention,auto,hallucination",
    )
    log.warning(technical)
    return Action(technical=technical, friendly=friendly)


def intervene_goal_idleness(incident: dict) -> Action | None:
    gid = incident["goal_id"]
    desc = incident.get("description") or f"goal #{gid}"
    state = _load_state()
    seen = state.setdefault("idle_alerts", {})
    last_alert = float(seen.get(str(gid), 0))
    now = time.time()
    if now - last_alert < IMSG_COOLDOWN_SECONDS * 4:
        return None
    seen[str(gid)] = now
    _save_state(state)
    try:
        memory_mod.add_task(
            title=f"Goal #{gid} idle: {desc}",
            description=(
                f"No advance for {incident['idle_seconds']} sec. "
                f"Decide whether to nudge it, cancel, or re-scope."
            ),
            urgency="low",
            source="watchdog",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("add_task for idle goal failed: %s", e)
        return None
    idle_human = _humanize_seconds(int(incident["idle_seconds"]))
    technical = f"goal #{gid} idle {incident['idle_seconds']}s: surfaced as task"
    friendly = (
        f"Goal '{desc}' has been sittin' for {idle_human} with nobody touchin' it. "
        f"Pinned it to your Tasks tab — figure out if you wanna nudge it, "
        f"kill it, or shrink the scope."
    )
    _audit_fact(
        f"Behavioral watchdog: surfaced goal idleness as task. {technical}",
        tags="intervention,auto,goal_idle",
    )
    log.info(technical)
    return Action(technical=technical, friendly=friendly)


def intervene_tool_error_storm(incident: dict) -> Action | None:
    conv_id = incident["conv_id"]
    try:
        deleted = memory_mod.trim_repeating_replies(
            conv_id, n_check=3, threshold=0.5,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("trim during error storm failed: %s", e)
        return None
    where = _conv_friendly(conv_id)
    technical = f"tool-error storm in conv={conv_id} (n={incident['count']}): trimmed {deleted}"
    friendly = (
        f"Charles keeps swingin' at the same tool wrong over and over in "
        f"{where}. Wiped his bad attempts so he can swing fresh."
    )
    _audit_fact(
        f"Behavioral watchdog intervention: {technical}. Sample: {incident.get('sample', '')[:200]}",
        tags="intervention,auto,tool_error_storm",
    )
    log.warning(technical)
    return Action(technical=technical, friendly=friendly)


def intervene_hung_respond(incident: dict) -> Action | None:
    """Heartbeat log frozen while pid alive: the agent is wedged. Kickstart."""
    state = _load_state()
    last_kick = float(state.get("last_kickstart", 0))
    now = time.time()
    if now - last_kick < KICKSTART_COOLDOWN_SECONDS:
        return None
    if not _kickstart_charles():
        return None
    state["last_kickstart"] = now
    _save_state(state)
    stale_human = _humanize_seconds(int(incident['log_stale_seconds']))
    technical = f"hung respond() detected (log stale {incident['log_stale_seconds']}s) — kickstarted"
    friendly = (
        f"Charles froze up, boss — ain't moved in {stale_human}. Kicked him. "
        f"Should be back to work now."
    )
    _audit_fact(
        f"Behavioral watchdog intervention: {technical}. Pid was {incident['pid']}.",
        tags="intervention,auto,hung_respond,kickstart",
    )
    log.warning(technical)
    return Action(technical=technical, friendly=friendly)


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def prune_conv_history() -> int:
    """Cap each conversation_id at CONV_HISTORY_CAP_PER_ID rows (oldest dropped)."""
    deleted_total = 0
    with sqlite3.connect(str(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        cids = [
            r[0] for r in c.execute(
                "SELECT conversation_id FROM conversations "
                "GROUP BY conversation_id HAVING COUNT(*) > ?",
                (CONV_HISTORY_CAP_PER_ID,),
            ).fetchall()
        ]
        for cid in cids:
            row = c.execute(
                "SELECT id FROM conversations WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT 1 OFFSET ?",
                (cid, CONV_HISTORY_CAP_PER_ID),
            ).fetchone()
            if not row:
                continue
            cur = c.execute(
                "DELETE FROM conversations WHERE conversation_id=? AND id <= ?",
                (cid, row["id"]),
            )
            deleted_total += cur.rowcount
        c.commit()
    if deleted_total:
        _audit_fact(
            f"Watchdog prune: trimmed {deleted_total} old turn(s) across "
            f"{len(cids)} conv_id(s) (cap {CONV_HISTORY_CAP_PER_ID}/conv).",
            tags="prune,auto,conv_history",
        )
        log.info("pruned %d conv rows from %d conv_ids", deleted_total, len(cids))
    return deleted_total


def prune_daily_log() -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=DAILY_LOG_RETENTION_DAYS)
    ).strftime("%Y-%m-%d")
    with sqlite3.connect(str(DB_PATH)) as c:
        cur = c.execute(
            "DELETE FROM daily_log WHERE substr(created_at, 1, 10) < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        c.commit()
    if deleted:
        _audit_fact(
            f"Watchdog prune: dropped {deleted} daily_log row(s) older than "
            f"{DAILY_LOG_RETENTION_DAYS} days.",
            tags="prune,auto,daily_log",
        )
        log.info("pruned %d daily_log rows older than %d days", deleted, DAILY_LOG_RETENTION_DAYS)
    return deleted


def prune_goal_notes() -> int:
    """Cap notes per active goal at GOAL_NOTES_LINE_CAP. Wipe notes for cancelled/done."""
    pruned = 0
    with sqlite3.connect(str(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, status, notes FROM goals").fetchall()
        for r in rows:
            notes = r["notes"] or ""
            if not notes:
                continue
            if r["status"] in ("cancelled", "done"):
                if notes.strip():
                    c.execute("UPDATE goals SET notes='' WHERE id=?", (r["id"],))
                    pruned += 1
                continue
            lines = [ln for ln in notes.split("\n") if ln.strip()]
            if len(lines) > GOAL_NOTES_LINE_CAP:
                kept = "\n".join(lines[-GOAL_NOTES_LINE_CAP:])
                c.execute("UPDATE goals SET notes=? WHERE id=?", (kept, r["id"]))
                pruned += 1
        c.commit()
    if pruned:
        _audit_fact(
            f"Watchdog prune: trimmed notes on {pruned} goal(s) "
            f"(cap {GOAL_NOTES_LINE_CAP}/goal; wiped on cancelled/done).",
            tags="prune,auto,goal_notes",
        )
        log.info("pruned notes on %d goals", pruned)
    return pruned


def prune_self_modify_backups() -> int:
    if not SELF_MODIFY_BACKUPS.exists():
        return 0
    cutoff = time.time() - SELF_MODIFY_BACKUP_RETENTION_DAYS * 86400
    deleted = 0
    for p in SELF_MODIFY_BACKUPS.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                deleted += 1
        except Exception as e:  # noqa: BLE001
            log.warning("backup prune failed for %s: %s", p, e)
    if deleted:
        _audit_fact(
            f"Watchdog prune: dropped {deleted} self_modify_backups item(s) older "
            f"than {SELF_MODIFY_BACKUP_RETENTION_DAYS} days.",
            tags="prune,auto,self_modify_backups",
        )
        log.info("pruned %d self_modify backups", deleted)
    return deleted


def prune_audio_tmp() -> int:
    if not AUDIO_TMP.exists():
        return 0
    cutoff = time.time() - AUDIO_TMP_RETENTION_HOURS * 3600
    deleted = 0
    for p in AUDIO_TMP.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception as e:  # noqa: BLE001
            log.warning("audio tmp prune failed for %s: %s", p, e)
    if deleted:
        log.info("pruned %d audio tmp files", deleted)
    return deleted


def enforce_conv_table_ceiling() -> int:
    """If conversations table > hard ceiling, drop oldest to bring under."""
    with sqlite3.connect(str(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        n = c.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        if n <= CONV_TABLE_HARD_CEILING:
            return 0
        excess = n - CONV_TABLE_HARD_CEILING
        # Find the cutoff id (delete rows with id <= cutoff)
        row = c.execute(
            "SELECT id FROM conversations ORDER BY id ASC LIMIT 1 OFFSET ?",
            (excess - 1,),
        ).fetchone()
        if not row:
            return 0
        cur = c.execute("DELETE FROM conversations WHERE id <= ?", (row["id"],))
        deleted = cur.rowcount
        c.commit()
    if deleted:
        _audit_fact(
            f"Watchdog prune (HARD CEILING): conversations was {n} rows, "
            f"dropped {deleted} oldest to enforce {CONV_TABLE_HARD_CEILING}.",
            tags="prune,auto,conv_ceiling",
        )
        log.warning("HARD CEILING enforced: dropped %d oldest conv rows", deleted)
    return deleted


def maybe_consolidate_facts() -> None:
    """Trigger consolidate_memory if long_term_facts > threshold. The consolidator
    itself is a heavy operation — only run if the table genuinely needs it."""
    try:
        with _ro_conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM long_term_facts WHERE tags NOT LIKE '%superseded%'"
            ).fetchone()["n"]
        if n < LONG_TERM_FACTS_CONSOLIDATE_THRESHOLD:
            return
        # Lazy import — pulls in core.tools registration which may be heavy
        from tools.memory_consolidation import consolidate_memory
        result = consolidate_memory(window_hours=24, dry_run=False)
        log.info("auto-consolidation: %s", result.replace("\n", " | ")[:300])
    except Exception as e:  # noqa: BLE001
        log.warning("auto-consolidation failed: %s", e)


def run_full_prune() -> dict:
    """Run all pruners. Returns counts per category."""
    return {
        "conv_history": prune_conv_history(),
        "conv_ceiling": enforce_conv_table_ceiling(),
        "daily_log": prune_daily_log(),
        "goal_notes": prune_goal_notes(),
        "self_modify_backups": prune_self_modify_backups(),
        "audio_tmp": prune_audio_tmp(),
    }


# ---------------------------------------------------------------------------
# iMessage to John (rate-limited, categorized)
# ---------------------------------------------------------------------------

def _alert_john(category: str, message: str) -> bool:
    """Send iMessage to John at most once per category per IMSG_COOLDOWN_SECONDS.

    Categories are internal (routing/cooldown only). The message itself is
    always plain English — never echoed to the user with the category tag.
    """
    state = _load_state()
    cooldowns = state.setdefault("imsg_cooldowns", {})
    now = time.time()
    last = float(cooldowns.get(category, 0))
    if now - last < IMSG_COOLDOWN_SECONDS:
        log.info("imsg suppressed (cooldown) [%s]: %s", category, message[:100])
        return False
    cooldowns[category] = now
    _save_state(state)

    msg_full = f"Charles watchdog: {message}"
    msg_esc = msg_full.replace("\\", "\\\\").replace('"', '\\"')
    target_esc = JOHN_IMESSAGE.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '    set targetService to 1st service whose service type = iMessage\n'
        f'    set targetBuddy to buddy "{target_esc}" of targetService\n'
        f'    send "{msg_esc}" to targetBuddy\n'
        'end tell'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15, check=True,
        )
        log.warning("alerted John [%s]: %s", category, message[:100])
        return True
    except Exception as e:  # noqa: BLE001
        log.error("imsg failed [%s]: %s", category, e)
        return False


# ---------------------------------------------------------------------------
# Boss Hog inbound iMessage — John dictates, Boss Hog acks in voice
#
# John types iMessages to the Mac's number. Boss Hog polls chat.db each tick
# for unread messages from John, sends a one-line acknowledgment in voice,
# and logs both sides into CHARLES_LOG so it shows up in the Mac UI's
# Activity tab.
#
# Phase 1 (current): ack-only. No command parsing, no agent invocation.
# Phase 2 (future): parse "kill goal #5", "what's status?", route to actions.
# ---------------------------------------------------------------------------

_BOSS_HOG_ACKS = (
    "10-4, boss. Got it.",
    "Heard ya, boss. On the case.",
    "Copy that. Workin' on it.",
    "Roger. Won't drop the ball.",
    "Aight, boss. Noted.",
    "Loud and clear, boss.",
    "Got it. Won't forget.",
    "Heard. Movin' on it.",
)


def _read_new_imessages_from_john(last_rowid: int) -> list[tuple[int, str]]:
    """Query chat.db for new iMessages from John since last_rowid.
    Returns [(rowid, text), ...] in ascending rowid order."""
    handles = "'+16156637932','16156637932','+1 6156637932'"
    sql = (
        "SELECT m.ROWID, COALESCE(m.text, '') "
        "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
        f"WHERE h.id IN ({handles}) AND m.is_from_me = 0 "
        f"AND m.ROWID > {last_rowid} "
        f"ORDER BY m.ROWID ASC LIMIT 20;"
    )
    cmd = f"sqlite3 -separator '|' ~/Library/Messages/chat.db {shlex.quote(sql)}"
    osa = f"do shell script {json.dumps(cmd)}"
    try:
        r = subprocess.run(
            ["osascript", "-e", osa],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return []
        out = r.stdout.strip()
    except Exception:  # noqa: BLE001
        return []

    msgs: list[tuple[int, str]] = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        try:
            rowid = int(parts[0])
        except ValueError:
            continue
        text = parts[1].strip()
        if not text:
            continue
        # Skip our own outbound that bounced back via attribution quirks
        if text.startswith(("Boss Hog:", "Charles watchdog:")):
            continue
        msgs.append((rowid, text))
    return msgs


def _seed_imsg_high_water_mark() -> int:
    """First-run only: read the current max iMessage rowid from John so we
    don't ack 1000 historical messages on first poll."""
    handles = "'+16156637932','16156637932','+1 6156637932'"
    sql = (
        "SELECT COALESCE(MAX(m.ROWID), 0) FROM message m "
        "JOIN handle h ON m.handle_id = h.ROWID "
        f"WHERE h.id IN ({handles}) AND m.is_from_me = 0;"
    )
    cmd = f"sqlite3 ~/Library/Messages/chat.db {shlex.quote(sql)}"
    osa = f"do shell script {json.dumps(cmd)}"
    try:
        r = subprocess.run(
            ["osascript", "-e", osa],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return int(r.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _send_boss_hog_ack(text: str) -> bool:
    """Send a one-line ack to John as Boss Hog."""
    target_esc = JOHN_IMESSAGE.replace("\\", "\\\\").replace('"', '\\"')
    full = f"Boss Hog: {text}"
    msg_esc = full.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '    set targetService to 1st service whose service type = iMessage\n'
        f'    set targetBuddy to buddy "{target_esc}" of targetService\n'
        f'    send "{msg_esc}" to targetBuddy\n'
        'end tell'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Boss Hog ack send failed: %s", e)
        return False


_JOHN_INBOX_PATH = WORKSPACE / "john_inbox.md"


def _append_to_john_inbox(text: str) -> None:
    """Append John's iMessage to the inbox file Claude Code reads at session
    start. Append-only so historical context survives. The next Claude Code
    session can `tail -100 ~/charles/workspace/john_inbox.md` to catch up
    on anything John said while AFK.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        line = f"[{ts}] {text.replace(chr(10), ' ')}\n"
        with open(_JOHN_INBOX_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:  # noqa: BLE001
        log.warning("john_inbox write failed: %s", e)


def _log_boss_hog_exchange(john_text: str, ack_text: str) -> None:
    """Log the inbound iMessage + ack into CHARLES_LOG so the UI shows it,
    AND append the John message to john_inbox.md for the next Claude Code
    session to read on startup."""
    _append_to_john_inbox(john_text)
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute(
                "INSERT INTO conversations (conversation_id, role, content, created_at) "
                "VALUES ('charles_log', 'user', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (f"[John → Boss Hog (iMessage)]: {john_text}",),
            )
            c.execute(
                "INSERT INTO conversations (conversation_id, role, content, created_at) "
                "VALUES ('charles_log', 'assistant', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (f"[Boss Hog → John (iMessage)]: {ack_text}",),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("imsg log to charles_log failed: %s", e)


def poll_john_imessages(state: dict) -> int:
    """Read new iMessages from John, ack each in Boss Hog voice, log to
    CHARLES_LOG. Returns the count of new messages processed.

    Mutates `state` in place — caller is responsible for `_save_state(state)`.
    This avoids a race where tick()'s outer save would overwrite our changes.

    First-run protection: seeds the high-water mark to current max rowid so
    Boss Hog doesn't ack the entire iMessage history on first poll.
    """
    last_seen = state.get("last_imsg_rowid_seen")

    if last_seen is None:
        # First run — seed and skip processing
        seeded = _seed_imsg_high_water_mark()
        state["last_imsg_rowid_seen"] = seeded
        log.info("Boss Hog imsg poll seeded high-water mark at rowid=%d", seeded)
        return 0

    new = _read_new_imessages_from_john(int(last_seen))
    if not new:
        return 0

    # Cap acks per tick — if John sends 10 messages in a burst, ack the
    # first three normally and a single summary for the rest. Avoids spam.
    max_acks = 3
    processed = 0
    last_rowid = int(last_seen)
    for rowid, text in new[:max_acks]:
        ack = random.choice(_BOSS_HOG_ACKS)
        if _send_boss_hog_ack(ack):
            _log_boss_hog_exchange(text, ack)
        last_rowid = max(last_rowid, rowid)
        processed += 1

    if len(new) > max_acks:
        rest = len(new) - max_acks
        summary_ack = f"Got the rest, boss — {rest} more queued up."
        if _send_boss_hog_ack(summary_ack):
            _log_boss_hog_exchange(
                f"({rest} more messages from John this tick)", summary_ack,
            )
        last_rowid = max(last_rowid, max(r for r, _ in new))

    state["last_imsg_rowid_seen"] = last_rowid
    return processed


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------

def detect_all() -> list[dict]:
    incidents: list[dict] = []
    for fn in (
        detect_response_loops,
        detect_narration_stalls,
        detect_hallucinations,
        detect_goal_idleness,
        detect_tool_error_storms,
        detect_system_health,
        detect_hung_respond,
    ):
        try:
            incidents.extend(fn())
        except Exception as e:  # noqa: BLE001
            log.exception("detector %s failed: %s", fn.__name__, e)
    return incidents


_INTERVENORS = {
    "response_loop": intervene_response_loop,
    "narration_stall": intervene_narration_stall,
    "hallucination": intervene_hallucination,
    "goal_idle": intervene_goal_idleness,
    "tool_error_storm": intervene_tool_error_storm,
    "hung_respond": intervene_hung_respond,
}


def tick() -> None:
    state = _load_state()

    # Boss Hog inbound iMessage poll — runs every tick (~30s).
    # Mutates `state` in place; the outer _save_state(state) at the bottom of
    # tick() persists the high-water-mark update.
    try:
        n = poll_john_imessages(state)
        if n:
            log.info("Boss Hog: acked %d iMessage(s) from John", n)
    except Exception as e:  # noqa: BLE001
        log.exception("Boss Hog imsg poll failed: %s", e)

    incidents = detect_all()
    behavioral_incidents = [i for i in incidents if i["kind"] in _INTERVENORS]
    system_incidents = [i for i in incidents if i["kind"] not in _INTERVENORS]

    # Behavioral remediation
    actions: list[Action] = []
    for inc in behavioral_incidents:
        fn = _INTERVENORS.get(inc["kind"])
        if fn is None:
            continue
        try:
            res = fn(inc)
            if res:
                actions.append(res)
        except Exception as e:  # noqa: BLE001
            log.exception("intervenor for %s failed: %s", inc["kind"], e)

    # System crises — alert John (rate-limited per category, Boss Hog voice)
    for inc in system_incidents:
        kind = inc["kind"]
        if kind == "env_missing":
            _alert_john(
                "system_crisis",
                "Charles can't get into the secrets file (.env's gone). He's "
                "locked out — Telegram, Gmail, Stripe, none of it. Need you "
                "to put it back, boss.",
            )
        elif kind == "disk_low":
            _alert_john(
                "system_crisis",
                f"Hard drive's tight, boss — only {inc['free_gb']} GB free. "
                f"Free up some room. Under a couple GB and shit'll start "
                f"breakin'.",
            )
        elif kind == "mlx_unreachable":
            _alert_john(
                "system_crisis",
                "Charles's brain ain't answerin'. MLX server's down. He's "
                "offline 'til you bring it back. On the Mac Studio: check "
                "MLX-LM is runnin' on port 8080.",
            )
        elif kind == "conv_table_pressure":
            _alert_john(
                "system_crisis",
                f"Chat history's gettin' fat, boss — {inc['count']:,} rows, "
                f"ceiling's {CONV_TABLE_HARD_CEILING:,}. I'm prunin' as we go "
                f"but figured you should know in case Charles is loopin'.",
            )

    # Consecutive-loop escalation: if intervenors ACTUALLY ACT N ticks in a
    # row (not just observe an incident), kickstart the agent.
    # Was: incremented on any behavioral_incidents — but if the intervenor
    # decided "no-op" (e.g. response_loop where trim found nothing to trim),
    # the incident gets re-detected next tick and the counter falsely climbs
    # to 3, triggering an unwanted kickstart. Fix: gate on `actions` so only
    # *attempted remediations* escalate.
    if actions and _agent_loaded():
        consec = int(state.get("consec_intervention_ticks", 0)) + 1
        state["consec_intervention_ticks"] = consec
        if consec >= KICKSTART_AFTER_N_LOOPS:
            now = time.time()
            last_kick = float(state.get("last_kickstart", 0))
            if now - last_kick >= KICKSTART_COOLDOWN_SECONDS:
                if _kickstart_charles():
                    state["last_kickstart"] = now
                    state["consec_intervention_ticks"] = 0
                    _audit_fact(
                        f"Behavioral watchdog escalation: kickstarted Charles after "
                        f"{consec} consecutive intervention ticks. Last actions: "
                        f"{'; '.join(a.technical for a in actions[-3:])}",
                        tags="intervention,auto,kickstart,escalation",
                    )
                    last_friendly = actions[-1].friendly if actions else "(no detail)"
                    _alert_john(
                        "kickstart",
                        f"Had to fix Charles {consec} times in a row, boss, so I "
                        f"rebooted the dumb bastard. Should be back to work. "
                        f"Last shit he was doin': {last_friendly}",
                    )
                else:
                    _alert_john(
                        "system_crisis",
                        f"Tried to kick Charles back online {consec} times — "
                        f"didn't take. Need you on this, boss. Open Terminal: "
                        f"'launchctl kickstart -k gui/$(id -u)/com.charles.agent'.",
                    )
    else:
        state["consec_intervention_ticks"] = 0

    # Periodic prune
    last_prune = float(state.get("last_prune", 0))
    now = time.time()
    if now - last_prune >= PRUNE_EVERY_SECONDS:
        try:
            counts = run_full_prune()
            log.info("prune sweep: %s", counts)
            maybe_consolidate_facts()
        except Exception as e:  # noqa: BLE001
            log.exception("prune sweep failed: %s", e)
        state["last_prune"] = now

    _save_state(state)

    if actions:
        log.warning(
            "tick interventions (%d): %s",
            len(actions),
            " | ".join(a.technical for a in actions),
        )
    elif behavioral_incidents or system_incidents:
        log.info(
            "tick observed: behavioral=%d system=%d (no action)",
            len(behavioral_incidents), len(system_incidents),
        )


def main() -> None:
    log.info(
        "behavior_watchdog starting (check=%ds, prune=%ds, db=%s)",
        CHECK_SECONDS, PRUNE_EVERY_SECONDS, DB_PATH,
    )
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
