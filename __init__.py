"""session-mirror: mirror non-Discord Hermes sessions into Discord threads (post_llm_call hook)."""
from __future__ import annotations

import atexit
import logging
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

try:  # package import inside Hermes; flat import when unit-tested standalone
    from . import core
except ImportError:  # pragma: no cover
    import core  # type: ignore

logger = logging.getLogger(__name__)
_CTX = None
_MIRROR = None
_MIRROR_CHANNEL = None
_ACTIVE = False
_NEXT_CHECK = 0.0
_MIRROR_KEY = None
_WARNED_REASON = None
_LOCK = threading.RLock()
RETRY_SECONDS = 60
DEFAULT_PLATFORMS = "desktop,cli,acp,tui"
UNLIMITED = 10 ** 9  # max_chars <= 0 means no truncation


def _setting(key, default):
    try:
        v = _CTX.get_config(key, default) if _CTX else default
        return default if v is None else v
    except Exception:
        return default


def _token(name: str):
    try:
        from agent import secret_scope
        v = secret_scope.get_secret(name)
        if v:
            return v
    except Exception:
        pass
    v = os.environ.get(name)
    if v:
        return v
    cmd = str(_setting("token_command", "") or "").strip()
    if cmd:  # opt-in: e.g. a password-manager CLI; never run unless the user configured it
        try:
            r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return None


def _base_redactor():
    try:
        from agent.redact import redact_for_egress
        return redact_for_egress
    except Exception:
        return None


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _now() -> float:
    return time.monotonic()


def _session_row(session_id):
    """Read (title, source) of a session from Hermes' state DB (read-only); (None, None) on failure."""
    try:
        db = _hermes_home() / "state.db"
        if not db.exists():
            return (None, None)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT title, source FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            con.close()
        if not row:
            return (None, None)
        title = (row[0] or "").strip() or None
        source = (row[1] or "").strip().lower() or None
        return (title, source)
    except Exception:
        return (None, None)


def _is_oneshot(platform, source):
    """One-shot runs (`hermes -z` / `chat -q`) report platform 'cli' but session source 'oneshot'."""
    if (source or "") == "oneshot":
        return True
    try:
        from agent.session_source import session_source_for
        return session_source_for(platform) == "oneshot"
    except Exception:
        return False


def _fail(reason):
    """Back off 60 s; warn once per failure episode."""
    global _WARNED_REASON
    if _WARNED_REASON != reason:
        logger.warning("session-mirror: %s; inactive (retrying every %ds)", reason, RETRY_SECONDS)
        _WARNED_REASON = reason


def _max_chars():
    v = _setting("max_chars", 6000)
    if v is None:
        return 6000
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 6000
    return n if n > 0 else UNLIMITED


def _close_quietly(m):
    try:
        if m is not None and hasattr(m, "close"):
            m.close()
    except Exception:
        logger.debug("session-mirror: closing old mirror failed", exc_info=True)


def _get_mirror():
    """Return the Mirror, or None while paused/disabled/misconfigured. Re-evaluated every 60 s.

    May block up to token_command's 10 s timeout when (re)building the Mirror."""
    global _MIRROR, _ACTIVE, _NEXT_CHECK, _WARNED_REASON, _MIRROR_CHANNEL, _MIRROR_KEY
    with _LOCK:
        now = _now()
        if now < _NEXT_CHECK:
            return _MIRROR if _ACTIVE else None
        _NEXT_CHECK = now + RETRY_SECONDS
        _ACTIVE = False
        if _setting("paused", False) or not _setting("enabled", True):
            return None  # deliberate, not a failure: no warning
        channel = str(_setting("channel_id", "") or "").strip()
        if not channel:
            _fail("channel_id not set")
            return None
        name = str(_setting("token_secret", "DISCORD_BOT_TOKEN"))
        max_chars = _max_chars()
        key = (channel, name, max_chars)
        if _MIRROR is None or key != _MIRROR_KEY:
            token = _token(name)
            if not token:
                _fail("bot token unavailable")
                return None
            old = _MIRROR
            _MIRROR = core.Mirror(
                core.Discord(token, refresh=lambda: _token(name)), channel,
                core.ThreadMap(_hermes_home() / "state" / "session-mirror.json"),
                max_chars,
            )
            _MIRROR_CHANNEL = channel
            _MIRROR_KEY = key
            if old is not None:
                _close_quietly(old)
        _ACTIVE = True
        _WARNED_REASON = None
        return _MIRROR


def on_post_llm_call(session_id=None, user_message=None, assistant_response=None, platform=None, **_):
    try:
        allowed = {p.strip().lower() for p in str(_setting("platforms", DEFAULT_PLATFORMS)).split(",") if p.strip()}
        allowed.discard("discord")
        plat = str(platform or "").strip().lower()
        if not session_id or plat not in allowed:
            return None
        user = str(user_message or "")
        assistant = str(assistant_response or "")
        if not user.strip() and not assistant.strip():
            return None
        title, source = _session_row(session_id)
        if _is_oneshot(plat, source):
            return None
        m = _get_mirror()
        if m is None:
            return None
        base = _base_redactor()
        u = core.redact(user, base, fail_closed=True)
        a = core.redact(assistant, base, fail_closed=True)
        if all((not x.strip()) or core.is_unavailable(x) for x in (u, a)):
            return None  # nothing safe to send
        if title:
            title = core.redact(title, base, fail_closed=True)
            if core.is_unavailable(title) or not title.strip():
                title = None
        m.submit(session_id, u, a, title=title)
    except Exception:  # a hook must never break the turn
        logger.warning("session-mirror: hook failed", exc_info=True)
    return None


def _flush_at_exit():
    try:
        m = _MIRROR
        if m is not None:
            m.flush(3.0)
    except Exception:
        pass


atexit.register(_flush_at_exit)


def register(ctx):
    global _CTX
    _CTX = ctx
    ctx.register_hook("post_llm_call", on_post_llm_call)
