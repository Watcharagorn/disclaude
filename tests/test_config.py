"""Tests for disclaude.config -- pure, no third-party imports (tomllib is
stdlib), runs on system Python without the venv."""

import os
import tempfile
import unittest
from pathlib import Path

from disclaude import config


def base_raw(**overrides):
    raw = {
        "instance": {"name": "test-instance"},
        "discord": {
            "bot_token": "placeholder-token",
            "channel_id": 111111111111111111,
            "allowed_user_ids": [222222222222222222],
        },
        "claude": {"cwd": "/abs/path/to/project"},
    }
    for dotted, value in overrides.items():
        section, _, key = dotted.partition(".")
        raw.setdefault(section, {})[key] = value
    return raw


class ValidateConfigTests(unittest.TestCase):
    def test_valid_config_returns_expected_fields(self):
        cfg = config.validate_config(base_raw(), source_path=Path("/tmp/x.toml"), explicit=True)
        self.assertEqual(cfg.name, "test-instance")
        self.assertEqual(cfg.bot_token, "placeholder-token")
        self.assertEqual(cfg.channel_id, 111111111111111111)
        self.assertEqual(cfg.allowed_user_ids, (222222222222222222,))
        self.assertEqual(cfg.claude_cwd, Path("/abs/path/to/project"))
        self.assertEqual(cfg.permission_mode, "bypassPermissions")
        self.assertIsNone(cfg.model)
        self.assertEqual(cfg.max_concurrent, config.DEFAULT_MAX_CONCURRENT)

    def test_defaults_derive_state_and_log_paths_from_name(self):
        cfg = config.validate_config(base_raw(), source_path=Path("/tmp/x.toml"), explicit=True)
        self.assertEqual(cfg.state_file, config.default_state_file("test-instance"))
        self.assertEqual(cfg.log_file, config.default_log_file("test-instance"))
        self.assertIn("test-instance", str(cfg.state_file))
        self.assertIn("test-instance", str(cfg.log_file))

    def test_missing_bot_token_is_rejected(self):
        raw = base_raw()
        del raw["discord"]["bot_token"]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("bot_token", str(ctx.exception))

    def test_bot_token_env_var_overrides_file(self):
        raw = base_raw()
        raw["discord"]["bot_token"] = "from-file"
        os.environ[config.BOT_TOKEN_ENV_VAR] = "from-env"
        self.addCleanup(os.environ.pop, config.BOT_TOKEN_ENV_VAR, None)
        cfg = config.validate_config(raw, source_path=None, explicit=True)
        self.assertEqual(cfg.bot_token, "from-env")

    def test_missing_channel_id_is_rejected(self):
        raw = base_raw()
        del raw["discord"]["channel_id"]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("channel_id", str(ctx.exception))

    def test_non_snowflake_channel_id_is_rejected(self):
        raw = base_raw()
        raw["discord"]["channel_id"] = "not-a-number"
        with self.assertRaises(config.ConfigError):
            config.validate_config(raw, source_path=None, explicit=True)

    def test_empty_allowed_user_ids_is_rejected(self):
        raw = base_raw()
        raw["discord"]["allowed_user_ids"] = []
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("allowed_user_ids", str(ctx.exception))

    def test_non_integer_allowed_user_id_is_rejected(self):
        raw = base_raw()
        raw["discord"]["allowed_user_ids"] = [111, "bad"]
        with self.assertRaises(config.ConfigError):
            config.validate_config(raw, source_path=None, explicit=True)

    def test_missing_cwd_is_rejected(self):
        raw = base_raw()
        del raw["claude"]["cwd"]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("claude.cwd", str(ctx.exception))

    def test_relative_cwd_is_rejected(self):
        raw = base_raw()
        raw["claude"]["cwd"] = "relative/path"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("absolute", str(ctx.exception))

    def test_permission_mode_defaults_to_bypass_permissions(self):
        cfg = config.validate_config(base_raw(), source_path=None, explicit=True)
        self.assertEqual(cfg.permission_mode, "bypassPermissions")

    def test_non_bypass_permission_mode_is_rejected(self):
        raw = base_raw()
        raw["claude"]["permission_mode"] = "acceptEdits"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        self.assertIn("permission_mode", str(ctx.exception))
        self.assertIn("bypassPermissions", str(ctx.exception))

    def test_default_permission_mode_only_default_bypass_is_advertised(self):
        self.assertEqual(config.ALLOWED_PERMISSION_MODES, ("bypassPermissions",))

    def test_custom_system_prompt_append_is_honored(self):
        raw = base_raw()
        raw["claude"]["system_prompt_append"] = "custom append text"
        cfg = config.validate_config(raw, source_path=None, explicit=True)
        self.assertEqual(cfg.system_prompt_append, "custom append text")

    def test_default_system_prompt_append_covers_a3_and_a4(self):
        cfg = config.validate_config(base_raw(), source_path=None, explicit=True)
        self.assertIn("table", cfg.system_prompt_append.lower())
        self.assertIn("assumption", cfg.system_prompt_append.lower())

    def test_max_concurrent_defaults_and_rejects_non_positive(self):
        cfg = config.validate_config(base_raw(), source_path=None, explicit=True)
        self.assertEqual(cfg.max_concurrent, 3)

        raw = base_raw()
        raw["runtime"] = {"max_concurrent": 0}
        with self.assertRaises(config.ConfigError):
            config.validate_config(raw, source_path=None, explicit=True)

    def test_explicit_state_file_override_is_honored(self):
        raw = base_raw()
        raw["runtime"] = {"state_file": "/custom/state.json"}
        cfg = config.validate_config(raw, source_path=None, explicit=True)
        self.assertEqual(cfg.state_file, Path("/custom/state.json"))

    def test_multiple_errors_are_all_reported_together(self):
        raw = {"instance": {}, "discord": {}, "claude": {}}
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=None, explicit=True)
        message = str(ctx.exception)
        self.assertIn("bot_token", message)
        self.assertIn("channel_id", message)
        self.assertIn("allowed_user_ids", message)
        self.assertIn("claude.cwd", message)


