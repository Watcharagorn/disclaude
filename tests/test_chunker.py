"""Tests for disclaude.chunker -- pure, no third-party imports, runs on
system Python without the venv."""

import unittest
from datetime import datetime, timezone

from disclaude.chunker import (
    FENCE,
    MAX_CHUNK_LEN,
    THREAD_NAME_MAX_LEN,
    chunk_message,
    derive_thread_name,
)


def fence_balanced(chunk):
    return chunk.count(FENCE) % 2 == 0


class ChunkMessageTests(unittest.TestCase):
    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(chunk_message(""), [])
        self.assertEqual(chunk_message(None), [])

    def test_short_text_is_a_single_chunk(self):
        text = "hello, this is a short reply."
        self.assertEqual(chunk_message(text), [text])

    def test_every_chunk_respects_max_len(self):
        text = "\n\n".join(f"paragraph {i} " + ("x" * 300) for i in range(20))
        chunks = chunk_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_CHUNK_LEN)

    def test_prefers_paragraph_boundary_when_it_fits(self):
        # Two paragraphs that together exceed a small max_len, but each
        # individually fits -- the break must land between them, not
        # mid-paragraph.
        para_a = "A" * 40
        para_b = "B" * 40
        text = para_a + "\n\n" + para_b
        chunks = chunk_message(text, max_len=50)
        self.assertEqual(chunks[0], para_a)
        self.assertTrue(chunks[1].startswith(para_b))

    def test_reconstructs_content_with_no_fences(self):
        text = "\n\n".join(f"para{i} " + ("y" * 250) for i in range(10))
        chunks = chunk_message(text, max_len=300)
        # Every paragraph's distinguishing marker must survive, in order,
        # across the emitted chunks.
        joined = "\n\n".join(chunks)
        for i in range(10):
            self.assertIn(f"para{i} ", joined)

    def test_never_splits_a_code_fence_mid_block(self):
        code = "\n".join(f"line_{i} = {i}" for i in range(200))
        text = "intro paragraph\n\n```python\n" + code + "\n```\n\npost paragraph"
        chunks = chunk_message(text, max_len=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(fence_balanced(chunk), f"unbalanced fence in chunk: {chunk!r}")
            self.assertLessEqual(len(chunk), 200)

    def test_reopened_fence_carries_the_same_language_tag(self):
        code = "x = 1\n" * 400
        text = "```python\n" + code + "```"
        chunks = chunk_message(text, max_len=100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(fence_balanced(chunk))
        # Every continuation chunk (all but conceivably the very first, if
        # it happens to also open fresh) must reopen with "```python".
        for chunk in chunks[1:]:
            self.assertTrue(chunk.startswith("```python"), chunk[:20])

    def test_single_line_longer_than_max_len_is_hard_split(self):
        text = "z" * 5000
        chunks = chunk_message(text, max_len=2000)
        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2000)
        self.assertEqual("".join(chunks), text)

    def test_default_max_len_matches_discord_limit(self):
        self.assertEqual(MAX_CHUNK_LEN, 2000)

    def test_long_fenced_block_alone_stays_balanced_and_under_limit(self):
        # A single fenced block bigger than the chunk limit, with no
        # surrounding text at all.
        text = "```\n" + ("line\n" * 1000) + "```"
        chunks = chunk_message(text, max_len=500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
            self.assertTrue(fence_balanced(chunk))


class DeriveThreadNameTests(unittest.TestCase):
    def test_uses_first_line(self):
        text = "how's my portfolio today?\nsecond line ignored"
        self.assertEqual(derive_thread_name(text), "how's my portfolio today?")

    def test_strips_control_characters(self):
        text = "hello\x00\x01 world\x07"
        self.assertEqual(derive_thread_name(text), "hello world")

    def test_truncates_to_discord_cap(self):
        text = "x" * 500
        name = derive_thread_name(text)
        self.assertEqual(len(name), THREAD_NAME_MAX_LEN)
        self.assertEqual(name, "x" * THREAD_NAME_MAX_LEN)

    def test_empty_message_falls_back_to_timestamp(self):
        fixed_now = datetime(2026, 8, 12, 14, 30, 0, tzinfo=timezone.utc)
        name = derive_thread_name("", now=fixed_now)
        self.assertEqual(name, "chat 2026-08-12 14:30:00 UTC")

    def test_control_only_message_falls_back_to_timestamp(self):
        fixed_now = datetime(2026, 8, 12, 14, 30, 0, tzinfo=timezone.utc)
        name = derive_thread_name("\x00\x01\x02", now=fixed_now)
        self.assertTrue(name.startswith("chat "))

    def test_whitespace_only_message_falls_back_to_timestamp(self):
        fixed_now = datetime(2026, 8, 12, 14, 30, 0, tzinfo=timezone.utc)
        name = derive_thread_name("   \t  ", now=fixed_now)
        self.assertTrue(name.startswith("chat "))

    def test_none_message_falls_back_to_timestamp(self):
        fixed_now = datetime(2026, 8, 12, 14, 30, 0, tzinfo=timezone.utc)
        name = derive_thread_name(None, now=fixed_now)
        self.assertTrue(name.startswith("chat "))


if __name__ == "__main__":
    unittest.main()
