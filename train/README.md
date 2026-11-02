# 语境 → 表情 模型：训练管线

本目录是「表情包插件 · 语境模型」的本地训练管线：把合成标注的对话语境训练成一个
**numpy 单隐层 MLP**，导出插件可直接加载的 `data/emote-model.json`（零依赖 JS 推理）。

格式与验收契约在 [`model-format.md`](model-format.md)，标注口径在
[`annotation-guide.md`](annotation-guide.md)；两份文档是唯一事实源，改口径先改文档。

## 1. 依赖与环境

- Python 3.12（本机用 DSH 自带解释器）+ **numpy**（2.3.5）。无 sklearn / torch，零第三方 ML
  框架，不联网。
- 纯 CPU、秒级训练、峰值内存 <300 MB、不占 GPU（设备预算见 `model-format.md` 第 0 节）。
- 插件侧推理是零依赖 JS（`index.js` 的 `suggest` 动作），只加载模型 JSON，不引第三方库。

```bash
PY=/Users/mac/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
$PY -c "import numpy; print(numpy.__version__)"   # 2.3.5
```

## 2. 复现步骤

工作区约定：未提交的在制品放在影子工作区 `/tmp/emote-build/`（= 仓库副本），
由 Lead 把里程碑同步回仓库；下面命令在该工作区根目录执行。

```bash
cd /tmp/emote-build                           # 或在仓库 gqy-plugins-EmotePack-default/ 内

# 1) 合并/校验/去重/分层切分 + 生成 all.jsonl 与 4 个交叉验证折
$PY train/prepare.py
#   → train/data/{all.jsonl,train.jsonl,val.jsonl,stats.json,folds/fold-*.jsonl}

# 2) 4 折交叉验证选模 + 全量重训 + 导出（含 parity）
$PY train/train.py
#   → data/emote-model.json、train/checkpoints/{train-summary.json,best.npz,history.jsonl}

# 3) holdout 最终评估 + 关键词基线 + 泄漏检查 + 回写 metrics + 报告
$PY train/eval.py
#   → 更新 data/emote-model.json 的 metrics，生成 train/report.md
```

固定 `seed=20261102`，切分/词表/初始化/增强/多 seed 集成全部确定性可复现：同一配置重复训练
得到**完全相同的权重、vocab 与 parity**（`metrics` 里的 holdout/baseline 字段由 `eval.py`
回写，因此「训练直出的 JSON」与「评估后的 JSON」只在 metrics 上不同）。
脚本参数可覆盖默认路径（`--raw-dir/--data-dir/--out/--holdout/--report`），便于用 `/tmp`
下 mock 数据自测；`hidden` 固定 32、`vocab ≤1200`、模型 ≤600KB 是**预算硬上限**（脚本拒绝越界）。

## 3. 管线构成

| 文件 | 作用 |
| --- | --- |
| `model.py` | 共享实现：`normalize`（NFKC→lower→删所有空白）、末尾 120 码点截断、char 1/2/3-gram、binary 特征、MLP(tanh+softmax) 前向、softmax CE（支持 label smoothing）、反向传播、Adam、类别权重、评估指标、模型 JSON 契约校验 |
| `prepare.py` | 合并 `train/data/raw/authored-*.jsonl` → 逐行校验 → text 去重 → 按 label 分层 88/12 → 写 `all.jsonl`（去重全量）、`train/val.jsonl`、`stats.json`，并生成 **4 折交叉验证折**（默认「开头短语分组」：同类内开头相同的文本整组留出，模拟没见过的句式；`--fold-mode similarity` 保留 3-gram 聚类方式） |
| `train.py` | chi2（按 25 类取最大）选 ≤1200 个 1-3gram（每类强制覆盖 3 个）+ 4 折 CV 选模（指标 = 4 折平均 acc1 + 4 折平均 none F1，patience 早停）+ **在线数据增强**（每个 epoch 每样本采样 1 个增强版本：去「用户/助手」前缀、去句末标点、10% 删 1 字、10% 插入填充词）+ **全量重训**（train+val，多 seed 权重平均）+ 导出模型 JSON（5 位小数、parity ≥8、资源实测） |
| `eval.py` | holdout 最终评估（acc1/acc3/macro F1/按类 P-R-F1/混淆 top）、关键词 match 基线、holdout 泄漏检查（完全 + 近似重复）、Python 侧 parity 自检、推理延迟实测，回写 `metrics` 并生成 `report.md`；`metrics.val_*` 写 CV 口径（不是同作者 val） |
| `report.md` | 训练报告（数据、超参、CV 曲线、迭代对比、4 折明细、三口径对照、holdout、基线、泄漏、parity、资源实测、作者折结构性上限、结论与局限），由 `eval.py` 生成 |
| `data/emote-model.json` | 训练产物，插件运行时加载（相对插件根目录） |

## 4. 数据与选模口径

- 训练语料：`authored-{a,b,c,d}-1.jsonl` 各 600 条 = **2400 条**（去重后 2399），
  每作者 6 类 × 90 + none 60；`none` 共 240 条（10%）。
- 验收集：`holdout.jsonl` 280 条（24 类各 10 + none 40，独立第 5 位作者），
  **只由 `eval.py` 做最终评估**，`prepare.py` 从文件发现阶段就排除它。
- **为什么不用同作者随机 val 选模**：val 与 train 同作者、同句式分布，指标会高估泛化
  （实测同作者 val acc1 0.74 vs holdout 0.49）。
- **为什么不用「作者留一折」**：4 位作者负责的标签**互不相交**（A 只写 01-06…），
  留出某作者时该折 6 个类在训练中完全缺席，只能猜 `none`，结构性 acc1 上限 **0.100**
  （`report.md` 第 7 节有逐文件证据）。所以作者折只能作诊断，不能选模。
