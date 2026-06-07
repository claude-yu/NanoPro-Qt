"""IHC 免疫组化定量 —— 纯算法内核（可 headless 单测）。

复刻 Fiji「Colour Deconvolution」(Gabriel Landini 移植的 Ruifrok & Johnston 2001) +
IHC Profiler (Varghese et al. 2014) 的评分口径，**对齐到逐位一致**。

精确常数/公式来源见 `docs/ihc_alignment.md`（反编译本机 Colour_Deconvolution-3.0.3.jar 抠出，
与 GitHub fiji/Colour_Deconvolution 一致）。任何数值改动必须回该文档核对，禁止凭印象改。

纯 NumPy，**不依赖 skimage**（已从 PyInstaller 打包排除）。

引用：
- Ruifrok AC, Johnston DA (2001) Anal Quant Cytol Histol 23(4):291-299.
- Fiji Colour Deconvolution (sc.fiji.colourDeconvolution.StainMatrix).
- Varghese et al. (2014) IHC Profiler, PLoS ONE 9(5):e96801.
"""
from __future__ import annotations

import numpy as np

# ── 内置染色向量（Fiji colourdeconvolution.txt 逐位）。每条 = 3 个 stain 的 (R,G,B)。
#    第三个 stain (0,0,0) 是占位，build_stain_matrix 会按 Fiji 法推导正交残差。
STAIN_VECTORS: dict[str, list[list[float]]] = {
    "H DAB":            [[0.650000, 0.704000, 0.286000], [0.268000, 0.570000, 0.776000], [0.0, 0.0, 0.0]],
    "H&E":              [[0.644211, 0.716556, 0.266844], [0.092789, 0.954111, 0.283111], [0.0, 0.0, 0.0]],
    "H&E 2":            [[0.490157, 0.768971, 0.410402], [0.046153, 0.842068, 0.537393], [0.0, 0.0, 0.0]],
    "H&E DAB":          [[0.650000, 0.704000, 0.286000], [0.072000, 0.990000, 0.105000], [0.268000, 0.570000, 0.776000]],
    "H AEC":            [[0.650000, 0.704000, 0.286000], [0.274300, 0.679600, 0.680300], [0.0, 0.0, 0.0]],
    "H PAS":            [[0.644211, 0.716556, 0.266844], [0.175411, 0.972178, 0.154589], [0.0, 0.0, 0.0]],
    "Methyl Green DAB": [[0.980000, 0.144316, 0.133146], [0.268000, 0.570000, 0.776000], [0.0, 0.0, 0.0]],
    "Masson Trichrome": [[0.799511, 0.591352, 0.105287], [0.099972, 0.737386, 0.668033], [0.0, 0.0, 0.0]],
    "Alcian blue & H":  [[0.874622, 0.457711, 0.158256], [0.552556, 0.754400, 0.353744], [0.0, 0.0, 0.0]],
    "Giemsa":           [[0.834750, 0.513556, 0.196330], [0.092789, 0.954111, 0.283111], [0.0, 0.0, 0.0]],
}

# 在内置向量集里，每个 stain 的语义名（用于 UI 标签 / 选哪个通道是 DAB）。
STAIN_CHANNEL_NAMES: dict[str, list[str]] = {
    "H DAB":            ["Hematoxylin", "DAB", "Residual"],
    "H&E":              ["Hematoxylin", "Eosin", "Residual"],
    "H&E 2":            ["Hematoxylin", "Eosin", "Residual"],
    "H&E DAB":          ["Hematoxylin", "Eosin", "DAB"],
    "H AEC":            ["Hematoxylin", "AEC", "Residual"],
    "H PAS":            ["Hematoxylin", "PAS", "Residual"],
    "Methyl Green DAB": ["Methyl Green", "DAB", "Residual"],
    "Masson Trichrome": ["Aniline blue", "Fuchsin/Ponceau", "Residual"],
    "Alcian blue & H":  ["Alcian blue", "Hematoxylin", "Residual"],
    "Giemsa":           ["Methylene blue", "Eosin", "Residual"],
}

_LN255 = np.log(255.0)


