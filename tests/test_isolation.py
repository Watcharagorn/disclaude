"""Isolation tests: two differently-configured disclaude instances must
share nothing.

`ConfigPathIsolationTests` is pure (no third-party imports) and runs on
system Python. `BridgeEngineIsolationTests` needs discord.py and
claude-agent-sdk installed, so it constructs `Bridge`/`Engine` objects to
prove they hold no shared state -- the grep for module-level mutable state
(Verification Topic 5) checks the *source*; this checks the *behavior*.
It's skipped, not failed, when the venv isn't present.
"""

import tempfile
import unittest
from pathlib import Path

from disclaude import config

try:
    from disclaude.bridge import Bridge
    from disclaude.engine import Engine

    HAS_RUNTIME_DEPS = True
except ImportError:
    HAS_RUNTIME_DEPS = False


def make_config(name, cwd, channel_id, tmpdir, token="placeholder-token", *, state_in_tmpdir=False):
    """Build a valid config for isolation tests.

    `state_in_tmpdir=False` (the default) leaves `runtime.state_file`
    unset, so it derives the *real* default
    (`~/.disclaude/state/<name>.json`) -- this is what
    `ConfigPathIsolationTests` wants to exercise, and is safe because
    those tests only ever compare the resulting `Path` values, never
    write through them. Any test that actually calls `StateStore.save()`
    (directly, or via `set_session`/`Engine`) must pass
    `state_in_tmpdir=True` -- it must never touch the real user's home
    directory.
    """
    raw = {
        "instance": {"name": name},
        "discord": {
            "bot_token": token,
            "channel_id": channel_id,
            "allowed_user_ids": [999999999999999999],
        },
        "claude": {"cwd": cwd},
    }
    if state_in_tmpdir:
        raw["runtime"] = {"state_file": str(Path(tmpdir) / f"{name}-state.json")}
    return config.validate_config(raw, source_path=Path(tmpdir) / f"{name}.toml", explicit=True)


class ConfigPathIsolationTests(unittest.TestCase):
    def test_two_instances_derive_distinct_state_and_log_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_a = make_config("workspace-a", "/abs/path/a", 111111111111111111, tmp)
            cfg_b = make_config("workspace-b", "/abs/path/b", 222222222222222222, tmp)

            self.assertNotEqual(cfg_a.state_file, cfg_b.state_file)
            self.assertNotEqual(cfg_a.log_file, cfg_b.log_file)
            self.assertNotEqual(cfg_a.claude_cwd, cfg_b.claude_cwd)
            self.assertNotEqual(cfg_a.channel_id, cfg_b.channel_id)
            self.assertIn("workspace-a", str(cfg_a.state_file))
            self.assertIn("workspace-b", str(cfg_b.state_file))

    def test_two_instances_sharing_a_name_would_collide_by_construction(self):
        # Documents *why* the explicit-config "default" collision guard
        # exists in config.py: identical instance.name really does derive
        # identical state/log paths.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_a = make_config("same-name", "/abs/path/a", 111111111111111111, tmp)
            cfg_b = make_config("same-name", "/abs/path/b", 222222222222222222, tmp)
            self.assertEqual(cfg_a.state_file, cfg_b.state_file)
            self.assertEqual(cfg_a.log_file, cfg_b.log_file)


@unittest.skipUnless(
    HAS_RUNTIME_DEPS, "requires the venv (discord.py + claude-agent-sdk installed)"
)
class BridgeEngineIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cfg_a = make_config(
            "workspace-a", "/abs/path/a", 111111111111111111, self._tmpdir.name, state_in_tmpdir=True
        )
        self.cfg_b = make_config(
            "workspace-b", "/abs/path/b", 222222222222222222, self._tmpdir.name, state_in_tmpdir=True
        )

    def test_two_engines_hold_distinct_state_stores_and_semaphores(self):
        engine_a = Engine(self.cfg_a)
        engine_b = Engine(self.cfg_b)

        self.assertIsNot(engine_a.state, engine_b.state)
        self.assertIsNot(engine_a._semaphore, engine_b._semaphore)
        self.assertNotEqual(engine_a.state.path, engine_b.state.path)

        # Mutating one instance's session map must never leak into the other.
        engine_a.state.set_session("thread-1", "session-a")
        self.assertIsNone(engine_b.state.get_session_id("thread-1"))

    def test_two_bridges_hold_distinct_engines_and_configs(self):
        bridge_a = Bridge(self.cfg_a)
        bridge_b = Bridge(self.cfg_b)

        self.assertIsNot(bridge_a.engine, bridge_b.engine)
        self.assertIsNot(bridge_a.cfg, bridge_b.cfg)
        self.assertNotEqual(bridge_a.cfg.claude_cwd, bridge_b.cfg.claude_cwd)

    def test_bridge_and_engine_construct_with_a_placeholder_token(self):
        # What `disclaude check` relies on: neither constructor validates
        # or connects with the token, so a placeholder is enough.
        bridge = Bridge(self.cfg_a)
        self.assertEqual(bridge.cfg.bot_token, "placeholder-token")


if __name__ == "__main__":
    unittest.main()
