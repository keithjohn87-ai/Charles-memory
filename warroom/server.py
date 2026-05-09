"""War Room — FastAPI server exposing Charles state + commands.

Runs as a separate process from Charles. Reads memory.db directly for state
queries, calls into core.agent for reactive commands, and streams events
over WebSocket by polling the conversations table for new IDs.

Surface (all under /api):
  GET  /api/state/now                       summary for iPhone Now view
  GET  /api/state/conversations             list of distinct conv IDs
  GET  /api/state/conversations/{conv_id}   recent turns
  GET  /api/state/goals?status=active|all
  GET  /api/state/tasks?status=pending|all
  GET  /api/state/approvals
  GET  /api/state/activity?limit=N
  GET  /api/state/system
  GET  /api/state/tools
  POST /api/command/message                 {conv_id, text}
  POST /api/command/voice                   audio upload → transcribe → reply
  POST /api/command/approve                 {fact_id, response?}
  POST /api/command/deny                    {fact_id, reason?}
  POST /api/command/cancel-goal             {goal_id}
  POST /api/command/restart                 (no body)
  POST /api/push/register                   {token, platform, bundle_id}
  GET  /api/audio/{filename}                stream a generated audio reply
  WS   /ws/stream?sig=<signature>           live event stream

Auth: every request must carry header 'X-Charles-Signature' with HMAC-SHA256
of the request body using the shared secret. WS uses ?sig= query param. See
warroom/auth.py.

Run via: python -m warroom (defaults to 127.0.0.1:8765)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

# Make the charles repo importable when run as `python -m warroom`
if "/Users/home/charles" not in sys.path:
    sys.path.insert(0, "/Users/home/charles")

from warroom import auth, state

log = logging.getLogger("warroom.server")

# John's unified user-conversation id — same id Telegram uses, so context
# survives switching between UI and phone. If a UI client sends a stale or
# stress-test conv_id, we reroute it here so John always lands in one thread.
_OWNER_USER_CONV = "8455750177"
_REROUTE_PREFIXES = ("stress_", "smoketest", "wd_test_", "stop_test_")


def _normalize_user_conv_id(conv_id: str) -> str:
    """Reroute stale/test conv_ids to the owner's unified user thread.

    UI clients have historically gotten stuck pointing at leftover stress-test
    conv_ids (e.g., 'stress_stop_test'). Any conv_id matching a known test
    prefix gets routed to John's primary thread so context stays unified.
    Goal/heartbeat conv_ids (autonomous work) pass through unchanged.
    """
    if conv_id.startswith(_REROUTE_PREFIXES):
        return _OWNER_USER_CONV
    return conv_id


app = FastAPI(title="Charles War Room", version="0.1.0")


# ---- Auth middleware --------------------------------------------------------

@app.middleware("http")
async def hmac_auth(request: Request, call_next):
    # Allow no-auth on:
    #   /health          — connectivity check
    #   /preview*        — browser preview pages (Tailscale-only attack surface)
    #   /api/preview/*   — preview-supporting endpoints
    p = request.url.path
    if p == "/health" or p.startswith("/preview") or p.startswith("/api/preview/"):
        return await call_next(request)
    body = await request.body()
    sig = request.headers.get("X-Charles-Signature", "")
    if not auth.verify(body, sig):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    # FastAPI consumes body once; rebuild Receive so handlers can re-read it
    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request._receive = _receive  # type: ignore[attr-defined]
    return await call_next(request)


@app.on_event("startup")
async def _startup() -> None:
    # Background push poller — fans out APNs notifications when approval-pending
    # facts land. No-ops if APNs isn't configured yet.
    from warroom import push
    asyncio.create_task(push.poll_and_push())
    log.info("push poller scheduled")


@app.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}


# ---- Browser preview (no auth — Tailscale-only access) ----------------------

@app.get("/preview")
async def preview_root():
    """Serve the browser preview of the War Room iOS app — hits /api/preview/*
    endpoints to render real Charles state in mock-iPhone chrome.
    """
    from fastapi.responses import HTMLResponse
    html = (Path(__file__).parent / "preview.html").read_text()
    return HTMLResponse(content=html)


@app.get("/api/preview/now")
async def preview_now():
    return state.now_summary()


@app.get("/api/preview/approvals")
async def preview_approvals():
    return state.pending_approvals()


@app.get("/api/preview/activity")
async def preview_activity(limit: int = 8):
    return state.activity_feed(limit=limit)


@app.get("/api/preview/goals")
async def preview_goals():
    return state.goals_state(status="active")


# ---- State endpoints --------------------------------------------------------

@app.get("/api/state/now")
async def state_now():
    return state.now_summary()


@app.get("/api/state/conversations")
async def state_conversations(limit: int = 30):
    return state.conversations_index(limit=limit)


@app.get("/api/state/conversations/{conv_id}")
async def state_conversation(conv_id: str, limit: int = 50):
    # Same normalization as POST /api/command/message — if the UI is still
    # pointing at a stale stress-test conv_id, show it the unified thread.
    return state.conversation_history(_normalize_user_conv_id(conv_id), limit=limit)


@app.get("/api/state/goals")
async def state_goals(status: str = "active"):
    return state.goals_state(status=status)


@app.get("/api/state/tasks")
async def state_tasks(status: str = "pending"):
    return state.tasks_state(status=status)


@app.get("/api/state/approvals")
async def state_approvals():
    return state.pending_approvals()


# ---- Unified Tasks tab (approvals + Charles-created tasks + open requests) --

@app.get("/api/state/tasks-unified")
async def state_tasks_unified():
    """One view of everything that needs John's attention.

    Pulls from 3 sources:
      - long_term_facts tagged 'approval-pending' (Tier-2 governance gates)
      - tasks table (Charles-created or auto-extracted from his replies)
      - long_term_facts tagged 'open_request' (time-tracked follow-ups)

    Sorted by urgency (blocking > high > normal > low) then recency.
    """
    from core import memory as _mem
    import sqlite3
    out: list[dict] = []
    # Approvals → unified shape
    for a in state.pending_approvals():
        out.append({
            "id": f"approval:{a['id']}",
            "kind": "approval",
            "title": (a["fact"][:100] + "…") if len(a["fact"]) > 100 else a["fact"],
            "description": a["fact"],
            "urgency": "high",
            "source": "governance",
            "source_conv": None,
            "created_at": a["created_at"],
            "raw_id": a["id"],
        })
    # Charles-created/auto-extracted tasks
    for t in _mem.list_tasks(status="open", limit=100):
        out.append({
            "id": f"task:{t['id']}",
            "kind": "task",
            "title": t["title"],
            "description": t["description"],
            "urgency": t["urgency"],
            "source": t["source"],
            "source_conv": t["source_conv"],
            "created_at": t["created_at"],
            "raw_id": t["id"],
        })
    # Open requests (waiting-for-John follow-ups)
    con = sqlite3.connect("/Users/home/charles/workspace/memory.db")
    try:
        rows = con.execute(
            "SELECT id, fact, tags, created_at FROM long_term_facts "
            "WHERE tags LIKE '%open_request%' AND tags NOT LIKE '%resolved%' "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        con.close()
    for r in rows:
        fact = r[1]
        out.append({
            "id": f"open_request:{r[0]}",
            "kind": "open_request",
            "title": (fact[:100] + "…") if len(fact) > 100 else fact,
            "description": fact,
            "urgency": "normal",
            "source": "open_request",
            "source_conv": None,
            "created_at": r[3],
            "raw_id": r[0],
        })
    urg_rank = {"blocking": 0, "high": 1, "normal": 2, "low": 3}
    out.sort(key=lambda x: (urg_rank.get(x["urgency"], 9), -int("".join(c for c in x["created_at"] if c.isdigit())[:14] or "0")))
    return out


@app.post("/api/tasks/add")
async def tasks_add(req: Request):
    """John adds his own task via the UI."""
    body: dict[str, Any] = await req.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    description = body.get("description", "")
    urgency = body.get("urgency", "normal")
    from core import memory as _mem
    tid = _mem.add_task(title=title, description=description, urgency=urgency, source="john")
    return {"ok": True, "id": tid}


@app.post("/api/tasks/complete")
async def tasks_complete(req: Request):
    body: dict[str, Any] = await req.json()
    tid = body.get("id") or body.get("task_id")
    if not tid:
        raise HTTPException(400, "id required")
    from core import memory as _mem
    ok = _mem.complete_task(int(tid), note=body.get("note", ""))
    return {"ok": ok, "id": tid}


@app.post("/api/tasks/dismiss")
async def tasks_dismiss(req: Request):
    body: dict[str, Any] = await req.json()
    tid = body.get("id") or body.get("task_id")
    if not tid:
        raise HTTPException(400, "id required")
    from core import memory as _mem
    ok = _mem.dismiss_task(int(tid), reason=body.get("reason", ""))
    return {"ok": ok, "id": tid}


@app.get("/api/state/activity")
async def state_activity(limit: int = 50):
    return state.activity_feed(limit=limit)


@app.get("/api/state/system")
async def state_system():
    return state.system_stats()


@app.get("/api/state/tools")
async def state_tools():
    return state.tool_registry()


# ---- Command endpoints ------------------------------------------------------

@app.post("/api/command/message")
async def cmd_message(req: Request):
    body: dict[str, Any] = await req.json()
    conv_id = body.get("conv_id") or body.get("conversation_id")
    text = body.get("text")
    if not conv_id or not text:
        raise HTTPException(400, "conv_id and text required")
    normalized = _normalize_user_conv_id(str(conv_id))
    from core import agent
    reply = await asyncio.to_thread(agent.respond, text, normalized)
    return {"reply": reply, "conv_id": normalized}


# ---- Secrets channel — write to ~/charles/.env safely from the app --------
#
# Replaces the "John pastes a key into chat" anti-pattern. The app sends a
# {name, value} pair; the server appends/updates the .env file (gitignored,
# 0600 perms). The value is NEVER logged, never stored in the DB, never
# echoed back. List endpoint returns names only (no values).

_DOTENV_PATH = Path("/Users/home/charles/.env")


def _read_dotenv() -> dict[str, str]:
    if not _DOTENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in _DOTENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_dotenv(env: dict[str, str]) -> None:
    lines = []
    for k, v in env.items():
        # Quote if value has spaces or special chars
        if any(c in v for c in [' ', '#', '$']):
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")
    _DOTENV_PATH.write_text("\n".join(lines) + "\n")
    _DOTENV_PATH.chmod(0o600)


@app.get("/api/secrets/list")
async def secrets_list():
    """Returns names of secrets currently in ~/charles/.env. Values redacted."""
    env = _read_dotenv()
    return [
        {"name": k, "preview": (v[:4] + "…" + v[-4:] if len(v) > 12 else "***"), "length": len(v)}
        for k, v in sorted(env.items())
    ]


@app.post("/api/secrets/set")
async def secrets_set(req: Request):
    """Add or update a secret in ~/charles/.env.

    Body: {"name": "STRIPE_SECRET_KEY", "value": "sk_live_..."}
    Validates name is upper-snake, value is non-empty. Never logs the value.
    Charles needs a restart to pick up the change (env loaded at boot).
    """
    body: dict[str, Any] = await req.json()
    name = (body.get("name") or "").strip()
    value = body.get("value") or ""
    if not name or not value:
        raise HTTPException(400, "name and value required")
    if not all(c.isupper() or c.isdigit() or c == "_" for c in name) or name[0].isdigit():
        raise HTTPException(400, "name must be UPPER_SNAKE_CASE (letters/digits/underscores, no leading digit)")
    env = _read_dotenv()
    is_new = name not in env
    env[name] = value
    _write_dotenv(env)
    log.info("secret %s (name=%s, length=%d) — RESTART CHARLES to pick up",
             "added" if is_new else "updated", name, len(value))
    return {"ok": True, "name": name, "is_new": is_new, "restart_needed": True}


@app.post("/api/secrets/delete")
async def secrets_delete(req: Request):
    body: dict[str, Any] = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    env = _read_dotenv()
    existed = name in env
    if existed:
        del env[name]
        _write_dotenv(env)
        log.info("secret deleted (name=%s)", name)
    return {"ok": True, "deleted": existed, "restart_needed": existed}


@app.post("/api/command/stop")
async def cmd_stop(req: Request):
    """Cancel an in-flight agent.respond() for a conversation.

    Sets a stop event the agent loop checks between tool rounds. Mid-round
    LLM generation continues until done (can't kill MLX mid-token), but the
    NEXT round won't start. Effective stop time: 5-30 sec depending on what
    round Charles is in. Use when Charles spirals or you sent the wrong
    message. Body: {"conv_id": "..."}
    """
    body: dict[str, Any] = await req.json()
    conv_id = body.get("conv_id") or body.get("conversation_id")
    if not conv_id:
        raise HTTPException(400, "conv_id required")
    from core import agent as _agent
    fired = _agent.request_stop(str(conv_id))
    return {"ok": True, "conv_id": str(conv_id), "found_in_flight": fired}


@app.post("/api/command/reset-conversation")
async def cmd_reset_conversation(req: Request):
    """Wipe the recent rolling history for one conversation.

    UI's 'Reset Charles' button hits this. Use when Charles is pattern-locked
    or when starting a fresh topic. Long-term facts + goals are unaffected.
    Body: {"conv_id": "...", "keep_last_user_turn": true}  (default true)
    """
    body: dict[str, Any] = await req.json()
    conv_id = body.get("conv_id") or body.get("conversation_id")
    keep_last_user_turn = bool(body.get("keep_last_user_turn", True))
    if not conv_id:
        raise HTTPException(400, "conv_id required")
    from core import memory as _mem
    deleted = _mem.reset_conversation(str(conv_id), keep_last_user_turn=keep_last_user_turn)
    return {"ok": True, "conv_id": str(conv_id), "deleted": deleted}


@app.post("/api/command/approve")
async def cmd_approve(req: Request):
    body: dict[str, Any] = await req.json()
    fact_id = body.get("fact_id")
    response = body.get("response", "approved")
    if not fact_id:
        raise HTTPException(400, "fact_id required")
    return _resolve_approval(int(fact_id), f"approved: {response}")


@app.post("/api/command/deny")
async def cmd_deny(req: Request):
    body: dict[str, Any] = await req.json()
    fact_id = body.get("fact_id")
    reason = body.get("reason", "denied")
    if not fact_id:
        raise HTTPException(400, "fact_id required")
    return _resolve_approval(int(fact_id), f"denied: {reason}")


@app.post("/api/command/cancel-goal")
async def cmd_cancel_goal(req: Request):
    body: dict[str, Any] = await req.json()
    goal_id = body.get("goal_id")
    if goal_id is None:
        raise HTTPException(400, "goal_id required")
    from core import goals
    ok = goals.cancel(int(goal_id))
    if not ok:
        raise HTTPException(404, f"goal #{goal_id} not active")
    return {"ok": True, "goal_id": int(goal_id)}


# ---- Voice: audio upload → transcribe → reply → audio out ------------------

_AUDIO_TMP = Path("/tmp/warroom_audio")
_AUDIO_TMP.mkdir(parents=True, exist_ok=True)


@app.post("/api/command/voice")
async def cmd_voice(
    request: Request,
    audio: UploadFile = File(...),
    conv_id: str = Form("warroom-voice"),
):
    """Accept a recorded audio blob (m4a / wav / ogg), transcribe via mlx-whisper,
    run through agent.respond, return both the text reply AND a URL to a generated
    voice-clone reply audio file. Auth check has already run via middleware.
    """
    # Save inbound audio
    suffix = Path(audio.filename or "voice.m4a").suffix or ".m4a"
    inbound_path = _AUDIO_TMP / f"in_{uuid.uuid4().hex}{suffix}"
    with inbound_path.open("wb") as f:
        f.write(await audio.read())

    # Transcribe
    from core import transcribe as _transcribe
    try:
        transcript = await asyncio.to_thread(_transcribe.transcribe, str(inbound_path))
    except Exception as e:  # noqa: BLE001
        log.exception("transcription failed")
        return {"error": f"transcribe: {type(e).__name__}: {e}"}
    finally:
        inbound_path.unlink(missing_ok=True)

    if not transcript.strip():
        return {"transcript": "", "reply": "(empty transcript)", "audio_url": None}

    # Run through agent
    from core import agent
    reply = await asyncio.to_thread(agent.respond, transcript, conv_id)

    # Generate spoken reply (Charles's cloned voice)
    audio_url: str | None = None
    if reply and reply.strip():
        try:
            from core import speak as _speak
            ogg_path = await asyncio.to_thread(_speak.speak_to_ogg, reply)
            # Move into a stable cache so /api/audio/{name} can serve it
            named = _AUDIO_TMP / f"out_{uuid.uuid4().hex}.ogg"
            ogg_path.rename(named)
            audio_url = f"/api/audio/{named.name}"
        except Exception as e:  # noqa: BLE001
            log.exception("speak failed (text reply still returned): %s", e)

    return {"transcript": transcript, "reply": reply, "audio_url": audio_url}


@app.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    # Only allow files we generated under _AUDIO_TMP
    safe = _AUDIO_TMP / filename
    try:
        safe = safe.resolve()
        if not str(safe).startswith(str(_AUDIO_TMP.resolve())):
            raise HTTPException(403, "path escape")
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "not found")
    if not safe.exists():
        raise HTTPException(404, "audio file not found")
    return FileResponse(str(safe), media_type="audio/ogg")


# ---- Push notifications -----------------------------------------------------

_PUSH_DB_PATH = Path("/Users/home/charles/workspace/warroom_push_tokens.sqlite")


def _push_db() -> sqlite3.Connection:
    con = sqlite3.connect(str(_PUSH_DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS push_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            bundle_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT
        )
    """)
    con.commit()
    return con


@app.post("/api/push/register")
async def push_register(req: Request):
    """Register an APNs device token so the server can send push when an
    approval-pending fact lands. The actual APNs send is in
    warroom/push.py — needs an Apple Developer auth token (.p8 file) which
    John provisions in Phase 4.
    """
    body: dict[str, Any] = await req.json()
    token = body.get("token")
    platform = body.get("platform", "ios")
    bundle_id = body.get("bundle_id", "ai.charlescreator.warroom")
    if not token:
        raise HTTPException(400, "token required")
    con = _push_db()
    try:
        con.execute(
            "INSERT INTO push_tokens (token, platform, bundle_id) VALUES (?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP, "
            "platform=excluded.platform, bundle_id=excluded.bundle_id",
            (token, platform, bundle_id),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/command/restart")
async def cmd_restart():
    """Restart Charles via launchctl. War Room itself is unaffected."""
    import subprocess
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/com.charles.agent"],
        capture_output=True, text=True, timeout=15,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
    }


def _resolve_approval(fact_id: int, resolution: str) -> dict[str, Any]:
    """Append a 'resolved' tag to the approval-pending fact and write a follow-up fact."""
    import sqlite3
    from datetime import datetime, timezone
    db = "/Users/home/charles/workspace/memory.db"
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT fact, tags FROM long_term_facts WHERE id=? AND tags LIKE '%approval-pending%'",
            (fact_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"approval fact #{fact_id} not found or not pending")
        original_fact, original_tags = row
        new_tags = f"{original_tags},resolved" if "resolved" not in original_tags else original_tags
        con.execute("UPDATE long_term_facts SET tags=? WHERE id=?", (new_tags, fact_id))
        followup = (
            f"APPROVAL #{fact_id} {resolution} at "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
            f"Original ask: {original_fact[:200]}"
        )
        con.execute(
            "INSERT INTO long_term_facts (fact, tags) VALUES (?, ?)",
            (followup, f"approval-resolution,approval-pending:{fact_id}"),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "fact_id": fact_id, "resolution": resolution}


# ---- WebSocket event stream -------------------------------------------------

@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    sig = ws.query_params.get("sig", "")
    # WS auth uses signature over fixed string b"ws-stream"
    if not auth.verify(b"ws-stream", sig):
        await ws.close(code=4401, reason="invalid signature")
        return

    await ws.accept()
    last_id = state.latest_conversation_id()
    log.info("ws connected; starting at conversations.id=%d", last_id)

    try:
        while True:
            new_rows = state.conversation_rows_since(last_id, limit=50)
            for row in new_rows:
                await ws.send_text(json.dumps({"type": "turn", "data": row}))
                last_id = row["id"]
            # Periodic state snapshot every 10s for the Lock Screen widget refresh path
            if int(time.time()) % 10 == 0:
                await ws.send_text(json.dumps({"type": "snapshot", "data": state.now_summary()}))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        log.info("ws disconnected")
    except Exception as e:  # noqa: BLE001
        log.exception("ws error: %s", e)
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
