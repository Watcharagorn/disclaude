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

import logging

import discord

from .chunker import chunk_message, derive_thread_name
from .engine import Engine, EngineError

logger = logging.getLogger("disclaude.bridge")


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


def _attachment_notice(attachments):
    n = len(attachments)
    noun = "attachment" if n == 1 else "attachments"
    pronoun = "it was" if n == 1 else "they were"
    return f"_(this message had {n} {noun} -- disclaude v1 is text-only, so {pronoun} ignored)_"


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
            if message.attachments:
                await thread.send(_attachment_notice(message.attachments))

            try:
                async for block in self.engine.run_turn(thread.id, message.content):
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
