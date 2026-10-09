"""Protect release identity, complete assets, and synchronized versions."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_release
import publish_release


class TestRelease(unittest.TestCase):
    def test_semver_order_and_invalid_versions(self):
        versions = ["0.1.0-alpha.2", "0.1.0-alpha.10", "0.1.0-beta", "0.1.0", "0.2.0"]
        self.assertEqual(versions, sorted(versions, key=prepare_release.version_key))
        for value in ("v1.0.0", "1.0", "1.0.0-01", "1.0.0/../../x", "01.0.0"):
            with self.assertRaises(ValueError):
                prepare_release.version_key(value)

    def test_metadata_updates_and_validation_before_writes(self):
        names = [
            "Cargo.toml",
            "Cargo.lock",
            "pyproject.toml",
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
            ".codex-plugin/plugin.json",
            "src/whipit/__init__.py",
            "install.sh",
            "install.ps1",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, root / name)
            with patch.object(prepare_release, "ROOT", root):
                before = {name: (root / name).read_bytes() for name in names}
                with self.assertRaises(ValueError):
                    prepare_release.prepare(prepare_release.current_version())
                self.assertEqual(before, {name: (root / name).read_bytes() for name in names})
                (root / "install.ps1").write_text("invalid fixture")
                with self.assertRaises(ValueError):
                    prepare_release.prepare("99.0.0")
                self.assertEqual(before["Cargo.toml"], (root / "Cargo.toml").read_bytes())
                (root / "install.ps1").write_bytes(before["install.ps1"])
                prepare_release.prepare("99.0.0")
                for name in names:
                    self.assertIn("99.0.0", (root / name).read_text(), name)
                marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
                self.assertEqual(marketplace["plugins"][0]["version"], "99.0.0")

    def test_asset_completeness_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for target in publish_release.TARGETS:
                suffix = ".zip" if "windows" in target else ".tar.gz"
                for prefix in ("whip-it-", "whip-it-plugin-"):
                    asset = root / (prefix + target + suffix)
                    asset.write_bytes(b"fixture")
                    asset.with_name(asset.name + ".sha256").write_text(
                        hashlib.sha256(b"fixture").hexdigest()
                    )
            publish_release.verify_assets(root)
            asset.write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                publish_release.verify_assets(root)
            asset.write_bytes(b"fixture")
            (root / "unexpected").touch()
            with self.assertRaises(ValueError):
                publish_release.verify_assets(root)

    def test_unchanged_version_does_not_publish(self):
        version = prepare_release.current_version()
        with (
            patch.object(publish_release, "run", side_effect=["sha", f'version = "{version}"']),
            patch.object(publish_release.subprocess, "run"),
            patch.object(publish_release, "api") as api,
        ):
            publish_release.publish(Path("unused"))
            api.assert_not_called()

    def test_conflicting_tag_is_never_replaced(self):
        replies = [
            {"workflow_runs": [{"status": "completed", "conclusion": "success"}]},
            [{"ref": f"refs/tags/v{prepare_release.current_version()}"}],
        ]
        with (
            patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}),
            patch.object(publish_release, "run", side_effect=["sha", "different"]),
            patch.object(publish_release.subprocess, "run"),
            patch.object(publish_release, "verify_assets"),
            patch.object(publish_release, "api", side_effect=replies),
        ):
            with self.assertRaisesRegex(ValueError, "another commit"):
                publish_release.publish(Path("unused"), bootstrap=True)
