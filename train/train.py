#!/usr/bin/env python3
"""train：chi2 词表 → 4 折模板交叉验证选模 → 全量重训 → 导出模型 JSON。

选模口径（2026-11-02 修订）：同作者随机 val 会高估泛化（模型在学作者风格），
改用 **4 折模板交叉验证**：同一类内按 3-gram Jaccard 聚类，整簇留出，
验证集句式不出现在训练集。本语料 4 位作者负责的标签互不相交（A 只写 01-06…），
作者留一折会让该折 6 个类在训练里完全缺席（结构性 acc1 上限 0.100），
故只能作诊断口径，不能作选模口径。

- 选模/早停指标 = 4 折平均 acc1 + 4 折平均 none F1；
- 超参选定后用 **全部训练语料（不含 holdout）** 重训最终模型，epochs=CV 最优 epoch；
- holdout 只在 eval.py 最终评估一次，不参与调参。

在线数据增强（确定性）：每 epoch 每条样本采样一个增强版本（去「用户/助手」前缀、
去句末标点、10% 删 1 字、10% 插入填充词），用于逼近 holdout 的「换人写」分布。

输出：data/emote-model.json（严格 model-format.md 第 3 节）、train/checkpoints/*（不提交）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
import tracemalloc
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import model as M

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
DEFAULT_DATA_DIR = TRAIN_DIR / "data"
DEFAULT_ASSETS = REPO_ROOT / "data" / "assets.json"
DEFAULT_OUT = REPO_ROOT / "data" / "emote-model.json"
DEFAULT_CHECKPOINTS = TRAIN_DIR / "checkpoints"

PREFIXES = ("用户：", "助手：", "用户:", "助手:")
END_PUNCT = "。！？，、；：…～~,.!?"
FILLERS = ("啊", "呢", "吧")


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno} JSON 解析失败: {exc}") from exc
            if not isinstance(obj.get("text"), str) or not obj["text"]:
                raise SystemExit(f"{path}:{lineno} text 为空")
            if not isinstance(obj.get("label"), str) or not obj["label"]:
                raise SystemExit(f"{path}:{lineno} label 为空")
            rows.append(obj)
    return rows


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 词表构建：按类别 chi2 的最大值选特征，并强制覆盖每个类别
# ---------------------------------------------------------------------------

def build_vocab(texts: list[str], labels: np.ndarray, num_classes: int,
                max_vocab: int = M.MAX_VOCAB, min_df: int = 2) -> tuple[list[str], dict]:
    """返回 (按特征下标排列的 gram 列表, 统计)。"""
    gram_ids: dict[str, int] = {}
    rows: list[list[int]] = []
    for text in texts:
        idx = []
        for g in M.text_grams(text):
            j = gram_ids.get(g)
            if j is None:
                j = len(gram_ids)
                gram_ids[g] = j
            idx.append(j)
        idx.sort()
        rows.append(idx)

    n = len(texts)
    vocab_all = len(gram_ids)
    grams_of = [None] * vocab_all
    for g, j in gram_ids.items():
        grams_of[j] = g

    df = np.zeros(vocab_all, dtype=np.int64)
    for row in rows:
        if row:
            df[row] += 1

    if n == 0 or not any(rows):
        raise SystemExit("训练集为空，无法构建词表")

    flat_rows = np.concatenate([np.full(len(r), i, dtype=np.int64) for i, r in enumerate(rows)])
    flat_cols = np.concatenate([np.asarray(r, dtype=np.int64) for r in rows])
    y = np.asarray(labels, dtype=np.int64)
    class_counts = np.bincount(y, minlength=num_classes)

    best_score = np.zeros(vocab_all, dtype=np.float64)
    best_class = np.full(vocab_all, -1, dtype=np.int64)
    for c in range(num_classes):
        nc = int(class_counts[c])
        if nc == 0:
            continue
        mask = (y[flat_rows] == c)
        a = np.bincount(flat_cols[mask], minlength=vocab_all).astype(np.float64)
        b = df - a
        cc = nc - a
        d = n - nc - df + a
        denom = (a + b) * (cc + d) * (a + cc) * (b + d)
        with np.errstate(divide="ignore", invalid="ignore"):
            chi = n * (a * d - b * cc) ** 2 / denom
        chi = np.nan_to_num(chi, nan=0.0, posinf=0.0, neginf=0.0)
        better = chi > best_score
        best_score[better] = chi[better]
        best_class[better] = np.int64(c)

    cand = np.where(df >= min_df)[0]
    if len(cand) == 0:
        cand = np.where(df >= 1)[0]
    # 排序：chi2 降序 → df 降序 → gram 字典序（保证可复现）
    entries = sorted(
        ((grams_of[j], -float(best_score[j]), -int(df[j]), int(j)) for j in cand),
        key=lambda t: (t[1], t[2], t[0]),
    )
    order_ids = [int(j) for *_, j in entries]

    if len(order_ids) > max_vocab:
        chosen: list[int] = []
        seen: set[int] = set()
        # 强制覆盖每个类别：取该类别 chi2 最高的 3 个 gram
        for c in range(num_classes):
            if class_counts[c] == 0:
                continue
            idx_c = cand[best_class[cand] == c]
            if len(idx_c) == 0:
                continue
            k = min(3, len(idx_c))
            # 同分/同 df 时用 gram 字典序兜底，避免依赖 set/hash 的随机顺序（保证可复现）
            ranked = sorted(
                ((grams_of[j], -float(best_score[j]), -int(df[j]), int(j))
                 for j in idx_c.tolist()),
                key=lambda t: (t[1], t[2], t[0]),
            )
            for *_, j in ranked[:k]:
                if j not in seen and len(chosen) < max_vocab:
                    chosen.append(int(j))
                    seen.add(int(j))
        for j in order_ids:
            if len(chosen) >= max_vocab:
                break
            if j not in seen:
                chosen.append(j)
                seen.add(j)
        order_ids = chosen

    vocab = [grams_of[j] for j in order_ids]
    vocab_stats = {
        "unique_grams": vocab_all,
        "min_df": min_df,
        "selected": len(vocab),
        "candidates": int(len(cand)),
        "forced_per_class": 3,
        "max_chi2": round(float(best_score.max()), 4) if vocab_all else 0.0,
        "selected_chi2_median": round(float(np.median([best_score[j] for j in order_ids])), 4) if order_ids else 0.0,
    }
    return vocab, vocab_stats


# ---------------------------------------------------------------------------
# 特征 / 评估辅助
# ---------------------------------------------------------------------------

def predict_matrix(x: np.ndarray, params: dict[str, np.ndarray], batch: int = 1024) -> np.ndarray:
    out = np.empty((x.shape[0], params["b2"].shape[0]), dtype=M.FLOAT)
    for i in range(0, x.shape[0], batch):
        _, logits = M.forward(x[i:i + batch], params)
        out[i:i + batch] = M.softmax(logits)
    return out


def predict_rows(base_x: np.ndarray, params: dict[str, np.ndarray], ids: np.ndarray) -> np.ndarray:
    return predict_matrix(base_x[ids], params)


def class_weights(y: np.ndarray, num_classes: int, mode: str = "invfreq",
                  none_idx: int | None = None, none_boost: float = 1.0) -> np.ndarray:
    """类别权重（按样本均值归一到 1）；空类权重为 0。

    mode: invfreq（反频率，默认）/ sqrt（反频率开方，较温和）/ uniform（全 1）。
    none_boost: 只放大 none 类权重，用于把 none F1 拉回阈值以上（调参旋钮）。
    """
    counts = np.bincount(y, minlength=num_classes).astype(np.float64)
    w = np.zeros(num_classes, dtype=np.float64)
    present = counts > 0
    if mode == "uniform":
        w[present] = 1.0
    else:
        base = len(y) / (present.sum() * counts[present])
        w[present] = base if mode == "invfreq" else np.sqrt(base)
    if none_idx is not None and none_boost != 1.0 and counts[none_idx] > 0:
        w[none_idx] *= none_boost
    mean = float((w[y]).mean())
    if mean > 0:
        w = w / mean
    return w


def eval_metrics(probs: np.ndarray, y: np.ndarray, num_classes: int,
                 none_idx: int) -> dict:
    pc = M.per_class_metrics(probs, y, num_classes)
    return {
        "n": int(len(y)),
        "acc1": M.accuracy_at_k(probs, y, 1),
        "acc3": M.accuracy_at_k(probs, y, 3),
        "macro_f1": M.macro_f1(probs, y, num_classes),
        "none_precision": float(pc["precision"][none_idx]),
        "none_recall": float(pc["recall"][none_idx]),
        "none_f1": float(pc["f1"][none_idx]),
    }


# ---------------------------------------------------------------------------
# 在线数据增强（确定性、可复现）
# ---------------------------------------------------------------------------

def augment_text(text: str, rng: np.random.Generator) -> str:
    """一次增强：随机去 用户/助手 前缀、去句末标点、10% 删 1 字、10% 插入填充词。"""
    t = text
    if rng.random() < 0.5:
        for pre in PREFIXES:
            t = t.replace(pre, "")
    if rng.random() < 0.5:
        t = t.rstrip(END_PUNCT) or t
    if len(t) > 6 and rng.random() < 0.1:
        pos = int(rng.integers(1, len(t)))
        t = t[:pos] + t[pos + 1:]
    if t and rng.random() < 0.1:
        pos = int(rng.integers(0, len(t) + 1))
        t = t[:pos] + FILLERS[int(rng.integers(0, len(FILLERS)))] + t[pos:]
    return t if t.strip() else text


def build_features(texts: list[str], vocab_index: dict[str, int], vocab_size: int,
                   augment: bool, variants: int, seed: int):
    """返回 (干净特征矩阵 base_x, 增强特征矩阵列表 variants|None)。"""
    base_x = M.featurize(texts, vocab_index, vocab_size)
    if not augment or variants <= 0:
        return base_x, None
    variant_mats = []
    for v in range(variants):
        rng = np.random.default_rng((seed, 92017, v))
        aug_texts = [augment_text(t, rng) for t in texts]
        variant_mats.append(M.featurize(aug_texts, vocab_index, vocab_size).astype(np.float32))
    return base_x, variant_mats


# ---------------------------------------------------------------------------
# 单 epoch 训练（支持增强变体矩阵）
# ---------------------------------------------------------------------------

def train_epoch(params: dict[str, np.ndarray], adam: M.Adam, base_x: np.ndarray,
                variants, row_ids: np.ndarray,
                y_all: np.ndarray, sw_all: np.ndarray,
                rng: np.random.Generator, args) -> float:
    n = len(row_ids)
    if n == 0:
        return 0.0
    batch = max(1, min(args.batch_size, n))
    total, seen = 0.0, 0

    def step(xb, yb, wb):
        nonlocal total, seen
        if args.feature_dropout > 0:
            xb = xb * (rng.random(xb.shape) >= args.feature_dropout)
        h, logits = M.forward(xb, params)
        loss, dlogits = M.softmax_ce(logits, yb, wb, args.label_smoothing)
        grads = M.backward(xb, params, h, dlogits, args.l2)
        adam.step(params, grads)
        total += loss * len(yb)
        seen += len(yb)

    if not variants:
        order = rng.permutation(n)
        for start in range(0, n, batch):
            sel = order[start:start + batch]
            ids = row_ids[sel]
            step(base_x[ids], y_all[ids], sw_all[ids])
    else:
        K = len(variants)
        pick = rng.integers(0, K, size=n)
        order = rng.permutation(n)
        for v in range(K):
            group = order[pick[order] == v]
            if group.size == 0:
                continue
            group = group[rng.permutation(group.size)]
            for start in range(0, group.size, batch):
                sel = group[start:start + batch]
                ids = row_ids[sel]
                step(variants[v][ids].astype(M.FLOAT), y_all[ids], sw_all[ids])
    return total / max(1, seen)


# ---------------------------------------------------------------------------
# parity 样本选择（覆盖不同长度 / 多轮 / none）
# ---------------------------------------------------------------------------

def select_parity_rows(val_rows: list[dict], classes: list[str], limit: int = 12) -> list[dict]:
    def norm_len(r: dict) -> int:
        return len(M.tail_truncate(M.normalize(r["text"])))

    def key(r: dict):
        return (norm_len(r), r["text"])

    picked: list[dict] = []
    seen: set[str] = set()

    def take(rows: list[dict]) -> None:
        for r in rows:
            if len(picked) >= limit:
                return
            if r["text"] in seen:
                continue
            seen.add(r["text"])
            picked.append(r)

    ordered = sorted(val_rows, key=key)
    take(ordered[:3])                                            # 短
    take(sorted(val_rows, key=lambda r: (-norm_len(r), r["text"]))[:2])  # 长（可能触发截断）
    multi = sorted([r for r in val_rows if "用户：" in r["text"] and "助手：" in r["text"]], key=key)
    take(multi[:3])                                              # 多轮
    none_rows = sorted([r for r in val_rows if r["label"] == M.NONE_LABEL], key=key)
    take(none_rows[:2])                                          # none 语境
    if len(picked) < limit:
        by_label: dict[str, list[dict]] = {}
        for r in ordered:
            by_label.setdefault(r["label"], []).append(r)
        idx = 0
        while len(picked) < limit and idx < 200:
            progressed = False
            for label in classes:
                bucket = by_label.get(label)
                if bucket and idx < len(bucket):
                    progressed = True
                    take([bucket[idx]])
                if len(picked) >= limit:
                    break
            if not progressed:
                break
            idx += 1
    take(ordered)  # 兜底
    return picked[:limit]


# ---------------------------------------------------------------------------
# 折加载 / 交叉验证 / 全量重训
# ---------------------------------------------------------------------------

def load_folds(all_rows: list[dict], data_dir: Path, cv_mode: str) -> list[dict]:
    """返回 [{name, train_ids, val_ids}]（ids 为 all_rows 下标）。"""
    text_to_id = {r["text"]: i for i, r in enumerate(all_rows)}
    folds: list[dict] = []
    if cv_mode == "template":
        fold_dir = data_dir / "folds"
        files = sorted(fold_dir.glob("fold-*.jsonl")) if fold_dir.exists() else []
        if len(files) < 2:
            raise SystemExit(f"[train] 错误：{fold_dir} 下没有 fold-*.jsonl，先跑 prepare.py")
        for f in files:
            val_rows = load_jsonl(f)
            val_ids = np.asarray([text_to_id[r["text"]] for r in val_rows
                                  if r["text"] in text_to_id], dtype=np.int64)
            if len(val_ids) == 0:
                continue
            mask = np.ones(len(all_rows), dtype=bool)
            mask[val_ids] = False
            folds.append({"name": f.stem, "train_ids": np.where(mask)[0], "val_ids": val_ids})
    elif cv_mode == "author":
        by_source: dict[str, list[int]] = {}
        for i, r in enumerate(all_rows):
            by_source.setdefault(r.get("source", ""), []).append(i)
        for src in sorted(by_source):
            val_ids = np.asarray(by_source[src], dtype=np.int64)
            mask = np.ones(len(all_rows), dtype=bool)
            mask[val_ids] = False
            folds.append({"name": src, "train_ids": np.where(mask)[0], "val_ids": val_ids})
    else:
        raise SystemExit(f"[train] 未知 --cv-mode {cv_mode!r}")
    return folds


def run_cv(args, folds: list[dict], base_x: np.ndarray, variants,
           y_all: np.ndarray, sw_all: np.ndarray, num_classes: int,
           none_idx: int, vocab_size: int) -> dict:
    """4 折交叉验证；同时跟踪 raw 权重与 EMA 权重，按 CV 分数选优。"""
    decay = max(0.0, float(args.ema_decay))
    states = []
    for k in range(len(folds)):
        p = M.init_params(vocab_size, args.hidden, num_classes,
                          np.random.default_rng(args.seed + k))
        states.append({"params": p, "adam": M.Adam(p, lr=args.lr), "ema": None})

    best = {"raw": {"score": -math.inf, "epoch": 0, "params": None},
            "ema": {"score": -math.inf, "epoch": 0, "params": None}}
    best_overall = -math.inf
    history: list[dict] = []
    no_improve = 0

    def agg(metrics: list[dict]) -> dict:
        return {
            "acc1": float(np.mean([m["acc1"] for m in metrics])),
            "acc3": float(np.mean([m["acc3"] for m in metrics])),
            "none_precision": float(np.mean([m["none_precision"] for m in metrics])),
            "none_recall": float(np.mean([m["none_recall"] for m in metrics])),
            "none_f1": float(np.mean([m["none_f1"] for m in metrics])),
            "macro_f1": float(np.mean([m["macro_f1"] for m in metrics])),
        }

    for epoch in range(1, args.epochs + 1):
        epoch_rng = np.random.default_rng((args.seed, 7001, epoch))
        fold_metrics = []
        fold_metrics_ema = []
        for k, fold in enumerate(folds):
            st = states[k]
            train_loss = train_epoch(st["params"], st["adam"], base_x, variants,
                                     fold["train_ids"], y_all, sw_all, epoch_rng, args)
            if decay > 0:
                if st["ema"] is None:
                    st["ema"] = {kk: vv.copy() for kk, vv in st["params"].items()}
                else:
                    st["ema"] = {kk: decay * st["ema"][kk] + (1.0 - decay) * st["params"][kk]
                                 for kk in st["params"]}
            probs = predict_rows(base_x, st["params"], fold["val_ids"])
            m = eval_metrics(probs, y_all[fold["val_ids"]], num_classes, none_idx)
            m["fold"] = fold["name"]
            m["train_loss"] = round(float(train_loss), 5)
            fold_metrics.append(m)
            if decay > 0:
                probs_ema = predict_rows(base_x, st["ema"], fold["val_ids"])
                m_ema = eval_metrics(probs_ema, y_all[fold["val_ids"]], num_classes, none_idx)
                m_ema["fold"] = fold["name"]
                m_ema["train_loss"] = round(float(train_loss), 5)
                fold_metrics_ema.append(m_ema)

        raw = agg(fold_metrics)
        raw["score"] = raw["acc1"] + raw["none_f1"]
        row = {
            "epoch": epoch,
            "cv_acc1": round(raw["acc1"], 5),
            "cv_acc3": round(raw["acc3"], 5),
            "cv_none_f1": round(raw["none_f1"], 5),
            "cv_macro_f1": round(raw["macro_f1"], 5),
            "cv_score": round(raw["score"], 5),
            "folds": fold_metrics,
        }
        if decay > 0:
            ema = agg(fold_metrics_ema)
            ema["score"] = ema["acc1"] + ema["none_f1"]
            row.update({
                "cv_acc1_ema": round(ema["acc1"], 5),
                "cv_acc3_ema": round(ema["acc3"], 5),
                "cv_none_f1_ema": round(ema["none_f1"], 5),
                "cv_macro_f1_ema": round(ema["macro_f1"], 5),
                "cv_score_ema": round(ema["score"], 5),
                "folds_ema": fold_metrics_ema,
            })
        history.append(row)

        improved_any = False
        for variant, metrics, fold_m in (("raw", raw, fold_metrics),
                                         ("ema", ema if decay > 0 else None, fold_metrics_ema)):
            if metrics is None:
                continue
            if metrics["score"] > best[variant]["score"] + 1e-9:
                best[variant] = {
                    "score": metrics["score"], "epoch": epoch,
                    "params": ([{kk: vv.copy() for kk, vv in st["params"].items()} for st in states]
                               if variant == "raw"
                               else [{kk: vv.copy() for kk, vv in st["ema"].items()} for st in states]),
                    "folds": fold_m,
                }
                improved_any = True
        top_score = max(best["raw"]["score"], best["ema"]["score"])
        if top_score > best_overall + 1e-9:
            best_overall = top_score
            no_improve = 0
        else:
            no_improve += 1

        if not args.quiet:
            folds_txt = " ".join(f"{m['fold']}:{m['acc1']:.3f}/{m['none_f1']:.3f}"
                                 for m in fold_metrics)
            extra = ""
            if decay > 0:
                extra = f"  | ema acc1 {row['cv_acc1_ema']:.4f} noneF1 {row['cv_none_f1_ema']:.4f}"
            print(f"[train] cv epoch {epoch:2d}  raw acc1 {raw['acc1']:.4f}  acc3 {raw['acc3']:.4f}  "
                  f"noneF1 {raw['none_f1']:.4f}  score {raw['score']:.4f}{extra}"
                  f"{'  *' if improved_any else ''}  [{folds_txt}]")

        if args.patience > 0 and no_improve >= args.patience:
            if not args.quiet:
                print(f"[train] CV 早停：连续 {args.patience} 个 epoch 未提升 "
                      f"(mean acc1 + mean none F1)")
            break

    variant = "raw"
    if decay > 0 and best["ema"]["score"] > best["raw"]["score"] + 1e-9:
        variant = "ema"
    chosen = best[variant]
    best_entry = None
    for h in history:
        if variant == "raw" and h["cv_score"] == round(chosen["score"], 5):
            best_entry = h
        if variant == "ema" and h.get("cv_score_ema") == round(chosen["score"], 5):
            best_entry = h
    if best_entry is None:
        best_entry = history[-1]

    return {
        "history": history,
        "best_epoch": chosen["epoch"],
        "best_score": chosen["score"],
        "variant": variant,
        "raw_best_score": best["raw"]["score"],
        "ema_best_score": best["ema"]["score"] if decay > 0 else None,
        "best_folds": chosen["folds"],
        "per_fold": chosen["folds"],
        "best_metrics": {
            "acc1": round(float(np.mean([m["acc1"] for m in chosen["folds"]])), 5),
            "acc3": round(float(np.mean([m["acc3"] for m in chosen["folds"]])), 5),
            "none_f1": round(float(np.mean([m["none_f1"] for m in chosen["folds"]])), 5),
            "none_precision": round(float(np.mean([m["none_precision"] for m in chosen["folds"]])), 5),
            "none_recall": round(float(np.mean([m["none_recall"] for m in chosen["folds"]])), 5),
            "macro_f1": round(float(np.mean([m["macro_f1"] for m in chosen["folds"]])), 5),
        },
    }


def train_final(args, all_ids: np.ndarray, base_x: np.ndarray, variants,
                y_all: np.ndarray, sw_all: np.ndarray, num_classes: int,
                vocab_size: int, epochs: int, use_ema: bool = False) -> tuple[dict, list[dict]]:
    """在全部训练语料上重训最终模型（多 seed 权重平均；可选 EMA 权重）。"""
    n_seeds = max(1, args.seed_ensemble)
    decay = max(0.0, float(args.ema_decay)) if use_ema else 0.0
    params_list = []
    per_seed = []
    for s in range(n_seeds):
        seed = args.seed + 1000 * s
        p = M.init_params(vocab_size, args.hidden, num_classes, np.random.default_rng(seed))
        adam = M.Adam(p, lr=args.lr)
        ema = None
        last_loss = 0.0
        for epoch in range(1, epochs + 1):
            rng = np.random.default_rng((seed, 8101, epoch))
            last_loss = train_epoch(p, adam, base_x, variants, all_ids, y_all, sw_all, rng, args)
            if decay > 0:
                if ema is None:
                    ema = {k: v.copy() for k, v in p.items()}
                else:
                    ema = {k: decay * ema[k] + (1.0 - decay) * p[k] for k in p}
            if not args.quiet:
                print(f"[train] final seed {seed} epoch {epoch:2d}  train_loss {last_loss:.4f}")
        per_seed.append({"seed": seed, "epochs": epochs,
                         "final_train_loss": round(float(last_loss), 5),
                         "weight_mode": "ema" if decay > 0 else "raw"})
        params_list.append(ema if decay > 0 else p)
    if len(params_list) > 1:
        params = {k: np.mean([pp[k] for pp in params_list], axis=0) for k in params_list[0]}
        if not args.quiet:
            print(f"[train] 最终模型：{len(params_list)} 个 seed 权重平均")
    else:
        params = params_list[0]
    return params, per_seed


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练语境→表情小模型并导出 data/emote-model.json")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--checkpoints-dir", type=Path, default=DEFAULT_CHECKPOINTS)
    ap.add_argument("--vocab-size", type=int, default=M.MAX_VOCAB)
    ap.add_argument("--min-df", type=int, default=2)
    ap.add_argument("--hidden", type=int, default=M.HIDDEN)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--patience", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--l2", type=float, default=1e-2)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--cv-mode", choices=("template", "author", "none"), default="template",
                    help="选模口径：template=模板折交叉验证（默认）/ author=作者留一（诊断）/ none=同作者 val")
    ap.add_argument("--augment", dest="augment", action="store_true", default=True,
                    help="启用在线数据增强（默认开）")
    ap.add_argument("--no-augment", dest="augment", action="store_false")
    ap.add_argument("--augment-variants", type=int, default=3,
                    help="每样本预生成的增强变体数（每 epoch 采样 1 个）")
    ap.add_argument("--class-weight-mode", choices=("invfreq", "sqrt", "uniform"),
                    default="invfreq", help="类别权重方案")
    ap.add_argument("--label-smoothing", type=float, default=0.0,
                    help="label smoothing ε（0 关闭）")
    ap.add_argument("--feature-dropout", type=float, default=0.0,
                    help="训练时按样本随机丢弃特征比例（0 关闭）")
    ap.add_argument("--none-boost", type=float, default=1.0,
                    help="none 类权重放大倍数")
    ap.add_argument("--seed-ensemble", type=int, default=1,
                    help="最终模型多 seed 权重平均个数")
    ap.add_argument("--ema-decay", type=float, default=0.0,
                    help="EMA 权重平均衰减（0=关闭；CV 会同时评估 raw 与 EMA，按 CV 选优）")
    ap.add_argument("--round-tag", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.hidden != M.HIDDEN:
        print(f"[train] 错误：hidden 固定为 {M.HIDDEN}（设备预算硬上限），收到 {args.hidden}",
              file=sys.stderr)
        return 1
    if args.vocab_size > M.MAX_VOCAB:
        print(f"[train] 错误：vocab 上限 {M.MAX_VOCAB}（设备预算硬上限），收到 {args.vocab_size}",
              file=sys.stderr)
        return 1
    if args.lr > 1e-3:
        print(f"[train] 提示：迭代期建议 lr<=1e-3（当前 {args.lr}），继续按命令行执行",
              file=sys.stderr)

    tracemalloc.start()
    t_start = time.perf_counter()

    assets = M.load_assets(args.assets)
    classes = assets["classes"]
    class_index = {c: i for i, c in enumerate(classes)}
    num_classes = len(classes)
    none_idx = class_index[M.NONE_LABEL]

    # ---- 数据：CV 模式用 all.jsonl + folds；否则用同作者 train/val ----
    same_author_val = None
    if args.cv_mode == "none":
        train_path = args.data_dir / "train.jsonl"
        val_path = args.data_dir / "val.jsonl"
        for p in (train_path, val_path):
            if not p.exists():
                print(f"[train] 错误：缺少 {p}，先跑 prepare.py", file=sys.stderr)
                return 1
        train_rows = load_jsonl(train_path)
        val_rows = load_jsonl(val_path)
        for name, rows in (("train", train_rows), ("val", val_rows)):
            bad = [r for r in rows if r["label"] not in class_index]
            if bad:
                print(f"[train] 错误：{name} 有 {len(bad)} 条 label 非法", file=sys.stderr)
                return 1
        all_rows = train_rows
        same_author_val = val_rows
    else:
        all_path = args.data_dir / "all.jsonl"
        if not all_path.exists():
            print(f"[train] 错误：缺少 {all_path}，先跑 prepare.py（会生成 all.jsonl 与 folds/）",
                  file=sys.stderr)
            return 1
        all_rows = load_jsonl(all_path)
        bad = [r for r in all_rows if r["label"] not in class_index]
        if bad:
            print(f"[train] 错误：all.jsonl 有 {len(bad)} 条 label 非法", file=sys.stderr)
            return 1
        val_path = args.data_dir / "val.jsonl"
        if val_path.exists():
            same_author_val = load_jsonl(val_path)

    texts = [r["text"] for r in all_rows]
    y_all = np.asarray([class_index[r["label"]] for r in all_rows], dtype=np.int64)
    all_ids = np.arange(len(all_rows), dtype=np.int64)

    t_vocab = time.perf_counter()
    # 词表只用训练语料（holdout 不参与）；CV 模式下折间共享词表，属常规做法
    vocab, vocab_stats = build_vocab(texts, y_all, num_classes,
                                     max_vocab=args.vocab_size, min_df=args.min_df)
    vocab_index = {g: i for i, g in enumerate(vocab)}
    vocab_sec = time.perf_counter() - t_vocab

    t_feat = time.perf_counter()
    base_x, variants = build_features(texts, vocab_index, len(vocab),
                                      args.augment, args.augment_variants, args.seed)
    feat_sec = time.perf_counter() - t_feat

    cw = class_weights(y_all, num_classes, mode=args.class_weight_mode,
                       none_idx=none_idx, none_boost=args.none_boost)
    sw_all = cw[y_all]

    cv_result = None
    final_epochs = args.epochs
    folds: list[dict] = []
    t_train = time.perf_counter()

    if args.cv_mode == "none":
        # 兼容模式：同作者 train/val 早停
        params = M.init_params(len(vocab), args.hidden, num_classes,
                               np.random.default_rng(args.seed))
        adam = M.Adam(params, lr=args.lr)
        train_ids = np.arange(len(train_rows), dtype=np.int64)
        val_ids = np.arange(len(train_rows), len(all_rows), dtype=np.int64)
        best = {"score": (-1.0, -math.inf), "params": None, "epoch": 0}
        no_improve = 0
        history = []
        for epoch in range(1, args.epochs + 1):
            rng = np.random.default_rng((args.seed, 7001, epoch))
            train_loss = train_epoch(params, adam, base_x, variants, train_ids,
                                     y_all, sw_all, rng, args)
            probs = predict_rows(base_x, params, val_ids)
            v = eval_metrics(probs, y_all[val_ids], num_classes, none_idx)
            history.append({"epoch": epoch, "train_loss": round(float(train_loss), 5),
                            "val_acc1": round(v["acc1"], 5), "val_acc3": round(v["acc3"], 5)})
            improved = v["acc1"] > best["score"][0] + 1e-9
            if improved:
                best = {"score": (v["acc1"], -v["acc3"]),
                        "params": {k: vv.copy() for k, vv in params.items()},
                        "epoch": epoch}
                no_improve = 0
            else:
                no_improve += 1
            if not args.quiet:
                print(f"[train] epoch {epoch:2d} train_loss {train_loss:.4f} "
                      f"val_acc1 {v['acc1']:.4f} val_acc3 {v['acc3']:.4f}"
                      f"{' *' if improved else ''}")
            if args.patience > 0 and no_improve >= args.patience:
                break
        params = best["params"]
        final_epochs = best["epoch"]
        cv_result = {"mode": "none", "history": history, "best_epoch": best["epoch"],
                     "best_metrics": eval_metrics(predict_rows(base_x, params, val_ids),
                                                  y_all[val_ids], num_classes, none_idx),
                     "per_fold": []}
        train_sec = time.perf_counter() - t_train
    else:
        folds = load_folds(all_rows, args.data_dir, args.cv_mode)
        if not args.quiet:
            print(f"[train] CV 模式 {args.cv_mode}，{len(folds)} 折，"
                  f"每折 val={[len(f['val_ids']) for f in folds]} 条")
        cv_result = run_cv(args, folds, base_x, variants, y_all, sw_all,
                           num_classes, none_idx, len(vocab))
        cv_result["mode"] = args.cv_mode
        final_epochs = max(1, cv_result["best_epoch"])
        if not args.quiet:
            bm = cv_result["best_metrics"]
            print(f"[train] CV 最优 epoch={cv_result['best_epoch']}  "
                  f"mean acc1={bm['acc1']:.4f} acc3={bm['acc3']:.4f} noneF1={bm['none_f1']:.4f}")
        params, per_seed_final = train_final(args, all_ids, base_x, variants,
                                             y_all, sw_all, num_classes, len(vocab),
                                             final_epochs,
                                             use_ema=(cv_result.get("variant") == "ema"))
        cv_result["final_train"] = {
            "epochs": final_epochs,
            "rows": int(len(all_rows)),
            "seeds": per_seed_final,
            "augment": bool(args.augment),
            "augment_variants": int(args.augment_variants) if args.augment else 0,
        }
        train_sec = time.perf_counter() - t_train

    train_probs = predict_matrix(base_x, params)
    train_acc1 = M.accuracy_at_k(train_probs, y_all, 1)
    train_acc3 = M.accuracy_at_k(train_probs, y_all, 3)
    cv_metrics = cv_result["best_metrics"]

    # 同作者 val 参考（最终模型已在全量训练，指标偏乐观）
    same_val_metrics = None
    if same_author_val:
        val_texts = {r["text"] for r in same_author_val}
        val_ids = np.asarray([i for i, r in enumerate(all_rows) if r["text"] in val_texts],
                             dtype=np.int64)
        if len(val_ids):
            same_val_metrics = eval_metrics(predict_rows(base_x, params, val_ids),
                                            y_all[val_ids], num_classes, none_idx)

    # ---- 导出：权重先 5 位小数取整，parity 用取整后的权重计算（与 JS 加载的一致） ----
    model_json = {
        "schema": M.SCHEMA,
        "task": M.TASK,
        "trained_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "dataset": {
            "train": int(len(all_rows)),
            "val": int(len(same_author_val)) if same_author_val else 0,
            "holdout": 0,  # eval.py 在最终评估时写入真实条数
            "seed": args.seed,
            "source": "synthetic-authored-v1",
        },
        "classes": classes,
        "input": {
            "normalize": M.NORMALIZE_NAME,
            "max_chars": M.MAX_CHARS,
            "ngram_order": list(M.NGRAM_ORDER),
            "vocab": vocab,
            "feature_value": M.FEATURE_VALUE,
        },
        "model": {
            "type": M.MODEL_TYPE,
            "hidden": args.hidden,
            "w1": [[M.round5(v) for v in row] for row in params["w1"]],
            "b1": [M.round5(v) for v in params["b1"]],
            "w2": [[M.round5(v) for v in row] for row in params["w2"]],
            "b2": [M.round5(v) for v in params["b2"]],
        },
        "metrics": {
            "val_acc1": M.round5(cv_metrics["acc1"]),
            "val_acc3": M.round5(cv_metrics["acc3"]),
            "train_acc1": M.round5(train_acc1),
            "none_precision": M.round5(cv_metrics["none_precision"]),
            "none_recall": M.round5(cv_metrics["none_recall"]),
            "holdout_acc1": 0.0,
            "holdout_acc3": 0.0,
            "holdout_none_f1": 0.0,
            "baseline_acc1": 0.0,
            "baseline_acc3": 0.0,
        },
        "parity": [],
    }

    export_params = M.params_from_model(model_json)
    parity_rows = select_parity_rows(all_rows, classes, limit=12)
    for row in parity_rows:
        probs = M.predict_probs_texts([row["text"]], vocab, export_params)[0]
        model_json["parity"].append({
            "text": row["text"],
            "probs": [M.round5(v) for v in probs],
        })
    for item in model_json["parity"]:
        s = sum(item["probs"])
        if abs(s - 1.0) > 1e-3:
            print(f"[train] 错误：parity probs 和 {s} 偏离 1", file=sys.stderr)
            return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    size_bytes = M.dump_model_json(model_json, args.out)
    errs = M.validate_model_json(model_json, size_bytes=size_bytes, expected_classes=classes)
    if errs:
        print("[train] 模型 JSON 契约校验失败：", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        return 1

    # 训练产物落 checkpoint（不提交）
    args.checkpoints_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.checkpoints_dir / "best.npz",
        w1=params["w1"], b1=params["b1"], w2=params["w2"], b2=params["b2"],
        vocab=np.asarray(vocab, dtype=object), classes=np.asarray(classes, dtype=object),
        y_all=y_all,
    )

    total_sec = time.perf_counter() - t_start
    tracemalloc_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru_maxrss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":  # Linux 上 ru_maxrss 单位是 KB
        ru_maxrss *= 1024

    history_path = args.checkpoints_dir / "history.jsonl"
    previous_runs = 0
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as f:
            previous_runs = sum(1 for line in f if line.strip())
    round_tag = args.round_tag or f"r{previous_runs + 1}"

    stats_path = args.data_dir / "stats.json"
    data_hashes = {}
    if stats_path.exists():
        try:
            data_hashes = json.loads(stats_path.read_text(encoding="utf-8")).get("hashes", {})
        except json.JSONDecodeError:
            data_hashes = {}

    summary = {
        "round_tag": round_tag,
        "finished_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "mode": "cv" if args.cv_mode != "none" else "single-split",
        "config": {
            "vocab_size": args.vocab_size,
            "min_df": args.min_df,
            "hidden": args.hidden,
            "epochs": args.epochs,
            "patience": args.patience,
            "lr": args.lr,
            "l2": args.l2,
            "batch_size": args.batch_size,
            "class_weight_mode": args.class_weight_mode,
            "none_boost": args.none_boost,
            "label_smoothing": args.label_smoothing,
            "feature_dropout": args.feature_dropout,
            "seed_ensemble": args.seed_ensemble,
            "seed": args.seed,
            "cv_mode": args.cv_mode,
            "augment": bool(args.augment),
            "augment_variants": int(args.augment_variants) if args.augment else 0,
            "ema_decay": args.ema_decay,
        },
        "data": {
            "all": len(all_rows),
            "same_author_train": len(train_rows) if args.cv_mode == "none" else None,
            "same_author_val": len(same_author_val) if same_author_val else None,
            "folds": [{"name": f["name"], "train": int(len(f["train_ids"])),
                       "val": int(len(f["val_ids"]))} for f in folds],
            "stats_hashes": data_hashes,
        },
        "vocab": vocab_stats,
        "cv": cv_result,
        "cv_variant": cv_result.get("variant", "raw"),
        "best_epoch": cv_result["best_epoch"],
        "epochs_run": len(cv_result["history"]),
        "history": cv_result["history"],
        "val": {
            "acc1": M.round5(cv_metrics["acc1"]),
            "acc3": M.round5(cv_metrics["acc3"]),
            "none_precision": M.round5(cv_metrics["none_precision"]),
            "none_recall": M.round5(cv_metrics["none_recall"]),
            "none_f1": M.round5(cv_metrics["none_f1"]),
            "macro_f1": M.round5(cv_metrics["macro_f1"]),
            "source": "cv-mean" if args.cv_mode != "none" else "same-author-val",
        },
        "same_author_val": same_val_metrics,
        "train_acc1": M.round5(train_acc1),
        "train_acc3": M.round5(train_acc3),
        "final_epochs": final_epochs,
        "timing_sec": {
            "vocab": round(vocab_sec, 3),
            "featurize": round(feat_sec, 3),
            "train": round(train_sec, 3),
            "total": round(total_sec, 3),
        },
        "model": {
            "path": str(args.out),
            "size_bytes": size_bytes,
            "vocab": len(vocab),
            "hidden": args.hidden,
            "parity": len(model_json["parity"]),
        },
        "resources": {
            "train_sec": round(train_sec, 3),
            "total_sec": round(total_sec, 3),
            "peak_rss_bytes": ru_maxrss,
            "peak_rss_mb": round(ru_maxrss / (1024 * 1024), 1),
            "tracemalloc_peak_mb": round(tracemalloc_peak / (1024 * 1024), 1),
            "model_bytes": size_bytes,
        },
    }
    (args.checkpoints_dir / "train-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print(f"[train] 轮次 {round_tag}（{summary['mode']}）：best_epoch={cv_result['best_epoch']} "
          f"CV mean acc1={cv_metrics['acc1']:.4f} acc3={cv_metrics['acc3']:.4f} "
          f"noneF1={cv_metrics['none_f1']:.4f}；训练集 acc1={train_acc1:.4f}")
    print(f"[train] 词表 {len(vocab)}/{vocab_stats['unique_grams']}（min_df={args.min_df}），"
          f"训练 {train_sec:.1f}s + 词表/特征 {vocab_sec:.1f}s/{feat_sec:.1f}s，"
          f"总 {total_sec:.1f}s")
    print(f"[train] 已导出 {args.out}（{size_bytes} 字节，{len(model_json['parity'])} 条 parity），"
          f"契约校验 OK")
    print(f"[train] 资源：训练 {train_sec:.1f}s / 总 {total_sec:.1f}s，"
          f"峰值 RSS {ru_maxrss / (1024 * 1024):.1f} MB"
          f"（Python 分配峰值 {tracemalloc_peak / (1024 * 1024):.1f} MB），模型 {size_bytes} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
