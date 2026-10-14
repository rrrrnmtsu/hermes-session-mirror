import atexit
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("session_mirror_plugin", ROOT / "__init__.py")
plug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plug)

REAL_IS_ONESHOT = plug._is_oneshot


class Ctx:
    def __init__(self, **cfg):
        self.cfg, self.hooks = cfg, {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


class FakeMirror:
    def __init__(self, *a, **k):
        self.args, self.calls, self.boom = a, [], False

    def submit(self, *a, **k):
        if self.boom:
            raise RuntimeError("x")
        self.calls.append((a, k))
        return True

    def flush(self, timeout=5.0):
        self.flushed = getattr(self, "flushed", []) + [timeout]
        return True

    def close(self, timeout=2.0):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.made = []
        p = mock.patch.multiple(
            plug, _MIRROR=None, _MIRROR_KEY=None, _ACTIVE=False, _NEXT_CHECK=0.0, _WARNED_REASON=None, _MIRROR_CHANNEL=None,
            _now=lambda: self.t[0], _token=lambda n: "tok", _session_row=lambda s: (None, None), _is_oneshot=lambda p, s: s == "oneshot",
            _base_redactor=lambda: None)
        p.start(); self.addCleanup(p.stop)
        fake_core = mock.Mock()
        fake_core.redact = lambda text, base=None, fail_closed=False: text
        fake_core.is_unavailable = lambda t: t == "[redaction-unavailable]"
        fake_core.Discord = lambda token, refresh=None: ("discord", token, refresh)
        fake_core.ThreadMap = lambda path: ("map", path)

        def mk(*a):
            m = FakeMirror(*a); self.made.append(m); return m
        fake_core.Mirror = mk
        c = mock.patch.object(plug, "core", fake_core); c.start(); self.addCleanup(c.stop)

    def reg(self, **cfg):
        cfg.setdefault("channel_id", 123)
        ctx = Ctx(**cfg)
        plug.register(ctx)
        self.ctx = ctx
        return ctx.hooks["post_llm_call"]

    def submits(self):
        return [c for m in self.made for c in m.calls]


class Tests(Base):
    def test_registers_only_post_llm_call(self):
        self.reg()
        self.assertEqual(list(self.ctx.hooks), ["post_llm_call"])

    def test_platform_filter(self):
        h = self.reg()
        h("s", "hi", "yo", "discord"); h("s", "hi", "yo", "weird"); h("s", "hi", "yo", None)
        self.assertEqual(self.submits(), [])
        h("s", "hi", "yo", "desktop")
        self.assertEqual(len(self.submits()), 1)
        self.assertEqual(self.made[0].args[1], "123")  # int channel coerced

    def test_discord_cannot_be_forced_on(self):
        h = self.reg(platforms="discord,cli")
        h("s", "a", "b", "discord")
        self.assertEqual(self.submits(), [])

    def test_blank_turns_skipped(self):
        h = self.reg()
        h("s", "  ", "\n", "cli"); h("s", None, None, "cli")
        self.assertEqual(self.submits(), [])
        h("s", "", "reply", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_paused_and_reread(self):
        h = self.reg(paused=True)
        h("s", "a", "b", "cli")
        self.assertEqual(self.submits(), [])
        self.ctx.cfg["paused"] = False
        self.t[0] += 30
        h("s", "a", "b", "cli")
        self.assertEqual(self.submits(), [])  # cache still valid
        self.t[0] += 31
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)
        self.ctx.cfg["paused"] = True
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_enabled_false_reread(self):
        h = self.reg(enabled=False)
        h("s", "a", "b", "cli")
        self.assertEqual(self.submits(), [])
        self.ctx.cfg["enabled"] = True
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_missing_channel_retries_and_warns_once(self):
        h = self.reg(channel_id="")
        with self.assertLogs(plug.logger, "WARNING") as lg:
            h("s", "a", "b", "cli")
            self.t[0] += 61
            h("s", "a", "b", "cli")
        self.assertEqual(len(lg.records), 1)
        self.assertEqual(self.submits(), [])
        self.ctx.cfg["channel_id"] = "9"
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_missing_token_retries(self):
        tok = {"v": None}
        h = self.reg()
        with mock.patch.object(plug, "_token", lambda n: tok["v"]):
            h("s", "a", "b", "cli")
            self.assertEqual(self.submits(), [])
            tok["v"] = "T"
            h("s", "a", "b", "cli")
            self.assertEqual(self.submits(), [])  # within 60 s
            self.t[0] += 61
            h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_refresh_callable_reruns_token(self):
        seen = []
        captured = {}
        plug.core.Discord = lambda token, refresh=None: captured.update(t=token, r=refresh) or "d"
        h = self.reg()
        with mock.patch.object(plug, "_token", lambda n: seen.append(n) or "tok%d" % len(seen)):
            h("s", "a", "b", "cli")
            self.assertEqual(captured["t"], "tok1")
            self.assertEqual(captured["r"](), "tok2")

    def test_hook_never_raises(self):
        h = self.reg()
        h("s", "a", "b", "cli")
        self.made[0].boom = True
        self.assertIsNone(h("s", "a", "b", "cli"))
        with mock.patch.object(plug, "_get_mirror", side_effect=RuntimeError):
            self.assertIsNone(h("s", "a", "b", "cli"))

    def test_title_passed_through(self):
        h = self.reg()
        with mock.patch.object(plug, "_session_row", lambda s: ("My title", "cli")):
            h("s", "a", "b", "cli")
        self.assertEqual(self.submits()[0][1], {"title": "My title"})

    def test_redaction_applied_to_all_fields_fail_closed(self):
        calls = []

        def red(text, base=None, fail_closed=False):
            calls.append(fail_closed)
            return "R(" + text + ")"
        plug.core.redact = red
        h = self.reg()
        with mock.patch.object(plug, "_session_row", lambda s: ("T", "cli")), \
                mock.patch.object(plug, "_base_redactor", lambda: (lambda t: t)):
            h("s", "u", "a", "cli")
        self.assertEqual(self.submits()[0], (("s", "R(u)", "R(a)"), {"title": "R(T)"}))
        self.assertEqual(calls, [True, True, True])

    def test_base_none_submits_nothing_raw(self):
        # real fail-closed semantics: no base redactor -> marker
        plug.core.redact = lambda t, base=None, fail_closed=False: "[redaction-unavailable]" if base is None else t
        h = self.reg()
        with mock.patch.object(plug, "_session_row", lambda s: ("T", "cli")):
            h("s", "secret sk-abc", "reply", "cli")
        self.assertEqual(self.submits(), [])

    def test_partial_unavailable_never_raw(self):
        plug.core.redact = lambda t, base=None, fail_closed=False: "[redaction-unavailable]" if t == "bad" else t
        h = self.reg()
        with mock.patch.object(plug, "_session_row", lambda s: ("bad", "cli")):
            h("s", "bad", "ok", "cli")
        a, k = self.submits()[0]
        self.assertEqual(a[1], "[redaction-unavailable]")
        self.assertIsNone(k["title"])

    def test_default_platforms_include_tui_and_case_insensitive(self):
        h = self.reg()
        h("s", "a", "b", "tui"); h("s", "a", "b", "CLI"); h("s", "a", "b", "Desktop"); h("s", "a", "b", "ACP")
        self.assertEqual(len(self.submits()), 4)
        h("s", "a", "b", "DISCORD")
        self.assertEqual(len(self.submits()), 4)
        h2 = self.reg(platforms="Desktop, TUI")
        h2("s", "a", "b", "tui"); h2("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 5)

    def test_oneshot_skipped(self):
        h = self.reg()
        with mock.patch.object(plug, "_session_row", lambda s: (None, "oneshot")):
            h("s", "a", "b", "cli")
        self.assertEqual(self.submits(), [])
        with mock.patch.object(plug, "_session_row", lambda s: (None, "cli")):
            h("s", "a", "b", "cli")
        self.assertEqual(len(self.submits()), 1)

    def test_is_oneshot_uses_session_source_signal(self):
        real = mock.Mock(__name__="x")
        for ret, want in (("oneshot", True), ("cli", False)):
            mod = mock.Mock(session_source_for=lambda p, r=ret: r)
            with mock.patch.dict(sys.modules, {"agent": mock.Mock(), "agent.session_source": mod}):
                self.assertEqual(REAL_IS_ONESHOT("cli", "cli"), want)
        self.assertTrue(REAL_IS_ONESHOT("cli", "oneshot"))
        self.assertFalse(REAL_IS_ONESHOT("cli", None))

    def test_max_chars_reaches_mirror_and_unlimited(self):
        h = self.reg(max_chars=1234)
        h("s", "a", "b", "cli")
        self.assertEqual(self.made[0].args[3], 1234)
        self.assertEqual(plug._max_chars(), 1234)
        for v, want in ((0, plug.UNLIMITED), (-5, plug.UNLIMITED), ("x", 6000), (None, 6000)):
            self.ctx.cfg["max_chars"] = v
            self.assertEqual(plug._max_chars(), want, v)

    def test_channel_change_builds_new_mirror_and_closes_old(self):
        h = self.reg(channel_id="1")
        h("s", "a", "b", "cli")
        self.ctx.cfg["channel_id"] = "2"
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.made), 2)
        self.assertEqual(self.made[1].args[1], "2")
        self.assertTrue(getattr(self.made[0], "closed", False))
        self.assertFalse(getattr(self.made[1], "closed", False))
        self.ctx.cfg["max_chars"] = 77
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.made), 3)
        self.assertTrue(self.made[1].closed)
        self.t[0] += 61
        h("s", "a", "b", "cli")  # unchanged -> same mirror
        self.assertEqual(len(self.made), 3)

    def test_old_mirror_close_failure_swallowed(self):
        h = self.reg(channel_id="1")
        h("s", "a", "b", "cli")
        self.made[0].close = mock.Mock(side_effect=RuntimeError)
        self.ctx.cfg["channel_id"] = "2"
        self.t[0] += 61
        h("s", "a", "b", "cli")
        self.assertEqual(len(self.made), 2)

    def test_lock_exists(self):
        self.assertTrue(hasattr(plug._LOCK, "acquire"))


