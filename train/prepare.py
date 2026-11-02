#!/usr/bin/env python3
"""prepare：合并训练语料 → 校验 → 去重 → 按 label 分层切分 → train/val/stats。

输入：train/data/raw/authored-*.jsonl（**显式排除 holdout.jsonl**，本脚本不读它）。
输出：train/data/train.jsonl、train/data/val.jsonl、train/data/stats.json。

切分：每个 label 内部按 text 排序后固定 seed 洗牌，全局 val 比例 12%（最大余数法
保证 24 类各 1 条也分得到；小类至少 1 条进 val、至多留 1 条给 train）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import model as M

TRAIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = TRAIN_DIR.parent
DEFAULT_RAW_DIR = TRAIN_DIR / "data" / "raw"
DEFAULT_OUT_DIR = TRAIN_DIR / "data"
DEFAULT_ASSETS = REPO_ROOT / "data" / "assets.json"

SEED = 20261102
VAL_FRAC = 0.12
MAX_TEXT_CHARS = 2000  # 与 suggest 动作的 context 上限一致


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _length_stats(lengths: list[int]) -> dict:
    if not lengths:
        return {"count": 0}
    arr = np.asarray(lengths, dtype=np.float64)
    buckets = {
        "<8": int(np.sum(arr < 8)),
        "8-40": int(np.sum((arr >= 8) & (arr < 40))),
        "40-90": int(np.sum((arr >= 40) & (arr < 90))),
        "90-160": int(np.sum((arr >= 90) & (arr <= 160))),
        ">160": int(np.sum(arr > 160)),
    }
    return {
        "count": len(lengths),
        "min": int(arr.min()),
        "mean": round(float(arr.mean()), 2),
        "median": round(float(np.median(arr)), 2),
        "p90": round(float(np.percentile(arr, 90)), 2),
        "max": int(arr.max()),
        "buckets": buckets,
    }


def discover_raw_files(raw_dir: Path) -> list[Path]:
    """只取 authored-*.jsonl；显式断言 holdout.jsonl 不在列表里。"""
    files = sorted(p for p in raw_dir.glob("authored-*.jsonl") if p.is_file())
    for p in files:
        if p.name == "holdout.jsonl":
            raise SystemExit(f"prepare 拒绝读取 holdout：{p}")
    return files


def read_jsonl(path: Path, classes: set[str], errors: list[dict], stats_row: dict) -> list[dict]:
    """读一个语料文件并逐行校验；坏行计入 errors，不中断。"""
    rows: list[dict] = []
    stats_row["lines"] = 0
    stats_row["bad_lines"] = 0
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        stats_row["lines"] += 1
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append({"file": path.name, "line": lineno, "reason": f"JSON 解析失败: {exc}"})
            stats_row["bad_lines"] += 1
            continue
        if not isinstance(obj, dict):
            errors.append({"file": path.name, "line": lineno, "reason": "不是 JSON 对象"})
            stats_row["bad_lines"] += 1
            continue
        text_value = obj.get("text")
        label = obj.get("label")
        source = obj.get("source")
        note = obj.get("note", "")
        reason = None
        if not isinstance(text_value, str) or not text_value.strip():
            reason = "text 缺失/非字符串/为空"
        elif "\n" in text_value or "\r" in text_value:
            reason = "text 含换行符"
        elif len(text_value) > MAX_TEXT_CHARS:
            reason = f"text 长度 {len(text_value)} 超过 {MAX_TEXT_CHARS}"
        elif not isinstance(label, str) or label not in classes:
            reason = f"label 非法: {label!r}"
        elif not isinstance(source, str) or not source.strip():
            reason = "source 缺失/空"
        elif not isinstance(note, str):
            reason = "note 不是字符串"
        if reason:
            errors.append({"file": path.name, "line": lineno, "reason": reason})
            stats_row["bad_lines"] += 1
            continue
        rows.append({
            "text": text_value,
            "label": label,
            "source": source.strip(),
            "note": note,
            "_file": path.name,
            "_line": lineno,
        })
    return rows


def stratified_split(rows: list[dict], classes: list[str], seed: int,
                     val_frac: float = VAL_FRAC) -> tuple[list[dict], list[dict], dict]:
    """按 label 分层切分；返回 (train, val, 切分明细)。"""
    by_label: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_label[r["label"]].append(r)

    n_total = len(rows)
    target_val = int(round(n_total * val_frac))
    base = {}
    remainders = []
    for ci, label in enumerate(classes):
        n = len(by_label.get(label, []))
        if n == 0:
            base[label] = 0
            continue
        q = n * val_frac
        floor = int(math.floor(q))
        base[label] = floor
        remainders.append((q - floor, ci, label))

    # 先给每类至少 1 条（只要该类 ≥2 条），再用最大余数法对齐全局 12%
    for label in classes:
        n = len(by_label.get(label, []))
        if n >= 2 and base[label] == 0:
            base[label] = 1
    assigned = sum(base.values())
    if assigned > target_val:
        ordered = sorted(((base[l], len(by_label.get(l, [])), i, l) for i, l in enumerate(classes)),
                         key=lambda t: (-t[0], t[2]))
        for count, n, _i, label in ordered:
            if assigned <= target_val:
                break
            while assigned > target_val and base[label] > 1:
                base[label] -= 1
                assigned -= 1
    elif assigned < target_val:
        ordered = sorted(remainders, key=lambda t: (-t[0], t[1]))
        idx = 0
        while assigned < target_val and ordered:
            _, _ci, label = ordered[idx % len(ordered)]
            n = len(by_label.get(label, []))
            if base[label] < n - 1:  # 至少给 train 留 1 条
                base[label] += 1
                assigned += 1
            idx += 1
            if idx > 1000:
                break

    rng = np.random.default_rng(seed)
    train_rows: list[dict] = []
    val_rows: list[dict] = []
    detail = []
    for label in classes:
        group = sorted(by_label.get(label, []), key=lambda r: (r["text"], r["source"]))
        n = len(group)
        n_val = base.get(label, 0)
        if n >= 2:
            n_val = max(1, min(n_val, n - 1))
        else:
            n_val = 0
        if n == 0:
            detail.append({"label": label, "total": 0, "train": 0, "val": 0})
            continue
        order = rng.permutation(n)
        val_idx = set(int(i) for i in order[:n_val])
        for i, row in enumerate(group):
            (val_rows if i in val_idx else train_rows).append(row)
        detail.append({"label": label, "total": n, "train": n - n_val, "val": n_val})

    train_rows.sort(key=lambda r: (r["label"], r["text"]))
    val_rows.sort(key=lambda r: (r["label"], r["text"]))
    detail.sort(key=lambda d: classes.index(d["label"]))
    return train_rows, val_rows, {
        "target_val": target_val,
        "classes": detail,
        "by_class_truncated": {label: base.get(label, 0) for label in classes},
    }


def _char_trigrams(text: str) -> set[str]:
    s = M.normalize(text)
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def build_template_folds(rows: list[dict], classes: list[str], n_folds: int = 4,
                         sim_threshold: float = 0.55) -> tuple[list[list[dict]], dict]:
    """按「同类内模板聚类」切 4 折：整簇留出，验证集句式不出现在训练集。

    这是「跨作者泛化」的可用代理：本语料 4 位作者负责的标签互不相交
    （A 只写 01-06 …），作者留一折会让该折 6 个类在训练里完全缺席
    （结构性 acc1 上限仅 0.100），无法用于选模；模板折保留全部 25 类，
    只留出未见过的表达方式，更接近 holdout 的「换个人写」场景。
    """
    by_label: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_label[row["label"]].append(i)

    fold_members: list[list[int]] = [[] for _ in range(n_folds)]
    cluster_stats = {}
    for label in classes:
        idxs = by_label.get(label, [])
        n = len(idxs)
        if n == 0:
            continue
        feats = [_char_trigrams(rows[i]["text"]) for i in idxs]
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in range(n):
            fa = feats[a]
            if not fa:
                continue
            for b in range(a + 1, n):
                fb = feats[b]
                if not fb:
                    continue
                inter = len(fa & fb)
                if inter == 0:
                    continue
                union = len(fa) + len(fb) - inter
                if union and inter / union >= sim_threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra

        clusters: dict[int, list[int]] = defaultdict(list)
        for a in range(n):
            clusters[find(a)].append(idxs[a])
        order = sorted(clusters.values(), key=lambda c: (-len(c), c[0]))
        for cluster in order:
            loads = [len(f) for f in fold_members]
            k = min(range(n_folds), key=lambda j: (loads[j], j))
            fold_members[k].extend(cluster)
        cluster_stats[label] = {"clusters": len(order),
                                "largest": max(len(c) for c in order),
                                "sizes": sorted((len(c) for c in order), reverse=True)[:5]}

    folds = []
    for ids in fold_members:
        rows_f = sorted((rows[i] for i in ids), key=lambda r: (r["label"], r["text"]))
        folds.append(rows_f)
    detail = {
        "mode": "template",
        "n_folds": n_folds,
        "similarity_threshold": sim_threshold,
        "sizes": [len(f) for f in folds],
        "by_class": cluster_stats,
    }
    return folds, detail


def build_opener_folds(rows: list[dict], classes: list[str], n_folds: int = 4,
                       opener_chars: int = 6) -> tuple[list[list[dict]], dict]:
    """按「开头短语」分组切折：同类内开头相同的文本整组留出。

    取 normalize 后去掉 用户:/助手: 前缀的前 opener_chars 个字符作为组 key；
    验证折里会出现训练折没见过的开头/句式，比随机切分更接近「换个人写」。
    """
    def opener(text: str) -> str:
        s = M.normalize(text)
        for pre in ("用户:", "助手:"):
            if s.startswith(pre):
                s = s[len(pre):]
        return s[:opener_chars]

    by_label: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_label[row["label"]].append(i)

    fold_members: list[list[int]] = [[] for _ in range(n_folds)]
    group_stats = {}
    for label in classes:
        idxs = by_label.get(label, [])
        if not idxs:
            continue
        groups: dict[str, list[int]] = defaultdict(list)
        for i in idxs:
            groups[opener(rows[i]["text"])].append(i)
        order = sorted(groups.values(), key=lambda g: (-len(g), g[0]))
        for group in order:
            loads = [len(f) for f in fold_members]
            k = min(range(n_folds), key=lambda j: (loads[j], j))
            fold_members[k].extend(group)
        group_stats[label] = {"groups": len(order), "largest": max(len(g) for g in order)}

    folds = [sorted((rows[i] for i in ids), key=lambda r: (r["label"], r["text"]))
             for ids in fold_members]
    detail = {
        "mode": "opener",
        "n_folds": n_folds,
        "opener_chars": opener_chars,
        "sizes": [len(f) for f in folds],
        "by_class_groups": group_stats,
    }
    return folds, detail


def write_jsonl(path: Path, rows: list[dict]) -> str:
    lines = []
    for r in rows:
        out = {"text": r["text"], "label": r["label"], "source": r["source"], "note": r["note"]}
        lines.append(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _hash_file(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合并/校验/去重/分层切分训练语料")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--val-frac", type=float, default=VAL_FRAC)
    ap.add_argument("--folds", type=int, default=4, help="模板折数量（跨作者泛化代理）")
    ap.add_argument("--fold-sim", type=float, default=0.55, help="模板聚类相似度阈值（3-gram Jaccard）")
    ap.add_argument("--no-folds", action="store_true", help="只做同作者 88/12 切分，不生成折")
    ap.add_argument("--fold-mode", choices=("opener", "similarity"), default="opener",
                    help="折分组方式：opener=开头短语分组（默认，模拟未见句式）/ similarity=3-gram 聚类")
    args = ap.parse_args(argv)

    assets = M.load_assets(args.assets)
    classes = assets["classes"]
    class_set = set(classes)

    files = discover_raw_files(args.raw_dir)
    if not files:
        print(f"[prepare] 错误：{args.raw_dir} 下没有 authored-*.jsonl", file=sys.stderr)
        return 1

    errors: list[dict] = []
    all_rows: list[dict] = []
    raw_stats = []
    for path in files:
        row_stat = {"file": path.name, "sha256": _hash_file(path)}
        rows = read_jsonl(path, class_set, errors, row_stat)
        row_stat["valid"] = len(rows)
        # source 与文件名一致性（只警告，不改数据）
        expected_source = path.stem
        mismatch = sum(1 for r in rows if r["source"] != expected_source)
        if mismatch:
            row_stat["source_mismatch"] = mismatch
            print(f"[prepare] 警告：{path.name} 有 {mismatch} 条 source 与文件名不一致"
                  f"（期望 {expected_source!r}），保留原值", file=sys.stderr)
        row_stat["labels"] = dict(sorted(Counter(r["label"] for r in rows).items()))
        raw_stats.append(row_stat)
        all_rows.extend(rows)

    # 去重：text 完全一致视为重复；同 text 不同 label 记为冲突（保留首条并告警）
    dedup: dict[str, dict] = {}
    duplicates = 0
    conflicts = []
    for row in all_rows:
        prev = dedup.get(row["text"])
        if prev is None:
            dedup[row["text"]] = row
            continue
        duplicates += 1
        if prev["label"] != row["label"]:
            conflicts.append({
                "text": row["text"], "kept": prev["label"], "dropped": row["label"],
                "kept_file": prev["_file"], "dropped_file": row["_file"],
            })

    unique_rows = list(dedup.values())
    if conflicts:
        print(f"[prepare] 警告：{len(conflicts)} 条重复 text 的 label 冲突（保留首条）", file=sys.stderr)

    # 去重后各 label 计数（写报告用）
    pre_split_balance = dict(sorted(Counter(r["label"] for r in unique_rows).items()))
    for label in classes:
        if pre_split_balance.get(label, 0) == 0:
            print(f"[prepare] 警告：label {label} 没有语料", file=sys.stderr)

    train_rows, val_rows, split_detail = stratified_split(unique_rows, classes, args.seed, args.val_frac)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    train_path = args.out_dir / "train.jsonl"
    val_path = args.out_dir / "val.jsonl"
    train_hash = write_jsonl(train_path, train_rows)
    val_hash = write_jsonl(val_path, val_rows)

    # all.jsonl：去重后的全部训练语料（不含 holdout），供交叉验证与最终重训使用
    all_path = args.out_dir / "all.jsonl"
    all_hash = write_jsonl(all_path, unique_rows)

    folds_detail = None
    fold_files = []
    if not args.no_folds and args.folds >= 2:
        if args.fold_mode == "opener":
            fold_rows, folds_detail = build_opener_folds(
                unique_rows, classes, n_folds=args.folds)
        else:
            fold_rows, folds_detail = build_template_folds(
                unique_rows, classes, n_folds=args.folds, sim_threshold=args.fold_sim)
        fold_dir = args.out_dir / "folds"
        fold_dir.mkdir(parents=True, exist_ok=True)
        for old in fold_dir.glob("fold-*.jsonl"):
            old.unlink()
        for k, rows_f in enumerate(fold_rows, start=1):
            fold_path = fold_dir / f"fold-{k}.jsonl"
            write_jsonl(fold_path, rows_f)
            fold_files.append({"file": fold_path.name, "rows": len(rows_f),
                               "sha256": _hash_file(fold_path)})
        folds_detail["files"] = fold_files
        print(f"[prepare] 交叉验证折 {args.folds} 折（{folds_detail['mode']}）："
              f"{[len(r) for r in fold_rows]} 条")

    raw_lengths = [len(r["text"]) for r in unique_rows]
    norm_lengths = [len(M.tail_truncate(M.normalize(r["text"]))) for r in unique_rows]
    truncated = sum(1 for r in unique_rows if len(M.normalize(r["text"])) > M.MAX_CHARS)

    stats = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "seed": args.seed,
        "val_frac": args.val_frac,
        "assets": {"path": str(args.assets), "sha256": _hash_file(args.assets), "classes": len(classes)},
        "raw": {
            "files": raw_stats,
            "total_lines": sum(s["lines"] for s in raw_stats),
            "valid": len(all_rows),
            "duplicates_removed": duplicates,
            "label_conflicts": len(conflicts),
            "conflict_examples": conflicts[:5],
            "error_count": len(errors),
            "error_examples": errors[:10],
            "holdout_read": False,
        },
        "split": {
            "train": len(train_rows),
            "val": len(val_rows),
            "train_ratio": round(len(train_rows) / len(unique_rows), 4),
            "val_ratio": round(len(val_rows) / len(unique_rows), 4),
            "target_val": split_detail["target_val"],
            "by_class": split_detail["classes"],
            "pre_split_balance": pre_split_balance,
        },
        "length": {
            "raw_chars": _length_stats(raw_lengths),
            "normalized_codepoints": _length_stats(norm_lengths),
            "truncated_to_max_chars": truncated,
            "max_chars": M.MAX_CHARS,
        },
        "hashes": {"train_sha256": train_hash, "val_sha256": val_hash, "all_sha256": all_hash},
        "folds": folds_detail,
    }
    stats_path = args.out_dir / "stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[prepare] 读取 {len(files)} 个 authored 文件，{len(all_rows)} 条有效行，"
          f"去重 {duplicates} 条，坏行 {len(errors)} 条")
    print(f"[prepare] train={len(train_rows)} val={len(val_rows)} "
          f"(val={len(val_rows) / max(1, len(unique_rows)):.1%})，"
          f"长度均值 {stats['length']['raw_chars']['mean']}，截断样本 {truncated}")
    print(f"[prepare] 写出 {train_path}、{val_path}、{stats_path}")
    if errors:
        print(f"[prepare] 注意：{len(errors)} 个坏行已跳过，示例：{errors[:3]}", file=sys.stderr)
    if conflicts:
        print(f"[prepare] 注意：{len(conflicts)} 条 label 冲突已按首条处理", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
