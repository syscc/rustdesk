#!/usr/bin/env python3
"""Read and validate build metadata from an upstream RustDesk checkout."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "inspect-build-source requires Python 3.11+ or the tomli package"
        ) from error
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


SHA40_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE | re.ASCII)
SAFE_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z._+-]*", re.ASCII)
AOM_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)", re.ASCII)
DEPENDENCY_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
OUTPUT_NAMES = (
    "source-sha",
    "version",
    "vcpkg-commit",
    "supports-drm",
    "supports-msi-template",
    "needs-pam",
    "needs-nasm2",
)


class InspectionError(RuntimeError):
    pass


def _is_sha40(value: object) -> bool:
    return isinstance(value, str) and SHA40_RE.fullmatch(value) is not None


def read_source_sha(source: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InspectionError("could not resolve the checked-out source commit") from error

    sha = result.stdout.strip()
    if not _is_sha40(sha):
        raise InspectionError("git rev-parse HEAD did not return a SHA-40")
    return sha.lower()


def load_toml(path: Path, description: str) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            parsed = tomllib.load(file)
    except FileNotFoundError as error:
        raise InspectionError(f"missing {description}") from error
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise InspectionError(f"could not parse {description}") from error
    if not isinstance(parsed, dict):
        raise InspectionError(f"invalid {description}")
    return parsed


def read_cargo_manifest(source: Path) -> dict[str, Any]:
    return load_toml(source / "Cargo.toml", "Cargo.toml")


def get_version(manifest: Mapping[str, Any]) -> str:
    package = manifest.get("package")
    version = package.get("version") if isinstance(package, Mapping) else None
    if not isinstance(version, str):
        raise InspectionError("Cargo.toml is missing [package].version")
    if len(version) > 128 or SAFE_VERSION_RE.fullmatch(version) is None:
        raise InspectionError("Cargo.toml [package].version is not filename-safe")
    return version


def get_vcpkg_commit(source: Path) -> str:
    try:
        data = json.loads((source / "vcpkg.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InspectionError("missing vcpkg.json") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InspectionError("could not parse vcpkg.json") from error
    if not isinstance(data, dict):
        raise InspectionError("invalid vcpkg.json")

    candidates: list[str] = []
    configuration = data.get("vcpkg-configuration")
    if configuration is not None:
        if not isinstance(configuration, dict):
            raise InspectionError("invalid vcpkg-configuration")
        if "default-registry" in configuration:
            registry = configuration["default-registry"]
            if not isinstance(registry, dict) or registry.get("kind") != "builtin":
                raise InspectionError("vcpkg default-registry must be builtin")
            baseline = registry.get("baseline")
            if not _is_sha40(baseline):
                raise InspectionError("vcpkg builtin default-registry baseline is missing or invalid")
            candidates.append(baseline.lower())

    builtin_baseline = data.get("builtin-baseline")
    if builtin_baseline is not None:
        if not _is_sha40(builtin_baseline):
            raise InspectionError("invalid builtin-baseline")
        candidates.append(builtin_baseline.lower())

    if not candidates:
        raise InspectionError("vcpkg.json is missing a builtin SHA-40 baseline")
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise InspectionError("vcpkg.json contains conflicting builtin baselines")
    return candidates[0].lower()


def needs_nasm2(source: Path) -> bool:
    path = source / "res/vcpkg/aom/vcpkg.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InspectionError("missing res/vcpkg/aom/vcpkg.json") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InspectionError("could not parse res/vcpkg/aom/vcpkg.json") from error
    if not isinstance(data, Mapping):
        raise InspectionError("invalid res/vcpkg/aom/vcpkg.json")

    version = data.get("version-semver")
    match = AOM_VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise InspectionError("res/vcpkg/aom/vcpkg.json is missing a valid version-semver")
    return tuple(map(int, match.groups())) < (3, 14, 0)


def _read_python_ast(
    path: Path, description: str, *, required: bool = False
) -> ast.Module | None:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if required:
            raise InspectionError(f"missing {description}") from error
        return None
    except (OSError, UnicodeError) as error:
        raise InspectionError(f"could not read {description}") from error
    try:
        return ast.parse(source, filename=path.name)
    except SyntaxError as error:
        raise InspectionError(f"could not parse {description}") from error


def has_callable_top_level_function(path: Path, function_name: str, *, required: bool = False) -> bool:
    tree = _read_python_ast(path, path.name, required=required)
    if tree is None:
        return False
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        required_positional = len(positional) - len(node.args.defaults)
        if required_positional > 0:
            continue
        if any(default is None for default in node.args.kw_defaults):
            continue
        return True
    return False


def has_constant_add_argument(path: Path, option: str) -> bool:
    tree = _read_python_ast(path, path.name)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        values: Iterable[ast.expr] = node.args
        if any(isinstance(value, ast.Constant) and value.value == option for value in values):
            return True
    return False


def has_cargo_feature(manifest: Mapping[str, Any], feature: str) -> bool:
    features = manifest.get("features")
    return isinstance(features, Mapping) and feature in features


def _dependency_tables(manifest: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for section in DEPENDENCY_SECTIONS:
        table = manifest.get(section)
        if table is not None:
            if not isinstance(table, Mapping):
                raise InspectionError(f"Cargo.toml [{section}] is invalid")
            yield table

    targets = manifest.get("target")
    if targets is None:
        return
    if not isinstance(targets, Mapping):
        raise InspectionError("Cargo.toml [target] is invalid")
    for target in targets.values():
        if not isinstance(target, Mapping):
            raise InspectionError("Cargo.toml target table is invalid")
        for section in DEPENDENCY_SECTIONS:
            table = target.get(section)
            if table is not None:
                if not isinstance(table, Mapping):
                    raise InspectionError(f"Cargo.toml target {section} is invalid")
                yield table


def needs_pam(manifest: Mapping[str, Any]) -> bool:
    pam_packages = {"pam", "pam-sys"}
    for table in _dependency_tables(manifest):
        for dependency_name, specification in table.items():
            if dependency_name in pam_packages:
                return True
            if isinstance(specification, Mapping) and specification.get("package") in pam_packages:
                return True
    return False


def inspect_source(source: Path) -> dict[str, str]:
    source = source.resolve()
    if not source.is_dir():
        raise InspectionError("source is not a directory")

    manifest = read_cargo_manifest(source)
    supports_drm = has_callable_top_level_function(
        source / "build.py", "build_libdrmtap_so", required=True
    ) and has_cargo_feature(manifest, "drm") and has_cargo_feature(manifest, "drm-wake")

    return {
        "source-sha": read_source_sha(source),
        "version": get_version(manifest),
        "vcpkg-commit": get_vcpkg_commit(source),
        "supports-drm": str(supports_drm).lower(),
        "supports-msi-template": str(
            has_constant_add_argument(source / "res/msi/preprocess.py", "--template")
        ).lower(),
        "needs-pam": str(needs_pam(manifest)).lower(),
        "needs-nasm2": str(needs_nasm2(source)).lower(),
    }


def write_github_outputs(outputs: Mapping[str, str], path: Path) -> None:
    with path.open("a", encoding="utf-8") as file:
        for name in OUTPUT_NAMES:
            file.write(f"{name}={outputs[name]}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outputs = inspect_source(args.source)
        if args.github_output:
            write_github_outputs(outputs, Path(args.github_output))
    except (InspectionError, OSError, KeyError) as error:
        print(f"inspect-build-source: error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
