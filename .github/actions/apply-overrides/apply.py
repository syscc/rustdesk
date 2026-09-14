#!/usr/bin/env python3
"""Apply syscc's repository-variable-driven changes to an upstream RustDesk checkout."""

import os
import re
from pathlib import Path


ROOT = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
SRC_PATH = ROOT / "src/common.rs"
HBB_CONFIG_PATH = ROOT / "libs/hbb_common/src/config.rs"
CUSTOM_SETTINGS_MARKER = "// syscc custom settings from repository variables"


def get_required_variable(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required repository variable {name} is empty or not configured"
        )
    return value


def get_optional_variable(name: str) -> str:
    return os.environ.get(name, "").strip()


def rust_string(value: str, name: str) -> str:
    if "\r" in value or "\n" in value:
        raise RuntimeError(f"Repository variable {name} must not contain a newline")
    escaped = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint < 0x20:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def replace_regex_once(
    text: str, pattern: str, replacement: str, description: str
) -> str:
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one upstream {description} declaration, found {len(matches)}"
        )
    match = matches[0]
    return text[: match.start()] + replacement + text[match.end() :]


def custom_settings_block(indent: str, fixed_password: str) -> str:
    lines = [f"{indent}{CUSTOM_SETTINGS_MARKER}"]
    if fixed_password:
        lines.extend(
            [
                f"{indent}// Set fixed password",
                f"{indent}{{",
                f"{indent}    let mut hard_settings = config::HARD_SETTINGS.write().unwrap();",
                f'{indent}    hard_settings.insert("password".to_string(), {fixed_password}.to_string());',
                f"{indent}    // 同时设置验证方法为只使用固定密码",
                f'{indent}    hard_settings.insert("verification-method".to_string(), "use-permanent-password".to_string());',
                f"{indent}}}",
            ]
        )
    lines.extend(
        [
            f"{indent}// Ensure remote configuration modification is enabled by default",
            f"{indent}{{",
            f"{indent}    let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();",
            f"{indent}    defaults",
            f"{indent}        .entry(keys::OPTION_ALLOW_REMOTE_CONFIG_MODIFICATION.to_string())",
            f'{indent}        .or_insert("Y".to_string());',
            f"{indent}}}",
            f"{indent}// Enable hiding connection management window by default",
            f"{indent}{{",
            f"{indent}    let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();",
            f"{indent}    defaults",
            f'{indent}        .entry("allow-hide-cm".to_string())',
            f'{indent}        .or_insert("Y".to_string());',
            f"{indent}}}",
        ]
    )
    return "\n".join(lines) + "\n"


def replace_api_server(source: str, api_server: str) -> str:
    if not api_server:
        return source

    function_start = source.find("fn get_api_server_(")
    if function_start < 0:
        raise RuntimeError("Could not find the upstream API server function")
    function_end = source.find("\n}\n", function_start)
    if function_end < 0:
        raise RuntimeError("Could not find the end of the upstream API server function")

    function = source[function_start:function_end]
    matches = list(
        re.finditer(r'(?m)^(\s*)"https?://[^"\n]+"\.to_owned\(\)$', function)
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one API server fallback in upstream function, found {len(matches)}"
        )
    match = matches[0]
    replacement = f'{match.group(1)}{rust_string(api_server, "API_SERVER")}.to_owned()'
    function = function[: match.start()] + replacement + function[match.end() :]
    return source[:function_start] + function + source[function_end:]


def apply_source_overrides() -> None:
    if not SRC_PATH.is_file():
        raise RuntimeError(f"Missing source file: {SRC_PATH}")

    server_domain = rust_string(
        get_required_variable("SERVER_DOMAIN"), "SERVER_DOMAIN"
    )
    fixed_password_value = get_optional_variable("FIXED_PASSWORD")
    fixed_password = (
        rust_string(fixed_password_value, "FIXED_PASSWORD")
        if fixed_password_value
        else ""
    )
    api_server_value = get_optional_variable("API_SERVER")

    source = SRC_PATH.read_text(encoding="utf-8")
    source = replace_api_server(source, api_server_value)

    debug_block = custom_settings_block("        ", fixed_password)
    runtime_block = custom_settings_block("    ", fixed_password)
    marker_count = source.count(CUSTOM_SETTINGS_MARKER)
    if marker_count == 0:
        debug_anchor = "        read_custom_client(data.trim());\n"
        if source.count(debug_anchor) != 1:
            raise RuntimeError("Could not find the debug custom-client anchor")
        source = source.replace(debug_anchor, debug_anchor + debug_block, 1)

        runtime_anchor = "        read_custom_client(&data.trim());\n    }\n}"
        if source.count(runtime_anchor) != 1:
            raise RuntimeError("Could not find the runtime custom-client anchor")
        source = source.replace(
            runtime_anchor,
            "        read_custom_client(&data.trim());\n    }\n\n" + runtime_block + "}",
            1,
        )
    elif marker_count != 2:
        raise RuntimeError(
            f"Expected both syscc custom settings blocks or none, found {marker_count}"
        )

    required = (
        f'{rust_string(api_server_value, "API_SERVER")}.to_owned()'
        if api_server_value
        else None,
        'keys::OPTION_ALLOW_REMOTE_CONFIG_MODIFICATION',
        CUSTOM_SETTINGS_MARKER,
    )
    if any(value is not None and value not in source for value in required):
        raise RuntimeError("Source override validation failed")
    SRC_PATH.write_text(source, encoding="utf-8")


def apply_hbb_config_override() -> None:
    if not HBB_CONFIG_PATH.is_file():
        raise RuntimeError(f"Missing hbb_common config: {HBB_CONFIG_PATH}")

    server_domain = rust_string(
        get_required_variable("SERVER_DOMAIN"), "SERVER_DOMAIN"
    )
    public_key = rust_string(
        get_required_variable("SERVER_PUBLIC_KEY"), "SERVER_PUBLIC_KEY"
    )
    config = HBB_CONFIG_PATH.read_text(encoding="utf-8")
    config = replace_regex_once(
        config,
        r"(?m)^pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[\"[^\"\n]*\"\];$",
        f"pub const RENDEZVOUS_SERVERS: &[&str] = &[{server_domain}];",
        "rendezvous server",
    )
    config = replace_regex_once(
        config,
        r"(?m)^pub const RS_PUB_KEY: &str = \"[^\"\n]*\";$",
        f"pub const RS_PUB_KEY: &str = {public_key};",
        "rendezvous public key",
    )

    required = (
        f"pub const RENDEZVOUS_SERVERS: &[&str] = &[{server_domain}];",
        f"pub const RS_PUB_KEY: &str = {public_key};",
    )
    if any(value not in config for value in required):
        raise RuntimeError("hbb_common config override validation failed")
    HBB_CONFIG_PATH.write_text(config, encoding="utf-8")


if __name__ == "__main__":
    apply_source_overrides()
    apply_hbb_config_override()
    print("Applied syscc repository-variable changes to the checked-out source")