- **实际口径：4 折「开头短语分组」折**。先验证过 3-gram 聚类折：本语料近重复极少
  （类内 pair Jaccard p99 ≈ 0.09），聚类折退化成随机折；改用开头短语分组后，验证折里
  出现训练折没见过的开头/句式，CV acc1 ≈ 0.70，比随机切分的 val 低 3–4 个点，更接近
  跨作者场景（但仍比 holdout 高约 0.21，差距是**标注口径差异**，见局限）。
- 选模指标 = 4 折平均 acc1 + 4 折平均 none F1；选定后用全部 2399 条重训最终模型，
  holdout 只评估一次。

## 5. 模型卡（2026-11-02 r2）

| 项 | 值 |
| --- | --- |
| 任务 | 文本语境（最近 1–3 轮）→ 25 类（24 表情 + none） |
| 结构 | char 1/2/3-gram（binary，vocab=1200）→ 全连接 tanh（hidden=32）→ softmax（25） |
| 参数 | 1200×32 + 32 + 32×25 + 25 ≈ 39.3k |
| 体积 | 347,899 字节（339.7 KB，预算 ≤600KB） |
| 训练 | 纯 numpy/CPU：4 折 CV + 全量重训（3 seed 平均）共 **9.1s**，峰值 RSS **177 MB** |
| 推理 | Python 参考全链路 p50 **0.029ms** / p95 **0.072ms**（预算 <10ms）；插件侧 JS 见 `test/model.mjs` |
| 指标 | 4 折 CV acc1 **0.699** / acc3 **0.870** / none F1 **0.689**；holdout acc1 **0.493** / acc3 **0.664** / acc5 **0.761** / none F1 **0.615**；关键词基线 holdout acc1 0.146（模型 **+34.6 点**）；置信度-覆盖率：30%→0.821、50%→0.664、70%→**0.602**、90%→0.520 |
| 适用 | 日常闲聊/技术协助语境下「该不该发表情、发哪张」的粗筛；给出 top-3 候选与 none 判定 |
| 不适用 | 敏感/医疗/法律/财务语境（应 none）、反讽/隐喻/混合情绪、超长上下文（只取末尾 120 码点）、需要高可解释性/合规的场景 |

`metrics` 语义：`val_*` = 4 折 CV 均值（选模口径）；`train_acc1` = 全量训练集拟合度；
`none_precision/recall` = CV 均值；`holdout_*` 与 `baseline_*` 由 `eval.py` 在 holdout 上算一次后写入；
`holdout_acc5`、`coverage_70_top1_precision` 是只读补充评估字段（同样由 eval 写入，不参与推理/调参）。
模型 JSON 里的指标用**导出（5 位小数取整后）权重**重新前向计算，与插件加载的数值一致。

**验收状态（不达标不隐瞒）**：holdout `none F1` 0.615（≥0.55 PASS）、超基线 34.6 点（≥8 PASS）；
`acc1` 0.493（目标 0.55 **FAIL**）、`acc3` 0.664（目标 0.80 **FAIL**）。阈值由 Lead/用户决定，
模型侧已按 CV 把能调的（正则/词表/类别权重/增强/全量重训）都调过一轮，详见 `report.md`。

## 6. 已知局限（数据瓶颈优先）

1. **标注口径差异是大头**：每类 90 条全部来自同一位作者，holdout 由独立第 5 位作者写同一批
   标签；holdout 上 8 个「弱信号/近义类」F1 ≈ 0（`09-打哈欠`、`10-得意`、`04-皱眉`、`07-专注`、
   `16-无语`、`01-平静`、`08-喝茶`、`11-思考`），而词面线索强的类（`05-困倦`、`12`、`17`、`19`、
   `20`、`21`）F1 0.78–0.89。这说明模型没学到「跨作者的判决口径」，不是简单调参能补的。
2. **容量硬约束**：≤1200 个 char 1-3gram + 单隐层 32 维是设备预算下的刻意选择；长语境只取
   末尾 120 码点，前文信息会丢；对反讽/省略/混合情绪判别力有限。
3. **近义类边界**：`04-皱眉`/`16-无语`/`22-扶额`、`05-困倦`/`09-打哈欠`/`24-托腮`、
   `02-浅笑`/`18-大笑`、`15-害羞`/`23-比心` 仍是主要混淆来源（见 `report.md` 混淆 top）。
4. **holdout 只有一次机会**：未用于调参；迭代只依据 4 折 CV 与参考 val。
5. **不联网/不引第三方**：语义理解上限受 char n-gram 限制，无法做中文分词级组合泛化。

## 7. 变更记录

- 2026-11-02 r1：首版管线（同作者 88/12 选模）→ holdout acc1 0.471。
- 2026-11-02 r2：按 Lead 迭代指令改为 **4 折交叉验证选模**（作者折不可用 → 开头短语分组折），
  正则加强（l2 3e-3、dropout 0.25、label smoothing 0.05）、none 权重 1.5、在线数据增强、
  全量 2399 条重训 + 3 seed 权重平均 → holdout acc1 0.493 / none F1 0.615；资源与预算全部 PASS，
  acc1/acc3 仍未达阈值，按约定上报由 Lead/用户决策。
- 2026-11-02 r2soup（收尾）：CV 双轨评估 raw vs EMA（5 seed、EMA decay 0.9），
  EMA 最优 CV 分 1.278 < raw 1.387 → **按约定否决 EMA、保留 r2 权重**；
  纯评估增强：补 holdout acc5=0.761 与置信度-覆盖率曲线（30/50/70/90%），
  新增只读 metrics 字段 `holdout_acc5`、`coverage_70_top1_precision`，模型仍 339.8KB。
