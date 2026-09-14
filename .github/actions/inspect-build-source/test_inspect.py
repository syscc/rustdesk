import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


TEST_DIRECTORY = Path(__file__).resolve().parent
import unittest


MODULE_PATH = TEST_DIRECTORY / "inspect_source.py"
SPEC = importlib.util.spec_from_file_location("inspect_build_source", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
inspect_build_source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspect_build_source)

BASELINE_A = "1" * 40
BASELINE_B = "a" * 40


class InspectBuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def write(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def initialize_git(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Inspect Test",
                "-c",
                "user.email=inspect@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=self.root,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()

    def write_vcpkg_registry_baseline(self, baseline=BASELINE_A, builtin=None):
        data = {
            "vcpkg-configuration": {
                "default-registry": {"kind": "builtin", "baseline": baseline}
            }
        }
        if builtin is not None:
            data["builtin-baseline"] = builtin
        self.write("vcpkg.json", json.dumps(data))

    def write_aom_version(self, version):
        self.write("res/vcpkg/aom/vcpkg.json", json.dumps({"version-semver": version}))

    def write_master_style_source(self, side_effect=None):
        self.write(
            "Cargo.toml",
            """[package]
name = "rustdesk"
version = "1.5.0-beta.1+ci.2"

[features]
drm = []
drm-wake = ["drm"]

[dependencies]
serde = "1"
""",
        )
        effect = ""
        if side_effect is not None:
            effect = f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('executed')\n"
        self.write("build.py", effect + "def build_libdrmtap_so():\n    return None\n")
        self.write(
            "res/msi/preprocess.py",
            """import argparse

def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", action="store_true")
    return parser
""",
        )
        self.write_vcpkg_registry_baseline()
        self.write_aom_version("3.14.1")

    def test_master_style_source(self):
        self.write_master_style_source()
        sha = self.initialize_git()

        outputs = inspect_build_source.inspect_source(self.root)

        self.assertEqual(
            outputs,
            {
                "source-sha": sha,
                "version": "1.5.0-beta.1+ci.2",
                "vcpkg-commit": BASELINE_A,
                "supports-drm": "true",
                "supports-msi-template": "true",
                "needs-pam": "false",
                "needs-nasm2": "false",
            },
        )
        manifest = inspect_build_source.read_cargo_manifest(self.root)
        self.assertTrue(inspect_build_source.has_cargo_feature(manifest, "drm-wake"))

    def test_old_tag_style_source(self):
        self.write(
            "Cargo.toml",
            """[package]
name = "rustdesk"
version = "1.2.3"

[features]
flutter = []

[target.'cfg(target_os = "linux")'.dependencies]
login = { package = "pam-sys", version = "1" }
""",
        )
        self.write(
            "build.py",
            """if True:
    def build_libdrmtap_so():
        pass
""",
        )
        self.write(
            "res/msi/preprocess.py",
            """# parser.add_argument("--template")
TEMPLATE_OPTION = "--template"
parser.add_argument("--custom", help=TEMPLATE_OPTION)
""",
        )
        self.write("vcpkg.json", json.dumps({"builtin-baseline": BASELINE_B}))
        self.write_aom_version("3.12.1")
        sha = self.initialize_git()

        outputs = inspect_build_source.inspect_source(self.root)

        self.assertEqual(outputs["source-sha"], sha)
        self.assertEqual(outputs["vcpkg-commit"], BASELINE_B)
        self.assertEqual(outputs["supports-drm"], "false")
        self.assertEqual(outputs["supports-msi-template"], "false")
        self.assertEqual(outputs["needs-pam"], "true")
        self.assertEqual(outputs["needs-nasm2"], "true")

    def test_aom_version_controls_nasm2_requirement(self):
        self.write_aom_version("3.12.1")
        self.assertTrue(inspect_build_source.needs_nasm2(self.root))

        self.write_aom_version("3.14.1")
        self.assertFalse(inspect_build_source.needs_nasm2(self.root))

    def test_aom_manifest_missing_or_invalid_fails_inspection(self):
        with self.assertRaises(inspect_build_source.InspectionError):
            inspect_build_source.needs_nasm2(self.root)

        cases = (
            ("{", "invalid JSON"),
            (json.dumps({}), "missing version-semver"),
            (json.dumps({"version-semver": "3.14"}), "incomplete version"),
            (json.dumps({"version-semver": "3.14.1-rc.1"}), "pre-release version"),
        )
        for content, label in cases:
            with self.subTest(label=label):
                self.write("res/vcpkg/aom/vcpkg.json", content)
                with self.assertRaises(inspect_build_source.InspectionError):
                    inspect_build_source.needs_nasm2(self.root)

    def test_drm_requires_callable_sync_top_level_function_and_both_features(self):
        self.write_master_style_source()
        self.initialize_git()
        build_path = self.root / "build.py"
        cases = (
            ("# def build_libdrmtap_so(): pass\n", False, "comment"),
            ("async def build_libdrmtap_so(): pass\n", False, "async"),
            ("def build_libdrmtap_so(required): pass\n", False, "required positional"),
            ("def build_libdrmtap_so(*, required): pass\n", False, "required kw-only"),
            ("def build_libdrmtap_so(optional=None): pass\n", True, "optional positional"),
        )
        for source, expected, label in cases:
            with self.subTest(function=label):
                build_path.write_text(source, encoding="utf-8")
                outputs = inspect_build_source.inspect_source(self.root)
                self.assertEqual(outputs["supports-drm"], str(expected).lower())

        self.write(
            "Cargo.toml",
            """[package]
name = "rustdesk"
version = "1.5.0"

[features]
drm = []
""",
        )
        outputs = inspect_build_source.inspect_source(self.root)
        self.assertEqual(outputs["supports-drm"], "false")

    def test_missing_build_py_fails_inspection(self):
        self.write_master_style_source()
        self.initialize_git()
        (self.root / "build.py").unlink()

        with self.assertRaises(inspect_build_source.InspectionError):
            inspect_build_source.inspect_source(self.root)

    def test_vcpkg_baseline_changes_are_observed(self):
        self.write_master_style_source()
        self.initialize_git()
        self.assertEqual(inspect_build_source.get_vcpkg_commit(self.root), BASELINE_A)

        self.write_vcpkg_registry_baseline(BASELINE_B)

        self.assertEqual(inspect_build_source.get_vcpkg_commit(self.root), BASELINE_B)

    def test_vcpkg_baseline_missing_invalid_and_conflicting_fail(self):
        cases = (
            ({}, "missing"),
            ({"builtin-baseline": "$(inject)"}, "invalid"),
            (
                {
                    "builtin-baseline": BASELINE_A,
                    "vcpkg-configuration": {
                        "default-registry": {
                            "kind": "builtin",
                            "baseline": BASELINE_B,
                        }
                    },
                },
                "conflicting",
            ),
            (
                {
                    "builtin-baseline": BASELINE_A,
                    "vcpkg-configuration": {
                        "default-registry": {
                            "kind": "git",
                            "baseline": BASELINE_A,
                        }
                    },
                },
                "non-builtin default-registry",
            ),
            (
                {
                    "builtin-baseline": BASELINE_A,
                    "vcpkg-configuration": {
                        "default-registry": {"kind": "builtin"}
                    },
                },
                "missing default-registry baseline",
            ),
        )
        for data, label in cases:
            with self.subTest(label=label):
                self.write("vcpkg.json", json.dumps(data))
                with self.assertRaises(inspect_build_source.InspectionError):
                    inspect_build_source.get_vcpkg_commit(self.root)

    def test_matching_vcpkg_baseline_fields_are_allowed(self):
        self.write_vcpkg_registry_baseline(BASELINE_A, builtin=BASELINE_A)
        self.assertEqual(inspect_build_source.get_vcpkg_commit(self.root), BASELINE_A)

    def test_version_missing_or_filename_injection_fails(self):
        manifests = (
            {"package": {"name": "rustdesk"}},
            {"package": {"version": "1.2.3/../../artifact"}},
            {"package": {"version": "1.2.3\nsource-sha=malicious"}},
            {"package": {"version": "${{ secrets.TOKEN }}"}},
            {"package": {"version": "1" * 129}},
        )
        for manifest in manifests:
            with self.subTest(manifest=manifest):
                with self.assertRaises(inspect_build_source.InspectionError):
                    inspect_build_source.get_version(manifest)

    def test_sha_missing_or_injected_fails(self):
        results = (
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=BASELINE_A + "\nsource-sha=malicious\n", stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="G" * 40 + "\n", stderr=""),
        )
        original_run = inspect_build_source.subprocess.run
        try:
            for result in results:
                with self.subTest(stdout=result.stdout):
                    inspect_build_source.subprocess.run = lambda *args, result=result, **kwargs: result
                    with self.assertRaises(inspect_build_source.InspectionError):
                        inspect_build_source.read_source_sha(self.root)
        finally:
            inspect_build_source.subprocess.run = original_run

    def test_template_detection_requires_constant_add_argument(self):
        path = self.write(
            "preprocess.py",
            """# parser.add_argument("--template")
OPTION = "--template"
parser.add_argument(OPTION)
parser.add_argument("--custom", help="--template")
""",
        )
        self.assertFalse(
            inspect_build_source.has_constant_add_argument(path, "--template")
        )

        self.write(
            "preprocess.py",
            """parser.add_argument(
    "-t",
    "--template",
    action="store_true",
)
""",
        )
        self.assertTrue(
            inspect_build_source.has_constant_add_argument(path, "--template")
        )

    def test_upstream_python_is_never_executed(self):
        side_effect = self.root / "side-effect"
        self.write_master_style_source(side_effect)
        self.initialize_git()

        outputs = inspect_build_source.inspect_source(self.root)

        self.assertEqual(outputs["supports-drm"], "true")
        self.assertFalse(side_effect.exists())

    def test_cli_writes_seven_outputs_and_stdout_json(self):
        self.write_master_style_source()
        self.initialize_git()
        github_output = self.root / "github-output"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--source", str(self.root),
             "--github-output", str(github_output)],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        lines = github_output.read_text(encoding="utf-8").splitlines()
        written = dict(line.split("=", 1) for line in lines)
        self.assertEqual(
            inspect_build_source.OUTPUT_NAMES,
            (
                "source-sha",
                "version",
                "vcpkg-commit",
                "supports-drm",
                "supports-msi-template",
                "needs-pam",
                "needs-nasm2",
            ),
        )
        self.assertEqual(
            [line.split("=", 1)[0] for line in lines],
            list(inspect_build_source.OUTPUT_NAMES),
        )
        self.assertEqual(len(written), 7)
        self.assertEqual(written, printed)


if __name__ == "__main__":
    unittest.main()
