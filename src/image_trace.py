"""image_trace —— 位图 → 矢量 SVG「图像描摹」引擎核心（对齐 Adobe Illustrator 图像描摹）。

纯算法层，**不依赖 PySide6 widget**（便于离屏单测）。产物是干净的 `<path fill d>` SVG 字符串，
直接喂 `svg_io.parse_svg` → `build_items` → 登记 kind='vector' 层（编辑/导出 SVG/PDF 全部复用现成基础设施）。

两个引擎，同一签名 `trace_to_svg(image, params) -> (svg_str, stats)`，可一键切换：
- **vtracer**（首选）：visioncortex/vtracer，MIT，Rust 核 + win_amd64 wheel；原生彩色分层 tracing，
  贝塞尔平滑，输出每色块一条 `<path fill d>`（实测喂 svg_io 0 只读回退）。
- **diy**（兜底）：纯 numpy + cv2（已打包，Apache/BSD），颜色量化 → 逐簇掩膜 → findContours(RETR_CCOMP，
  含孔洞) → approxPolyDP 简化 → 可选 Catmull-Rom 贝塞尔平滑；自拼 SVG。零新增依赖。

面板参数对齐（见 TraceParams 与 docstring）：模式(彩/灰/黑白)、颜色数 N、受限调板、路径松紧、边角、
杂色去噪、方法(相邻/重叠)、创建(填色/描边)、曲线类型(样条/多边形/像素)。
vtracer 无原生「颜色数 N / 受限调板 / 灰度」→ 在本模块上游用 PIL 预量化补齐（缺口全收敛在这一层）。

fail-loud：trace_to_svg 返回 stats（引擎/色数/路径数/耗时/缩放/量化前后/降级原因/空产物/碎裂告警/
diy丢弃计数），调用方据此报数，绝不静默（铁律②）。
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, replace as _dc_replace
from typing import Optional

import numpy as np

try:
    import vtracer  # MIT，Rust pyd；缺失时自动降级 diy（VTRACER_AVAILABLE=False）
    VTRACER_AVAILABLE = True
except Exception:  # noqa: BLE001 —— 冻结包未收齐 pyd 等任何导入异常都降级，不让整个模块崩
    vtracer = None
    VTRACER_AVAILABLE = False


# ============== 描摹前文字删除（MSER 检测 → 预览框确认 → inpaint 抹除）==============
# 设计：只删【聚成横排文字行】的小连通域（≥min_chars 个字符横向排列）；孤立小块(数据点/符号/单标签)
# 不成行 → 不删（守住"每个素材都能描边"）。删除不可逆 → UI 必须先把框给用户看、可增删，再 inpaint。
def _nms_contain(boxes, contain=0.7):
    """去掉被另一框大面积包含的嵌套框（MSER 同字符多层）。boxes=(x,y,w,h)。"""
    out = []
    boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)  # 大→小
    for x, y, w, h in boxes:
        a = w * h
        dup = False
        for X, Y, W, H in out:
            ix = max(0, min(x + w, X + W) - max(x, X)); iy = max(0, min(y + h, Y + H) - max(y, Y))
            if ix * iy >= contain * a:   # 本框大部分落在已留框内 → 嵌套，丢
                dup = True; break
        if not dup:
            out.append((x, y, w, h))
    return out


def _cluster_text_lines(boxes, min_chars=2):
    """把字符候选按【同一行 + 横向相邻】贪心聚成文字行；成员数 ≥min_chars 才算文字行（孤立的不算）。"""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    used = [False] * len(boxes)
    lines = []
    for i in range(len(boxes)):
        if used[i]:
            continue
        group = [boxes[i]]; used[i] = True
        changed = True
        while changed:
            changed = False
            gy = sum(b[1] + b[3] / 2 for b in group) / len(group)
            gh = sum(b[3] for b in group) / len(group)
            gx0 = min(b[0] for b in group); gx1 = max(b[0] + b[2] for b in group)
            for j in range(len(boxes)):
                if used[j]:
                    continue
                x2, y2, w2, h2 = boxes[j]; c2 = y2 + h2 / 2
                if abs(c2 - gy) < 0.6 * gh and 0.5 < h2 / gh < 2.0 \
                        and x2 <= gx1 + 1.6 * gh and x2 + w2 >= gx0 - 1.6 * gh:   # 同行+横向近
                    group.append(boxes[j]); used[j] = True; changed = True
        if len(group) >= min_chars:
            x0 = min(b[0] for b in group); y0 = min(b[1] for b in group)
            x1 = max(b[0] + b[2] for b in group); y1 = max(b[1] + b[3] for b in group)
            lines.append((int(x0), int(y0), int(x1), int(y1)))
    return lines


def detect_text_regions(rgb, min_h=6, max_h=None, min_chars=2):
    """MSER 检测【横排文字行】→ 返回行级 bbox 列表 [(x0,y0,x1,y1)]。纯 cv2，零依赖。

    只标"成行的小字符"；孤立小连通域(数据点/图例符号/单字母标签)不成行 → 不返回(不删)。
    用户在面板上看框、可增删，确认后再 remove_text inpaint —— 守住"每个素材都能描边"且可逆。
    max_h=None → 按图高自适应(max(48, 4%图高))，防高分辨率大图字号超 48px 被全过滤而漏检。
    """
    import cv2
    gray = cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2GRAY)
    if max_h is None:
        max_h = max(48, int(0.04 * gray.shape[0]))   # 随图高自适应（下游宽高比/密度/成行护栏仍兜底防误检）
    cands = []
    for inv in (gray, 255 - gray):                     # 正反都跑：抓暗字/亮字
        mser = cv2.MSER_create()
        try:
            mser.setMinArea(15); mser.setMaxArea(int(0.02 * gray.size))
        except Exception:  # noqa: BLE001
            pass
        regions, _ = mser.detectRegions(inv)
        for r in regions:
            x, y, w, h = cv2.boundingRect(r.reshape(-1, 1, 2))
            if not (min_h <= h <= max_h):
                continue
            if not (0.06 <= w / max(1, h) <= 2.4):     # 字符细高/方；过宽的色块排除
                continue
            if len(r) / float(w * h + 1) < 0.12:       # 太稀疏(噪声)
                continue
            cands.append((int(x), int(y), int(w), int(h)))
    cands = _nms_contain(cands)
    return _cluster_text_lines(cands, min_chars)


def remove_text(rgb, boxes, pad=2):
    """按确认后的文字框 inpaint 抹除（Telea 用周围背景填，扁平图背景多纯白→填得干净）。返回新图。"""
    import cv2
    if not boxes:
        return rgb.copy()
    h, w = rgb.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for (x0, y0, x1, y1) in boxes:
        x0 = max(0, int(x0) - pad); y0 = max(0, int(y0) - pad)
        x1 = min(w, int(x1) + pad); y1 = min(h, int(y1) + pad)
        mask[y0:y1, x0:x1] = 255
    return cv2.inpaint(np.ascontiguousarray(rgb[..., :3]), mask, 3, cv2.INPAINT_TELEA)


# ============================================================ 参数模型
@dataclass
class TraceParams:
    """图像描摹参数（对齐 Illustrator 图像描摹面板）。滑块均 0–100，内部映射到引擎实参。"""
    mode: str = "color"            # 模式：'color' 彩色 | 'gray' 灰度 | 'bw' 黑白
    n_colors: int = 0              # 颜色数：0=自动（引擎自适应分层）；>0=上游预量化到精确 N 色（2–256）
    palette: Optional[list] = None  # 受限调板：['#rrggbb', ...]；非空时覆盖 n_colors（量化到该色板）
    paths: float = 50.0            # 路径松紧 0–100（高=更贴合原图/更多锚点；低=更简化）
    corners: float = 50.0          # 边角 0–100（高=更多尖角；低=更平滑）
    noise: int = 6                 # 杂色：去除 < noise 像素面积的斑点（filter_speckle）。默认 6——实测裁决：太大(16)会把
                                   # 细描边/箭头/字形当噪点删掉=线稿糊(暗线恢复 16→33% / 6→63%)；6 保细节，路径多些正常
    method: str = "overlapping"    # 方法：'overlapping' 重叠(堆叠/stacked) | 'abutting' 相邻(挖洞/cutout)
    create: str = "fill"           # 创建：'fill' 填色 | 'stroke' 描边（描边仅 diy 支持；vtracer 选描边会降级 diy）
    curve: str = "spline"          # 曲线：'spline' 样条(平滑) | 'polygon' 多边形(尖角) | 'pixel' 像素
    engine: str = "vtracer"        # 引擎：'vtracer'（首选）| 'diy'（自研兜底）
    max_dim: int = 0               # >0：长边缩放到此像素（预览快档）；0=原分辨率
    bw_threshold: int = 128        # 黑白模式阈值（0–255）：≥阈值=白、<阈值=黑（两引擎都尊重，上游预二值化）
    stroke_width: float = 1.0      # 描边创建模式的描边宽
    ignore_white: bool = False     # 透明度：丢弃近白底 path（透明底，便于叠加；对齐 AI「透明度」）
    ignore_colors: Optional[list] = None  # 忽略颜色：['#rrggbb',...]，描摹后丢弃这些色的 path（对齐 AI「忽略颜色」）
    ignore_tol: int = 24           # 忽略颜色容差（sRGB 欧氏距离，含近白）
    snap_lines: bool = False       # 将曲线与线条对齐：近水平/垂直线段吸附为正交直线（对齐 AI「将曲线与线条对齐」）
    group: bool = False            # 默认不打组：所有 path 为独立顶层元素 → 导入后每个图形元素直接可拖/可改色；
                                   # True=包进一个 <g> 整体移动（需撤组才能动单个）。面板「描完打组」复选控制。
    # potrace 档（engine='potrace'，外部 potrace.exe 子进程）参数：暗描边层 / 彩色层 各一组 turdsize+opttolerance
    potrace_turdsize: int = 12     # 暗层去小斑阈值（大→锚点少；>20 会抹文字，守 ≤20）
    potrace_opttolerance: float = 0.8  # 暗层曲线拟合容差（大→更平滑锚点少）
    color_turdsize: int = 10       # 彩色层去小斑
    color_opttolerance: float = 0.6    # 彩色层曲线容差
    alphamax: float = 1.0          # potrace 角点阈值（小=多尖角，大=多平滑）


# 映射常数（在真实样本上标定，见 docs / 任务2；改这里别散落魔法数）
_LEN_THRESH_HI = 10.0   # paths=0   → 最松（少锚点）
_LEN_THRESH_LO = 3.5    # paths=100 → 最紧（多锚点贴合）
_CORNER_HI = 110.0      # corners=0   → corner_threshold 大（少尖角/更平滑）
_CORNER_LO = 20.0       # corners=100 → corner_threshold 小（多尖角）
_GRAY_AUTO_LEVELS = 16  # 灰度自动档默认灰阶数（对齐 AI 灰度默认级数，避免切出大量相近灰层）
_BANDING_WARN_RATIO = 5  # 预量化后 n_paths > N×色数 → 渐变碎裂告警（fail-loud）


def _lerp(a: float, b: float, t01: float) -> float:
    t = max(0.0, min(1.0, t01 / 100.0))
    return a + (b - a) * t


# ============================================================ 输入归一化
def _load_rgb(image) -> np.ndarray:
    """任意输入 → (H,W,3) uint8 RGB。

    接受：文件路径 str / numpy (H,W,3|4) uint8 / PIL.Image。带 alpha → 在白底上合成（描摹图多为不透明白底）。
    """
    if isinstance(image, str):
        from PIL import Image
        arr = np.asarray(Image.open(image).convert("RGBA"), dtype=np.uint8)
    elif isinstance(image, np.ndarray):
        arr = image
    else:  # 鸭子类型当 PIL.Image
        arr = np.asarray(image.convert("RGBA"), dtype=np.uint8)

    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:  # 灰度 → 复制三通道
        return np.repeat(arr[:, :, None], 3, axis=2)
    if arr.shape[2] == 3:
        return np.ascontiguousarray(arr)
    if arr.shape[2] == 4:  # 在白底上 alpha 合成（避免 vtracer 把透明当黑/边缘脏）
        rgb = arr[:, :, :3].astype(np.float32)
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        comp = rgb * a + 255.0 * (1.0 - a)
        return np.ascontiguousarray(np.clip(comp, 0, 255).astype(np.uint8))
    raise ValueError(f"不支持的图像通道数：{arr.shape}")


def _maybe_downscale(rgb: np.ndarray, max_dim: int) -> tuple[np.ndarray, float]:
    """长边 > max_dim 则等比缩小（预览快档）。返回 (缩放后图, scale)；scale<1 表示缩小过。"""
    if max_dim <= 0:
        return rgb, 1.0
    h, w = rgb.shape[:2]
    long_side = max(h, w)
    if long_side <= max_dim:
        return rgb, 1.0
    import cv2
    scale = max_dim / float(long_side)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    out = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    return out, scale


# ============================================================ 上游预量化（补 vtracer 没有的「颜色数/受限调板/灰度」）
def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _count_colors(rgb: np.ndarray) -> int:
    return int(len(np.unique(rgb.reshape(-1, 3), axis=0)))


def _quantize(rgb: np.ndarray, params: TraceParams) -> tuple[np.ndarray, int, bool]:
    """据 params 做预量化。返回 (量化后 RGB, 实际色数, 是否做了量化)。

    优先级：受限调板 > 精确 N 色 > 灰度自动档(量化到 _GRAY_AUTO_LEVELS) > 原样(交引擎自适应)。
    n_actual=0 仅当未量化（彩色自动档）。
    """
    work = rgb
    # 灰度：去饱和（vtracer 无独立灰度模式 → 先转灰再当彩色 trace，调板自然塌成灰阶）
    if params.mode == "gray":
        from PIL import Image
        work = np.asarray(Image.fromarray(work).convert("L").convert("RGB"), dtype=np.uint8)

    # 受限调板：每像素映射到色板最近色（sRGB 欧氏，int32 防平方回绕）
    if params.palette:
        pal = np.array([_hex_to_rgb(c) for c in params.palette], dtype=np.int32)  # (K,3)
        flat = work.reshape(-1, 3).astype(np.int32)                                # int32：255²=65025 不溢出
        d = ((flat[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2)                 # (N,K)
        idx = d.argmin(axis=1)
        out = pal[idx].astype(np.uint8).reshape(work.shape)
        return out, _count_colors(out), True

    # 精确 N 色：PIL median-cut（稳定、无需 sklearn）
    if params.n_colors and params.n_colors > 0:
        out = _pil_quantize(work, params.n_colors)
        return out, _count_colors(out), True

    # 灰度自动档：量化到默认灰阶（避免 vtracer 在连续灰阶上切出大量相近层；对齐 AI 灰度默认级数）
    if params.mode == "gray":
        out = _pil_quantize(work, _GRAY_AUTO_LEVELS)
        return out, _count_colors(out), True

    # 彩色自动档：原样交引擎自适应分层
    return work, 0, False


def _pil_quantize(rgb: np.ndarray, n: int) -> np.ndarray:
    from PIL import Image
    n = max(2, min(256, int(n)))
    q = Image.fromarray(rgb).quantize(colors=n, method=Image.Quantize.MEDIANCUT,
                                      dither=Image.Dither.NONE)
    return np.asarray(q.convert("RGB"), dtype=np.uint8)


# ============================================================ vtracer 引擎
def _vtracer_kwargs(params: TraceParams, prequantized: bool) -> dict:
    """TraceParams → vtracer convert_* kwargs（面板语义对齐，注意多处反向映射）。"""
    kw = {}
    # 模式
    kw["colormode"] = "binary" if params.mode == "bw" else "color"
    # 方法：重叠=stacked（默认堆叠）；相邻=cutout（挖洞，边对边不重叠）
    kw["hierarchical"] = "cutout" if params.method == "abutting" else "stacked"
    # 曲线类型
    kw["mode"] = {"spline": "spline", "polygon": "polygon", "pixel": "none"}.get(params.curve, "spline")
    # 杂色去噪 ≈ 1:1
    kw["filter_speckle"] = max(0, int(params.noise))
    # 路径松紧（反向）：高 paths → 小 length_threshold（更贴合）
    kw["length_threshold"] = round(_lerp(_LEN_THRESH_HI, _LEN_THRESH_LO, params.paths), 3)
    # 边角（反向）：高 corners → 小 corner_threshold（更多尖角）
    kw["corner_threshold"] = int(round(_lerp(_CORNER_HI, _CORNER_LO, params.corners)))
    # 颜色保真：已上游预量化 → 高精度保住调板，layer_difference=16 让 vtracer 合并相近碎层(防渐变 banding 爆路径，
    # 不用 0：0=禁止任何合并→每个量化 band 独立成路径→渐变内容路径暴增，观感与 Illustrator 减色相反)；
    # 自动 → vtracer 默认 6/16。
    if prequantized:
        kw["color_precision"] = 8
        kw["layer_difference"] = 16
    return kw


def _trace_vtracer(rgb: np.ndarray, params: TraceParams, prequantized: bool) -> str:
    """vtracer 引擎：RGB → PNG bytes(内存) → convert_raw_image_to_svg → SVG 字符串（不落盘）。"""
    if not VTRACER_AVAILABLE:
        raise RuntimeError("vtracer 不可用（未安装或冻结包未收齐 .pyd）")
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")  # 无损 PNG，保住预量化的精确色
    svg = vtracer.convert_raw_image_to_svg(buf.getvalue(), img_format="png",
                                           **_vtracer_kwargs(params, prequantized))
    if not svg or "<svg" not in svg:  # fail-loud：引擎吐空/非 SVG（0 路径的合法 SVG 在顶层 trace_to_svg 统一判）
        raise RuntimeError("vtracer 返回空或非法 SVG")
    return svg


# ============================================================ 锐利硬边·BioRender 档（逐色三层分离 cv2 管线）
def _trace_crisp(rgb: np.ndarray, params: TraceParams) -> tuple[str, int, dict]:
    """Illustrator 图像描摹级【锐利硬边】彩色矢量（2026-06-10 ultracode 9-agent 研发 + 实测裁决，方案D骨架+A色量化）。

    对扁平科研图标(BioRender式:纯色大色块+深色细描边线稿)实测 vs vtracer 锐利档：暗线恢复 0.93 vs 0.63、
    25 path/色 vs 962/782(每色块独立可拖可改色)、221ms vs 500ms。代价=posterize 略降饱和(色保真 S比 0.74 vs vtracer 0.93)。

    三层分离(消 washing 根因)：A 暗描边层(gray<dark_th,单一二值,dilate 1px 救 1-2px 细线→暗线恢复 0.41→0.94)；
    B 背景层(纯白/低饱和淡色,丢,不耗色板预算)；C 彩色填充层(单独量化,代表色取簇内饱和≥p70 中位=satp70 抗淡,
    相邻簇 dilate 1px 消白缝→饱和比 0.38→0.74)。逐簇 RETR_CCOMP 外环+内孔,**每连通域独立 evenodd**(防跨域奇偶相消
    把暗线消掉),approxPolyDP 小 eps 硬边不转贝塞尔。fail-loud:drops 报丢弃数。
    """
    import cv2
    h, w = rgb.shape[:2]
    rgb = np.ascontiguousarray(rgb[..., :3])
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv_s = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1]

    # 参数映射（面板滑块 → 管线常数）
    n_levels = params.n_colors if (params.n_colors and params.n_colors >= 2) else 24
    if params.palette:
        n_levels = len(params.palette)
    dark_th = 110; white_th = 235; sat_th = 28
    min_area = max(2, int(params.noise) // 2)        # noise6→3
    _mk = max(0, int(params.noise))                  # 杂色 → 中值滤波核(合并碎色岛/消发花)；0=不滤波
    med_k = 0 if _mk < 3 else min(9, _mk if _mk % 2 == 1 else _mk + 1)  # 奇数 3..9，noise6→7
    dark_min_px = 2
    eps_px = max(0.4, _lerp(1.6, 0.5, params.paths))  # 路径松紧→直边简化 eps（高 paths=更贴）
    seam_close = 1; dark_dilate = 1

    # 深色描边层：暗且【低饱和】才算线稿——高饱和(sat>=150)的深色是有颜色的填充(深品红/深蓝原子)，
    # 入暗层会被单色化丢色（对齐 potrace 修复）；纯黑线 sat~0 仍入暗层。
    dark_mask = (gray < dark_th) & (hsv_s < 150)
    palelike = ((np.all(rgb >= white_th, axis=2)) | ((hsv_s < sat_th) & (gray >= dark_th))) & ~dark_mask
    # 关键：只把【连到图像边界】的淡色判背景丢弃；图标内部的淡色渐变填充(不连边界)保留为彩色，
    # 否则渐变里的浅色部分被当背景掏成白洞 → 满屏发花 speckle（PK 曲线渐变填充实测主因）。
    num, lbl = cv2.connectedComponents(palelike.astype(np.uint8), connectivity=4)
    border = np.unique(np.concatenate([lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]]))
    border = border[border != 0]                       # 0=非淡色区，排除
    bg_mask = np.isin(lbl, border) if len(border) else np.zeros_like(dark_mask)
    # 近纯白(min>=245)不消耗彩色调色板预算（高光/白原子/抗锯齿白→透出白底，对齐 potrace）；保留淡彩给彩色层。
    near_white = rgb.min(axis=2) >= 245
    color_mask = ~dark_mask & ~bg_mask & ~near_white

    drops = {"dropped_small_contours": 0, "empty_color_blocks": 0, "bg_dropped_px": int(bg_mask.sum())}
    body = []  # (is_dark, area, path_str)

    def _emit_mask(mask, color, is_dark, mn_px, dilate=0):
        npx = int(mask.sum())
        if npx < mn_px:
            if npx > 0:
                drops["empty_color_blocks"] += 1
            return
        if dilate > 0:  # 消簇间白缝 / 救细线：轻微外扩
            kk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate + 1, 2 * dilate + 1))
            mask = cv2.dilate(mask, kk)
        contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            drops["empty_color_blocks"] += 1
            return
        subpaths = []
        for cnt in contours:
            if _contour_pixels(cnt) < mn_px:
                drops["dropped_small_contours"] += 1
                continue
            approx = cv2.approxPolyDP(cnt, eps_px, True).reshape(-1, 2)
            if len(approx) < 2:
                drops["dropped_small_contours"] += 1
                continue
            d = _poly_to_d(approx, smooth=False, corner_deg=0.0)  # 硬边 M/L/Z，不转贝塞尔
            if d:
                subpaths.append(d)
        if not subpaths:
            drops["empty_color_blocks"] += 1
            return
        fill = "#%02X%02X%02X" % (int(color[0]), int(color[1]), int(color[2]))
        body.append((is_dark, npx, f'<path d="{" ".join(subpaths)}" fill="{fill}" fill-rule="evenodd"/>'))

    # C 彩色层：双边滤波平滑渐变（保边不糊轮廓）→【饱和度感知调色板】量化（饱和簇/淡彩簇分开抢预算，
    # 保住少数派彩色原子不被近白+淡背景吞掉色板，对齐 potrace 修复）。代表色=簇中位（已按饱和分离，不需 satp70）。
    rgb_color_src = cv2.bilateralFilter(rgb, 7, 60, 60) if med_k >= 3 else rgb
    ys, xs = np.where(color_mask)
    if len(ys) > 0:
        import image_trace_potrace as _itp
        cpx_all = rgb_color_src[ys, xs].astype(np.uint8)
        cs_all = hsv_s[ys, xs]
        pal = _itp._sat_aware_palette(cpx_all, cs_all, max(2, n_levels))
        labels = _itp._nearest_palette(cpx_all, pal)
        lab2d = np.full((h, w), -1, dtype=np.int32)
        lab2d[ys, xs] = labels
        # 标签图中值滤波：合并渐变/抗锯齿切出的碎色岛（消"发花"speckle，降锚点）。
        if med_k >= 3 and len(pal) <= 254:
            shift = cv2.medianBlur(np.clip(lab2d + 1, 0, 255).astype(np.uint8), med_k)
            lab2d = shift.astype(np.int32) - 1
            lab2d[~color_mask] = -1                              # 非彩色区还原（中值可能渗入边沿）
        for ci in range(len(pal)):
            m = lab2d == ci
            if not m.any():
                continue
            ys2, xs2 = np.where(m)
            rep = np.median(rgb[ys2, xs2], axis=0).astype(np.uint8)
            _emit_mask(m.astype(np.uint8), rep, False, min_area, dilate=seam_close)

    # A 暗描边层：单一二值（不按色细分→线连续），dilate 1px 救细线
    dark_px = rgb[dark_mask]
    if len(dark_px) > 0:
        dark_color = np.median(dark_px, axis=0).astype(np.uint8)
        _emit_mask(dark_mask.astype(np.uint8), dark_color, True, dark_min_px, dilate=dark_dilate)

    body.sort(key=lambda t: (t[0], -t[1]))             # 彩色块(底,大→小) → 暗描边(顶)
    n_colors = len(set(p.split('fill="')[1][:7] for _, _, p in body)) if body else 0
    svg = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n'
           + "\n".join(p for _, _, p in body) + "\n</svg>\n")
    return svg, max(1, n_colors), drops


# ============================================================ 自研 cv2 引擎（兜底）
def _trace_diy(rgb: np.ndarray, params: TraceParams) -> tuple[str, int, dict]:
    """自研兜底：颜色量化 → 逐色块 findContours(含孔洞,evenodd) → approxPolyDP → 可选贝塞尔 → 拼 SVG。

    返回 (svg, 实际色数, drops)；drops 记录被丢弃量（fail-loud 报数，铁律②）：
    removed_speckle_px(形态学开运算抹掉的像素) / dropped_small_contours(面积<阈值的轮廓) / empty_color_blocks(整块被过滤).

    薄结构(细线/坐标轴/字形)保护：面积判定用「填充像素数」而非 cv2.contourArea（细线 contourArea≈0 会被误删）；
    形态学开运算只在 noise 较大(≥8)时才做（新默认 noise=12/清晰预设 16），小杂色时不腐蚀保住 1–2px 细线。
    holes：每色块 path 用「外轮廓 + 内孔子路径 + fill-rule=evenodd」表达 → 下层颜色透出。
    注：svg_io 不保留 fill-rule 属性，但 Qt QPainterPath 默认 OddEvenFill，故 App 内渲染/往返孔洞正确；
    外部 SVG 查看器看导出文件时默认 nonzero 可能填孔（已知小缺口，vtracer 首选无此问题，见审查记录）。
    """
    import cv2
    h, w = rgb.shape[:2]
    drops = {"removed_speckle_px": 0, "dropped_small_contours": 0, "empty_color_blocks": 0}

    # 量化（自动档兜底 16 色，避免每像素一色块爆量）
    if params.mode == "bw":
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        bw = (gray >= params.bw_threshold).astype(np.uint8)
        quant = np.where(bw[:, :, None] == 1, 255, 0).astype(np.uint8).repeat(3, axis=2)
        n_actual = 2
    else:
        eff = params if (params.n_colors or params.palette) else _dc_replace(params, n_colors=16)
        quant, n_actual, _ = _quantize(rgb, eff)

    colors = np.unique(quant.reshape(-1, 3), axis=0)
    # 面积降序（大块先画作底，小块叠上；配合 evenodd 孔洞，画序影响小但更稳）
    areas = [(int((np.all(quant == c, axis=2)).sum()), c) for c in colors]
    areas.sort(key=lambda t: -t[0])

    eps_frac = _lerp(0.02, 0.001, params.paths)  # 路径松紧 → approxPolyDP epsilon 比例（反向）
    corner_deg = _lerp(_CORNER_HI, _CORNER_LO, params.corners)  # 边角阈值（同 vtracer 反向语义）
    min_area = max(1, int(params.noise))
    do_open = params.noise >= 8  # 仅大杂色阈值才形态学开运算（默认不腐蚀，保住细线）
    bw_stroke = params.mode == "bw" and params.create == "stroke"

    body = []
    n_color_kept = 0
    for area, c in areas:
        if area < min_area:
            drops["empty_color_blocks"] += 1
            continue
        if bw_stroke and int(c[0]) > 200 and int(c[1]) > 200 and int(c[2]) > 200:
            continue  # 黑白描边模式跳过白底块（白描边不可见，只徒增冗余路径）
        mask = np.all(quant == c, axis=2).astype(np.uint8)
        if do_open:  # 形态学开运算去针孔斑点（核随 noise 自适应）；计数被抹掉的像素
            ks = max(3, int(params.noise) // 3 * 2 + 1)
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
            before = int(mask.sum())
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
            drops["removed_speckle_px"] += max(0, before - int(mask.sum()))
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            drops["empty_color_blocks"] += 1
            continue
        subpaths = []
        for cnt in contours:
            if _contour_pixels(cnt) < min_area:  # 像素量判定（薄结构 contourArea≈0 不可靠）
                drops["dropped_small_contours"] += 1
                continue
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, eps_frac * peri, True).reshape(-1, 2)
            if len(approx) < 2:
                drops["dropped_small_contours"] += 1
                continue
            d = _poly_to_d(approx, smooth=(params.curve == "spline"), corner_deg=corner_deg)
            if d:
                subpaths.append(d)
        if not subpaths:
            drops["empty_color_blocks"] += 1
            continue
        n_color_kept += 1
        fill = "#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))
        d_all = " ".join(subpaths)
        if params.create == "stroke":
            body.append(f'<path d="{d_all}" fill="none" stroke="{fill}" '
                        f'stroke-width="{_num(params.stroke_width)}" fill-rule="evenodd"/>')
        else:
            body.append(f'<path d="{d_all}" fill="{fill}" fill-rule="evenodd"/>')

    svg = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n'
           + "\n".join(body) + "\n</svg>\n")
    return svg, (n_color_kept if n_color_kept else n_actual), drops


def _contour_pixels(cnt) -> int:
    """轮廓「有效像素量」估计（用于薄结构保护，避免细线被 contourArea≈0 误删）。

    大块直接用 contourArea；薄结构(area≈0)用半周长近似其线长（1px×L 线 contourArea≈0 但闭合周长≈2L，
    半周长≈L≈像素数）。纯算几何量，O(1) 每轮廓——绝不 drawContours 到整图（否则 N 个小轮廓 → O(N×图) 卡死）。
    """
    import cv2
    a = cv2.contourArea(cnt)
    if a >= 8:  # 明显够大，无需精算（min_area 上限远小于此）
        return int(a)
    half_peri = cv2.arcLength(cnt, True) / 2.0  # 薄结构：半周长≈线长≈像素数
    return int(max(a, half_peri))


def _angle_at(p_prev, p, p_next) -> float:
    """p 处转角的「偏离直线」度数：0=直行，越大转得越急。用于尖角判定。"""
    v1 = np.array(p) - np.array(p_prev)
    v2 = np.array(p_next) - np.array(p)
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return 0.0
    cosang = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosang)))


def _poly_to_d(pts: np.ndarray, smooth: bool, corner_deg: float) -> str:
    """闭合多边形点 → SVG d 串。smooth=False → M/L/Z；smooth=True → Catmull-Rom 贝塞尔，
    转角 > corner_deg 的顶点保留为尖角（不平滑），其余平滑。"""
    n = len(pts)
    if n < 2:
        return ""
    if not smooth or n < 3:
        d = ["M %s %s" % (_num(pts[0][0]), _num(pts[0][1]))]
        for p in pts[1:]:
            d.append("L %s %s" % (_num(p[0]), _num(p[1])))
        d.append("Z")
        return " ".join(d)

    # 标记尖角顶点（转角大于阈值 → 该点两侧不平滑）
    sharp = [False] * n
    for i in range(n):
        ang = _angle_at(pts[(i - 1) % n], pts[i], pts[(i + 1) % n])
        if ang > corner_deg:
            sharp[i] = True

    d = ["M %s %s" % (_num(pts[0][0]), _num(pts[0][1]))]
    for i in range(n):  # 每段 pts[i]→pts[i+1]，闭合 → 含最后一段回首点
        p0 = pts[(i - 1) % n]
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        p3 = pts[(i + 2) % n]
        if sharp[i] or sharp[(i + 1) % n]:  # 任一端为尖角 → 直线段（保住尖角）
            d.append("L %s %s" % (_num(p2[0]), _num(p2[1])))
            continue
        # Catmull-Rom → 三次贝塞尔控制点（张力 1/6）
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        d.append("C %s %s %s %s %s %s" % (_num(c1[0]), _num(c1[1]),
                                          _num(c2[0]), _num(c2[1]), _num(p2[0]), _num(p2[1])))
    d.append("Z")
    return " ".join(d)


def _num(v) -> str:
    f = float(v)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    return ("%.2f" % f).rstrip("0").rstrip(".")


# ============================================================ 顶层入口
def trace_to_svg(image, params: Optional[TraceParams] = None) -> tuple[str, dict]:
    """位图 → 矢量 SVG 字符串 + stats（fail-loud 报数）。

    image: 路径 str / numpy (H,W,3|4) uint8 / PIL.Image。
    返回 (svg_str, stats)；stats 含 engine/mode/n_colors_requested/n_colors_actual/n_paths/scale/
    src_size/out_size/elapsed_ms/prequantized/empty/degraded(降级原因,None=未降级)/
    diy_drops(仅 diy 有丢弃时)/banding_warn(渐变碎裂提示)。
    """
    params = params or TraceParams()
    t0 = time.perf_counter()
    rgb = _load_rgb(image)
    h0, w0 = rgb.shape[:2]
    rgb, scale = _maybe_downscale(rgb, params.max_dim)

    # 黑白阈值（MUST-1）：上游按 bw_threshold 预二值化成纯黑白 → vtracer binary / diy 两路都尊重该阈值
    # （vtracer.convert_* 无 binary 阈值参数，内部自动二值化；预二值化后其内部阈值无所谓）。
    if params.mode == "bw":
        rgb = _binarize(rgb, params.bw_threshold)

    degraded = None
    engine = params.engine
    if engine == "vtracer" and not VTRACER_AVAILABLE:  # fail-loud：自动降级 diy
        engine = "diy"
        degraded = "vtracer 不可用 → 自动降级自研引擎"
    # 描边创建 vtracer 不支持 → 降级 diy（fail-loud 报数，不静默吞掉用户所选参数）
    if engine == "vtracer" and params.create == "stroke":
        engine = "diy"
        degraded = "描边创建 vtracer 不支持 → 降级自研引擎"

    # 硬边锐利档（自研三层 cv2）只做彩色填色 → 黑白/灰度/描边 降级自研 diy（fail-loud）
    if engine == "crisp" and not (params.mode == "color" and params.create == "fill"):
        engine = "diy"
        note = "硬边锐利档仅彩色填色，该模式降级自研引擎"
        degraded = note if degraded is None else degraded + "；" + note

    # potrace 档（外部 potrace.exe 子进程）只做彩色填色；黑白/灰度/描边降级 diy
    if engine == "potrace" and not (params.mode == "color" and params.create == "fill"):
        engine = "crisp"
        note = "potrace 档仅彩色填色，该模式降级硬边锐利档"
        degraded = note if degraded is None else degraded + "；" + note

    prequantized = False
    n_actual = 0
    diy_drops = None
    if engine == "potrace":
        import image_trace_potrace as itp
        exe, kind = itp.find_potrace_exe()
        if kind != "exe":                               # potrace.exe 缺失 → 降级 crisp（fail-loud，绝不 import potracer）
            engine = "crisp"
            degraded = "potrace.exe 缺失 → 降级硬边锐利档（crisp）"
            svg, n_actual, diy_drops = _trace_crisp(rgb, params)
        else:
            try:
                svg, n_actual, diy_drops = itp.trace_potrace(rgb, params, exe)
            except Exception as e:  # noqa: BLE001 —— exe 超时/非零退出/解析失败 → 降级 crisp
                engine = "crisp"
                degraded = f"potrace 失败({e}) → 降级硬边锐利档（crisp）"
                svg, n_actual, diy_drops = _trace_crisp(rgb, params)
    elif engine == "crisp":
        svg, n_actual, diy_drops = _trace_crisp(rgb, params)
    elif engine == "vtracer":
        if params.mode != "bw":  # 黑白交 vtracer binary 处理，不预量化
            rgb, n_actual, prequantized = _quantize(rgb, params)
        try:
            svg = _trace_vtracer(rgb, params, prequantized)
        except Exception as e:  # noqa: BLE001 —— 引擎异常 → 降级 diy（不让描摹整体失败）
            engine = "diy"
            degraded = f"vtracer 失败({e}) → 降级自研引擎"
            svg, n_actual, diy_drops = _trace_diy(rgb, params)
    else:
        svg, n_actual, diy_drops = _trace_diy(rgb, params)
        # diy 不区分 method（恒 evenodd 孔洞）→ fail-loud 标注（铁律②）
        if params.method != "overlapping":
            note = "自研引擎不区分相邻/重叠，method 未生效"
            degraded = note if degraded is None else degraded + "；" + note

    h, w = rgb.shape[:2]

    # 忽略颜色 / 透明度（MUST-2）：丢弃 fill 命中忽略色（含近白）的 <path> → 透明底/去指定色（两引擎统一出口过滤）
    ignore = list(params.ignore_colors or [])
    if params.ignore_white:
        ignore.append("#FFFFFF")
    n_ignored = 0
    if ignore:
        svg, n_ignored = _drop_fill_paths(svg, ignore, params.ignore_tol)

    # 将曲线与线条对齐（SHOULD-2）：近水平/垂直直线段吸附为正交直线（对 L 段，不动 C 曲线）
    n_snapped = 0
    if params.snap_lines:
        svg, n_snapped = _snap_svg_lines(svg)

    n_paths = svg.count("<path")
    n_anchors = _count_anchors(svg)

    # 编组（对齐 AI 扩展后的组）：所有 path 包进一个 <g> → 导入后是一个可整体移动/解组的组，不再是几千散件
    if params.group and n_paths > 1:
        svg = _wrap_in_group(svg)

    # fail-loud：空产物（所有色块被过滤 / 图近单色）→ 标降级，UI 据此弹提示，不静默登记空矢量层
    empty = (n_paths == 0)
    if empty and degraded is None:
        degraded = f"{engine} 产物 0 路径（所有色块被 noise/min_area 过滤或图近单色，请放宽阈值）"

    # fail-loud：预量化在渐变内容上把 banding 切成大量碎层（n_paths 远超色数）→ 提示改自动档/上调阈值
    banding_warn = None
    if prequantized and n_actual > 0 and n_paths > _BANDING_WARN_RATIO * n_actual:
        banding_warn = (f"减色档产生 {n_paths} 路径（远超 {n_actual} 色）—— 渐变内容碎裂，"
                        f"建议改自动档(颜色数=0)或上调杂色/降低颜色数")

    stats = {
        "engine": engine,
        "mode": params.mode,
        "n_colors_requested": params.n_colors if not params.palette else len(params.palette),
        "n_colors_actual": n_actual,
        "n_paths": n_paths,
        "n_anchors": n_anchors,
        "scale": scale,
        "src_size": (w0, h0),
        "out_size": (w, h),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "prequantized": prequantized,
        "empty": empty,
        "degraded": degraded,
        "banding_warn": banding_warn,
    }
    if n_ignored:
        stats["ignored_paths"] = n_ignored   # fail-loud：忽略色丢弃的 path 数
    if n_snapped:
        stats["snapped_lines"] = n_snapped   # 吸附为正交直线的段数
    if diy_drops and any(v > 0 for v in diy_drops.values()):
        stats["diy_drops"] = diy_drops  # 仅有丢弃时上浮（薄结构/斑点被删计数）
    return svg, stats


# ============================================================ 后处理工具（黑白阈值/忽略色/对齐直线/锚点数）
def _binarize(rgb: np.ndarray, threshold: int) -> np.ndarray:
    """按亮度阈值二值化成纯黑白 RGB（≥阈值=白(255)，<阈值=黑(0)）。"""
    import cv2
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    bw = np.where(gray >= int(threshold), 255, 0).astype(np.uint8)
    return np.repeat(bw[:, :, None], 3, axis=2)


_FILL_RE = re.compile(r'fill="(#[0-9A-Fa-f]{6})"')
_PATH_RE = re.compile(r'<path\b[^>]*?/>', re.DOTALL)
_D_RE = re.compile(r'\bd="([^"]*)"')


def _hexdist2(a: tuple, b: tuple) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _drop_fill_paths(svg: str, ignore_hex: list, tol: int) -> tuple[str, int]:
    """删掉 fill 命中忽略色（sRGB 欧氏距 ≤ tol）的 <path .../>。返回 (新svg, 删除数)。

    只对 fill 实色 path 生效（描边 fill=none 不受影响）；两引擎产物均为单行自闭合 <path>，正则安全。
    """
    targets = [_hex_to_rgb(h) for h in ignore_hex]
    tol2 = int(tol) ** 2
    dropped = [0]

    def _sub(m):
        seg = m.group(0)
        fm = _FILL_RE.search(seg)
        if not fm:
            return seg
        c = _hex_to_rgb(fm.group(1))
        if any(_hexdist2(c, t) <= tol2 for t in targets):
            dropped[0] += 1
            return ""  # 删除该 path（背景透出/去该色）
        return seg

    out = _PATH_RE.sub(_sub, svg)
    return out, dropped[0]


def _snap_svg_lines(svg: str, tol_deg: float = 6.0) -> tuple[str, int]:
    """对 SVG 每条 <path d> 的 L 段做近水平/垂直吸附（对齐 AI「将曲线与线条对齐」）。返回 (新svg, 吸附段数)。"""
    cnt = [0]

    def _sub(m):
        d = m.group(1)
        nd, k = _snap_d(d, tol_deg)
        cnt[0] += k
        return 'd="%s"' % nd if k else m.group(0)

    out = _D_RE.sub(_sub, svg)
    return out, cnt[0]


def _snap_d(d: str, tol_deg: float) -> tuple[str, int]:
    """对 d 串的 L 段吸附为正交直线：段近水平→末点 y 对齐到起点 y；近垂直→末点 x 对齐起点 x。

    只动 L 段（不碰 C/Q/A 曲线，避免破坏形状）；M/Z 原样。跟踪当前点。返回 (新d, 吸附段数)。
    兼容 vtracer 多边形档「命令字母粘连首坐标 + 逗号分隔」(如 M0,0 L30,0)：先把命令字母与坐标拆开再分词。
    """
    # 命令字母前后补空格 + 逗号转空格 → 命令与坐标必分开（vtracer polygon: "M0,0 L30,0" / spline: "M0 0 C..." 都正确）
    toks = re.sub(r"([MmLlCcZzHhVvQqSsTtAa])", r" \1 ", d.replace(",", " ")).split()
    out = []
    i = 0
    cx = cy = 0.0
    snapped = 0
    import math
    while i < len(toks):
        t = toks[i]
        if t in ("M", "m", "L", "l"):
            x, y = float(toks[i + 1]), float(toks[i + 2])
            ax, ay = (cx + x, cy + y) if t in ("m", "l") else (x, y)  # 相对→绝对
            if t in ("L", "l"):
                dx, dy = ax - cx, ay - cy
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    ang = math.degrees(math.atan2(abs(dy), abs(dx)))  # 0=水平,90=垂直
                    if ang <= tol_deg:        # 近水平 → 拉平 y
                        ay = cy; snapped += 1
                    elif ang >= 90 - tol_deg:  # 近垂直 → 拉直 x
                        ax = cx; snapped += 1
                out.append("L %s %s" % (_num(ax), _num(ay)))
            else:
                out.append("M %s %s" % (_num(ax), _num(ay)))
            cx, cy = ax, ay
            i += 3
        elif t in ("C", "c"):  # 三次贝塞尔：原样保留，仅更新当前点到末锚
            seg = toks[i:i + 7]
            out.append(" ".join(seg))
            cx, cy = float(seg[5]), float(seg[6])  # 注：绝对 C；相对 c 少见于本产物，简化按绝对更新
            i += 7
        elif t in ("Z", "z"):
            out.append("Z"); i += 1
        else:  # 其它命令（Q/A 等）原样吞一个 token，避免死循环（本产物只出 M/L/C/Z）
            out.append(t); i += 1
    return " ".join(out), snapped


def _wrap_in_group(svg: str) -> str:
    """把 <svg> 内所有内容包进一个 <g> → svg_io 解析为 group VElem → 导入后是一个组（可整体移/解组，对齐 AI）。"""
    m = re.search(r'<svg\b[^>]*>', svg)
    end = svg.rfind("</svg>")
    if not m or end < 0:
        return svg
    start = m.end()
    return svg[:start] + '\n<g id="trace">' + svg[start:end] + "</g>\n" + svg[end:]


def _count_anchors(svg: str) -> int:
    """估计锚点总数：统计所有 d 串里的绘图命令数（M/L 各 1 锚、C/Q 各 1 锚、Z 不计）。近似供信息区显示。"""
    n = 0
    for m in _D_RE.finditer(svg):
        n += len(re.findall(r'[MLCQSTAmlcqsta]', m.group(1)))
    return n
