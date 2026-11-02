# gqy-plugins-EmotePack-default

[![CI](https://github.com/GQYTeam/gqy-plugins-EmotePack-default/actions/workflows/ci.yml/badge.svg)](https://github.com/GQYTeam/gqy-plugins-EmotePack-default/actions/workflows/ci.yml)

GQY 的「表情包」插件：24 张 512×512 静态表情 + 4 个动图包，通过插件运行时提供 `list` / `show` / `match` / `suggest` 四个动作（`suggest` 是本地训练、零依赖 JS 推理的「语境 → 表情」小模型）。

当前版本 **1.2.0**：在四个动作之外，`manifest.json` 还声明了 `settings`（`auto_emote` / `min_score`），供 GQY 设置页与「回复后自动发表情」使用，详见下文「设置项（GQY 设置页）」。

## 干什么

- 内置 `emotes/` 24 张静态表情（512×512 PNG），覆盖平静、开心、生气、无语、托腮等日常情绪。
- 内置 `motions/` 4 个动图包（GIF），尺寸有 512×512 与 240×240 两种。
- 通过 GQY 插件运行时暴露四个动作：`list`（列出全部资产）、`show`（取单个表情或动图，可选内联为 data URI）、`match`（在 24 张表情里按情绪标签确定性匹配）、`suggest`（按对话语境用本地小模型推荐表情）。已实现，stdio 换行 JSON-RPC，零依赖。
- `suggest` 的语境模型是训练产物 `data/emote-model.json`（离线 numpy 训练、插件内零依赖 JS 推理），指标、体积、局限见下文「语境模型」。
- manifest 声明 `settings`（`auto_emote` / `min_score`）：GQY 可在每次回复结束后，按语境模型结果与设置自动追加一张表情（见「设置项」）。
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
| `suggest` | `context` 必填（1–2000 字符）；`limit` 可选（默认 3，范围 1–5） | `{decision, none_score, predictions:[{name, label, score, file}], hint?}`；语境模型推理，`decision` 为 `emote` / `none`；`none` 时 `predictions` 为空数组并带 `hint`（「该语境不适合发表情」） |

几点说明：

- `list` 只读清单，不读图片内容，包体很小；`bytes` 现读磁盘，不缓存。每次调用前都重新校验清单与磁盘：缺失、多余、空文件报错；系统元数据（`.DS_Store`、`.localized`、`Thumbs.db`、`._*`）忽略，不算多余文件。
- `show` 只认清单里的 `name`（也接受清单里的 `file` 路径），不接受任意路径（防目录穿越）。默认不内联、只回文件信息；`inline:true` 才带 data URI。**表情适合按需内联**（约 200–270 KiB，data URI 约 0.3–0.4 MB）；**动图慎用**——最大 5.6 MB，data URI 约 7.5 MB，接近 GQY stdio 单行 8 MB 上限，容易把一行响应撑爆。
- `match` 不联网、不调模型、可复现：`label` 完全相等 +100；`tag` 完全相等 +60；`label` 包含 query +40；query 包含 `label` +30；tag 与 query 互相包含各 +20。同一项只取最高档（label 全等时不再叠 +40）；只匹配 24 张表情，动图用 `show` 按名称取。全 0 分返回空 `matches` 并给 `hint`（「没有匹配的表情，可用 list 查看全部」）。
- `suggest` 也只读、不联网：模型在插件进程内推理，不调用外部服务。`context` 非法（缺失/非字符串/空/超 2000 字符/纯空白）回 `-32602`；模型文件缺失或损坏回 `-32000`（进程不崩）；`limit` 只影响返回条数、不影响决策（`decision` 只看 25 类概率第一名是不是 `none`）。

## 语境模型（`suggest`）

> 模型卡（指标、许可、下载与复现）：[`MODEL_CARD.md`](MODEL_CARD.md)。


`suggest` 是本地训练的「语境 → 表情」小模型：输入一段对话语境（最近 1–3 轮，1–2000 字符），输出 24 张表情 + `none` 共 25 类的概率，返回 `decision`、`none_score` 与 top-k 候选。**定位是粗筛 / top-3 建议**——发不发、发哪张由角色或调用方决定，模型不自动发图。

### 怎么训练

- 语料：合成标注（`synthetic-authored-v1`），4 位作者按 [`train/annotation-guide.md`](train/annotation-guide.md) 写，**不含真实用户聊天记录**；训练 2,399 条、同作者 val 288 条、独立作者 holdout 280 条（holdout 只在最终验收评估一次，不参与调参）。
- 特征 / 模型：char 1/2/3-gram → binary 特征 → 1,200 维词表 → 单隐层 32 维 tanh → softmax；numpy 实现，无第三方 ML 框架，纯 CPU 秒级训练、全程不联网。
- 选模：4 折模板交叉验证（同类内按模板聚类留出整簇，验证集句式不进训练），holdout 不参与选模。
- 推理：插件内零依赖 JS（`index.js`），严格按 [`train/model-format.md`](train/model-format.md) 第 4 节与 Python 参考实现逐字对齐（NFKC → 小写 → 删空白 → 末尾 120 码点 → 1/2/3-gram → MLP → softmax）；`test/model.mjs` 做结构 + parity + 动作级验收。
- 复现命令见 `train/model-format.md` 第 7 节（`prepare.py` → `train.py` → `eval.py`）。

### 指标（真实值；三种口径分开标注，不要混用）

| 口径 | 说明 | acc1 | acc3 | none F1 |
| --- | --- | --- | --- | --- |
| 4 折模板 CV（**选模口径**） | 同作者、留出未见句式，4 折均值；`metrics.val_acc1` / `val_acc3` 即此口径 | 0.69863 | 0.86995 | 0.68857 |
| 同作者 val（参考，偏乐观） | 88/12 分层切分；最终模型已并入全量重训，**不可作为泛化证据** | 0.7396 | 0.9028 | 0.6364 |
| **holdout（独立作者，最终验收）** | 第 5 位作者另写 280 条，只评估一次 | **0.49286** | **0.66429** | **0.61538** |

holdout 的 top-5 命中率 **acc5 = 0.76071**（`metrics.holdout_acc5`）。
关键词 `match` 基线在 holdout 上 acc1 0.14643 / acc3 0.23571；模型比基线高 34.6 / 42.9 个百分点。
按 `train/model-format.md` §6：none F1 ≥0.55 与「比基线高 ≥8pp」达标；holdout_acc1 ≥0.55（目标 0.60）、holdout_acc3 ≥0.80 **尚未达标**（实测 0.4929 / 0.6643）——属合成语料跨作者口径差异导致的已知局限，不是集成问题。

**置信度覆盖（能力边界，建议调用方按阈值门控）**：按 top1 置信度从高到低取子集，子集内 top1 精度为——覆盖 30% 时 0.8214、50% 时 0.6643、70% 时 **0.6020**（`metrics.coverage_70_top1_precision`）、90% 时 0.5198。覆盖率越高，精度越接近全量 0.4929。`suggest` 只给建议；调用方可按 `top1 score`（或 `1 - none_score`）阈值决定是否自动发，阈值以下降级为 top-3 候选或 `match`。

### 模型卡

| 项 | 值 |
| --- | --- |
| `schema` / `task` | `1` / `context-emote-clf` |
| `trained_at` | `2026-11-02` |
| `classes` | 25（24 张表情 + `none`，顺序与 `data/assets.json` 一致） |
| 输入规格 | 末尾 120 码点；char 1/2/3-gram；binary |
| 结构 | MLP tanh，`hidden=32`，`vocab≤1200` |
| 体积 | 347,959 字节（约 339.8 KiB，预算 ≤600 KB） |
| 推理延迟 | 插件 JS 0.03–0.06 ms/次（200 次均值，M2；不同测量轮次差异）；`suggest` 端到端 p50 0.5–1 ms、经 HTTP p50 0.95 ms（≤50ms 阈值） |
| holdout acc5 / 覆盖 70% 精度 | 0.76071 / 0.60204（`metrics.holdout_acc5` / `metrics.coverage_70_top1_precision`） |
| 训练资源 | 4 折 CV + 全量重训约 9.1 s，峰值 RSS 177 MB，纯 CPU |
| 模型卡字段 | `list` 返回 `model`：`{schema, trained_at, classes, metrics:{val_acc1, holdout_acc1, holdout_acc5, none_recall, coverage_70_top1_precision}, size_bytes}` |

### 局限

- **跨作者口径差异（核心）**：4 位作者的标签互不相交、句式自成一派；模板 CV / 同作者 val ≈0.70 / 0.74，换第 5 位作者的 holdout 降到 0.49，存在系统性分布偏移。
- **弱类 8 个**（holdout 单类 F1 < 0.2）：`01-平静` 0.160、`04-皱眉` 0.125、`07-专注` 0.133、`08-喝茶` 0.182、`09-打哈欠` 0.000、`10-得意` 0.000、`11-思考` 0.182、`16-无语` 0.154；这些类信号弱或与同情绪家族重叠，换作者后措辞差异最大。
- 容易把弱类误判到同情绪家族（如 `09-打哈欠` → `02-浅笑`），所以只做**粗筛 / top-3 建议**，不自动发图；调用方可结合上下文二次裁决。
- **能力边界（置信度）**：全量 holdout acc1 只有 0.49；按置信度门控后，覆盖 70% 时 top1 精度 0.602、覆盖率 30% 时 0.821。建议只在阈值以上自动发，阈值以下降级为 top-3 候选或 `match`。
- 长语境只取末尾 120 码点；反讽、省略、混合情绪判别力有限。
- 合成语料、无真实用户数据；真实场景需重新采集与标注（需用户授权并去标识化）。

### 与 `match` 的区别

| | `match` | `suggest` |
| --- | --- | --- |
| 输入 | 短词（如「累」） | 一段对话语境（1–2000 字符） |
| 方法 | 关键词 / 标签确定性打分 | 语境小模型（25 类 softmax） |
| 输出 | 分数降序候选 | `decision` + `none_score` + top-k 概率 |
| 适合 | 明确关键词、要可解释 | 「该不该发、发哪张」的粗筛 |
| 不联网 | 是 | 是 |

调用示例（真实输出；模型为合成语料训练，示例不保证语义最优）：

```json
{"action":"suggest","arguments":{"context":"用户：谢谢你一直帮我改代码，太靠谱了","limit":3}}
```
```json
{"decision":"emote","none_score":0.04833,
 "predictions":[
   {"name":"23-比心","label":"比心","score":0.188186,"file":"emotes/23-比心.png"},
   {"name":"22-扶额","label":"扶额","score":0.072398,"file":"emotes/22-扶额.png"},
   {"name":"07-专注","label":"专注","score":0.061816,"file":"emotes/07-专注.png"}]}
```
```json
{"action":"suggest","arguments":{"context":"用户：部署又失败了，报错是 connection reset，帮我看下日志"}}
```
```json
{"decision":"none","none_score":0.35938,"predictions":[],"hint":"该语境不适合发表情"}
```

## 设置项（GQY 设置页）

本插件在 `manifest.json` 的 `settings` 里声明两项设置，GQY WebUI 的插件页会据此渲染设置表单；改设置**不需要改插件目录**，在线安装的只读版本也能配置：

| key | 类型 | 默认 | 作用 |
| --- | --- | --- | --- |
| `auto_emote` | boolean | `true` | 回复后自动发表情开关。关闭后 GQY 在回复结束时**不再**自动发；`suggest` 动作仍可手动调用。 |
| `min_score` | number（0–1） | `0` | 自动发的最低置信度：只有模型 `decision="emote"` 且 top1 表情分数 **≥ 该值**时才自动发；0 表示只要模型判断该发就发。 |

- **存储**：`$GQY_HOME/plugin-settings/emote-pack-default.json`（0600，GQY 管理）。设置值**永不写进插件目录**，所以安装版只读也能改设置；卸载 / 删除插件会一并清理设置。
- **读写**：`GET /api/plugins/emote-pack-default/settings` 返回 `schema / values / effective / source / read_only`；`PUT` 同路径保存，按 schema 严格校验（类型不符 / 未知键 / 选项越界拒绝，`null` 恢复默认）；GQY WebUI 插件页提供同一份设置表单。
- **谁消费设置**：`auto_emote` / `min_score` 由 GQY 核心读取与执行；插件进程不消费设置，`suggest` 只负责返回建议。
- **自动发表情的行为**：每次 `/api/chat` 回复结束、插件就绪且 `auto_emote=true`、模型 `decision="emote"`、top1 分数 ≥ `min_score` 时，GQY 会把 `emote` 事件追加到 SSE 流，前端据此渲染一张表情图；插件卸载或关闭 `auto_emote` 即停止。

## 安装方式

GQY 按 `$GQY_HOME/plugins/<id>/manifest.json` 发现插件，约定见 [`GQY/docs/designs/04-插件运行时.md`](../GQY/docs/designs/04-插件运行时.md) 的 P3（manifest 字段）与 P9（安装 / 扫描）。`$GQY_HOME` 默认是 `~/.gqy`，服务默认监听 `127.0.0.1:5173`。

把本仓库部署成（目录名必须是 plugin id，不是仓库名）：

```
$GQY_HOME/plugins/emote-pack-default/
  manifest.json      # 本仓库自带，直接使用
  index.js           # 零依赖 stdio JSON-RPC 服务
  package.json
  data/assets.json   # 唯一资产索引，只读
  data/emote-model.json  # 语境模型（训练产物，只读）
  emotes/            # 24 张 512×512 PNG
  motions/           # 4 个 GIF
  docs/
  README.md
```

- 自检（装之前也能跑）：`node --check index.js && npm test`，走 manifest/资产校验 + 语境模型校验（结构 + Python↔JS parity + suggest 动作级）+ initialize → list → show → match → suggest → shutdown 全链路。零依赖，只需 Node ≥18。
- GQY WebUI：插件页 → 加载 / 卸载、设置（`auto_emote` / `min_score`）、删除；设置与加载也可直接走 API（见下）。
- 设置 API：`GET|PUT /api/plugins/emote-pack-default/settings`（`GET` 返回 schema/values/effective；`PUT` 保存，`null` 恢复默认）。
- 加载：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/load`
- 调试调用：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/call -H 'Content-Type: application/json' -d '{"action":"match","arguments":{"query":"累"}}'`
- 卸载：`curl -X POST http://127.0.0.1:5173/api/plugins/emote-pack-default/unload`（幂等）。
- 管理接口受 02 R8 鉴权：设了 `GQY_AUTH_TOKEN` 时请求要带 token。
- 安装插件 = 信任它：GQY 不设运行时权限墙（见 `GQY/docs/designs/11-安全与沙盒.md` SEC5），只从可信来源安装。

## 工具名

加载成功后四个动作注册成外部工具（04 P11）：`emote-pack-default__list`、`emote-pack-default__show`、`emote-pack-default__match`、`emote-pack-default__suggest`。

默认不注入模型：外部工具只在 `GQY_ENABLE_EXTERNAL=1` 或运行时开关开启时进 agent 的工具列表；HTTP `POST /api/plugins/emote-pack-default/call` 始终可单独调试。

## 与主仓库的关系

- 本仓库与 `GQY/` 主仓库地位平等，都在 GQYTeam 组织工作区里（见 org 根 `README.md` 的仓库地图）。
- 本仓库依赖 GQY 的插件运行时与 manifest 约定，但不 import GQY 的代码。
- 跨仓库决定只写一份，放在主仓库 `GQY/docs/designs/`；本仓库只引用，不复制。

## 不做

- **不写文件**：只读 `emotes/`、`motions/`、`data/assets.json`、`data/emote-model.json`；不上传、不生成、不改名、不删资产。
- **不联网**：`match` 全在本地按标签打分，`suggest` 全在本机小模型推理，不调外部服务、不下载模型。
- **不自己发图**：插件只提供动作与设置声明；`suggest` 只回建议（`decision` + `none_score` + top-k）。「回复后自动发表情」由 GQY 核心按 `auto_emote` / `min_score` 决定并走 SSE `emote` 事件，插件本身不推送图片。
- **不转码**：GIF 与 PNG 都是成品；运行时不做格式转换、不缩放、不压缩。
- **不导出微信包**：不做表情包导出 / 打包；「微信 240」只是 `motions/` 里已有的一个 240×240 GIF。

## 许可与署名

本仓库按 **PolyForm Noncommercial License 1.0.0** 授权，全文见 [`LICENSE`](LICENSE)。

- **禁止商用。** 生产、部署、售卖、SaaS、内置于商业产品等任何商业目的的使用，都不在授权范围内。
- **必须署名。** fork、二次开发、摘取代码 / 文档 / 设计，都必须标注来源与原作者。
- **保留通知。** 分发任何副本（含修改后的）必须带上 `LICENSE`、[`NOTICE`](NOTICE) 与 `LICENSE` 顶部的 `Required Notice` 原文。

衍生项目的 `README.md`（或同等声明文件）里原样写清这四项：

```text
来源项目：GQYTeam/gqy-plugins-EmotePack-default
项目地址：https://github.com/GQYTeam/gqy-plugins-EmotePack-default
原作者：GQYTeam
许可证：PolyForm Noncommercial License 1.0.0
```

完整条款与示例见 [`NOTICE`](NOTICE)；第三方代码 / 素材 / 参考项目的许可与出处记在 `docs/third-party/`，各自许可优先于本仓库许可。
