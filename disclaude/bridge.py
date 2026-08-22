"""Discord gateway wiring: filtering, thread creation, typing indicator,
chunked replies.

This is the only module in disclaude that talks to Discord. Every other
module -- engine.py above all -- is Discord-agnostic; `Bridge` is what
turns an `Engine`'s yielded text blocks into actual Discord messages, using
`chunker.chunk_message` for the splitting. Keeping posting logic
concentrated here is what let engine.py be tested, and reasoned about,
without a Discord connection at all.
"""

from __future__ import annotations

import base64
import logging

import aiohttp
import discord

from .chunker import chunk_message, derive_thread_name
from .engine import Engine, EngineError, ImagePart

logger = logging.getLogger("disclaude.bridge")

# Anthropic API constants (not per-instance preferences, so not config
# knobs): images larger than this, or with a long edge over this, are
# skipped client-side rather than sent -- the API doesn't reliably reject
# either condition with a visible error (verified during Step 0's de-risk:
# an oversized-edge image is silently downscaled, not rejected), so
# catching them here is the only way to give the user a clear reason.
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_EDGE_PX = 8000
MAX_IMAGES_PER_MESSAGE = 10  # matches Discord's own per-message attachment cap


def _build_intents():
    """Only what disclaude actually needs: guild membership (to see the
    configured channel and threads under it), guild message events, and
    the message content itself (a privileged intent Discord requires
    opting into per-app, per the README's bot-setup step)."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


def _partition_attachments(attachments):
    """Split incoming attachments into (usable, skipped):
    `usable` is `[(attachment, media_type)]` for attachments to fetch and
    send; `skipped` is `[(attachment, reason)]` for everything else. Pure
    -- does no I/O, only reads fields Discord already sent with the
    message (`content_type`, `size`, `width`, `height`).

    A `content_type` of `None`, or one outside the four types Claude
    accepts, is skipped as non-image; a parameterized type (e.g.
    "image/png; foo=bar") is normalized by splitting on ";" first. Missing
    `width`/`height` (a mislabeled file Discord couldn't introspect) is let
    through -- the size cap still bounds it, and a file the API genuinely
    can't decode surfaces as Claude saying so in its reply, not a crash
    (verified during Step 0's de-risk).
    """
    images, skipped = [], []
    for att in attachments:
        media_type = (att.content_type or "").split(";")[0].strip().lower()
        if media_type not in IMAGE_MEDIA_TYPES:
            skipped.append((att, "not an image type Claude accepts"))
        elif att.size > MAX_IMAGE_BYTES:
            skipped.append((att, "larger than 5 MB"))
        elif max(att.width or 0, att.height or 0) > MAX_IMAGE_EDGE_PX:
            skipped.append((att, "longer than 8000 px on one side"))
        elif len(images) >= MAX_IMAGES_PER_MESSAGE:
            skipped.append((att, f"over the {MAX_IMAGES_PER_MESSAGE}-image limit"))
        else:
            images.append((att, media_type))
    return images, skipped


def _attachment_notice(skipped, sent_count):
    """One combined italic notice naming every skipped attachment and why,
    or `None` when nothing was skipped. Images that go through get no
    notice -- Claude's reply referencing them is the evidence."""
    if not skipped:
        return None
    parts = ", ".join(f"`{att.filename}` ({reason})" for att, reason in skipped)
    n = len(skipped)
    noun = "attachment" if n == 1 else "attachments"
    tail = " -- the rest were sent to Claude" if sent_count > 0 else ""
    return f"_({n} {noun} skipped: {parts}{tail})_"


class Bridge(discord.Client):
    """One instance's Discord client.

    Strictly per-instance: constructed from one `DisclaudeConfig`, holding
    its own `Engine` (and, through it, its own state store and
    concurrency semaphore) as an instance attribute. Nothing here is
    module-level, so two `Bridge` objects -- e.g. belonging to two
    differently-configured disclaude instances sharing a process, as the
    isolation tests exercise -- never share a session map, a concurrency
    budget, or a gateway connection.
    """

    def __init__(self, cfg, *, engine=None):
        super().__init__(intents=_build_intents())
        self.cfg = cfg
        self.engine = engine or Engine(cfg)

    async def on_ready(self):
        logger.info(
            "READY -- logged in as %s (instance=%s, channel=%s)",
            self.user,
            self.cfg.name,
            self.cfg.channel_id,
        )

    async def on_message(self, message):
        # discord.py schedules on_message as its own task per event, so
        # concurrent top-level messages are already handled concurrently
        # here; Engine's own semaphore caps how many turns actually run
        # Claude at once. A broad catch at this boundary is deliberate: an
        # unhandled exception here would otherwise vanish into discord.py's
        # default on_error (stderr only), silently dropping the message.
        try:
            await self._handle_message(message)
        except Exception:  # noqa: BLE001 -- last-resort guard, logged with full context
            logger.exception("unhandled error while processing message %s", message.id)

    async def _handle_message(self, message):
        if message.author.bot:
            return

        channel = message.channel
        in_configured_channel = channel.id == self.cfg.channel_id
        in_configured_thread = (
            isinstance(channel, discord.Thread) and channel.parent_id == self.cfg.channel_id
        )

        # Filter order per the plan: channel id first, then author -- both
        # before any Claude turn. Everything else is dropped with one log
        # line and no reply.
        if not (in_configured_channel or in_configured_thread):
            logger.info(
                "ignored: channel %s is not the configured channel (%s) or one of its threads",
                channel.id,
                self.cfg.channel_id,
            )
            return

        if message.author.id not in self.cfg.allowed_user_ids:
            logger.info("ignored: author %s is not in discord.allowed_user_ids", message.author.id)
            return

        if in_configured_channel:
            thread = await message.create_thread(name=derive_thread_name(message.content))
            logger.info(
                "message accepted: new thread %s created from message %s (author=%s, channel=%s)",
                thread.id,
                message.id,
                message.author.id,
                channel.id,
            )
        else:
            thread = channel
            logger.info(
                "message accepted: thread %s (author=%s, channel=%s)",
                thread.id,
                message.author.id,
                channel.id,
            )

        async with thread.typing():
            image_parts, skipped = [], []
            raw_bytes_total = 0
            if message.attachments:
                usable, skipped = _partition_attachments(message.attachments)
                for att, media_type in usable:
                    try:
                        data = await att.read()
                    except (discord.HTTPException, aiohttp.ClientError, OSError):
                        skipped.append((att, "couldn't be downloaded from Discord"))
                        continue
                    if len(data) > MAX_IMAGE_BYTES:  # trust-but-verify Discord's size field
                        skipped.append((att, "larger than 5 MB"))
                        continue
                    raw_bytes_total += len(data)
                    image_parts.append(
                        ImagePart(
                            media_type=media_type,
                            data_b64=base64.b64encode(data).decode("ascii"),
                        )
                    )

                notice = _attachment_notice(skipped, len(image_parts))
                if notice:
                    for chunk in chunk_message(notice):
                        await thread.send(chunk)

                logger.info(
                    "attachments: thread=%s images_sent=%d skipped=%d bytes=%d types=%s",
                    thread.id,
                    len(image_parts),
                    len(skipped),
                    raw_bytes_total,
                    ",".join(sorted({p.media_type for p in image_parts})) or "-",
                )

                if not message.content and not image_parts:
                    # Image-only message, everything skipped: the notice
                    # above already explains why -- don't burn a Claude
                    # turn on an empty message.
                    return

            try:
                async for block in self.engine.run_turn(
                    thread.id, message.content, images=tuple(image_parts)
                ):
                    for chunk in chunk_message(block):
                        if chunk:
                            await thread.send(chunk)
                            logger.info(
                                "chunk posted: thread=%s chars=%d", thread.id, len(chunk)
                            )
            except EngineError as exc:
                logger.warning("turn failed in thread %s: %s", thread.id, exc)
                await thread.send(f"\N{WARNING SIGN} Something went wrong on that turn: {exc}")

    def run_forever(self):
        """Connect to the Discord gateway and block until the process
        exits. The sole place in disclaude that actually reaches Discord
        -- `disclaude check` never calls this. `log_handler=None` avoids
        discord.py installing its own stderr handler on top of the one
        `cli.py` already configured on the root logger."""
        self.run(self.cfg.bot_token, log_handler=None)
