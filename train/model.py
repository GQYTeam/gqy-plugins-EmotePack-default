#!/usr/bin/env python3
"""语境 → 表情 小模型：numpy 参考实现（Python 侧唯一基准）。

本文件是 `model-format.md` 第 4 节推理链的 Python 侧实现，被
`prepare.py` / `train.py` / `eval.py` 共用：

    normalize → 取末尾 120 码点 → char 1/2/3-gram
    → binary 词表特征 → h = tanh(x·w1 + b1) → logits = h·w2 + b2 → softmax

约束：只依赖 numpy，不联网，不读 holdout；除格式校验外不做业务判断。
"""

from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path

import numpy as np

# ---- 契约常量（与 model-format.md 逐字对应） ----
SCHEMA = 1
TASK = "context-emote-clf"
MODEL_TYPE = "mlp-tanh-softmax"
NORMALIZE_NAME = "nfkc-lower-strip-all-whitespace"
MAX_CHARS = 120
NGRAM_ORDER = (1, 2, 3)
FEATURE_VALUE = "binary"
NONE_LABEL = "none"
# 设备预算硬上限（model-format.md 第 0/3 节）：vocab ≤1200、hidden 固定 32、文件 ≤600KB
MAX_VOCAB = 1200
HIDDEN = 32
MAX_MODEL_BYTES = 600 * 1024
MIN_PARITY = 8
METRIC_KEYS = (
    "val_acc1",
    "val_acc3",
    "train_acc1",
    "none_precision",
    "none_recall",
    "holdout_acc1",
    "holdout_acc3",
    "holdout_none_f1",
    "baseline_acc1",
    "baseline_acc3",
)
# 只读的补充评估字段（eval.py 写入，可选；不影响推理与验收）
OPTIONAL_METRIC_KEYS = ("holdout_acc5", "coverage_70_top1_precision")

# 特征/模型小工具用的 dtype
FLOAT = np.float64


# ---------------------------------------------------------------------------
# 1. 文本规范化与 n-gram（决定 Python↔JS parity，改动必须同步 model-format.md）
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """NFKC → 全部小写 → 删除所有空白字符（空格/制表/换行/全角空格）。"""
    if not isinstance(text, str):
        raise TypeError(f"text 必须是 str，收到 {type(text).__name__}")
    lowered = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in lowered if not ch.isspace())


def tail_truncate(text: str, max_chars: int = MAX_CHARS) -> str:
    """取末尾 max_chars 个 Unicode 码点；超长只保留尾部。"""
    if max_chars <= 0:
        return ""
    return text[-max_chars:]


def text_grams(text: str) -> set[str]:
    """normalize + 尾部截断后，按字符起点取 1/2/3-gram 集合。"""
    return grams_from_normalized(tail_truncate(normalize(text)))


def grams_from_normalized(s: str, orders: tuple[int, ...] = NGRAM_ORDER) -> set[str]:
    """从已 normalize + 截断的串取 n-gram 集合（越界只取存在的）。"""
    out: set[str] = set()
    n = len(s)
    for i in range(n):
        for k in orders:
            if i + k <= n:
                out.add(s[i:i + k])
    return out


def encode(text: str, vocab_index: dict[str, int]) -> list[int]:
    """把一条语境编码成命中词表的特征下标（升序，无重复）。"""
    hits = [vocab_index[g] for g in text_grams(text) if g in vocab_index]
    hits.sort()
    return hits


def featurize(texts, vocab_index: dict[str, int], vocab_size: int | None = None) -> np.ndarray:
    """批量编码成 binary 稠密矩阵（numpy，float64）。"""
    n = len(texts)
    if vocab_size is None:
        vocab_size = len(vocab_index)
    x = np.zeros((n, vocab_size), dtype=FLOAT)
    for i, text in enumerate(texts):
        hits = encode(text, vocab_index)
        if hits:
            x[i, hits] = 1.0
    return x


