"""What the mirror guarantees: nothing secret leaves, nothing oversize is sent, order is kept."""
import io
import json
import sys
import time
import urllib.error
import urllib.request
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402


class Redaction(unittest.TestCase):
    def test_tokens_are_masked_even_without_hermes_redactor(self):
        s = "key sk-abcdefghijklmnopqrstuv and ghp_" + "a" * 30
        out = core.redact(s)
        self.assertNotIn("sk-abcdef", out)
        self.assertNotIn("ghp_aaaa", out)

    def test_broken_base_redactor_still_falls_back(self):
        def boom(_):
            raise RuntimeError
        self.assertIn("[REDACTED]", core.redact("AKIA" + "A" * 16, boom))


class Chunking(unittest.TestCase):
    def test_no_piece_exceeds_limit_and_content_preserved(self):
        text = ("line of text\n" * 400).strip()
        parts = core.chunk(text)
        self.assertTrue(all(0 < len(p) <= core.CHUNK for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))

    def test_render_turn_headers_and_limit(self):
        parts = core.render_turn("hi", "x" * 5000, 10_000)
        self.assertTrue(parts[0].startswith("**🧑 you**"))
        self.assertTrue(parts[1].startswith("**🤖 Hermes**"))
        self.assertTrue(all(len(p) <= 2000 for p in parts))

    def test_truncation_marker(self):
        self.assertIn("truncated", core.clip("a" * 100, 10))

    def test_thread_name_first_nonblank_line_capped(self):
        self.assertEqual(core.thread_name("\n\n" + "z" * 200), "z" * 90)


class FakeDiscord:
    def __init__(self):
        self.created, self.posts, self.renames, self.unarchived = [], [], [], []
        self.fail = []  # scripted: list of (thread_id or None, DiscordError), consumed on matching post
        self.rename_error = None

    def create_thread(self, ch, name):
        self.created.append((ch, name))
        return str(1000 + len(self.created))

    def post(self, ch, content):
        for i, (tid, err) in enumerate(self.fail):
            if tid is None or tid == ch:
                del self.fail[i]
                raise err
        self.posts.append((ch, content))

    def rename_thread(self, tid, name):
        if self.rename_error:
            raise self.rename_error
        self.renames.append((tid, name))

    def unarchive_thread(self, tid):
        self.unarchived.append(tid)


class Flow(unittest.TestCase):
    def test_one_thread_per_session_posts_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            tm = core.ThreadMap(Path(d) / "m.json")
            fd = FakeDiscord()
            m = core.Mirror.__new__(core.Mirror)
            m.discord, m.channel_id, m.threads, m.max_chars = fd, "C", tm, 6000
            m.send("s1", "hello", "world")
            m.send("s1", "again", "ok")
            self.assertEqual(len(fd.created), 1)
            self.assertEqual([p[0] for p in fd.posts], ["1001"] * 4)
            self.assertIn("hello", fd.posts[0][1])
            saved = json.loads((Path(d) / "m.json").read_text())["s1"]
            self.assertEqual((saved["thread"], saved["name"]), ("1001", "hello"))


def mk_mirror(fd, tm, max_chars=6000):
    m = core.Mirror.__new__(core.Mirror)
    m.discord, m.channel_id, m.threads, m.max_chars = fd, "C", tm, max_chars
    return m


