"""Exercise API failures without credentials or live requests."""

import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import kimi_client as kimi


def response(content=None, finish="stop"):
    return {
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class TestKimiClient(unittest.TestCase):
    def invoke(self, payload=None, errors=None):
        opener = MagicMock()
        stream = opener.open.return_value.__enter__.return_value
        stream.read.return_value = json.dumps(payload).encode()
        if errors:
            opener.open.side_effect = errors
        with (
            patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}),
            patch.object(kimi.urllib.request, "build_opener", return_value=opener),
            patch.object(kimi.time, "sleep"),
        ):
            result = kimi.request([{"role": "user", "content": "review JSON"}])
        return result, opener

    def test_request_contract_and_usage(self):
        value = {"rationale": "checked the branch", "findings": [], "abstention": ""}
        result, opener = self.invoke(response(value))
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, kimi.ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(body["model"], "kimi-k3")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 8192)
        self.assertNotIn("temperature", body)
        self.assertEqual(result["response"], value)
        self.assertEqual(result["usage"]["total_tokens"], 15)

    def test_cache_key_is_stable_and_usage_is_numeric_only(self):
        value = response({"rationale": "checked branch", "findings": [], "abstention": ""})
        value["usage"]["prompt_tokens_details"] = {
            "cached_tokens": 8,
            "cache_write_tokens": 2,
            "secret": "omit",
        }
        with patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_HEAD_REF": "release/test"}
        ):
            result, first = self.invoke(value)
            _, second = self.invoke(value)
        first_key = json.loads(first.open.call_args.args[0].data)["prompt_cache_key"]
        self.assertEqual(
            first_key, json.loads(second.open.call_args.args[0].data)["prompt_cache_key"]
        )
        self.assertNotIn("owner/repo", first_key)
        self.assertEqual(result["usage"]["cached_tokens"], 8)
        self.assertEqual(result["usage"]["cache_write_tokens"], 2)
        self.assertNotIn("secret", result["usage"])

    def test_missing_key_makes_no_request(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(kimi.urllib.request, "build_opener") as opener,
            self.assertRaisesRegex(kimi.KimiError, "missing KIMI_API_KEY"),
        ):
            kimi.request([])
        opener.assert_not_called()

    def test_only_explicit_transient_statuses_retry_once(self):
        for status, calls in ((401, 1), (403, 1), (302, 1), (429, 2), (500, 2), (503, 2)):
            with self.subTest(status=status):
                opener = MagicMock()
                opener.open.side_effect = [
                    urllib.error.HTTPError(kimi.ENDPOINT, status, "private text", {}, None)
                    for _ in range(2)
                ]
                with (
                    patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}),
                    patch.object(kimi.urllib.request, "build_opener", return_value=opener),
                    patch.object(kimi.time, "sleep"),
                    self.assertRaisesRegex(kimi.KimiError, f"^kimi HTTP {status}$"),
                ):
                    kimi.request([])
                self.assertEqual(opener.open.call_count, calls)

    def test_transient_retry_can_succeed(self):
        stream = MagicMock()
        stream.__enter__.return_value.read.return_value = json.dumps(response({})).encode()
        result, opener = self.invoke(
            errors=[urllib.error.HTTPError(kimi.ENDPOINT, 429, "", {}, None), stream]
        )
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(result["response"], {})

    def test_quota_exhaustion_does_not_retry_or_expose_message(self):
        error_body = json.dumps(
            {
                "error": {
                    "type": "exceeded_current_quota_error",
                    "message": "private account information",
                }
            }
        ).encode()
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            kimi.ENDPOINT, 429, "", {}, io.BytesIO(error_body)
        )
        with (
            patch.dict(os.environ, {"KIMI_API_KEY": "test-key"}),
            patch.object(kimi.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(
                kimi.KimiError, "^kimi HTTP 429: account balance or quota exhausted$"
            ),
        ):
            kimi.request([])
        self.assertEqual(opener.open.call_count, 1)

    def test_incomplete_or_malformed_responses_fail(self):
        for payload in (response({}, "length"), response({}, "tool_calls"), {}, [], response()):
            with self.subTest(payload=payload):
                if payload == response():
                    payload["choices"][0]["message"]["content"] = "invalid JSON"
                with self.assertRaises(kimi.KimiError):
                    self.invoke(payload)

    def test_network_errors_do_not_retry_or_expose_details(self):
        with self.assertRaisesRegex(kimi.KimiError, "^kimi connection failed or timed out$"):
            self.invoke(errors=[urllib.error.URLError("private credentials")])

    def test_expired_deadline_does_not_spawn_worker(self):
        with (
            patch.object(kimi.subprocess, "run") as run,
            self.assertRaisesRegex(kimi.KimiError, "deadline exhausted"),
        ):
            kimi.complete([], 0)
        run.assert_not_called()

    def test_stalled_worker_is_terminated_with_progress(self):
        real_run = subprocess.run

        def stalled(*args, **kwargs):
            return real_run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

        logs = io.StringIO()
        with (
            patch.object(kimi.subprocess, "run", side_effect=stalled),
            patch.object(kimi, "CALL_SECONDS", 0.2),
            patch.object(kimi, "PROGRESS_SECONDS", 0.01),
            redirect_stderr(logs),
            self.assertRaisesRegex(kimi.KimiError, "execution deadline"),
        ):
            kimi.complete([], time.monotonic() + 10)
        self.assertIn("model=kimi-k3 reasoning_effort=low", logs.getvalue())
        self.assertIn("kimi waiting for completion", logs.getvalue())
        self.assertIn("kimi call finished", logs.getvalue())

    def test_redirects_are_refused(self):
        self.assertIsNone(
            kimi.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other")
        )
