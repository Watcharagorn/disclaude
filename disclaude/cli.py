"""disclaude command-line interface: `run` and `check`.

Both subcommands resolve their config through `disclaude.config.load_config`
-- the sole load/validate path, never a parallel implementation -- so a
config that passes `check` behaves identically under `run`.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys

from . import config as config_module

CLAUDE_BINARY = "claude"


def _add_config_arg(parser):
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Path to the instance config TOML. Defaults to "
            f"{config_module.DEFAULT_CONFIG_PATH} when omitted."
        ),
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="disclaude",
        description="A standalone, self-hostable Discord <-> Claude chat engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Connect to Discord and serve this instance.")
    _add_config_arg(run_parser)

    check_parser = sub.add_parser(
        "check",
        help="Validate config and environment without connecting to Discord (dry run).",
    )
    _add_config_arg(check_parser)

    return parser


def _configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def cmd_run(args):
    try:
        cfg = config_module.load_config(args.config)
    except config_module.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _configure_logging()
    # Imported here, not at module top, so `disclaude --help` and config
    # errors never pay discord.py's import cost or require it installed.
    from .bridge import Bridge

    logging.getLogger("disclaude.cli").info(
        "starting instance %r (cwd=%s, channel=%s)",
        cfg.name,
        cfg.claude_cwd,
        cfg.channel_id,
    )
    Bridge(cfg).run_forever()
    return 0


def cmd_check(args):
    """Dry run: validate the config, construct the bridge/engine objects
    with whatever token the config holds (a placeholder is fine -- nothing
    here authenticates it), confirm the `claude` binary is reachable, and
    read its login state. Never connects to the Discord gateway.
    """
    try:
        cfg = config_module.load_config(args.config)
    except config_module.ConfigError as exc:
        print(f"FAIL config: {exc}", file=sys.stderr)
        return 1

    print(f"OK   config: instance {cfg.name!r} loaded from {cfg.source_path}")
    print(f"     claude.cwd:      {cfg.claude_cwd}")
    print(f"     permission_mode: {cfg.permission_mode}")
    print(f"     state file:      {cfg.state_file}")
    print(f"     log file:        {cfg.log_file}")

    ok = True

    try:
        from .bridge import Bridge
        from .engine import Engine

        engine = Engine(cfg)
        Bridge(cfg, engine=engine)
        print("OK   bridge/engine objects constructed (no Discord connection made)")
    except Exception as exc:  # noqa: BLE001 -- surfaced as a check failure, not a crash
        print(f"FAIL bridge/engine construction: {exc}", file=sys.stderr)
        ok = False

    claude_path = shutil.which(CLAUDE_BINARY)
    if claude_path is None:
        print(f"FAIL {CLAUDE_BINARY!r} binary not found on PATH", file=sys.stderr)
        ok = False
    else:
        print(f"OK   claude binary: {claude_path}")
        _check_claude_login()

    if not ok:
        return 1

    print("check passed -- no Discord connection was made")
    return 0


def _check_claude_login():
    """Report Claude Code's own login state via `claude auth status`. Any
    outcome short of a confirmed login prints a WARNING, never a failure --
    an indeterminate result (binary present but the status probe itself
    misbehaves) shouldn't block `check` from otherwise passing.
    """
    try:
        result = subprocess.run(
            [CLAUDE_BINARY, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        status = json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        print(
            f"WARN claude login: could not determine login state ({exc}) "
            "-- this is a warning, not a failure",
            file=sys.stderr,
        )
        return

    if status.get("loggedIn"):
        who = status.get("email") or status.get("authMethod") or "an account"
        print(f"OK   claude login: logged in ({who})")
    else:
        print(
            "WARN claude login: not logged in -- run `claude` interactively "
            "or `claude setup-token` before `disclaude run`",
            file=sys.stderr,
        )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "check":
        return cmd_check(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
