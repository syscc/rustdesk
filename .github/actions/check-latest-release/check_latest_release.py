#!/usr/bin/env python3
"""Compare the newest upstream stable release with the newest published local release."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request


API_ROOT = "https://api.github.com"
VERSION_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# Upstream tags must be X.Y.Z release versions; the loose pattern above would
# read `20240101-nightly` as (20240101, 0, 0) and build it.
STRICT_VERSION_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    """A release lookup that cannot produce a usable comparison."""


def fetch_json(url, token):
    """Return the decoded JSON body of a GitHub API URL.

    This is the only HTTP call in the script; tests replace it as a seam.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ReleaseError("GET {} returned HTTP {}".format(url, error.code)) from None
    except urllib.error.URLError as error:
        raise ReleaseError("GET {} failed: {}".format(url, error.reason)) from None
    except ValueError as error:
        raise ReleaseError("GET {} returned invalid JSON: {}".format(url, error)) from None


def parse_version(tag):
    """Parse a release tag into a comparable three-part version tuple."""
    if not isinstance(tag, str):
        return None
    match = VERSION_PATTERN.match(tag)
    if match is None:
        return None
    parts = [int(part) for part in match.groups() if part is not None]
    return tuple(parts + [0] * (3 - len(parts)))


def format_version(version):
    return ".".join(str(part) for part in version)


def normalized_tag(tag):
    return tag[1:] if tag.startswith("v") else tag


def latest_upstream_release(repository, token):
    """Return the raw tag of the newest upstream stable release."""
    url = "{}/repos/{}/releases/latest".format(API_ROOT, repository)
    release = fetch_json(url, token)
    if not isinstance(release, dict):
        raise ReleaseError("GET {} returned an unexpected payload".format(url))
    tag = release.get("tag_name")
    if release.get("draft") or release.get("prerelease"):
        raise ReleaseError(
            "{} has no stable release: latest release {} is a draft or prerelease".format(
                repository, tag
            )
        )
    if parse_version(tag) is None or STRICT_VERSION_PATTERN.match(tag) is None:
        raise ReleaseError(
            "{} latest stable release tag {!r} is not a version".format(repository, tag)
        )
    return tag


def local_stable_release(repository, token):
    """Return (version, tag) of the newest local stable release with assets."""
    url = "{}/repos/{}/releases?per_page=100".format(API_ROOT, repository)
    releases = fetch_json(url, token)
    if not isinstance(releases, list):
        raise ReleaseError("GET {} returned an unexpected payload".format(url))
    newest = None
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        # flutter-build.yml creates an empty release before the assets exist.
        # An assetless release is an interrupted publish, not a finished
        # version; skipping it lets the next run retry that build.
        if not release.get("assets"):
            continue
        version = parse_version(release.get("tag_name"))
        if version is None:
            continue
        if newest is None or version > newest[0]:
            newest = (version, release.get("tag_name"))
    return newest


def compare(upstream_tag, upstream_version, local):
    if local is None:
        return True, "No published stable release with assets in the local repository."
    local_version, local_tag = local
    if upstream_version > local_version:
        return True, "Upstream {} is newer than local {}.".format(
            upstream_tag, local_tag
        )
    if upstream_version < local_version:
        return False, "Local {} is newer than upstream {}.".format(
            local_tag, upstream_tag
        )
    return False, "Upstream {} matches the published local version.".format(upstream_tag)


def build_summary(
    upstream_repository,
    upstream_tag,
    upstream_version,
    local_repository,
    local_version,
    sync,
    reason,
):
    local_display = "`{}`".format(local_version) if local_version else "(none)"
    return "\n".join(
        [
            "| Sync check | Value |",
            "| --- | --- |",
            "| Upstream repository | `{}` |".format(upstream_repository),
            "| Upstream tag | `{}` |".format(upstream_tag),
            "| Upstream version | `{}` |".format(upstream_version),
            "| Local repository | `{}` |".format(local_repository),
            "| Local version | {} |".format(local_display),
            "| Sync required | `{}` |".format("true" if sync else "false"),
            "| Reason | {} |".format(reason.replace("|", "\\|")),
        ]
    )


def write_github_output(outputs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            handle.write("{}={}\n".format(name, value))


def append_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-repository", default="rustdesk/rustdesk")
    parser.add_argument("--local-repository", default="syscc/rustdesk")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        upstream_tag = latest_upstream_release(args.upstream_repository, token)
        upstream_version = parse_version(upstream_tag)
        local = local_stable_release(args.local_repository, token)
    except Exception as error:  # any lookup failure must fail loudly, never skip
        print("::error::{}".format(error), file=sys.stderr)
        return 1

    local_version = format_version(local[0]) if local else ""
    sync, reason = compare(upstream_tag, upstream_version, local)
    upstream_version_text = normalized_tag(upstream_tag)
    outputs = {
        "upstream-tag": upstream_tag,
        "upstream-version": upstream_version_text,
        "local-version": local_version,
        "sync": "true" if sync else "false",
    }
    write_github_output(outputs)
    summary = build_summary(
        args.upstream_repository,
        upstream_tag,
        upstream_version_text,
        args.local_repository,
        local_version,
        sync,
        reason,
    )
    print(summary)
    append_step_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())