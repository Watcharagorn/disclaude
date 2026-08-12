"""Tests for disclaude.state -- pure, no third-party imports, runs on
system Python without the venv."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from disclaude.state import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "state" / "test-instance.json"

    def test_load_on_missing_file_is_an_empty_map(self):
        store = StateStore(self.path).load()
        self.assertEqual(store.entries(), {})
        self.assertIsNone(store.get_session_id("12345"))

    def test_set_session_then_get_round_trips(self):
        store = StateStore(self.path).load()
        store.set_session("thread-1", "session-abc")
        self.assertEqual(store.get_session_id("thread-1"), "session-abc")

    def test_set_session_persists_to_disk(self):
        store = StateStore(self.path).load()
        store.set_session("thread-1", "session-abc")

        reloaded = StateStore(self.path).load()
        self.assertEqual(reloaded.get_session_id("thread-1"), "session-abc")

    def test_set_session_coerces_thread_id_to_string(self):
        store = StateStore(self.path).load()
        store.set_session(12345, "session-abc")  # int, as a Discord snowflake often is
        self.assertEqual(store.get_session_id(12345), "session-abc")
        self.assertEqual(store.get_session_id("12345"), "session-abc")

    def test_set_session_keeps_created_at_across_updates(self):
        store = StateStore(self.path).load()
        first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.set_session("thread-1", "session-a", now=first_time)

        second_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
        store.set_session("thread-1", "session-b", now=second_time)

        entry = store.entries()["thread-1"]
        self.assertEqual(entry["created_at"], first_time.isoformat())
        self.assertEqual(entry["last_active"], second_time.isoformat())
        self.assertEqual(entry["session_id"], "session-b")

    def test_prune_drops_entries_inactive_over_30_days(self):
        store = StateStore(self.path).load()
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.set_session("stale-thread", "session-old", now=old_time)

        recent_time = datetime(2026, 8, 10, tzinfo=timezone.utc)
        store.set_session("fresh-thread", "session-new", now=recent_time)

        reload_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
        reloaded = StateStore(self.path)

        # Simulate "load at reload_time" by loading then re-pruning at a
        # fixed instant, since load() prunes against real now() otherwise.
        reloaded.load()
        reloaded._prune(now=reload_time)

        entries = reloaded.entries()
        self.assertNotIn("stale-thread", entries)
        self.assertIn("fresh-thread", entries)

    def test_prune_keeps_entries_with_unparseable_timestamps_rather_than_drop_data(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {"threads": {"weird-thread": {"session_id": "s", "created_at": "?", "last_active": "not-a-date"}}},
                f,
            )
        store = StateStore(self.path).load()
        self.assertIn("weird-thread", store.entries())

    def test_save_is_atomic_no_partial_file_left_behind(self):
        store = StateStore(self.path).load()
        store.set_session("thread-1", "session-abc")
        leftovers = list(self.path.parent.glob(".state-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_entries_returns_a_copy_not_a_live_reference(self):
        store = StateStore(self.path).load()
        store.set_session("thread-1", "session-abc")
        snapshot = store.entries()
        snapshot["thread-1"]["session_id"] = "mutated"
        self.assertEqual(store.get_session_id("thread-1"), "session-abc")


if __name__ == "__main__":
    unittest.main()