# ---------------------------------------------------------------------------
# 2. 前向 / 反向 / Adam
# ---------------------------------------------------------------------------

def softmax(logits: np.ndarray) -> np.ndarray:
    """数值稳定的 softmax（减 max 后 exp、归一化）。"""
    z = np.asarray(logits, dtype=FLOAT)
    z = z - np.max(z, axis=-1, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=-1, keepdims=True)


def forward(x: np.ndarray, params: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """返回 (hidden 激活, logits)。x 为 0/1 矩阵（batch×vocab）。"""
    z1 = x @ params["w1"] + params["b1"]
    h = np.tanh(z1)
    logits = h @ params["w2"] + params["b2"]
    return h, logits


def init_params(vocab_size: int, hidden: int, num_classes: int, rng: np.random.Generator,
                w1_dense: bool = True) -> dict[str, np.ndarray]:
    """Glorot 风格初始化。w1 始终是稠密矩阵（导出格式要求 [vocab][hidden]）。"""
    del w1_dense  # 保留参数名兼容，词表特征一律稠密
    lim1 = math.sqrt(6.0 / (vocab_size + hidden))
    lim2 = math.sqrt(6.0 / (hidden + num_classes))
    return {
        "w1": rng.uniform(-lim1, lim1, size=(vocab_size, hidden)).astype(FLOAT),
        "b1": np.zeros(hidden, dtype=FLOAT),
        "w2": rng.uniform(-lim2, lim2, size=(hidden, num_classes)).astype(FLOAT),
        "b2": np.zeros(num_classes, dtype=FLOAT),
    }


def softmax_ce(logits: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None,
               label_smoothing: float = 0.0):
    """softmax 交叉熵（可带样本权重 / label smoothing）+ 对 logits 的梯度。

    返回 (loss, dlogits)。loss 为加权平均；dlogits 已含 1/Σw 缩放。
    label_smoothing>0 时软标签 = (1-ε)·onehot + ε/C，用于跨作者口径偏移下的正则化。
    """
    n, num_classes = logits.shape
    p = softmax(logits)
    idx = np.arange(n)
    w = np.ones(n, dtype=FLOAT) if sample_weight is None else np.asarray(sample_weight, dtype=FLOAT)
    denom = float(w.sum())
    if denom <= 0:
        denom = 1.0
    target = np.zeros_like(p)
    target[idx, y] = 1.0
    if label_smoothing > 0:
        target = target * (1.0 - label_smoothing) + label_smoothing / num_classes
        loss = float(-(np.log(p + 1e-12) * target).sum(axis=1).dot(w) / denom)
    else:
        loss = float(-(np.log(p[idx, y] + 1e-12) * w).sum() / denom)
    dlogits = (p - target) * w[:, None] / denom
    return loss, dlogits


def backward(x: np.ndarray, params: dict[str, np.ndarray], h: np.ndarray,
             dlogits: np.ndarray, l2: float = 0.0) -> dict[str, np.ndarray]:
    """反向传播，返回各参数梯度（L2 只加在权重上，不加 bias）。"""
    grads = {}
    grads["w2"] = h.T @ dlogits
    grads["b2"] = dlogits.sum(axis=0)
    dh = dlogits @ params["w2"].T
    dz1 = dh * (1.0 - h * h)
    grads["w1"] = x.T @ dz1
    grads["b1"] = dz1.sum(axis=0)
    if l2:
        grads["w1"] = grads["w1"] + l2 * params["w1"]
        grads["w2"] = grads["w2"] + l2 * params["w2"]
    return grads


class Adam:
    """Adam 优化器（numpy 实现，固定超参、无外部依赖）。"""

    def __init__(self, params: dict[str, np.ndarray], lr: float = 0.02,
                 beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-8):
        self.lr = lr
        self.beta1 = beta1
        self.beta2 = beta2
        self.eps = eps
        self.t = 0
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}

    def step(self, params: dict[str, np.ndarray], grads: dict[str, np.ndarray]) -> None:
        self.t += 1
        b1, b2 = self.beta1, self.beta2
        bc1 = 1.0 - b1 ** self.t
        bc2 = 1.0 - b2 ** self.t
        for k, g in grads.items():
            self.m[k] = b1 * self.m[k] + (1.0 - b1) * g
            self.v[k] = b2 * self.v[k] + (1.0 - b2) * (g * g)
            m_hat = self.m[k] / bc1
            v_hat = self.v[k] / bc2
            params[k] -= self.lr * m_hat / (np.sqrt(v_hat) + self.eps)


