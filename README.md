# disclaude

A standalone, self-hostable Discord ↔ Claude chat engine. Message a dedicated
channel in your own private Discord server; disclaude spins up a
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
session in a Discord thread, with the same tools, CLAUDE.md, skills, hooks,
and settings an interactive `claude` session in that project would get, and
replies in the thread. Reply in the thread to continue the same session.

Each instance is one config file, one process, one Discord server, one
target project, and one Claude subscription -- your own. There is no hosted
or multi-tenant version; you run this on a machine you control, against a
Claude Code login you control.

> [!WARNING]
> **Read the [Security](#security) section before you invite anyone to
> your server.** disclaude's default mode gives every allowlisted Discord
> account full local control of your target project -- the same power an
> interactive `claude` session has, with no per-action approval.

## How it works

1. You post a message in the configured channel. disclaude creates a
   Discord thread named from your message and starts a Claude session with
   `cwd` set to your target project.
2. Claude's reply streams back into the thread, chunked to fit Discord's
   2000-character message limit.
3. You reply inside that thread to continue the same session -- disclaude
   resumes it by the Claude session id it recorded for that thread.
4. Two top-level messages in a row get two threads and two independent,
   concurrent sessions.
5. Anything from an unlisted channel, or a listed channel but an unlisted
   user, is dropped silently (one log line, no reply).

## Prerequisites

- **Python 3.11 or newer.** disclaude's config parser uses the stdlib
  `tomllib`, added in 3.11.
  - If your `python3` is very new (e.g. 3.14, released only recently),
    some dependencies may not yet publish prebuilt wheels for it, and
    `pip install` can sit for many minutes building one from source
    instead of failing fast. If `pip install -e .` seems to hang, Ctrl-C
    it and create the venv with a slightly older 3.11-3.13 interpreter
    instead (e.g. `brew install python@3.12`, then
    `python3.12 -m venv .venv`) -- wheel availability, not disclaude
    itself, is the constraint.
- **Claude Code installed and logged in** under the subscription you want
  turns to bill. Either:
  - run `claude` interactively once and complete the login flow, or
  - run `claude setup-token` for a long-lived token suitable for a
    headless/daemon environment.

  `disclaude check` (below) reads this login state and warns -- it does
  not fail the check -- if it can't confirm you're logged in.
- **A Discord account** to create the bot application and a server (yours,
  or one where you can create channels) to install it into.

## 1. Create the Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. **New Application**, name it whatever you like.
2. **Bot** tab:
   - **Reset Token**, copy it somewhere safe -- you'll paste it into your
     config next and it won't be shown again.
   - Turn **Public Bot** off. This app should only ever be installable by
     you.
   - Under **Privileged Gateway Intents**, enable **Message Content
     Intent**. Without it, disclaude receives message events with no
     text.
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`.
   - Bot Permissions: **View Channel**, **Send Messages**, **Create
     Public Threads**, **Send Messages in Threads**, **Read Message
     History**.
   - Open the generated URL and install the app into your (private)
     server.
4. In Discord, create a dedicated text channel (e.g. `#claude`) for this
   instance. With **Developer Mode** on (User Settings → Advanced), Copy
   Channel ID, and copy your own User ID the same way (right-click your
   name).

## 2. Install disclaude

```bash
git clone git@github.com:Watcharagorn/disclaude.git
cd disclaude
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 3. Configure an instance

```bash
mkdir -p ~/.disclaude
cp examples/disclaude.toml.example ~/.disclaude/config.toml
chmod 600 ~/.disclaude/config.toml
```

Edit `~/.disclaude/config.toml` (every field is documented inline):
paste your bot token, channel id, your Discord user id into
`allowed_user_ids`, and `claude.cwd` pointing at the project you want this
instance to chat about. Leave `instance.name` as `"default"` here -- that's
what makes this the config `disclaude run` reads automatically, with no
`--config` flag.

## 4. Validate before connecting anything

```bash
.venv/bin/disclaude check
```

This validates the config, constructs the same objects `run` would (with
your real token, but never uses it), confirms the `claude` binary is on
`PATH`, and reports your Claude login state. It never opens a connection to
Discord. Fix anything it reports `FAIL` on; a `WARN` (e.g. an indeterminate
login state) doesn't block you, but is worth checking before you rely on
this in practice.

## 5. Run it

```bash
.venv/bin/disclaude run
```

Leave that running, then post a message in your configured channel. Ctrl-C
stops it.

## 6. Run it as a background service (launchd, macOS)

```bash
cp examples/com.disclaude.example.plist ~/Library/LaunchAgents/com.disclaude.default.plist
```

Edit the copy: replace every `<name>` with `default`, and every
`/path/to/...` with real absolute paths (your clone's `.venv/bin/python`,
your config, a log path such as `~/.disclaude/logs/default.log`). Then:

```bash
mkdir -p ~/.disclaude/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.disclaude.default.plist
launchctl kickstart -k gui/$(id -u)/com.disclaude.default
```

Confirm it's actually up, not just loaded:

```bash
launchctl list | grep com.disclaude.default   # shows a PID
tail -f ~/.disclaude/logs/default.log          # look for the "READY" line
```

`KeepAlive` restarts the process if it crashes or the Mac reboots; the
gateway session resumes and your existing thread↔session map (in
`~/.disclaude/state/default.json`) is unaffected.

## Security

> [!WARNING]
> The default `claude.permission_mode = "bypassPermissions"` means **anyone
> in `allowed_user_ids` has the same local power over `claude.cwd` as
> someone sitting at your keyboard running `claude` themselves** --
> reading, writing, and executing, with no per-action confirmation. The
> boundary disclaude enforces is *who can talk to the instance*, not *what
> the instance is allowed to do once they do*. There is no other
> `permission_mode` in v1 (see [Out of scope](#out-of-scope)) precisely
> because a turn started from Discord has no channel to answer an
> interactive approval prompt -- config validation rejects any other
> value.

Given that, treat an allowlisted Discord account like a credential:

- Keep the server **private** and **Public Bot disabled** (step 1 above).
- Keep `allowed_user_ids` as short as possible -- ideally just you.
- Enable **Discord two-factor authentication** on every allowlisted
  account.
- The bot token lives only in your config file (`chmod 600`) or the
  `DISCLAUDE_BOT_TOKEN` environment variable. Rotating it is a Developer
  Portal reset plus one edit; the repo's `.gitignore` keeps any real
  config out of version control.
- Never put `ANTHROPIC_API_KEY` in the environment a disclaude instance
  runs in -- see [Billing](#billing-and-quota) below.

## Known limitations

- **Dropped messages during downtime.** disclaude does no channel-history
  backfill on startup. A message sent while the process is down, once
  Discord's own gateway-resume window (a few minutes) has elapsed, is
  simply never seen. If you need a message answered, wait for the
  `READY` line in the log (or the `launchctl list` PID) before sending it.
- **Attachments are ignored.** v1 is text-only. A message with an
  attachment gets a one-line "ignored" notice in the reply; image support
  is a follow-up.
- **No table rendering.** Discord doesn't render GitHub-flavored markdown
  tables. The built-in system-prompt append tells Claude to avoid them, so
  replies use lists or plain text instead.
- **One channel per instance.** No DMs, no @mention-anywhere. Run a second
  instance (below) if you want a second channel.

## Billing and quota

Turns run under your existing Claude subscription login (OAuth), the same
as an interactive `claude` session -- not the metered Anthropic API. There
is deliberately **no `ANTHROPIC_API_KEY`** in the launchd example's
environment: its presence would outrank the OAuth login and silently
switch every turn to metered API billing, so leave it out of wherever you
run this. Complementing that, leave your account's extra-usage / API-credit
top-up **disabled** in your Anthropic account settings -- that's a
one-time setting no amount of code here can enforce for you.

Because turns share your subscription's usage pool with every other
interactive session under the same login, a disclaude instance running
alongside heavy interactive use can push you toward that pool's limit --
at which point turns pause until the window resets rather than being
auto-billed. Size `runtime.max_concurrent` in your config accordingly.

## Running multiple instances

Nothing here is a singleton -- run as many instances as you want, each with
its own config, launchd job, Discord server/channel, and target project.
Every instance's state, log, and launchd label derive from its
`instance.name`, so distinct names are all two instances need to coexist
without touching each other's files (this is unit-tested -- see
`tests/test_isolation.py`).

Example: a second instance for a different project ("blog"), alongside the
first ("default") from a different Discord server:

```bash
cp examples/disclaude.toml.example ~/.disclaude/blog.toml
chmod 600 ~/.disclaude/blog.toml
# edit ~/.disclaude/blog.toml:
#   instance.name = "blog"          (must NOT be "default" -- see below)
#   discord.bot_token / channel_id / allowed_user_ids for the second server
#   claude.cwd = "/path/to/your/blog/project"

.venv/bin/disclaude check --config ~/.disclaude/blog.toml

cp examples/com.disclaude.example.plist ~/Library/LaunchAgents/com.disclaude.blog.plist
# edit the copy: every <name> -> blog, ProgramArguments' --config ->
# ~/.disclaude/blog.toml, log paths -> ~/.disclaude/logs/blog.log

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.disclaude.blog.plist
launchctl kickstart -k gui/$(id -u)/com.disclaude.blog
```

Two independent daemons now run: `com.disclaude.default` (state
`~/.disclaude/state/default.json`, log `~/.disclaude/logs/default.log`)
and `com.disclaude.blog` (state `~/.disclaude/state/blog.json`, log
`~/.disclaude/logs/blog.log`). Note the one rule this depends on: **any
config passed via an explicit `--config` must set `instance.name` to
something other than `"default"`** -- `"default"` is reserved for the
global config at `~/.disclaude/config.toml` (the one `disclaude run` reads
when `--config` is omitted). Config validation rejects an explicit
`--config` file that still resolves to `"default"`, specifically to catch
exactly this kind of accidental path/label/state collision before it
happens silently.

## Out of scope (v1)

- DMs and @mention-anywhere -- one dedicated channel per instance.
- A hosted, multi-tenant service -- self-hosted, one instance per user, by
  design.
- PyPI publication -- install from the git repo (`pip install -e .` /
  `pipx install git+...`) for now.
- systemd/Windows service templates -- launchd (macOS) only; adapting the
  plist's intent to your init system is on you for now.
- Slash commands, voice, rich embeds -- plain-text chat only.
- Non-`bypassPermissions` modes / interactive approval flows -- config
  validation rejects anything else (see [Security](#security)).
- Channel-history backfill after downtime (see
  [Known limitations](#known-limitations)).

## Development

```bash
python3 -m unittest discover tests
```

`chunker.py`, `state.py`, and `config.py` have zero third-party imports and
their tests run on plain system Python. `tests/test_isolation.py`'s
bridge/engine isolation tests need the venv (discord.py +
claude-agent-sdk) and skip themselves cleanly when it isn't present.

## License

MIT -- see [LICENSE](LICENSE).