def http_err(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class FakeResp:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class ScriptedOpener:
    """Each call pops the next script item: an exception to raise or bytes to return."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.headers.get("Authorization"), req.data))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResp(item)


class SleepPatched(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self._orig = core._sleep
        core._sleep = self.slept.append

    def tearDown(self):
        core._sleep = self._orig


class DiscordErrors(SleepPatched):
    def test_http_error_becomes_discord_error_with_status_and_body(self):
        d = core.Discord("tok", ScriptedOpener(http_err(404, b"gone")))
        with self.assertRaises(core.DiscordError) as cm:
            d.post("1", "x")
        self.assertEqual((cm.exception.status, cm.exception.body), (404, "gone"))

    def test_network_error_on_patch_is_status_zero_after_three_attempts(self):
        op = ScriptedOpener(*[urllib.error.URLError("down")] * 3)
        with self.assertRaises(core.DiscordError) as cm:
            core.Discord("t", op).rename_thread("1", "x")
        self.assertEqual(cm.exception.status, 0)
        self.assertEqual(len(op.calls), 3)

    def test_timeout_is_status_zero(self):
        op = ScriptedOpener(*[TimeoutError("t")] * 3)
        with self.assertRaises(core.DiscordError) as cm:
            core.Discord("t", op).rename_thread("1", "x")
        self.assertEqual(cm.exception.status, 0)

    def test_500_is_retried_then_succeeds_without_real_sleep(self):
        op = ScriptedOpener(http_err(500), http_err(502), b"{}")
        core.Discord("t", op).rename_thread("1", "x")
        self.assertEqual(len(op.calls), 3)
        self.assertEqual(len(self.slept), 2)

    def test_500_gives_up_after_three_attempts(self):
        op = ScriptedOpener(http_err(500), http_err(500), http_err(500), b"{}")
        with self.assertRaises(core.DiscordError):
            core.Discord("t", op).rename_thread("1", "x")
        self.assertEqual(len(op.calls), 3)

    def test_4xx_other_than_401_429_is_not_retried(self):
        op = ScriptedOpener(http_err(400), b"{}")
        with self.assertRaises(core.DiscordError):
            core.Discord("t", op).post("1", "x")
        self.assertEqual(len(op.calls), 1)

    def test_429_honours_retry_after_capped_at_10s_and_retries(self):
        op = ScriptedOpener(http_err(429, b'{"retry_after": 99}'), b"{}")
        core.Discord("t", op).post("1", "x")
        self.assertEqual(self.slept, [10])
        self.assertEqual(len(op.calls), 2)

    def test_429_short_retry_after_is_used_as_is(self):
        op = ScriptedOpener(http_err(429, b'{"retry_after": 0.5}'), b"{}")
        core.Discord("t", op).post("1", "x")
        self.assertEqual(self.slept, [0.5])


class DiscordRefresh(SleepPatched):
    def test_401_refreshes_token_once_and_retries_with_new_token(self):
        op = ScriptedOpener(http_err(401), b"{}")
        calls = []
        d = core.Discord("old", op, refresh=lambda: calls.append(1) or "new")
        d.post("1", "x")
        self.assertEqual(len(calls), 1)
        self.assertEqual([c[2] for c in op.calls], ["Bot old", "Bot new"])

    def test_401_twice_raises_and_refreshes_only_once(self):
        op = ScriptedOpener(http_err(401), http_err(401))
        calls = []
        d = core.Discord("old", op, refresh=lambda: calls.append(1) or "new")
        with self.assertRaises(core.DiscordError) as cm:
            d.post("1", "x")
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(len(calls), 1)

    def test_401_without_refresh_raises_immediately(self):
        op = ScriptedOpener(http_err(401))
        with self.assertRaises(core.DiscordError):
            core.Discord("old", op).post("1", "x")
        self.assertEqual(len(op.calls), 1)

    def test_401_when_refresh_returns_none_raises(self):
        op = ScriptedOpener(http_err(401))
        with self.assertRaises(core.DiscordError):
            core.Discord("old", op, refresh=lambda: None).post("1", "x")
        self.assertEqual(len(op.calls), 1)


class DiscordMethods(SleepPatched):
    def test_create_thread_returns_id_string_and_caps_name_at_100(self):
        op = ScriptedOpener(b'{"id": 123}')
        tid = core.Discord("t", op).create_thread("5", "n" * 300)
        self.assertEqual(tid, "123")
        self.assertEqual(len(json.loads(op.calls[0][3])["name"]), 100)

    def test_rename_thread_patches_channel_with_capped_name(self):
        op = ScriptedOpener(b"{}")
        core.Discord("t", op).rename_thread("9", "r" * 300)
        method, url, _, data = op.calls[0]
        self.assertEqual((method, url.endswith("/channels/9")), ("PATCH", True))
        self.assertEqual(json.loads(data), {"name": "r" * 100})

    def test_unarchive_thread_patches_archived_false(self):
        op = ScriptedOpener(b"{}")
        core.Discord("t", op).unarchive_thread("9")
        self.assertEqual(op.calls[0][0], "PATCH")
        self.assertEqual(json.loads(op.calls[0][3]), {"archived": False})

    def test_post_disables_mentions(self):
        op = ScriptedOpener(b"{}")
        core.Discord("t", op).post("9", "@everyone")
        self.assertEqual(json.loads(op.calls[0][3])["allowed_mentions"], {"parse": []})


class SelfHealing(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tm = core.ThreadMap(Path(self._d.name) / "m.json")
        self.fd = FakeDiscord()
        self.m = mk_mirror(self.fd, self.tm)

    def tearDown(self):
        self._d.cleanup()

    def test_deleted_thread_404_creates_fresh_thread_and_remaps(self):
        self.m.send("s", "one", "two")  # T1
        self.fd.fail = [("1001", core.DiscordError(404, "Unknown Channel"))]
        self.m.send("s", "three", "four")
        self.assertEqual(len(self.fd.created), 2)
        self.assertEqual(self.tm.get("s"), "1002")
        self.assertEqual([p[0] for p in self.fd.posts[-2:]], ["1002", "1002"])

    def test_archived_thread_403_is_unarchived_then_retried_without_duplicates(self):
        self.m.send("s", "one", "two")
        self.fd.fail = [("1001", core.DiscordError(403, "Missing Access"))]
        n = len(self.fd.posts)
        self.m.send("s", "three", "four")
        self.assertEqual(self.fd.unarchived, ["1001"])
        self.assertEqual(len(self.fd.posts) - n, 2)
        self.assertEqual(len(self.fd.created), 1)

    def test_error_body_mentioning_archived_triggers_unarchive(self):
        self.m.send("s", "one", "two")
        self.fd.fail = [("1001", core.DiscordError(400, "Thread is archived"))]
        self.m.send("s", "a", "b")
        self.assertEqual(self.fd.unarchived, ["1001"])

    def test_retry_after_failure_mid_turn_never_reposts_earlier_chunks(self):
        long_user = "\n".join("u%03d" % i + "x" * 90 for i in range(60))  # several chunks
        parts = core.render_turn(long_user, "reply", 100000)
        self.assertGreater(len(parts), 3)
        self.m.max_chars = 100000
        self.m.send("s", "seed", "")  # create T1
        n = len(self.fd.posts)
        calls = {"n": 0}
        orig = self.fd.post

        def flaky(ch, content):
            calls["n"] += 1
            if calls["n"] == 3:
                raise core.DiscordError(403, "archived")
            orig(ch, content)
        self.fd.post = flaky
        self.m.send("s", long_user, "reply")
        sent = [p[1] for p in self.fd.posts[n:]]
        self.assertEqual(sent, parts)  # exactly once, in order

    def test_404_mid_turn_sends_whole_turn_to_new_thread_header_included(self):
        long_user = "\n".join("u%03d" % i + "x" * 90 for i in range(60))
        self.m.max_chars = 100000
        parts = core.render_turn(long_user, "r", 100000)
        self.m.send("s", "seed", "")
        n = len(self.fd.posts)
        calls = {"n": 0}
        orig = self.fd.post

        def flaky(ch, content):
            calls["n"] += 1
            if calls["n"] == 2:
                raise core.DiscordError(404, "gone")
            orig(ch, content)
        self.fd.post = flaky
        self.m.send("s", long_user, "r")
        new_thread = self.tm.get("s")
        self.assertEqual(new_thread, "1002")
        self.assertEqual([c for ch, c in self.fd.posts[n:] if ch == new_thread], parts)
        self.assertTrue(parts[0].startswith("**🧑 you**"))
        self.assertEqual(self.fd.created[-1][1], core.thread_name_for(None, long_user))

    def test_persistent_404_stops_after_cap_with_three_creates_for_new_session(self):
        self.fd.fail = [(None, core.DiscordError(404, "gone"))] * 10
        with self.assertRaises(core.DiscordError) as cm:
            self.m.send("s", "one", "two")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(self.fd.created), 3)  # initial + 2 recreations

    def test_persistent_404_on_mapped_thread_creates_exactly_cap_new_threads(self):
        self.m.send("s", "one", "two")
        self.assertEqual(len(self.fd.created), 1)
        self.fd.fail = [(None, core.DiscordError(404, "gone"))] * 10
        with self.assertRaises(core.DiscordError):
            self.m.send("s", "a", "b")
        self.assertEqual(len(self.fd.created), 3)

    def test_unrecoverable_error_propagates(self):
        self.m.send("s", "one", "two")
        self.fd.fail = [("1001", core.DiscordError(400, "bad"))]
        with self.assertRaises(core.DiscordError):
            self.m.send("s", "a", "b")

    def test_repeated_403_does_not_loop_forever(self):
        self.m.send("s", "one", "two")
        self.fd.fail = [("1001", core.DiscordError(403, "x")), ("1001", core.DiscordError(403, "x"))]
        with self.assertRaises(core.DiscordError):
            self.m.send("s", "a", "b")
        self.assertEqual(self.fd.unarchived, ["1001"])


class FenceChunking(unittest.TestCase):
    def _unclosed(self, piece):
        return core._fence_after(piece, None) is not None

    def test_big_fenced_block_is_split_with_every_piece_balanced(self):
        code = "\n".join("print(%d)  # %s" % (i, "y" * 40) for i in range(120))
        text = "intro\n```python\n" + code + "\n```\noutro"
        self.assertGreater(len(text), 5000)
        parts = core.chunk(text)
        self.assertGreater(len(parts), 2)
        for p in parts:
            self.assertLessEqual(len(p), core.CHUNK)
            self.assertFalse(self._unclosed(p), p[-30:])
        for p in parts[1:-1]:
            self.assertTrue(p.startswith("```python\n"))
        # no code line lost
        for i in range(120):
            self.assertTrue(any("print(%d)  #" % i in p for p in parts))

    def test_reopened_fence_keeps_language_tag(self):
        text = "```rust\n" + "\n".join("let a%d = 1;" % i for i in range(400)) + "\n```"
        parts = core.chunk(text)
        self.assertTrue(all(p.startswith("```rust\n") for p in parts))
        self.assertTrue(all(p.endswith("```") for p in parts))

    def test_unclosed_fence_in_input_is_closed(self):
        parts = core.chunk("```\nabc")
        self.assertEqual(parts, ["```\nabc\n```"])

    def test_text_without_fences_is_unchanged_by_fence_logic(self):
        self.assertEqual(core.chunk("hello"), ["hello"])

    def test_fence_closing_between_pieces_does_not_reopen(self):
        text = "```\nA\n```\n" + "\n".join("line %d" % i for i in range(500))
        parts = core.chunk(text)
        self.assertFalse(parts[1].startswith("```"))

    def test_rendered_turn_with_big_fence_stays_under_discord_limit(self):
        code = "\n".join("x = %d" % i for i in range(2000))
        parts = core.render_turn("q", "```py\n" + code + "\n```", 100000)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertTrue(all(not self._unclosed(p.split("\n", 1)[1] if p.startswith("**") else p) for p in parts))


class RoleLabelling(unittest.TestCase):
    def test_system_prefixes_are_detected_after_lstrip(self):
        for pre in ["[Cronjob x]", "[SYSTEM: a]", "[IMPORTANT: a]", "[System note]", "[Note: a]",
                    "[CONTEXT COMPACTION] a", "[Background x]", "<system-reminder>a"]:
            self.assertTrue(core.is_system_text("  \n" + pre), pre)

    def test_human_text_is_not_system(self):
        for t in ["hello", "", "see [SYSTEM] docs", "[link](x)"]:
            self.assertFalse(core.is_system_text(t), t)

    def test_render_turn_labels_system_text_as_system_not_you(self):
        parts = core.render_turn("[Cronjob: daily]\nrun it", "done", 1000)
        self.assertTrue(parts[0].startswith("**⚙️ system**"))
        self.assertNotIn("🧑", parts[0])
        self.assertTrue(parts[1].startswith("**🤖 Hermes**"))

    def test_thread_name_skips_system_text(self):
        self.assertEqual(core.thread_name("[SYSTEM: x]\n\nFix the login bug"), "Fix the login bug")

    def test_thread_name_falls_back_when_only_system_text(self):
        self.assertEqual(core.thread_name("[Cronjob: x]\nrun"), "Hermes session")
        self.assertEqual(core.thread_name(""), "Hermes session")

    def test_thread_name_for_prefers_real_title_capped_at_90(self):
        self.assertEqual(core.thread_name_for("  My Title  ", "hello"), "My Title")
        self.assertEqual(core.thread_name_for("t" * 200, "hello"), "t" * 90)

    def test_thread_name_for_ignores_empty_or_generic_title(self):
        self.assertEqual(core.thread_name_for(None, "hello"), "hello")
        self.assertEqual(core.thread_name_for("  ", "hello"), "hello")
        self.assertEqual(core.thread_name_for("New Session", "hello"), "hello")
        self.assertEqual(core.thread_name_for("Untitled", "[SYSTEM x]"), "Hermes session")


class ThreadMapFormat(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.path = Path(self._d.name) / "m.json"

    def tearDown(self):
        self._d.cleanup()

    def test_legacy_string_values_are_read_transparently(self):
        self.path.write_text(json.dumps({"s1": "1009"}))
        tm = core.ThreadMap(self.path)
        self.assertEqual(tm.get("s1"), "1009")
        self.assertIsNone(tm.get_name("s1"))

    def test_set_stores_dict_with_thread_name_ts(self):
        tm = core.ThreadMap(self.path)
        tm.set("s", "1001", "nm")
        e = json.loads(self.path.read_text())["s"]
        self.assertEqual((e["thread"], e["name"]), ("1001", "nm"))
        self.assertIsInstance(e["ts"], float)
        self.assertEqual((tm.get("s"), tm.get_name("s")), ("1001", "nm"))

    def test_set_keeps_legacy_entries_when_writing(self):
        self.path.write_text(json.dumps({"old": "1000"}))
        tm = core.ThreadMap(self.path)
        tm.set("new", "1001")
        self.assertEqual((tm.get("old"), tm.get("new")), ("1000", "1001"))

    def test_drop_removes_entry_and_missing_drop_is_harmless(self):
        tm = core.ThreadMap(self.path)
        tm.set("s", "1001")
        tm.drop("s")
        tm.drop("nope")
        self.assertIsNone(tm.get("s"))

    def test_get_unknown_and_corrupt_file_return_none(self):
        self.path.write_text("{not json")
        tm = core.ThreadMap(self.path)
        self.assertIsNone(tm.get("x"))
        self.assertIsNone(tm.get_name("x"))

    def test_prunes_to_500_evicting_oldest_by_ts(self):
        old = {"s%d" % i: {"thread": str(1000 + i), "name": None, "ts": float(i + 1)} for i in range(500)}
        self.path.write_text(json.dumps(old))
        tm = core.ThreadMap(self.path)
        tm.set("fresh", "2001")
        d = json.loads(self.path.read_text())
        self.assertEqual(len(d), 500)
        self.assertNotIn("s0", d)
        self.assertIn("s1", d)
        self.assertIn("fresh", d)

    def test_legacy_entries_are_evicted_first(self):
        data = {"leg%d" % i: "1000" for i in range(500)}
        data["recent"] = {"thread": "2002", "name": None, "ts": 5.0}
        self.path.write_text(json.dumps(data))
        tm = core.ThreadMap(self.path)
        tm.set("fresh", "2001")
        d = json.loads(self.path.read_text())
        self.assertEqual(len(d), 500)
        self.assertIn("recent", d)

    def test_write_is_atomic_no_tmp_left_behind(self):
        tm = core.ThreadMap(self.path)
        tm.set("s", "1001")
        self.assertFalse(self.path.with_suffix(".tmp").exists())


class MirrorTitles(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tm = core.ThreadMap(Path(self._d.name) / "m.json")
        self.fd = FakeDiscord()
        self.m = mk_mirror(self.fd, self.tm)

    def tearDown(self):
        self._d.cleanup()

    def test_new_thread_is_named_from_title_when_given(self):
        self.m.send("s", "hello", "hi", title="Deploy fix")
        self.assertEqual(self.fd.created[0][1], "Deploy fix")
        self.assertEqual(self.tm.get_name("s"), "Deploy fix")

    def test_new_thread_falls_back_to_first_message_without_title(self):
        self.m.send("s", "hello there", "hi")
        self.assertEqual(self.fd.created[0][1], "hello there")

    def test_changed_title_renames_existing_thread_and_stores_name(self):
        self.m.send("s", "hello", "hi", title="A")
        self.m.send("s", "more", "ok", title="B")
        self.assertEqual(self.fd.renames, [("1001", "B")])
        self.assertEqual(self.tm.get_name("s"), "B")

    def test_same_title_does_not_rename(self):
        self.m.send("s", "hello", "hi", title="A")
        self.m.send("s", "more", "ok", title="A")
        self.assertEqual(self.fd.renames, [])

    def test_no_title_never_renames(self):
        self.m.send("s", "hello", "hi", title="A")
        self.m.send("s", "more", "ok")
        self.assertEqual(self.fd.renames, [])

    def test_rename_failure_is_swallowed_and_posting_continues(self):
        self.m.send("s", "hello", "hi", title="A")
        self.fd.rename_error = core.DiscordError(500, "boom")
        n = len(self.fd.posts)
        self.m.send("s", "more", "ok", title="B")
        self.assertEqual(len(self.fd.posts) - n, 2)


class MirrorQueue(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tm = core.ThreadMap(Path(self._d.name) / "m.json")

    def tearDown(self):
        self._d.cleanup()

    def test_flush_returns_true_after_queued_items_are_sent(self):
        fd = FakeDiscord()
        m = core.Mirror(fd, "C", self.tm, 6000)
        self.assertTrue(m.submit("s", "hi", "yo", title="T"))
        self.assertTrue(m.submit("s", "hi2", "yo2"))
        self.assertTrue(m.flush(5))
        self.assertEqual(len(fd.posts), 4)
        self.assertEqual(fd.created[0][1], "T")

    def test_flush_on_idle_mirror_is_true(self):
        self.assertTrue(core.Mirror(FakeDiscord(), "C", self.tm, 10).flush(1))

    def test_flush_times_out_false_when_sender_is_stuck(self):
        fd = FakeDiscord()
        gate = __import__("threading").Event()
        orig = fd.create_thread
        fd.create_thread = lambda c, n: (gate.wait(5), orig(c, n))[1]
        m = core.Mirror(fd, "C", self.tm, 100)
        m.submit("s", "a", "b")
        self.assertFalse(m.flush(0.2))
        gate.set()
        self.assertTrue(m.flush(5))

    def test_worker_survives_errors_and_processes_next_item(self):
        fd = FakeDiscord()
        fd.fail = [(None, core.DiscordError(400, "bad"))]
        m = core.Mirror(fd, "C", self.tm, 6000)
        m.submit("s1", "first", "x")
        m.submit("s2", "second", "y")
        self.assertTrue(m.flush(5))
        self.assertTrue(any("second" in p[1] for p in fd.posts))

    def test_worker_survives_unexpected_exception_types(self):
        fd = FakeDiscord()
        fd.fail = [(None, RuntimeError("weird"))]
        m = core.Mirror(fd, "C", self.tm, 6000)
        m.submit("s1", "first", "x")
        m.submit("s2", "second", "y")
        self.assertTrue(m.flush(5))
        self.assertTrue(any("second" in p[1] for p in fd.posts))

    def test_submit_returns_false_when_queue_full(self):
        m = core.Mirror.__new__(core.Mirror)
        m.q = core.queue.Queue(maxsize=1)
        self.assertTrue(m.submit("s", "a", "b"))
        self.assertFalse(m.submit("s", "a", "b"))


class PostIdempotency(SleepPatched):
    def test_post_is_not_retried_on_status_zero(self):
        op = ScriptedOpener(urllib.error.URLError("reset"), b"{}")
        with self.assertRaises(core.DiscordError) as cm:
            core.Discord("t", op).post("1", "x")
        self.assertEqual((cm.exception.status, len(op.calls)), (0, 1))

    def test_post_is_not_retried_on_500(self):
        op = ScriptedOpener(http_err(500), b"{}")
        with self.assertRaises(core.DiscordError):
            core.Discord("t", op).post("1", "x")
        self.assertEqual(len(op.calls), 1)

    def test_post_is_retried_on_502_503_504(self):
        op = ScriptedOpener(http_err(502), http_err(503), b"{}")
        core.Discord("t", op).post("1", "x")
        self.assertEqual(len(op.calls), 3)
        op = ScriptedOpener(http_err(504), b"{}")
        core.Discord("t", op).post("1", "x")
        self.assertEqual(len(op.calls), 2)

    def test_post_retried_on_429(self):
        op = ScriptedOpener(http_err(429, b'{"retry_after": 1}'), b"{}")
        core.Discord("t", op).post("1", "x")
        self.assertEqual(len(op.calls), 2)

    def test_post_nonce_is_short_enforced_and_reused_across_retries(self):
        op = ScriptedOpener(http_err(502), b"{}")
        core.Discord("t", op).post("1", "x")
        bodies = [json.loads(c[3]) for c in op.calls]
        self.assertEqual(bodies[0]["nonce"], bodies[1]["nonce"])
        self.assertIsInstance(bodies[0]["nonce"], str)
        self.assertLessEqual(len(bodies[0]["nonce"]), 25)
        self.assertTrue(bodies[0]["enforce_nonce"])

    def test_post_nonce_differs_between_calls(self):
        op = ScriptedOpener(b"{}", b"{}")
        d = core.Discord("t", op)
        d.post("1", "x")
        d.post("1", "x")
        self.assertNotEqual(json.loads(op.calls[0][3])["nonce"], json.loads(op.calls[1][3])["nonce"])

    def test_create_thread_not_retried_on_status_zero_or_500(self):
        for err in (urllib.error.URLError("x"), http_err(500)):
            op = ScriptedOpener(err, b'{"id": 5}')
            with self.assertRaises(core.DiscordError):
                core.Discord("t", op).create_thread("3", "n")
            self.assertEqual(len(op.calls), 1)

    def test_user_agent_version_is_0_2(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req.headers.get("User-agent"))
            return FakeResp()
        core.Discord("t", opener).post("1", "x")
        self.assertIn("0.2", seen[0])


class IdValidation(SleepPatched):
    def test_non_numeric_ids_raise_before_any_request(self):
        op = ScriptedOpener(b"{}")
        d = core.Discord("t", op)
        for call in (lambda: d.create_thread("C", "n"), lambda: d.post("T/../x", "c"),
                     lambda: d.rename_thread("abc", "n"), lambda: d.unarchive_thread(""),
                     lambda: d.post("1" * 26, "c")):
            with self.assertRaises(core.DiscordError) as cm:
                call()
            self.assertEqual((cm.exception.status, cm.exception.body), (0, "invalid id"))
        self.assertEqual(op.calls, [])

    def test_numeric_id_is_accepted(self):
        op = ScriptedOpener(b"{}")
        core.Discord("t", op).post("123456789012345678", "c")
        self.assertEqual(len(op.calls), 1)


class RenameStorm(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tm = core.ThreadMap(Path(self._d.name) / "m.json")
        self.fd = FakeDiscord()
        self.m = mk_mirror(self.fd, self.tm)

    def tearDown(self):
        self._d.cleanup()

    def test_is_generic_title(self):
        for t in (None, "", "  ", "New Session", "untitled", "\u200b", "Hermes\nsession"):
            self.assertTrue(core.is_generic_title(t), repr(t))
        self.assertFalse(core.is_generic_title("Deploy fix"))

    def test_generic_title_never_renames(self):
        self.m.send("s", "hello", "hi", title="Real")
        self.m.send("s", "more", "ok", title="New Session")
        self.m.send("s", "more", "ok", title="  ")
        self.assertEqual(self.fd.renames, [])

    def test_failing_rename_is_not_retried_every_turn(self):
        self.m.send("s", "hello", "hi", title="A")
        self.fd.rename_error = core.DiscordError(429, "slow")
        self.m.send("s", "x", "y", title="B")
        self.fd.rename_error = None
        self.m.send("s", "x", "y", title="B")
        self.assertEqual(self.fd.renames, [])
        self.assertEqual(self.tm.get_name("s"), "B")

    def test_title_equal_after_normalisation_does_not_rename(self):
        self.m.send("s", "hello", "hi", title="A  b\nc")
        self.m.send("s", "x", "y", title="A b c")
        self.assertEqual(self.fd.renames, [])


class RedactionFailClosed(unittest.TestCase):
    def test_fail_closed_without_base_returns_marker(self):
        self.assertEqual(core.redact("secret", None, fail_closed=True), core.REDACTION_UNAVAILABLE)
        self.assertEqual(core.REDACTION_UNAVAILABLE, "[redaction-unavailable]")

    def test_fail_closed_with_raising_base_returns_marker(self):
        def boom(_):
            raise RuntimeError
        self.assertEqual(core.redact("secret", boom, fail_closed=True), core.REDACTION_UNAVAILABLE)

    def test_fail_closed_with_working_base_redacts_normally(self):
        self.assertEqual(core.redact("abc", lambda t: t.upper(), fail_closed=True), "ABC")

    def test_default_stays_fail_open(self):
        self.assertEqual(core.redact("plain"), "plain")

    def test_is_unavailable(self):
        c = core.REDACTION_UNAVAILABLE
        self.assertTrue(core.is_unavailable(c))
        self.assertTrue(core.is_unavailable("  " + c + "\n" + c))
        self.assertFalse(core.is_unavailable("hi " + c))
        self.assertFalse(core.is_unavailable(""))

    def test_mirror_skips_unavailable_side_and_posts_the_other(self):
        with tempfile.TemporaryDirectory() as d:
            fd = FakeDiscord()
            m = mk_mirror(fd, core.ThreadMap(Path(d) / "m.json"))
            m.send("s", core.REDACTION_UNAVAILABLE, "answer")
            self.assertEqual(len(fd.posts), 1)
            self.assertTrue(fd.posts[0][1].startswith("**🤖 Hermes**"))

    def test_mirror_posts_nothing_and_creates_no_thread_when_both_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            fd = FakeDiscord()
            m = mk_mirror(fd, core.ThreadMap(Path(d) / "m.json"))
            m.send("s", core.REDACTION_UNAVAILABLE, core.REDACTION_UNAVAILABLE)
            m.send("s", "", "  ")
            self.assertEqual((fd.posts, fd.created), ([], []))


class Sanitisation(unittest.TestCase):
    def test_title_newlines_collapse_to_one_line(self):
        self.assertEqual(core.thread_name_for("a\n\n b\t c", "x"), "a b c")

    def test_control_and_bidi_chars_are_stripped(self):
        self.assertEqual(core.thread_name_for("a\x00b\x7fc\u202ed\u2066e", "x"), "abcde")
        self.assertEqual(core.thread_name("he\x1bllo"), "hello")

    def test_blank_after_invisible_stripping_falls_back(self):
        self.assertEqual(core.thread_name("\u200b\u00a0\ufeff"), "Hermes session")
        self.assertEqual(core.thread_name_for("\u200b\u200c", "\u200d"), "Hermes session")

    def test_at_sign_is_neutralised_in_names(self):
        self.assertEqual(core.thread_name("ping @everyone"), "ping @\u200beveryone")
        self.assertEqual(core.thread_name_for("hi @here", "x"), "hi @\u200bhere")

    def test_render_turn_defangs_raw_mentions(self):
        parts = core.render_turn("hey <@123> <#456>", "<@&9> ok", 1000)
        self.assertIn("<\u200b@123>", parts[0])
        self.assertIn("<\u200b#456>", parts[0])
        self.assertIn("<\u200b@&9>", parts[1])
        self.assertNotIn("<@", "".join(parts))


class FenceHardening(unittest.TestCase):
    def test_long_language_tag_is_capped_so_pieces_stay_within_size(self):
        tag = "x" * 500
        text = "```" + tag + "\n" + "\n".join("l%d" % i for i in range(2000)) + "\n```"
        parts = core.chunk(text, 300)
        self.assertTrue(all(len(p) <= 300 for p in parts))
        self.assertTrue(parts[1].startswith("```" + "x" * 20 + "\n"))

    def test_tilde_fence_is_reopened_with_tildes(self):
        text = "~~~sh\n" + "\n".join("echo %d" % i for i in range(400)) + "\n~~~"
        parts = core.chunk(text, 500)
        self.assertGreater(len(parts), 2)
        for p in parts:
            self.assertTrue(p.startswith("~~~sh\n"), p[:10])
            self.assertTrue(p.endswith("\n~~~"))
            self.assertLessEqual(len(p), 500)

    def test_tilde_inside_backtick_fence_does_not_close_it(self):
        self.assertIsNotNone(core._fence_after("```\n~~~\nx", None))
        self.assertIsNone(core._fence_after("~~~\nx\n~~~", None))

    def test_indented_fence_is_still_recognised(self):
        parts = core.chunk("   ```py\n" + "\n".join("a%d" % i for i in range(300)) + "\n   ```", 300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(p.startswith("```py\n") for p in parts[1:]))
        self.assertTrue(all(p.endswith("```") for p in parts))


class ThreadMapRobustness(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.path = Path(self._d.name) / "m.json"

    def tearDown(self):
        self._d.cleanup()

    def test_bad_entries_are_skipped_without_raising(self):
        self.path.write_text(json.dumps({
            "ok": {"thread": "11", "name": "n", "ts": 1.0},
            "badts": {"thread": "12", "name": None, "ts": "yesterday"},
            "list": [1, 2], "num": 5, "nothread": {"name": "x", "ts": 1},
            "nonnumeric": {"thread": "T1", "ts": 1}, "legacybad": "abc"}))
        tm = core.ThreadMap(self.path)
        self.assertEqual(tm.get("ok"), "11")
        for k in ("badts", "list", "num", "nothread", "nonnumeric", "legacybad"):
            self.assertIsNone(tm.get(k), k)

    def test_corrupt_json_is_moved_aside_not_overwritten(self):
        self.path.write_text("{not json")
        tm = core.ThreadMap(self.path)
        tm.set("s", "5")
        corrupt = self.path.with_name("m.json.corrupt")
        self.assertEqual(corrupt.read_text(), "{not json")
        self.assertEqual(tm.get("s"), "5")

    def test_second_corruption_overwrites_previous_corrupt_file(self):
        corrupt = self.path.with_name("m.json.corrupt")
        corrupt.write_text("old")
        self.path.write_text("[1,2")
        core.ThreadMap(self.path).get("x")
        self.assertEqual(corrupt.read_text(), "[1,2")

    def test_non_dict_top_level_is_treated_as_corrupt(self):
        self.path.write_text("[]")
        self.assertIsNone(core.ThreadMap(self.path).get("x"))
        self.assertTrue(self.path.with_name("m.json.corrupt").exists())

    def test_file_is_private_0600(self):
        core.ThreadMap(self.path).set("s", "5")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_tmp_files_left_and_lock_sidecar_used(self):
        core.ThreadMap(self.path).set("s", "5")
        names = sorted(p.name for p in Path(self._d.name).iterdir())
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)
        if core.fcntl is not None:
            self.assertIn("m.json.lock", names)

    def test_two_instances_many_threads_lose_no_updates(self):
        import threading
        maps = [core.ThreadMap(self.path), core.ThreadMap(self.path)]
        errors = []

        def work(n):
            try:
                for i in range(15):
                    maps[n % 2].set("k%d-%d" % (n, i), str(1000 + n * 100 + i))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        d = json.loads(self.path.read_text())
        self.assertEqual(len(d), 90)


class MirrorClose(unittest.TestCase):
    def test_close_flushes_stops_worker_and_blocks_submit(self):
        with tempfile.TemporaryDirectory() as d:
            fd = FakeDiscord()
            m = core.Mirror(fd, "C", core.ThreadMap(Path(d) / "m.json"), 6000)
            self.assertTrue(m.submit("s", "hi", "yo"))
            self.assertTrue(m.close(2.0))
            self.assertEqual(len(fd.posts), 2)
            self.assertFalse(m._thread.is_alive())
            self.assertFalse(m.submit("s", "again", "x"))
            self.assertTrue(m.close(0.5))  # idempotent




class UsageFooterAndAttachmentNames(unittest.TestCase):
    FOOTER = "answer\n\n<details><summary>\U0001F4CAusage $0.001</summary>\n\n| a | b |\n\n</details>"

    def test_usage_footer_is_not_mirrored(self):
        out = "\n".join(core.render_turn("q", self.FOOTER, 6000))
        self.assertIn("answer", out)
        self.assertNotIn("usage", out)
        self.assertNotIn("<details", out)

    def test_footer_only_assistant_text_posts_no_assistant_part(self):
        parts = core.render_turn("q", "<details><summary>\U0001F4CAusage</summary>x</details>", 6000)
        self.assertEqual(len(parts), 1)

    def test_text_without_footer_is_untouched(self):
        self.assertEqual(core.strip_usage_footer("plain <details>x</details>"), "plain <details>x</details>")

    def test_attachment_only_first_line_falls_back_to_generic_name(self):
        self.assertEqual(core.thread_name("@file:.hermes/composer-pastes/pasted_1.txt"), core.FALLBACK_NAME)

    def test_attachment_line_is_skipped_for_thread_name(self):
        self.assertEqual(core.thread_name("@file:x/y.txt\nreal question"), "real question")


if __name__ == "__main__":
    unittest.main()
