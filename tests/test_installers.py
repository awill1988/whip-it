"""Exercise setup without fetching releases or changing the user's installation."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_plugin import package
from scripts.prepare_release import current_version


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
            "INSTALL_FIXTURES": str(self.root),
            "INSTALL_TEST_OS": "Linux",
            "INSTALL_TEST_ARCH": "x86_64",
            "INSTALL_CLIENT_LOG": str(self.root / "clients.log"),
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
        for client in ("claude", "codex", "agy"):
            scripts[client] = (
                "import os,pathlib,sys\n"
                "with open(os.environ['INSTALL_CLIENT_LOG'], 'a') as f: f.write(repr(sys.argv[1:])+'\\n')\n"
                "sys.exit(1 if os.environ.get('INSTALL_CLIENT_FAIL') else 0)\n"
            )
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o755)

    def archive(self, target):
        binary = self.root / "whip-it"
        binary.write_text(f"#!/bin/sh\nprintf 'whip-it {current_version()}\\n'\n")
        binary.chmod(0o755)
        return package(binary, target, self.root)

    def install(self, client="agy", *options):
        return subprocess.run(
            ["sh", str(ROOT / "install.sh"), "--client", client, *options],
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
        for client in ("claude", "codex", "agy"):
            for _ in range(2):
                result = self.install(client)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.env["HOME"]) / ".zshrc").exists())
        log = (self.root / "clients.log").read_text()
        self.assertIn("marketplace", log)
        self.assertIn("whip-it@awill1988", log)

    def test_registration_failure_can_retry(self):
        self.archive("x86_64-unknown-linux-musl")
        self.env["INSTALL_CLIENT_FAIL"] = "1"
        self.assertNotEqual(self.install().returncode, 0)
        del self.env["INSTALL_CLIENT_FAIL"]
        self.assertEqual(self.install().returncode, 0)

    def test_codex_upgrade_replaces_only_conflicting_marketplace(self):
        self.archive("x86_64-unknown-linux-musl")
        client = self.bin / "codex"
        client.write_text(
            f"#!{sys.executable}\n"
            "import os,pathlib,sys\n"
            "marker=pathlib.Path(os.environ['INSTALL_FIXTURES'])/'removed'\n"
            "if sys.argv[1:4]==['plugin','marketplace','add'] and not marker.exists():\n"
            " print(\"Error: marketplace 'awill1988' is already added from a different source\", file=sys.stderr)\n"
            " sys.exit(1)\n"
            "if sys.argv[1:]==['plugin','marketplace','remove','awill1988']: marker.touch()\n"
        )
        result = self.install("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "removed").exists())

    def test_invalid_version_never_downloads(self):
        self.assertNotEqual(self.install("agy", "--version", "../bad").returncode, 0)
        self.assertFalse(Path(self.env["WHIP_IT_INSTALL_DIR"]).exists())

    def test_bad_checksum_or_download_preserves_existing_install(self):
        archive = self.archive("x86_64-unknown-linux-musl")
        archive.with_name(archive.name + ".sha256").write_text("0" * 64 + "  bad\n")
        destination = Path(self.env["WHIP_IT_INSTALL_DIR"]) / "whip-it"
        destination.parent.mkdir(parents=True)
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
            if "WHIP_IT_TEST_EXECUTABLE" in os.environ:
                self.fail(f"configured native executable does not exist: {executable}")
            self.skipTest("build the native executable before testing the windows installer")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = package(executable, "x86_64-pc-windows-msvc", root)
            (root / "checksum").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
            script = root / "test.ps1"
            script.write_text(
                "param($Installer, $Root)\n"
                "$ErrorActionPreference='Stop'\n"
                "function Invoke-WebRequest { param($Uri,$OutFile,$TimeoutSec)\n"
                "  $source=if ($Uri.EndsWith('.sha256')) {'checksum'} else {'whip-it-plugin-x86_64-pc-windows-msvc.zip'}\n"
                "  Copy-Item (Join-Path $Root $source) $OutFile\n}\n"
                "function agy { $global:LASTEXITCODE=0 }\n"
                "function claude { $global:LASTEXITCODE=0 }\n"
                "function codex {\n"
                " if ($args[2] -eq 'add' -and -not $global:removed) {\n"
                "  $global:LASTEXITCODE=1\n"
                "  Write-Output \"Error: marketplace 'awill1988' is already added from a different source\"\n"
                " } else { $global:LASTEXITCODE=0 }\n"
                " if ($args[2] -eq 'remove') { $global:removed=$true }\n"
                "}\n"
                "$dest=Join-Path $Root 'bin with spaces'\n"
                "& $Installer -InstallDir $dest -Client agy\n"
                "& $Installer -InstallDir $dest -Client agy\n"
                "& $Installer -InstallDir $dest -Client claude\n"
                "& $Installer -InstallDir $dest -Client codex\n"
                "if (-not $global:removed) { throw 'codex upgrade not registered' }\n"
                "$binary=(Get-ChildItem $dest -Recurse -Filter whip-it.exe)[0].FullName\n"
                "$before=(Get-FileHash $binary).Hash\n"
                "Set-Content (Join-Path $Root 'checksum') ('0' * 64)\n"
                "$failed=$false\n"
                "try { & $Installer -InstallDir $dest -Client agy } catch { $failed=$true }\n"
                "if (-not $failed) { throw 'invalid archive accepted' }\n"
                "if ((Get-FileHash $binary).Hash -ne $before) "
                "{ throw 'existing installation changed' }\n"
            )
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(script), str(ROOT / "install.ps1"), str(root)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list((root / "bin with spaces").glob(".whip-it-install-*")), [])
