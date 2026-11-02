# 语境模型：格式与验收契约

本文件锁定「语境 → 表情」小模型的**数据切分、权重格式、Python/JS 推理对齐规则与验收阈值**。训练、插件集成、独立验收都按它执行；改格式需要 Lead 同意。

**工作区约定（重要）**：仓库工作区会被并行会话做标准化清理（可能 `git clean`/`checkout`）。所有未提交的在制品统一放在影子工作区 `/tmp/emote-build/`（= 本仓库副本）；Lead 负责把里程碑同步回仓库并提交。本文路径均相对影子工作区。

## 0. 设备预算（用户约束）

本模型刻意做小：目标在 Apple M2 / 16GB 上 **纯 CPU、秒级训练、内存 <300MB、不占 GPU**；模型 JSON ≤600KB；单次推理 <10ms。语料规模 2400 条左右（4 位作者 × 600）+ 独立验收集 280 条；不要为了指标盲目加数据或加参数。

## 1. 目标与形态

- 任务：文本语境（最近 1–3 轮对话）→ 25 类（24 张表情 + `none`）。
- 训练：本地 numpy，**不联网、不用真实用户聊天记录**；语料为合成标注（见 `annotation-guide.md`）。
- 推理：插件内零依赖 JS（`index.js`），加载 `data/emote-model.json`，不引第三方库、不调外部服务。

## 2. 数据与切分

- 原始语料：`train/data/raw/authored-*.jsonl`（训练用，约 2400 条）+ `train/data/raw/holdout.jsonl`（280 条，**仅验收用，禁止进入训练/调参**）。
- 切分：`train/prepare.py` 合并训练用 raw → 校验 → 去重（text 完全一致视为重复）→ 按 label 分层随机切分 **train 88% / val 12%**（固定 seed=20261102）。
- 输出：`train/data/train.jsonl`、`train/data/val.jsonl`（不提交）；`train/data/stats.json`（计数、长度分布、去重数）。
- **防泄漏**：holdout 的 text 不得出现在 train/val；验收时断言（完全一致 + 近似重复「去空白后相似度 ≥0.9」）。

## 3. 模型 JSON 格式（`data/emote-model.json`）

```json
{
  "schema": 1,
  "task": "context-emote-clf",
  "trained_at": "2026-11-02",
  "dataset": {"train": 2112, "val": 288, "holdout": 280, "seed": 20261102, "source": "synthetic-authored-v1"},
  "classes": ["01-平静", "02-浅笑", "…", "24-托腮", "none"],
  "input": {
    "normalize": "nfkc-lower-strip-all-whitespace",
    "max_chars": 120,
    "ngram_order": [1, 2, 3],
    "vocab": ["…"],
    "feature_value": "binary"
  },
  "model": {
    "type": "mlp-tanh-softmax",
    "hidden": 32,
    "w1": [[0.01, -0.02]],
    "b1": [0.0],
    "w2": [[0.03]],
    "b2": [0.0]
  },
  "metrics": {
    "val_acc1": 0.0, "val_acc3": 0.0, "train_acc1": 0.0,
    "none_precision": 0.0, "none_recall": 0.0,
    "holdout_acc1": 0.0, "holdout_acc3": 0.0, "holdout_none_f1": 0.0,
    "baseline_acc1": 0.0, "baseline_acc3": 0.0
  },
  "parity": [{"text": "语境文本", "probs": [0.1, 0.0]}]
}
```

约束（**预算硬上限**）：

- `classes` 长度 25，顺序固定：24 个 `data/assets.json` 的 `emotes[].name`（按清单顺序）再 `none`。
- `vocab` 长度 **≤ 1200**，不允许空串，按特征下标顺序排列；`hidden` **= 32**。
- `w1` 形状 `[len(vocab)][hidden]`，`w2` 形状 `[hidden][25]`，`b1` 长 `hidden`，`b2` 长 25。
- 数值保留 ≥4 位小数（推荐 5 位），不得出现 NaN/Infinity；文件 **≤600KB**。
- `metrics.holdout_*` 与 `baseline_*` 由 `train/eval.py` 在 holdout 上计算后写入。
- `parity` ≥ 8 条（覆盖不同长度、多轮、none），`probs` 为 softmax 全 25 维、和 ≈ 1。