class DefaultInstanceCollisionGuardTests(unittest.TestCase):
    """The 2026-08-12 owner directive: an explicitly-passed --config whose
    instance.name resolves to "default" must be rejected, since it would
    silently collide with the global default instance's state/log paths
    and launchd label."""

    def test_explicit_config_named_default_is_rejected(self):
        raw = base_raw()
        raw["instance"]["name"] = "default"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=Path("/tmp/other.toml"), explicit=True)
        message = str(ctx.exception)
        self.assertIn("default", message)
        self.assertIn("--config", message)

    def test_explicit_config_with_missing_name_also_resolves_to_default_and_is_rejected(self):
        raw = base_raw()
        del raw["instance"]["name"]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(raw, source_path=Path("/tmp/other.toml"), explicit=True)
        self.assertIn("default", str(ctx.exception))

    def test_non_explicit_config_named_default_is_allowed(self):
        raw = base_raw()
        raw["instance"]["name"] = "default"
        cfg = config.validate_config(raw, source_path=config.DEFAULT_CONFIG_PATH, explicit=False)
        self.assertEqual(cfg.name, "default")

    def test_non_explicit_config_with_missing_name_defaults_to_default_and_is_allowed(self):
        raw = base_raw()
        del raw["instance"]["name"]
        cfg = config.validate_config(raw, source_path=config.DEFAULT_CONFIG_PATH, explicit=False)
        self.assertEqual(cfg.name, config.DEFAULT_INSTANCE_NAME)

    def test_explicit_config_with_non_default_name_is_allowed(self):
        cfg = config.validate_config(base_raw(), source_path=Path("/tmp/other.toml"), explicit=True)
        self.assertEqual(cfg.name, "test-instance")


class LoadConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _write(self, name, text):
        path = Path(self._tmpdir.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_raises_actionable_error(self):
        missing = Path(self._tmpdir.name) / "nope.toml"
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(missing)
        self.assertIn(str(missing), str(ctx.exception))
        self.assertIn("examples/disclaude.toml.example", str(ctx.exception))

    def test_invalid_toml_raises_actionable_error(self):
        bad = self._write("bad.toml", "this is not [valid toml")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(bad)
        self.assertIn("TOML", str(ctx.exception))

    def test_loading_an_explicit_path_sets_explicit_true(self):
        good = self._write(
            "good.toml",
            """
            [instance]
            name = "my-instance"

            [discord]
            bot_token = "t"
            channel_id = 111111111111111111
            allowed_user_ids = [222222222222222222]

            [claude]
            cwd = "/abs/path"
            """,
        )
        cfg = config.load_config(good)
        self.assertEqual(cfg.name, "my-instance")
        self.assertEqual(cfg.source_path, good)

    def test_loading_default_path_named_default_succeeds_only_when_path_is_none(self):
        good = self._write(
            "default-named.toml",
            """
            [instance]
            name = "default"

            [discord]
            bot_token = "t"
            channel_id = 111111111111111111
            allowed_user_ids = [222222222222222222]

            [claude]
            cwd = "/abs/path"
            """,
        )
        # Passed explicitly (even though the file happens to say "default")
        # -- the guard fires because the *call* was explicit.
        with self.assertRaises(config.ConfigError):
            config.load_config(good)


if __name__ == "__main__":
    unittest.main()
