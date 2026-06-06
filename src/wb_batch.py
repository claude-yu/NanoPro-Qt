"""WB 批量定量引擎 —— 纯算法/无 Qt，可 headless 单测。

多张 WB 图一次跑完，复用已对齐 ImageJ 的 gel_analyzer：
  每图 载入(ImageJ 口径) → 自动框泳道带区(或用传入 rect) → 凝胶分析(分峰+峰脚直线扣背景) → 每带 Area
  → 汇总成长表/宽表 CSV（归一化留 Excel）。
解决 ImageJ 多图要开一堆窗口的痛点：一次调用得到所有图所有带的结构化结果。
"""
from __future__ import annotations

import csv
import os

import numpy as np

import wb_quant as wb
import gel_analyzer as ga


# ---------- 载入（ImageJ 口径，纯 PIL，无 Qt）----------
def load_measure_array(path) -> np.ndarray:
    """调色板(P)图用原始索引（与 ImageJ 测量一致），其余转感知灰度。"""
    from PIL import Image
    im = Image.open(path)
    if im.mode in ("P", "I", "I;16", "F"):
        return np.asarray(im).astype(np.float64)
    return wb.to_gray(np.asarray(im.convert("RGB")))


def auto_polarity(arr) -> str:
    return "dark_on_light" if float(np.median(arr)) > 128 else "light_on_dark"


# ---------- 自动框泳道带区 ----------
def auto_lane_rect(arr, polarity: str = "auto", thr_frac: float = 0.30, pad: int = 6):
    """自动定位「条带所在矩形」：扣全局中位背景后，取信号 > thr_frac×峰值 的像素包围盒，外扩 pad。
    高阈值只圈住暗带本身（不含整片膜背景，避免列均值被稀释），故批量结果接近手动紧框。
    返回 (x0,y0,x1,y1)；找不到则返回整图。"""
    pol = auto_polarity(arr) if polarity == "auto" else polarity
    sig = wb.to_signal(arr, pol)
    net = np.clip(sig - float(np.median(sig)), 0, None)
    H, W = net.shape
    if net.max() <= 0:
        return (0, 0, W, H)
    mask = net > net.max() * thr_frac          # 只留显著的暗带像素
    ys, xs = np.where(mask)
    if xs.size == 0 or ys.size == 0:
        return (0, 0, W, H)
    x0 = max(0, int(xs.min()) - pad); x1 = min(W, int(xs.max()) + 1 + pad)
    y0 = max(0, int(ys.min()) - pad); y1 = min(H, int(ys.max()) + 1 + pad)
    return (x0, y0, x1, y1)


# ---------- 单图 / 批量分析 ----------
def analyze_one(path, lane_rect=None, n_bands=None, polarity: str = "auto") -> dict:
    """单图 → 结果 dict。lane_rect=None 自动框；n_bands=None 自动分峰。异常不抛，记进 error（大声失败）。"""
    name = os.path.basename(str(path))
    try:
        arr = load_measure_array(path)
    except Exception as ex:
        return {"path": str(path), "name": name, "ok": False, "error": "载入失败: %s" % ex,
                "lane_rect": None, "polarity": "", "bands": []}
    pol = auto_polarity(arr) if polarity == "auto" else polarity
    rect = tuple(int(v) for v in lane_rect) if lane_rect is not None else auto_lane_rect(arr, pol)
    try:
        prof, bands = ga.analyze_gel(arr, rect, polarity=pol, n_bands=n_bands)
    except Exception as ex:
        return {"path": str(path), "name": name, "ok": False, "error": "分析失败: %s" % ex,
                "lane_rect": rect, "polarity": pol, "bands": []}
    if not bands:
        return {"path": str(path), "name": name, "ok": False, "error": "未检测到条带",
                "lane_rect": rect, "polarity": pol, "bands": []}
    out = [{"band": b["band"], "area": float(b["area"]), "peak": int(b["peak"]),
            "left": int(b["left"]), "right": int(b["right"]),
            "width": int(b.get("width", b["right"] - b["left"]))} for b in bands]
    return {"path": str(path), "name": name, "ok": True, "error": "",
            "lane_rect": rect, "polarity": pol, "bands": out}