# ── 染色矩阵：L2 归一 + Fiji 的 2nd/3rd 向量残差推导（StainMatrix.java L110-162）。
def build_stain_matrix(stain: "str | np.ndarray | list") -> np.ndarray:
    """返回 cos[3][3]：每行是一个 stain 的单位 RGB 方向向量（已补全残差、已 floor 防零）。

    `stain` 可为内置名（STAIN_VECTORS 的 key）或自定义的 3×3 (R,G,B)×3 数组。
    """
    if isinstance(stain, str):
        if stain not in STAIN_VECTORS:
            raise KeyError(f"未知染色 {stain!r}；可选：{list(STAIN_VECTORS)}")
        mod = np.array(STAIN_VECTORS[stain], dtype=float)
    else:
        mod = np.asarray(stain, dtype=float).reshape(3, 3)

    cos = np.zeros((3, 3), dtype=float)
    for i in range(3):
        ln = float(np.sqrt((mod[i] ** 2).sum()))
        if ln != 0.0:
            cos[i] = mod[i] / ln

    # stain2 全 0 → 由 stain1 通道轮转 (cosz0, cosx0, cosy0)  (L118-121)
    if cos[1, 0] == 0.0 and cos[1, 1] == 0.0 and cos[1, 2] == 0.0:
        cos[1, 0] = cos[0, 2]
        cos[1, 1] = cos[0, 0]
        cos[1, 2] = cos[0, 1]

    # stain3 全 0 → 逐通道正交补 sqrt(1 - c0^2 - c1^2)，再整体 L2 归一  (L123-152)
    if cos[2, 0] == 0.0 and cos[2, 1] == 0.0 and cos[2, 2] == 0.0:
        for c in range(3):
            s = cos[0, c] ** 2 + cos[1, c] ** 2
            cos[2, c] = 0.0 if s > 1.0 else float(np.sqrt(1.0 - s))
        ln = float(np.sqrt((cos[2] ** 2).sum()))
        if ln != 0.0:
            cos[2] /= ln

    # 任一余弦分量恰为 0 → 置 0.001 防闭式逆除零  (L153-162)
    cos[cos == 0.0] = 0.001
    return cos


def deconvolution_matrix(cos: np.ndarray) -> np.ndarray:
    """Fiji 闭式 3×3 逆 → q (9,) 行主序（StainMatrix.java L169-180）。

    q 的语义：stain i 浓度 = q[i*3]*OD_R + q[i*3+1]*OD_G + q[i*3+2]*OD_B。
    刻意逐行转写 Fiji 而非用 np.linalg.inv，以保证逐位一致。
    """
    cosx0, cosy0, cosz0 = cos[0]
    cosx1, cosy1, cosz1 = cos[1]
    cosx2, cosy2, cosz2 = cos[2]
    q = np.zeros(9, dtype=float)

    A = cosy1 - cosx1 * cosy0 / cosx0
    V = cosz1 - cosx1 * cosz0 / cosx0
    C = cosz2 - cosy2 * V / A + cosx2 * (V / A * cosy0 / cosx0 - cosz0 / cosx0)
    q[2] = (-cosx2 / cosx0 - cosx2 / A * cosx1 / cosx0 * cosy0 / cosx0 + cosy2 / A * cosx1 / cosx0) / C
    q[1] = -q[2] * V / A - cosx1 / (cosx0 * A)
    q[0] = 1.0 / cosx0 - q[1] * cosy0 / cosx0 - q[2] * cosz0 / cosx0
    q[5] = (-cosy2 / A + cosx2 / A * cosy0 / cosx0) / C
    q[4] = -q[5] * V / A + 1.0 / A
    q[3] = -q[4] * cosy0 / cosx0 - q[5] * cosz0 / cosx0
    q[8] = 1.0 / C
    q[7] = -q[8] * V / A
    q[6] = -q[7] * cosy0 / cosx0 - q[8] * cosz0 / cosx0
    return q


def optical_density(rgb_u8: np.ndarray) -> np.ndarray:
    """Fiji OD：-255·ln((v+1)/255)/ln255。输入 uint8 (...,3) → float OD (...,3)。"""
    v = rgb_u8.astype(np.float64)
    return -255.0 * np.log((v + 1.0) / 255.0) / _LN255


