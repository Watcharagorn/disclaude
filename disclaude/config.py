"""Instance configuration: TOML load + validation.

Stdlib only (`tomllib`, `pathlib`) -- zero third-party imports, so config
validation (and its tests) run on system Python without the project's
venv. `tomllib` is why disclaude requires Python >= 3.11.

`cli.py`'s `run` and `check` subcommands both call `load_config()` -- the
sole load/validate path, never a parallel implementation -- so a config
that passes `check` behaves identically under `run`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".disclaude" / "config.toml"
DEFAULT_INSTANCE_NAME = "default"

# v1 accepts only bypassPermissions: an SDK turn triggered from Discord has
# no channel to answer an interactive tool-approval prompt (A3), so any
# other permission_mode would simply hang the turn forever.
ALLOWED_PERMISSION_MODES = ("bypassPermissions",)

BOT_TOKEN_ENV_VAR = "DISCLAUDE_BOT_TOKEN"

DEFAULT_SYSTEM_PROMPT_APPEND = (
    "You are replying inside a Discord thread that is often read on a phone. "
    "Keep replies short and skimmable. Discord does not render GitHub-flavored "
    "markdown tables -- never use one; prefer short lists or plain sentences "
    "instead. Interactive question tools are not available in this context -- "
    "state your assumptions and proceed rather than asking a clarifying "
    "question."
)

DEFAULT_MAX_CONCURRENT = 3


class ConfigError(ValueError):
    """Raised for any invalid or missing config value.

    Every message is meant to be actionable on its own -- printed as-is by
    `cli.py` -- since the person reading it is very likely wiring up their
    first instance from the README, not debugging disclaude's internals.
    """


@dataclass(frozen=True)
class DisclaudeConfig:
    name: str
    bot_token: str
    channel_id: int
    allowed_user_ids: tuple
    claude_cwd: Path
    permission_mode: str
    model: object  # str | None
    system_prompt_append: str
    max_concurrent: int
    state_file: Path
    log_file: Path
    source_path: Path


def default_state_file(name):
    return Path.home() / ".disclaude" / "state" / f"{name}.json"


def default_log_file(name):
    return Path.home() / ".disclaude" / "logs" / f"{name}.log"


def load_config(path=None):
    """Load and validate a disclaude instance config from a TOML file.

    `path=None` means "the caller did not pass --config" -- it resolves to
    the global default config (`~/.disclaude/config.toml`) and *permits*
    `instance.name = "default"`. Any non-None `path` is treated as an
    explicit --config, which the collision guard below rejects if it
    resolves to `instance.name = "default"` (silently colliding with the
    default instance's state/log paths and launchd label otherwise).
    """
    explicit = path is not None
    resolved = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH

    if not resolved.exists():
        raise ConfigError(
            f"Config file not found: {resolved}\n"
            "Copy examples/disclaude.toml.example to this path (or pass "
            "--config <path>) and fill in your own values."
        )

    with open(resolved, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Could not parse {resolved} as TOML: {exc}") from exc

    return validate_config(raw, source_path=resolved, explicit=explicit)


def validate_config(raw, *, source_path, explicit):
    """Validate an already-parsed config dict (as `tomllib.load` returns)
    and return a `DisclaudeConfig`. Split out from `load_config` so tests
    -- including the isolation tests -- can validate configs built in
    memory, with no file I/O and no dependency on the venv.
    """
    errors = []

    instance = raw.get("instance", {}) if isinstance(raw, dict) else {}
    name = instance.get("name", DEFAULT_INSTANCE_NAME)
    if not isinstance(name, str) or not name.strip():
        errors.append("instance.name must be a non-empty string")
        name = DEFAULT_INSTANCE_NAME
    elif explicit and name == DEFAULT_INSTANCE_NAME:
        errors.append(
            'instance.name resolves to "default", but this config was loaded '
            "via an explicit --config path. \"default\" is reserved for the "
            f"global config at {DEFAULT_CONFIG_PATH} (read when --config is "
            "omitted) -- reusing it here would silently collide with that "
            "instance's state file, log file, and launchd label. Set "
            "instance.name to something else (e.g. the target project or "
            "Discord server name)."
        )

    discord = raw.get("discord", {}) if isinstance(raw, dict) else {}

    bot_token = os.environ.get(BOT_TOKEN_ENV_VAR) or discord.get("bot_token")
    if not bot_token or not isinstance(bot_token, str):
        errors.append(
            "discord.bot_token is required (or set the "
            f"{BOT_TOKEN_ENV_VAR} environment variable)"
        )

    channel_id = discord.get("channel_id")
    if not _is_snowflake(channel_id):
        errors.append(
            "discord.channel_id is required and must be a positive integer "
            f"(a Discord channel id), got: {channel_id!r}"
        )

    allowed_user_ids_raw = discord.get("allowed_user_ids")
    allowed_user_ids = ()
    if not isinstance(allowed_user_ids_raw, list) or not allowed_user_ids_raw:
        errors.append(
            "discord.allowed_user_ids is required and must be a non-empty "
            "list of Discord user id integers"
        )
    else:
        bad = [v for v in allowed_user_ids_raw if not _is_snowflake(v)]
        if bad:
            errors.append(
                "discord.allowed_user_ids must contain only positive "
                f"integers, got invalid entries: {bad!r}"
            )
        else:
            allowed_user_ids = tuple(allowed_user_ids_raw)

    claude = raw.get("claude", {}) if isinstance(raw, dict) else {}

    cwd_raw = claude.get("cwd")
    claude_cwd = None
    if not cwd_raw or not isinstance(cwd_raw, str):
        errors.append(
            "claude.cwd is required and must be an absolute path to the "
            "target Claude Code project"
        )
    else:
        claude_cwd = Path(cwd_raw).expanduser()
        if not claude_cwd.is_absolute():
            errors.append(f"claude.cwd must be an absolute path, got: {cwd_raw!r}")

    permission_mode = claude.get("permission_mode", "bypassPermissions")
    if permission_mode not in ALLOWED_PERMISSION_MODES:
        errors.append(
            f"claude.permission_mode={permission_mode!r} is not supported. "
            f"disclaude v1 accepts only {list(ALLOWED_PERMISSION_MODES)!r} -- "
            "a turn run from Discord has no channel to answer an interactive "
            "permission prompt, so any other mode would hang forever. See "
            "the README security warning before changing this."
        )

    model = claude.get("model")
    if model is not None and not isinstance(model, str):
        errors.append("claude.model must be a string if set")

    system_prompt_append = claude.get(
        "system_prompt_append", DEFAULT_SYSTEM_PROMPT_APPEND
    )
    if not isinstance(system_prompt_append, str):
        errors.append("claude.system_prompt_append must be a string if set")

    runtime = raw.get("runtime", {}) if isinstance(raw, dict) else {}

    max_concurrent = runtime.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
    if (
        not isinstance(max_concurrent, int)
        or isinstance(max_concurrent, bool)
        or max_concurrent < 1
    ):
        errors.append("runtime.max_concurrent must be a positive integer")

    if errors:
        where = f" in {source_path}" if source_path else ""
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"Invalid disclaude config{where}:\n{bullet_list}")

    state_file_raw = runtime.get("state_file")
    state_file = (
        Path(state_file_raw).expanduser() if state_file_raw else default_state_file(name)
    )

    return DisclaudeConfig(
        name=name,
        bot_token=bot_token,
        channel_id=channel_id,
        allowed_user_ids=allowed_user_ids,
        claude_cwd=claude_cwd,
        permission_mode=permission_mode,
        model=model,
        system_prompt_append=system_prompt_append,
        max_concurrent=max_concurrent,
        state_file=state_file,
        log_file=default_log_file(name),
        source_path=source_path,
    )


def _is_snowflake(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
