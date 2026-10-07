"""Unit tests for whipit.state session tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile
import unittest

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
        self.assertEqual(data["subagents_spawned"], 0)
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
        self.assertEqual(data["limits"]["detected_phrase"], "no subagents")

    def test_increment_and_blocked(self):
        session = SessionState("test-session-3", state_dir=self.state_dir)
        c1 = session.increment_spawned(1)
        self.assertEqual(c1, 1)
        c2 = session.increment_spawned(2)
        self.assertEqual(c2, 3)

        b1 = session.record_blocked_override()
        self.assertEqual(b1, 1)
        b2 = session.record_blocked_override()
        self.assertEqual(b2, 2)

        data = session.read()
        self.assertEqual(data["subagents_spawned"], 3)
        self.assertEqual(data["overrides_blocked"], 2)

    def test_reset(self):
        session = SessionState("test-session-4", state_dir=self.state_dir)
        session.increment_spawned(5)
        self.assertTrue(session.file_path.exists())
        session.reset()
        self.assertFalse(session.file_path.exists())
        data = session.read()
        self.assertEqual(data["subagents_spawned"], 0)


if __name__ == "__main__":
    unittest.main()