# ---------------------------------------------------------------------------
# 3. 预测 / 评估指标（train.py 与 eval.py 共用）
# ---------------------------------------------------------------------------

def predict_probs(x: np.ndarray, params: dict[str, np.ndarray]) -> np.ndarray:
    _, logits = forward(x, params)
    return softmax(logits)


def predict_probs_texts(texts, vocab: list[str], params: dict[str, np.ndarray]) -> np.ndarray:
    """按一段文本列表直接预测（给 parity / 独立核查用）。"""
    index = {g: i for i, g in enumerate(vocab)}
    x = featurize(texts, index, len(vocab))
    return predict_probs(x, params)


def ce_loss(probs: np.ndarray, y: np.ndarray) -> float:
    n = len(y)
    if n == 0:
        return 0.0
    return float(-np.log(probs[np.arange(n), y] + 1e-12).mean())


def accuracy_at_k(probs: np.ndarray, y: np.ndarray, k: int = 1) -> float:
    if len(y) == 0:
        return 0.0
    k = min(k, probs.shape[1])
    topk = np.argsort(-probs, axis=1)[:, :k]
    return float(np.mean([y[i] in topk[i] for i in range(len(y))]))


def per_class_metrics(probs: np.ndarray, y: np.ndarray, num_classes: int) -> dict[str, list]:
    """按类 precision / recall / f1 / support。"""
    pred = np.argmax(probs, axis=1)
    precision, recall, f1, support = [], [], [], []
    for c in range(num_classes):
        tp = int(np.sum((pred == c) & (y == c)))
        fp = int(np.sum((pred == c) & (y != c)))
        fn = int(np.sum((pred != c) & (y == c)))
        p = tp / (tp + fp) if tp + fp > 0 else 0.0
        r = tp / (tp + fn) if tp + fn > 0 else 0.0
        f = 2 * p * r / (p + r) if p + r > 0 else 0.0
        precision.append(p)
        recall.append(r)
        f1.append(f)
        support.append(int(np.sum(y == c)))
    return {"precision": precision, "recall": recall, "f1": f1, "support": support}


def macro_f1(probs: np.ndarray, y: np.ndarray, num_classes: int) -> float:
    """宏平均 F1，只统计 support > 0 的类，避免无样本类把分数拉成 0。"""
    m = per_class_metrics(probs, y, num_classes)
    present = [i for i, s in enumerate(m["support"]) if s > 0]
    if not present:
        return 0.0
    return float(np.mean([m["f1"][i] for i in present]))


