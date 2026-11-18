import io
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import check_latest_release


def release(tag, draft=False, prerelease=False, assets=1):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": "asset"} for _ in range(assets)],
    }


class CheckLatestReleaseTests(unittest.TestCase):
    def setUp(self):
        self.latest = None
        self.releases = []
        self.original_fetch_json = check_latest_release.fetch_json
        check_latest_release.fetch_json = self.fake_fetch_json
        self.addCleanup(
            setattr, check_latest_release, "fetch_json", self.original_fetch_json
        )

    def fake_fetch_json(self, url, token):
        if url.endswith("/releases/latest"):
            return self.latest
        if "/releases?per_page=100" in url:
            return self.releases
        raise AssertionError("unexpected GitHub API URL: {}".format(url))

    def run_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            summary = Path(temporary) / "step-summary.md"
            environment = {
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False), redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                status = check_latest_release.main([])
            outputs = {}
            if output.exists():
                for line in output.read_text(encoding="utf-8").splitlines():
                    name, _, value = line.partition("=")
                    outputs[name] = value
            summary_text = (
                summary.read_text(encoding="utf-8") if summary.exists() else ""
            )
            return status, outputs, stdout.getvalue(), stderr.getvalue(), summary_text

    def test_matching_versions_do_not_sync(self):
        self.latest = release("1.5.3")
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(
            outputs,
            {
                "upstream-tag": "1.5.3",
                "upstream-version": "1.5.3",
                "local-version": "1.5.3",
                "sync": "false",
            },
        )
        self.assertIn("matches", stdout)
        self.assertIn("`false`", summary)

    def test_newer_upstream_triggers_sync(self):
        self.latest = release("1.5.4")
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["upstream-tag"], "1.5.4")
        self.assertEqual(outputs["upstream-version"], "1.5.4")
        self.assertEqual(outputs["local-version"], "1.5.3")
        self.assertEqual(outputs["sync"], "true")
        self.assertIn("Upstream 1.5.4 is newer than local 1.5.3", stdout)
        self.assertIn("| Sync required | `true` |", summary)

    def test_three_part_versions_compare_numerically(self):
        self.latest = release("1.10.0")
        self.releases = [release("1.9.0")]
        status, outputs, stdout, stderr, _ = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["upstream-version"], "1.10.0")
        self.assertEqual(outputs["local-version"], "1.9.0")
        self.assertEqual(outputs["sync"], "true")
        self.assertIn("Upstream 1.10.0 is newer than local 1.9.0", stdout)

    def test_upstream_tag_that_is_not_a_version_fails_loud(self):
        # A prefix match would read this as (20240101, 0, 0) and build it.
        self.latest = release("20240101-nightly")
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("not a version", stderr)
        self.assertEqual(outputs, {})
        self.assertEqual(stdout, "")
        self.assertEqual(summary, "")

    def test_upstream_tag_with_a_date_prefix_is_rejected(self):
        # Only X.Y.Z is a release version, so a date-shaped tag never builds.
        self.latest = release("20240101")
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("not a version", stderr)
        self.assertEqual(outputs, {})
        self.assertEqual(stdout, "")
        self.assertEqual(summary, "")

    def test_upstream_release_without_a_tag_fails_loud(self):
        self.latest = {"draft": False, "prerelease": False}
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("not a version", stderr)
        self.assertEqual(outputs, {})

    def test_newer_local_does_not_sync(self):
        self.latest = release("1.5.2")
        self.releases = [release("1.5.3")]
        status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["sync"], "false")
        self.assertEqual(outputs["local-version"], "1.5.3")
        self.assertIn("Local 1.5.3 is newer than upstream 1.5.2", summary)

    def test_no_local_stable_release_triggers_sync(self):
        self.latest = release("1.5.4")
        self.releases = []
        status, outputs, _, stderr, summary = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["local-version"], "")
        self.assertEqual(outputs["sync"], "true")
        self.assertIn("No published stable release", summary)

    def test_draft_prerelease_and_assetless_releases_are_ignored(self):
        self.latest = release("1.6.0")
        self.releases = [
            release("1.5.3"),
            release("1.5.9", draft=True),
            release("1.5.8", prerelease=True),
            release("1.5.7", assets=0),
        ]
        status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["local-version"], "1.5.3")
        self.assertEqual(outputs["sync"], "true")

    def test_assetless_newer_release_leaves_the_version_to_retry(self):
        # flutter-build.yml pre-creates an empty release. If the 1.5.4 build
        # failed after that, only the empty 1.5.4 tag exists and the next run
        # must still build 1.5.4 instead of treating it as published.
        self.latest = release("1.5.4")
        self.releases = [release("1.5.3"), release("1.5.4", assets=0)]
        status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["local-version"], "1.5.3")
        self.assertEqual(outputs["sync"], "true")

    def test_leading_v_is_normalized(self):
        self.latest = release("v1.5.4")
        self.releases = [release("v1.5.3")]
        status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(outputs["upstream-tag"], "v1.5.4")
        self.assertEqual(outputs["upstream-version"], "1.5.4")
        self.assertEqual(outputs["local-version"], "1.5.3")
        self.assertEqual(outputs["sync"], "true")

    def test_upstream_api_404_fails_loud(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/rustdesk/rustdesk/releases/latest",
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(check_latest_release, "fetch_json", side_effect=error):
            status, outputs, stdout, stderr, summary = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("404", stderr)
        self.assertEqual(outputs, {})
        self.assertEqual(stdout, "")
        self.assertEqual(summary, "")

    def test_upstream_api_non_200_fails_loud(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/rustdesk/rustdesk/releases/latest",
            502,
            "Bad Gateway",
            {},
            None,
        )
        with mock.patch.object(check_latest_release, "fetch_json", side_effect=error):
            status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("502", stderr)
        self.assertEqual(outputs, {})

    def test_fetch_json_wraps_http_errors_without_leaking_the_token(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/rustdesk/rustdesk/releases/latest",
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(
            check_latest_release.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaises(check_latest_release.ReleaseError) as raised:
                self.original_fetch_json(
                    "https://api.github.com/repos/rustdesk/rustdesk/releases/latest",
                    "test-token",
                )
        self.assertIn("returned HTTP 404", str(raised.exception))
        self.assertNotIn("test-token", str(raised.exception))

    def test_latest_release_that_is_not_stable_fails_loud(self):
        self.latest = release("1.5.4", prerelease=True)
        status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertIn("draft or prerelease", stderr)
        self.assertEqual(outputs, {})

    def test_local_api_failure_also_fails_loud(self):
        self.latest = release("1.5.4")

        def failing_fetch_json(url, token):
            if url.endswith("/releases/latest"):
                return self.latest
            raise check_latest_release.ReleaseError("GET {} returned HTTP 500".format(url))

        with mock.patch.object(check_latest_release, "fetch_json", failing_fetch_json):
            status, outputs, _, stderr, _ = self.run_script()
        self.assertEqual(status, 1)
        self.assertIn("::error::", stderr)
        self.assertEqual(outputs, {})

    def test_offline_run_without_github_files_still_reports_on_stdout(self):
        self.latest = release("1.5.4")
        self.releases = [release("1.5.3")]
        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ, {"GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}, clear=False
        ), redirect_stdout(stdout):
            status = check_latest_release.main([])
        self.assertEqual(status, 0)
        self.assertIn("1.5.4", stdout.getvalue())
        self.assertIn("1.5.3", stdout.getvalue())
        self.assertIn("true", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()