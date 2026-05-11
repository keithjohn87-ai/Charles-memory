"""Memory consolidation — nightly cleanup of long_term_facts.

Without consolidation, long_term_facts is append-only and gets noisy fast:
duplicate facts, stale one-off task notes, redundant entries from the same
event captured multiple times. After a few weeks, recall(query=...) starts
returning low-signal results.

The consolidator runs as a scheduled task at 04:00 EST nightly (after the
03:00 backup). On each run:

  1. Pull facts from the last N hours (default 24).
  2. Group by primary tag.
  3. For each group, find near-duplicates by content overlap.
  4. Mark duplicates with tag 'superseded' (don't delete — preserve history).
  5. Write a summary fact: 'Consolidation YYYY-MM-DD — N reviewed, M superseded'.
  6. Append a markdown line to workspace/memory/consolidation_log.md.

Charles can also call this on demand. Default is dry_run=False (real changes).
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from core.tools import tool
from core import memory as _mem

log = logging.getLogger("charles.memory_consolidation")

DB_PATH = Path("/Users/home/charles/workspace/memory.db")
MEMORY_DIR = Path("/Users/home/charles/workspace/memory")
SIMILARITY_THRESHOLD = 0.78  # 78% similar = treat as duplicate


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _primary_tag(tags: str) -> str:
    """First non-trivial tag is the primary classifier."""
    parts = [t.strip() for t in (tags or "").split(",") if t.strip()]
    # Skip generic noise tags
    skip = {"reference", "priority", "fact"}
    for p in parts:
        if p.lower() not in skip and ":" not in p:
            return p
    return parts[0] if parts else "untagged"


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # Quick prefix check — most duplicates start the same way
    if a[:60].lower() == b[:60].lower():
        return 1.0
    return SequenceMatcher(None, a[:500], b[:500]).ratio()


def _pick_canonical(group: list[sqlite3.Row]) -> tuple[int, list[int]]:
    """Pick which fact in the dup group to keep. Heuristic: longest content,
    most recent created_at as tiebreaker. Returns (keep_id, supersede_ids)."""
    if len(group) == 1:
        return group[0]["id"], []
    sorted_rows = sorted(
        group,
        key=lambda r: (-len(r["fact"] or ""), r["created_at"] or ""),
    )
    keep = sorted_rows[0]
    supersede = [r["id"] for r in sorted_rows[1:] if r["id"] != keep["id"]]
    return keep["id"], supersede


@tool(
    name="consolidate_memory",
    summary=(
        "Review long_term_facts saved in the last N hours, find near-duplicates, mark them "
        "superseded (NOT deleted — preserved with a tag), write a daily summary. "
        "Defaults: 24h window, real run. Run nightly via scheduled task; run manually any time "
        "the fact store feels noisy."
    ),
    triggers=("consolidate memory", "clean up facts", "deduplicate facts", "memory cleanup"),
    schema={
        "type": "object",
        "properties": {
            "window_hours": {"type": "integer", "description": "Look back N hours. Default 24.", "default": 24},
            "dry_run": {"type": "boolean", "description": "If true, don't write — just report what would happen.", "default": False},
        },
    },
)
def consolidate_memory(window_hours: int = 24, dry_run: bool = False) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    con = _conn()
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id, fact, tags, created_at FROM long_term_facts "
        "WHERE created_at >= ? AND tags NOT LIKE '%superseded%' "
        "ORDER BY id",
        (cutoff,),
    ).fetchall()

    if not rows:
        con.close()
        return f"(no facts in last {window_hours}h)"

    # Group by primary tag
    by_tag: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_tag[_primary_tag(r["tags"])].append(r)

    superseded_count = 0
    kept_count = 0
    by_tag_summary: list[str] = []

    for tag, facts in by_tag.items():
        # Build dup groups via single-pass similarity
        groups: list[list[sqlite3.Row]] = []
        for f in facts:
            placed = False
            for g in groups:
                if _similar(f["fact"], g[0]["fact"]) >= SIMILARITY_THRESHOLD:
                    g.append(f)
                    placed = True
                    break
            if not placed:
                groups.append([f])

        for g in groups:
            keep_id, supersede_ids = _pick_canonical(g)
            kept_count += 1
            if not supersede_ids:
                continue
            superseded_count += len(supersede_ids)
            if not dry_run:
                for sid in supersede_ids:
                    # Append 'superseded:<keep_id>' to existing tags
                    row = cur.execute("SELECT tags FROM long_term_facts WHERE id=?", (sid,)).fetchone()
                    new_tags = f"{row['tags']},superseded,superseded_by:{keep_id}" if row else f"superseded,superseded_by:{keep_id}"
                    cur.execute("UPDATE long_term_facts SET tags=? WHERE id=?", (new_tags, sid))
        by_tag_summary.append(f"  {tag}: {len(facts)} facts → {sum(1 for g in groups if len(g) > 1)} dup-groups")

    if not dry_run:
        # Save a summary fact for posterity
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary_text = (
            f"MEMORY CONSOLIDATION {date_str} — {len(rows)} facts reviewed in last {window_hours}h, "
            f"{kept_count} kept, {superseded_count} superseded as duplicates. "
            f"Tags: {', '.join(sorted(by_tag.keys()))}"
        )
        # Route through canonical taxonomy — daily_summary is a leaf under system_health.
        _summary_embed = None
        try:
            from core import embeddings as _embed
            _summary_embed = _embed.encode(summary_text)
        except Exception:  # noqa: BLE001
            pass
        cur.execute(
            "INSERT INTO long_term_facts (fact, tags, topic, embedding) VALUES (?, ?, ?, ?)",
            (summary_text, "consolidation,daily_summary", "daily_summary", _summary_embed),
        )
        con.commit()

        # Append to markdown log so John can scan history
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        log_path = MEMORY_DIR / "consolidation_log.md"
        with log_path.open("a") as fp:
            fp.write(f"\n## {date_str} ({datetime.now(timezone.utc).strftime('%H:%M')}Z)\n")
            fp.write(f"- Reviewed {len(rows)} facts (last {window_hours}h), kept {kept_count}, superseded {superseded_count}\n")
            for line in by_tag_summary:
                fp.write(f"  -{line}\n")

    con.close()

    out = [
        f"Memory consolidation {'(DRY RUN) ' if dry_run else ''}— last {window_hours}h:",
        f"  Reviewed: {len(rows)} facts",
        f"  Kept: {kept_count}",
        f"  Superseded: {superseded_count}",
        "",
        "By tag:",
    ]
    out.extend(by_tag_summary)
    return "\n".join(out)
