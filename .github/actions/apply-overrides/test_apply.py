import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("apply.py")
COMMON_SOURCE = '''pub fn load_custom_client() {
    #[cfg(debug_assertions)]
    if let Ok(data) = std::fs::read_to_string("./custom.txt") {
        read_custom_client(data.trim());
        return;
    }
    let Some(path) = std::env::current_exe().map_or(None, |x| x.parent().map(|x| x.to_path_buf()))
    else {
        return;
    };
    let path = path.join("custom.txt");
    if path.is_file() {
        let Ok(data) = std::fs::read_to_string(&path) else {
            return;
        };
        read_custom_client(&data.trim());
    }
}

fn get_api_server_() -> String {
    "https://api.example.invalid".to_owned()
}
'''
CORE_SOURCE = '''fn core_main() {
    let _is_quick_support = !crate::platform::is_installed()
        && args.is_empty()
        && (config::LocalConfig::get_option("pre-elevate-service") == "Y");
}
'''
HBB_SOURCE = '''pub const RENDEZVOUS_SERVERS: &[&str] = &["upstream.example.invalid"];
pub const RS_PUB_KEY: &str = "upstream-public-key";
'''
MARKER = "// syscc pre-elevate-service default"


class ApplyOverridesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.common = self.write("src/common.rs", COMMON_SOURCE)
        self.core = self.write("src/core_main.rs", CORE_SOURCE)
        self.hbb = self.write("libs/hbb_common/src/config.rs", HBB_SOURCE)

    def write(self, relative_path, text):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_apply(self, overrides=False, elevate=False):
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
        env.update(
            GITHUB_WORKSPACE=str(self.root),
            APPLY_SYSCC_OVERRIDES=str(overrides).lower(),
            PRE_ELEVATE_SERVICE=str(elevate).lower(),
        )
        if overrides:
            env.update(SERVER_DOMAIN="custom.example.invalid", SERVER_PUBLIC_KEY="test-public-key")
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_disabled_does_not_touch_source_or_require_supported_source(self):
        self.core.unlink()
        result = self.run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.common.read_text(encoding="utf-8"), COMMON_SOURCE)
        self.assertEqual(self.hbb.read_text(encoding="utf-8"), HBB_SOURCE)

    def test_enabled_without_server_secrets_only_sets_windows_local_default(self):
        result = self.run_apply(elevate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.common.read_text(encoding="utf-8")
        self.assertIn('#[cfg(windows)]\n    {\n        config::DEFAULT_LOCAL_SETTINGS', source)
        self.assertIn('.entry("pre-elevate-service".to_string())\n            .or_insert("Y".to_string());', source)
        self.assertEqual(source.count(MARKER), 1)
        self.assertLess(source.index(MARKER), source.index("#[cfg(debug_assertions)]"))
        self.assertNotIn("DEFAULT_SETTINGS", source)
        self.assertNotIn("OVERWRITE_LOCAL_SETTINGS", source)
        self.assertEqual(self.hbb.read_text(encoding="utf-8"), HBB_SOURCE)
        self.assertEqual(self.core.read_text(encoding="utf-8"), CORE_SOURCE)

    def test_enabled_is_idempotent(self):
        first = self.run_apply(elevate=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        source = self.common.read_bytes()
        second = self.run_apply(elevate=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.common.read_bytes(), source)

    def test_rejects_unsupported_source_before_writing(self):
        self.core.write_text('fn core_main() {}\n', encoding="utf-8")
        result = self.run_apply(elevate=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Selected source does not support pre-elevate-service", result.stderr)
        self.assertEqual(self.common.read_text(encoding="utf-8"), COMMON_SOURCE)

    def test_rejects_ambiguous_loader_before_writing(self):
        source = COMMON_SOURCE + "\npub fn load_custom_client() {\n}\n"
        self.common.write_text(source, encoding="utf-8")
        result = self.run_apply(elevate=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected exactly one upstream custom-client loader", result.stderr)
        self.assertEqual(self.common.read_text(encoding="utf-8"), source)

    def test_rejects_incomplete_existing_patch(self):
        source = COMMON_SOURCE.replace("pub fn load_custom_client() {", "pub fn load_custom_client() {\n    " + MARKER)
        self.common.write_text(source, encoding="utf-8")
        result = self.run_apply(elevate=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pre-elevate-service override validation failed", result.stderr)
        self.assertEqual(self.common.read_text(encoding="utf-8"), source)

    def test_existing_overrides_and_elevation_can_be_combined_or_left_disabled(self):
        result = self.run_apply(overrides=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.common.read_text(encoding="utf-8")
        self.assertNotIn(MARKER, source)
        self.assertEqual(source.count("// syscc custom settings from repository variables"), 2)
        self.assertIn('pub const RENDEZVOUS_SERVERS: &[&str] = &["custom.example.invalid"];', self.hbb.read_text(encoding="utf-8"))
        result = self.run_apply(overrides=True, elevate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.common.read_text(encoding="utf-8")
        self.assertEqual(source.count(MARKER), 1)
        self.assertEqual(source.count("// syscc custom settings from repository variables"), 2)


if __name__ == "__main__":
    unittest.main()
