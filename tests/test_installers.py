"""Exercise setup without fetching releases or changing the user's installation."""

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "posix installer")
class TestPosixInstaller(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="whip-it-setup-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "tools"
        self.bin.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "SHELL": "/bin/zsh",
            "WHIP_IT_INSTALL_DIR": str(self.root / "user's bin"),
            "WHIP_IT_VERSION": "0.1.0",
            "WHIP_IT_NO_MODIFY_PATH": "0",
            "INSTALL_FIXTURES": str(self.root),
            "INSTALL_TEST_OS": "Linux",
            "INSTALL_TEST_ARCH": "x86_64",
        }
        Path(self.env["HOME"]).mkdir()
        scripts = {
            "uname": (
                "import os,sys\nprint(os.environ['INSTALL_TEST_OS' if sys.argv[1]=='-s' "
                "else 'INSTALL_TEST_ARCH'])\n"
            ),
            "curl": (
                "import os,pathlib,shutil,sys\n"
                "if os.environ.get('INSTALL_TEST_FAIL'): sys.exit(22)\n"
                "url=next(a for a in sys.argv if a.startswith('https://'))\n"
                "source=pathlib.Path(os.environ['INSTALL_FIXTURES'])/url.rsplit('/',1)[1]\n"
                "shutil.copyfile(source,sys.argv[sys.argv.index('-o')+1])\n"
            ),
        }
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o755)

    def archive(self, target):
        path = self.root / f"whip-it-{target}.tar.gz"
        data = b"#!/bin/sh\nprintf 'whip-it 0.1.0\\n'\n"
        with tarfile.open(path, "w:gz") as stream:
            entry = tarfile.TarInfo("whip-it")
            entry.size = len(data)
            entry.mode = 0o755
            stream.addfile(entry, io.BytesIO(data))
        path.with_name(path.name + ".sha256").write_text(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        )
        return path

    def install(self):
        return subprocess.run(
            ["sh", str(ROOT / "install.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_platform_selection_and_repeat_installation(self):
        for system, arch, target in (
            ("Linux", "x86_64", "x86_64-unknown-linux-musl"),
            ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
            ("Darwin", "arm64", "universal-apple-darwin"),
            ("Darwin", "x86_64", "universal-apple-darwin"),
        ):
            self.env.update(INSTALL_TEST_OS=system, INSTALL_TEST_ARCH=arch)
            self.archive(target)
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        profile = Path(self.env["HOME"]) / ".zshrc"
        self.assertEqual(profile.read_text().count("# whip-it installer"), 1)
        result = subprocess.run(
            ["sh", "-c", '. "$1"; command -v whip-it', "sh", str(profile)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), self.env["WHIP_IT_INSTALL_DIR"] + "/whip-it")

    def test_bad_checksum_or_download_preserves_existing_install(self):
        archive = self.archive("x86_64-unknown-linux-musl")
        archive.with_name(archive.name + ".sha256").write_text("0" * 64 + "  bad\n")
        destination = Path(self.env["WHIP_IT_INSTALL_DIR"]) / "whip-it"
        destination.parent.mkdir()
        destination.write_text("existing installation")
        for failure in ("", "download failed"):
            self.env["INSTALL_TEST_FAIL"] = failure
            result = self.install()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_text(), "existing installation")
            self.assertEqual(list(destination.parent.glob(".whip-it-install.*")), [])

    def test_unsupported_architecture_does_not_install(self):
        self.env["INSTALL_TEST_ARCH"] = "riscv64"
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(Path(self.env["WHIP_IT_INSTALL_DIR"]).exists())


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "windows installer")
class TestWindowsInstaller(unittest.TestCase):
    def test_verified_install_repeat_and_failure_preserve_executable(self):
        executable = Path(
            os.environ.get("WHIP_IT_TEST_EXECUTABLE", ROOT / "target/release/whip-it.exe")
        )
        if not executable.is_file():
            self.skipTest("build the native executable before testing the windows installer")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.write(executable, "whip-it.exe")
            (root / "checksum").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
            script = root / "test.ps1"
            script.write_text(
                "param($Installer, $Root)\n"
                "$ErrorActionPreference='Stop'\n"
                "function Invoke-WebRequest { param($Uri,$OutFile,$TimeoutSec)\n"
                "  $source=if ($Uri.EndsWith('.sha256')) {'checksum'} else {'fixture.zip'}\n"
                "  Copy-Item (Join-Path $Root $source) $OutFile\n}\n"
                "$dest=Join-Path $Root 'bin with spaces'\n"
                "& $Installer -InstallDir $dest -NoModifyPath\n"
                "& $Installer -InstallDir $dest -NoModifyPath\n"
                "$before=(Get-FileHash (Join-Path $dest 'whip-it.exe')).Hash\n"
                "Set-Content (Join-Path $Root 'checksum') ('0' * 64)\n"
                "$failed=$false\n"
                "try { & $Installer -InstallDir $dest -NoModifyPath } catch { $failed=$true }\n"
                "if (-not $failed) { throw 'invalid archive accepted' }\n"
                "if ((Get-FileHash (Join-Path $dest 'whip-it.exe')).Hash -ne $before) "
                "{ throw 'existing installation changed' }\n"
            )
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(script), str(ROOT / "install.ps1"), str(root)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
