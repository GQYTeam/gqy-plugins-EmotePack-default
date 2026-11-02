# 表情包语境模型 · 模型卡（MODEL CARD）

「语境 → 表情」小模型：给一段对话语境，判断助手该不该发一张表情、发哪一张。
**模型文件**：[`data/emote-model.json`](data/emote-model.json)（347,959 字节，sha256 `36885fe34c8ea2649b9ee976e39e060dba90128bbc308614c909da1b497bac9e`）
**版本**：1.2.0（随插件发布）
**许可**：PolyForm Noncommercial License 1.0.0（禁止商业用途；fork / 二改 / 借鉴必须标注来源与署名，见 [NOTICE](NOTICE)）

---

## 1. 它是什么

- 任务：文本语境（最近 1–3 轮对话，取本轮用户消息）→ 25 类 softmax：**24 张表情 + `none`（不该发）**。
- 结构：char 1/2/3-gram → binary 特征 → 1200 维词表 → 单隐层 32 维 tanh → softmax；**39,257 个参数**。
- 体积：**347,959 B（≈339.8 KiB）**；纯 CPU 推理 **0.03–0.06 ms/次**（M2），Python 参考实现 p50 0.033 ms。
- 依赖：numpy（训练）/ 零依赖 JS（插件内推理）；不联网、不调外部模型。
- 训练：本地 numpy，4 折模板交叉验证选模；训练总耗时实测 **9.07 s**、峰值内存 177 MB（Apple M2 CPU）。

## 2. 数据

- **全合成标注**：4 位作者按 [`train/annotation-guide.md`](train/annotation-guide.md) 各写 600 条（24 类 ×90 + `none` 60），共 2,400 条。
- **独立验收集**：第 5 位作者另写 280 条（24 类 ×10 + `none` 40），只用于最终验收，不参与训练/调参。
- **不含任何真实用户聊天记录**；无个人信息（手机号/邮箱/地址/身份证等已检查）。
- 语料与切分脚本、训练报告全部开源在本仓库 `train/`（可复现）。

## 3. 指标（诚实口径，三种分开）

| 口径 | 说明 | acc1 | acc3 | none F1 |
| --- | --- | --- | --- | --- |
| 4 折模板 CV | 选模口径（同作者、留出未见句式） | 0.69863 | 0.86995 | 0.68857 |
| 同作者 val | 参考，偏乐观（已并入全量重训） | 0.7396 | 0.9028 | 0.6364 |
| **holdout（独立作者）** | 最终验收，只评一次 | **0.49286** | **0.66429** | **0.61538** |

- holdout **acc5 = 0.76071**（top-5 命中）；关键词基线 acc1 = 0.14643 → 模型高 **+34.6 个百分点**。
- 置信度覆盖（按 top1 概率门控）：覆盖 30% → 精度 **0.8214**；50% → 0.6643；70% → **0.6020**；90% → 0.5198。
- 已知局限：4 位训练作者标签互不相交、句式自成一派，换第 5 位作者的 holdout 存在分布偏移；8 个近义/兜底类（01/04/07/08/09/10/11/16）holdout 单类 F1 < 0.2。**定位：粗筛 / top-3 建议，不是最终裁决；建议按置信度门控使用。**

## 4. 怎么用

### 作为 GQY 插件（推荐）

```bash
# 安装 CLI（一次）
go install github.com/GQYTeam/gqy-plugins/cmd/gqy-plugin@latest

# 安装插件（自动下载本模型并放进 $GQY_HOME/plugins/emote-pack-default/）
"$(go env GOPATH)/bin/gqy-plugin" install emote-pack-default --server http://127.0.0.1:5173 --load
```

插件对外动作 `suggest`：参数 `{context: string(1–2000), limit?: 1–5}`，返回
`{"decision":"emote"|"none","none_score":0.08,"predictions":[{"name":"23-比心","label":"比心","score":0.20,"file":"emotes/23-比心.png"}]}`。
GQY 核心在每次 `/api/chat` 回复结束后按插件设置 `auto_emote` / `min_score` 自动调用。

### 只下载模型文件

- 仓库直链（raw）：`https://raw.githubusercontent.com/GQYTeam/gqy-plugins-EmotePack-default/main/data/emote-model.json`
- GitHub Release 资产：`emote-model-1.2.0.json`（同一文件，带 SHA256SUMS）
- 校验：`shasum -a 256 emote-model.json` 应等于 `36885fe34c8ea2649b9ee976e39e060dba90128bbc308614c909da1b497bac9e`

### 自己写推理（Python 参考实现）

`train/model.py` 里是完整参考实现（normalize → 末尾 120 码点 → 1/2/3-gram binary → tanh MLP → softmax），
`train/model-format.md` 第 4 节是 Python/JS 必须逐字对齐的推理规则。插件内 JS 实现在 `index.js`，
`test/model.mjs` 用 12 条 parity 固化了 Python↔JS 一致性（argmax 100%、概率误差 ≤5e-6）。

> 备注：训练用的是 numpy，推理端不依赖 numpy；参考实现也可以直接读 JSON 权重用任意语言重写。

## 5. 复现

```bash
PY=/Users/mac/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
$PY train/prepare.py   # 语料校验 + 去重 + 分层切分 + 4 折
$PY train/train.py     # CV 选模 + 全量重训 → data/emote-model.json（含 parity）
$PY train/eval.py      # holdout 评估 + 基线 + 泄漏检查 → train/report.md
```

细节见 [`train/README.md`](train/README.md)、[`train/report.md`](train/report.md)。

## 6. 引用与署名

使用、修改或分发本模型请保留 `NOTICE` 与 `LICENSE`，并注明来源：
`gqy-plugins-EmotePack-default · emote-model v1.2.0 · https://github.com/GQYTeam/gqy-plugins-EmotePack-default`
