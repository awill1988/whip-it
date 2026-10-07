"""Unit tests for whipit.config configuration loading."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json
import os
import tempfile
import unittest

from whipit.config import load_config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_config(self):
        config, source = load_config(cwd=self.work_dir)
        self.assertEqual(config["mode"], "enforce")
        self.assertEqual(config["default_max_subagents"], 0)
        self.assertFalse(config["auto_clamp"])

    def test_workspace_file_override(self):
        ws_config = self.work_dir / ".whip-it.json"
        ws_config.write_text(json.dumps({"mode": "advisory", "default_max_subagents": 2}))

        config, source = load_config(cwd=self.work_dir)
        self.assertEqual(config["mode"], "advisory")
        self.assertEqual(config["default_max_subagents"], 2)
        self.assertEqual(source, ws_config.resolve())

    def test_env_var_override(self):
        old_mode = os.environ.get("WHIP_IT_MODE")
        old_max = os.environ.get("WHIP_IT_MAX_SUBAGENTS")
        try:
            os.environ["WHIP_IT_MODE"] = "off"
            os.environ["WHIP_IT_MAX_SUBAGENTS"] = "5"

            config, _ = load_config(cwd=self.work_dir)
            self.assertEqual(config["mode"], "off")
            self.assertEqual(config["default_max_subagents"], 5)
        finally:
            if old_mode:
                os.environ["WHIP_IT_MODE"] = old_mode
            else:
                os.environ.pop("WHIP_IT_MODE", None)

            if old_max:
                os.environ["WHIP_IT_MAX_SUBAGENTS"] = old_max
            else:
                os.environ.pop("WHIP_IT_MAX_SUBAGENTS", None)


if __name__ == "__main__":
    unittest.main()
