"""CI-only Kimi transport; credentials and raw provider responses stay private."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.moonshot.ai/v1/chat/completions"
MODEL = "kimi-k2.7-code"
OUTPUT_TOKENS = 8192
CALL_SECONDS = 120
PROGRESS_SECONDS = 30
MAX_RESPONSE_BYTES = 1048576  # 1 MiB


class KimiError(Exception):
    """A sanitized error that may be included in a review report."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(messages):
    key = os.environ.get("KIMI_API_KEY", "").strip()
    if not key:
        raise KimiError("missing KIMI_API_KEY repository secret")
    body = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": OUTPUT_TOKENS,
        }
    ).encode()
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(2):
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=CALL_SECONDS) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if attempt == 0 and (status == 429 or 500 <= status <= 599):
                time.sleep(1)
                continue
            raise KimiError(f"kimi HTTP {status}") from None
        except (OSError, urllib.error.URLError):
            raise KimiError("kimi connection failed or timed out") from None
    if len(data) > MAX_RESPONSE_BYTES:
        raise KimiError("kimi response exceeds size limit")
    try:
        response = json.loads(data)
        choice = response["choices"][0]
        if choice["finish_reason"] != "stop":
            raise KimiError("kimi response incomplete or truncated")
        content = choice["message"]["content"]
        result = json.loads(content)
        usage = response.get("usage", {})
        counts = {
            name: value
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(value := usage.get(name)) is int and value >= 0
        }
        return {"response": result, "usage": counts}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise KimiError("kimi returned an invalid structured response") from None


def complete(messages, deadline):
    remaining = min(CALL_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        raise KimiError("assessment deadline exhausted")
    started = time.monotonic()
    finished = threading.Event()

    def progress():
        while not finished.wait(PROGRESS_SECONDS):
            print(
                f"kimi running: {time.monotonic() - started:.0f}s elapsed",
                file=sys.stderr,
                flush=True,
            )

    worker = threading.Thread(target=progress, daemon=True)
    worker.start()
    try:
        # A process deadline also bounds DNS, retries, and slowly delivered HTTP bodies.
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            input=json.dumps(messages),
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        if proc.returncode != 0:
            raise KimiError("kimi worker failed")
        result = json.loads(proc.stdout)
        if "error" in result:
            raise KimiError(result["error"])
        print(f"kimi usage: {json.dumps(result['usage'])}", file=sys.stderr, flush=True)
        return result["response"]
    except subprocess.TimeoutExpired:
        raise KimiError("kimi call exceeded its execution deadline") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise KimiError("kimi worker returned an invalid result") from None
    finally:
        finished.set()
        worker.join()
        print(
            f"kimi call finished after {time.monotonic() - started:.1f}s",
            file=sys.stderr,
            flush=True,
        )


if __name__ == "__main__":
    try:
        print(json.dumps(request(json.load(sys.stdin))))
    except KimiError as error:
        print(json.dumps({"error": str(error)}))
    except Exception:
        print(json.dumps({"error": "kimi worker failed"}))
