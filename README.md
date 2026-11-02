# gqy-plugins-EmotePack-default

GQY 的「表情包」插件：24 张 512×512 静态表情 + 4 个动图包，通过插件运行时提供 `list` / `show` / `match` 三个动作。

## 干什么

- 内置 `emotes/` 24 张静态表情（512×512 PNG），覆盖平静、开心、生气、无语、托腮等日常情绪。
- 内置 `motions/` 4 个动图包（GIF），尺寸有 512×512 与 240×240 两种。
- 通过 GQY 插件运行时暴露三个动作：`list`（列出全部资产）、`show`（取单个表情或动图，可选内联为 data URI）、`match`（在 24 张表情里按情绪标签确定性匹配）。已实现，stdio 换行 JSON-RPC，零依赖。
- 资产索引的唯一来源是 `data/assets.json`，插件启动时读取；本文表里的数字与它一致。

## 资产清单

### 表情（`emotes/`，24 张）

全部 512×512、PNG（RGBA），单张 190.7–268.3 KiB，合计 5,927,196 字节（约 5.65 MiB）。标签取自 `data/assets.json`，字节按 `stat -f%z` 实测。

| 名称 | 标签 | 文件 | 字节 |
| --- | --- | --- | --- |
| `01-平静` | 平静、冷静、日常、默认 | `emotes/01-平静.png` | 254235 |
| `02-浅笑` | 浅笑、微笑、开心、高兴 | `emotes/02-浅笑.png` | 252518 |
| `03-侧目` | 侧目、怀疑、斜眼、观察 | `emotes/03-侧目.png` | 255270 |
| `04-皱眉` | 皱眉、疑惑、不满、为难 | `emotes/04-皱眉.png` | 255853 |
| `05-困倦` | 困倦、累、困、疲惫 | `emotes/05-困倦.png` | 255439 |
| `06-惊讶` | 惊讶、吃惊、震惊、意外 | `emotes/06-惊讶.png` | 254495 |
| `07-专注` | 专注、认真、工作、集中 | `emotes/07-专注.png` | 195253 |
| `08-喝茶` | 喝茶、悠闲、放松、休息 | `emotes/08-喝茶.png` | 216273 |
| `09-打哈欠` | 打哈欠、困、无聊、疲惫 | `emotes/09-打哈欠.png` | 228732 |
| `10-得意` | 得意、骄傲、自信、成功 | `emotes/10-得意.png` | 232692 |
| `11-思考` | 思考、疑问、考虑、想 | `emotes/11-思考.png` | 245796 |
| `12-被叫到` | 被叫到、召唤、点名、警觉 | `emotes/12-被叫到.png` | 241089 |
| `13-生气` | 生气、愤怒、不满、抗议 | `emotes/13-生气.png` | 263766 |
| `14-落泪` | 落泪、难过、伤心、哭 | `emotes/14-落泪.png` | 266004 |
| `15-害羞` | 害羞、脸红、紧张、矜持 | `emotes/15-害羞.png` | 269460 |
| `16-无语` | 无语、无奈、沉默、尴尬 | `emotes/16-无语.png` | 259359 |
| `17-睡着` | 睡着、睡、晚安、休息 | `emotes/17-睡着.png` | 274746 |
| `18-大笑` | 大笑、笑、开心、哈哈 | `emotes/18-大笑.png` | 263943 |
| `19-抱猫` | 抱猫、猫、可爱、治愈 | `emotes/19-抱猫.png` | 218379 |
| `20-撑伞` | 撑伞、雨天、下雨、浪漫 | `emotes/20-撑伞.png` | 237228 |
| `21-举牌` | 举牌、牌子、提示、举 | `emotes/21-举牌.png` | 235431 |
| `22-扶额` | 扶额、无奈、头疼、尴尬 | `emotes/22-扶额.png` | 241180 |
| `23-比心` | 比心、喜欢、爱、谢谢 | `emotes/23-比心.png` | 244225 |
| `24-托腮` | 托腮、发呆、无聊、等待 | `emotes/24-托腮.png` | 265830 |

### 动图（`motions/`，4 个）

帧数、帧时长、总时长取自 `data/assets.json`，已用 Pillow 逐帧复核一致；字节按 `stat -f%z` 实测，合计 8,342,839 字节（约 7.96 MiB）。

| 名称 | 尺寸 | 帧数 | 帧时长 | 总时长 | 文件 | 字节 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `全24态` | 512×512 | 24 | 520ms | 12.48s | `motions/表情包-全24态.gif` | 2137263 | 24 态依次轮播 |
| `全局呼吸24态` | 512×512 | 72 | 400ms | 28.8s | `motions/表情包-全局呼吸24态.gif` | 5597005 | 24 态各 3 帧呼吸循环 |
| `动作六态` | 512×512 | 6 | 620ms | 3.72s | `motions/表情包-动作六态.gif` | 481899 | 六动作态轮播（抱猫/撑伞/举牌/扶额/比心/托腮） |
| `微信240-24态` | 240×240 | 6 | 450ms | 2.7s | `motions/表情包-微信240-24态.gif` | 126672 | 微信尺寸版，六动作态轮播（见下方注意） |

