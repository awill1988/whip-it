"""Build a client-installable release directory with one native executable."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def stage(binary, target, destination):
    version = re.search(r'^version = "([^"]+)"', (ROOT / "Cargo.toml").read_text(), re.M)[1]
    name = "whip-it.exe" if "windows" in target else "whip-it"
    for directory in (".claude-plugin", ".codex-plugin", "hooks"):
        shutil.copytree(ROOT / directory, destination / directory)
    for filename in ("plugin.json", "hooks.json", "LICENSE"):
        shutil.copy2(ROOT / filename, destination / filename)
    (destination / "bin").mkdir()
    shutil.copy2(binary, destination / "bin" / name)
    (destination / "bin" / name).chmod(0o755)
    for filename in ("hooks/hooks.json", "hooks/codex-plugin.json", "hooks.json"):
        path = destination / filename
        path.write_text(path.read_text().replace("bin/whip-it", f"bin/{name}"))
    (destination / "release.json").write_text(json.dumps({"version": version, "target": target}))


def package(binary, target, output):
    if not re.fullmatch(r"[a-z0-9_-]+", target):
        raise ValueError("invalid target")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (f"whip-it-plugin-{target}" + (".zip" if "windows" in target else ".tar.gz"))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stage(binary, target, root)
        if "windows" in target:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        stream.write(path, path.relative_to(root).as_posix())
        else:
            with tarfile.open(archive, "w:gz") as stream:
                for path in sorted(root.iterdir()):
                    stream.add(path, arcname=path.name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/native")
    args = parser.parse_args()
    print(package(args.binary, args.target, args.output))