def colour_deconvolution(rgb_u8: np.ndarray, stain: "str | np.ndarray" = "H DAB"):
    """Fiji Colour Deconvolution。

    输入 RGB uint8 (H,W,3)。返回 (conc, img8)：
    - conc  : float (3,H,W) 每个 stain 的浓度（= 矩阵·OD，OD 空间，越大=染色越强），IOD/均值用它。
    - img8  : uint8 (3,H,W) Fiji 的 8-bit 输出图（回变换，**越暗=染色越强**），IHC Profiler/可视化用它。
    通道顺序 = STAIN_CHANNEL_NAMES[stain]（H DAB → [Hematoxylin, DAB, Residual]）。
    """
    rgb_u8 = np.asarray(rgb_u8)
    if rgb_u8.ndim != 3 or rgb_u8.shape[2] < 3:
        raise ValueError("colour_deconvolution 需要 (H,W,3) 的 RGB 图")
    cos = build_stain_matrix(stain)
    q = deconvolution_matrix(cos)

    od = optical_density(rgb_u8[:, :, :3])  # (H,W,3)
    od_r, od_g, od_b = od[..., 0], od[..., 1], od[..., 2]

    conc = np.empty((3, *rgb_u8.shape[:2]), dtype=np.float64)
    img8 = np.empty((3, *rgb_u8.shape[:2]), dtype=np.uint8)
    for i in range(3):
        scaled = od_r * q[i * 3] + od_g * q[i * 3 + 1] + od_b * q[i * 3 + 2]
        conc[i] = scaled
        out = np.exp(-(scaled - 255.0) * _LN255 / 255.0)  # 8-bit 回变换
        out = np.minimum(out, 255.0)
        img8[i] = np.floor(out + 0.5).astype(np.uint8)
    return conc, img8


# ── IHC Profiler 评分（作用在 DAB 解卷积后的 8-bit 灰度，越暗=越强；macro L60-114）。
# 分区边界（含端点）：High 0-60 / Positive 61-120 / Low 121-180 / Negative 181-235 / 排除 236-255。
_IHC_ZONES = (
    ("high_positive", 0, 60),
    ("positive", 61, 120),
    ("low_positive", 121, 180),
    ("negative", 181, 235),
)
_IHC_EXCLUDE = (236, 255)
_IHC_TIER_LABEL = {
    "high_positive": "High Positive",
    "positive": "Positive",
    "low_positive": "Low Positive",
    "negative": "Negative",
}


def ihc_profiler(dab_img8: np.ndarray, mask: "np.ndarray | None" = None) -> dict:
    """IHC Profiler 评分。输入 = DAB 解卷积 8-bit 图（colour_deconvolution 的 img8[dab_idx]）。

    返回 {counts, percents, score(0-4), tier, label, excluded}。
    复刻：排除 236-255（近白背景）后算各区占比；score=Σ(权重·占比)；任一区>66% 直接判该区。
    """
    g = np.asarray(dab_img8)
    if mask is not None:
        g = g[np.asarray(mask, dtype=bool)]
    g = g.ravel()
    hist = np.bincount(g, minlength=256)[:256]

    counts = {name: int(hist[lo:hi + 1].sum()) for name, lo, hi in _IHC_ZONES}
    excluded = int(hist[_IHC_EXCLUDE[0]:_IHC_EXCLUDE[1] + 1].sum())
    denom = int(hist.sum()) - excluded  # PixelUnderConsideration
    # 无可测组织（掩膜后为空 / 全是排除的近白背景）→ 不冒充 Negative，明确报"无组织"（fail-loud）。
    if denom <= 0:
        return {"counts": counts, "excluded": excluded, "denom": 0,
                "percents": {name: 0.0 for name, _, _ in _IHC_ZONES},
                "score": 0.0, "tier": None, "label": "—"}
    percents = {name: (counts[name] / denom * 100.0) for name, _, _ in _IHC_ZONES}

    weights = {"high_positive": 4, "positive": 3, "low_positive": 2, "negative": 1}
    score = sum(percents[name] / 100.0 * weights[name] for name, _, _ in _IHC_ZONES)

    # 覆盖规则：任一区 >66% → 直接该区标签
    tier = None
    for name, _, _ in _IHC_ZONES:
        if percents[name] > 66.0:
            tier = name
            break
    if tier is None:
        if score >= 2.95:
            tier = "high_positive"
        elif score >= 1.95:
            tier = "positive"
        elif score >= 0.95:
            tier = "low_positive"
        else:
            tier = "negative"

    return {
        "counts": counts,
        "excluded": excluded,
        "denom": denom,
        "percents": percents,
        "score": float(score),
        "tier": tier,
        "label": _IHC_TIER_LABEL[tier],
    }


