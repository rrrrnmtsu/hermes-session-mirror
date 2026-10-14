"""Pure logic for session-mirror: redaction, chunking, Discord REST, thread map.

Kept free of Hermes imports so it is unit-testable and reusable outside a gateway.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

try:  # POSIX only; without it ThreadMap falls back to the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

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

REDACTION_UNAVAILABLE = "[redaction-unavailable]"


def redact(text: str, base: Optional[Callable[[str], str]] = None, fail_closed: bool = False) -> str:
    """Mask secrets. With fail_closed, a missing/broken `base` yields REDACTION_UNAVAILABLE, never raw text."""
    if base is None:
        if fail_closed:
            return REDACTION_UNAVAILABLE
    else:
        try:
            text = base(text)
        except Exception:
            logger.debug("session-mirror: base redactor failed", exc_info=True)
            if fail_closed:
                return REDACTION_UNAVAILABLE
    return _FALLBACK.sub("[REDACTED]", text)


def is_unavailable(text: Optional[str]) -> bool:
    """True when text consists only of the redaction-unavailable marker."""
    if not text or REDACTION_UNAVAILABLE not in text:
        return False
    return not text.replace(REDACTION_UNAVAILABLE, "").strip()


_FENCE_CHARS = ("`", "~")
_TAG_MAX = 20


def _fence_after(text: str, state=None):
    """Fence state after `text`. state is None (closed) or (fence_char, lang_tag) when open."""
    for line in text.split("\n"):
        st = line.strip()
        ch = st[:1]
        if ch not in _FENCE_CHARS or not st.startswith(ch * 3):
            continue
        if state is not None:
            if ch == state[0]:
                state = None
        else:
            rest = st.lstrip(ch).strip()
            state = (ch, rest.split(" ")[0][:_TAG_MAX] if rest else "")
    return state


def chunk(text: str, size: int = CHUNK) -> list[str]:
    """Split on newline boundaries where possible; never emit an empty or oversize piece.

    Fence-aware (``` and ~~~): a piece never ends inside an open block. A split inside a fence
    closes it at the end of the piece and reopens it (same fence char, tag capped at 20 chars)
    at the start of the next.
    """
    text = text.strip()
    out: list[str] = []
    state = None  # fence carried over from the previous piece
    close_len = 4  # "\n" + 3 fence chars
    while text:
        prefix = f"{state[0] * 3}{state[1]}\n" if state is not None else ""
        room = size - len(prefix)
        end_state = _fence_after(text, state)
        if len(text) <= room and end_state is None:
            out.append(prefix + text)
            break
        if end_state is not None and len(text) + close_len <= room:
            out.append(prefix + text + "\n" + end_state[0] * 3)
            break
        budget = max(room - close_len, 1)
        cut = text.rfind("\n", 0, budget)
        if cut < budget // 2:
            cut = budget
        piece = text[:cut].rstrip()
        rest = text[cut:].lstrip("\n")
        new_state = _fence_after(piece, state)
        if not piece:  # only whitespace before the cut: skip it, avoid empty pieces
            text = rest
            continue
        out.append(prefix + piece + ("\n" + new_state[0] * 3 if new_state is not None else ""))
        state, text = new_state, rest
    return [c for c in out if c.strip()]


def clip(text: str, limit: int) -> str:
    if limit > 0 and len(text) > limit:
        return text[:limit] + f"\n…(+{len(text) - limit} chars truncated)"
    return text


_SYSTEM_PREFIXES = ("[Cronjob", "[SYSTEM", "[IMPORTANT", "[System", "[Note:",
                    "[CONTEXT COMPACTION", "[Background", "<system-reminder")
_GENERIC_TITLES = {"", "session", "hermes session", "new session", "new chat", "untitled",
                   "untitled session", "new conversation"}
FALLBACK_NAME = "Hermes session"
NAME_MAX = 90

_CTRL = re.compile("[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_INVISIBLE = re.compile("[\u200b\u200c\u200d\u00a0\ufeff]")
_MENTION_OPEN = re.compile(r"<(?=[@#])")


def is_system_text(text: str) -> bool:
    """True for injected, non-human text (cron prompts, system notes, compaction notices)."""
    return bool(text) and text.lstrip().startswith(_SYSTEM_PREFIXES)


def _clean_name(raw: str, limit: int = NAME_MAX) -> str:
    """Single-line, control/bidi-free, mention-neutral thread name; FALLBACK_NAME if blank."""
    s = re.sub(r"\s+", " ", raw or "")
    s = _CTRL.sub("", s).strip()
    if not _INVISIBLE.sub("", s).strip():
        return FALLBACK_NAME
    return s.replace("@", "@\u200b")[:limit].strip()


def is_generic_title(title: Optional[str]) -> bool:
    """True for empty/placeholder titles that must never name or rename a thread."""
    if title is None:
        return True
    return _clean_name(title).lower() in _GENERIC_TITLES


def thread_name(first_message: str) -> str:
    if not first_message or not first_message.strip():
        return FALLBACK_NAME
    if not is_system_text(first_message):
        lines = [l.strip() for l in first_message.splitlines() if l.strip()]
        real = [l for l in lines if not _ATTACH_LINE.match(l)]
        if not real:
            return FALLBACK_NAME
        return _clean_name(real[0])
    # Injected text: use the first paragraph that is not itself system text, if any.
    for para in re.split(r"\n\s*\n", first_message):
        if para.strip() and not is_system_text(para):
            return _clean_name(para.strip().splitlines()[0])
    return FALLBACK_NAME


def thread_name_for(title: Optional[str], first_message: str) -> str:
    if not is_generic_title(title):
        return _clean_name(title or "")
    return thread_name(first_message)


_USAGE_RE = re.compile(r"\n*<details\s*>\s*<summary\s*>[^<]*\U0001F4CA[^<]*(?:用量|usage).*\Z", re.S | re.I)
_ATTACH_LINE = re.compile(r"^\s*(?:@file:|@image:|@url:)\S*\s*$")


def strip_usage_footer(text: str) -> str:
    """Drop the turn-usage plugin's trailing <details> block; it is cost telemetry, not conversation."""
    return _USAGE_RE.sub("", text or "").rstrip()


def _defang(text: str) -> str:
    """Break raw mention/channel syntax (<@123>, <#123>) with a zero-width space."""
    return _MENTION_OPEN.sub("<\u200b", text)


def render_turn(user: str, assistant: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    if user and user.strip():
        header = "**⚙️ system**" if is_system_text(user) else "**🧑 you**"
        pieces = chunk(clip(_defang(user), max_chars), CHUNK - len(header) - 1)
        parts.append(header + "\n" + pieces[0])
        parts.extend(pieces[1:])
    assistant = strip_usage_footer(assistant)
    if assistant and assistant.strip():
        header = "**🤖 Hermes**"
        pieces = chunk(clip(_defang(assistant), max_chars), CHUNK - len(header) - 1)
        parts.append(header + "\n" + pieces[0])
        parts.extend(pieces[1:])
    return parts


_sleep = time.sleep


class DiscordError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"Discord HTTP {status}: {body[:200]}")
        self.status, self.body = status, body


_ID_RE = re.compile(r"\d{1,25}")


def _check_id(value) -> str:
    """Reject anything but a short decimal snowflake before it reaches a URL path."""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise DiscordError(0, "invalid id")
    return value


class Discord:
    def __init__(self, token: str, opener=urllib.request.urlopen,
                 refresh: Optional[Callable[[], Optional[str]]] = None):
        self._token = token
        self._open = opener
        self._refresh = refresh

    def _once(self, method: str, path: str, data: bytes) -> dict:
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": f"Bot {self._token}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (hermes-session-mirror, 0.2)"},
        )
        try:
            with self._open(req, timeout=15) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            raise DiscordError(e.code, body) from e
        except (urllib.error.URLError, OSError) as e:  # timeouts are OSError subclasses
            raise DiscordError(0, str(e)) from e

    @staticmethod
    def _transient(method: str, status: int) -> bool:
        # POST is not idempotent: a timeout/reset/500 may mean the server already accepted it,
        # so only statuses that prove it was NOT processed (gateway errors) are retried.
        if method == "POST":
            return status in (502, 503, 504)
        return status == 0 or status >= 500

    def _req(self, method: str, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        refreshed, rate_hits, transient = False, 0, 0
        while True:
            try:
                return self._once(method, path, data)
            except DiscordError as e:
                if e.status == 401 and self._refresh is not None and not refreshed:
                    refreshed = True
                    try:
                        new = self._refresh()
                    except Exception:
                        logger.debug("session-mirror: token refresh failed", exc_info=True)
                        new = None
                    if new:
                        self._token = new
                        continue
                elif e.status == 429 and rate_hits < 2:
                    rate_hits += 1
                    try:
                        wait = float(json.loads(e.body).get("retry_after", 1))
                    except Exception:
                        wait = 1.0
                    _sleep(min(wait, 10))
                    continue
                elif self._transient(method, e.status) and transient < 2:
                    transient += 1
                    _sleep(0.5 * transient)
                    continue
                raise

    def create_thread(self, channel_id: str, name: str) -> str:
        _check_id(channel_id)
        r = self._req("POST", f"/channels/{channel_id}/threads",
                      {"name": name[:100], "type": 11, "auto_archive_duration": 10080})
        return str(r["id"])

    def post(self, thread_id: str, content: str) -> None:
        _check_id(thread_id)
        nonce = uuid.uuid4().hex[:25]  # once per call: retries reuse it so Discord can dedupe
        self._req("POST", f"/channels/{thread_id}/messages",
                  {"content": content, "allowed_mentions": {"parse": []},
                   "nonce": nonce, "enforce_nonce": True})

    def rename_thread(self, thread_id: str, name: str) -> None:
        _check_id(thread_id)
        self._req("PATCH", f"/channels/{thread_id}", {"name": name[:100]})

    def unarchive_thread(self, thread_id: str) -> None:
        _check_id(thread_id)
        self._req("PATCH", f"/channels/{thread_id}", {"archived": False})


MAX_ENTRIES = 500
_DIGITS = re.compile(r"[0-9]+")


class ThreadMap:
    """session_id -> {'thread', 'name', 'ts'}; reads the legacy {session: "id"} format too.

    Safe across threads (lock) and processes (flock on '<name>.lock' around load-modify-save).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self):
        with self._lock:
            if fcntl is None:
                yield
                return
            fd = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.path.with_name(self.path.name + ".lock")),
                             os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                logger.debug("session-mirror: file lock unavailable", exc_info=True)
            try:
                yield
            finally:
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    def get(self, session_id: str) -> Optional[str]:
        with self._locked():
            e = self._load().get(session_id)
            return e["thread"] if e else None

    def get_name(self, session_id: str) -> Optional[str]:
        with self._locked():
            e = self._load().get(session_id)
            return e["name"] if e else None

    def set(self, session_id: str, thread_id: str, name: Optional[str] = None) -> None:
        with self._locked():
            d = self._load()
            d[session_id] = {"thread": str(thread_id), "name": name, "ts": time.time()}
            if len(d) > MAX_ENTRIES:
                keep = sorted(d.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:MAX_ENTRIES]
                d = dict(keep)
            self._save(d)

    def drop(self, session_id: str) -> None:
        with self._locked():
            d = self._load()
            if d.pop(session_id, None) is not None:
                self._save(d)

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(d))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _quarantine(self) -> None:
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
            logger.warning("session-mirror: corrupt thread map moved to %s.corrupt", self.path.name)
        except OSError:
            logger.debug("session-mirror: could not quarantine corrupt map", exc_info=True)

    def _load(self) -> dict:
        try:
            text = self.path.read_text()
        except OSError:
            return {}
        try:
            raw = json.loads(text)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            self._quarantine()
            return {}
        out = {}
        for k, v in raw.items():
            try:
                if not isinstance(k, str):
                    continue
                if isinstance(v, str):
                    thread, name, ts = v, None, 0.0
                elif isinstance(v, dict):
                    thread = v.get("thread")
                    if isinstance(thread, int) and not isinstance(thread, bool):
                        thread = str(thread)
                    name = v.get("name")
                    name = name if isinstance(name, str) else None
                    ts = float(v.get("ts") or 0.0)
                    if not math.isfinite(ts):
                        continue
                else:
                    continue
                if not isinstance(thread, str) or not _DIGITS.fullmatch(thread):
                    continue
                out[k] = {"thread": thread, "name": name, "ts": ts}
            except Exception:
                continue
        return out


_STOP = object()


class Mirror:
    """Serial background sender so a slow/blocked Discord never delays a turn."""

    def __init__(self, discord: Discord, channel_id: str, threads: ThreadMap, max_chars: int):
        self.discord, self.channel_id, self.threads, self.max_chars = discord, channel_id, threads, max_chars
        self.q: "queue.Queue[object]" = queue.Queue(maxsize=200)
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="session-mirror", daemon=True)
        self._thread.start()

    def submit(self, session_id: str, user: str, assistant: str, title: Optional[str] = None) -> bool:
        if getattr(self, "_closed", False):
            return False
        try:
            self.q.put_nowait((session_id, user, assistant, title))
            return True
        except queue.Full:
            logger.warning("session-mirror: queue full, dropping a turn")
            return False

    def _run(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is _STOP:
                    return
                self.send(*item)
            except Exception:
                logger.warning("session-mirror: send failed", exc_info=True)
            finally:
                self.q.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued item has been processed. True if drained within timeout."""
        deadline = time.monotonic() + timeout
        with self.q.all_tasks_done:
            while self.q.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.q.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout: float = 2.0) -> bool:
        """Stop accepting turns, drain the queue, stop the worker. True if the worker exited."""
        deadline = time.monotonic() + timeout
        self._closed = True
        self.flush(timeout)
        worker = getattr(self, "_thread", None)
        if worker is None or not worker.is_alive():
            return True
        try:
            self.q.put(_STOP, timeout=max(deadline - time.monotonic(), 0.01))
        except queue.Full:
            return False
        worker.join(max(deadline - time.monotonic(), 0.01))
        return not worker.is_alive()

    def _new_thread(self, session_id: str, title: Optional[str], user: str) -> str:
        name = thread_name_for(title, user)
        tid = self.discord.create_thread(self.channel_id, name)
        self.threads.set(session_id, tid, name)
        return tid

    def send(self, session_id: str, user: str, assistant: str, title: Optional[str] = None) -> None:
        if is_unavailable(user):
            user = ""
        if is_unavailable(assistant):
            assistant = ""
        parts = render_turn(user, assistant, self.max_chars)
        if not parts:
            return
        tid = self.threads.get(session_id)
        if tid is None:
            tid = self._new_thread(session_id, title, user)
        elif title and title.strip() and not is_generic_title(title):
            new_name = thread_name_for(title, user)
            if new_name != self.threads.get_name(session_id):
                try:
                    self.discord.rename_thread(tid, new_name)
                except Exception:
                    logger.warning("session-mirror: rename failed", exc_info=True)
                # Store the attempted name even on failure so a failing rename (e.g. 429)
                # is not retried every turn.
                self.threads.set(session_id, tid, new_name)
        posted, unarchived, recreated = 0, False, 0
        while posted < len(parts):
            try:
                self.discord.post(tid, parts[posted])
                posted += 1
            except DiscordError as e:
                if e.status == 404 and recreated < 2:
                    recreated += 1
                    self.threads.drop(session_id)
                    tid = self._new_thread(session_id, title, user)
                    posted = 0  # the new thread gets the whole turn, header included
                elif (e.status == 403 or "archived" in (e.body or "").lower()) and not unarchived:
                    unarchived = True
                    self.discord.unarchive_thread(tid)
                else:
                    raise
