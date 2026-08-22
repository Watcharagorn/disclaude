"""Per-thread Claude Agent SDK sessions.

One SDK client per turn, run under a per-instance concurrency semaphore,
with session resume (A1) and a fresh-session fallback when resuming fails.

This module never touches Discord. Its public surface is `Engine.run_turn`,
an async generator that yields plain text blocks as they complete; the
caller (bridge.py) owns all chunking, posting, and typing-indicator logic.
Keeping that boundary here -- rather than, say, formatting a reply string
-- is what lets bridge.py be the single place Discord-facing text is
composed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
)

from .state import StateStore

logger = logging.getLogger("disclaude.engine")

RESET_NOTICE = (
    "_(the previous conversation in this thread could not be resumed, so "
    "this reply starts a fresh session -- earlier context in this thread "
    "is lost)_"
)


class EngineError(Exception):
    """A turn failed outright (not a resumable-session hiccup, which is
    handled internally). Carries a short, human-readable reason; bridge.py
    decides how that reason is presented in Discord."""


@dataclass(frozen=True)
class ImagePart:
    """One image to attach to a turn: already-downloaded, already-base64
    bytes plus the media type Claude needs to decode them. bridge.py builds
    these from Discord attachments; this module never touches Discord, so
    it carries no filename -- engine.py doesn't need one, and it keeps
    filenames out of any temptation to log them here."""

    media_type: str
    data_b64: str


def build_user_message(prompt_text, images):
    """Build the stream-json user-message dict `client.query()` expects
    when a turn carries images, mirroring the shape the SDK itself builds
    for a plain string prompt (`ClaudeSDKClient.query`,
    claude_agent_sdk/client.py) but with image content blocks ahead of the
    text block, per Anthropic's vision guidance (image first, question
    after). `session_id` is deliberately omitted -- `query()` backfills it
    with "default" when missing, identical to the plain-string path; which
    Claude session this resumes is controlled entirely by the CLI
    `--resume` flag set from `ClaudeAgentOptions`, never by this field.
    """
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": image.data_b64,
            },
        }
        for image in images
    ]
    if prompt_text:
        content.append({"type": "text", "text": prompt_text})
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


async def _single_message_stream(message):
    yield message


class Engine:
    """One instance's Claude Agent SDK wiring.

    Strictly per-instance: every `Engine` is constructed from one
    `DisclaudeConfig` and holds its own `StateStore` and semaphore as
    instance attributes. Nothing here is module-level, so two `Engine`
    objects -- e.g. belonging to two differently-configured disclaude
    instances sharing a process, as the isolation tests exercise --
    never share a session map or a concurrency budget.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = StateStore(cfg.state_file).load()
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent)

    def _options(self, *, resume_session_id):
        return ClaudeAgentOptions(
            cwd=self.cfg.claude_cwd,
            setting_sources=["user", "project"],
            permission_mode=self.cfg.permission_mode,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.cfg.system_prompt_append,
            },
            model=self.cfg.model,
            resume=resume_session_id,
        )

    async def run_turn(self, thread_id, prompt_text, images=()):
        """Run one Claude turn bound to `thread_id`, yielding each
        completed assistant text block as it arrives.

        Resumes the thread's stored session when one exists (A1). A resume
        is judged to have failed only if establishing the SDK connection
        itself fails -- that is the boundary the installed SDK exposes
        most reliably for "this session id no longer resumes"; a failure
        *after* a successful connect is treated as a genuine turn failure
        instead (retrying it fresh wouldn't fix a rate limit or billing
        error, and would be confusing after a partial reply). This
        boundary is the piece the plan flags as A1 "verified during
        implementation" -- confirm it against real resumed sessions during
        the live smoke (plan Step 5) and tighten here if live behavior
        differs.

        Persists the resulting session_id after a completed turn. Raises
        `EngineError` if the turn fails outright.
        """
        async with self._semaphore:
            resume_id = self.state.get_session_id(thread_id)

            if resume_id is not None:
                client = ClaudeSDKClient(options=self._options(resume_session_id=resume_id))
                try:
                    await client.connect()
                except ClaudeSDKError as exc:
                    logger.warning(
                        "resume failed for thread %s (session %s): %s -- "
                        "falling back to a fresh session",
                        thread_id,
                        resume_id,
                        exc,
                    )
                    yield RESET_NOTICE
                else:
                    logger.info("session resumed: thread=%s session=%s", thread_id, resume_id)
                    async for block in self._drive(client, prompt_text, thread_id, images):
                        yield block
                    return

            client = ClaudeSDKClient(options=self._options(resume_session_id=None))
            try:
                await client.connect()
            except ClaudeSDKError as exc:
                raise EngineError(f"could not start a Claude session: {exc}") from exc

            logger.info("session started: new session for thread=%s", thread_id)
            async for block in self._drive(client, prompt_text, thread_id, images):
                yield block

    async def _drive(self, client, prompt_text, thread_id, images):
        """Run the query/response loop on an already-connected client,
        yielding text blocks and persisting the session on success.
        Always disconnects, even on failure."""
        start = time.monotonic()
        try:
            if images:
                await client.query(_single_message_stream(build_user_message(prompt_text, images)))
            else:
                await client.query(prompt_text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield block.text
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        reason = (
                            message.result
                            or "; ".join(message.errors or [])
                            or "the turn ended in an error"
                        )
                        raise EngineError(reason)
                    self.state.set_session(thread_id, message.session_id)
                    logger.info(
                        "turn complete: thread=%s session=%s duration=%.2fs",
                        thread_id,
                        message.session_id,
                        time.monotonic() - start,
                    )
        except EngineError:
            raise
        except ClaudeSDKError as exc:
            raise EngineError(str(exc)) from exc
        finally:
            await self._safe_disconnect(client)

    @staticmethod
    async def _safe_disconnect(client):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never masks the real error
            logger.debug("error while disconnecting the Claude SDK client", exc_info=True)
