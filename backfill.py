"""Backfill: replay existing sessions from Hermes' state.db into Discord threads.

Pure functions plus one driver; the Discord/ThreadMap/Mirror objects are injected so this stays
testable. Sessions already present in the thread map are skipped, which makes a re-run safe.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    from . import core
except ImportError:  # pragma: no cover
    import core  # type: ignore

_TEXT_PART_TYPES = ("text", "input_text", "output_text")


def message_text(content) -> str:
    if not isinstance(content, str):
        return ""
    s = content.strip()
    if s.startswith("["):
        try:
            parts = json.loads(s)
            if isinstance(parts, list):
                return "\n".join(p.get("text", "") for p in parts
                                 if isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES).strip()
        except ValueError:
            pass
    return s


def pair_turns(rows: Iterable[tuple]) -> list[tuple[str, str]]:
    """(role, content) rows -> [(user, assistant)]; consecutive assistant text joins into one reply.
    Tool rows and user messages without any assistant text are dropped."""
    turns: list[list] = []
    cur: Optional[list] = None
    for role, content in rows:
        text = message_text(content)
        if role == "user" and text:
            cur = [text, []]
            turns.append(cur)
        elif role == "assistant" and text and cur is not None:
            cur[1].append(text)
    return [(u, "\n\n".join(a)) for u, a in turns if a]


def select_sessions(db_path: Path, sources: Iterable[str], include_archived: bool = False,
                    only: Optional[str] = None, limit: Optional[int] = None) -> list[tuple[str, Optional[str], list]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        srcs = [s.strip() for s in sources if s.strip()]
        marks = ",".join("?" * len(srcs))
        sql = f"SELECT id, title FROM sessions WHERE source IN ({marks}) AND coalesce(title,'') NOT LIKE 'Subagent:%'"
        args: list = list(srcs)
        if not include_archived:
            sql += " AND archived=0 AND hidden=0"
        if only:
            sql += " AND id=?"
            args.append(only)
        sql += " ORDER BY started_at"
        out = []
        for sid, title in con.execute(sql, args).fetchall():
            rows = con.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND active=1 "
                "AND coalesce(_compressed_summary,0)=0 ORDER BY timestamp, id", (sid,)).fetchall()
            turns = pair_turns(rows)
            if turns:
                out.append((sid, title, turns))
        return out[-limit:] if limit else out
    finally:
        con.close()


def run(sessions: list, mirror, threads, redact_base: Optional[Callable[[str], str]],
        dry_run: bool = False, log: Callable[[str], None] = print) -> dict:
    """Send every not-yet-mirrored session. Redaction is fail-closed, same as the live hook."""
    stats = {"sessions": 0, "turns": 0, "skipped": 0}
    for sid, title, turns in sessions:
        if threads.get(sid):
            stats["skipped"] += 1
            log(f"skip (already mirrored) {sid}")
            continue
        log(f"{'would mirror' if dry_run else 'mirror'} {sid} turns={len(turns)} {(title or '')[:40]}")
        if dry_run:
            stats["sessions"] += 1
            stats["turns"] += len(turns)
            continue
        safe_title = core.redact(title, redact_base, True) if title else None
        for user, assistant in turns:
            u = core.redact(user, redact_base, True)
            a = core.redact(assistant, redact_base, True)
            if core.is_unavailable(u) and core.is_unavailable(a):
                continue
            mirror.send(sid, u, a, title=safe_title)
            stats["turns"] += 1
        stats["sessions"] += 1
    return stats