def positive_area_fraction(dab_img8: np.ndarray, threshold: int = 180,
                           mask: "np.ndarray | None" = None) -> float:
    """DAB 阳性面积占比。8-bit 解卷积图**越暗=越强** → 阳性 = 像素值 ≤ threshold。

    默认 180 对齐 IHC Profiler 的「Negative 起点 181」（即把 high/positive/low 视为阳性）。
    返回 [0,1]。
    """
    g = np.asarray(dab_img8)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        g = g[m]
    g = g.ravel()
    if g.size == 0:
        return 0.0
    return float((g <= threshold).sum()) / float(g.size)


def integrated_od(dab_conc: np.ndarray, mask: "np.ndarray | None" = None,
                  positive_only: bool = False, dab_img8: "np.ndarray | None" = None,
                  threshold: int = 180) -> dict:
    """IOD（积分光密度）= Σ DAB 浓度。conc = colour_deconvolution 的 conc[dab_idx]（OD 空间）。

    positive_only=True 时只累加阳性像素（需提供 dab_img8 做阈值）。
    返回 {iod, mean_od, area_px}。
    """
    c = np.asarray(dab_conc, dtype=np.float64)
    sel = np.ones(c.shape, dtype=bool)
    if mask is not None:
        sel &= np.asarray(mask, dtype=bool)
    if positive_only:
        if dab_img8 is None:
            raise ValueError("positive_only 需要 dab_img8 做阈值")
        sel &= (np.asarray(dab_img8) <= threshold)
    vals = c[sel]
    area = int(vals.size)
    iod = float(vals.sum()) if area else 0.0
    return {"iod": iod, "mean_od": (iod / area if area else 0.0), "area_px": area}


def h_score(dab_img8: np.ndarray, mask: "np.ndarray | None" = None) -> float:
    """经典 H-score（0-300）。**另立口径**，非 IHC Profiler 原生（后者是 0-4 分）。

    用 IHC Profiler 的强度分区映射 3 档：H = 3·%强 + 2·%阳 + 1·%弱（占比基于排除背景后的像素）。
    """
    r = ihc_profiler(dab_img8, mask=mask)
    p = r["percents"]
    return float(3.0 * p["high_positive"] + 2.0 * p["positive"] + 1.0 * p["low_positive"])


def analyze(rgb_u8: np.ndarray, stain: str = "H DAB", mask: "np.ndarray | None" = None,
            pos_threshold: int = 180) -> dict:
    """一站式：解卷积 + DAB 全套指标。返回 dict（含通道名、IHC Profiler、阳性面积、IOD、H-score）。"""
    conc, img8 = colour_deconvolution(rgb_u8, stain)
    names = STAIN_CHANNEL_NAMES.get(stain, ["Stain1", "Stain2", "Residual"])
    dab_idx = names.index("DAB") if "DAB" in names else 1
    prof = ihc_profiler(img8[dab_idx], mask=mask)
    return {
        "stain": stain,
        "channel_names": names,
        "dab_index": dab_idx,
        "ihc_profiler": prof,
        "positive_area_frac": positive_area_fraction(img8[dab_idx], pos_threshold, mask),
        "iod": integrated_od(conc[dab_idx], mask),
        "iod_positive": integrated_od(conc[dab_idx], mask, positive_only=True,
                                      dab_img8=img8[dab_idx], threshold=pos_threshold),
        "h_score": h_score(img8[dab_idx], mask=mask),
    }


# ── 纤维化 / 胶原定量（P1.2 扩展：目标通道面积、Otsu、背景排除、天狼星红红色面积）。
def tissue_mask(rgb_u8: np.ndarray, white_level: float = 0.90) -> np.ndarray:
    """组织掩膜：排除近白背景（玻片）。max(R,G,B)/255 < white_level 视为组织。返回 bool (H,W)。"""
    mx = np.asarray(rgb_u8)[..., :3].max(axis=2).astype(np.float32) / 255.0
    return mx < white_level


