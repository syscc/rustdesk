#!/usr/bin/env python3
"""Apply syscc's small source changes to an upstream RustDesk checkout."""

from pathlib import Path


ROOT = Path.cwd()
SRC_PATH = ROOT / "src/common.rs"
HBB_CONFIG_PATH = ROOT / "libs/hbb_common/src/config.rs"

DEBUG_OVERRIDES = """        // Set permanent password
        {
            let mut hard_settings = config::HARD_SETTINGS.write().unwrap();
            hard_settings.insert("password".to_string(), "0000".to_string());
            // 同时设置验证方法为只使用固定密码
            hard_settings.insert("verification-method".to_string(), "use-permanent-password".to_string());
        }
        // Ensure remote configuration modification is enabled by default
        {
            let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();
            defaults
                .entry(keys::OPTION_ALLOW_REMOTE_CONFIG_MODIFICATION.to_string())
                .or_insert("Y".to_string());
        }
        // Enable hiding connection management window by default
        {
            let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();
            defaults
                .entry("allow-hide-cm".to_string())
                .or_insert("Y".to_string());
        }
"""

RUNTIME_OVERRIDES = """    // Set permanent password
    {
        let mut hard_settings = config::HARD_SETTINGS.write().unwrap();
        hard_settings.insert("password".to_string(), "0000".to_string());
        // 同时设置验证方法为只使用固定密码
        hard_settings.insert("verification-method".to_string(), "use-permanent-password".to_string());
    }
    // Ensure remote configuration modification is enabled by default
    {
        let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();
        defaults
            .entry(keys::OPTION_ALLOW_REMOTE_CONFIG_MODIFICATION.to_string())
            .or_insert("Y".to_string());
    }
    // Enable hiding connection management window by default
    {
        let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();
        defaults
            .entry("allow-hide-cm".to_string())
            .or_insert("Y".to_string());
    }
"""


def replace_once(text: str, old: str, new: str, description: str) -> str:
    if new in text and old not in text:
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one upstream {description} anchor, found {count}"
        )
    return text.replace(old, new, 1)


def apply_source_overrides() -> None:
    if not SRC_PATH.is_file():
        raise RuntimeError(f"Missing source file: {SRC_PATH}")

    source = SRC_PATH.read_text(encoding="utf-8")
    source = replace_once(
        source,
        '"https://admin.rustdesk.com".to_owned()',
        '"https://rustdesk.yyej.com".to_owned()',
        "API server URL",
    )

    debug_anchor = "        read_custom_client(data.trim());\n"
    if DEBUG_OVERRIDES not in source:
        if source.count(debug_anchor) != 1:
            raise RuntimeError("Could not find the debug custom-client anchor")
        source = source.replace(debug_anchor, debug_anchor + DEBUG_OVERRIDES, 1)

    runtime_anchor = "        read_custom_client(&data.trim());\n    }\n}"
    if RUNTIME_OVERRIDES not in source:
        if source.count(runtime_anchor) != 1:
            raise RuntimeError("Could not find the runtime custom-client anchor")
        source = source.replace(runtime_anchor, "        read_custom_client(&data.trim());\n    }\n\n" + RUNTIME_OVERRIDES + "}", 1)

    required = (
        '"https://rustdesk.yyej.com".to_owned()',
        'hard_settings.insert("password".to_string(), "0000".to_string());',
        'keys::OPTION_ALLOW_REMOTE_CONFIG_MODIFICATION',
        'hard_settings.insert("verification-method".to_string(), "use-permanent-password".to_string());',
    )
    if any(value not in source for value in required):
        raise RuntimeError("Source override validation failed")
    SRC_PATH.write_text(source, encoding="utf-8")


def apply_hbb_config_override() -> None:
    if not HBB_CONFIG_PATH.is_file():
        raise RuntimeError(f"Missing hbb_common config: {HBB_CONFIG_PATH}")

    config = HBB_CONFIG_PATH.read_text(encoding="utf-8")
    config = replace_once(
        config,
        'pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];',
        'pub const RENDEZVOUS_SERVERS: &[&str] = &["rustdesk.yyej.com"];',
        "rendezvous server",
    )
    config = replace_once(
        config,
        'pub const RS_PUB_KEY: &str = "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=";',
        'pub const RS_PUB_KEY: &str = "hb1dgUucE14VC8prE9Kapq9mo7iHJQDX8dxZUBxOOoo=";',
        "rendezvous public key",
    )

    required = (
        'pub const RENDEZVOUS_SERVERS: &[&str] = &["rustdesk.yyej.com"];',
        'pub const RS_PUB_KEY: &str = "hb1dgUucE14VC8prE9Kapq9mo7iHJQDX8dxZUBxOOoo=";',
    )
    if any(value not in config for value in required):
        raise RuntimeError("hbb_common config override validation failed")
    HBB_CONFIG_PATH.write_text(config, encoding="utf-8")


if __name__ == "__main__":
    apply_source_overrides()
    apply_hbb_config_override()
    print("Applied syscc changes to src/common.rs and libs/hbb_common/src/config.rs")
