"""Documented limits: best-effort delivery, bounded queue, bounded thread map, no persistent retry."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backfill  # noqa: E402
import core  # noqa: E402


class CountingDiscord:
    def __init__(self):
        self.created, self.posts = [], []

    def create_thread(self, channel, name):
        self.created.append(name)
        return str(1000 + len(self.created))

    def post(self, tid, content):
        self.posts.append((tid, content))

    def rename_thread(self, tid, name):
        pass

    def unarchive_thread(self, tid):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.tm = core.ThreadMap(Path(self._d.name) / "m.json")
        backfill._sleep = lambda _s: None


class QueueLimit(Base):
    def test_full_queue_of_200_drops_new_turns_and_never_resends_them(self):
        gate = threading.Event()
        fd = CountingDiscord()
        orig = fd.create_thread
        fd.create_thread = lambda c, n: (gate.wait(10), orig(c, n))[1]  # worker stuck on item 1
        m = core.Mirror(fd, "C", self.tm, 6000)
        accepted = [m.submit("s", "u%d" % i, "a") for i in range(300)]
        # one item is held by the worker, so 200 queued + 1 in flight = 201 accepted at most
        self.assertLessEqual(sum(accepted), 201)
        self.assertGreaterEqual(sum(accepted), 200)
        self.assertFalse(accepted[-1])
        gate.set()
        self.assertTrue(m.flush(10))
        sent_users = {c.split("\n", 1)[1] for _, c in fd.posts if c.startswith("**🧑 you**")}
        self.assertNotIn("u299", sent_users)  # dropped turn is gone for good
        self.assertEqual(len(sent_users), sum(accepted))
        m.close()


class ThreadMapLimit(Base):
    def test_backfill_remirrors_session_evicted_from_full_map(self):
        for i in range(core.MAX_ENTRIES):
            self.tm.set("old%d" % i, str(2000 + i))
        sess = [("old0", "T", [("q", "a")])]
        fd = CountingDiscord()
        m = core.Mirror(fd, "C", self.tm, 6000)
        self.assertIsNotNone(self.tm.get("old0"))
        st = backfill.run(sess, m, self.tm, lambda t: t, log=lambda _: None)
        self.assertEqual(st["skipped"], 1)  # still in the map -> skipped
        self.tm.set("newer", "9999")  # 501st entry evicts the oldest (old0)
        self.assertIsNone(self.tm.get("old0"))
        st = backfill.run(sess, m, self.tm, lambda t: t, log=lambda _: None)
        self.assertEqual(st["skipped"], 0)  # dedupe is lost: a second thread is created
        self.assertEqual(len(fd.created), 1)
        self.assertEqual(len(self.tm._load()), core.MAX_ENTRIES)


class InterruptedBackfill(Base):
    def test_failure_midway_leaves_partial_thread_and_rerun_does_not_resume_it(self):
        class FailsOnSecondTurn(CountingDiscord):
            def post(self, tid, content):
                if "q2" in content:
                    raise core.DiscordError(400, "bad")
                super().post(tid, content)

        fd = FailsOnSecondTurn()
        m = core.Mirror(fd, "C", self.tm, 6000)
        sess = [("s1", "T", [("q1", "a1"), ("q2", "a2"), ("q3", "a3")])]
        st = backfill.run(sess, m, self.tm, lambda t: t, log=lambda _: None)
        self.assertEqual(st["failed"], 1)
        self.assertIsNotNone(self.tm.get("s1"))  # thread exists with only the first turn
        before = len(fd.posts)
        st = backfill.run(sess, m, self.tm, lambda t: t, log=lambda _: None)
        self.assertEqual(st["skipped"], 1)  # rerun skips it: remaining turns are never sent
        self.assertEqual(len(fd.posts), before)

    def test_live_send_failure_is_attempted_once_and_not_requeued(self):
        attempts = []

        def failing_post(tid, content):
            attempts.append(content)
            raise core.DiscordError(400, "bad")

        fd = CountingDiscord()
        fd.post = failing_post
        m = core.Mirror(fd, "C", self.tm, 6000)
        m.submit("s", "hello", "world")
        self.assertTrue(m.flush(5))
        self.assertEqual(len(attempts), 1)  # no persistent retry queue
        self.assertTrue(m.flush(1))
        self.assertEqual(len(attempts), 1)
        m.close()


if __name__ == "__main__":
    unittest.main()
