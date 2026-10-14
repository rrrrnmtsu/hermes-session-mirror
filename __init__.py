"""session-mirror: mirror non-Discord Hermes sessions into Discord threads (post_llm_call hook)."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

try:  # package import inside Hermes; flat import when unit-tested standalone
    from . import core
except ImportError:  # pragma: no cover
    import core  # type: ignore

logger = logging.getLogger(__name__)
_CTX = None
_MIRROR = None
_DISABLED_REASON = None


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


def _get_mirror():
    global _MIRROR, _DISABLED_REASON
    if _MIRROR is not None or _DISABLED_REASON:
        return _MIRROR
    if not _setting("enabled", True):
        _DISABLED_REASON = "disabled"
        return None
    channel = str(_setting("channel_id", "") or "").strip()
    if not channel:
        _DISABLED_REASON = "channel_id not set"
        logger.warning("session-mirror: channel_id not set; inactive")
        return None
    token = _token(str(_setting("token_secret", "DISCORD_BOT_TOKEN")))
    if not token:
        _DISABLED_REASON = "no token"
        logger.warning("session-mirror: bot token unavailable; inactive")
        return None
    _MIRROR = core.Mirror(
        core.Discord(token), channel,
        core.ThreadMap(_hermes_home() / "state" / "session-mirror.json"),
        int(_setting("max_chars", 6000) or 6000),
    )
    return _MIRROR


def on_post_llm_call(session_id=None, user_message=None, assistant_response=None, platform=None, **_):
    try:
        allowed = {p.strip().lower() for p in str(_setting("platforms", "desktop,cli,acp")).split(",") if p.strip()}
        allowed.discard("discord")
        if not session_id or (platform or "").lower() not in allowed:
            return None
        m = _get_mirror()
        if m is None:
            return None
        base = _base_redactor()
        m.submit(session_id, core.redact(user_message or "", base), core.redact(assistant_response or "", base))
    except Exception:  # a hook must never break the turn
        logger.warning("session-mirror: hook failed", exc_info=True)
    return None


def register(ctx):
    global _CTX
    _CTX = ctx
    ctx.register_hook("post_llm_call", on_post_llm_call)
