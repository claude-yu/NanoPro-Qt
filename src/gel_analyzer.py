"""复刻 ImageJ Gel Analyzer（Analyze ▸ Gels ▸ Plot Lanes）—— 纯 NumPy，可 headless 单测。

ImageJ 工作流对应：
  选泳道矩形(makeRectangle) → Plot Lanes 得密度曲线(gel_profile = 列/行平均，等价 ImageJ getProfile 的
  column/row-average plot) → 画直线基线封峰(straight_baseline) → wand 点峰量面积(peak_area_baseline)。
口径与 ImageJ 一致：曲线 = 选区内沿「泳道宽度」方向求平均像素值；峰 Area = (曲线−直线基线) 的积分(>0 部分)。
极性：bands 一律朝上成峰（auto：底亮则反相，底暗则原样），与 ImageJ 反相 LUT 凝胶一致。
归一化按用户要求留给 Excel（CSV 只出每带 Area）。
"""
from __future__ import annotations

import numpy as np

import wb_quant as wb


def gel_profile(arr, rect=None, horizontal: bool | None = None, polarity: str = "auto") -> np.ndarray:
    """泳道密度曲线（等价 ImageJ Plot Lanes / getProfile 的均值剖面）。

    rect=(x0,y0,x1,y1)；None=整图。horizontal：泳道走向是否水平（多带横排）；None=按选区长宽比自动
    （宽>高→水平，沿 x 出峰；否则竖直，沿 y 出峰）——与 ImageJ「Are the lanes really horizontal?」一致。
    polarity：auto/dark_on_light/light_on_dark；auto 时底亮(中位>128)则反相，保证带朝上成峰。
    返回 1D 曲线：沿泳道走向每个位置 = 垂直方向的**平均**像素信号（ImageJ 用均值，不是和）。
    """
    a = np.asarray(arr, np.float64)
    if rect is not None:
        x0, y0, x1, y1 = [int(v) for v in rect]
        a = a[y0:y1, x0:x1]
    if a.size == 0:
        return np.zeros(0)
    pol = polarity
    if pol == "auto":
        pol = "dark_on_light" if float(np.median(a)) > 128 else "light_on_dark"
    sig = wb.to_signal(a, pol)
    if horizontal is None:
        horizontal = sig.shape[1] >= sig.shape[0]   # 宽≥高 → 水平泳道
    return sig.mean(axis=0) if horizontal else sig.mean(axis=1)


def _smooth(p, k):
    p = np.asarray(p, np.float64)
    if p.size == 0:                     # 空剖面：直接返回（np.convolve 对空数组会抛 ValueError）
        return p
    k = min(int(k), p.size)             # 核不得超过长度：否则 mode="same" 会把短剖面撑大→带下标越界
    if k <= 1:
        return p
    try:                                # Savitzky-Golay 保峰高/峰宽、灭单像素尖刺（优于移动平均）
        from scipy.signal import savgol_filter
        w = k if k % 2 == 1 else k - 1  # 窗口需奇数且 > polyorder
        if 5 <= w < p.size:
            return savgol_filter(p, w, 3)
    except Exception:
        pass
    return np.convolve(p, np.ones(k) / k, mode="same")


def nearest_valley(prof, i, window: int = 10) -> int:
    """i 附近 ±window 内平滑曲线的局部极小下标（分隔线吸附到谷底用）。"""
    p = _smooth(np.asarray(prof, np.float64), 7)
    n = p.size
    if n == 0:
        return int(i)
    i = max(0, min(int(i), n - 1))      # 先夹紧到 [0,n-1]，防 i≥n 时切片为空 argmin 崩
    lo = max(0, i - window); hi = min(n, i + window + 1)
    return lo + int(np.argmin(p[lo:hi]))


def _peaks_to_bands(p, peaks):
    """峰位列表 → [(left,peak,right)]：相邻峰间谷=峰脚，首/末峰外侧按中位间距开窗。"""
    n = p.size
    spacing = int(np.median(np.diff(peaks))) if len(peaks) > 1 else n
    bands = []
    for idx, pk in enumerate(peaks):
        lo = peaks[idx - 1] if idx > 0 else max(0, pk - spacing)
        hi = peaks[idx + 1] if idx < len(peaks) - 1 else min(n - 1, pk + spacing)
        left = lo + int(np.argmin(p[lo:pk + 1])) if pk > lo else lo
        right = pk + int(np.argmin(p[pk:hi + 1])) if hi > pk else hi
        bands.append((int(left), int(pk), int(right)))
    return bands


