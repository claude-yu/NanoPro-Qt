"""OpenCV 抠图算法（魔棒 / 羽化 / 按掩码提取）+ QImage<->numpy 互转。

cv2 函数只吃 numpy（可 headless 基准）；QImage 互转里才惰性 import PySide6。
"""
from __future__ import annotations

import cv2
import numpy as np


# ---------- 纯 numpy/cv2 算法（无需 Qt，可 headless 计时）----------
def magic_wand_mask(rgba: np.ndarray, x: int, y: int, tol: int) -> np.ndarray:
    """从 (x,y) 选相近颜色的连续区域 → 二值掩码 (H,W) uint8（255=选中）。
    度量从逐通道 RGB L2 升级为 LAB ΔE76 感知色距（cv2 8bit LAB：L/a/b 均 0-255，a/b 偏移 128），
    对半透明/渐变背景比 RGB 更稳——LAB 感知均匀，同样的容差不会沿渐变一路爬出。
    保留两条核心语义不变：候选 = 与【种子固定参考色】的色距 ≤ 阈值（非逐通道 L∞、非浮动范围）；
    再取含种子的 4 连通分量（"连续区域"语义）。容差 tol(0-255 滑块) 直接作 ΔE 阈值（不再 ·3，
    LAB 各通道与 tol 同量级 0-255）。"""
    h, w = rgba.shape[:2]
    lab = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2LAB).astype(np.int32)  # L/a/b 各 0-255
    seed_lab = lab[int(y), int(x)]
    de = float(tol)                                                    # ΔE76 阈值 = 滑块容差
    cand = (((lab - seed_lab) ** 2).sum(axis=2) <= de * de).astype(np.uint8)  # 与固定 seed 比 LAB 欧氏(≈ΔE76)
    num, labels = cv2.connectedComponents(cand, connectivity=4)        # 4 邻接，对齐 WebView flood
    comp = int(labels[int(y), int(x)])
    if comp == 0:
        return np.zeros((h, w), np.uint8)
    return (labels == comp).astype(np.uint8) * 255


def grabcut_mask(rgba: np.ndarray, seed_mask=None, rect=None, iters: int = 5, progress_cb=None):
    """GrabCut 前景分割（对非纯色背景抠主体）。
    seed_mask: (H,W) uint8 选区(>0=确定/可能前景) 优先；否则用 rect=(x0,y0,x1,y1) 矩形框。
    progress_cb(done, total)->bool|None：传了则【逐次迭代】并回调进度（返回 False 可提前停，用于进度条+取消）；
    None=一次性算完（保持原行为，供无需进度的调用方）。
    返回 (fg_mask uint8 255/0, err_str)；失败返回 (None, '原因') —— 大声失败，不静默。"""
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] < 3:
        return None, "图像格式无效（需 HxWx≥3 通道）"
    h, w = rgba.shape[:2]
    have_seed = seed_mask is not None and np.asarray(seed_mask).shape == (h, w) and (np.asarray(seed_mask) > 0).any()
    if not have_seed and rect is None:
        return None, "需要先框选或取选区作为前景种子"
    bgr = np.ascontiguousarray(cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR))  # grabCut 只吃 3 通道 8U
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    gc_mask = np.zeros((h, w), np.uint8)
    try:
        if have_seed:
            # 沿用现有选区语义：选区内 = 可能前景，其余 = 可能背景（不标确定边，简洁优先）
            gc_mask[:] = cv2.GC_PR_BGD
            gc_mask[np.asarray(seed_mask) > 0] = cv2.GC_PR_FGD
            init_mode, rect_arg = cv2.GC_INIT_WITH_MASK, None
        else:
            x0, y0, x1, y1 = rect
            x0, x1 = sorted((int(x0), int(x1)))
            y0, y1 = sorted((int(y0), int(y1)))
            x0 = max(0, x0); y0 = max(0, y0); x1 = min(w, x1); y1 = min(h, y1)
            if x1 <= x0 or y1 <= y0:
                return None, "框选区域无效"
            init_mode, rect_arg = cv2.GC_INIT_WITH_RECT, (x0, y0, x1 - x0, y1 - y0)
        n = max(1, int(iters))
        if progress_cb is None:
            cv2.grabCut(bgr, gc_mask, rect_arg, bgd_model, fgd_model, n, init_mode)
        else:
            # 逐次迭代，每步回调进度（首次 init，之后 GC_EVAL 续算）；cb 返回 False 提前停（取消）
            for i in range(n):
                cv2.grabCut(bgr, gc_mask, (rect_arg if i == 0 else None),
                            bgd_model, fgd_model, 1, (init_mode if i == 0 else cv2.GC_EVAL))
                if progress_cb(i + 1, n) is False:
                    break
    except cv2.error as e:
        return None, f"GrabCut 失败: {e}"
    fg = (((gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD)).astype(np.uint8)) * 255
    if not fg.any():
        return None, "GrabCut 没分出前景（试试更贴主体的框/选区，或调 AI 抠图）"
    return fg, ""