注意：`motions/表情包-微信240-24态.gif` 的文件名写「24 态」，实际内容是 **240×240、6 帧、450ms/帧、共 2.7s 的六动作态轮播**，名称与内容不符。本表按实测事实写；读清单请以 `data/assets.json` 的 `motions[].frames` / `frame_ms` / `duration_ms` 为准，不要按文件名推断。

## 动作

| 动作 | 参数 | 返回 |
| --- | --- | --- |
| `list` | 无 | `{count, emotes[], motions[]}` 的 JSON 字符串（`count` 为 28）；每条含 `name` / `label` / `tags` / `file` / `bytes` / `width` / `height`，动图另含 `frames` / `frame_ms` / `duration_ms` / `note` |
| `show` | `name` 必填；`inline` 可选（默认 false） | `{name, label, media, file, bytes}`；`inline:true` 时追加 `data_uri`（`data:image/png;base64,…` 或 `data:image/gif;base64,…`） |
| `match` | `query` 必填；`limit` 可选（默认 3，范围 1–24） | `{query, matches:[{name, label, score, file}], hint?}`；只在 24 张表情上打分，按 label / tag 确定性打分降序，同分按清单顺序 |

几点说明：

- `list` 只读清单，不读图片内容，包体很小；`bytes` 现读磁盘，不缓存。每次调用前都重新校验清单与磁盘：缺失、多余、空文件报错；系统元数据（`.DS_Store`、`.localized`、`Thumbs.db`、`._*`）忽略，不算多余文件。
- `show` 只认清单里的 `name`（也接受清单里的 `file` 路径），不接受任意路径（防目录穿越）。默认不内联、只回文件信息；`inline:true` 才带 data URI。**表情适合按需内联**（约 200–270 KiB，data URI 约 0.3–0.4 MB）；**动图慎用**——最大 5.6 MB，data URI 约 7.5 MB，接近 GQY stdio 单行 8 MB 上限，容易把一行响应撑爆。
- `match` 不联网、不调模型、可复现：`label` 完全相等 +100；`tag` 完全相等 +60；`label` 包含 query +40；query 包含 `label` +30；tag 与 query 互相包含各 +20。同一项只取最高档（label 全等时不再叠 +40）；只匹配 24 张表情，动图用 `show` 按名称取。全 0 分返回空 `matches` 并给 `hint`（「没有匹配的表情，可用 list 查看全部」）。

## 安装方式

GQY 按 `$GQY_HOME/plugins/<id>/manifest.json` 发现插件，约定见 [`GQY/docs/designs/04-插件运行时.md`](../GQY/docs/designs/04-插件运行时.md) 的 P3（manifest 字段）与 P9（安装 / 扫描）。`$GQY_HOME` 默认是 `~/.gqy`，服务默认监听 `127.0.0.1:5173`。

把本仓库部署成（目录名必须是 plugin id，不是仓库名）：

```
$GQY_HOME/plugins/emote-pack-default/
  manifest.json      # 本仓库自带，直接使用
  index.js           # 零依赖 stdio JSON-RPC 服务
  package.json
  data/assets.json   # 唯一资产索引，只读
  emotes/            # 24 张 512×512 PNG
  motions/           # 4 个 GIF
  docs/
  README.md
```

- 自检（装之前也能跑）：`node --check index.js && npm test`，走 manifest 校验 + initialize → list → show → match → shutdown 全链路。零依赖，只需 Node ≥18。
- 加载：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/load`
- 调试调用：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/call -H 'Content-Type: application/json' -d '{"action":"match","arguments":{"query":"累"}}'`
- 卸载：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/unload`（幂等）。
- 管理接口受 02 R8 鉴权：设了 `GQY_AUTH_TOKEN` 时请求要带 token。
- 安装插件 = 信任它：GQY 不设运行时权限墙（见 `GQY/docs/designs/11-安全与沙盒.md` SEC5），只从可信来源安装。

## 工具名

加载成功后三个动作注册成外部工具（04 P11）：`emote-pack-default__list`、`emote-pack-default__show`、`emote-pack-default__match`。

默认不注入模型：外部工具只在 `GQY_ENABLE_EXTERNAL=1` 或运行时开关开启时进 agent 的工具列表；HTTP `POST /api/plugins/emote-pack-default/call` 始终可单独调试。

## 与主仓库的关系

- 本仓库与 `GQY/` 主仓库地位平等，都在 GQYTeam 组织工作区里（见 org 根 `README.md` 的仓库地图）。
- 本仓库依赖 GQY 的插件运行时与 manifest 约定，但不 import GQY 的代码。
- 跨仓库决定只写一份，放在主仓库 `GQY/docs/designs/`；本仓库只引用，不复制。

## 不做

- **不写文件**：只读 `emotes/`、`motions/`、`data/assets.json`；不上传、不生成、不改名、不删资产。
- **不联网**：`match` 全在本地按标签打分，不调模型、不调外部接口。
- **不转码**：GIF 与 PNG 都是成品；运行时不做格式转换、不缩放、不压缩。
- **不导出微信包**：不做表情包导出 / 打包；「微信 240」只是 `motions/` 里已有的一个 240×240 GIF。
