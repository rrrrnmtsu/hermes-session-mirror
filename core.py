"""Pure logic for session-mirror: redaction, chunking, Discord REST, thread map.

Kept free of Hermes imports so it is unit-testable and reusable outside a gateway.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
CHUNK = 1900  # Discord hard limit is 2000; leave room for the role header.

# Why local fallback patterns: Hermes' redact_for_egress is the real filter, but the plugin
# must fail closed (not leak) when that import breaks, so a coarse second net stays on.
_FALLBACK = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,})"
)


def redact(text: str, base: Optional[Callable[[str], str]] = None) -> str:
    if base is not None:
        try:
            text = base(text)
        except Exception:
            logger.debug("session-mirror: base redactor failed", exc_info=True)
    return _FALLBACK.sub("[REDACTED]", text)


def chunk(text: str, size: int = CHUNK) -> list[str]:
    """Split on newline boundaries where possible; never emit an empty or oversize piece."""
    text = text.strip()
    out: list[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return [c for c in out if c]


def clip(text: str, limit: int) -> str:
    if limit > 0 and len(text) > limit:
        return text[:limit] + f"\n…(+{len(text) - limit} chars truncated)"
    return text


def thread_name(first_message: str) -> str:
    line = next((l.strip() for l in first_message.splitlines() if l.strip()), "session")
    return line[:90]


def render_turn(user: str, assistant: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    if user and user.strip():
        body = clip(user, max_chars)
        pieces = chunk(body)
        parts.append("**🧑 you**\n" + pieces[0])
        parts.extend(pieces[1:])
    if assistant and assistant.strip():
        body = clip(assistant, max_chars)
        pieces = chunk(body)
        parts.append("**🤖 Hermes**\n" + pieces[0])
        parts.extend(pieces[1:])
    return parts


class Discord:
    def __init__(self, token: str, opener=urllib.request.urlopen):
        self._token = token
        self._open = opener

    def _req(self, method: str, path: str, payload: dict, retries: int = 2) -> dict:
        data = json.dumps(payload).encode()
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                API + path, data=data, method=method,
                headers={"Authorization": f"Bot {self._token}", "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (hermes-session-mirror, 0.1)"},
            )
            try:
                with self._open(req, timeout=15) as r:
                    body = r.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries:
                    try:
                        wait = float(json.loads(e.read()).get("retry_after", 1))
                    except Exception:
                        wait = 1.0
                    time.sleep(min(wait, 10))
                    continue
                raise
        return {}

    def create_thread(self, channel_id: str, name: str) -> str:
        r = self._req("POST", f"/channels/{channel_id}/threads",
                      {"name": name, "type": 11, "auto_archive_duration": 10080})
        return str(r["id"])

    def post(self, channel_id: str, content: str) -> None:
        self._req("POST", f"/channels/{channel_id}/messages",
                  {"content": content, "allowed_mentions": {"parse": []}})


class ThreadMap:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Optional[str]:
        with self._lock:
            return self._load().get(session_id)

    def set(self, session_id: str, thread_id: str) -> None:
        with self._lock:
            d = self._load()
            d[session_id] = thread_id
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(self.path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}


class Mirror:
    """Serial background sender so a slow/blocked Discord never delays a turn."""

    def __init__(self, discord: Discord, channel_id: str, threads: ThreadMap, max_chars: int):
        self.discord, self.channel_id, self.threads, self.max_chars = discord, channel_id, threads, max_chars
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=200)
        threading.Thread(target=self._run, name="session-mirror", daemon=True).start()

    def submit(self, session_id: str, user: str, assistant: str) -> bool:
        try:
            self.q.put_nowait((session_id, user, assistant))
            return True
        except queue.Full:
            logger.warning("session-mirror: queue full, dropping a turn")
            return False

    def _run(self) -> None:
        while True:
            item = self.q.get()
            try:
                self.send(*item)
            except Exception:
                logger.warning("session-mirror: send failed", exc_info=True)

    def send(self, session_id: str, user: str, assistant: str) -> None:
        tid = self.threads.get(session_id)
        if tid is None:
            tid = self.discord.create_thread(self.channel_id, thread_name(user))
            self.threads.set(session_id, tid)
        for part in render_turn(user, assistant, self.max_chars):
            self.discord.post(tid, part)
