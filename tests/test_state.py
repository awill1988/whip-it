"""Unit tests for whipit.state session tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json
import tempfile
import unittest
from unittest.mock import patch

from whipit.detector import PromptLimits
from whipit.state import SessionState


class TestSessionState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_state(self):
        session = SessionState("test-session-1", state_dir=self.state_dir)
        data = session.read()
        self.assertEqual(data["session_id"], "test-session-1")
        self.assertEqual(data["subagents_reserved"], 0)
        self.assertEqual(data["overrides_blocked"], 0)
        self.assertIsNone(data["limits"])

    def test_record_prompt_limits(self):
        session = SessionState("test-session-2", state_dir=self.state_dir)
        limits = PromptLimits(
            subagents_allowed=False, max_subagents=0, detected_phrase="no subagents"
        )
        session.record_prompt_limits(limits)

        data = session.read()
        self.assertIsNotNone(data["limits"])
        self.assertFalse(data["limits"]["subagents_allowed"])
        self.assertNotIn("detected_phrase", data["limits"])

    def test_increment_and_blocked(self):
        session = SessionState("test-session-3", state_dir=self.state_dir)
        c1 = session.reserve_subagents(1)
        self.assertEqual(c1, 1)
        c2 = session.reserve_subagents(2)
        self.assertEqual(c2, 3)

        b1 = session.record_blocked_override()
        self.assertEqual(b1, 1)
        b2 = session.record_blocked_override()
        self.assertEqual(b2, 2)

        data = session.read()
        self.assertEqual(data["subagents_reserved"], 3)
        self.assertEqual(data["overrides_blocked"], 2)

    def test_reset(self):
        session = SessionState("test-session-4", state_dir=self.state_dir)
        session.reserve_subagents(5)
        self.assertTrue(session.file_path.exists())
        session.reset()
        self.assertFalse(session.file_path.exists())
        data = session.read()
        self.assertEqual(data["subagents_reserved"], 0)

    def test_legacy_state_is_scrubbed_on_update(self):
        session = SessionState("legacy-session", state_dir=self.state_dir)
        session.file_path.parent.mkdir(parents=True)
        session.file_path.write_text(
            json.dumps(
                {
                    "session_id": "legacy-session",
                    "subagents_spawned": 1,
                    "limits": {"subagents_allowed": False, "detected_phrase": "private prompt"},
                    "overrides_blocked": 0,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
        )
        session.record_blocked_override()
        disk = session.file_path.read_text()
        self.assertNotIn("private prompt", disk)
        self.assertNotIn("created_at", disk)
        self.assertEqual(session.read()["subagents_reserved"], 1)

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        session = SessionState("atomic", state_dir=self.state_dir)
        session.reserve_subagents(1)
        with patch("pathlib.Path.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                session.reserve_subagents(1)
        self.assertEqual(session.read()["subagents_reserved"], 1)
        self.assertEqual(tuple(session.file_path.parent.glob(".*")), ())

    def test_exclusive_temporary_file_does_not_overwrite_or_delete_collision(self):
        session = SessionState("collision", state_dir=self.state_dir)
        session.file_path.parent.mkdir(parents=True)
        candidate = session.file_path.parent / f".{session.file_path.name}.{'00' * 16}"
        candidate.write_text("existing")
        with patch("whipit.state.os.urandom", return_value=b"\0" * 16):
            with self.assertRaises(FileExistsError):
                session.reserve_subagents(1)
        self.assertEqual(candidate.read_text(), "existing")


if __name__ == "__main__":
    unittest.main()
