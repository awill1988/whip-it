"""Import verified CI archives into the plugin, or check the bundled release."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "universal-apple-darwin",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources():
    return sorted(
        [ROOT / "Cargo.toml", ROOT / "Cargo.lock", ROOT / ".cargo/config.toml"]
        + list((ROOT / "native").rglob("*.rs"))
    )


def check():
    manifest = json.loads((ROOT / "bin/native/manifest.json").read_text())
    assert manifest["sources"] == {
        str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in sources()
    }, "native sources changed; refresh the bundled CI executables"
    expected = {f"{target}/whip-it" + (".exe" if "windows" in target else "") for target in TARGETS}
    assert set(manifest["binaries"]) == expected, "incomplete platform bundle"
    for name, checksum in manifest["binaries"].items():
        path = ROOT / "bin/native" / name
        data = path.read_bytes()
        assert digest(data) == checksum, f"checksum mismatch: {name}"
        for marker in (b"resourceSpans", b"scopeSpans", b"whipit.duration.", b"WHIP_IT_TRACE"):
            assert marker not in data, f"diagnostics found in {name}"
    print("bundled executables match recorded hashes and native sources")


def bundle(artifacts, source_commit):
    source_hashes = {}
    for path in sources():
        name = str(path.relative_to(ROOT))
        built = subprocess.check_output(["git", "show", f"{source_commit}:{name}"], cwd=ROOT)
        assert built == path.read_bytes(), f"CI source differs: {name}"
        source_hashes[name] = digest(built)
    binaries = {}
    for target in TARGETS:
        windows = "windows" in target
        name = "whip-it.exe" if windows else "whip-it"
        filename = f"whip-it-{target}" + (".zip" if windows else ".tar.gz")
        matches = list(artifacts.rglob(filename))
        assert len(matches) == 1, f"expected one archive: {filename}"
        archive = matches[0]
        checksum = archive.with_name(filename + ".sha256").read_text().split()[0]
        assert digest(archive.read_bytes()) == checksum, f"invalid archive: {filename}"
        if windows:
            with zipfile.ZipFile(archive) as stream:
                data = stream.read(name)
        else:
            with tarfile.open(archive) as stream:
                member = stream.getmember(name)
                assert member.isfile(), f"expected regular executable: {filename}"
                data = stream.extractfile(member).read()
        binaries[f"{target}/{name}"] = data
    destination = ROOT / "bin/native"
    for name, data in binaries.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "source_commit": source_commit,
                "sources": source_hashes,
                "binaries": {name: digest(data) for name, data in binaries.items()},
            },
            indent=2,
        )
        + "\n"
    )
    check()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    if args.artifacts:
        if not args.source_commit:
            parser.error("--source-commit is required with --artifacts")
        bundle(args.artifacts, args.source_commit)
    else:
        check()