## 4. Python ↔ JS 推理对齐（必须逐字一致）

1. `normalize`：Unicode NFKC → 全部转小写 → 删除所有空白字符（空格、制表、换行、全角空格）。
2. 取**末尾** `max_chars=120` 个 Unicode 码点（`[...string]` / Python 迭代语义），超长只保留尾部。
3. 从截断后的串按字符起点取 1-gram、2-gram、3-gram（越界只取存在的）组成集合；**二元特征**（出现=1，不计数）。
4. 只保留 `vocab` 中存在的 gram；特征向量按 `vocab` 下标置 1。
5. `h = tanh(x · w1 + b1)`（x 为 0/1 稀疏，等价于命中行求和 + b1）。
6. `logits = h · w2 + b2`；`softmax`（减 max 后 exp、归一化）。
7. 决策：`none` 概率最高 → `decision="none"`；否则 top1 表情 → `decision="emote"`。`limit` 只影响返回条数，不影响决策。

容差：两边 argmax 必须 100% 一致；同输入概率最大绝对误差 ≤1e-3。

## 5. 插件动作契约（`suggest`）

`manifest.json` 增加动作 `suggest`：

- 参数：`{context: string（必填，1–2000 字符）, limit?: integer（1–5，默认 3）}`。
- 返回（`result.content` 为 JSON 字符串）：
  ```json
  {"decision":"emote","none_score":0.08,
   "predictions":[{"name":"05-困倦","label":"困倦","score":0.71,"file":"emotes/05-困倦.png"}]}
  ```
  `decision="none"` 时 `predictions` 为空数组，并带 `hint`：「该语境不适合发表情」。
- 错误：`context` 缺失/非字符串/空 → `-32602`；模型文件缺失或损坏 → `-32000`（带清晰原因），进程不崩。
- `list` 增加只读字段 `model`：`{schema, trained_at, classes:25, metrics:{val_acc1,holdout_acc1,none_recall}, size_bytes}`。
- `match`（关键词匹配）保持不变，防止行为回退。

## 6. 验收阈值（不达标先自查，不盲目加数据/加参数）

在 `holdout.jsonl`（独立作者、从未进入训练/调参）上：

1. `holdout_acc1 ≥ 0.55`（目标 0.60），`holdout_acc3 ≥ 0.80`。
2. `holdout` 上 `none` 类 F1 ≥ 0.55。
3. 模型 `holdout_acc1` 比「关键词 match 基线」高 ≥ 8 个百分点（基线由 `train/eval.py` 实现并写入 metrics）。
4. Python ↔ JS parity：argmax 100% 一致，概率误差 ≤1e-3。
5. 无网络请求；语料无真实用户聊天记录；holdout 与 train/val 无泄漏。
6. 插件侧：`npm test` 全绿；真实 gqy-server 加载后 `/call suggest` 返回合理结果；`manifest` 校验通过。

## 7. 复现命令（在影子工作区 /tmp/emote-build 执行）

```bash
PY=/Users/mac/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
cd /tmp/emote-build
$PY train/prepare.py          # 合并 + 校验 + 去重 + 切分 → train/data/{train,val}.jsonl、stats.json
$PY train/train.py            # 训练 + 选模 + 写 data/emote-model.json（含 parity）
$PY train/eval.py             # val/holdout 评估 + 基线对比 + 写 train/report.md 与 metrics
```

`train/report.md` 必须包含：数据统计、超参、训练曲线（每 epoch loss/val_acc1）、val/holdout 指标、混淆矩阵 top 错误对、基线对比、**资源实测（训练耗时、峰值内存、模型体积、推理延迟）**、结论与局限。