def batch_analyze(paths, n_bands=None, polarity: str = "auto", progress=None) -> list:
    """批量：逐图 analyze_one。progress(i, total, name) 可选回调（刷 UI 用）。"""
    paths = list(paths)
    results = []
    for i, p in enumerate(paths):
        if progress is not None:
            progress(i, len(paths), os.path.basename(str(p)))
        results.append(analyze_one(p, n_bands=n_bands, polarity=polarity))
    return results


# ---------- 导出 ----------
def export_long_csv(results, path):
    """长表：每带一行（Image, Band, Area, Width, PeakX, ok, error）。归一化留 Excel。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Image", "Band", "Area", "Width", "PeakX", "polarity", "ok", "error"])
        for r in results:
            if not r["bands"]:
                w.writerow([r["name"], "", "", "", "", r.get("polarity", ""), int(r["ok"]), r["error"]])
                continue
            for b in r["bands"]:
                w.writerow([r["name"], b["band"], "%.3f" % b["area"], b["width"], b["peak"],
                            r.get("polarity", ""), int(r["ok"]), r["error"]])


def detect_band_boxes(arr, polarity: str = "auto", n_bands=None):
    """每条带各框一个紧致框（=面板「智能检测条带」同款，供批量审核用）。返回 (boxes, horizontal, pol)。"""
    pol = auto_polarity(arr) if polarity == "auto" else polarity
    rx0, ry0, rx1, ry1 = auto_lane_rect(arr, pol)
    horiz = (rx1 - rx0) >= (ry1 - ry0)
    prof = ga.gel_profile(arr, (rx0, ry0, rx1, ry1), horizontal=horiz, polarity=pol)
    bands = ga.find_bands(prof, n_bands=n_bands)
    if not bands:
        return [], horiz, pol
    from wb_quant import to_signal
    s = to_signal(arr, pol); med = float(np.median(s)); pad = 2
    boxes = []
    for (l, pk, r) in bands:
        # 泳道轴两端吸到附近谷底→框端落在背景上，端到端直线基线不骑到邻带（扣背景更准）
        l = ga.nearest_valley(prof, int(l), 8); r = ga.nearest_valley(prof, int(r), 8)
        if r <= l:
            l, r = int(l), int(r) if int(r) > int(l) else int(l) + 1
        if horiz:
            ax0 = rx0 + l; ax1 = rx0 + r
            sub = np.clip(s[ry0:ry1, ax0:ax1] - med, 0, None)
            rows = np.where(sub.max(axis=1) > sub.max() * 0.4)[0] if sub.size and sub.max() > 0 else np.array([])
            by0 = ry0 + max(0, int(rows.min()) - pad) if rows.size else ry0
            by1 = ry0 + min(ry1 - ry0, int(rows.max()) + 1 + pad) if rows.size else ry1
        else:
            by0 = ry0 + l; by1 = ry0 + r
            sub = np.clip(s[by0:by1, rx0:rx1] - med, 0, None)
            cols = np.where(sub.max(axis=0) > sub.max() * 0.4)[0] if sub.size and sub.max() > 0 else np.array([])
            ax0 = rx0 + max(0, int(cols.min()) - pad) if cols.size else rx0
            ax1 = rx0 + min(rx1 - rx0, int(cols.max()) + 1 + pad) if cols.size else rx1
        if ax1 - ax0 >= 4 and by1 - by0 >= 4:
            boxes.append((int(ax0), int(by0), int(ax1), int(by1)))
    return boxes, horiz, pol


def boxes_to_areas(arr, boxes, horizontal: bool, polarity: str):
    """对每个框算一条带的峰面积（端到端直线基线；与面板单框一带同口径）。返回 [area...]。"""
    areas = []
    for box in boxes:
        prof = ga.gel_profile(arr, box, horizontal=horizontal, polarity=polarity)
        if prof.size < 2:
            areas.append(0.0); continue
        areas.append(float(ga.straight_baseline_area(prof, 0, prof.size - 1)["area"]))
    return areas


def export_wide_csv(results, path):
    """宽表：行=图，列=Band1..BandN 的 Area（带数不齐则留空）。便于 Excel 直接除内参。"""
    maxb = max((len(r["bands"]) for r in results), default=0)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Image"] + ["Band%d" % (i + 1) for i in range(maxb)] + ["ok", "error"])
        for r in results:
            areas = ["%.3f" % b["area"] for b in r["bands"]]
            areas += [""] * (maxb - len(areas))
            w.writerow([r["name"]] + areas + [int(r["ok"]), r["error"]])
