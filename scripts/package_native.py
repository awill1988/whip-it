"""Package a native executable with its license and sha256 checksum."""

import argparse
import hashlib
from pathlib import Path
import tarfile
import zipfile

from verify_release import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.binary.is_file():
        parser.error("binary does not exist")
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in args.target):
        parser.error("invalid target")
    verify(args.binary)
    args.output.mkdir(parents=True, exist_ok=True)
    license_path = Path(__file__).resolve().parents[1] / "LICENSE"
    windows = "windows" in args.target
    archive = args.output / (f"whip-it-{args.target}" + (".zip" if windows else ".tar.gz"))
    if windows:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            stream.write(args.binary, "whip-it.exe")
            stream.write(license_path, "LICENSE")
    else:
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(args.binary, arcname="whip-it")
            stream.add(license_path, arcname="LICENSE")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")
    print(archive)


if __name__ == "__main__":
    main()
