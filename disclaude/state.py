"""Per-instance thread <-> Claude-session map.

Zero third-party imports -- this module (and its tests) must run on plain
system Python, without the project's venv.

Every `StateStore` is constructed with its own file path and holds its map
in an instance attribute; nothing here is module-level, so two `StateStore`
objects (e.g. belonging to two differently-configured disclaude instances)
never share state even if they happen to run in the same process (tests
exercise exactly this).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRUNE_AFTER_DAYS = 30


class StateStore:
    """One instance's thread<->session map, backed by one JSON file."""

    def __init__(self, path):
        self.path = Path(path)
        self._entries = {}  # thread_id (str) -> {session_id, created_at, last_active}

    def load(self):
        """Load the map from disk (a missing file means an empty map, not
        an error -- the first run of a new instance has none yet), then
        prune entries whose thread has been inactive over PRUNE_AFTER_DAYS.
        Returns self so callers can chain `StateStore(path).load()`.
        """
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._entries = dict(raw.get("threads", {})) if isinstance(raw, dict) else {}
        else:
            self._entries = {}
        self._prune()
        return self

    def _prune(self, *, now=None):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=PRUNE_AFTER_DAYS)
        kept = {}
        for thread_id, entry in self._entries.items():
            last_active = entry.get("last_active") if isinstance(entry, dict) else None
            ts = _parse_iso(last_active)
            if ts is None or ts >= cutoff:
                kept[thread_id] = entry
        self._entries = kept

    def get_session_id(self, thread_id):
        entry = self._entries.get(str(thread_id))
        return entry.get("session_id") if entry else None

    def set_session(self, thread_id, session_id, *, now=None):
        """Record (or update) the session bound to a thread and persist
        immediately -- called after every completed turn per the pack
        convention this mirrors: state is never left un-flushed for a
        completed action."""
        thread_id = str(thread_id)
        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        existing = self._entries.get(thread_id)
        created_at = existing["created_at"] if existing else now_iso
        self._entries[thread_id] = {
            "session_id": session_id,
            "created_at": created_at,
            "last_active": now_iso,
        }
        self.save()

    def save(self):
        """Atomic write (temp file + rename) so a crash mid-write can never
        leave the map file truncated or corrupt for the next turn to read."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"threads": self._entries}, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def entries(self):
        """A copy of the current thread_id -> entry map. Each entry dict is
        also copied, so mutating the returned structure can never corrupt
        internal state."""
        return {thread_id: dict(entry) for thread_id, entry in self._entries.items()}


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
