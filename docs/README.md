# docs

本仓库（`gqy-plugins-EmotePack-default`）自己的文档索引。仓库是 GQY 的「表情包」插件，文档保持最小；三层文档规范与跨仓库决定都在主仓库 `GQY/docs/`。

## 文档索引

| 文件 | 内容 |
| --- | --- |
| [`../README.md`](../README.md) | 插件定位、24 张表情与 4 个动图清单、三个动作、安装方式、工具名、与主仓库关系、不做 |
| `docs/README.md`（本文件） | 文档索引 + manifest 约定 + 动作语义 + 当前状态 |
| [`GQY/docs/designs/04-插件运行时.md`](../../GQY/docs/designs/04-插件运行时.md) | 插件运行时四层模型与 P1–P12（本插件相关：P3 manifest、P4 适配器、P8 进程回收、P9 热更新、P11 工具注册） |
| [`GQY/docs/blueprint/plugin.md`](../../GQY/docs/blueprint/plugin.md) | manifest 全字段表、原生 stdio 换行 JSON-RPC 契约、`/api/plugins` 接口（本插件按它逐字段实现） |
| [`GQY/docs/construction/015-表情包插件.md`](../../GQY/docs/construction/015-表情包插件.md) | 本插件的施工单与验收记录 |
| [`GQY/docs/construction/012-图片展示插件.md`](../../GQY/docs/construction/012-图片展示插件.md) | 上一个插件仓库的先例（硬编码清单版，本插件与它的差异见 015） |
| [`GQY/docs/designs/11-安全与沙盒.md`](../../GQY/docs/designs/11-安全与沙盒.md) | 安装即信任（SEC5），插件不设运行时权限墙 |

## manifest 约定（已实现）

`manifest.json` 在本仓库内提交，随仓库部署。字段规则见 [`GQY/docs/blueprint/plugin.md`](../../GQY/docs/blueprint/plugin.md) 的 manifest 全字段表。

| 字段 | 值 |
| --- | --- |
| `id` | `emote-pack-default`（目录名必须与它一致） |
| `name` / `version` | `表情包插件` / `1.0.0` |
| `protocol` | `gqy-jsonrpc-v1`（原生 stdio 换行 JSON-RPC 2.0） |
| `command` | `["node", "index.js"]`，相对 manifest 目录，不经过 shell |
| `cwd` | `.` |
| `timeout_ms` | `10000` |
| `concurrency` | `concurrent` |
| `actions` | `list` / `show` / `match`，各带 JSON Schema |
| `provides` / `requires` | 空数组 |

三个动作的 schema 要点：

- `list`：无参数，`{"type":"object","properties":{}}`。
- `show`：`name` 必填（string，清单里的 name，也接受清单里的 file 路径）；`inline` 可选（boolean，默认 false）。
- `match`：`query` 必填（string）；`limit` 可选（integer，1–24，默认 3）。

协议方法：`initialize` 回 `{ok:true,name,version}`；`call` 传 `{action, arguments}`，结果取 `result.content`；`shutdown` 回 ok 后退出 0。stdin EOF 自退是最后一道防线（04 P8）。错误码：非法请求对象 `-32600`、未知方法或动作 `-32601`、参数形状不合法 `-32602`、业务 / 资产错误 `-32000`、非法 JSON 行 `-32700`（`id` 回 null）；全部只回错误、不崩进程。参数错误与业务错误分成两个码，调用方可以区分。

## 动作语义

补充 `../README.md` 动作表之外的细节：

- `list`：返回 `{count, emotes[], motions[]}` 的 JSON 字符串（`count` 是表情 + 动图合计，28）。每条含 `name` / `label` / `tags` / `file` / `bytes` / `width` / `height`；动图另含 `frames` / `frame_ms` / `duration_ms` / `note`。启动时读 `data/assets.json` 当清单，`bytes` 现读磁盘不缓存；每次调用前都重新校验清单与磁盘——缺失、多余、空文件、魔数与扩展名不符、尺寸不符都报清晰错误，不静默跳过。资产目录里只允许清单登记的文件；系统元数据（`.DS_Store`、`.localized`、`Thumbs.db`、`._*`）忽略，其余多余文件或子目录报错。
- `show`：只查清单，不接受任意路径（防目录穿越）。返回 `{name, label, media, file, bytes}`；`inline:true` 时追加 `data_uri`：PNG 是 `data:image/png;base64,…`，GIF 是 `data:image/gif;base64,…`。默认不内联：表情约 200–270 KiB，单张表情 inline 约 0.3–0.4 MB，适合按需用；动图最大 5.6 MB，data URI 约 7.5 MB，接近蓝图规定的 stdio 单行 8 MB 缓冲上限，动图慎用 inline。
- `match`：只在 24 张表情上打分（动图用 `show` 按名称取），确定性，不联网、不调模型。`label` 完全相等 +100；`tag` 完全相等 +60；`label` 包含 query +40；query 包含 `label` +30；tag 与 query 互相包含各 +20；全等是包含的特例，同一项只取最高档、不叠加。按分数降序，同分按 `data/assets.json` 顺序；`limit` 默认 3、上限 24。全 0 分返回空 `matches`，并给 `hint`：「没有匹配的表情，可用 list 查看全部」。
- 三个动作都不写文件：资产与清单只读；参数不合法回 `-32602`，`name` / `query` 找不到回 `-32000`。

## 当前状态

- 实现已就位：`manifest.json`、`index.js`（零依赖，只用 Node 内置模块）、`package.json`，测试在 `test/manifest.mjs`、`test/handshake.mjs`。
- 测试命令：`cd gqy-plugins-EmotePack-default && node --check index.js && npm test`（`npm test` 串起 manifest 校验与握手测试；零依赖，Node ≥18）。截至 2026-11-02 本地跑通：manifest 与资产 490 项检查（含临时副本系统元数据用例）通过，握手全链路通过。
- 资产清单唯一来源：`data/assets.json`（`schema: 1`，24 条 `emotes` + 4 条 `motions`，含宽高、帧数、帧时长、总时长、说明），只读，不由插件改写。
- 动图口径：`motions/表情包-微信240-24态.gif` 实测 240×240、6 帧、450ms/帧、2.7s；文件名里的「24 态」与内容不符，以 `data/assets.json` 为准。
- 安装与自检步骤见 `../README.md`；GQY 侧端到端结果见施工单 `015-表情包插件.md` 的验收记录（「留底」一节）。
