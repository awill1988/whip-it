"""Native checks must cover runtime and distribution changes without rebuilding for reviews."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.native_changes import requires_native


class TestNativeChanges(unittest.TestCase):
    def test_runtime_and_distribution_inputs_require_verification(self):
        for path in (
            "native/state.rs",
            "Cargo.lock",
            ".cargo/config.toml",
            "build.rs",
            "src/whipit/engine.py",
            "hooks.json",
            "hooks/hooks.json",
            "install.ps1",
            "scripts/package_plugin.py",
            "scripts/verify_native.py",
            "tests/test_installers.py",
            "benchmarks/run.py",
            "examples/run.py",
            ".github/workflows/native.yml",
            ".claude-plugin/plugin.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(requires_native([path]))

    def test_review_docs_and_release_control_do_not_rebuild(self):
        self.assertFalse(
            requires_native(
                [
                    "README.md",
                    "docs/usage.md",
                    "tools/adversarial_reviewer/kimi_client.py",
                    ".github/workflows/adversarial-review.yml",
                    "scripts/publish_release.py",
                    "tests/test_releases.py",
                ]
            )
        )

    def test_mixed_change_still_runs_native_checks(self):
        self.assertTrue(requires_native(["README.md", "native/policy.rs"]))