def otsu_threshold(vals_u8: np.ndarray, mask: "np.ndarray | None" = None) -> int:
    """Otsu 阈值（0-255，最大化类间方差）。vals=8-bit。返回 int。"""
    v = np.asarray(vals_u8)
    g = (v[np.asarray(mask, bool)] if mask is not None else v).ravel().astype(np.int64)
    if g.size == 0:
        return 127
    hist = np.bincount(g, minlength=256)[:256].astype(np.float64)
    # 单值通道：类间方差恒 0、argmax 退化到 0 → 阈值=0 会把均匀强染色 ROI 误判 0% 阳性。
    # 直接返回那个唯一值（pos = ch<=value 即整片选中，正确）。空输入退 127。
    nz = np.flatnonzero(hist)
    if nz.size <= 1:
        return int(nz[0]) if nz.size else 127
    p = hist / hist.sum()
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    eps = 1e-12
    sigma_b = np.where(denom > eps, (mu_t * omega - mu) ** 2 / (denom + eps), 0.0)
    return int(np.argmax(sigma_b))


def channel_positive_area(channel8: np.ndarray, tissue: "np.ndarray | None" = None,
                          threshold: "str | int" = "otsu", conc: "np.ndarray | None" = None) -> dict:
    """目标通道阳性面积（8-bit 解卷积图，**暗=强染色** → 阳性 = 值 ≤ 阈值）。

    threshold='otsu'（在组织内自动）或具体 int。tissue=组织掩膜（None=全图）。
    返回 {threshold, area_frac, pos_px, total_px, mean_od}。mean_od 需传 conc（OD 浓度图）。
    """
    ch = np.asarray(channel8)
    tmask = np.asarray(tissue, bool) if tissue is not None else np.ones(ch.shape, bool)
    total = int(tmask.sum())
    if total == 0:
        return {"threshold": 0, "area_frac": 0.0, "pos_px": 0, "total_px": 0, "mean_od": 0.0}
    thr = otsu_threshold(ch[tmask]) if isinstance(threshold, str) else int(threshold)
    pos = tmask & (ch <= thr)
    pos_px = int(pos.sum())
    mod = float(np.asarray(conc)[pos].mean()) if (conc is not None and pos_px) else 0.0
    return {"threshold": int(thr), "area_frac": pos_px / total, "pos_px": pos_px, "total_px": total, "mean_od": mod}


def _rgb_to_hsv(rgb_u8: np.ndarray):
    """向量化 RGB(uint8) → (H 0-360, S 0-1, V 0-1)。"""
    a = np.asarray(rgb_u8)[..., :3].astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    df = mx - mn
    eps = 1e-9
    h = np.zeros_like(mx)
    m = (df > eps) & (mx == r); h[m] = ((g - b)[m] / df[m]) % 6
    m = (df > eps) & (mx == g); h[m] = ((b - r)[m] / df[m]) + 2
    m = (df > eps) & (mx == b); h[m] = ((r - g)[m] / df[m]) + 4
    h *= 60.0
    s = np.where(mx > eps, df / (mx + eps), 0.0)
    return h, s, mx


