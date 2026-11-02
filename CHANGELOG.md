# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 建立仓库标准基线：`LICENSE`（PolyForm Noncommercial License 1.0.0）、`NOTICE`、`CONTRIBUTING.md`、`SECURITY.md`、`.editorconfig`、`Makefile`、`.github/`（CI、Issue / PR 模板、dependabot）。

### Changed

- `package.json` 补 `repository` / `homepage` 元数据，`license` 指向 `LICENSE`；仍为零依赖，无 devDependencies。
- `README.md` 顶部加 CI 徽章，末尾加「许可与署名」小节（禁止商用 + 四项署名格式 + 指向 `NOTICE`）。
- `.gitignore` 补 `__pycache__/`、`*.pyc` 等本地临时产物忽略项。

### Fixed

- 无。

## [1.0.0] - 2026-11-02

### Added

- 首个版本：24 张 512×512 静态表情（`emotes/`）+ 4 个动图包（`motions/`）。
- 零依赖 stdio 换行 JSON-RPC（`gqy-jsonrpc-v1`）插件服务 `index.js`，动作 `list` / `show` / `match`。
- 资产索引唯一来源 `data/assets.json`，启动时读取、每次调用按磁盘重新校验（缺失 / 多余 / 空文件 / 尺寸不符都报清晰错误）。
- 本地自检：`test/manifest.mjs`（manifest 与资产清单校验）与 `test/handshake.mjs`（stdio 握手全链路）。
