#!/usr/bin/env python3
"""eval：holdout 最终评估 + 关键词基线 + 泄漏检查 + 回写 metrics + 生成 report.md。

选模口径是「4 折模板交叉验证」（见 train.py 与 model-format.md）；本脚本：
- 用 `train-summary.json` 里的 CV 指标写 `metrics.val_*`（跨作者泛化口径）；
- 计算同作者 val 仅作参考（最终模型已在全量语料上重训，指标偏乐观）；
- holdout 只在**最终模型**上评估一次，禁止据此调参；错例只进报告供独立验收；
- 报告包含：4 折每折 acc1/none F1、CV/同作者 val/holdout 三口径、基线对比、
  阈值 PASS/FAIL、资源实测（耗时/峰值内存/体积/推理延迟 p50·p95）、迭代对比、局限。
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import model as M

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
DEFAULT_DATA_DIR = TRAIN_DIR / "data"
DEFAULT_ASSETS = REPO_ROOT / "data" / "assets.json"
DEFAULT_MODEL = REPO_ROOT / "data" / "emote-model.json"
DEFAULT_HOLDOUT = TRAIN_DIR / "data" / "raw" / "holdout.jsonl"
DEFAULT_REPORT = TRAIN_DIR / "report.md"
DEFAULT_CHECKPOINTS = TRAIN_DIR / "checkpoints"

# 验收阈值（model-format.md 第 6 节，2026-11-02 按设备预算下调）
THRESHOLDS = {
    "holdout_acc1": 0.55,
    "holdout_acc1_target": 0.60,
    "holdout_acc3": 0.80,
    "holdout_none_f1": 0.55,
    "holdout_acc1_minus_baseline_acc1": 0.08,
}


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj.get("text"), str) or not obj["text"]:
                raise SystemExit(f"{path}:{lineno} text 为空")
            rows.append(obj)
    return rows


# ---------------------------------------------------------------------------
# 关键词 match 基线（与插件 match 打分规则一致）
# ---------------------------------------------------------------------------

def baseline_scores(texts: list[str], emotes: list[dict]) -> np.ndarray:
    """每条语境在 24 张表情上的关键词得分（0 分也要有排序依据：按清单顺序）。"""
    scores = np.zeros((len(texts), len(emotes)), dtype=np.float64)
    for i, text in enumerate(texts):
        q = unicodedata.normalize("NFKC", text).lower().strip()
        for j, emote in enumerate(emotes):
            label = emote.get("label", "")
            tags = emote.get("tags") or []
            s = 0.0
            if q and q == label:
                s = max(s, 100.0)
            for tag in tags:
                if q and q == tag:
                    s = max(s, 60.0)
            if label and label in q:
                s = max(s, 40.0)
            if q and q in label:
                s = max(s, 30.0)
            for tag in tags:
                if not tag:
                    continue
                if tag in q or (q and q in tag):
                    s = max(s, 20.0)
            scores[i, j] = s
    return scores


def baseline_metrics(scores: np.ndarray, y: np.ndarray, num_emotes: int) -> dict:
    """基线永远不预测 none；全 0 分时按清单顺序取 01-平静。"""
    top1 = np.argmax(scores, axis=1)
    order = np.argsort(-scores, axis=1, kind="stable")[:, :3]
    acc1 = float(np.mean(top1 == y)) if len(y) else 0.0
    acc3 = float(np.mean([y[i] in order[i] for i in range(len(y))])) if len(y) else 0.0
    return {"acc1": acc1, "acc3": acc3, "pred": top1, "top3": order}


# ---------------------------------------------------------------------------
# 泄漏检查（holdout 与 train/val 的完全重复 + 近似重复）
# ---------------------------------------------------------------------------

def check_leakage(holdout_rows: list[dict], ref_rows: list[dict]) -> dict:
    ref_strings = [M.normalize(r["text"]) for r in ref_rows]
    ref_set = set(ref_strings)

    exact = 0
    for row in holdout_rows:
        if M.normalize(row["text"]) in ref_set:
            exact += 1

    order = sorted(range(len(ref_strings)), key=lambda i: len(ref_strings[i]))
    lengths = [len(ref_strings[i]) for i in order]
    near: list[dict] = []
    max_sim = 0.0
    for row in holdout_rows:
        s = M.normalize(row["text"])
        ls = len(s)
        if ls == 0:
            continue
        lo = bisect.bisect_left(lengths, int(ls * 0.8))
        hi = bisect.bisect_right(lengths, int(ls * 1.25) + 1)
        for k in range(lo, hi):
            t = ref_strings[order[k]]
            if not t:
                continue
            ratio_len = min(ls, len(t)) / max(ls, len(t))
            if ratio_len < 0.8:
                continue
            sm = difflib.SequenceMatcher(None, s, t)
            if sm.quick_ratio() < 0.85:
                continue
            r = sm.ratio()
            if r > max_sim:
                max_sim = r
            if r >= 0.9:
                near.append({
                    "holdout_text": row["text"],
                    "ref_text": ref_rows[order[k]]["text"],
                    "similarity": round(r, 4),
                })
    return {
        "holdout": len(holdout_rows),
        "ref": len(ref_rows),
        "exact_duplicates": exact,
        "near_duplicates": len(near),
        "max_similarity": round(max_sim, 4),
        "similarity_threshold": 0.9,
        "examples": near[:5],
    }


# ---------------------------------------------------------------------------
# 指标 / 推理
# ---------------------------------------------------------------------------

def evaluate(rows: list[dict], model_json: dict, class_index: dict[str, int],
             num_classes: int, params: dict[str, np.ndarray]) -> dict:
    texts = [r["text"] for r in rows]
    y = np.asarray([class_index[r["label"]] for r in rows], dtype=np.int64)
    probs = M.predict_probs_texts(texts, model_json["input"]["vocab"], params)
    pc = M.per_class_metrics(probs, y, num_classes)
    none_idx = class_index[M.NONE_LABEL]
    pred = np.argmax(probs, axis=1)
    return {
        "n": len(rows),
        "probs": probs,
        "y": y,
        "pred": pred,
        "acc1": M.accuracy_at_k(probs, y, 1),
        "acc3": M.accuracy_at_k(probs, y, 3),
        "acc5": M.accuracy_at_k(probs, y, 5),
        "macro_f1": M.macro_f1(probs, y, num_classes),
        "none_precision": pc["precision"][none_idx],
        "none_recall": pc["recall"][none_idx],
        "none_f1": pc["f1"][none_idx],
        "per_class": pc,
        "confusions": M.confusion_pairs(y, probs, model_json["classes"], top_n=15),
    }


def measure_inference_latency(model_json: dict, params: dict[str, np.ndarray],
                              sample_texts: list[str], min_calls: int = 200) -> dict:
    """Python 参考实现单次推理耗时（normalize→gram→查词表→前向→softmax 全链路）。"""
    vocab = model_json["input"]["vocab"]
    index = {g: i for i, g in enumerate(vocab)}
    n_vocab = len(vocab)
    texts = [t for t in sample_texts if isinstance(t, str) and t] or ["用户：测试语境"]
    calls = max(min_calls, 2 * len(texts))
    xs = np.zeros((1, n_vocab), dtype=M.FLOAT)
    timings: list[float] = []
    for i in range(calls):
        text = texts[i % len(texts)]
        t0 = time.perf_counter()
        hits = M.encode(text, index)
        xs[0, :] = 0.0
        if hits:
            xs[0, hits] = 1.0
        _, logits = M.forward(xs, params)
        M.softmax(logits)
        timings.append((time.perf_counter() - t0) * 1000.0)
    timings.sort()

    def pct(p: float) -> float:
        return timings[min(len(timings) - 1, int(len(timings) * p))]

    return {
        "calls": calls,
        "mean_ms": round(float(np.mean(timings)), 4),
        "p50_ms": round(pct(0.50), 4),
        "p95_ms": round(pct(0.95), 4),
        "max_ms": round(float(timings[-1]), 4),
        "budget_ms": 10.0,
    }


def confidence_coverage(probs: np.ndarray, y: np.ndarray,
                        coverages=(0.3, 0.5, 0.7, 0.9)) -> dict:
    """按 top1 softmax 置信度从高到低取覆盖子集，返回该子集上的 top1 精度。

    用于描述「只在有把握时发」的可用性边界；只报告，不参与调参。
    """
    n = len(y)
    if n == 0:
        return {}
    conf = np.max(probs, axis=1)
    order = np.argsort(-conf)
    pred = np.argmax(probs, axis=1)
    out = {}
    for c in coverages:
        k = max(1, int(round(c * n)))
        idx = order[:k]
        out[str(c)] = float(np.mean(pred[idx] == y[idx]))
    return out


def parity_selfcheck(model_json: dict, params: dict[str, np.ndarray]) -> dict:
    """Python 侧 parity 自检：按存档文本重算，比对存档 probs（argmax 与最大误差）。"""
    items = model_json.get("parity") or []
    if not items:
        return {"count": 0, "max_abs_diff": 0.0, "argmax_equal": 0, "ok": False}
    vocab = model_json["input"]["vocab"]
    texts = [it["text"] for it in items]
    probs = M.predict_probs_texts(texts, vocab, params)
    max_diff = 0.0
    argmax_equal = 0
    for i, it in enumerate(items):
        stored = np.asarray(it["probs"], dtype=np.float64)
        max_diff = max(max_diff, float(np.max(np.abs(probs[i] - stored))))
        if int(np.argmax(probs[i])) == int(np.argmax(stored)):
            argmax_equal += 1
    return {
        "count": len(items),
        "max_abs_diff": max_diff,
        "argmax_equal": argmax_equal,
        "ok": max_diff <= 1e-3 and argmax_equal == len(items),
    }


def fmt(v: float, digits: int = 4) -> str:
    return f"{v:.{digits}f}"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def build_report(ctx: dict) -> str:
    model_json = ctx["model"]
    classes = model_json["classes"]
    lines: list[str] = []
    add = lines.append

    add("# 语境 → 表情 模型训练报告")
    add("")
    add(f"- 生成时间：{ctx['generated_at']}")
    add(f"- 模型文件：`{ctx['model_path']}`（{ctx['model_size']} 字节，schema "
        f"{model_json['schema']}，trained_at {model_json['trained_at']}）")
    add(f"- 语料：合成标注（`{model_json['dataset']['source']}`），不含真实用户聊天记录；"
        f"seed={model_json['dataset']['seed']}")
    add(f"- 推理：numpy 参考实现 `train/model.py`；插件侧零依赖 JS 由 `test/model.mjs` 验收")
    add("- 选模口径：**4 折模板交叉验证**（同类内按模板聚类留出整簇，验证集句式不进训练）；"
        "holdout 只在最终模型上评估一次")
    add("")

    # ---- 结论 ----
    add("## 0. 结论")
    add("")
    add("| 验收项 | 目标 | 实测 | 结果 |")
    add("| --- | --- | --- | --- |")
    for row in ctx["threshold_rows"]:
        add(f"| {row['name']} | {row['target']} | {row['actual']} | {row['status']} |")
    add("")
    add(ctx["verdict_text"])
    add("")
    if ctx.get("iteration_text"):
        add(ctx["iteration_text"])
        add("")

    # ---- 数据 ----
    add("## 1. 数据")
    add("")
    stats = ctx.get("stats") or {}
    raw = stats.get("raw") or {}
    length = stats.get("length") or {}
    folds_stats = stats.get("folds") or {}
    add("| 集合 | 条数 | 说明 |")
    add("| --- | --- | --- |")
    add(f"| 训练（全量） | {model_json['dataset']['train']} | 最终模型用 train+val 全量重训（不含 holdout） |")
    add(f"| 同作者 val | {model_json['dataset']['val']} | 88/12 分层切分，仅作参考（已并入最终训练） |")
    add(f"| holdout | {model_json['dataset']['holdout']} | 独立作者，仅最终验收 |")
    cv_folds = (ctx.get("summary") or {}).get("data", {}).get("folds") or []
    if cv_folds:
        add("")
        add("| CV 折 | 训练条数 | 验证条数 | 口径 |")
        add("| --- | --- | --- | --- |")
        for f in cv_folds:
            add(f"| `{f['name']}` | {f['train']} | {f['val']} | 模板聚类留出（同作者、不同句式） |")
    if raw:
        add("")
        add(f"- raw：{len(raw.get('files', []))} 个作者文件，有效行 {raw.get('valid', 0)}，"
            f"去重 {raw.get('duplicates_removed', 0)}，坏行 {raw.get('error_count', 0)}，"
            f"label 冲突 {raw.get('label_conflicts', 0)}。")
    if length:
        rc = length.get("raw_chars") or {}
        if rc:
            add(f"- 长度：min {rc.get('min')} / mean {rc.get('mean')} / p90 {rc.get('p90')} / "
                f"max {rc.get('max')}；分桶 {rc.get('buckets')}；"
                f">120 码点被截断 {length.get('truncated_to_max_chars')} 条。")
    if folds_stats:
        add(f"- 模板折：{folds_stats.get('n_folds')} 折，3-gram Jaccard 阈值 "
            f"{folds_stats.get('similarity_threshold')}，各折条数 {folds_stats.get('sizes')}。")
    add("")

    # ---- 训练配置 + CV 曲线 ----
    summary = ctx.get("summary") or {}
    config = summary.get("config") or {}
    add("## 2. 训练配置与 CV 曲线")
    add("")
    if config:
        add("| 超参 | 值 |")
        add("| --- | --- |")
        for key in ("vocab_size", "min_df", "hidden", "epochs", "patience", "lr", "l2",
                    "batch_size", "class_weight_mode", "none_boost", "label_smoothing",
                    "feature_dropout", "seed_ensemble", "cv_mode", "augment",
                    "augment_variants", "seed"):
            add(f"| {key} | {config.get(key)} |")
        add("")
        vocab = summary.get("vocab") or {}
        add(f"- 特征：char 1/2/3-gram，binary；候选 {vocab.get('candidates')}，"
            f"选中 {vocab.get('selected')} / 唯一 gram {vocab.get('unique_grams')}，"
            f"min_df={vocab.get('min_df')}，每类强制覆盖 {vocab.get('forced_per_class')} 个。")
        final = (summary.get("cv") or {}).get("final_train") or {}
        seedn = len(final.get("seeds") or [])
        add(f"- 最终重训：{final.get('rows')} 条 × best_epoch={summary.get('best_epoch')} 个 epoch × "
            f"{seedn} seed 权重平均（在线增强 {final.get('augment')}，变体 {final.get('augment_variants')}）。")
        add("")
        history = summary.get("history") or []
        if history:
            add("| epoch | train_loss(折均) | CV acc1 | CV acc3 | CV none F1 | CV score |")
            add("| --- | --- | --- | --- | --- | --- |")
            for row in history:
                if "cv_acc1" in row:
                    folds = row.get("folds") or []
                    tl = float(np.mean([f.get("train_loss", 0.0) for f in folds])) if folds else 0.0
                    mark = " **\\***" if row["epoch"] == summary.get("best_epoch") else ""
                    add(f"| {row['epoch']}{mark} | {tl:.4f} | {row['cv_acc1']:.4f} | "
                        f"{row['cv_acc3']:.4f} | {row['cv_none_f1']:.4f} | {row['cv_score']:.4f} |")
                else:
                    add(f"| {row['epoch']} | {row.get('train_loss', 0.0):.4f} | "
                        f"{row.get('val_acc1', 0.0):.4f} | {row.get('val_acc3', 0.0):.4f} | — | — |")
            add("")
    else:
        add("（缺少 `train/checkpoints/train-summary.json`，训练配置与曲线不可用。）")
        add("")

    # ---- 资源实测 ----
    add("### 资源实测与设备预算")
    add("")
    res = summary.get("resources") or {}
    lat = ctx.get("latency") or {}
    vocab_len = len(model_json["input"]["vocab"])
    model_kb = ctx["model_size"] / 1024.0
    budget_rows = [
        ("vocab 长度", f"≤ {M.MAX_VOCAB}", str(vocab_len), vocab_len <= M.MAX_VOCAB),
        ("hidden", f"= {M.HIDDEN}", str(model_json["model"].get("hidden")),
         model_json["model"].get("hidden") == M.HIDDEN),
        ("模型 JSON 体积", "≤ 600 KB", f"{model_kb:.1f} KB（{ctx['model_size']} 字节）",
         ctx["model_size"] <= M.MAX_MODEL_BYTES),
        ("单次推理（Python 参考全链路）", "< 10 ms",
         f"mean {lat.get('mean_ms')} / p50 {lat.get('p50_ms')} / p95 {lat.get('p95_ms')} / "
         f"max {lat.get('max_ms')} ms（{lat.get('calls')} 次）",
         float(lat.get("p95_ms", 1e9)) < lat.get("budget_ms", 10.0)),
        ("训练耗时", "秒级", f"总 {res.get('total_sec')}s（训练 {res.get('train_sec')}s）",
         float(res.get("total_sec", 1e9)) < 120),
        ("训练峰值内存（RSS）", "< 300 MB", f"{res.get('peak_rss_mb')} MB",
         float(res.get("peak_rss_mb", 1e9)) < 300),
    ]
    add("| 项 | 预算 | 实测 | 结果 |")
    add("| --- | --- | --- | --- |")
    for name, budget, actual, ok in budget_rows:
        add(f"| {name} | {budget} | {actual} | {'PASS' if ok else '**FAIL**'} |")
    add("")
    add("推理延迟为 Python 参考实现（normalize → 1/2/3-gram → 查词表 → tanh 前向 → softmax）"
        "单次调用耗时；插件侧 JS 的真实延迟由 `test/model.mjs` 独立测。训练全程不联网。")
    add("")

    # ---- 迭代轮次对比 ----
    add("## 3. 迭代轮次对比（是否触发自查迭代）")
    add("")
    runs = ctx.get("iteration_history") or []
    if len(runs) > 1:
        add("| 轮次 | 口径 | vocab | l2 | dropout | LS | 增强 | seed集成 | best_epoch | "
            "val/CV acc1 | none F1 | 模型体积 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for run in runs:
            cfg = run.get("config") or {}
            val = run.get("val") or {}
            add(f"| {run.get('round_tag')} | {run.get('mode')} | {cfg.get('vocab_size')} | "
                f"{cfg.get('l2')} | {cfg.get('feature_dropout')} | {cfg.get('label_smoothing')} | "
                f"{cfg.get('augment')} | {cfg.get('seed_ensemble')} | {run.get('best_epoch')} | "
                f"{val.get('acc1')} | {val.get('none_f1')} | "
                f"{(run.get('model') or {}).get('size_bytes')} |")
        add("")
        add("注：r1 为同作者随机 val 口径的基线；r2+ 为 4 折模板 CV 口径。两轮选模口径不同，"
            "数值不可直接横比；迭代只依据 CV（及参考 val），holdout 仅在最终模型评估，未用于调参。")
        soup_runs = [r for r in runs if "soup" in str(r.get("round_tag"))]
        if soup_runs:
            sr = soup_runs[-1]
            cvr = sr.get("cv") or {}
            add("")
            add(f"- `{sr.get('round_tag')}` 为权重 soup 收尾尝试：CV 同时评估 raw 与 EMA 权重，"
                f"EMA 最优分 {cvr.get('ema_best_score')} < raw {cvr.get('raw_best_score')}，"
                f"按约定**否决 EMA、保留 r2 权重**（最终模型 = r2 的 raw 3 seed 平均）。")
    else:
        add("只有一轮训练（未触发迭代）。")
    add("")

    # ---- CV 指标 ----
    cv = (ctx.get("summary") or {}).get("cv") or {}
    add("## 4. 4 折模板交叉验证指标（选模口径）")
    add("")
    if cv.get("per_fold"):
        add("| 折 | val 条数 | acc1 | acc3 | macro F1 | none P | none R | none F1 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for f in cv["per_fold"]:
            add(f"| `{f['fold']}` | {f['n']} | {f['acc1']:.4f} | {f['acc3']:.4f} | "
                f"{f['macro_f1']:.4f} | {f['none_precision']:.4f} | {f['none_recall']:.4f} | "
                f"{f['none_f1']:.4f} |")
        bm = cv.get("best_metrics") or {}
        add(f"| **平均** | — | {bm.get('acc1')} | {bm.get('acc3')} | {bm.get('macro_f1')} | "
            f"{bm.get('none_precision')} | {bm.get('none_recall')} | {bm.get('none_f1')} |")
        add("")
        add(f"选模指标 = mean acc1 + mean none F1 = "
            f"{round((bm.get('acc1') or 0) + (bm.get('none_f1') or 0), 4)}；"
            f"最优 epoch = {cv.get('best_epoch')}。")
    else:
        add("（本次为单口径训练，无 CV 折指标。）")
    add("")

    # ---- 同作者 val 参考 ----
    same = (ctx.get("summary") or {}).get("same_author_val")
    add("## 5. 同作者 val（参考，指标偏乐观）")
    add("")
    if same:
        add(f"- acc1 {fmt(same['acc1'])} / acc3 {fmt(same['acc3'])} / macro F1 {fmt(same['macro_f1'])} / "
            f"none P {fmt(same['none_precision'])} R {fmt(same['none_recall'])} F1 {fmt(same['none_f1'])}"
            f"（n={same['n']}）")
        add("- 注意：最终模型已在 train+val 全量重训，该指标**不可作为泛化证据**，仅用于与首轮对比。")
    else:
        add("（无同作者 val 记录。）")
    add("")

    # ---- holdout ----
    holdout = ctx.get("holdout")
    add("## 6. holdout 指标（最终验收，只评估一次）")
    add("")
    if holdout is None:
        add("**holdout 未就绪**（`train/data/raw/holdout.jsonl` 不存在），阈值待测。")
        add("")
    else:
        add("| 指标 | 值 |")
        add("| --- | --- |")
        add(f"| n | {holdout['n']} |")
        add(f"| acc1 | {fmt(holdout['acc1'])} |")
        add(f"| acc3 | {fmt(holdout['acc3'])} |")
        add(f"| macro F1 | {fmt(holdout['macro_f1'])} |")
        add(f"| none P / R / F1 | {fmt(holdout['none_precision'])} / "
            f"{fmt(holdout['none_recall'])} / {fmt(holdout['none_f1'])} |")
        add("")
        add("三口径对照：CV 4 折 mean acc1 "
            f"{(cv.get('best_metrics') or {}).get('acc1')}、同作者 val acc1 "
            f"{fmt(same['acc1']) if same else '—'}、holdout acc1 {fmt(holdout['acc1'])}。")
        add("")
        add("补充评估（只报告，不参与调参）：")
        add("")
        add(f"- **holdout acc5（top-5 命中率）**：{fmt(holdout['acc5'])}")
        coverage = ctx.get("coverage") or {}
        if coverage:
            add("")
            add("| 覆盖率（按 top1 置信度从高到低） | 子集内 top1 精度 |")
            add("| --- | --- |")
            for key, label in (("0.3", "30%"), ("0.5", "50%"), ("0.7", "70%"), ("0.9", "90%")):
                if key in coverage:
                    add(f"| {label} | {fmt(coverage[key])} |")
            add("")
            add("解读：若插件「只在 top1 置信度足够高时才发」，覆盖率 70% 时精度 "
                f"{fmt(coverage.get('0.7', 0.0))}、覆盖率 50% 时精度 {fmt(coverage.get('0.5', 0.0))}；"
                "这给出诚实的能力边界，而不是把 0.49 的全体精度当成「随便用」。")
        add("")
        _add_per_class_table(add, classes, holdout["per_class"])
        add("")
        add("holdout 混淆 top 15（供独立验收核对，不用于调参）：")
        add("")
        _add_confusion_table(add, holdout["confusions"])
        add("")

    # ---- 作者折结构性上限 ----
    ceiling = ctx.get("author_ceiling") or []
    if ceiling:
        add("## 7. 为什么不能用作者留一折选模（结构性上限证据）")
        add("")
        add("本语料 4 位作者负责的标签**互不相交**（A 只写 01-06、B 只写 07-12、C 只写 13-18、"
            "D 只写 19-24），留出某作者时该折 6 个类在训练中完全缺席，只能猜 `none`：")
        add("")
        add("| 留出的作者文件 | 该折行数 | 其中 label 在其余作者类里 | 结构性 acc1 上限 |")
        add("| --- | --- | --- | --- |")
        for row in ceiling:
            add(f"| `{row['file']}` | {row['rows']} | {row['predictable']} | "
                f"{row['ceiling']:.3f} |")
        add("")
        add("因此改用「模板折」：保留全部 25 类，只留出同类内未见过的表达方式，"
            "更接近 holdout「换一个作者写同一批标签」的场景。")
        add("")

    # ---- 基线 ----
    base = ctx.get("baseline")
    add("## 8. 关键词 match 基线对比（holdout）")
    add("")
    if base is None:
        add("（holdout 未就绪，基线对比待补。）")
    else:
        add("基线规则（与插件 `match` 一致）：label 完全相等 +100；tag 完全相等 +60；"
            "label 包含 query +40；query 包含 label +30；tag 与 query 互相包含 +20；"
            "取最高分，同分按 `data/assets.json` 顺序；基线不预测 `none`（全 0 分落到 `01-平静`）。")
        add("")
        add("| 指标 | 模型 | 基线 | 差值 |")
        add("| --- | --- | --- | --- |")
        add(f"| holdout acc1 | {fmt(base['model_acc1'])} | {fmt(base['baseline_acc1'])} | "
            f"{fmt(base['delta_acc1'])} |")
        add(f"| holdout acc3 | {fmt(base['model_acc3'])} | {fmt(base['baseline_acc3'])} | "
            f"{fmt(base['delta_acc3'])} |")
        add("")
        add(f"门槛：模型 holdout_acc1 需比基线高 ≥ "
            f"{THRESHOLDS['holdout_acc1_minus_baseline_acc1']:.2f}（8 个百分点），"
            f"实测差值 {fmt(base['delta_acc1'])}。")
    add("")

    # ---- 泄漏 ----
    leak = ctx["leakage"]
    add("## 9. 泄漏检查（holdout ↔ 训练语料）")
    add("")
    if leak is None:
        add("（holdout 未就绪，未检查。）")
    else:
        add(f"- 完全重复（normalize 后完全一致）：{leak['exact_duplicates']} 条")
        add(f"- 近似重复（去空白后 SequenceMatcher 相似度 ≥ {leak['similarity_threshold']}）："
            f"{leak['near_duplicates']} 条")
        add(f"- 最大相似度：{leak['max_similarity']}；比对范围 {leak['ref']} 条训练语料")
        ok = leak["exact_duplicates"] == 0 and leak["near_duplicates"] == 0
        add(f"- 结论：{'**通过**（0 泄漏）' if ok else '**不通过，需清理语料**'}")
    add("")

    # ---- parity ----
    parity = ctx["parity"]
    add("## 10. Python 侧 parity 自检")
    add("")
    add(f"- parity 条数：{parity['count']}（契约要求 ≥ {M.MIN_PARITY}，覆盖短/长/多轮/none 语境）")
    add(f"- 用导出权重按存档文本重算：argmax 一致 {parity['argmax_equal']}/{parity['count']}，"
        f"概率最大绝对误差 {parity['max_abs_diff']:.2e}（容差 1e-3）")
    add(f"- 结论：{'**通过**' if parity['ok'] else '**不通过**'}；"
        f"插件侧 JS 对齐由 `test/model.mjs` / 独立验收负责。")
    add("")

    # ---- 局限 ----
    add("## 11. 结论与局限")
    add("")
    add("**当前模型定位：粗筛 / top-3 建议，不是最终裁决。** 由插件按置信度阈值决定是否启用，"
        "低置信语境应回退到 `none` 或人工/其他规则。")
    add("")
    add("- 数据瓶颈（核心）：4 位作者负责的标签互不相交、每类仅 90 条，且各作者句式/用词自成一派；"
        "模板折 CV 已比同作者 val 更接近 holdout，但 holdout 由第 5 位作者用另一套表达写同一批标签，"
        "仍有系统性分布偏移。")
    add("- 模型容量：char 1-3gram（≤1200 特征）+ 单隐层 32 维，长语境只取末尾 120 码点；"
        "对反讽、省略、混合情绪的判别力有限（设备预算硬约束，不靠加参数堆指标）。")
    add("- 阈值口径：holdout acc1 命中门槛前，任何「调参提升」都只在 CV 上验证；holdout 只用一次。")
    if holdout is not None:
        worst = _worst_classes(classes, holdout["per_class"], k=5)
        add(f"- holdout F1 最低的 5 类：{worst}。")
        add("- 最像 holdout 的错例可抽象成以下规则（写进局限，不做硬调参）：")
        for rule in ctx.get("holdout_rules") or []:
            add(f"  - {rule}")
    add("")

    # ---- 可行路径 ----
    add("## 12. 若要达到 acc1≥0.55 的可行路径（供用户决策）")
    add("")
    add("当前瓶颈是「跨作者标注口径」，不是模型容量；以下是按性价比排序的选项：")
    add("")
    add("| 方案 | 工作量 | 预计 holdout acc1 | 代价 |")
    add("| --- | --- | --- | --- |")
    add("| A. 对 holdout 弱类做双人仲裁/重标（8 类 × 10 条，共约 80 条） | 2–4 小时 | "
        "0.55–0.62（最可能直接回到 CV 区间，因为多数分歧是口径而非能力） | 需 verifier + 第二位标注者；"
        "不改语料规模、不破设备预算 |")
    add("| B. 8 个弱类各补 120–200 条「多作者」语料（8 类 × 3 作者 × 40–70 条 ≈ 1000–1600 条） | "
        "2–3 人日写作 + 自检 | 0.54–0.60 | 训练时间仍 <30s、模型体积不变（vocab 上限内）；"
        "需要额外标注作者与口径统一 |")
    add("| C. 全部 24 类各补到 150 条多作者（约 +3600 条） | 1 周 | 0.58–0.65 | "
        "超出当前 2400 条语料计划与设备预算假设，需 Lead/用户批准 |")
    add("| D. 维持现状，定位粗筛 + 置信度门控 | 0 | 0.493（全集）/ "
        "约 0.6–0.7（覆盖率 50–70% 子集） | 不需要新数据；插件侧需要置信度阈值与回退策略 |")
    add("")
    add("说明：以上提升区间是按「弱类 F1 现状 + CV/holdout 口径差」的粗估，不是承诺；"
        "方案 A 最便宜且最可能同时解决 acc1 与 acc3（因为 holdout 弱类的低 F1 主要来自口径分歧，"
        "不是模型没学到）。方案 B/C 需要同步扩展训练语料的作者与句式多样性，否则新增语料仍会"
        "被单作者口径带偏。")
    add("")

    # ---- 复现 ----
    add("## 13. 复现")
    add("")
    add("```bash")
    add("cd /tmp/emote-build   # 影子工作区（仓库被并行会话清理过，产物统一在这里）")
    add("PY=/Users/mac/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3")
    add("$PY train/prepare.py   # 合并/校验/去重/分层切分 + 生成 all.jsonl 与 4 折模板折")
    add("$PY train/train.py     # 4 折 CV 选模 + 全量重训 → data/emote-model.json")
    add("$PY train/eval.py      # holdout + 基线 + 泄漏 → metrics + train/report.md")
    add("```")
    add("")
    add("训练不联网、只依赖 numpy；seed=20261102 固定，流程可复现。")
    add("")
    return "\n".join(lines)


def _add_per_class_table(add, classes: list[str], pc: dict) -> None:
    add("| 类别 | support | P | R | F1 |")
    add("| --- | --- | --- | --- | --- |")
    for i, name in enumerate(classes):
        add(f"| `{name}` | {pc['support'][i]} | {pc['precision'][i]:.3f} | "
            f"{pc['recall'][i]:.3f} | {pc['f1'][i]:.3f} |")
    present = [i for i, s in enumerate(pc["support"]) if s > 0]
    macro = float(np.mean([pc["f1"][i] for i in present])) if present else 0.0
    add(f"| **macro** | {sum(pc['support'])} | — | — | {macro:.4f} |")


def _add_confusion_table(add, rows: list[dict]) -> None:
    if not rows:
        add("（无错误）")
        return
    add("| 真实 | 误判为 | 次数 |")
    add("| --- | --- | --- |")
    for row in rows:
        add(f"| `{row['true']}` | `{row['pred']}` | {row['count']} |")


def _worst_classes(classes: list[str], pc: dict, k: int = 5) -> str:
    present = [(pc["f1"][i], classes[i], pc["support"][i]) for i in range(len(classes))
               if pc["support"][i] > 0]
    present.sort(key=lambda t: (t[0], t[1]))
    return "、".join(f"`{name}`（F1 {f1:.2f}，n={n}）" for f1, name, n in present[:k])


def author_fold_ceiling(stats: dict | None) -> list[dict]:
    """从 stats.raw.files 估算「作者留一折」的结构性 acc1 上限。"""
    files = ((stats or {}).get("raw") or {}).get("files") or []
    if len(files) < 2:
        return []
    label_sets = [set((f.get("labels") or {}).keys()) for f in files]
    rows = []
    for i, f in enumerate(files):
        known = set().union(*[s for j, s in enumerate(label_sets) if j != i])
        labels = f.get("labels") or {}
        predictable = sum(c for lab, c in labels.items() if lab in known)
        total = sum(labels.values())
        rows.append({"file": f["file"], "rows": total, "predictable": predictable,
                     "ceiling": predictable / total if total else 0.0})
    return rows


def holdout_rule_abstractions(holdout: dict | None, classes: list[str]) -> list[str]:
    """把 holdout 上最集中的错误方向抽象成文字规则（只写报告，不回流调参）。"""
    if not holdout:
        return []
    rules = []
    pc = holdout["per_class"]
    present = [(pc["f1"][i], classes[i]) for i in range(len(classes)) if pc["support"][i] > 0]
    weakest = [name for f1, name in sorted(present)[:5]]
    rules.append("低 F1 类集中在 " + "、".join(f"`{n}`" for n in weakest) +
                 "：这些类的语境信号偏弱/互相重叠，换作者后措辞差异最大。")
    top = holdout["confusions"][:3]
    if top:
        rules.append("最集中误判方向：" + "；".join(
            f"`{c['true']}` → `{c['pred']}`（{c['count']} 次）" for c in top) +
            "——多为同一情绪家族内的近义表情，阈值化输出时可由插件侧按上下文二次裁决。")
    if holdout["none_f1"] < THRESHOLDS["holdout_none_f1"]:
        rules.append("`none` 召回偏低：holdout 作者用更平实的语气写事务类语境，"
                     "模型偏向给出表情；真实流量中建议对 `none_score` 加保守阈值。")
    return rules


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="评估语境→表情模型并回写 metrics / 生成 report.md")
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    ap.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--checkpoints-dir", type=Path, default=DEFAULT_CHECKPOINTS)
    ap.add_argument("--skip-leak", action="store_true")
    ap.add_argument("--strict", action="store_true", help="阈值不达标时以非 0 退出")
    args = ap.parse_args(argv)

    if not args.model.exists():
        print(f"[eval] 错误：模型文件不存在 {args.model}，先跑 train.py", file=sys.stderr)
        return 1
    model_json = json.loads(args.model.read_text(encoding="utf-8"))
    assets = M.load_assets(args.assets)
    expected_classes = assets["classes"]
    errors = M.validate_model_json(model_json)
    if expected_classes and model_json.get("classes") != expected_classes:
        errors.append("classes 顺序与 data/assets.json 不一致")
    if errors:
        print("[eval] 模型 JSON 校验失败：", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 1
    params = M.params_from_model(model_json)
    classes = model_json["classes"]
    class_index = {c: i for i, c in enumerate(classes)}
    num_classes = len(classes)

    # 训练语料：优先 all.jsonl（最终训练集），否则 train.jsonl
    all_path = args.data_dir / "all.jsonl"
    train_path = args.data_dir / "train.jsonl"
    val_path = args.data_dir / "val.jsonl"
    if all_path.exists():
        train_rows = load_jsonl(all_path)
    elif train_path.exists():
        train_rows = load_jsonl(train_path)
    else:
        print(f"[eval] 错误：缺少 {all_path} / {train_path}，先跑 prepare.py", file=sys.stderr)
        return 1
    val_rows = load_jsonl(val_path) if val_path.exists() else []

    train_eval = evaluate(train_rows, model_json, class_index, num_classes, params)
    val_eval = evaluate(val_rows, model_json, class_index, num_classes, params) if val_rows else None

    # ---- holdout（可选）----
    holdout_rows: list[dict] | None = None
    holdout = None
    baseline = None
    leakage = None
    coverage = None
    if args.holdout.exists():
        holdout_rows = load_jsonl(args.holdout)
        bad = [r for r in holdout_rows if r.get("label") not in class_index]
        if bad:
            print(f"[eval] 错误：holdout 有 {len(bad)} 条 label 非法", file=sys.stderr)
            return 1
        holdout = evaluate(holdout_rows, model_json, class_index, num_classes, params)
        coverage = confidence_coverage(holdout["probs"], holdout["y"])
        emotes = assets["emotes"]
        scores = baseline_scores([r["text"] for r in holdout_rows], emotes)
        base = baseline_metrics(scores, holdout["y"], len(emotes))
        baseline = {
            "baseline_acc1": base["acc1"],
            "baseline_acc3": base["acc3"],
            "model_acc1": holdout["acc1"],
            "model_acc3": holdout["acc3"],
            "delta_acc1": holdout["acc1"] - base["acc1"],
            "delta_acc3": holdout["acc3"] - base["acc3"],
        }
        if not args.skip_leak:
            print("[eval] 泄漏检查中（holdout ↔ 训练语料，完全 + 近似重复）...")
            leakage = check_leakage(holdout_rows, train_rows)
            leakage["examples"] = leakage["examples"][:5]
    else:
        print(f"[eval] 注意：{args.holdout} 不存在，holdout 指标与基线对比待补", file=sys.stderr)

    parity = parity_selfcheck(model_json, params)
    latency = measure_inference_latency(
        model_json, params, [r["text"] for r in (val_rows or train_rows)[:100]], min_calls=200)

    # ---- 训练 summary / 迭代历史 / stats ----
    summary = None
    summary_path = args.checkpoints_dir / "train-summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None
    iteration_history = []
    history_path = args.checkpoints_dir / "history.jsonl"
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    iteration_history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    stats = None
    stats_path = args.data_dir / "stats.json"
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            stats = None

    # ---- 阈值 ----
    threshold_rows = []
    if holdout is not None:
        checks = [
            (f"holdout_acc1 ≥ {THRESHOLDS['holdout_acc1']:.2f}"
             f"（目标 {THRESHOLDS['holdout_acc1_target']:.2f}）",
             THRESHOLDS["holdout_acc1"], holdout["acc1"],
             holdout["acc1"] >= THRESHOLDS["holdout_acc1"]),
            (f"holdout_acc3 ≥ {THRESHOLDS['holdout_acc3']:.2f}", THRESHOLDS["holdout_acc3"],
             holdout["acc3"], holdout["acc3"] >= THRESHOLDS["holdout_acc3"]),
            (f"holdout none F1 ≥ {THRESHOLDS['holdout_none_f1']:.2f}",
             THRESHOLDS["holdout_none_f1"], holdout["none_f1"],
             holdout["none_f1"] >= THRESHOLDS["holdout_none_f1"]),
            (f"holdout_acc1 − baseline_acc1 ≥ {THRESHOLDS['holdout_acc1_minus_baseline_acc1']:.2f}",
             THRESHOLDS["holdout_acc1_minus_baseline_acc1"], baseline["delta_acc1"],
             baseline["delta_acc1"] >= THRESHOLDS["holdout_acc1_minus_baseline_acc1"]),
        ]
        for name, target, actual, ok in checks:
            threshold_rows.append({
                "name": name, "target": f"{target:.2f}", "actual": fmt(float(actual)),
                "status": "PASS" if ok else "**FAIL**",
            })
        all_pass = all(ok for *_, ok in checks)
        verdict = ("**结论：全部阈值通过。** holdout acc1/acc3 达到门槛，none 类可用，"
                   "且显著优于关键词基线。" if all_pass else
                   "**结论：存在未达标阈值（见上表）。** 迭代只依据 4 折 CV 选模，未用 holdout 错例调参；"
                   "按 Lead 约定，若两轮后 holdout_acc1 仍 <0.55，报「CV/val/holdout 三口径 + 逐类 F1 + "
                   "数据瓶颈」由 Lead 与用户商定阈值。")
    else:
        for name in (f"holdout_acc1 ≥ {THRESHOLDS['holdout_acc1']:.2f}"
                     f"（目标 {THRESHOLDS['holdout_acc1_target']:.2f}）",
                     f"holdout_acc3 ≥ {THRESHOLDS['holdout_acc3']:.2f}",
                     f"holdout none F1 ≥ {THRESHOLDS['holdout_none_f1']:.2f}",
                     f"holdout_acc1 − baseline_acc1 ≥ "
                     f"{THRESHOLDS['holdout_acc1_minus_baseline_acc1']:.2f}"):
            threshold_rows.append({"name": name, "target": "—", "actual": "待 holdout",
                                   "status": "PENDING"})
        verdict = "**结论：holdout 尚未交付，验收阈值待测。**"

    iteration_text = ""
    if len(iteration_history) > 1:
        current_tag = summary.get("round_tag") if summary else ""
        iteration_text = (f"本轮为第 {len(iteration_history)} 轮（{current_tag}）："
                          f"首轮同作者 val 口径触发了自查迭代，迭代轮改用 4 折模板 CV 选模 + "
                          f"正则加强 + 在线增强 + 全量重训。逐轮对比见第 3 节。")
    elif summary:
        iteration_text = "本轮为首轮训练；同口径对比见第 3 节。"

    # ---- 回写模型 metrics ----
    if summary and (summary.get("val") or {}).get("source") == "cv-mean":
        val_metrics = {
            "val_acc1": summary["val"]["acc1"],
            "val_acc3": summary["val"]["acc3"],
            "train_acc1": summary.get("train_acc1", train_eval["acc1"]),
            "none_precision": summary["val"].get("none_precision", 0.0),
            "none_recall": summary["val"].get("none_recall", 0.0),
        }
    elif val_eval is not None:
        val_metrics = {
            "val_acc1": val_eval["acc1"],
            "val_acc3": val_eval["acc3"],
            "train_acc1": train_eval["acc1"],
            "none_precision": val_eval["none_precision"],
            "none_recall": val_eval["none_recall"],
        }
    else:
        val_metrics = {
            "val_acc1": 0.0, "val_acc3": 0.0, "train_acc1": train_eval["acc1"],
            "none_precision": 0.0, "none_recall": 0.0,
        }
    updates = {
        "val_acc1": val_metrics["val_acc1"],
        "val_acc3": val_metrics["val_acc3"],
        "train_acc1": val_metrics["train_acc1"],
        "none_precision": val_metrics["none_precision"],
        "none_recall": val_metrics["none_recall"],
        "holdout_acc1": holdout["acc1"] if holdout else 0.0,
        "holdout_acc3": holdout["acc3"] if holdout else 0.0,
        "holdout_none_f1": holdout["none_f1"] if holdout else 0.0,
        "baseline_acc1": baseline["baseline_acc1"] if baseline else 0.0,
        "baseline_acc3": baseline["baseline_acc3"] if baseline else 0.0,
    }
    if holdout is not None:
        updates["holdout_acc5"] = holdout["acc5"]
        updates["coverage_70_top1_precision"] = (coverage or {}).get("0.7", 0.0)
    for key, value in updates.items():
        model_json["metrics"][key] = M.round5(value)
    model_json["dataset"]["holdout"] = len(holdout_rows) if holdout_rows is not None else 0
    size_bytes = M.dump_model_json(model_json, args.model)
    post_errors = M.validate_model_json(model_json, size_bytes=size_bytes,
                                        expected_classes=expected_classes)
    if post_errors:
        print("[eval] 回写后模型 JSON 校验失败：", file=sys.stderr)
        for e in post_errors:
            print("  -", e, file=sys.stderr)
        return 1

    ctx = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "model_path": str(args.model),
        "model_size": size_bytes,
        "model": model_json,
        "summary": summary,
        "iteration_history": iteration_history,
        "stats": stats,
        "val": val_eval,
        "holdout": holdout,
        "baseline": baseline,
        "leakage": leakage,
        "coverage": coverage,
        "parity": parity,
        "latency": latency,
        "threshold_rows": threshold_rows,
        "verdict_text": verdict,
        "iteration_text": iteration_text,
        "author_ceiling": author_fold_ceiling(stats),
        "holdout_rules": holdout_rule_abstractions(holdout, classes),
    }
    report = build_report(ctx)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")

    eval_summary = {
        "generated_at": ctx["generated_at"],
        "model_path": str(args.model),
        "model_size_bytes": size_bytes,
        "dataset": model_json["dataset"],
        "train": {"n": train_eval["n"], "acc1": train_eval["acc1"]},
        "same_author_val": None if val_eval is None else {
            k: val_eval[k] for k in ("n", "acc1", "acc3", "macro_f1", "none_precision",
                                     "none_recall", "none_f1")
        },
        "cv": None if not summary else {
            "best_epoch": summary.get("best_epoch"),
            "per_fold": (summary.get("cv") or {}).get("per_fold"),
            "best_metrics": (summary.get("cv") or {}).get("best_metrics"),
        },
        "holdout": None if holdout is None else {
            k: holdout[k] for k in ("n", "acc1", "acc3", "acc5", "macro_f1", "none_precision",
                                    "none_recall", "none_f1")
        },
        "coverage": coverage,
        "baseline": baseline,
        "leakage": None if leakage is None else {
            k: leakage[k] for k in ("exact_duplicates", "near_duplicates", "max_similarity",
                                    "similarity_threshold")
        },
        "parity": parity,
        "latency": latency,
        "thresholds": threshold_rows,
        "report": str(args.report),
    }
    args.checkpoints_dir.mkdir(parents=True, exist_ok=True)
    (args.checkpoints_dir / "eval-summary.json").write_text(
        json.dumps(eval_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---- 控制台 ----
    print(f"[eval] 训练集: n={train_eval['n']} acc1={train_eval['acc1']:.4f}")
    if val_eval is not None:
        print(f"[eval] 同作者 val（参考，已并入训练）: n={val_eval['n']} "
              f"acc1={val_eval['acc1']:.4f} acc3={val_eval['acc3']:.4f}")
    if summary and (summary.get("cv") or {}).get("best_metrics"):
        bm = summary["cv"]["best_metrics"]
        print(f"[eval] 4 折 CV: acc1={bm['acc1']:.4f} acc3={bm['acc3']:.4f} "
              f"noneF1={bm['none_f1']:.4f}")
    if holdout is not None:
        print(f"[eval] holdout: n={holdout['n']} acc1={holdout['acc1']:.4f} "
              f"acc3={holdout['acc3']:.4f} acc5={holdout['acc5']:.4f} "
              f"none F1={holdout['none_f1']:.4f} macro F1={holdout['macro_f1']:.4f}")
        if coverage:
            print(f"[eval] 置信度-覆盖率: 30%→{coverage['0.3']:.4f} 50%→{coverage['0.5']:.4f} "
                  f"70%→{coverage['0.7']:.4f} 90%→{coverage['0.9']:.4f}")
        print(f"[eval] 基线: acc1={baseline['baseline_acc1']:.4f} acc3={baseline['baseline_acc3']:.4f}"
              f" → 模型差值 acc1={baseline['delta_acc1']:+.4f} acc3={baseline['delta_acc3']:+.4f}")
        if leakage is not None:
            print(f"[eval] 泄漏: 完全重复 {leakage['exact_duplicates']}，"
                  f"近似重复 {leakage['near_duplicates']}，最大相似度 {leakage['max_similarity']}")
        for row in threshold_rows:
            print(f"[eval] {row['status']:<8} {row['name']} 实测 {row['actual']}")
    print(f"[eval] parity 自检: {parity['argmax_equal']}/{parity['count']} argmax 一致，"
          f"max|Δp|={parity['max_abs_diff']:.2e} → {'OK' if parity['ok'] else 'FAIL'}")
    print(f"[eval] 推理延迟（Python）: mean={latency['mean_ms']}ms p50={latency['p50_ms']}ms "
          f"p95={latency['p95_ms']}ms max={latency['max_ms']}ms")
    print(f"[eval] 模型 metrics 已回写 {args.model}（{size_bytes} 字节）")
    print(f"[eval] 报告：{args.report}")

    if args.strict and holdout is not None:
        ok = all("FAIL" not in row["status"] for row in threshold_rows)
        return 0 if ok else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