def rgb2lab(rgb_u8: np.ndarray):
    """纯 NumPy sRGB(0-255) → CIELAB (D65)，与 skimage.color.rgb2lab 同。返回 (L, a, b) float 数组。

    a* = 红-绿轴（红胶原高、黄底≈0），对强度归一，是天狼星红/红色分离的关键通道。
    """
    arr = np.asarray(rgb_u8)[..., :3].astype(np.float64) / 255.0
    m = arr > 0.04045
    arr = np.where(m, ((arr + 0.055) / 1.055) ** 2.4, arr / 12.92)   # 逆 sRGB 伽马
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    X = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    Y = (r * 0.2126729 + g * 0.7151522 + b * 0.0721750) / 1.0
    Z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def f(t):
        d = 6.0 / 29.0
        return np.where(t > d ** 3, np.cbrt(t), t / (3 * d * d) + 4.0 / 29.0)

    fx, fy, fz = f(X), f(Y), f(Z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def sirius_red_area(rgb_u8: np.ndarray, sensitivity: float = 50.0,
                    tissue: "np.ndarray | None" = None) -> dict:
    """天狼星红红色胶原面积（占组织）—— CIELAB **a*−b*（红度−黄度）** 固定阈值法（鲁棒、跨强度，亮场标准口径）。

    天狼星红无解卷积向量。HSV 饱和度法对红胶原 vs 黄底分不开（色相在红处回绕、V 混亮度）→ 改用 Lab：
    a*=红-绿轴、b*=黄-蓝轴；红胶原 a* 高 b* 低 → a*−b* 大；橙组织 a* 高 b* 也高 → 小；黄底 a* 低 → 负。
    （注：曾试 Otsu/R−G 兜底，但实证 R−G 会反向、Otsu 在橙底过选，故定为固定 a*−b* 阈值，见下。）
    思路源自 QuantSeg(bpae027) 用 Lab a*/hue、MDPI 10.11.1585 多器官鲁棒法。

    判据 = **a*−b*（红度减黄度）**：红胶原 a* 高 b* 低 → 大；橙组织 a* 高 b* 也高 → 小；黄底 a* 低 → 负。
    实证在肾纤维化 model/sham 上以「模型胶原>对照」为真值标定（a*−b*>5 给 3.79× 区分，远好于纯 a*/R−G——
    R−G 会反向，因橙组织 R 也远大于 G）。

    sensitivity: 0-100，默认 50（阈值 6）；调高→阈值下移更灵敏(抓更多 faint 胶原)，调低→更严。
    返回 {area_frac, red_px, tissue_px, red_mask, tissue_mask, thr}。

    **性能**：rgb2lab 全图很贵（幂运算+立方根，~300ms）；UI 拖灵敏度时请缓存 `sirius_red_precompute`
    一次，再对每个灵敏度调 `sirius_red_mask`（廉价比较），避免每动一下都重算 Lab。
    """
    return sirius_red_mask(sirius_red_precompute(rgb_u8, tissue), sensitivity)


def sirius_red_precompute(rgb_u8: np.ndarray, tissue: "np.ndarray | None" = None) -> dict:
    """天狼星红昂贵的与灵敏度无关部分（rgb2lab + 组织掩膜），算一次缓存复用。"""
    rgb = np.asarray(rgb_u8)
    L, a, b = rgb2lab(rgb)
    tmask = np.asarray(tissue, bool) if tissue is not None else ~((L > 85) & (np.abs(a) < 8) & (np.abs(b) < 12))
    return {"ab": (a - b), "tissue": tmask, "valid": tmask & (a > 2.0)}   # valid=组织内且偏红侧


def sirius_red_mask(pre: dict, sensitivity: float = 50.0) -> dict:
    """对缓存的预计算施加灵敏度阈值（廉价）。pre=sirius_red_precompute 的结果。"""
    thr = 6.0 - (sensitivity - 50.0) * 0.08             # sens50→6，范围约 [2,10]
    red = pre["valid"] & (pre["ab"] > thr)              # 红度>黄度 且 偏红侧（valid 已含 a>2 与组织）
    tissue_px = int(pre["tissue"].sum()); red_px = int(red.sum())
    return {"area_frac": (red_px / tissue_px if tissue_px else 0.0),
            "red_px": red_px, "tissue_px": tissue_px, "red_mask": red, "tissue_mask": pre["tissue"],
            "thr": float(thr)}


# ── CLI（headless 测试 / 单图分析）。
def _load_rgb(path: str) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def _main(argv: "list[str] | None" = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="IHC 免疫组化定量（对齐 Fiji Colour Deconvolution + IHC Profiler）")
    ap.add_argument("image", help="输入 IHC 图（RGB）")
    ap.add_argument("--stain", default="H DAB", help=f"染色方案，默认 H DAB；可选 {list(STAIN_VECTORS)}")
    ap.add_argument("--pos-threshold", type=int, default=180, help="阳性阈值（8-bit DAB，≤ 为阳性），默认 180")
    ap.add_argument("--save-channels", metavar="PREFIX", help="把分离后的各 stain 8-bit 图存为 PREFIX_<name>.png")
    args = ap.parse_args(argv)

    rgb = _load_rgb(args.image)
    res = analyze(rgb, stain=args.stain, pos_threshold=args.pos_threshold)

    if args.save_channels:
        from PIL import Image
        _, img8 = colour_deconvolution(rgb, args.stain)
        for i, nm in enumerate(res["channel_names"]):
            Image.fromarray(img8[i]).save(f"{args.save_channels}_{nm}.png")

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
