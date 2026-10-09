"""Exercise qualification selection with mocked GitHub responses."""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("node"), "node is required for workflow policy tests")
class ReviewPolicyTests(unittest.TestCase):
    def policy(self, **case):
        workflow = (ROOT / ".github/workflows/adversarial-review.yml").read_text()
        policy = textwrap.dedent(
            workflow.split("script: |\n", 1)[1].split("\n  adversarial_review:", 1)[0]
        )
        harness = """
const input = JSON.parse(process.argv[1]);
const context = {repo: {owner: 'awill1988', repo: 'whip-it'}, issue: {number: 1},
  eventName: input.event ?? 'pull_request',
  payload: {pull_request: input.event === 'workflow_dispatch' ? null :
    {draft: input.draft ?? false, additions: input.lines ?? 10}}};
const outputs = {};
const core = {setOutput: (k,v) => outputs[k] = v};
const github = {
  rest: {pulls: {listFiles: () => {}}},
  paginate: async () => input.files ?? []
};
"""
        result = subprocess.run(
            [
                "node",
                "-e",
                harness
                + "\n(async () => {\n"
                + policy
                + "\n})().then(() => console.log(JSON.stringify(outputs)));",
                json.dumps(case),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_reviewer_changes_and_manual_runs_qualify(self):
        for case in (
            {"event": "workflow_dispatch"},
            {"files": [{"filename": "tools/adversarial_reviewer/kimi_client.py"}]},
            {"files": [{"filename": ".github/workflows/adversarial-review.yml"}]},
            {
                "files": [
                    {
                        "filename": "removed.py",
                        "previous_filename": "tools/adversarial_reviewer/old.py",
                    }
                ]
            },
        ):
            with self.subTest(case=case):
                self.assertEqual(self.policy(**case), {"qualify": "true"})

    def test_ordinary_changes_skip_only_qualification_regardless_of_size(self):
        for lines in (10, 1000, 1001, 100000):
            with self.subTest(lines=lines):
                self.assertEqual(
                    self.policy(lines=lines, files=[{"filename": "native/main.rs"}]),
                    {"qualify": "false"},
                )

    def test_drafts_do_not_qualify(self):
        self.assertEqual(
            self.policy(draft=True, files=[{"filename": "tools/adversarial_reviewer/test.py"}]),
            {"qualify": "false"},
        )
