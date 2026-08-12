"""Direct tests of `Bridge._handle_message`'s filter predicate -- the
instance's entire access boundary (channel id, then author allowlist, both
before any thread creation or Claude turn).

Needs discord.py installed (for `discord.Thread`/mock isinstance checks and
`Bridge` itself), so this module follows `test_isolation.py`'s
skipUnless pattern: it collects cleanly under system Python (skipped) and
runs fully under the project's venv.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from disclaude import config as config_module

try:
    import discord

    from disclaude.bridge import Bridge

    HAS_RUNTIME_DEPS = True
except ImportError:
    HAS_RUNTIME_DEPS = False

CONFIGURED_CHANNEL_ID = 555555555555555555
OTHER_CHANNEL_ID = 666666666666666666
ALLOWED_USER_ID = 111111111111111111
OTHER_USER_ID = 222222222222222222


def make_cfg(tmp_path):
    raw = {
        "instance": {"name": "filter-test"},
        "discord": {
            "bot_token": "placeholder-token",
            "channel_id": CONFIGURED_CHANNEL_ID,
            "allowed_user_ids": [ALLOWED_USER_ID],
        },
        "claude": {"cwd": str(tmp_path)},
        "runtime": {"state_file": str(tmp_path / "state.json")},
    }
    return config_module.validate_config(raw, source_path=tmp_path / "cfg.toml", explicit=True)


class _AsyncContextManager:
    """Stands in for discord.py's `Typing` context manager returned by
    `Messageable.typing()` -- `thread.typing()` itself is a plain (sync)
    call whose return value is used with `async with`."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def make_text_channel(channel_id):
    """A channel that is deliberately *not* a `discord.Thread` -- exercises
    the "wrong channel" and "top-level in the configured channel" paths."""
    channel = MagicMock()
    channel.id = channel_id
    return channel


def make_thread(thread_id, parent_id):
    """A stand-in Discord thread. Setting `__class__` to `discord.Thread`
    is what makes `isinstance(thread, discord.Thread)` -- the exact check
    `_handle_message` performs -- return True for this mock."""
    thread = MagicMock()
    thread.__class__ = discord.Thread
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.send = AsyncMock()
    thread.typing = MagicMock(return_value=_AsyncContextManager())
    return thread


def make_message(*, channel, author_id, content="hello", is_bot=False, attachments=None):
    message = MagicMock()
    message.id = 999999999999999999
    message.content = content
    message.channel = channel
    message.author = MagicMock()
    message.author.id = author_id
    message.author.bot = is_bot
    message.attachments = attachments or []
    message.create_thread = AsyncMock()
    return message


class FakeEngine:
    """Stands in for `engine.py`'s real `Engine` -- records the
    (thread_id, prompt_text) each call reached it with and yields whatever
    text blocks the test hands it, without touching the SDK or Discord."""

    def __init__(self, blocks=()):
        self.blocks = list(blocks)
        self.calls = []

    async def run_turn(self, thread_id, prompt_text):
        self.calls.append((thread_id, prompt_text))
        for block in self.blocks:
            yield block


@unittest.skipUnless(
    HAS_RUNTIME_DEPS, "requires the venv (discord.py + claude-agent-sdk installed)"
)
class BridgeFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cfg = make_cfg(Path(self._tmpdir.name))

    def make_bridge(self, blocks=()):
        return Bridge(self.cfg, engine=FakeEngine(blocks=blocks))

    async def test_wrong_channel_is_dropped(self):
        bridge = self.make_bridge()
        channel = make_text_channel(OTHER_CHANNEL_ID)
        message = make_message(channel=channel, author_id=ALLOWED_USER_ID)

        with self.assertLogs("disclaude.bridge", level="INFO") as cm:
            await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(bridge.engine.calls, [])
        self.assertEqual(len(cm.output), 1)
        self.assertIn("ignored", cm.output[0])
        self.assertIn(str(OTHER_CHANNEL_ID), cm.output[0])

    async def test_non_allowlisted_author_in_right_channel_is_dropped(self):
        bridge = self.make_bridge()
        channel = make_text_channel(CONFIGURED_CHANNEL_ID)
        message = make_message(channel=channel, author_id=OTHER_USER_ID)

        with self.assertLogs("disclaude.bridge", level="INFO") as cm:
            await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(bridge.engine.calls, [])
        self.assertEqual(len(cm.output), 1)
        self.assertIn("ignored", cm.output[0])
        self.assertIn(str(OTHER_USER_ID), cm.output[0])

    async def test_bot_message_never_loops(self):
        # A message authored by any bot -- including disclaude's own posted
        # replies -- must never re-enter the engine, even if it lands in
        # the configured channel from an allowlisted author id.
        bridge = self.make_bridge()
        channel = make_text_channel(CONFIGURED_CHANNEL_ID)
        message = make_message(channel=channel, author_id=ALLOWED_USER_ID, is_bot=True)

        await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(bridge.engine.calls, [])

    async def test_valid_top_level_message_proceeds_to_thread_and_engine(self):
        bridge = self.make_bridge(blocks=["ack"])
        channel = make_text_channel(CONFIGURED_CHANNEL_ID)
        thread = make_thread(thread_id=777777777777777777, parent_id=CONFIGURED_CHANNEL_ID)
        message = make_message(channel=channel, author_id=ALLOWED_USER_ID, content="hi there")
        message.create_thread.return_value = thread

        await bridge._handle_message(message)

        message.create_thread.assert_awaited_once()
        self.assertEqual(bridge.engine.calls, [(thread.id, "hi there")])
        thread.send.assert_awaited_once_with("ack")

    async def test_valid_thread_reply_proceeds_to_engine_without_new_thread(self):
        bridge = self.make_bridge(blocks=["ack2"])
        thread = make_thread(thread_id=888888888888888888, parent_id=CONFIGURED_CHANNEL_ID)
        message = make_message(channel=thread, author_id=ALLOWED_USER_ID, content="follow up")

        await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(bridge.engine.calls, [(thread.id, "follow up")])
        thread.send.assert_awaited_once_with("ack2")


if __name__ == "__main__":
    unittest.main()
