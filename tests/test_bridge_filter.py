"""Direct tests of `Bridge._handle_message`'s filter predicate -- the
instance's entire access boundary (channel id, then author allowlist, both
before any thread creation or Claude turn) -- and of its attachment
handling: partitioning Discord attachments into images to send and
attachments to skip, downloading/encoding the former, and notifying about
the latter.

Needs discord.py installed (for `discord.Thread`/mock isinstance checks and
`Bridge` itself), so this module follows `test_isolation.py`'s
skipUnless pattern: it collects cleanly under system Python (skipped) and
runs fully under the project's venv.
"""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from disclaude import config as config_module

try:
    import aiohttp
    import discord

    from disclaude.bridge import Bridge, _partition_attachments
    from disclaude.engine import ImagePart

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


def make_attachment(
    *,
    filename="pic.png",
    content_type="image/png",
    size=1024,
    width=100,
    height=100,
    data=b"fakepng",
):
    """A stand-in `discord.Attachment` -- carries the fields
    `_partition_attachments` reads (content_type/size/width/height) plus a
    fake `.read()` for the download step, without a real Discord CDN."""
    att = MagicMock()
    att.filename = filename
    att.content_type = content_type
    att.size = size
    att.width = width
    att.height = height
    att.read = AsyncMock(return_value=data)
    return att


def make_http_exception(status=404, reason="Not Found", message="not found"):
    """A real, constructible `discord.HTTPException` -- its `__init__`
    reads `response.status`/`response.reason`, so a bare mock response
    with those two attributes set is enough."""
    response = MagicMock()
    response.status = status
    response.reason = reason
    return discord.HTTPException(response, message)


