"""Exercise the workflow's shared policy with mocked GitHub responses."""

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
            workflow.split("script: &owner_policy |\n", 1)[1].split("\n  adversarial_review:", 1)[0]
        )
        harness = """
const input = JSON.parse(process.argv[1]);
const pr = {
  state: 'open', draft: false, additions: input.lines ?? 1001, deletions: 0,
  user: {login: input.author ?? 'awill1988'},
  head: {sha: input.head ?? 'head', repo: {full_name: input.repo ?? 'awill1988/whip-it'}},
  base: {sha: 'base', repo: {owner: {type: 'User', login: 'awill1988'}}}
};
const context = {repo: {owner: 'awill1988', repo: 'whip-it'}, issue: {number: 1},
  payload: {pull_request: {draft: input.draft ?? false, head: {sha: 'head'}},
    repository: {full_name: 'awill1988/whip-it'}}};
const outputs = {}, submitted = [];
let gets = 0;
const core = {setOutput: (k,v) => outputs[k] = v, notice: () => {}};
const github = {
  rest: {
    pulls: {
      get: async () => ({data: {...pr, head: {...pr.head,
        sha: ++gets > 1 && input.superseded ? 'new-head' : pr.head.sha}}}),
      listReviews: () => {},
      listFiles: () => {},
      createReview: async review => submitted.push(review)
    },
    repos: {getContent: async args => {
      if (args.ref !== 'base') throw new Error('untrusted ownership ref');
      if (input.missing) throw Object.assign(new Error('missing'), {status: 404});
      return {data: {encoding: 'base64',
        content: Buffer.from(input.owners ?? '* @awill1988').toString('base64')}};
    }}
  },
  paginate: async method => method === github.rest.pulls.listFiles ? (input.files ?? []) : input.duplicate ? [{
    user: {login: 'github-actions[bot]'}, commit_id: 'head', state: 'APPROVED',
    body: '<!-- whip-it-owner-review-exception -->'
  }] : []
};
process.env.SUBMIT_REVIEW = 'true';
process.env.LINE_THRESHOLD = input.threshold ?? '';
"""
        script = (
            harness
            + "\n(async () => {\n"
            + policy
            + """
})().then(() => console.log(JSON.stringify({outputs, submitted})))
 .catch(error => {console.error(error); process.exitCode = 1;});
"""
        )
        return subprocess.run(
            ["node", "-e", script, json.dumps(case)], capture_output=True, text=True, timeout=10
        )

    def test_eligible_and_bootstrap(self):
        for case in ({}, {"missing": True}, {"lines": 1002, "threshold": "1001"}):
            with self.subTest(case=case):
                result = self.policy(**case)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["outputs"]["skip_review"], "true")
                self.assertEqual(report["submitted"][0]["event"], "APPROVE")

    def test_ineligible(self):
        for case in (
            {"lines": 999},
            {"lines": 1000},
            {"draft": True},
            {"head": "stale"},
            {"author": "other"},
            {"repo": "other/whip-it"},
            {"owners": "* @other"},
            {"owners": "* @awill1988\n/private @other"},
            {"owners": "* @org/team"},
            {"owners": ""},
            {"missing": True, "author": "other"},
            {"files": [{"filename": "tools/adversarial_reviewer/kimi_client.py"}]},
            {"files": [{"filename": ".github/workflows/adversarial-review.yml"}]},
        ):
            with self.subTest(case=case):
                result = self.policy(**case)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["outputs"]["skip_review"], "false")
                self.assertEqual(report["submitted"], [])

    def test_stale_and_duplicate_approval(self):
        for case in ({"superseded": True}, {"duplicate": True}):
            result = self.policy(**case)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["submitted"], [])

    def test_invalid_threshold(self):
        for threshold in ("zero", "0", "-1", "9007199254740992"):
            self.assertNotEqual(self.policy(threshold=threshold).returncode, 0)
