"""WB（Western blot）灰度定量 —— 纯算法，无 Qt，可 headless 单测。

对应 ImageJ Gel Analyzer：
  极性/8-bit(to_gray/to_signal) · Subtract Background rolling-ball(rolling_ball_bg)
  · 框带 Measure→IntDen(measure_roi，任意选区掩码) · 局部背景(ring_background)
  · Plot Lanes 泳道密度曲线(lane_profile) · 峰面积(peak_area) · 自动泳道/带(detect_lanes/detect_bands)
  · 归一化到内参(normalize)。
两种量化路径：B 框带 IntDen（measure_roi，默认最稳）/ A 泳道曲线+峰面积（lane_profile+peak_area）。
"""
from __future__ import annotations

import numpy as np


# ---------- 预处理：灰度 + 极性 ----------
def to_gray(img) -> np.ndarray:
    """任意图 → 单通道 float(0..255)。RGB 用感知亮度，RGBA 忽略 alpha。"""
    a = np.asarray(img).astype(np.float64)
    if a.ndim == 3:
        if a.shape[2] >= 3:
            a = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
        else:
            a = a[..., 0]
    return np.clip(a, 0, 255)


def to_signal(gray, polarity: str = "dark_on_light") -> np.ndarray:
    """转成「信号 = 亮」。dark_on_light（浅底暗带，灰度扫描默认）→ 反相 255−gray；
    light_on_dark（深底亮带，如化学发光）→ 原样。"""
    g = np.clip(np.asarray(gray, np.float64), 0, 255)
    return (255.0 - g) if polarity == "dark_on_light" else g


# ---------- 背景扣除 ----------
def rolling_ball_bg(signal, radius: float = 50.0) -> np.ndarray:
    """ImageJ「Subtract Background」同款 rolling-ball 背景估计（返回背景图，net=signal−bg）。"""
    from skimage.restoration import rolling_ball
    return rolling_ball(np.asarray(signal, np.float64), radius=radius)


def ring_background(signal, mask, ring_px: int = 10) -> float:
    """局部背景：ROI 向外扩一圈环带的中位信号（每带各自扣本地背景，对不均匀膜更稳）。"""
    import cv2
    sig = np.asarray(signal, np.float64)
    m = (np.asarray(mask) > 0).astype(np.uint8)
    if m.sum() == 0:
        return 0.0
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ring_px * 2 + 1, ring_px * 2 + 1))
    ring = (cv2.dilate(m, k) > 0) & (m == 0)
    return float(np.median(sig[ring])) if ring.any() else 0.0


# ---------- 测量（B 法：任意选区掩码 → IntDen，对应 ImageJ Measure）----------
def rect_mask(shape, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """矩形 → 二值掩码（矩形是掩码的特例；魔棒/套索/GrabCut 直接给掩码）。"""
    h, w = shape[:2]
    m = np.zeros((h, w), bool)
    m[max(0, int(y0)):min(h, int(y1)), max(0, int(x0)):min(w, int(x1))] = True
    return m


def measure_roi(signal, mask, bg: float = 0.0) -> dict:
    """对掩码内像素测量（ImageJ Set Measurements 同口径）。
    IntDen = Area × Mean = Σ(signal − bg)（clamp≥0）。bg：0=已全局扣背景 / 或传 ring_background 的局部背景。"""
    sig = np.asarray(signal, np.float64)
    m = np.asarray(mask) > 0
    area = int(m.sum())
    if area == 0:
        return {"area": 0, "mean": 0.0, "intden": 0.0, "min": 0.0, "max": 0.0, "raw_mean": 0.0, "bg": float(bg)}
    vals = sig[m]
    net = np.clip(vals - bg, 0, None)
    return {"area": area, "mean": float(net.mean()), "intden": float(net.sum()),
            "min": float(vals.min()), "max": float(vals.max()), "raw_mean": float(vals.mean()), "bg": float(bg)}


# ---------- 泳道曲线 + 峰面积（A 法：ImageJ Plot Lanes）----------
def lane_profile(signal, rect, vertical: bool = True) -> np.ndarray:
    """泳道密度曲线：沿泳道长轴每个位置 = 横截面平均信号（ImageJ Plot Lanes）。
    vertical=True：泳道竖直（蛋白上→下跑），profile 长度 = 泳道高。"""
    x0, y0, x1, y1 = [int(v) for v in rect]
    sub = np.asarray(signal, np.float64)[y0:y1, x0:x1]
    if sub.size == 0:
        return np.zeros(0)
    return sub.mean(axis=1) if vertical else sub.mean(axis=0)


def peak_area(profile, baseline=None) -> float:
    """峰面积 = profile 减基线后 >0 部分的积分（ImageJ 画基线封峰 + 魔棒点峰量面积）。
    baseline=None → 用两端较低值作直基线（UI 里可手拖基线 / 自动 rolling 基线）。"""
    p = np.asarray(profile, np.float64)
    if p.size == 0:
        return 0.0
    base = (min(p[0], p[-1]) if baseline is None else float(baseline))
    return float(np.clip(p - base, 0, None).sum())


# ---------- 自动泳道 / 带检测（保留手动；自动只给初值）----------
def detect_lanes(signal, smooth: int = 7, thr_frac: float = 0.15, min_w: int = 3):
    """竖直投影找泳道：按列求信号和 → 平滑 → 高于阈值的连续列段 = 泳道。返回 [(x0,x1)]。"""
    col = np.asarray(signal, np.float64).sum(axis=0)
    if smooth > 1:
        col = np.convolve(col, np.ones(smooth) / smooth, mode="same")
    if col.max() <= 0:
        return []
    on = col > col.max() * thr_frac
    lanes, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            if j - i >= min_w:
                lanes.append((int(i), int(j)))
            i = j
        else:
            i += 1
    return lanes


def detect_bands(signal, lane_rect, smooth: int = 5, thr_frac: float = 0.2, min_h: int = 2):
    """泳道内水平投影找带：按行求信号和 → 高于阈值的连续行段 = 带。返回该泳道内 [(y0,y1)]（全图坐标）。"""
    x0, y0, x1, y1 = [int(v) for v in lane_rect]
    sub = np.asarray(signal, np.float64)[y0:y1, x0:x1]
    if sub.size == 0:
        return []
    row = sub.sum(axis=1)
    if smooth > 1:
        row = np.convolve(row, np.ones(smooth) / smooth, mode="same")
    if row.max() <= 0:
        return []
    on = row > row.max() * thr_frac
    bands, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            if j - i >= min_h:
                bands.append((y0 + int(i), y0 + int(j)))
            i = j
        else:
            i += 1
    return bands


# ---------- 归一化（对内参带 → 相对表达量）----------
def normalize(intdens, control_index: int):
    """对内参带（如 GAPDH）归一化 → 相对表达量。control 为 0 时原样返回（大声：不 0 除）。"""
    arr = np.asarray(intdens, np.float64)
    if not (0 <= control_index < arr.size):
        return arr.copy()
    c = arr[control_index]
    return arr.copy() if c == 0 else arr / c