def confusion_pairs(y: np.ndarray, probs: np.ndarray, classes: list[str], top_n: int = 15):
    """错误最多的 (真实类, 预测类) 对，按次数降序。"""
    pred = np.argmax(probs, axis=1)
    counts: dict[tuple[int, int], int] = {}
    for t, p in zip(y, pred):
        if int(t) != int(p):
            key = (int(t), int(p))
            counts[key] = counts.get(key, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    out = []
    for (t, p), n in rows[:top_n]:
        out.append({"true": classes[t], "pred": classes[p], "count": int(n)})
    return out


def predict_labels(probs: np.ndarray) -> np.ndarray:
    return np.argmax(probs, axis=1)


# ---------------------------------------------------------------------------
# 4. 资产清单（classes 顺序的唯一来源）
# ---------------------------------------------------------------------------

def load_assets(assets_path: str | Path) -> dict:
    """读 data/assets.json；返回 {"classes": [...], "emotes": [...]}。"""
    path = Path(assets_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    emotes = data.get("emotes") or []
    if not emotes:
        raise ValueError(f"{path} 中没有 emotes[]")
    classes = [e["name"] for e in emotes] + [NONE_LABEL]
    return {"classes": classes, "emotes": emotes, "raw": data}


# ---------------------------------------------------------------------------
# 5. 模型 JSON 的读写与契约校验
# ---------------------------------------------------------------------------

def params_from_model(obj: dict) -> dict[str, np.ndarray]:
    m = obj["model"]
    return {
        "w1": np.asarray(m["w1"], dtype=FLOAT),
        "b1": np.asarray(m["b1"], dtype=FLOAT),
        "w2": np.asarray(m["w2"], dtype=FLOAT),
        "b2": np.asarray(m["b2"], dtype=FLOAT),
    }


def is_finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _check_matrix(errors: list[str], name: str, value, rows: int, cols: int) -> None:
    if not isinstance(value, list) or len(value) != rows:
        errors.append(f"model.{name} 应为长度 {rows} 的数组")
        return
    for i, row in enumerate(value):
        if not isinstance(row, list) or len(row) != cols:
            errors.append(f"model.{name}[{i}] 应为长度 {cols} 的数组")
            return
        for v in row:
            if not is_finite_number(v):
                errors.append(f"model.{name}[{i}] 含非有限数值 {v!r}")
                return


def _check_vector(errors: list[str], name: str, value, length: int) -> None:
    if not isinstance(value, list) or len(value) != length:
        errors.append(f"model.{name} 应为长度 {length} 的数组")
        return
    for v in value:
        if not is_finite_number(v):
            errors.append(f"model.{name} 含非有限数值 {v!r}")
            return


def validate_model_json(obj: dict, size_bytes: int | None = None,
                        expected_classes: list[str] | None = None) -> list[str]:
    """按 model-format.md 第 3 节校验模型 JSON；返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["模型 JSON 顶层必须是对象"]

    if obj.get("schema") != SCHEMA:
        errors.append(f"schema 必须为 {SCHEMA}")
    if obj.get("task") != TASK:
        errors.append(f"task 必须为 {TASK!r}")
    if not isinstance(obj.get("trained_at"), str) or not obj.get("trained_at"):
        errors.append("trained_at 必须是非空字符串")

    dataset = obj.get("dataset") or {}
    if not isinstance(dataset, dict):
        errors.append("dataset 必须是对象")
        dataset = {}
    for key in ("train", "val", "holdout", "seed"):
        if not isinstance(dataset.get(key), int):
            errors.append(f"dataset.{key} 必须是整数")
    if not isinstance(dataset.get("source"), str) or not dataset.get("source"):
        errors.append("dataset.source 必须是非空字符串")

    classes = obj.get("classes")
    if not isinstance(classes, list) or len(classes) != 25:
        errors.append("classes 必须是长度 25 的数组")
    else:
        if len(set(classes)) != 25:
            errors.append("classes 有重复项")
        if not all(isinstance(c, str) and c for c in classes):
            errors.append("classes 必须都是非空字符串")
        if classes[-1] != NONE_LABEL:
            errors.append(f"classes 最后一项必须是 {NONE_LABEL!r}")
        if expected_classes is not None and classes != expected_classes:
            errors.append("classes 顺序与 data/assets.json 不一致（应为 emotes[].name + none）")

    inp = obj.get("input") or {}
    if not isinstance(inp, dict):
        errors.append("input 必须是对象")
        inp = {}
    if inp.get("normalize") != NORMALIZE_NAME:
        errors.append(f"input.normalize 必须为 {NORMALIZE_NAME!r}")
    if inp.get("max_chars") != MAX_CHARS:
        errors.append(f"input.max_chars 必须为 {MAX_CHARS}")
    if inp.get("ngram_order") != list(NGRAM_ORDER):
        errors.append(f"input.ngram_order 必须为 {list(NGRAM_ORDER)}")
    if inp.get("feature_value") != FEATURE_VALUE:
        errors.append(f"input.feature_value 必须为 {FEATURE_VALUE!r}")

    vocab = inp.get("vocab")
    if not isinstance(vocab, list) or not vocab:
        errors.append("input.vocab 必须是非空数组")
        vocab_size = 0
    else:
        vocab_size = len(vocab)
        if vocab_size > MAX_VOCAB:
            errors.append(f"input.vocab 长度 {vocab_size} 超过 {MAX_VOCAB}")
        if any((not isinstance(g, str)) or g == "" for g in vocab):
            errors.append("input.vocab 不允许空串或非字符串")
        if len(set(vocab)) != vocab_size:
            errors.append("input.vocab 有重复 gram")

    mdl = obj.get("model") or {}
    if not isinstance(mdl, dict):
        errors.append("model 必须是对象")
        mdl = {}
    if mdl.get("type") != MODEL_TYPE:
        errors.append(f"model.type 必须为 {MODEL_TYPE!r}")
    hidden = mdl.get("hidden")
    if hidden != HIDDEN:
        errors.append(f"model.hidden 必须等于 {HIDDEN}（预算硬上限，见 model-format.md 第 3 节）")
        hidden = 0
    if vocab_size and hidden:
        _check_matrix(errors, "w1", mdl.get("w1"), vocab_size, hidden)
        _check_vector(errors, "b1", mdl.get("b1"), hidden)
        _check_matrix(errors, "w2", mdl.get("w2"), hidden, 25)
        _check_vector(errors, "b2", mdl.get("b2"), 25)

    metrics = obj.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics 必须是对象")
    else:
        allowed = set(METRIC_KEYS) | set(OPTIONAL_METRIC_KEYS)
        extra = set(metrics) - allowed
        missing = set(METRIC_KEYS) - set(metrics)
        if missing:
            errors.append(f"metrics 缺少字段: {sorted(missing)}")
        if extra:
            errors.append(f"metrics 含未知字段: {sorted(extra)}")
        for key in allowed:
            if key in metrics and not is_finite_number(metrics[key]):
                errors.append(f"metrics.{key} 必须是有限数值")

    parity = obj.get("parity")
    if not isinstance(parity, list) or len(parity) < MIN_PARITY:
        errors.append(f"parity 至少 {MIN_PARITY} 条")
    else:
        for i, item in enumerate(parity):
            if not isinstance(item, dict):
                errors.append(f"parity[{i}] 必须是对象")
                continue
            if not isinstance(item.get("text"), str) or not item.get("text"):
                errors.append(f"parity[{i}].text 必须是非空字符串")
            probs = item.get("probs")
            if not isinstance(probs, list) or len(probs) != 25:
                errors.append(f"parity[{i}].probs 必须是长度 25 的数组")
                continue
            if any(not is_finite_number(v) for v in probs):
                errors.append(f"parity[{i}].probs 含非有限数值")
                continue
            if any(v < 0 for v in probs):
                errors.append(f"parity[{i}].probs 含负数")
            if abs(sum(probs) - 1.0) > 1e-3:
                errors.append(f"parity[{i}].probs 之和 {sum(probs):.6f} 偏离 1")
            if max(probs) <= 0:
                errors.append(f"parity[{i}].probs 全 0")

    if size_bytes is not None and size_bytes > MAX_MODEL_BYTES:
        errors.append(f"模型体积 {size_bytes} 字节超过 {MAX_MODEL_BYTES}（600 KB）")

    return errors


def round5(value: float) -> float:
    """保留 5 位小数；-0.0 归一成 0.0，避免 JSON 出现 -0.0。"""
    v = round(float(value), 5)
    return 0.0 if v == 0 else v


def dump_model_json(obj: dict, path: str | Path) -> int:
    """紧凑写出模型 JSON（UTF-8 中文不转义），返回字节数。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    p = Path(path)
    p.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))