def feather_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    """羽化掩码边缘（高斯模糊）。"""
    if radius <= 0:
        return mask
    k = radius * 2 + 1
    return cv2.GaussianBlur(mask, (k, k), 0)


def extract_by_mask(rgba: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按掩码取出：新 alpha = 原 alpha × mask/255。返回新 RGBA。"""
    out = rgba.copy()
    a = out[:, :, 3].astype(np.float32) * (mask.astype(np.float32) / 255.0)
    out[:, :, 3] = np.clip(a, 0, 255).astype(np.uint8)
    return out


def adjust_brightness_contrast(rgba: np.ndarray, brightness: int = 0, contrast: int = 0) -> np.ndarray:
    """PS「图像>调整>亮度/对比度」经典线性近似（便于实时预览）。
    brightness ∈ [-150,150]，contrast ∈ [-100,100]。
    对比因子 f=(259*(c+255))/(255*(259-c))（c=0→f=1；c=100→分母159，不除零）；
    out_rgb = clip(f*(in_rgb-128)+128+brightness, 0, 255)。仅作用 RGB，alpha 原样保留。"""
    brightness = max(-150, min(150, int(brightness)))   # 护栏：夹到声明范围
    contrast = max(-100, min(100, int(contrast)))        # 防未来误用 c≥259 致除零/数值翻转
    if brightness == 0 and contrast == 0:
        return rgba.copy()  # 滑回 0 即恢复原图
    out = rgba.copy()       # 不就地改入参（对齐 extract_by_mask）
    rgb = out[:, :, :3].astype(np.float32)
    f = (259.0 * (contrast + 255.0)) / (255.0 * (259.0 - contrast))
    rgb = f * (rgb - 128.0) + 128.0 + float(brightness)
    out[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)  # alpha 切片 [:,:,3] 不碰
    return out


def remove_by_mask(rgba: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按掩码删除（去背景）：选中处 alpha 置 0。"""
    out = rgba.copy()
    out[:, :, 3] = (out[:, :, 3].astype(np.float32) * (1.0 - mask.astype(np.float32) / 255.0)).astype(np.uint8)
    return out


def draw_brush_segment(mask: np.ndarray, x0, y0, x1, y1, size) -> np.ndarray:
    """选区画笔：在掩码上按笔宽 size 描一段圆头线（255）。供选区涂抹笔累积选区用。"""
    cv2.line(mask, (int(round(x0)), int(round(y0))), (int(round(x1)), int(round(y1))),
             255, thickness=max(1, int(size)), lineType=cv2.LINE_8)
    return mask


def polygon_mask(h: int, w: int, points_xy) -> np.ndarray:
    """套索：多边形顶点(局部坐标) → 填充掩码 (H,W) uint8。"""
    mask = np.zeros((h, w), np.uint8)
    pts = np.array(points_xy, dtype=np.int32)
    if pts.shape[0] >= 3:
        cv2.fillPoly(mask, [pts], 255)
    return mask


def rect_mask(h: int, w: int, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """矩形选区 → 掩码（坐标自动排序+裁剪到画面内）。"""
    mask = np.zeros((h, w), np.uint8)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0 = max(0, x0); y0 = max(0, y0); x1 = min(w, x1); y1 = min(h, y1)
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = 255
    return mask


def fill_color_from_edge(rgba: np.ndarray, mask: np.ndarray):
    """取选区外缘 4–9px 的【不透明】邻居像素、逐通道取【中值】作为填洞背景色
    （移植 sampleBgColor, features.js:641-680）。中值对元素抗锯齿边的离群点鲁棒：均匀背景
    下能得到精确背景色，填洞无色差。空则回退 1–9px 任意邻居，再空回退白。"""
    sel = (mask > 0).astype(np.uint8)
    rgb = rgba[:, :, :3]
    alpha = rgba[:, :, 3]

    def band(min_d: int, max_d: int) -> np.ndarray:
        """距 mask (min_d, max_d] 的外缘环（不含 mask 内）。"""
        outer = cv2.dilate(sel, np.ones((2 * max_d + 1, 2 * max_d + 1), np.uint8)) > 0
        inner = (cv2.dilate(sel, np.ones((2 * min_d + 1, 2 * min_d + 1), np.uint8)) > 0) if min_d > 0 else (sel > 0)
        return outer & (~inner)

    px = band(3, 9) & (alpha > 200)          # 主：4–9px 外、不透明(alpha>200)，跳过元素抗锯齿边
    if not px.any():
        px = band(0, 9)                       # 回退：1–9px 任意邻居
    if not px.any():
        return (255, 255, 255)
    med = np.median(rgb[px], axis=0)          # 逐通道中值
    return tuple(int(round(v)) for v in med)


def fill_by_mask(rgba: np.ndarray, mask: np.ndarray, color) -> np.ndarray:
    """把选区填成 color 覆盖原位（移植 buildErasePatch『膨胀3px 盖抗锯齿边 + 1px 软边』,
    features.js:682-723）：先膨胀≈3px 完整盖住元素抗锯齿边，再对填充边缘做 1px 羽化过渡，
    避免硬填后洞边残留一圈元素残影。直接合成进源 RGBA。"""
    out = rgba.copy()
    m = (mask > 0).astype(np.uint8)
    dil = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=3)        # 3px 膨胀盖 AA 边
    soft = (cv2.GaussianBlur(dil.astype(np.float32) * 255.0, (3, 3), 0) / 255.0)[:, :, None]  # 1px 软边 0..1
    fill_rgb = np.array(color[:3], np.float32)
    base_rgb = out[:, :, :3].astype(np.float32)
    out[:, :, :3] = np.clip(base_rgb * (1.0 - soft) + fill_rgb * soft, 0, 255).astype(np.uint8)
    a = out[:, :, 3].astype(np.float32)
    out[:, :, 3] = np.clip(np.maximum(a, soft[:, :, 0] * 255.0), 0, 255).astype(np.uint8)  # 软边处抬升 alpha
    return out


def mask_bbox(mask: np.ndarray):
    """选区外接矩形 (x0,y0,x1,y1) 半开区间；空选区返回 None。用于把抠出物裁到选区大小。"""
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def content_bbox(rgba: np.ndarray, thr: int = 8):
    """【实心内容】的紧致外接矩形 (x0,y0,x1,y1) 半开区间；全透明/无 alpha 返回 None。
    用于让连接线/裁剪锚到素材【真正的图】而非四周透明留白的整张大框（对齐 BioRender 紧致图标）。
    自适应阈值：图里有明显实心内容(高 alpha)时，按其 ~22% 作阈值【忽略淡水印/柔光晕】（如 pngtree
    满图淡水印）；整体偏淡的图则退回低阈值 thr，不至误裁。注意：【不透明】的水印文字(如「Designed by
    pngtree」)本身就是实心内容，alpha 裁不掉——那种要用裁剪工具手动裁。"""
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] < 4:
        return None
    a = rgba[:, :, 3]
    if a.size:
        thr = max(thr, int(int(a.max()) * 0.22))  # 自适应：忽略远低于主体的淡水印
    ys, xs = np.where(a > thr)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def background_mask(rgba: np.ndarray, tol: int) -> np.ndarray:
    """背景掩码（移植 WebView backgroundMask, features.js:314-348）：双重判定——
    (a) 步进容差：每步只和**相邻像素**比、差够小才扩散；
    (b) 全局色彩包络：参考背景色 ref = 四边环均值，gEnv = max(70, tol*2.5)，与 ref 欧氏距离 > gEnv
    的像素不算背景，且作为【传播屏障】——洪泛在第一个出包络像素处停住，钻不过抗锯齿渐变边进入与
    背景全局差很远的实心元素（features.js:307-313,334-337 注释）。返回 bool (H,W)。
    实现取舍：用 cv2.floodFill（快），通过把"出包络"像素预占进 ff mask 实现"传播阻断"语义；
    步进度量为 cv2 的逐通道 L∞（每通道差≤tol），而 WebView 为欧氏 L2（dr²+dg²+db²≤tol²·3）。
    L∞ 比 L2 更严（cv2 通过 ⟹ WebView 通过，反之不一定）→ 本实现**偏保守**：在单通道主导的
    彩色强渐变背景上洪泛更早停住，可能漏标本该是背景的像素(把它当前景)；白/灰平背景下两者几乎一致。
    遇彩色渐变背景漏判时调高「容差」即可。白底科研图为主，影响可忽略。"""
    h, w = rgba.shape[:2]
    rgb = rgba[:, :, :3].astype(np.float32)
    # 参考背景色 = 四边环均值（对应 features.js:318-322，角点不重复计）
    ring = np.concatenate([
        rgb[0, :, :], rgb[h - 1, :, :],
        rgb[1:h - 1, 0, :], rgb[1:h - 1, w - 1, :],
    ], axis=0)
    ref = ring.mean(axis=0)
    g_env = max(70.0, float(tol) * 2.5)
    within = (((rgb - ref) ** 2).sum(axis=2) <= g_env * g_env)  # (H,W) 全局包络内

    bgr = np.ascontiguousarray(cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR))
    ff = np.zeros((h + 2, w + 2), np.uint8)
    # 把"出包络"像素预占为屏障(=1)：floodFill 只填 mask==0 处，故洪泛碰到出包络像素就停，
    # 进不了被该带挡在身后的包络内像素（对齐 withinEnvelope 作为传播阻断器, features.js:334-337）。
    ff[1:-1, 1:-1][~within] = 1
    lo = (int(tol),) * 3
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)  # 无 FIXED_RANGE → 浮动范围(与邻像素比)
    # 遍历每一个边界像素下种（对齐 features.js:332-333 全边界 seedEdge）：被屏障(1)/已填(255)的种子由
    # ff==0 守卫廉价跳过，故成本仅 O(周长)；strided 采样会漏掉被屏障夹住的短 within 边界段(审核#2)。
    seeds = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] \
        + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    for sx, sy in seeds:
        if ff[sy + 1, sx + 1] == 0:  # 包络内(未被预占=1)且未被前次填充(=255) → 可作种子
            cv2.floodFill(bgr, ff, (int(sx), int(sy)), 0, lo, lo, flags)
    return ff[1:-1, 1:-1] == 255  # 仅被洪泛填充(255)的算背景；预占屏障(1)不算


