#!/usr/bin/env python3
"""Idempotently fetch and verify model weights and llama-cli runner binary."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "adversarial-reviewer"
)


def load_env(env_path: Path) -> dict:
    values = {}
    with open(env_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip()
    return values


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def download_file(urls: list, dest_path: Path, expected_sha256: str = None) -> bool:
    temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    for url in urls:
        if not url:
            continue
        print(f"attempting download from {url}...")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "whip-it-adversarial-reviewer/1.0"}
            )
            with (
                urllib.request.urlopen(req, timeout=60) as response,
                open(temp_path, "wb") as out_file,
            ):
                shutil.copyfileobj(response, out_file)

            if expected_sha256:
                actual_sha256 = compute_sha256(temp_path)
                if actual_sha256.lower() != expected_sha256.lower():
                    print(
                        f"checksum mismatch: expected {expected_sha256}, got {actual_sha256}",
                        file=sys.stderr,
                    )
                    if temp_path.exists():
                        temp_path.unlink()
                    continue

            temp_path.rename(dest_path)
            print(f"successfully downloaded and verified {dest_path.name}")
            return True
        except Exception as error:
            print(f"download failed for {url}: {error}", file=sys.stderr)
            if temp_path.exists():
                temp_path.unlink()

    return False


def ensure_model(cache_dir: Path, config: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_name = config["MODEL_NAME"]
    expected_sha256 = config["MODEL_SHA256"]
    model_path = cache_dir / model_name

    if model_path.exists():
        actual_sha256 = compute_sha256(model_path)
        if actual_sha256.lower() == expected_sha256.lower():
            print(f"model {model_name} already present and verified in cache.")
            return model_path
        print("cached model checksum invalid, refetching...")
        model_path.unlink()

    urls = [config.get("PRIMARY_MODEL_URL"), config.get("FALLBACK_MODEL_URL")]
    if not download_file(urls, model_path, expected_sha256):
        raise RuntimeError(f"failed to download valid model {model_name}")

    return model_path


def ensure_runner(cache_dir: Path, config: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    version = config.get("LLAMA_CLI_VERSION", "latest")
    runner_zip = cache_dir / f"llama-runner-{version}.zip"
    extract_dir = cache_dir / "llama_runner"
    runner_bin = extract_dir / "build" / "bin" / "llama-cli"
    if not runner_bin.exists():
        runner_bin = extract_dir / "llama-cli"

    if runner_bin.exists():
        return runner_bin

    url = config.get("LLAMA_RUNNER_URL")
    if not url:
        return None

    if not runner_zip.exists():
        if not download_file([url], runner_zip):
            print("runner binary download skipped or unavailable.", file=sys.stderr)
            return None

    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(runner_zip, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        runner_bin = extract_dir / "build" / "bin" / "llama-cli"
        if not runner_bin.exists():
            runner_bin = extract_dir / "llama-cli"

        if runner_bin.exists():
            runner_bin.chmod(0o755)
            return runner_bin
    except Exception as error:
        print(f"failed to extract llama runner: {error}", file=sys.stderr)

    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch model and runner for adversarial code review."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=SCRIPT_DIR / "model.env",
        help="Path to model.env configuration file",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Directory to store model and runner binaries",
    )
    args = parser.parse_args()

    if not args.env_file.exists():
        print(f"error: environment file {args.env_file} does not exist", file=sys.stderr)
        return 1

    config = load_env(args.env_file)
    try:
        print(f"using cache directory: {args.cache_dir}")
        model_path = ensure_model(args.cache_dir, config)
        print(f"model ready at: {model_path}")
        runner_path = ensure_runner(args.cache_dir, config)
        if runner_path:
            print(f"runner ready at: {runner_path}")
        else:
            print("runner binary not extracted; system llama-cli or mock mode will be used.")
        return 0
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
