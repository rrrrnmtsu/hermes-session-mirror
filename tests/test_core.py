"""What the mirror guarantees: nothing secret leaves, nothing oversize is sent, order is kept."""
import json
import sys
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
        self.created, self.posts = [], []

    def create_thread(self, ch, name):
        self.created.append((ch, name))
        return "T1"

    def post(self, ch, content):
        self.posts.append((ch, content))


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
            self.assertEqual([p[0] for p in fd.posts], ["T1"] * 4)
            self.assertIn("hello", fd.posts[0][1])
            self.assertEqual(json.loads((Path(d) / "m.json").read_text()), {"s1": "T1"})


if __name__ == "__main__":
    unittest.main()