def mask_to_sprite(rgba: np.ndarray, mask: np.ndarray, erode: bool = True, feather: float = 1.0):
    """掩码 → 透明素材（移植 maskToSprite, features.js:164-282）：8 连通 1px 腐蚀去边缘
    污染 + 羽化，且羽化后必须用【原始 mask】再约束 alpha——否则高斯模糊会把 alpha 外扩到
    周围背景色像素，重新引入 WebView 注释(features.js:241-245)专门要消除的背景色光晕(色差)。
    返回 (cropped_rgba, x0, y0)；空则 None。"""
    m0 = (mask > 0).astype(np.uint8)            # 原始 mask（约束用，对应 WebView 的 mask）
    work = m0
    if erode:
        # 8 连通 1px 腐蚀（含 45° 对角）：丢掉整圈抗锯齿边，留下的全是纯内部色——色差核心守卫。
        # borderValue=0：越界按"未选"处理，使贴画布边缘的对象其 1px 边缘环也被腐蚀（对齐 features.js:178-188
        # 只遍历内部、最外圈永不存活；cv2 默认 borderValue=+inf 会保留贴边像素，引入残留色差环）。
        er = cv2.erode(m0, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
        if er.any():
            work = er
    fr = max(0.5, feather) if erode else feather
    ys, xs = np.where(work > 0)                  # bbox 取自 work（腐蚀后），对应 features.js:196
    if len(xs) == 0:
        return None
    if fr > 0.05:
        k = int(2 * np.ceil(fr) + 1)
        blurred = cv2.GaussianBlur(work * 255, (k, k), 0)
        # 关键：把羽化后的 alpha 约束回原始 mask，软边只向内羽化、不渗进背景色像素
        alpha_full = np.where(m0 > 0, blurred, 0).astype(np.float32)
    else:
        alpha_full = (work * 255).astype(np.float32)
    pad = int(np.ceil(fr + 1))
    x0 = max(0, int(xs.min()) - pad); y0 = max(0, int(ys.min()) - pad)
    x1 = min(rgba.shape[1], int(xs.max()) + 1 + pad)
    y1 = min(rgba.shape[0], int(ys.max()) + 1 + pad)
    out = rgba[y0:y1, x0:x1].copy()
    a = out[:, :, 3].astype(np.float32) * (alpha_full[y0:y1, x0:x1] / 255.0)
    out[:, :, 3] = np.clip(a, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(out), x0, y0


def auto_decompose(rgba: np.ndarray, tol: int = 36, min_blob_frac: float = 0.0006,
                   feather: float = 1.0, max_pieces: int = 200):
    """自动拆解（移植 WebView autoDecompose）：背景掩码→前景连通块(8连通)→面积≥W·H·minBlobFrac
    的块按面积大→小，每块 maskToSprite(腐蚀+羽化)成透明素材。文字等小块被 minArea 过滤掉。
    增强：(1) 闭运算把抗锯齿/细缝粘回主体减碎片；(2) Canny 边缘在 ~bg 区内补回弱对比主体边界，
    让背景判漏的主体仍能成闭合连通块。返回 (pieces_list, info_str)——找不到独立元素时 info_str 给
    出明确建议（大声失败，不静默返回空 list）。"""
    h, w = rgba.shape[:2]
    bg = background_mask(rgba, tol)
    not_bg = ~bg
    fg = (not_bg & (rgba[:, :, 3] > 8)).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))  # 补碎洞：抗锯齿/细缝粘回主体
    # Canny 边缘辅助：把背景判漏的弱对比主体边界补进前景（仅限 ~bg 区，dilate 让边缘成闭合连通）
    gray = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    fg = (fg | ((edges > 0) & not_bg & (rgba[:, :, 3] > 8))).astype(np.uint8)
    min_area = max(80, int(w * h * min_blob_frac))
    num, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    comps = sorted(
        [(i, int(stats[i, cv2.CC_STAT_AREA])) for i in range(1, num) if stats[i, cv2.CC_STAT_AREA] >= min_area],
        key=lambda t: -t[1])
    out = []
    for comp_lab, _ in comps[:max_pieces]:
        sprite = mask_to_sprite(rgba, labels == comp_lab, erode=True, feather=feather)
        if sprite is not None:
            out.append(sprite[0])
    if not out:
        total_blobs = max(0, num - 1)
        if total_blobs == 0:
            info = "未找到独立元素：背景可能非纯色或整层接近一色，建议调高容差，或改用 GrabCut/AI 抠图"
        else:
            info = "找到的块都太小被过滤（碎片/文字）：背景可能非纯色，建议调高容差，或改用 GrabCut/AI 抠图"
        return out, info
    return out, ""


