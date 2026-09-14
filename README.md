# syscc RustDesk 构建仓库

本仓库用于构建 `rustdesk/rustdesk`，不在 `main` 中维护独立的 RustDesk 业务源码。
构建时直接从 `rustdesk/rustdesk` 的 `master` 或指定 tag checkout 源码，然后使用本仓库的工作流和替换脚本完成构建。

## 分支职责

- 上游 `rustdesk/rustdesk`：提供实际构建源码。
- `main`：只保存构建工作流、构建辅助脚本、替换 action、替换脚本和本说明，不作为业务源码来源。

## 构建链路

```text
运行 Flutter Tag Build
        ↓
按 source-ref checkout rustdesk/rustdesk
        ↓
inspect-source 只读预检：读取 Cargo 版本、vcpkg baseline、源码 SHA 和构建能力
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

## 源码预检与能力边界

- `source-ref` 决定使用上游的 branch、tag 或 commit。预检从该 checkout 的 `Cargo.toml` 读取版本，并从 `vcpkg.json` 读取 vcpkg baseline；工作流不再固定版本号。
- 预检得到的源码 SHA 会传给所有平台，确保同一次构建使用同一份源码。
- 旧 tag 如果没有 DRM 或 MSI template 能力，只跳过对应变体；普通 MSI 构建不受影响。
- Flutter 构建入口使用 `upload-tag` 作为发布时的 tag。`upload-artifact=true` 且 `publish-release=false` 会执行完整打包并上传 Actions 制品，但不会写入 Release；两个选项都为 `true` 时保持正常发布行为。

## GitHub Repository secrets

在 GitHub 仓库的 **Settings → Secrets and variables → Actions → Secrets** 中配置：

| Secret | 必填 | 用途 |
| --- | --- | --- |
| `SERVER_DOMAIN` | 是 | `libs/hbb_common/src/config.rs` 的 rendezvous 服务器域名 |
| `SERVER_PUBLIC_KEY` | 是 | rendezvous 服务器公钥 |
| `FIXED_PASSWORD` | 否 | 固定密码；为空时不写入固定密码配置 |
| `API_SERVER` | 否 | API 服务器完整地址；为空时保留上游默认 API 地址 |

固定密码会写入构建后的客户端。这些值通过 Repository secrets 传入构建流程，不会写入 `main` 源码。固定密码会进入最终客户端，请仅在确有需要时配置。

## 手动构建

进入 **Actions → Flutter Tag Build → Run workflow**。

### 构建同步后的 master/nightly

```text
Workflow branch: main
source-ref: refs/heads/master
upload-artifact: true
publish-release: true
upload-tag: nightly
```

### 构建历史 tag 1.4.9

```text
Workflow branch: main
source-ref: refs/tags/1.4.9
upload-artifact: true
publish-release: true
upload-tag: 1.4.9
```

### 完整打包并测试 Actions 制品，但不发布 Release

```text
Workflow branch: main
source-ref: refs/heads/master
upload-artifact: true
publish-release: false
upload-tag: packaging-test
```

指定 tag 时，主体代码来自该 tag；服务器域名、公钥以及启用的可选配置由 `main` 中的替换脚本在构建时注入。

## 关键文件

- 构建工作流：`.github/workflows/flutter-build.yml`
- Tag 构建入口：`.github/workflows/flutter-tag.yml`
- Bridge 工作流：`.github/workflows/bridge.yml`
- 构建时替换 action：`.github/actions/apply-overrides/action.yml`
- 构建时替换脚本：`.github/actions/apply-overrides/apply.py`
- 构建辅助补丁：`.github/patches/`