class AtExit(Base):
    def test_register_called_at_import(self):
        with mock.patch.object(atexit, "register") as reg:
            spec2 = importlib.util.spec_from_file_location("sm_again", ROOT / "__init__.py")
            m2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(m2)
        reg.assert_called_once_with(m2._flush_at_exit)

    def test_flush_at_exit_flushes_current_mirror(self):
        h = self.reg()
        h("s", "a", "b", "cli")
        plug._flush_at_exit()
        self.assertEqual(self.made[0].flushed, [3.0])

    def test_flush_at_exit_swallows(self):
        h = self.reg()
        h("s", "a", "b", "cli")
        self.made[0].flush = mock.Mock(side_effect=RuntimeError)
        plug._flush_at_exit()

    def test_flush_at_exit_no_mirror(self):
        plug._flush_at_exit()  # _MIRROR is None: no error


class Row(unittest.TestCase):
    def test_missing_db(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(plug, "_hermes_home", lambda: Path(d)):
            self.assertEqual(plug._session_row("s1"), (None, None))

    def test_reads_title_and_source(self):
        with tempfile.TemporaryDirectory() as d:
            con = sqlite3.connect(Path(d) / "state.db")
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT)")
            con.execute("INSERT INTO sessions VALUES ('s1','Hello','cli'),('s2',NULL,'Oneshot')")
            con.commit(); con.close()
            with mock.patch.object(plug, "_hermes_home", lambda: Path(d)):
                self.assertEqual(plug._session_row("s1"), ("Hello", "cli"))
                self.assertEqual(plug._session_row("s2"), (None, "oneshot"))
                self.assertEqual(plug._session_row("nope"), (None, None))

    def test_bad_schema(self):
        with tempfile.TemporaryDirectory() as d:
            sqlite3.connect(Path(d) / "state.db").close()
            with mock.patch.object(plug, "_hermes_home", lambda: Path(d)):
                self.assertEqual(plug._session_row("s1"), (None, None))


