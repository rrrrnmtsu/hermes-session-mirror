"""Backfill guarantees: only real conversation is replayed, re-runs never duplicate, secrets never leave."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backfill  # noqa: E402
import core  # noqa: E402


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("create table sessions(id text, source text, title text, started_at real, archived int default 0, hidden int default 0)")
    c.execute("create table messages(id integer primary key autoincrement, session_id text, role text, content text, timestamp real, active int default 1, _compressed_summary int default 0)")
    def sess(i, src, title, arch=0, hid=0):
        c.execute("insert into sessions values(?,?,?,?,?,?)", (i, src, title, float(len(i)), arch, hid))
    def msg(i, role, content, ts):
        c.execute("insert into messages(session_id,role,content,timestamp) values(?,?,?,?)", (i, role, content, ts))
    sess("s1", "desktop", "Plan"); msg("s1", "user", "hi", 1); msg("s1", "assistant", "part1", 2)
    msg("s1", "tool", "ignored tool output", 3); msg("s1", "assistant", "part2", 4)
    msg("s1", "user", "no answer yet", 5)
    msg("s1", "user", json.dumps([{"type": "text", "text": "multi"}, {"type": "image_url", "image_url": "x"}]), 6)
    msg("s1", "assistant", "ok", 7)
    sess("s2", "desktop", "Old", arch=1); msg("s2", "user", "a", 1); msg("s2", "assistant", "b", 2)
    sess("s3", "discord", "D"); msg("s3", "user", "a", 1); msg("s3", "assistant", "b", 2)
    sess("s4", "desktop", "Subagent: x"); msg("s4", "user", "a", 1); msg("s4", "assistant", "b", 2)
    sess("s5", "desktop", "Empty")
    c.commit(); c.close()


class FakeMirror:
    def __init__(self):
        self.sent = []

    def send(self, sid, user, assistant, title=None):
        self.sent.append((sid, user, assistant, title))


class FakeThreads:
    def __init__(self, known=()):
        self.known = set(known)

    def get(self, sid):
        return "1" if sid in self.known else None


class Pairing(unittest.TestCase):
    def test_tool_rows_dropped_and_assistant_parts_joined(self):
        rows = [("user", "q"), ("assistant", "a"), ("tool", "t"), ("assistant", "b")]
        self.assertEqual(backfill.pair_turns(rows), [("q", "a\n\nb")])

    def test_user_message_without_answer_is_not_a_turn(self):
        self.assertEqual(backfill.pair_turns([("user", "q")]), [])

    def test_multipart_content_keeps_only_text_parts(self):
        c = json.dumps([{"type": "text", "text": "t"}, {"type": "image_url", "image_url": "u"}])
        self.assertEqual(backfill.message_text(c), "t")


class Selection(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = Path(self.d) / "state.db"
        make_db(self.db)

    def test_default_skips_archived_other_sources_subagents_and_empty(self):
        ids = [s[0] for s in backfill.select_sessions(self.db, ["desktop"])]
        self.assertEqual(ids, ["s1"])

    def test_include_archived_adds_archived(self):
        ids = [s[0] for s in backfill.select_sessions(self.db, ["desktop"], include_archived=True)]
        self.assertEqual(ids, ["s1", "s2"])

    def test_only_and_last(self):
        self.assertEqual([s[0] for s in backfill.select_sessions(self.db, ["desktop"], True, only="s2")], ["s2"])
        self.assertEqual(len(backfill.select_sessions(self.db, ["desktop"], True, limit=1)), 1)


class Run(unittest.TestCase):
    SESS = [("s1", "T", [("q1", "a1"), ("q2", "a2")]), ("s2", None, [("q", "a")])]

    def test_known_sessions_are_skipped_so_rerun_never_duplicates(self):
        m = FakeMirror()
        st = backfill.run(self.SESS, m, FakeThreads({"s1"}), lambda t: t, log=lambda _: None)
        self.assertEqual({x[0] for x in m.sent}, {"s2"})
        self.assertEqual(st, {"sessions": 1, "turns": 1, "skipped": 1})

    def test_dry_run_posts_nothing(self):
        m = FakeMirror()
        st = backfill.run(self.SESS, m, FakeThreads(), lambda t: t, dry_run=True, log=lambda _: None)
        self.assertEqual(m.sent, [])
        self.assertEqual(st["turns"], 3)

    def test_secrets_never_reach_mirror(self):
        m = FakeMirror()
        secret = "sk-" + "a" * 30
        backfill.run([("s1", "t " + secret, [("key " + secret, "reply " + secret)])], m, FakeThreads(),
                     lambda t: t, log=lambda _: None)
        self.assertNotIn(secret, json.dumps(m.sent))

    def test_no_redactor_sends_nothing_raw(self):
        m = FakeMirror()
        backfill.run([("s1", "t", [("q", "a")])], m, FakeThreads(), None, log=lambda _: None)
        self.assertEqual(m.sent, [])


if __name__ == "__main__":
    unittest.main()