def split_montage(rgba: np.ndarray, bg_tol: int = 20, min_gap: int = 0,
                  min_cell: int = 24, max_cells: int = 400):
    """把一张「图标合集」大图(白底上排着多个图标)按【空白沟槽】递归 XY-cut 切成单个图标。
    用空白切而非连通块：一个图标常由多笔不相连的笔画组成，连通块会把它碎成几十片；
    空白沟槽切法整块保留图标(含其下方小标签)。返回 [(x,y,w,h)] 紧致包围盒(已去四周空白)。
    bg_tol: 与背景色差≤此值算空白；min_gap: 0=自适应(按图尺寸~1%)；min_cell: 小于此边长的块丢弃。
    纯 numpy，可 headless 单测。"""
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] < 3:
        return []
    h, w = rgba.shape[:2]
    rgb = rgba[:, :, :3].astype(np.int16)
    alpha = rgba[:, :, 3] if rgba.shape[2] >= 4 else np.full((h, w), 255, np.uint8)
    corners = np.array([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]], dtype=np.int16)
    bg = np.median(corners, axis=0)                       # 背景色=四角中位(对白底/任意纯底都稳)
    diff = np.abs(rgb - bg).max(axis=2)
    ink = (diff > bg_tol) & (alpha > 8)                   # True=有内容(墨), False=背景空白
    gap = min_gap if min_gap > 0 else max(6, int(0.012 * max(h, w)))  # 自适应沟槽阈值
    boxes = []

    def biggest_gap(empty):
        """1D 布尔(True=该行/列全空) → 内部最长空白游程 (长度, 起, 止)。两端空白已被收缩排除。"""
        best, i, n = (0, -1, -1), 0, len(empty)
        while i < n:
            if empty[i]:
                j = i
                while j < n and empty[j]:
                    j += 1
                if j - i > best[0]:
                    best = (j - i, i, j)
                i = j
            else:
                i += 1
        return best

    def cut(x0, y0, x1, y1, depth):
        sub = ink[y0:y1, x0:x1]
        if sub.size == 0 or not sub.any():
            return
        cols = np.where(sub.any(axis=0))[0]               # 先收缩到紧致内容框
        rows = np.where(sub.any(axis=1))[0]
        nx0, nx1 = x0 + int(cols[0]), x0 + int(cols[-1]) + 1
        ny0, ny1 = y0 + int(rows[0]), y0 + int(rows[-1]) + 1
        sub = ink[ny0:ny1, nx0:nx1]
        sh, sw = sub.shape
        if depth > 60:
            boxes.append((nx0, ny0, sw, sh)); return
        cg = biggest_gap(~sub.any(axis=0))                # 列方向最大空白(竖直沟槽)
        rg = biggest_gap(~sub.any(axis=1))                # 行方向最大空白(水平沟槽)
        if cg[0] >= rg[0] and cg[0] >= gap and 0 < cg[1] and cg[2] < sw:
            mid = nx0 + (cg[1] + cg[2]) // 2              # 沿竖直沟槽切左右
            cut(nx0, ny0, mid, ny1, depth + 1); cut(mid, ny0, nx1, ny1, depth + 1)
        elif rg[0] >= gap and 0 < rg[1] and rg[2] < sh:
            mid = ny0 + (rg[1] + rg[2]) // 2              # 沿水平沟槽切上下
            cut(nx0, ny0, nx1, mid, depth + 1); cut(nx0, mid, nx1, ny1, depth + 1)
        elif sw >= min_cell and sh >= min_cell:           # 无可切沟槽 → 一个叶子单元
            boxes.append((nx0, ny0, sw, sh))

    cut(0, 0, w, h, 0)
    boxes.sort(key=lambda b: (b[1] // max(1, min_cell), b[0]))  # 按阅读序(行优先)排
    return boxes[:max_cells]


def mask_contours(mask: np.ndarray):
    """掩码 → 轮廓点列表（每个为 Nx2 int 数组，含内外边界），用于画蚂蚁线 / 涂抹预览。

    性能：先裁到选区外接矩形再 findContours —— 成本随选区大小而非整幅画布。
    原来在大画布上每帧对全图扫描，是选区画笔描线跟不上手的主因（审核 HIGH）。
    """
    bb = mask_bbox(mask)
    if bb is None:
        return []
    x0, y0, x1, y1 = bb
    # 四周各留 1px 边距，保证贴边选区的轮廓仍能闭合（findContours 需要边界外有 0）
    x0p, y0p = max(0, x0 - 1), max(0, y0 - 1)
    x1p, y1p = min(mask.shape[1], x1 + 1), min(mask.shape[0], y1 + 1)
    sub = np.ascontiguousarray(mask[y0p:y1p, x0p:x1p])  # 列切片是非连续视图，cv2 需连续 uint8
    cnts, _ = cv2.findContours(sub, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if len(c) >= 2:
            pts = c.reshape(-1, 2).copy()
            pts[:, 0] += x0p  # 偏移回全图坐标
            pts[:, 1] += y0p
            out.append(pts)
    return out


# ---------- QImage <-> numpy（UI 用；惰性 import PySide6）----------
def qimage_to_rgba(qimg) -> np.ndarray:
    from PySide6 import QtGui
    img = qimg.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    h, w = img.height(), img.width()
    bpl = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=h * bpl).reshape(h, bpl)
    return buf[:, : w * 4].reshape(h, w, 4).copy()


def rgba_to_qimage(arr: np.ndarray):
    from PySide6 import QtGui
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = arr.shape[:2]
    return QtGui.QImage(arr.data, w, h, 4 * w, QtGui.QImage.Format.Format_RGBA8888).copy()


def masked_qimage(qimg, mask):
    """图层「有效显示图」=按蒙版调透明度（非破坏，不改原 image）。mask=None→原样返回 qimg；
    否则 新 alpha = 原 alpha × mask/255。画布显示 / 导出合成 / 缩略图三处共用此函数，保证画布==导出永不分叉。
    mask 为 uint8 (H,W)，须与 qimg 同尺寸（调用方负责对齐）。"""
    if mask is None:
        return qimg
    return rgba_to_qimage(extract_by_mask(qimage_to_rgba(qimg), mask))
