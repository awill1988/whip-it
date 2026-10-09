"""Qualification must stop when further inference cannot make the gate pass."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import evaluate_model


class TestModelQualification(unittest.TestCase):
    def test_failed_case_stops_qualification(self):
        with (
            patch("sys.argv", ["evaluate_model", "--runner", "runner"]),
            patch.object(
                evaluate_model,
                "run_model_reviewer",
                return_value=("COMMENT", [], "review incomplete: runner timed out."),
            ) as reviewer,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(evaluate_model.main(), 1)
        self.assertEqual(reviewer.call_count, 1)
        self.assertIn("starting case 1/6", output.getvalue())
        self.assertIn('"duration_seconds"', output.getvalue())

    def test_all_cases_share_qualification_deadline(self):
        with (
            patch("sys.argv", ["evaluate_model", "--runner", "runner"]),
            patch.object(
                evaluate_model,
                "run_model_reviewer",
                side_effect=[(case[3], [], "assessed") for case in evaluate_model.CASES],
            ) as reviewer,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(evaluate_model.main(), 0)
        deadlines = [call.kwargs["deadline"] for call in reviewer.call_args_list]
        self.assertEqual(len(deadlines), len(evaluate_model.CASES))
        self.assertEqual(len(set(deadlines)), 1)