class FakeDiscord:
    def __init__(self):
        self.created, self.posts, self.renames = [], [], []

    def create_thread(self, ch, name):
        self.created.append((ch, name))
        return "%d" % (900 + len(self.created))

    def post(self, ch, content):
        self.posts.append((ch, content))

    def rename_thread(self, tid, name):
        self.renames.append((tid, name))

    def unarchive_thread(self, tid):
        pass


class Integration(unittest.TestCase):
    """Real core module + FakeDiscord."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fd = FakeDiscord()
        self.title = [None]
        p = mock.patch.multiple(
            plug, _MIRROR=None, _MIRROR_KEY=None, _ACTIVE=False, _NEXT_CHECK=0.0, _WARNED_REASON=None,
            _MIRROR_CHANNEL=None, _token=lambda n: "tok", _hermes_home=lambda: Path(self.tmp.name),
            _session_row=lambda s: (self.title[0], "cli"), _is_oneshot=lambda p, s: False,
            _base_redactor=lambda: (lambda t: t))
        p.start(); self.addCleanup(p.stop)
        c = mock.patch.object(plug.core, "Discord", lambda token, refresh=None: self.fd)
        c.start(); self.addCleanup(c.stop)
        ctx = Ctx(channel_id="555")
        plug.register(ctx)
        self.h = ctx.hooks["post_llm_call"]

        def fin():
            if plug._MIRROR is not None:
                plug._MIRROR.close()
        self.addCleanup(fin)

    def flush(self):
        self.assertTrue(plug._MIRROR.flush(5.0))

    def test_full_hook_creates_thread_and_posts(self):
        self.h("123", "hello user", "hello assistant", "cli")
        self.flush()
        self.assertEqual(len(self.fd.created), 1)
        self.assertEqual(self.fd.created[0][0], "555")
        text = "\n".join(c for _, c in self.fd.posts)
        self.assertIn("hello user", text)
        self.assertIn("hello assistant", text)
        self.assertGreaterEqual(len(self.fd.posts), 2)  # header + text per side
        self.assertTrue((Path(self.tmp.name) / "state" / "session-mirror.json").exists())

    def test_new_title_renames_once(self):
        self.h("123", "hi", "yo", "cli")
        self.flush()
        self.title[0] = "Better name"
        self.h("123", "again", "ok", "cli")
        self.h("123", "third", "ok", "cli")
        self.flush()
        self.assertEqual(len(self.fd.created), 1)
        self.assertEqual(len(self.fd.renames), 1)
        self.assertIn("Better name", self.fd.renames[0][1])

    def test_secret_never_posted(self):
        secret = "sk-" + "aB3dE5gH7jK9mN1pQ3sT5vW7"
        self.title[0] = "about " + secret
        self.h("123", "my key is " + secret, "you said " + secret, "cli")
        self.flush()
        self.assertTrue(self.fd.posts)
        everything = " ".join([c for _, c in self.fd.posts] + [n for _, n in self.fd.created]
                              + [n for _, n in self.fd.renames])
        self.assertNotIn(secret, everything)

    def test_no_base_redactor_posts_nothing(self):
        with mock.patch.object(plug, "_base_redactor", lambda: None):
            self.h("123", "raw text", "raw reply", "cli")
        if plug._MIRROR is not None:
            self.flush()
        self.assertEqual(self.fd.posts, [])


if __name__ == "__main__":
    unittest.main()
