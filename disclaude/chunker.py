"""Pure text-shaping helpers for what disclaude sends to and names in Discord.

Zero third-party imports -- this module (and its tests) must run on plain
system Python, without the project's venv.

Two responsibilities live here because both are about presenting arbitrary
Claude-authored text within Discord's constraints:

- `chunk_message` splits a reply into pieces no longer than Discord's
  2000-character message cap, preferring paragraph (blank-line) boundaries,
  and never splits a ``` code fence across two chunks -- a fence still open
  at a chunk boundary is closed at the end of that chunk and reopened, with
  the same language tag, at the start of the next.
- `derive_thread_name` turns an incoming message's first line into a
  Discord-legal thread name.
"""

from __future__ import annotations

MAX_CHUNK_LEN = 2000
FENCE = "```"

# Defensive cap on a reopened fence's language tag. Real tags ("python",
# "bash", ...) are a handful of characters; capping guarantees the
# `FENCE + lang` opener we inject at the top of a continuation chunk can
# never itself approach MAX_CHUNK_LEN, so the packer below always has room
# to place at least one character of real content in a fresh chunk.
_MAX_FENCE_LANG_LEN = 32

THREAD_NAME_MAX_LEN = 100  # Discord's cap on a thread name


def _line_fence_delta(line):
    """Return (flips, lang) for one line of text.

    `flips` is True when the line contains an odd number of ``` markers,
    meaning it toggles whether we're inside a fence. `lang` is the language
    tag following the *last* marker on the line -- only meaningful when
    this line is the one *opening* a fence (callers ignore it otherwise).

    A line with an even number of markers (including zero) never toggles
    fence state, matching how Discord/CommonMark render fences.
    """
    count = line.count(FENCE)
    if count % 2 == 0:
        return False, ""
    idx = line.rfind(FENCE)
    lang = line[idx + len(FENCE):].strip()
    return True, lang[:_MAX_FENCE_LANG_LEN]


def chunk_message(text, max_len=MAX_CHUNK_LEN):
    """Split `text` into a list of chunks, each <= max_len characters.

    Breaks are chosen, in order of preference, at:
      1. the last blank line (paragraph boundary) that still fits, and
      2. failing that, the line boundary where the running length would
         overflow, and
      3. failing that (a single line longer than max_len on its own),
         a raw character split.

    A ``` code fence left open at a chunk boundary is closed at the end of
    that chunk and reopened (same language tag) at the top of the next, so
    every returned chunk has a balanced, even count of ``` markers and is
    independently valid markdown. Fence tracking does not look inside a
    raw character split (case 3) for an embedded ``` marker -- a single
    unbroken line over 2000 characters that also happens to contain a fence
    marker mid-line is not fence-tracked precisely; this is a deliberate,
    documented limit on an already-pathological input shape.
    """
    if not text:
        return []

    pending = text.split("\n")
    chunks = []
    reopen_lang = None  # fence language the *next* chunk must reopen with

    while pending:
        buf = []
        buf_len = 0
        cur_lang = reopen_lang
        reopen_lang = None
        if cur_lang is not None:
            opener = FENCE + cur_lang
            buf.append(opener)
            buf_len = len(opener)

        last_blank_idx = None  # index within buf of the last safe paragraph break

        while pending:
            line = pending[0]
            flips, lang = _line_fence_delta(line)
            prospective_lang = (lang if cur_lang is None else None) if flips else cur_lang
            # If this line is the last one we place, and it leaves a fence
            # open, a closing "\n```" still has to be appended at flush
            # time -- reserve room for it now so that append can never
            # push the chunk over max_len.
            reserve = (len(FENCE) + 1) if prospective_lang is not None else 0
            add_len = len(line) + (1 if buf else 0)

            if buf_len + add_len + reserve > max_len:
                # Would this line fit whole in a brand new, empty chunk (own
                # reserve only)? Then it isn't genuinely oversized -- flush
                # what we have and let it start the next chunk untouched,
                # rather than needlessly (and, for fence bookkeeping,
                # unsafely) splitting it mid-line.
                if len(line) + reserve <= max_len:
                    break
                # Genuinely too big to ever fit whole -- hard character-split,
                # reserving room via the *pre-line* fence state (a split
                # piece is not scanned for fence markers of its own --
                # documented limitation for this already-pathological shape).
                split_reserve = (len(FENCE) + 1) if cur_lang is not None else 0
                room = max_len - buf_len - (1 if buf else 0) - split_reserve
                if room > 0:
                    piece = line[:room]
                    pending[0] = line[room:]
                    buf.append(piece)
                    buf_len += len(piece) + (1 if len(buf) > 1 else 0)
                    continue
                break

            pending.pop(0)
            cur_lang = prospective_lang
            buf.append(line)
            buf_len += add_len
            if line == "" and cur_lang is None:
                last_blank_idx = len(buf) - 1

        # Prefer breaking at the last paragraph boundary over the raw
        # length-driven cutoff, when there's more content left to place and
        # the boundary isn't already exactly where we stopped.
        if pending and last_blank_idx is not None and last_blank_idx < len(buf) - 1:
            give_back = buf[last_blank_idx + 1:]
            buf = buf[:last_blank_idx]
            pending[0:0] = give_back
            cur_lang = None
            for kept_line in buf:
                flips, lang = _line_fence_delta(kept_line)
                cur_lang = (lang if cur_lang is None else None) if flips else cur_lang

        # A trailing blank line is always just a separator -- the chunk
        # boundary itself already implies the paragraph break, so drop it
        # rather than emit a dangling newline.
        while buf and buf[-1] == "":
            buf.pop()

        text_out = "\n".join(buf)
        if cur_lang is not None:
            text_out += ("\n" if text_out else "") + FENCE
            reopen_lang = cur_lang
        chunks.append(text_out)

    return chunks


def derive_thread_name(message_text, *, now=None):
    """Derive a Discord thread name from an incoming message.

    Takes the message's first line, strips ASCII control characters, and
    truncates to Discord's 100-character thread-name cap. Falls back to a
    timestamped placeholder when that leaves nothing usable (an
    empty/whitespace-only or all-control-character first line -- e.g. a
    message that is only an attachment or emoji-only in a way that
    survives control stripping as blank).

    `now` is an injectable `datetime.datetime` for deterministic tests;
    real callers omit it and get the current UTC time.
    """
    first_line = (message_text or "").splitlines()[0] if message_text else ""
    cleaned = "".join(ch for ch in first_line if ch >= " " or ch == "")
    cleaned = cleaned.strip()

    if cleaned:
        return cleaned[:THREAD_NAME_MAX_LEN]

    if now is None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
    return f"chat {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
