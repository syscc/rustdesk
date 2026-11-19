<p align="center">
  <img src="https://raw.githubusercontent.com/rustdesk/rustdesk/master/res/logo-header.svg" alt="RustDesk - Your remote desktop">
</p>

# syscc RustDesk 构建仓库

[![本仓库最新发布](https://img.shields.io/github/release/syscc/rustdesk.svg?label=syscc%2Frustdesk)](https://github.com/syscc/rustdesk/releases) [![上游最新发布](https://img.shields.io/github/release/rustdesk/rustdesk.svg?label=rustdesk%2Frustdesk)](https://github.com/rustdesk/rustdesk/releases)

本仓库用于构建 [rustdesk/rustdesk](https://github.com/rustdesk/rustdesk)，不在 `main` 中维护独立的 RustDesk 业务源码。
构建时直接从 [rustdesk/rustdesk](https://github.com/rustdesk/rustdesk) 的 `master` 或指定 tag checkout 源码，然后使用本仓库的工作流和替换脚本完成构建。

## 分支职责

- 上游 `rustdesk/rustdesk`：提供实际构建源码。
- `main`：只保存构建工作流、构建辅助脚本、替换 action、替换脚本和本说明，不作为业务源码来源。

## 构建链路

```text
运行 Flutter Tag Build
        ↓
解析 source-ref 与 upload-tag：值为 Latest 时取上游最新正式版 tag（如 1.5.3）
        ↓
按 source-ref checkout rustdesk/rustdesk
        ↓
inspect-source 预检：读取 Cargo 版本、vcpkg baseline、源码 SHA 和构建能力，并在需要发布时预创建 Release
        ↓
所有平台按同一个源码 SHA checkout
        ↓
读取 GitHub Repository secrets
        ↓
执行 .github/actions/apply-overrides/apply.py
        ↓
只修改 src/common.rs 和 libs/hbb_common/src/config.rs
        ↓
执行上游 RustDesk Flutter 构建
```

本仓库中的 `apply.py` 不复制 `main` 的 RustDesk 源文件，也不覆盖整个 `libs/hbb_common` 目录；它只对当前 checkout 的上游文件做定向替换。

指定 tag 时，主体代码来自该 tag；服务器域名、公钥以及启用的可选配置由 `main` 中的替换脚本在构建时注入。

## 自动同步上游正式版

`.github/workflows/sync-latest.yml` 每 12 小时检查一次上游 `rustdesk/rustdesk` 的最新正式 Release，并与本仓库已发布的正式 Release 比较：上游版本更高、或本仓库还没有带构建制品的正式 Release 时，自动以该上游 tag 调用 `flutter-build.yml` 完成一次完整构建并发布正式版；版本相同或本地更高时跳过，不触发构建。比较逻辑位于 `.github/actions/check-latest-release/`，只使用 Python 标准库，结果写入运行的 Summary。也可通过 **Actions → Sync Latest Upstream Stable Release → Run workflow** 手动触发一次检查。

- **失败重试**：比较只把「带制品的正式 Release」视为已发布。若某次同步构建在任何制品上传前就失败（只留下预建的 Release），下个 12 小时周期会重试；若已上传制品（例如 SBOM 或部分平台包）之后失败，该版本会被视为已发布，不会自动重试，需要人工重跑该构建。
- **并发排队**：定时同步与 `flutter-tag.yml` 的构建在**发布标签相同**时共用 `flutter-release-<tag>` 并发组、互不取消；用默认 `upload-tag`（`Latest`）的手动运行组名不同，不会被串行化。同步是否构建取决于检测时刻的结果，若排队期间人工构建已完成，同步仍可能对同一版本再跑一次构建。
- **版本解析假设**：上游正式 Release 的 tag 必须是 `X.Y.Z` 或 `vX.Y.Z`；其它形状（如带日期或后缀）会直接报错终止，不会误触发构建。
- **本地 Release 上限**：比较只读取本仓库前 100 个 Release，当前数量远低于该上限，不影响判定。

## 源码预检与能力边界

- `source-ref` 决定使用上游的 branch、tag 或 commit；默认值为 `refs/tags/Latest`，运行时解析为上游最新正式版 tag（如 `refs/tags/1.5.3`）。
- `upload-tag` 决定发布时的 Release 标签名；默认值为 `Latest`，运行时解析为上游最新正式版本号（如 `1.5.3`）。发布始终创建正式版，是否写入 Release 由 `publish-release` 控制。
- `inspect-source` 预检从该 checkout 的 `Cargo.toml` 读取版本，并从 `vcpkg.json` 读取 vcpkg baseline；工作流不再固定版本号。
- 预检得到的源码 SHA 会传给所有平台，确保同一次构建使用同一份源码。
- 旧 tag 如果没有 DRM 或 MSI template 能力，只跳过对应变体；普通 MSI 构建不受影响。
- `upload-artifact=true` 且 `publish-release=false` 会执行完整打包并上传 Actions 制品，但不会写入 Release；两个选项都为 `true` 时保持正常发布行为。

## 仓库 Secrets 配置

在 GitHub 仓库的 **Settings → Secrets and variables → Actions → Secrets** 中配置：

| Secret | 必填 | 用途 |
| --- | --- | --- |
| `SERVER_DOMAIN` | 是 | `libs/hbb_common/src/config.rs` 的 rendezvous 服务器域名 |
| `SERVER_PUBLIC_KEY` | 是 | rendezvous 服务器公钥 |
| `FIXED_PASSWORD` | 否 | 固定密码；为空时不写入固定密码配置 |
| `API_SERVER` | 否 | API 服务器完整地址；为空时保留上游默认 API 地址 |
| `PRE_ELEVATE_SERVICE` | 否 | 设为 `true`，启用 Windows portable 启动时自动请求提升便携服务权限；未配置、`false` 或误拼为 `ture` 时不启用 |

这些值通过 Repository secrets 传入构建流程，不会写入 `main` 源码；固定密码会写入最终客户端，请仅在确有需要时配置。

签名与公证相关的 secrets（`ANDROID_SIGNING_KEY`、`MACOS_P12_BASE64`、`SIGN_BASE_URL`、`SIGN_SECRET_KEY` 等）为可选：未配置时工作流会跳过对应签名步骤，仍可完成构建；Android 的签名 APK 制品仅在配置 `ANDROID_SIGNING_KEY` 时上传。

## Windows 便携服务自动提权（可选）

`PRE_ELEVATE_SERVICE` 使用上表中的 Actions Repository Secret，不读取同名 Repository Variable。两个构建入口通过 `secrets: inherit` 传入 Secret，仅在 Windows 准备步骤中将其与 `true` 比较，不向 action 传递或打印原值。

- **生效范围**：手动运行 `Flutter Tag Build`、推送 tag 和自动同步上游正式版触发的构建都会读取此 Secret，无需每次勾选；改动只影响后续构建的客户端，不影响已发布的客户端。
- **开启后的行为**：Windows portable 客户端（Flutter x64/ARM64 和 Sciter x86）普通无参数启动时会自动请求提升便携服务权限，省去用户在 RustDesk 中手动点击提权。Windows 仍会显示 UAC，用户需要确认；标准账户可能需要输入管理员凭据，取消 UAC 不会自动获得管理员权限。
- **实现方式**：复用上游的 `pre-elevate-service` 默认设置，不强制提升主界面权限；已安装版继续使用现有服务。已有本地设置或自定义客户端配置可覆盖此默认值，关闭时保持上游行为。若所选旧版本不支持该设置，开启时构建会明确报错。
- **范围限制**：只对 Windows 构建任务生效；也可在调用 `flutter-build.yml` 时显式传入布尔输入 `pre-elevate-service: true` 单独启用，不依赖此 Secret，也无需启用 `apply-syscc-patches`。

## 手动构建

进入 **Actions → Flutter Tag Build → Run workflow**（`Workflow branch` 选 `main`）。

`source-ref` 默认 `refs/tags/Latest`、`upload-tag` 默认 `Latest`：运行时会调用上游 `rustdesk/rustdesk` 的最新正式 Release（自动排除 prerelease 和 draft），把 `source-ref` 解析为对应 tag（如 `refs/tags/1.5.3`）、`upload-tag` 解析为同一版本号（如 `1.5.3`）。直接点 **Run workflow**、不修改任何输入，即可构建并发布上游最新正式版；解析结果会写入运行的 Summary。解析失败（上游没有正式 Release 或 API 不可用）时工作流直接报错终止，不会回退到其他分支或标签。

`source-ref` 可写 `Latest`、`latest`、`refs/tags/Latest`、`refs/tags/latest`；`upload-tag` 只接受 `Latest` 或 `latest`。任一项显式填写 branch、tag、commit 或自定义 Release 标签时，按填写内容执行（例如 `source-ref` 填 `refs/heads/master`、`upload-tag` 仍留 `Latest`，则构建 master、发布为上游最新正式版本号）。

除手动触发外，向本仓库推送符合 `v1.5.3` / `1.5.3` 模式的 tag 也会触发构建：此时 `source-ref` 取该 tag 的完整 ref、`upload-tag` 取 tag 名，并强制上传制品与发布 Release，不解析 `Latest`。

常用组合：

| 场景 | `source-ref` | `upload-artifact` | `publish-release` | `upload-tag` |
| --- | --- | --- | --- | --- |
| 构建上游最新正式版（默认） | `refs/tags/Latest` | 勾选 | 勾选 | `Latest` |
| 构建同步后的 master/nightly | `refs/heads/master` | 勾选 | 勾选 | `nightly` |
| 构建历史 tag 1.4.9 | `refs/tags/1.4.9` | 勾选 | 勾选 | `1.4.9` |
| 只打包制品、不发布 Release | `refs/heads/master` | 勾选 | 取消勾选 | `packaging-test` |

## 关键文件

- 构建工作流：`.github/workflows/flutter-build.yml`
- Tag 构建入口：`.github/workflows/flutter-tag.yml`
- 自动同步工作流：`.github/workflows/sync-latest.yml`
- 上游版本比较 action：`.github/actions/check-latest-release/action.yml`
- Bridge 工作流：`.github/workflows/bridge.yml`
- 构建时替换 action：`.github/actions/apply-overrides/action.yml`
- 构建时替换脚本：`.github/actions/apply-overrides/apply.py`
- 构建辅助补丁：`.github/patches/`

## 许可证

本仓库遵循 [AGPL-3.0](LICENCE)；上游 RustDesk 源码的许可说明见 [rustdesk/rustdesk](https://github.com/rustdesk/rustdesk)。