def _merge_shallow(p, peaks, merge_frac: float = 0.35):
    """合并被「浅谷」隔开的相邻峰（涂抹/重叠带的肩峰，不是真分界）。
    谷相对两峰的下降量 < merge_frac×(峰高−全局最小) → 视为一条带的肩，并入较高峰。"""
    if len(peaks) < 2:
        return list(peaks)
    gmin = float(p.min())
    merged = [int(peaks[0])]
    for pk in peaks[1:]:
        pk = int(pk); prev = merged[-1]
        lo, hi = (prev, pk) if prev < pk else (pk, prev)
        valley = lo + int(np.argmin(p[lo:hi + 1]))
        hmin = min(float(p[prev]), float(p[pk]))
        drop = hmin - float(p[valley])
        if drop < merge_frac * (hmin - gmin):          # 谷太浅 → 肩峰，合并(保留较高峰)
            if float(p[pk]) > float(p[prev]):
                merged[-1] = pk
        else:
            merged.append(pk)
    return merged


def find_bands(profile, smooth: int = 11, min_prom_frac: float = 0.05, min_gap: int = 15,
               min_width: int = 5, min_area_frac: float = 0.03, merge_frac: float = 0.18,
               abs_floor: float = 2.0, n_bands: int | None = None):
    """沿曲线找峰(带)及其两侧谷(基线锚点)。返回 [(left, peak, right)]（曲线下标）。
    抗噪三件套（消除 1px 噪声尖峰被当成带）：① Savgol 平滑先灭尖刺 ② find_peaks 用
    突出度(≥min_prom_frac×动态范围)+最小峰宽(min_width)+峰间距(min_gap)+局部 wlen ③ 按面积(<min_area_frac×最大峰)/宽度后过滤。
    n_bands：已知带数时宽松检测→按突出度取最强 N 个（不硬调阈值，最稳）。"""
    from scipy.signal import find_peaks
    p = _smooth(np.asarray(profile, np.float64), smooth)
    n = p.size
    if n < 3:
        return []
    R = float(p.max() - p.min()) or 1.0
    wlen = int(max(20, min(n, 80)))     # 突出度基线局部搜索窗，防相邻带互相压制
    # 突出度阈值取「相对动态范围」与「绝对下限」的大者：空膜/平坦区 R 极小时，abs_floor 挡住噪声尖峰
    if n_bands:
        prom = 0.02 * R     # 用户已指定 N：宽松检测以找到淡带（top-N 已限数，无需 abs_floor）
        peaks, props = find_peaks(p, prominence=prom, distance=max(3, min_gap // 2),
                                  width=max(2, min_width // 2), wlen=wlen)
        if peaks.size == 0:
            return [(0, int(np.argmax(p)), n - 1)]
        if peaks.size > n_bands:        # 取突出度最高 N 个，按位置排序（确定性，不迭代阈值）
            keep = np.argsort(props["prominences"])[::-1][:n_bands]
            peaks = np.sort(peaks[keep])
    else:
        prom = max(min_prom_frac * R, abs_floor)
        peaks, props = find_peaks(p, prominence=prom, distance=max(1, min_gap),
                                  width=min_width, wlen=wlen)
        if peaks.size == 0:
            return [(0, int(np.argmax(p)), n - 1)]
        peaks = _merge_shallow(p, [int(x) for x in peaks], merge_frac)   # 合并浅谷肩峰
    bands = _peaks_to_bands(p, [int(x) for x in peaks])
    if not n_bands and len(bands) > 1:  # 自动模式：面积/宽度过滤假峰（N 模式用户已定数，不丢）
        areas = [straight_baseline_area(p, l, r)["area"] for (l, _, r) in bands]
        maxa = max(areas) or 1.0
        kept = [b for b, a in zip(bands, areas)
                if a >= min_area_frac * maxa and (b[2] - b[0]) >= min_width]
        bands = kept or bands
    return bands


def straight_baseline_area(profile, left: int, right: int) -> dict:
    """ImageJ「直线基线封峰」：从 profile[left] 到 profile[right] 连直线为基线，
    峰 Area = Σ(profile − baseline) 的 >0 部分（梯形积分，Δx=1px，与 ImageJ wand 同口径）。"""
    p = np.asarray(profile, np.float64)
    left = max(0, int(left)); right = min(p.size - 1, int(right))
    if left > right:
        left, right = right, left      # 参数反了也归正，不返回倒置区间
    if right <= left:
        return {"area": 0.0, "left": left, "right": right, "peak_h": 0.0}
    xs = np.arange(left, right + 1)
    base = np.linspace(p[left], p[right], xs.size)   # 两谷之间的直线基线
    above = np.clip(p[left:right + 1] - base, 0, None)
    _trapz = getattr(np, "trapezoid", None) or np.trapz   # NumPy≥2.0 改名 trapezoid
    area = float(_trapz(above))                       # 梯形积分（曲线下面积）
    return {"area": area, "left": left, "right": right,
            "peak_h": float(above.max()), "width": int(right - left)}


def lane_peaks(prof, bl, br, dividers):
    """ImageJ Plot Lanes 口径：一条泳道曲线 + 一条直线基线([bl,br] 两端连线) + 分隔竖线(dividers) →
    每段(相邻竖线/端点之间、基线以上)=一个峰。返回 [{l,r,peak,area,peak_h,width}]（曲线下标）。"""
    prof = np.asarray(prof, np.float64); n = prof.size
    if n == 0:
        return []
    bl = max(0, min(int(bl), n - 1)); br = max(0, min(int(br), n - 1))   # 夹紧后再取基线值，防越界/负索引回绕
    a, b = sorted((bl, br))
    if b <= a:
        return []
    denom = (br - bl) or 1
    bounds = [a] + sorted(int(d) for d in dividers if a < int(d) < b) + [b]
    _trapz = getattr(np, "trapezoid", None) or np.trapz
    peaks = []
    for s in range(len(bounds) - 1):
        l, r = int(bounds[s]), int(bounds[s + 1])
        if r <= l:
            continue
        idx = np.arange(l, r + 1)
        base = prof[bl] + (prof[br] - prof[bl]) * (idx - bl) / denom   # 整条泳道的直线基线
        above = np.clip(prof[l:r + 1] - base, 0, None)
        peaks.append({"l": l, "r": r, "peak": l + int(np.argmax(prof[l:r + 1])),
                      "area": float(_trapz(above)), "peak_h": float(above.max()), "width": r - l})
    return peaks


def analyze_gel(arr, rect=None, horizontal: bool | None = None, polarity: str = "auto",
                smooth: int = 15, min_prom_frac: float = 0.08, min_gap: int = 40,
                n_bands: int | None = None):
    """完整复刻：图(+选区) → 曲线 → 找带 → 直线基线 → 每带 Area。返回 (profile, [band dict])。"""
    prof = gel_profile(arr, rect, horizontal, polarity)
    bands = find_bands(prof, smooth=smooth, min_prom_frac=min_prom_frac, min_gap=min_gap, n_bands=n_bands)
    out = []
    for k, (l, pk, r) in enumerate(bands):
        m = straight_baseline_area(prof, l, r)
        m["band"] = k + 1
        m["peak"] = int(pk)
        out.append(m)
    return prof, out


def export_csv(path, bands, rect=None):
    """每带 Area → CSV（归一化留给 Excel）。"""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if rect is not None:
            w.writerow(["# lane_rect", *[int(v) for v in rect]])
        w.writerow(["Band", "Area", "PeakX", "Left", "Right", "Width", "PeakHeight"])
        for b in bands:
            w.writerow([b["band"], "%.3f" % b["area"], b["peak"], b["left"],
                        b["right"], b.get("width", b["right"] - b["left"]), "%.3f" % b["peak_h"]])


# ---------- CLI：python gel_analyzer.py input.tif [x0 y0 x1 y1] [out.csv] ----------
if __name__ == "__main__":
    import sys
    from PIL import Image
    args = sys.argv[1:]
    if not args:
        print("用法: python gel_analyzer.py <图.tif> [x0 y0 x1 y1] [out.csv]"); sys.exit(1)
    path = args[0]
    im = Image.open(path)
    arr = np.asarray(im).astype(np.float64) if im.mode in ("P", "I", "I;16", "F") \
        else wb.to_gray(np.asarray(im.convert("RGB")))
    rect = None
    out = "gel_areas.csv"
    rest = args[1:]
    if len(rest) >= 4 and all(t.lstrip("-").isdigit() for t in rest[:4]):
        rect = tuple(int(t) for t in rest[:4]); rest = rest[4:]
    if rest:
        out = rest[0]
    prof, bands = analyze_gel(arr, rect)
    for b in bands:
        print("Band %d: Area=%.3f (x=%d, %d-%d, w=%d)" % (b["band"], b["area"], b["peak"], b["left"], b["right"], b.get("width", 0)))
    export_csv(out, bands, rect)
    print("→ %d 带, 导出 %s" % (len(bands), out))