class FakeEngine:
    """Stands in for `engine.py`'s real `Engine` -- records the
    (thread_id, prompt_text, images) each call reached it with and yields
    whatever text blocks the test hands it, without touching the SDK or
    Discord."""

    def __init__(self, blocks=()):
        self.blocks = list(blocks)
        self.calls = []

    async def run_turn(self, thread_id, prompt_text, images=()):
        self.calls.append((thread_id, prompt_text, tuple(images)))
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
        self.assertEqual(bridge.engine.calls, [(thread.id, "hi there", ())])
        thread.send.assert_awaited_once_with("ack")

    async def test_valid_thread_reply_proceeds_to_engine_without_new_thread(self):
        bridge = self.make_bridge(blocks=["ack2"])
        thread = make_thread(thread_id=888888888888888888, parent_id=CONFIGURED_CHANNEL_ID)
        message = make_message(channel=thread, author_id=ALLOWED_USER_ID, content="follow up")

        await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(bridge.engine.calls, [(thread.id, "follow up", ())])
        thread.send.assert_awaited_once_with("ack2")

    # -- attachment handling --------------------------------------------

    async def test_image_and_text_reaches_engine_with_no_notice(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000001, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="pic.png", content_type="image/png", data=b"fakepng")
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="look at this", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(len(bridge.engine.calls), 1)
        thread_id, prompt_text, images = bridge.engine.calls[0]
        self.assertEqual(prompt_text, "look at this")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].media_type, "image/png")
        self.assertEqual(images[0].data_b64, base64.b64encode(b"fakepng").decode())

        # No skipped-attachment notice among the posted messages -- only
        # the normal reply chunk.
        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        self.assertEqual(sent_texts, ["ack"])

    async def test_non_image_attachment_is_skipped_with_notice(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000002, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="archive.zip", content_type="application/zip")
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="see attached", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(len(bridge.engine.calls), 1)
        _, _, images = bridge.engine.calls[0]
        self.assertEqual(images, ())

        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        notice_texts = [t for t in sent_texts if "skipped" in t]
        self.assertEqual(len(notice_texts), 1)
        self.assertIn("archive.zip", notice_texts[0])

    async def test_oversized_image_skipped_small_one_sent(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000003, parent_id=CONFIGURED_CHANNEL_ID)
        big = make_attachment(
            filename="huge.png", content_type="image/png", size=6 * 1024 * 1024
        )
        small = make_attachment(
            filename="small.jpg", content_type="image/jpeg", size=2048, data=b"smalljpeg"
        )
        message = make_message(
            channel=thread,
            author_id=ALLOWED_USER_ID,
            content="two files",
            attachments=[big, small],
        )

        await bridge._handle_message(message)

        _, _, images = bridge.engine.calls[0]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].media_type, "image/jpeg")

        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        notice_texts = [t for t in sent_texts if "skipped" in t]
        self.assertEqual(len(notice_texts), 1)
        self.assertIn("huge.png", notice_texts[0])
        self.assertNotIn("small.jpg", notice_texts[0])

    async def test_over_long_edge_image_is_skipped(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000004, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="wide.png", content_type="image/png", width=9000)
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="check this", attachments=[att]
        )

        await bridge._handle_message(message)

        _, _, images = bridge.engine.calls[0]
        self.assertEqual(images, ())
        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        self.assertTrue(any("skipped" in t and "wide.png" in t for t in sent_texts))

    async def test_download_failure_http_exception_degrades_gracefully(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000005, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="pic.png", content_type="image/png")
        att.read = AsyncMock(side_effect=make_http_exception())
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="hello there", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(len(bridge.engine.calls), 1)
        _, prompt_text, images = bridge.engine.calls[0]
        self.assertEqual(prompt_text, "hello there")
        self.assertEqual(images, ())

        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        self.assertTrue(any("skipped" in t for t in sent_texts))
        self.assertIn("ack", sent_texts)

    async def test_download_failure_aiohttp_client_error_degrades_gracefully(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000006, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="pic.png", content_type="image/png")
        att.read = AsyncMock(side_effect=aiohttp.ClientError("connection reset"))
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="hello again", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(len(bridge.engine.calls), 1)
        _, prompt_text, images = bridge.engine.calls[0]
        self.assertEqual(prompt_text, "hello again")
        self.assertEqual(images, ())

        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        self.assertTrue(any("skipped" in t for t in sent_texts))
        self.assertIn("ack", sent_texts)

    async def test_image_only_message_reaches_engine(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000007, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="pic.png", content_type="image/png", data=b"fakepng")
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(len(bridge.engine.calls), 1)
        thread_id, prompt_text, images = bridge.engine.calls[0]
        self.assertEqual(prompt_text, "")
        self.assertEqual(len(images), 1)
        self.assertIsInstance(images[0], ImagePart)

    async def test_image_only_all_skipped_does_not_call_engine(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000008, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="archive.zip", content_type="application/zip")
        message = make_message(
            channel=thread, author_id=ALLOWED_USER_ID, content="", attachments=[att]
        )

        await bridge._handle_message(message)

        self.assertEqual(bridge.engine.calls, [])
        sent_texts = [c.args[0] for c in thread.send.call_args_list]
        self.assertTrue(any("skipped" in t for t in sent_texts))

    async def test_follow_up_in_existing_thread_with_image_reaches_engine(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000009, parent_id=CONFIGURED_CHANNEL_ID)
        att = make_attachment(filename="pic.png", content_type="image/png", data=b"fakepng")
        message = make_message(
            channel=thread,
            author_id=ALLOWED_USER_ID,
            content="another one",
            attachments=[att],
        )

        await bridge._handle_message(message)

        message.create_thread.assert_not_called()
        self.assertEqual(len(bridge.engine.calls), 1)
        thread_id, prompt_text, images = bridge.engine.calls[0]
        self.assertEqual(thread_id, thread.id)
        self.assertEqual(len(images), 1)

    async def test_long_notice_is_chunked_under_discord_limit(self):
        bridge = self.make_bridge(blocks=["ack"])
        thread = make_thread(thread_id=100000000000000010, parent_id=CONFIGURED_CHANNEL_ID)
        attachments = [
            make_attachment(
                filename=f"a-very-long-filename-that-eats-space-{i:03d}-" + "x" * 150 + ".zip",
                content_type="application/zip",
            )
            for i in range(10)
        ]
        message = make_message(
            channel=thread,
            author_id=ALLOWED_USER_ID,
            content="many bad files",
            attachments=attachments,
        )

        await bridge._handle_message(message)

        for call in thread.send.call_args_list:
            (text,) = call.args
            self.assertLessEqual(len(text), 2000)


@unittest.skipUnless(
    HAS_RUNTIME_DEPS, "requires the venv (discord.py + claude-agent-sdk installed)"
)
class PartitionAttachmentsTests(unittest.TestCase):
    """Direct, Bridge-free tests of the pure partition helper."""

    def test_over_cap_attachments_split_ten_and_one(self):
        attachments = [make_attachment(filename=f"img{i}.png") for i in range(11)]

        usable, skipped = _partition_attachments(attachments)

        self.assertEqual(len(usable), 10)
        self.assertEqual(len(skipped), 1)
        self.assertIn("limit", skipped[0][1])

    def test_none_content_type_is_skipped(self):
        att = make_attachment(content_type=None)

        usable, skipped = _partition_attachments([att])

        self.assertEqual(usable, [])
        self.assertEqual(len(skipped), 1)

    def test_parameterized_content_type_is_normalized(self):
        att = make_attachment(content_type="image/png; foo=bar")

        usable, skipped = _partition_attachments([att])

        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0][1], "image/png")
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
