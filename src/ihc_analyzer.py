"""IHC / 组织化学定量分析面板（单图，P1.2）。

复用 WB 的 ROIView / 目录记忆框架；算法走 ihc_quant（颜色解卷积对齐 Fiji，逐位一致）。
覆盖两类场景：
- **DAB 免疫组化**：颜色解卷积分离 → 目标=DAB 通道 → IHC Profiler 分级/0-4 分 + 阳性面积%。
- **纤维化/胶原定量**：Masson(蓝色胶原)/H&E 等 → 选目标通道(如 Aniline blue) → Otsu/手动阈值 + 背景排除 → 阳性面积%；
  天狼星红(无解卷积向量) → HSV 红色面积法（亮场标准口径）。

阳性区域**高亮叠加**实时显示（ImageJ Threshold 式），并可用**画笔手动加(左键)/减(右键或Alt)**修正，
面积%从最终选区算。拖框选区域（无框=全图），每区一行；导出 CSV。纯 Qt + ihc_quant，可离屏构造测试。
"""
from __future__ import annotations

import csv
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import ihc_quant as iq
import ihc_batch as ib
import icons
import theme
from wb_analyzer import (
    ROIView, _last_dir, _remember_dir, _setup_result_table, _table_item
)   # 复用 ROI 视图 + 目录记忆（同一 QSettings）

SIRIUS_RED_KEY = "天狼星红 (红色面积)"   # UI 专用：非解卷积，走 HSV 红色胶原法

_CHANNEL_TINT = {
    "Hematoxylin": (78, 92, 178), "DAB": (120, 72, 40), "Eosin": (214, 84, 132),
    "AEC": (198, 64, 52), "PAS": (170, 70, 150), "Methyl Green": (60, 130, 90),
    "Methylene blue": (70, 100, 180), "Aniline blue": (60, 90, 180),
    "Alcian blue": (40, 120, 170), "Fuchsin/Ponceau": (200, 70, 90), "Residual": (150, 150, 150),
}
_DEFAULT_TINT = (150, 150, 150)
_TIER_COLOR = {"high_positive": "#8a3b1e", "positive": "#c06a2a", "low_positive": "#c9a23a", "negative": "#6b7280"}
# 阳性叠加可选醒目色（红色在红/粉组织上隐形 → 默认用对比强的亮绿/青）。
_OVERLAY_COLORS = {"亮绿": (60, 255, 90), "青": (0, 220, 255), "品红": (255, 0, 200),
                   "黄": (255, 230, 0), "红": (255, 45, 45), "蓝": (40, 120, 255)}


def _decode_rgb(path) -> np.ndarray:
    """解码图片 → RGB uint8 (H,W,3)。**纯 numpy，可在后台线程跑**（大 TIF 解码+16bit重标定移出主线程）。

    高位深 TIF（16/32-bit，含 I;16B/L/N 大小端，扫描仪常见）按 dtype 判定 → min→0/max→255 缩放到 8-bit，
    避免 convert('RGB') 把 >255 钳成全白。**用 float32(非float64,内存/时间减半)；只对浮点 dtype 做 isfinite(整数无 NaN/inf,跳过省一遍全图)。**
    """
    from PIL import Image
    im = Image.open(path)
    raw = np.asarray(im)
    if raw.dtype != np.uint8 or im.mode.startswith("I") or im.mode == "F":
        r = raw.astype(np.float32)                          # float32 足够 8-bit 输出精度，省一半内存/时间
        if np.issubdtype(raw.dtype, np.floating):           # 仅浮点图可能含 NaN/inf；整数 16-bit 跳过
            finite = np.isfinite(r)
            if not finite.all():
                print("[ihc load] 警告：浮点图含 %d 个非有限像素(NaN/inf)，已置 0 处理" % int((~finite).sum()))
                r = np.where(finite, r, np.float32(0))
        mn, mx = float(r.min()), float(r.max())
        scaled = (np.clip((r - mn) * (255.0 / (mx - mn)), 0, 255).astype(np.uint8)
                  if mx > mn else np.zeros(r.shape, np.uint8))
        arr = np.repeat(scaled[..., None], 3, axis=2) if scaled.ndim == 2 else scaled[..., :3]
    else:
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    return np.ascontiguousarray(arr)


def load_rgb_and_pixmap(path):
    """读图 → (rgb uint8 HxWx3, display QPixmap)。兼容旧接口；新载图路径走 _decode_rgb 后台解码。"""
    arr = _decode_rgb(path)
    return arr, _rgb_to_pixmap(arr)


def _rgb_to_pixmap(arr):
    arr = np.ascontiguousarray(arr)
    qimg = QtGui.QImage(arr.tobytes(), arr.shape[1], arr.shape[0],
                        arr.shape[1] * 3, QtGui.QImage.Format.Format_RGB888).copy()
    return QtGui.QPixmap.fromImage(qimg)


def channel_pixmap(img8_channel: np.ndarray, tint) -> QtGui.QPixmap:
    """8-bit 解卷积通道（暗=强染色）→ 着色 QPixmap：白底，染色越强 = 越饱和的 tint 色。"""
    s = (255.0 - img8_channel.astype(np.float32)) / 255.0
    tr, tg, tb = tint
    out = np.empty((*img8_channel.shape, 3), np.uint8)
    out[..., 0] = np.clip(255.0 - s * (255 - tr), 0, 255).astype(np.uint8)
    out[..., 1] = np.clip(255.0 - s * (255 - tg), 0, 255).astype(np.uint8)
    out[..., 2] = np.clip(255.0 - s * (255 - tb), 0, 255).astype(np.uint8)
    return _rgb_to_pixmap(out)


def _mask_outline(m: np.ndarray, thick: int = 1) -> np.ndarray:
    """选区边界（内边缘 1px，可加粗）。纯 numpy，蚂蚁线/轮廓用。"""
    e = np.zeros(m.shape, bool)
    e[:-1] |= m[:-1] & ~m[1:]; e[1:] |= m[1:] & ~m[:-1]
    e[:, :-1] |= m[:, :-1] & ~m[:, 1:]; e[:, 1:] |= m[:, 1:] & ~m[:, :-1]
    for _ in range(max(0, thick - 1)):   # 加粗：向内并扩
        d = np.zeros(m.shape, bool)
        d[:-1] |= e[1:]; d[1:] |= e[:-1]; d[:, :-1] |= e[:, 1:]; d[:, 1:] |= e[:, :-1]
        e |= d & m
    return e


def _overlay_array(rgb: np.ndarray, pos_mask: np.ndarray, color=(0, 220, 255),
                   gray_bg: bool = False, outline: bool = False) -> np.ndarray:
    """阳性可视化：选中区用醒目对比色 `color` 标出（红在红/粉组织上隐形 → 默认亮绿/青）。

    - outline=True：只画选区**轮廓**（蚂蚁线式），原组织全程可见，便于对照验证（推荐）。
    - outline=False：半透明填充选区。
    - gray_bg=True：非阳性去色压暗成灰底（选区稀疏时更跳）。
    """
    f = rgb.astype(np.float32)
    col = np.asarray(color, np.float32)
    if gray_bg:
        gray = f.mean(axis=2, keepdims=True)
        out = np.repeat(gray * 0.50 + 15.0, 3, axis=2)
    else:
        out = f.copy()
    if pos_mask is not None and pos_mask.any():
        m = pos_mask
        if outline:
            out[_mask_outline(m, thick=2)] = col
        else:
            a = 0.78 if gray_bg else 0.55
            out[m] = f[m] * (1.0 - a) + col * a
    return np.clip(out, 0, 255).astype(np.uint8)


def overlay_pixmap(rgb: np.ndarray, pos_mask: np.ndarray, color=(0, 220, 255),
                   gray_bg: bool = False, outline: bool = False) -> QtGui.QPixmap:
    """原图 + 阳性选区醒目叠加（默认原色 + 轮廓/填充对比色高亮）。"""
    return _rgb_to_pixmap(_overlay_array(rgb, pos_mask, color, gray_bg, outline))


class MaskEditView(ROIView):
    """ROIView + 不规则阳性选区编辑：**画笔**（圆刷涂）/ **套索**（freehand 自由圈选）。

    组织染色区是无规则形状 → 画笔精修、套索快速大块加/减。左键=加，右键或 Alt+左键=减。
    tool='roi' 时退化为原 ROIView 矩形框（沿用 WB）。不影响 WB 用法。
    """
    maskStroke = QtCore.Signal(int, int, int, bool)        # 画笔：x, y, 半径(px), additive
    lassoStroke = QtCore.Signal(object, bool)              # 套索：[(x,y)...], additive
    editBegan = QtCore.Signal()                            # 一次编辑操作开始 → 上层存撤销快照
    peekOriginal = QtCore.Signal(bool)                     # 按住 C = 闪烁看原图（True 按下/False 松开）
    toggleView = QtCore.Signal()                           # Tab = 在原图↔叠加间来回切换
    cellClick = QtCore.Signal(int, int, bool)              # 加/减核：x, y, 左键(True=加核/False=删核)

    def __init__(self):
        super().__init__()
        self.tool = "brush"           # roi / brush / lasso / count
        self.brush_radius = 14
        self._painting = False
        self._add = True
        self._lasso = None            # list[QPointF]（场景坐标），套索进行中
        self._space_pan = False       # 空格按住 → 手型拖拽平移画布
        self._saved_drag = None
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)   # 收键盘（空格平移）

    def _is_edit(self):
        return self.tool in ("brush", "lasso")

    def _edit_cursor(self):
        return (QtCore.Qt.CursorShape.CrossCursor
                if (self._is_edit() or self.tool == "count") else QtCore.Qt.CursorShape.ArrowCursor)

    def set_base(self, pix, full_w, full_h):
        """设底图：pix 可为下采样代理；scene 坐标系保持【全分辨率】(full_w×full_h)，靠 pixmap item
        setScale 拉伸占满 → ROI/画笔/套索/质心几何(全是图像 px)一行不改，只是显示用小图，重绘快。"""
        self.scene().clear()
        self._pix_item = self.scene().addPixmap(pix)
        self._pix_item.setScale(full_w / pix.width() if pix.width() else 1.0)
        self._pix_item.setTransformationMode(QtCore.Qt.TransformationMode.SmoothTransformation)
        self.scene().setSceneRect(0, 0, full_w, full_h)
        self.rois.clear(); self.roi_ids.clear(); self.markers.clear(); self.sel = -1
        self.fitInView(self.scene().sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.viewport().update()

    def event(self, e):
        # Tab 默认用于控件焦点切换 → 在 event 层截获，做"原图↔叠加"一键切换
        if e.type() == QtCore.QEvent.Type.KeyPress and e.key() == QtCore.Qt.Key.Key_Tab:
            self.toggleView.emit(); return True
        return super().event(e)

    def focusOutEvent(self, e):
        # 失焦（Alt+Tab/弹窗夺焦）时若 C/空格按住，keyRelease 不会到达 → 在此复位，防卡死
        if self._space_pan:
            self._space_pan = False
            self.setDragMode(self._saved_drag if self._saved_drag is not None
                             else QtWidgets.QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(self._edit_cursor())
        self._painting = False; self._lasso = None
        self.peekOriginal.emit(False)        # 取消可能卡住的 C 闪烁
        super().focusOutEvent(e)

    # 空格按住 = 手型平移（Photoshop 式）；C 按住 = 闪烁看原图对照
    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Space and not e.isAutoRepeat():
            self._painting = False; self._lasso = None   # 中途进平移：丢弃半截笔画，防松手后悬停乱涂
            self._space_pan = True
            self._saved_drag = self.dragMode()
            self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            e.accept(); return
        if e.key() == QtCore.Qt.Key.Key_C and not e.isAutoRepeat():
            self.peekOriginal.emit(True); e.accept(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space_pan = False
            self.setDragMode(self._saved_drag if self._saved_drag is not None
                             else QtWidgets.QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(self._edit_cursor())
            e.accept(); return
        if e.key() == QtCore.Qt.Key.Key_C and not e.isAutoRepeat():
            self.peekOriginal.emit(False); e.accept(); return
        super().keyReleaseEvent(e)

    def _stroke_brush(self, e):
        sp = self.mapToScene(e.position().toPoint())
        self.maskStroke.emit(int(round(sp.x())), int(round(sp.y())), int(self.brush_radius), self._add)

    def mousePressEvent(self, e):
        if self._space_pan:                                  # 平移交给 QGraphicsView(跳过 ROIView 画框)
            return super(ROIView, self).mousePressEvent(e)
        if self.tool == "browse":                            # 浏览：不画框不编辑（滚轮缩放/拖滚动条平移）
            return
        if self.tool == "count" and self._pix_item is not None and \
                e.button() in (QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.RightButton):
            sp = self.mapToScene(e.position().toPoint())     # 加/减核：左键补漏数核、右键删错点（Cell Counter 式）
            self.cellClick.emit(int(round(sp.x())), int(round(sp.y())),
                                e.button() == QtCore.Qt.MouseButton.LeftButton)
            return
        if self._is_edit() and self._pix_item is not None and \
                e.button() in (QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.RightButton):
            alt = bool(e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier)
            self._add = not (alt or e.button() == QtCore.Qt.MouseButton.RightButton)
            self.editBegan.emit()                            # 编辑前存撤销快照
            if self.tool == "brush":
                self._painting = True; self._stroke_brush(e)
            else:
                self._lasso = [self.mapToScene(e.position().toPoint())]
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._space_pan:
            return super(ROIView, self).mouseMoveEvent(e)
        if self.tool == "brush" and self._painting:
            self._stroke_brush(e); return
        if self.tool == "lasso" and self._lasso is not None:
            self._lasso.append(self.mapToScene(e.position().toPoint())); self.viewport().update(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._space_pan:
            return super(ROIView, self).mouseReleaseEvent(e)
        if self.tool == "brush" and self._painting:
            self._painting = False; return
        if self.tool == "lasso" and self._lasso is not None:
            pts = [(int(round(p.x())), int(round(p.y()))) for p in self._lasso]
            add = self._add; self._lasso = None; self.viewport().update()
            if len(pts) >= 3:
                self.lassoStroke.emit(pts, add)
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if self._is_edit():
            return   # 编辑模式：右键=减选区，不弹 ROI 右键菜单
        super().contextMenuEvent(e)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self._lasso and len(self._lasso) > 1:   # 画套索进行中的红色轮廓
            pen = QtGui.QPen(QtGui.QColor("#ff2d2d" if self._add else "#2d8cff"), 0)
            pen.setCosmetic(True); pen.setWidthF(1.6); pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QtGui.QPolygonF(self._lasso))


class HistRangeSlider(QtWidgets.QWidget):
    """ImageJ Threshold 式：直方图背景 + 双手柄([下限,上限])可拖 range 滑块。

    红色叠加在 [lo,hi] 区间内为阳性（暗=强 → 通常 lo=0、拖 hi 收紧）。两个手柄都能拖。
    """
    rangeChanged = QtCore.Signal(int, int)   # lo, hi（拖动中实时发）

    def __init__(self, vmin=0, vmax=255):
        super().__init__()
        self._vmin = vmin; self._vmax = vmax
        self._lo = vmin; self._hi = vmax
        self._hist = None                    # 归一化(sqrt)后的直方图，0..1
        self._drag = None                    # 'lo' / 'hi' / None
        self.setMinimumSize(220, 40); self.setMaximumHeight(44)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)

    def set_histogram(self, hist):
        if hist is None:
            self._hist = None
        else:
            h = np.sqrt(np.asarray(hist, dtype=np.float64))   # sqrt 压背景峰，看清谷
            self._hist = h / (h.max() or 1.0)
        self.update()

    def set_values(self, lo, hi):
        self._lo = int(max(self._vmin, min(int(lo), self._vmax)))
        self._hi = int(max(self._lo, min(int(hi), self._vmax)))
        self.update()

    def values(self):
        return self._lo, self._hi

    def _track(self):
        m = 7
        return m, self.width() - m

    def _v2x(self, v):
        x0, x1 = self._track()
        return x0 + (v - self._vmin) / max(1, (self._vmax - self._vmin)) * (x1 - x0)

    def _x2v(self, x):
        x0, x1 = self._track()
        t = (x - x0) / max(1.0, (x1 - x0))
        v = int(round(self._vmin + t * (self._vmax - self._vmin)))
        return max(self._vmin, min(self._vmax, v))   # 夹取 [vmin,vmax]：手柄拖出轨道也不显示越界值

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor("#1c2026"))
        x0, x1 = self._track()
        if self._hist is not None and len(self._hist):
            n = len(self._hist); bw = (x1 - x0) / n
            p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(QtGui.QColor("#525c6b"))
            for i, hv in enumerate(self._hist):
                bh = float(hv) * (h - 8)
                if bh > 0.3:
                    p.drawRect(QtCore.QRectF(x0 + i * bw, h - 3 - bh, max(1.0, bw), bh))
        lx, hx = self._v2x(self._lo), self._v2x(self._hi)
        p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(QtGui.QColor(0, 200, 255, 70))
        p.drawRect(QtCore.QRectF(lx, 2, hx - lx, h - 4))
        for x in (lx, hx):                       # 两个手柄
            pen = QtGui.QPen(QtGui.QColor("#00d0ff")); pen.setWidth(2); p.setPen(pen)
            p.drawLine(QtCore.QPointF(x, 1), QtCore.QPointF(x, h - 1))
            p.setBrush(QtGui.QColor("#00d0ff")); p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRect(QtCore.QRectF(x - 3, h / 2 - 6, 6, 12))
        p.end()

    def _nearest(self, x):
        return "lo" if abs(x - self._v2x(self._lo)) <= abs(x - self._v2x(self._hi)) else "hi"

    def mousePressEvent(self, e):
        self._drag = self._nearest(e.position().x()); self._apply(e)

    def mouseMoveEvent(self, e):
        if self._drag:
            self._apply(e)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def _apply(self, e):
        v = self._x2v(e.position().x())
        if self._drag == "lo":
            self._lo = min(v, self._hi)
        else:
            self._hi = max(v, self._lo)
        self.update(); self.rangeChanged.emit(self._lo, self._hi)


class _DeconvWorker(QtCore.QObject):
    """后台跑【解码大TIF + 16bit重标定 + colour_deconvolution / 天狼星红 rgb2lab】，主线程不冻结。

    source: ('path', 路径) 解码 / ('qimg', QImage) 当前图层转换 / ('rgb', 数组) 已有数组。
    结果带 token 防过期回调覆盖新图。全在非 GUI 线程：QImage/numpy 可跨线程，不建 QPixmap(主线程 done 才建)。
    """
    done = QtCore.Signal(object)   # dict: token/ok/mode/rgb/conc/img8/sr_pre/error

    def __init__(self):
        super().__init__(); self._job = None

    def set_job(self, source, mode, stain, token):
        self._job = (source, mode, stain, token)

    @QtCore.Slot()
    def run(self):
        job, self._job = self._job, None   # 取走即清空：防两次 trigger 都读到同一新 job(旧 job 漏跑/新 job 跑两遍)
        if job is None:
            return
        source, mode, stain, token = job
        res = {"token": token, "mode": mode}
        try:
            kind, val = source
            if kind == "path":
                rgb = _decode_rgb(val)                       # 解码+16bit重标定（大TIF的卡顿主因，移到后台）
            elif kind == "qimg":
                q = val.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                w, h = q.width(), q.height()
                rgb = np.ascontiguousarray(np.frombuffer(q.constBits(), np.uint8).reshape(h, w, 4)[..., :3])
            else:
                rgb = val
            res["rgb"] = rgb
            if mode == "red":
                res["sr_pre"] = iq.sirius_red_precompute(rgb)   # rgb2lab 很贵（~3s/4k）
            else:
                res["conc"], res["img8"] = iq.colour_deconvolution(rgb, stain)   # ~1.3s/4k
            res["ok"] = True
        except Exception as e:  # noqa: BLE001 —— 失败上浮到 UI，不静默
            res["ok"] = False; res["error"] = str(e)
        self.done.emit(res)


def _compute_count(sel, tm, hema, ck, mode, li_on):
    """计数核心（团块 / 苏木素逐核），返回 (阳性数, 阳性质心[(x,y)], 总核数|None)。

    纯 cv2+scipy，可在后台线程跑。li 不在此算（由调用方按有效质心+li_on 门控）。
    与 ihc_batch.count_and_li 逐口径一致（同 count_cells/classify_nuclei 参数）。
    """
    if mode == "nuclei" and hema is not None:
        r = iq.classify_nuclei(hema, tm, sel, **ck)
        return r["positive"], r["pos_centroids"], r["total"]
    cc = iq.count_cells(sel, **ck)
    total = None
    if li_on and hema is not None:                       # 路线B：总核=苏木素核∪DAB核
        hthr = iq.otsu_threshold(hema[tm]) if tm.any() else 0
        total = iq.count_cells(((hema <= hthr) & tm) | sel, **ck)["count"]
    return cc["count"], cc["centroids"], total


class _CountWorker(QtCore.QObject):
    """后台跑全图计数（count_cells/classify_nuclei，distanceTransform+分水岭，大密图秒级），主线程不冻结。"""
    done = QtCore.Signal(object)   # dict: token/ok/cnt/cents/total/error

    def __init__(self):
        super().__init__(); self._job = None

    def set_job(self, sel, tm, hema, ck, mode, li_on, token):
        self._job = (sel, tm, hema, ck, mode, li_on, token)

    @QtCore.Slot()
    def run(self):
        job, self._job = self._job, None
        if job is None:
            return
        sel, tm, hema, ck, mode, li_on, token = job
        res = {"token": token}
        try:
            cnt, cents, total = _compute_count(sel, tm, hema, ck, mode, li_on)
            res.update(cnt=cnt, cents=cents, total=total, ok=True)
        except Exception as e:  # noqa: BLE001
            res["ok"] = False; res["error"] = str(e)
        self.done.emit(res)


class IHCAnalyzerPanel(QtWidgets.QWidget):
    """单图 IHC / 组织化学定量。"""

    COLS = ["#", "测量", "阳性面积%", "阳性细胞数", "总核数", "阳性率%", "阈值", "平均OD", "IOD", "H-score", "IHC分(0-4)", "分级", "阳性px", "组织px"]
    _PROXY_MAX = 1500            # 大图交互预览的代理长边（拖阈值在代理上算，停手用全分辨率精算）
    _deconv_trigger = QtCore.Signal()   # 跨线程触发 deconv worker.run
    _count_trigger = QtCore.Signal()    # 跨线程触发 count worker.run

    def __init__(self, editor=None):
        super().__init__()
        self.editor = editor
        self._rgb = None            # 原始 RGB uint8 (H,W,3)
        # 大图性能：解卷积后台线程 + 下采样预览代理
        self._proxy_rgb = None; self._proxy_img8 = None; self._proxy_tissue = None
        self._proxy_sr_pre = None; self._proxy_scale = 1.0
        self._need_base = True      # 新图首帧用 set_base 设 scale/sceneRect/fitInView/清ROI；后续帧只 setPixmap
        self._deconv_thread = None; self._deconv_worker = None; self._deconv_token = 0
        self._count_thread = None; self._count_worker = None; self._count_token = 0
        self._closing = False       # 退出中：禁止防抖回调重建后台线程（铁律③：teardown 期 QThread 销毁硬崩）
        self._auto_count = {"cnt": None, "cents": [], "total": None}   # 全图自动计数缓存（后台填；人工加减核基于它即时算）
        self._conc = None           # 解卷积浓度 (3,H,W) float
        self._img8 = None           # 解卷积 8-bit (3,H,W)（暗=强）
        self._stain = "H DAB"
        self._names = iq.STAIN_CHANNEL_NAMES["H DAB"]
        self._mode = "deconv"       # deconv / red
        self._target_idx = 1
        self._thr_mode = "otsu"
        self._manual_thr = 180      # 上限 hi（暗=强 → ≤hi 为阳性）
        self._manual_lo = 0         # 下限 lo（排除过暗伪影；与 hi 组成 ImageJ 式 [min,max] 区间）
        self._sat_min = 50          # 天狼星红灵敏度 0-100（a*−b* 阈值，默认 50）
        self._exclude_bg = True
        self._display = "overlay"
        self._ov_color = _OVERLAY_COLORS["青"]      # 阳性叠加色（默认青，对所有染色对比强；红会撞色）
        self._ov_gray = False                       # 叠加灰底（非阳性去色，默认关=保留组织色）
        self._ov_outline = False                    # 轮廓/蚂蚁线（默认关=半透明填充，对密集纤维更清晰）
        self._sr_pre = None         # 天狼星红 rgb2lab 预计算缓存（贵，每图一次；拖灵敏度不重算）
        self._hist_target = -1      # 直方图当前对应的通道下标（变了才重算喂给双头滑块）
        self._tissue = None         # 组织掩膜 (H,W) bool
        self._pos_mask = None       # 当前阳性选区 (H,W) bool（自动阈值得来，可手动修正）
        self._mask_edited = False   # 用户是否手动改过选区（改过则参数不变时不重建）
        self._count_on = False      # 阳性细胞计数（CD31/Ki67 数个数）
        self._cell_size = 8         # 核大小(px)≈分裂粒度，越大越不易把粘连核拆过头
        self._cell_circ = 0.0       # 圆度过滤 [0,1]（0=不过滤；剔细长非核伪影，对齐 Analyze Particles）
        self._li_on = False         # 算阳性率%(标记指数 LI=阳性核/总核)，核标记(Ki67/ER/PR)金标准读数
        self._count_mode = "blob"   # 计数口径：blob=DAB+团块直接数(默认) / nuclei=苏木素逐核判正负(金标准)
        self._cell_centroids = []   # 全图阳性细胞质心（叠加打点；= 自动 + 人工修正后）
        self._manual_add = []       # 人工补的核质心 [(x,y)]（图像全图坐标）
        self._manual_del = []       # 人工删的位置 [(x,y)]（命中自动核则不计入）
        self._cells_edited = False  # 是否人工加减过核（summary 标注）
        self._undo_cells = []       # 加减核撤销栈（与选区 packbits 栈分开，存 (add[:],del[:])）
        self._count_timer = QtCore.QTimer(self); self._count_timer.setSingleShot(True)
        self._count_timer.timeout.connect(self._submit_count)   # 防抖后台计数（调参/拖阈值/画刷停手触发）
        self._cur_thr = 0
        self._red_full = None
        self._loaded_path = None
        self._results = []
        self._undo = []             # 撤销栈：(packbits, shape) 选区快照（packbits 省内存）
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(6)
        tc = theme.colors()

        def _qpush(text, icon_name=None, primary=False):
            b = QtWidgets.QPushButton(text); b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor); b.setMinimumHeight(27)
            if primary: b.setProperty("primary", True)
            if icon_name: b.setIcon(icons.tool_icon(icon_name, tc["text"], 16)); b.setIconSize(QtCore.QSize(16, 16))
            return b

        def _qtool(icon_name, text, tip):
            b = QtWidgets.QToolButton(); b.setObjectName("ihcToolButton"); b.setText(text); b.setToolTip(tip)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setIcon(icons.tool_icon(icon_name, tc["text"], 16)); b.setIconSize(QtCore.QSize(16, 16))
            return b

        def _lab(text):
            x = QtWidgets.QLabel(text); x.setObjectName("ihcFieldLabel"); return x

        # 顶栏
        top = QtWidgets.QFrame(); top.setObjectName("ihcTopBar")
        bar = QtWidgets.QHBoxLayout(top); bar.setContentsMargins(8, 6, 8, 6); bar.setSpacing(6)
        title = QtWidgets.QLabel("IHC / 组织化学定量"); title.setObjectName("ihcPanelTitle")
        bar.addWidget(title); bar.addSpacing(4)
        b_open = _qpush("载入图片…", "folder", True); b_open.clicked.connect(self.open_file)
        b_layer = _qpush("用当前图层", "import_image"); b_layer.clicked.connect(self.use_active_layer)
        bar.addWidget(b_open); bar.addWidget(b_layer); bar.addSpacing(8)
        bar.addWidget(_lab("染色"))
        self.cb_stain = QtWidgets.QComboBox()
        self.cb_stain.addItems(list(iq.STAIN_VECTORS.keys()) + [SIRIUS_RED_KEY])
        self.cb_stain.setCurrentText("H DAB")
        self.cb_stain.setToolTip("颜色解卷积方案（Fiji Colour Deconvolution 同名向量）。\nDAB 免疫组化用 H DAB；Masson 胶原用 Masson Trichrome；天狼星红走红色面积法。")
        self.cb_stain.currentTextChanged.connect(self._on_stain_changed)
        bar.addWidget(self.cb_stain)
        bar.addWidget(_lab("显示"))
        self.cb_disp = QtWidgets.QComboBox()
        self.cb_disp.setToolTip("原图/阳性叠加/各解卷积通道。阳性叠加 = 当前被算作阳性的像素（高亮对比色）。")
        self.cb_disp.currentIndexChanged.connect(self._on_display_changed)
        bar.addWidget(self.cb_disp)
        bar.addWidget(_lab("叠加色"))
        self.cb_ovcolor = QtWidgets.QComboBox()
        for nm in _OVERLAY_COLORS:
            self.cb_ovcolor.addItem(nm, _OVERLAY_COLORS[nm])
        self.cb_ovcolor.setCurrentText("青")
        self.cb_ovcolor.setToolTip("阳性高亮色。红色在红/粉组织上看不清 → 默认青色（对所有染色对比强）。")
        self.cb_ovcolor.currentIndexChanged.connect(self._on_overlay_style)
        bar.addWidget(self.cb_ovcolor)
        self.chk_outline = QtWidgets.QCheckBox("轮廓")
        self.chk_outline.setToolTip("只画选区轮廓(蚂蚁线式)，原组织全程可见。适合大块区域；密集纤维网络建议用填充。")
        self.chk_outline.toggled.connect(self._on_overlay_style)
        bar.addWidget(self.chk_outline)
        self.chk_gray = QtWidgets.QCheckBox("灰底")
        self.chk_gray.setToolTip("非阳性区去色压暗成灰底，阳性更跳（选区稀疏时好用）。默认关=保留原组织色便于对照。")
        self.chk_gray.toggled.connect(self._on_overlay_style)
        bar.addWidget(self.chk_gray)
        self.btn_peek = QtWidgets.QToolButton(); self.btn_peek.setObjectName("ihcToolButton"); self.btn_peek.setText("原图对照")
        self.btn_peek.setToolTip("核对选区：按 Tab 在 原图↔叠加 间一键来回切；或按住此钮/键盘 C 临时看原图，松开回叠加。")
        self.btn_peek.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_peek.pressed.connect(lambda: self._peek(True))
        self.btn_peek.released.connect(lambda: self._peek(False))
        bar.addWidget(self.btn_peek)
        bar.addStretch(1)
        bz = QtWidgets.QToolButton(); bz.setObjectName("ihcToolButton"); bz.setText("适应")
        bz.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        bz.setToolTip("适应窗口（滚轮缩放/拖滚动条平移）"); bz.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        bz.clicked.connect(lambda: self.view.zoom_fit()); bar.addWidget(bz)
        bzi = _qtool("zoom", "", "放大"); bzi.clicked.connect(lambda: self.view.zoom_by(1.25)); bar.addWidget(bzi)
        bzo = _qtool("zoom_out", "", "缩小"); bzo.clicked.connect(lambda: self.view.zoom_by(0.8)); bar.addWidget(bzo)
        root.addWidget(top)

        # 动作栏：测量通道 + 阈值 + 背景 + 手动修正画笔 + 测量/清空/导出
        action = QtWidgets.QFrame(); action.setObjectName("ihcActionBar")
        bar2 = QtWidgets.QHBoxLayout(action); bar2.setContentsMargins(8, 6, 8, 6); bar2.setSpacing(6)
        bar2.addWidget(_lab("测量通道"))
        self.cb_target = QtWidgets.QComboBox(); self.cb_target.setMinimumWidth(92)
        self.cb_target.setToolTip("量化哪个解卷积通道：DAB-IHC 选 DAB；Masson 胶原选 Aniline blue。")
        self.cb_target.currentIndexChanged.connect(self._on_target_changed)
        bar2.addWidget(self.cb_target)
        bar2.addWidget(_lab("阈值"))
        self.cb_thr = QtWidgets.QComboBox(); self.cb_thr.addItems(["Otsu 自动", "手动"])
        self.cb_thr.setToolTip("Otsu=组织内自动给初值；手动=用双头滑块定区间。直接拖手柄即转手动。")
        self.cb_thr.currentIndexChanged.connect(self._on_thrmode_changed)
        bar2.addWidget(self.cb_thr)
        # 双头直方图滑块（deconv 模式，ImageJ Threshold 式 [下限,上限]）
        self.range_slider = HistRangeSlider(0, 255); self.range_slider.setMinimumWidth(230)
        self.range_slider.setToolTip("ImageJ Threshold 式：直方图上拖两个手柄定 [下限,上限]。\n阳性 = 下限 ≤ 通道值 ≤ 上限（暗=强 → 一般 lo=0、拖右手柄收紧）。拖即转手动。")
        self.range_slider.rangeChanged.connect(self._on_range_changed)
        bar2.addWidget(self.range_slider)
        # 单滑块（red 灵敏度，0-100）
        self.sl_thr = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.sl_thr.setRange(0, 100)
        self.sl_thr.setValue(self._sat_min); self.sl_thr.setFixedWidth(150)
        self.sl_thr.valueChanged.connect(self._on_threshold_slider)
        bar2.addWidget(self.sl_thr)
        self.lb_thr = _lab(""); bar2.addWidget(self.lb_thr)
        self.chk_bg = QtWidgets.QCheckBox("排除近白背景"); self.chk_bg.setChecked(True)
        self.chk_bg.setToolTip("把近白(玻片)像素排除在分母外，胶原%/阳性%才是占组织的比例。")
        self.chk_bg.toggled.connect(self._on_bg_toggled)
        bar2.addWidget(self.chk_bg)
        bar2.addSpacing(8)
        # 阳性细胞计数（CD31/Ki67 数个数；分水岭分开粘连核 + 面积过滤，对齐 Fiji Watershed+Analyze Particles）
        self.chk_count = QtWidgets.QCheckBox("计数阳性核")
        self.chk_count.setToolTip("数阳性细胞核个数（CD31/Ki67 增殖细胞计数）；勾选后结果表加「阳性细胞数」列、叠加打点标记。\n分水岭自动分开粘连核；「核大小」调分裂粒度+剔噪。")
        self.chk_count.toggled.connect(self._on_count_toggled)
        bar2.addWidget(self.chk_count)
        self.sp_cell = QtWidgets.QSpinBox(); self.sp_cell.setRange(2, 60); self.sp_cell.setValue(self._cell_size)
        self.sp_cell.setPrefix("核 "); self.sp_cell.setSuffix(" px")
        self.sp_cell.setToolTip("核大小≈最小核半径：调大→不易把粘连核拆过头、剔小噪点；调小→分得更细")
        self.sp_cell.valueChanged.connect(self._on_cell_size)
        bar2.addWidget(self.sp_cell)
        self.btn_auto_cell = _qpush("自动核大小", "wand")
        self.btn_auto_cell.setToolTip("从当前阳性区自动估计典型核尺度填入「核 px」（距离变换内切半径中位数）；给初值，可再微调")
        self.btn_auto_cell.clicked.connect(self._auto_cell_size)
        bar2.addWidget(self.btn_auto_cell)
        self.sp_circ = QtWidgets.QDoubleSpinBox(); self.sp_circ.setRange(0.0, 1.0); self.sp_circ.setSingleStep(0.05)
        self.sp_circ.setDecimals(2); self.sp_circ.setValue(0.0); self.sp_circ.setPrefix("圆度≥ ")
        self.sp_circ.setToolTip("圆度过滤(对齐 ImageJ Analyze Particles，4π×面积/周长²)：剔细长非核伪影(血管壁/拖尾)。0=不过滤，核常用 0.5+")
        self.sp_circ.valueChanged.connect(self._on_cell_circ)
        bar2.addWidget(self.sp_circ)
        self.chk_li = QtWidgets.QCheckBox("阳性率%")
        self.chk_li.setToolTip("算标记指数 LI=阳性核/总核(Ki67/ER/PR 核标记金标准)：对苏木素复染通道数总核，结果表加「总核数」「阳性率%」。\n仅 DAB+苏木素类染色有效；需先勾「计数阳性核」。")
        self.chk_li.toggled.connect(self._on_li_toggled)
        bar2.addWidget(self.chk_li)
        self.cb_count_mode = QtWidgets.QComboBox()
        self.cb_count_mode.addItem("团块", "blob"); self.cb_count_mode.addItem("逐核", "nuclei")
        self.cb_count_mode.setToolTip("计数口径：团块=直接数 DAB+ 色块(快)；逐核=苏木素数所有核逐核判正负(病理金标准,Ki67/ER/PR 建议;需 DAB+苏木素)")
        self.cb_count_mode.currentIndexChanged.connect(self._on_count_mode)
        bar2.addWidget(self.cb_count_mode)
        bar2.addSpacing(8)
        self.b_auto = _qpush("自动阳性", "wand", True)
        self.b_auto.setToolTip("按当前染色/通道/阈值自动识别阳性区（高亮叠加显示）。之后可用套索/画笔在此基础上加/减修正。")
        self.b_auto.clicked.connect(self._auto_positive); bar2.addWidget(self.b_auto)
        bar2.addSpacing(8)
        # 不规则选区编辑工具（组织染色区无规则）：画笔精修 / 套索大块圈 / 框选矩形子区域
        bar2.addWidget(_lab("工具"))
        self.cb_tool = QtWidgets.QComboBox()
        self.cb_tool.addItem("画笔", "brush"); self.cb_tool.addItem("套索", "lasso"); self.cb_tool.addItem("框选区域", "roi")
        self.cb_tool.addItem("加/减核", "count")
        self.cb_tool.setToolTip("画笔/套索 = 改阳性选区（不规则，纠正自动多选/漏选）：左键=加，右键或 Alt+左键=减。\n框选区域 = 画矩形子区域分别统计。\n加/减核 = 计数手动修正(需先勾计数)：左键补漏数核、右键删多数的核（只改计数不改面积%）。")
        self.cb_tool.currentIndexChanged.connect(self._on_tool_changed)
        bar2.addWidget(self.cb_tool)
        self.sp_brush = QtWidgets.QSpinBox(); self.sp_brush.setRange(2, 200); self.sp_brush.setValue(14)
        self.sp_brush.setPrefix("笔 "); self.sp_brush.setSuffix(" px"); self.sp_brush.setToolTip("画笔半径")
        self.sp_brush.valueChanged.connect(lambda v: setattr(self.view, "brush_radius", int(v)))
        bar2.addWidget(self.sp_brush)
        self.btn_undo = _qpush("撤销", "undo"); self.btn_undo.setToolTip("撤销上一步选区编辑（Ctrl+Z）")
        self.btn_undo.clicked.connect(self.undo); bar2.addWidget(self.btn_undo)
        self.btn_reset_mask = _qpush("重置选区", "trash"); self.btn_reset_mask.setToolTip("丢弃所有手动修正，回到自动阈值选区")
        self.btn_reset_mask.clicked.connect(self._reset_mask); bar2.addWidget(self.btn_reset_mask)
        bar2.addStretch(1)
        b_clear = _qpush("清空 ROI", "trash"); b_clear.clicked.connect(self.clear_rois); bar2.addWidget(b_clear)
        self.b_batch = _qpush("批量定量…", "copy")
        self.b_batch.setToolTip("多张图一次跑完阳性面积%（统一设置）：左列表+叠加预览审核+组聚合(模型 vs 对照)+CSV。")
        self.b_batch.clicked.connect(self.open_batch); bar2.addWidget(self.b_batch)
        self.b_csv = _qpush("导出 CSV", "download"); self.b_csv.clicked.connect(self.export_csv); bar2.addWidget(self.b_csv)
        root.addWidget(action)

        # 主体
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); split.setObjectName("ihcSplit")
        self.view = MaskEditView(); self.view.setObjectName("ihcImageView")
        self.view.roiAdded.connect(self.measure)
        self.view.maskStroke.connect(self._on_mask_stroke)
        self.view.lassoStroke.connect(self._on_lasso_stroke)
        self.view.cellClick.connect(self._on_cell_click)
        self.view.editBegan.connect(self._push_undo)
        self.view.peekOriginal.connect(self._peek)
        self.view.toggleView.connect(self._toggle_view)
        self.view.tool = "brush"
        self.view.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Undo, self, activated=self.undo)   # Ctrl+Z
        # 拖阈值/灵敏度防抖：快速拖动只在停顿 40ms 后重算一次（避免每 tick 重建卡顿）
        self._thr_timer = QtCore.QTimer(self); self._thr_timer.setSingleShot(True)
        self._thr_timer.timeout.connect(self._rebuild_and_measure)
        # 轮廓模式画笔重绘合并：60ms 内多次涂改只全图重绘一次
        self._ov_timer = QtCore.QTimer(self); self._ov_timer.setSingleShot(True)
        self._ov_timer.timeout.connect(lambda: self._show_pixmap(self._overlay_now()) if self._rgb is not None else None)
        split.addWidget(self.view)
        right = QtWidgets.QFrame(); right.setObjectName("ihcResultsPanel")
        rv = QtWidgets.QVBoxLayout(right); rv.setContentsMargins(8, 8, 8, 8); rv.setSpacing(6)
        rhead = QtWidgets.QHBoxLayout()
        rtitle = QtWidgets.QLabel("结果"); rtitle.setObjectName("ihcSectionTitle"); rhead.addWidget(rtitle)
        rhead.addStretch(1)
        rtag = QtWidgets.QLabel("对齐 Fiji"); rtag.setObjectName("ihcTag"); rhead.addWidget(rtag)
        rv.addLayout(rhead)
        self.lb_summary = QtWidgets.QLabel("载入图片开始。"); self.lb_summary.setObjectName("ihcBannerInfo")
        self.lb_summary.setWordWrap(True); rv.addWidget(self.lb_summary)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS)); self.table.setObjectName("ihcTable")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        _setup_result_table(self.table, self.export_csv)
        self.table.itemSelectionChanged.connect(self._on_table_sel)
        self.empty_results = QtWidgets.QLabel("载入图片并完成测量后，结果会显示在这里。")
        self.empty_results.setObjectName("analysisEmpty")
        self.empty_results.setWordWrap(True)
        rv.addWidget(self.empty_results)
        rv.addWidget(self.table, 1)
        self.hint = QtWidgets.QLabel(); self.hint.setObjectName("ihcFieldLabel"); self.hint.setWordWrap(True)
        rv.addWidget(self.hint)
        split.addWidget(right)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2); split.setSizes([660, 440])
        root.addWidget(split, 1)
        self.status = QtWidgets.QLabel("提示：载入图 → 选染色 → 高亮叠加即所选阳性区；调阈值实时变；「手动修正」画笔加/减。")
        self.status.setObjectName("ihcStatus"); self.status.setWordWrap(True); root.addWidget(self.status)
        self._sync_controls()

    # ---------------- 载入 ----------------
    def open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择染色图片", _last_dir("ihc"), "图片 (*.tif *.tiff *.png *.jpg *.jpeg *.bmp)")
        if path:
            _remember_dir("ihc", path); self.load_path(path)

    def load_path(self, path):
        self._loaded_path = path
        self._reset_for_new_image()
        self._submit_source(("path", path))        # 解码+16bit重标定+解卷积 全后台，主线程不卡

    def use_active_layer(self):
        if self.editor is None:
            return
        layer = getattr(self.editor, "active", None)
        img = layer.get("image") if isinstance(layer, dict) else None
        if img is None or img.isNull():
            QtWidgets.QMessageBox.information(self, "无图层", "没有可用的活动图层图像。请用「载入图片」。"); return
        self._loaded_path = None
        self._reset_for_new_image()
        self._submit_source(("qimg", img))         # QImage→numpy 大拷贝也移后台

    def _set_rgb(self, rgb):
        """rgb 已在手（直传/离屏测试）：后台解卷积，done 回调 _refresh_display 出图（set_base）。"""
        self._reset_for_new_image()
        self._rgb = rgb
        self._submit_source(("rgb", rgb))

    def _reset_for_new_image(self):
        self._sr_pre = None; self._undo.clear()
        self._proxy_rgb = None
        self._rgb = None; self._conc = self._img8 = None
        self._pos_mask = None; self._cell_centroids = []
        self._manual_add = []; self._manual_del = []; self._cells_edited = False; self._undo_cells = []
        self._auto_count = {"cnt": None, "cents": [], "total": None}   # 新图清计数缓存（防显示上一张的旧计数）
        self._count_token += 1      # 使旧图在途计数回调过期丢弃（与 _deconv_token 对称，防幽灵/错位计数）
        self._need_base = True      # 新图：下一帧 set_base 重设 scale/sceneRect/fitInView/清 ROI

    # ---------------- 后台线程（解码+解卷积，主线程不卡）----------------
    def _ensure_deconv_thread(self):
        if self._deconv_thread is None:
            self._deconv_thread = QtCore.QThread(self)
            self._deconv_worker = _DeconvWorker()
            self._deconv_worker.moveToThread(self._deconv_thread)
            self._deconv_worker.done.connect(self._on_deconv_done)
            self._deconv_trigger.connect(self._deconv_worker.run)
            self._deconv_thread.start()

    def _submit_source(self, source):
        """提交一个图源(path/qimg/rgb)到后台：解码→(16bit重标定)→解卷积。token 防快速换图旧结果覆盖新图。"""
        self._deconv_token += 1
        self.status.setText("载入并解卷积中…（大图稍候，界面不会卡）")
        self._ensure_deconv_thread()
        self._deconv_worker.set_job(source, self._mode, self._stain, self._deconv_token)
        self._deconv_trigger.emit()

    def _start_deconv(self):
        """重算当前图的解卷积（换染色用，rgb 已在手）。"""
        if self._rgb is None:
            return
        self._submit_source(("rgb", self._rgb))

    # ---------------- 全图计数（后台线程，调参不冻结）----------------
    def _effective_count(self):
        """全图最终计数 = 后台自动缓存(_auto_count) + 人工加减核修正；分母同步防 LI>100%；li 受 li_on 门控。
        后台未算出时返回缓存（旧值，避免每次 measure 闪「—」）；首次为 None。"""
        ac = self._auto_count
        cnt, cents, total = ac.get("cnt"), list(ac.get("cents", [])), ac.get("total")
        if cnt is None:
            return None, [], None, None
        if self._manual_add or self._manual_del:
            n_before = len(cents)
            cents = self._effective_centroids(cents); cnt = len(cents)
            if total is not None:                 # 加阳性核→总核+1，删自动阳性核→总核-1
                total = max(cnt, total + (cnt - n_before))
        li = (cnt / total * 100.0) if (total and self._li_on) else None
        return cnt, cents, total, li

    def _ensure_count_thread(self):
        if self._closing:
            return
        if self._count_thread is None:
            self._count_thread = QtCore.QThread(self)
            self._count_worker = _CountWorker()
            self._count_worker.moveToThread(self._count_thread)
            self._count_worker.done.connect(self._on_count_done)
            self._count_trigger.connect(self._count_worker.run)
            self._count_thread.start()

    def _submit_count(self):
        """后台重算全图自动计数（count_cells/classify_nuclei）。token 防参数快速变时旧结果覆盖。"""
        if self._closing or not self._count_on or self._mode == "red" or self._pos_mask is None or self._img8 is None:
            return
        sel = self._pos_mask & self._tissue           # & 产生新数组 → 跨线程读安全
        if not sel.any():                             # 零阳性 → None（与批量 count_and_li 空选区口径一致，非 0）
            self._auto_count = {"cnt": None, "cents": [], "total": None}
            self._patch_count(); return
        min_a = max(4, int(round(self._cell_size * self._cell_size * 0.6)))
        ck = dict(min_area=min_a, max_area=min_a * 300, split=True,
                  peak_min_dist=self._cell_size, min_circularity=self._cell_circ)
        cs_idx = self._counterstain_idx()
        hema = self._img8[cs_idx] if cs_idx is not None else None
        self._count_token += 1
        self._ensure_count_thread()
        self._count_worker.set_job(sel, self._tissue, hema, ck, self._count_mode, self._li_on, self._count_token)
        self._count_trigger.emit()

    @QtCore.Slot(object)
    def _on_count_done(self, res):
        if res.get("token") != self._count_token:     # 过期（参数已变）→ 丢弃
            return
        if not res.get("ok"):
            self.status.setText("计数失败：%s" % res.get("error", "")); return
        self._auto_count = {"cnt": res["cnt"], "cents": res["cents"], "total": res["total"]}
        self._patch_count()

    def _patch_count(self):
        """把最新全图计数（缓存+人工修正）回填到结果行/表/标记点（不重算 area 等快指标）。"""
        if not self._results or self._mode == "red":
            return
        cnt, cents, total, li = self._effective_count()
        m = self._results[0][1]
        m["count"], m["centroids"], m["total"], m["li"] = cnt, cents, total, li
        self._cell_centroids = cents
        self._fill_table(); self._update_summary()
        self._refresh_display()

    @QtCore.Slot(object)
    def _on_deconv_done(self, res):
        if res.get("token") != self._deconv_token:   # 过期回调（已换图/换染色）→ fail-loud 丢弃
            return
        self._hist_target = -1
        if not res.get("ok"):
            self.status.setText("载入/解卷积失败：%s" % res.get("error", "")); return
        if res.get("rgb") is not None and self._rgb is None:   # 后台解码出的 rgb（path/qimg 源）
            self._rgb = res["rgb"]                              # 底图由下方 _build_proxy + _refresh_display(set_base) 出
        if res.get("mode") == "red":
            self._sr_pre = res.get("sr_pre")
            self._conc = self._img8 = None
        else:
            self._conc, self._img8 = res["conc"], res["img8"]
            self._names = iq.STAIN_CHANNEL_NAMES.get(self._stain, ["Stain1", "Stain2", "Residual"])
            self._target_idx = self._default_target()
        self._build_proxy()                        # 下采样预览代理（大图拖阈值用）
        self._build_pos_mask()
        self._rebuild_combos()
        self.measure()
        self._refresh_display()
        if self._rgb is not None:
            self.status.setText("已载入 %dx%d。红色=当前阳性区；拖滑块改阈值实时变；「手动修正」画笔加(左键)/减(右键)。"
                                % (self._rgb.shape[1], self._rgb.shape[0]))

    def _build_proxy(self):
        """大图(长边>_PROXY_MAX)生成下采样代理：拖阈值在代理上算预览(快)，停手用全分辨率精算入表。"""
        self._proxy_rgb = self._proxy_img8 = self._proxy_tissue = self._proxy_sr_pre = None
        self._proxy_scale = 1.0
        if self._rgb is None:
            return
        h, w = self._rgb.shape[:2]
        if max(h, w) <= self._PROXY_MAX:
            return                                  # 小图无需代理，拖动直接走全分辨率
        import cv2
        sc = self._PROXY_MAX / float(max(h, w))
        nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
        self._proxy_scale = sc
        self._proxy_rgb = cv2.resize(self._rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        if self._mode == "red":
            self._proxy_sr_pre = iq.sirius_red_precompute(self._proxy_rgb)   # 代理小，rgb2lab 快
        elif self._img8 is not None:
            self._proxy_img8 = [cv2.resize(ch, (nw, nh), interpolation=cv2.INTER_AREA) for ch in self._img8]
            self._proxy_tissue = (iq.tissue_mask(self._proxy_rgb) if self._exclude_bg
                                  else np.ones((nh, nw), bool))

    def _default_target(self):
        n = self._names
        if "DAB" in n: return n.index("DAB")
        for pref in ("Aniline blue", "Alcian blue"):
            if pref in n: return n.index(pref)
        return 0

    # ---------------- 阳性选区（自动阈值 → 可手动修正）----------------
    def _build_pos_mask(self):
        """按当前参数重算组织掩膜 + 自动阳性选区（丢弃手动修正）。"""
        if self._rgb is None:
            return
        if self._mode == "red":
            if self._sr_pre is None:                       # rgb2lab 很贵 → 每张图只算一次缓存
                self._sr_pre = iq.sirius_red_precompute(self._rgb)
            self._red_full = iq.sirius_red_mask(self._sr_pre, self._sat_min)   # 仅施加阈值（廉价）
            self._pos_mask = self._red_full["red_mask"].copy()
            self._tissue = self._red_full["tissue_mask"]   # 用 sirius 组织掩膜，与 area 口径一致
            self._cur_thr = self._sat_min
            self._mask_edited = False
            return
        if self._img8 is None:        # 解卷积尚在后台进行（async）→ 暂不建掩膜，done 回调里会重来
            return
        self._tissue = iq.tissue_mask(self._rgb) if self._exclude_bg else np.ones(self._rgb.shape[:2], bool)
        ch = self._img8[self._target_idx]
        if self._hist_target != self._target_idx:    # 目标通道变了 → 更新直方图(组织内)
            self.range_slider.set_histogram(np.bincount(ch[self._tissue], minlength=256)[:256])
            self._hist_target = self._target_idx
        if self._thr_mode == "otsu":
            hi = iq.otsu_threshold(ch[self._tissue]); lo = 0
        else:
            hi = self._manual_thr; lo = self._manual_lo
        self._cur_thr = int(hi)
        self._pos_mask = (ch >= lo) & (ch <= hi) & self._tissue   # ImageJ 式 [min,max] 区间
        self._mask_edited = False

    def _rebuild_and_measure(self):
        # 重建会丢弃手动修正 → 若有手动修正先存撤销快照（与 _auto_positive/_reset_mask 一致，Ctrl+Z 可救回）
        if self._mask_edited and self._pos_mask is not None:
            self._push_undo()
        self._build_pos_mask()
        if self._mode != "red" and self._thr_mode == "otsu":   # Otsu 算出新阈值 → 同步双头滑块([0,Otsu])
            self.range_slider.set_values(0, self._cur_thr)
            self.lb_thr.setText("Otsu≤%d·可拖调" % self._cur_thr)
        # 阈值定后 measure() 出面积/阈值（快），全图计数由 measure 内防抖 → 后台线程算（不冻 UI）。
        self.measure(); self._refresh_display()

    def _schedule_rebuild(self):
        """阈值/灵敏度拖动防抖：合并快速拖动，停顿后才重算（标签即时更新在调用处）。"""
        self._thr_timer.start(40)

    def _preview_threshold_proxy(self):
        """大图拖阈值即时预览：在下采样代理上算阳性叠加(~15ms)，放大回全尺寸贴显示；
        停手后 _rebuild_and_measure 用全分辨率精算入表。无代理(小图)则跳过，由防抖全分辨率刷。"""
        if self._proxy_rgb is None or self.cb_disp.currentData() != "overlay":
            return
        if self._mode == "red":
            if self._proxy_sr_pre is None:
                return
            pos = iq.sirius_red_mask(self._proxy_sr_pre, self._sat_min)["red_mask"]
        else:
            if self._proxy_img8 is None or self._thr_mode != "manual":
                return                                 # Otsu 拖不动；手动才有 lo/hi
            ch = self._proxy_img8[self._target_idx]
            pos = (ch >= self._manual_lo) & (ch <= self._manual_thr) & self._proxy_tissue
        pm = overlay_pixmap(self._proxy_rgb, pos, self._ov_color, self._ov_gray, self._ov_outline)
        self._show_pixmap(pm)                           # 代理 pixmap 直接贴（item setScale 已占满全分辨率 scene），不再放大回全尺寸

    # ---------------- 下拉/控件 ----------------
    def _rebuild_combos(self):
        self.cb_disp.blockSignals(True); self.cb_target.blockSignals(True)
        self.cb_disp.clear(); self.cb_target.clear()
        self.cb_disp.addItem("阳性叠加", "overlay")
        self.cb_disp.addItem("原图", "rgb")
        if self._mode == "red":
            self.cb_target.addItem("红色胶原", -1)
        else:
            for i, nm in enumerate(self._names):
                self.cb_disp.addItem(nm, i); self.cb_target.addItem(nm, i)
            ti = self.cb_target.findData(self._target_idx)
            if ti >= 0: self.cb_target.setCurrentIndex(ti)
        self.cb_disp.setCurrentIndex(0); self._display = "overlay"
        self.cb_disp.blockSignals(False); self.cb_target.blockSignals(False)
        self._sync_controls()

    def _thr_label_text(self):
        if self._mode == "red":
            return "灵敏度 %d" % self._sat_min
        if self._thr_mode == "manual":
            return "[%d,%d]" % (self._manual_lo, self._manual_thr)
        return "Otsu≤%d·可拖调" % self._cur_thr

    def _sync_controls(self):
        red = (self._mode == "red")
        self.cb_target.setEnabled(not red)
        self.cb_thr.setEnabled(not red)
        self.sl_thr.setVisible(red)            # red=单滑块(灵敏度)
        self.range_slider.setVisible(not red)  # deconv=双头直方图滑块
        if red:
            self.sl_thr.blockSignals(True); self.sl_thr.setValue(self._sat_min); self.sl_thr.blockSignals(False)
            self.chk_bg.setEnabled(False)
            self.hint.setText("天狼星红：CIELAB a*−b*（红度−黄度）法分离红胶原 vs 橙/黄底（占组织）。滑块=灵敏度，调高抓更多 faint 胶原。\n"
                              "高亮叠加=被算作胶原的像素；可用画笔/套索 左键加/右键减 修正。模型 vs 对照比相对值。")
        else:
            hi = self._manual_thr if self._thr_mode == "manual" else self._cur_thr
            self.range_slider.set_values(self._manual_lo if self._thr_mode == "manual" else 0, hi)
            self.chk_bg.setEnabled(True)
            self.hint.setText("阈值：直方图上拖**两个手柄**定 [下限,上限]（ImageJ Threshold 式，暗=强→一般拖右手柄收紧）。Otsu 给初值，拖即转手动。\n"
                              "调完不满意 → 画笔/套索 左键加 / 右键(或Alt)减，逐处纠正多选/漏选（核心修正手段）。\n"
                              "DAB 目标额外给 IHC Profiler 分级/0-4 分；Marker 框右键标记=不计入。")
        self.lb_thr.setText(self._thr_label_text())

    # ---------------- 显示（一律在下采样代理上渲染，scene 坐标仍全分辨率，重绘快）----------------
    def _disp(self):
        """显示用数据：大图返回(代理rgb, 代理img8, 全分辨率→显示的反向scale=代理/全, (pw,ph))，小图返回全分辨率。"""
        if self._proxy_rgb is not None and self._rgb is not None:
            ph, pw = self._proxy_rgb.shape[:2]
            return self._proxy_rgb, self._proxy_img8, (pw / float(self._rgb.shape[1])), (pw, ph)
        return self._rgb, self._img8, 1.0, None

    def _show_pixmap(self, pix):
        if self._count_on and self._cell_centroids:
            pix = self._paint_cell_markers(pix)
        v = self.view
        if self._need_base or v._pix_item is None:    # 新图：set_base 设 scale/sceneRect=全分辨率/fitInView/清ROI
            W = self._rgb.shape[1] if self._rgb is not None else pix.width()
            H = self._rgb.shape[0] if self._rgb is not None else pix.height()
            v.set_base(pix, W, H); self._need_base = False
        else:
            v._pix_item.setPixmap(pix)                # 同图刷新：只换 pixmap（scale 持续）→ 快
        v.viewport().update()

    def _paint_cell_markers(self, pix):
        """在【显示代理】上为每个阳性细胞打标记点。质心是全分辨率图像坐标 → 乘 inv(代理/全) 落到代理像素。"""
        pm = QtGui.QPixmap(pix)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        inv = self._disp()[2]                          # 全分辨率坐标 → 代理显示坐标
        r = max(1.2, self._cell_size * 0.35 * inv)
        pen = QtGui.QPen(QtGui.QColor("#ff1744")); pen.setWidthF(max(1.0, r * 0.5)); pen.setCosmetic(False)
        p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for (cx, cy) in self._cell_centroids:
            p.drawEllipse(QtCore.QPointF(cx * inv, cy * inv), r, r)
        p.end()
        return pm

    def _refresh_display(self):
        """按当前 cb_disp 选择重画底图（阳性叠加随选区变化时调用）。全部走显示代理。"""
        if self._rgb is None:
            return
        drgb, dimg8, _inv, _pd = self._disp()
        data = self.cb_disp.currentData()
        self._display = data
        if data == "overlay":
            self._show_pixmap(self._overlay_now())
        elif data == "rgb" or data is None:
            self._show_pixmap(_rgb_to_pixmap(drgb))
        elif dimg8 is not None and 0 <= int(data) < len(dimg8):
            idx = int(data)
            tint = _CHANNEL_TINT.get(self._names[idx], _DEFAULT_TINT)
            self._show_pixmap(channel_pixmap(dimg8[idx], tint))
        else:
            self._show_pixmap(_rgb_to_pixmap(drgb))   # 解卷积未完成 → 暂显原图

    def _on_display_changed(self, *_):
        self._refresh_display()

    def _peek(self, on):
        """闪烁对照：按住临时显示原图，松开恢复（不改 _display）。也走代理→按 C 不卡。"""
        if self._rgb is None:
            return
        if on:
            self._show_pixmap(_rgb_to_pixmap(self._disp()[0]))
        else:
            self._refresh_display()

    def _toggle_view(self):
        """Tab 一键在 原图 ↔ 阳性叠加 间来回切换。"""
        if self._rgb is None:
            return
        target = "rgb" if self.cb_disp.currentData() == "overlay" else "overlay"
        i = self.cb_disp.findData(target)
        if i >= 0:
            self.cb_disp.setCurrentIndex(i)   # 触发 _refresh_display
            self.status.setText("已切到：%s（Tab 再切回）" % ("原图" if target == "rgb" else "阳性叠加"))

    def _on_overlay_style(self, *_):
        self._ov_color = self.cb_ovcolor.currentData() or _OVERLAY_COLORS["青"]
        self._ov_gray = self.chk_gray.isChecked()
        self._ov_outline = self.chk_outline.isChecked()
        if self._display == "overlay":
            self._refresh_display()

    def _overlay_now(self):
        drgb, _i, _inv, pd = self._disp()
        if self._pos_mask is None:        # 解卷积未完成 → 暂显原图（不崩）
            return _rgb_to_pixmap(drgb)
        pm = self._pos_mask
        if pd is not None:                # 大图：把全分辨率掩膜降采样到代理尺寸（最近邻，~5ms）
            import cv2
            pm = cv2.resize(self._pos_mask.astype(np.uint8), pd, interpolation=cv2.INTER_NEAREST).astype(bool)
        return overlay_pixmap(drgb, pm, self._ov_color, self._ov_gray, self._ov_outline)

    # ---------------- 参数变更（重建选区，丢手动修正）----------------
    def _on_stain_changed(self, name):
        self._stain = name
        self._mode = "red" if name == SIRIUS_RED_KEY else "deconv"
        self._sr_pre = None; self._proxy_rgb = None
        self._start_deconv()   # 后台重算解卷积；done 回调里 build_pos_mask/combos/measure/refresh

    def _on_target_changed(self, *_):
        d = self.cb_target.currentData()
        if d is not None and d != -1:
            self._target_idx = int(d); self._rebuild_and_measure()

    def _on_thrmode_changed(self, idx):
        self._thr_mode = "manual" if idx == 1 else "otsu"
        self._sync_controls(); self._rebuild_and_measure()

    def _on_threshold_slider(self, v):
        # 仅 red 模式可见：灵敏度
        self._sat_min = int(v); self.lb_thr.setText("灵敏度 %d" % v)
        self._preview_threshold_proxy()
        self._schedule_rebuild()

    def _on_range_changed(self, lo, hi):
        # deconv 双头滑块：拖手柄即转手动覆盖 Otsu，定 [下限,上限]
        if self._thr_mode == "otsu":
            self._thr_mode = "manual"
            self.cb_thr.blockSignals(True); self.cb_thr.setCurrentIndex(1); self.cb_thr.blockSignals(False)
        self._manual_lo = int(lo); self._manual_thr = int(hi)
        self.lb_thr.setText("[%d,%d]" % (lo, hi))
        self._preview_threshold_proxy()    # 大图即时代理预览（快）
        self._schedule_rebuild()           # 防抖后全分辨率精算入表

    def _on_bg_toggled(self, on):
        self._exclude_bg = bool(on); self._rebuild_and_measure()

    # ---------------- 不规则选区编辑（画笔/套索）----------------
    def _on_tool_changed(self, *_):
        tool = self.cb_tool.currentData() or "brush"
        self.view._painting = False; self.view._lasso = None   # 切工具清残留笔画态（防御）
        self.view.tool = tool
        edit = tool in ("brush", "lasso")
        self.sp_brush.setEnabled(tool == "brush")
        self.view.setCursor(QtCore.Qt.CursorShape.CrossCursor if (edit or tool == "count")
                            else QtCore.Qt.CursorShape.ArrowCursor)
        if tool == "count":            # 加/减核：需先开计数，切到叠加看标记点
            if not self._count_on:
                self.chk_count.setChecked(True)
            i = self.cb_disp.findData("overlay")
            if i >= 0 and self.cb_disp.currentIndex() != i:
                self.cb_disp.setCurrentIndex(i)
            self.status.setText("加/减核：左键补漏数的核、右键删多数的核（只改计数不改面积%）；Ctrl+Z 撤销。")
            return
        if edit:   # 编辑模式自动切到阳性叠加，看得见涂的效果
            i = self.cb_disp.findData("overlay")
            if i >= 0 and self.cb_disp.currentIndex() != i:
                self.cb_disp.setCurrentIndex(i)
            self.status.setText("%s：左键拖=加阳性，右键(或 Alt+左键)拖=减。纠正自动多选/漏选。"
                                % ("画笔" if tool == "brush" else "套索自由圈选"))
        else:
            self.status.setText("框选区域：拖矩形画子区域分别统计（每框一行）。")

    def _apply_region(self, region, additive):
        """把一个 bool region 加/减进 _pos_mask（加限制在组织内）。"""
        if additive:
            self._pos_mask[region & self._tissue] = True
        else:
            self._pos_mask[region] = False
        self._mask_edited = True
        self._ensure_overlay_shown()
        self.measure()

    def _ensure_overlay_shown(self):
        """涂改后保证显示在"阳性叠加"上（否则在原图/通道上看不到反馈）。"""
        if self._display != "overlay":
            i = self.cb_disp.findData("overlay")
            if i >= 0:
                self.cb_disp.setCurrentIndex(i)   # 触发 _refresh_display 显示叠加
        else:
            self._show_pixmap(self._overlay_now())

    def _on_lasso_stroke(self, pts, additive):
        if self._pos_mask is None or self._rgb is None or len(pts) < 3:
            return
        import cv2
        H, W = self._pos_mask.shape
        poly = np.array(pts, np.int32).reshape(-1, 1, 2)
        region = np.zeros((H, W), np.uint8)
        cv2.fillPoly(region, [poly], 1)
        self._apply_region(region.astype(bool), additive)

    def _on_mask_stroke(self, x, y, r, additive):
        if self._pos_mask is None:
            return
        H, W = self._pos_mask.shape
        x0 = max(0, x - r); x1 = min(W, x + r + 1); y0 = max(0, y - r); y1 = min(H, y + r + 1)
        if x0 >= x1 or y0 >= y1:
            return
        yy, xx = np.ogrid[y0:y1, x0:x1]
        circ = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
        sub = self._pos_mask[y0:y1, x0:x1]
        if additive:
            tsub = self._tissue[y0:y1, x0:x1]
            sub[circ & tsub] = True      # 加：限制在组织内（不把玻片算阳性）
        else:
            sub[circ] = False
        self._mask_edited = True
        if self._display != "overlay":
            self._ensure_overlay_shown()                 # 切到叠加（全图一次）
        elif self._proxy_rgb is not None:
            self._show_pixmap(self._overlay_now())       # 大图：代理整体叠加重绘(~15ms)，比 bbox 贴全分辨率快
        elif self._ov_outline:
            self._ov_timer.start(60)                     # 轮廓模式全图重绘很贵 → 合并到停顿后一次（防卡）
        else:
            self._refresh_overlay_bbox(x0, y0, x1, y1)   # 小图填充模式：只重绘笔刷 bbox
        self.measure()                     # 只更新面积/掩膜（快）；全图计数由 measure 内防抖 → 后台算

    def _refresh_overlay_bbox(self, x0, y0, x1, y1):
        """按真实 _pos_mask 重算 [x0:x1,y0:y1] 小块的填充叠加，贴回显示底图（O(笔刷面积)）。"""
        if self._rgb is None or self.view._pix_item is None:
            return
        sub_pix = _rgb_to_pixmap(_overlay_array(self._rgb[y0:y1, x0:x1], self._pos_mask[y0:y1, x0:x1],
                                                self._ov_color, self._ov_gray, False))
        pm = self.view._pix_item.pixmap()
        p = QtGui.QPainter(pm)
        p.drawPixmap(int(x0), int(y0), sub_pix)
        p.end()
        self.view._pix_item.setPixmap(pm)
        self.view.viewport().update()

    # ---------------- 撤销（选区编辑快照）----------------
    def _push_undo(self):
        if self._pos_mask is None:
            return
        self._undo.append((np.packbits(self._pos_mask), self._pos_mask.shape))
        if len(self._undo) > 24:        # 限栈深，packbits 后每张 ~0.7MB
            self._undo.pop(0)

    def undo(self):
        if self.view.tool == "count" and self._undo_cells:   # 加/减核工具下 Ctrl+Z 撤销核标记编辑
            self._manual_add, self._manual_del = self._undo_cells.pop()
            self._cells_edited = bool(self._manual_add or self._manual_del)
            self._patch_count()           # 基于缓存即时算（不重跑 count_cells）
            self.status.setText("已撤销核标记（剩 %d 步）。" % len(self._undo_cells)); return
        if not self._undo:
            self.status.setText("没有可撤销的选区编辑。"); return
        packed, shape = self._undo.pop()
        self._pos_mask = np.unpackbits(packed, count=shape[0] * shape[1]).reshape(shape).astype(bool)
        self._mask_edited = True
        if self._display == "overlay":
            self._show_pixmap(self._overlay_now())
        self.measure()
        self.status.setText("已撤销（剩 %d 步）。" % len(self._undo))

    def _reset_mask(self):
        self._push_undo()
        self._rebuild_and_measure()

    def _auto_positive(self):
        """明确触发自动识别阳性区（重跑 Otsu/阈值，丢弃手动修正）。之后可套索/画笔修正。"""
        if self._rgb is None:
            QtWidgets.QMessageBox.information(self, "无图", "先载入图片。"); return
        self._push_undo()
        self._rebuild_and_measure()
        a = self._results[0][1]["area"] if self._results else 0.0
        self.status.setText("已自动识别阳性区：%.2f%%（%s）。高亮色=阳性；用套索/画笔加(左键)/减(右键)修正。"
                            % (a, "Otsu 自动" if self._thr_mode == "otsu" and self._mode != "red" else "当前阈值"))

    def clear_rois(self):
        self.view.rois.clear(); self.view.roi_ids.clear(); self.view.markers.clear(); self.view.sel = -1
        self.view.viewport().update(); self.measure()

    def open_batch(self):
        dlg = IHCBatchDialog(self, init_stain=self._stain)
        dlg.exec()

    # ---------------- 测量（从 _pos_mask 取面积）----------------
    def _metrics_for_rect(self, rect):
        if self._pos_mask is None:
            return None
        pm = self._pos_mask; tm = self._tissue
        if rect is not None:
            x0, y0, x1, y1 = rect
            pm = pm[y0:y1, x0:x1]; tm = tm[y0:y1, x0:x1]
        denom = int(tm.sum())
        sel = pm & tm
        pos = int(sel.sum())
        area = (pos / denom * 100.0) if denom else 0.0
        cnt, cents, total, li = None, [], None, None
        if self._count_on and self._mode != "red":           # 红色胶原模式不计数
            if rect is None:
                cnt, cents, total, li = self._effective_count()   # 全图：后台算好的缓存 + 人工修正（即时，不冻结）
            elif pos:                                         # 子区小，同步算（便宜）
                min_a = max(4, int(round(self._cell_size * self._cell_size * 0.6)))
                ck = dict(min_area=min_a, max_area=min_a * 300, split=True,
                          peak_min_dist=self._cell_size, min_circularity=self._cell_circ)
                cs_idx = self._counterstain_idx()
                hema = self._img8[cs_idx][rect[1]:rect[3], rect[0]:rect[2]] \
                    if (cs_idx is not None and self._img8 is not None) else None
                cnt, cents, total = _compute_count(sel, tm, hema, ck, self._count_mode, self._li_on)
                li = (cnt / total * 100.0) if (total and self._li_on) else None
        base = {"measure": "红色胶原", "area": area, "mean_od": None, "iod": None, "h_score": None,
                "score": None, "tier": None, "label": "—", "pos_px": pos, "px": denom,
                "count": cnt, "centroids": cents, "total": total, "li": li}
        if self._mode == "red":
            base["thr"] = "灵敏度%d" % self._sat_min
            return base
        idx = self._target_idx
        thr_txt = ("[%d,%d]" % (self._manual_lo, self._manual_thr)) if self._thr_mode == "manual" else ("Otsu≤%d" % self._cur_thr)
        cc = self._conc[idx]
        sub_cc = cc if rect is None else cc[rect[1]:rect[3], rect[0]:rect[2]]
        mean_od = float(sub_cc[sel].mean()) if pos else None
        iod = float(sub_cc[sel].sum()) if pos else None          # 积分光密度 = 阳性区 OD 总和；零阳性=未定义(None)，与批量 metrics_from 口径一致
        out = {**base, "measure": self._names[idx], "thr": thr_txt, "mean_od": mean_od, "iod": iod}
        if self._names[idx] == "DAB":
            ch8 = self._img8[idx]; sub8 = ch8 if rect is None else ch8[rect[1]:rect[3], rect[0]:rect[2]]
            prof = iq.ihc_profiler(sub8, tm)
            out["score"] = prof["score"]; out["tier"] = prof["tier"]; out["label"] = prof["label"]
            out["h_score"] = iq.h_score(sub8, mask=tm)           # 经典 H-score 0-300
        return out

    def _on_count_toggled(self, on):
        self._count_on = bool(on)
        if not on:
            self._cell_centroids = []
            self._auto_count = {"cnt": None, "cents": [], "total": None}   # 关计数清缓存
            self._count_token += 1    # 让 in-flight 后台计数回调过期失效，否则关计数后仍被回填（幽灵计数）
        self.measure()                    # 重测以加/去「阳性细胞数」列；开则 measure 内防抖触发后台计数
        self._refresh_display()           # 加/去标记点

    def _effective_centroids(self, auto):
        """自动质心叠加人工修正：删=剔掉被「删核」命中(<核大小)的自动核；加=追加「加核」点（Cell Counter 式）。"""
        cs2 = max(2, self._cell_size) ** 2
        if not self._manual_del:
            out = list(auto)
        else:
            out = [(cx, cy) for (cx, cy) in auto
                   if not any((cx - dx) ** 2 + (cy - dy) ** 2 < cs2 for (dx, dy) in self._manual_del)]
        out.extend(self._manual_add)
        return out

    def _on_cell_click(self, x, y, additive):
        if not self._count_on:
            self.status.setText("请先勾「计数阳性核」，再用「加/减核」手动修正。"); return
        cs2 = max(2, self._cell_size) ** 2
        snap = (list(self._manual_add), list(self._manual_del))   # 改动前快照（仅在真改动时入栈）
        if additive:                      # 加核：离已有任一质心太近则忽略（防重复点）
            if any((x - cx) ** 2 + (y - cy) ** 2 < cs2 for (cx, cy) in self._cell_centroids):
                self.status.setText("该处已有核，未重复添加。"); return
            self._manual_add.append((float(x), float(y)))
        else:                             # 删核：命中人工加的→直接移除；否则记入 _manual_del 剔自动核
            hit = next((i for i, (ax, ay) in enumerate(self._manual_add)
                        if (x - ax) ** 2 + (y - ay) ** 2 < cs2), None)
            if hit is not None:
                self._manual_add.pop(hit)
            elif any((x - cx) ** 2 + (y - cy) ** 2 < cs2 for (cx, cy) in self._cell_centroids):
                self._manual_del.append((float(x), float(y)))
            else:
                self.status.setText("附近没有可删除的核。"); return
        self._undo_cells.append(snap)     # 确有改动才消耗撤销槽
        if len(self._undo_cells) > 60:
            self._undo_cells.pop(0)
        self._cells_edited = True
        self._patch_count()               # 人工修正基于缓存质心即时算（不重跑 count_cells）

    def _mark_count_pending(self):
        """计数参数改变后【即时】把计数相关列(阳性细胞数/总核数/阳性率%)置「…」占位 → 点了就有反应；
        worker 回来 _fill_table 会用真值覆盖。只刷显示、不触发重算（防抖+后台 worker 性能定式不变）。"""
        if self.table.rowCount() == 0:
            return
        for _name in ("阳性细胞数", "总核数", "阳性率%"):
            if _name in self.COLS:
                it = self.table.item(0, self.COLS.index(_name))   # 全图恒为 row 0
                if it is not None and it.text() not in ("", "—"):
                    it.setText("…")

    def _on_cell_size(self, v):
        self._cell_size = int(v)
        if self._count_on:
            self._mark_count_pending()     # 即时占位反馈（拖参数当下计数列就变「…」，不等 250ms+worker）
            self._count_timer.start(250)   # 核大小只影响计数粒度→防抖，停手才重算一次（不每 tick 冻结）
            if self._manual_del:    # 减核点存图像坐标，核大小变→匹配半径变，可能不再精确命中
                self.status.setText("核大小已变，原手动减核点按新检测重新匹配，请复核标记点。")

    def _on_cell_circ(self, v):
        self._cell_circ = float(v)
        if self._count_on:
            self._mark_count_pending()     # 即时占位反馈
            self._count_timer.start(250)   # 圆度只影响计数→防抖，停手才重算一次

    def _on_count_mode(self, *_):
        self._count_mode = self.cb_count_mode.currentData() or "blob"
        if self._count_on:
            self.measure(); self._refresh_display()
            self._mark_count_pending()     # measure 内 _fill_table 刚填回旧口径值 → 覆盖成「…」占位等 worker 出新口径
            if self._manual_del:    # 切口径→自动核质心整体变，旧减核点可能错配，提示复核（fail-loud）
                self.status.setText("已切计数口径，原手动减核点按新检测重新匹配，请复核标记点。")

    def _auto_cell_size(self):
        if self._pos_mask is None or self._mode == "red":
            self.status.setText("自动核大小：需先有阳性区（DAB/灰度模式）。"); return
        r = iq.estimate_nucleus_size(self._pos_mask & self._tissue)
        self.sp_cell.setValue(max(2, min(60, r)))   # 触发 _on_cell_size→measure
        self.status.setText("自动核大小 = %d px（按当前阳性区估计，可微调）" % self.sp_cell.value())

    def _on_li_toggled(self, on):
        self._li_on = bool(on)
        if on and not self._count_on:        # LI 依赖计数 → 自动连带打开计数
            self.chk_count.setChecked(True)  # 触发 _on_count_toggled→measure
            return
        if self._count_on:
            self.measure()

    def _counterstain_idx(self):
        """复染(核)通道下标：优先名字含 Hematoxylin/Haematoxylin；否则取非目标、非 Residual 的首个通道。无则 None。"""
        if self._mode != "deconv" or self._names is None:
            return None
        for i, n in enumerate(self._names):
            if "ematoxylin" in n:
                return i
        for i, n in enumerate(self._names):
            if i != self._target_idx and "Residual" not in n and "residual" not in n:
                return i
        return None

    def measure(self):
        self._results = []
        if self._rgb is None:
            self.table.setRowCount(0); return
        if self._pos_mask is None:
            self._build_pos_mask()
        if self._pos_mask is None:        # 解卷积尚未完成（async）→ 暂无掩膜，等 done 回调再测
            self.table.setRowCount(0); self._sync_empty_results(); return
        rows = [("全图", None)]
        for i, rect in enumerate(self.view.rois):
            if i in self.view.markers:
                continue
            rows.append((str(i + 1), rect))
        for label, rect in rows:
            m = self._metrics_for_rect(rect)
            if m is not None:
                self._results.append((label, m))
        # 全图阳性细胞质心 → 叠加打点（仅全图，rect=None 时为全图坐标，来自计数缓存）
        self._cell_centroids = self._results[0][1].get("centroids", []) if self._results else []
        self._fill_table(); self._update_summary()
        if self._count_on and self._mode != "red":
            self._count_timer.start(150)   # 后台重算全图计数（防抖；调参/拖阈值/画刷停手后才算，不冻 UI）

    def _fill_table(self):
        self.table.setRowCount(len(self._results))
        for r, (label, m) in enumerate(self._results):
            vals = [label, m["measure"], "%.2f" % m["area"],
                    "—" if m.get("count") is None else str(m["count"]),
                    "—" if m.get("total") is None else str(m["total"]),
                    "—" if m.get("li") is None else "%.1f" % m["li"], str(m["thr"]),
                    "—" if m["mean_od"] is None else "%.1f" % m["mean_od"],
                    "—" if m.get("iod") is None else "%.4g" % m["iod"],
                    "—" if m.get("h_score") is None else "%.0f" % m["h_score"],
                    "—" if m["score"] is None else "%.3f" % m["score"], m["label"],
                    str(m.get("pos_px", 0)), str(m["px"])]
            grade_col = self.COLS.index("分级")
            for c, v in enumerate(vals):
                it = _table_item(v, numeric=(c not in (0, 1, grade_col)), key=(c == 2))
                if c == 0:
                    it.setData(QtCore.Qt.ItemDataRole.UserRole, label)
                if c == grade_col and m["tier"]:
                    it.setForeground(QtGui.QColor(_TIER_COLOR.get(m["tier"], "#6b7280")))
                self.table.setItem(r, c, it)
        self._sync_empty_results()

    def _sync_empty_results(self):
        if hasattr(self, "empty_results"):
            self.empty_results.setVisible(self.table.rowCount() == 0)

    def _update_summary(self):
        if not self._results:
            self.lb_summary.setText("无结果。"); return
        _, m = self._results[0]
        edited = "（含手动修正）" if self._mask_edited else ""
        if self._mode == "red":
            self.lb_summary.setText(f"<b>全图</b>　红色胶原面积 <b>{m['area']:.2f}%</b>{edited}（天狼星红，占组织）")
        elif m["score"] is not None and m["tier"] is None:
            self.lb_summary.setText(f"<b>全图</b>　无可测组织（近白背景已排除）　染色 {self._stain}")
        else:
            cnt_txt = ("　阳性细胞 <b>%d</b> 个" % m["count"]) if m.get("count") is not None else ""
            if m.get("li") is not None:
                cnt_txt += "　阳性率(LI) <b>%.1f%%</b>（%d/%d 核）" % (m["li"], m["count"], m["total"])
            if self._cells_edited:
                cnt_txt += "（人工+%d/-%d" % (len(self._manual_add), len(self._manual_del))
                if self._manual_del and self._li_on:
                    cnt_txt += "；删核同步移出 LI 分母"     # 删核=否定此为核，分子分母同 -1
                cnt_txt += "；仅作用于全图，子区为原始检测）" if self.view.rois else "）"
            if m["score"] is not None:
                col = _TIER_COLOR.get(m["tier"], "#6b7280")
                hs = ("　H-score %.0f" % m["h_score"]) if m.get("h_score") is not None else ""
                self.lb_summary.setText(
                    f"<b>全图</b>：<span style='color:{col}'><b>{m['label']}</b></span>"
                    f"　IHC 分 {m['score']:.2f}/4{hs}　DAB 阳性 {m['area']:.1f}%（{m['thr']}）{cnt_txt}{edited}")
            else:
                self.lb_summary.setText(
                    f"<b>全图</b>　{m['measure']} 阳性面积 <b>{m['area']:.2f}%</b>{cnt_txt}{edited}（{m['thr']}）　染色 {self._stain}")

    def _on_table_sel(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        r = rows[0].row()
        if r < len(self._results):
            label = self._results[r][0]
            if label.isdigit():
                idx = int(label) - 1
                if 0 <= idx < len(self.view.rois):
                    self.view.sel = idx; self.view.viewport().update()

    # ---------------- 导出 ----------------
    def export_csv(self):
        if not self._results:
            QtWidgets.QMessageBox.information(self, "无数据", "先载入图片并测量。"); return
        start = (_last_dir("ihc") + "/ihc_results.csv") if _last_dir("ihc") else "ihc_results.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出定量结果 CSV", start, "CSV (*.csv)")
        if not path:
            return
        _remember_dir("ihc", path)
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["区域", "染色", "测量通道", "阳性面积%", "阳性细胞数", "人工加核", "人工删核", "总核数", "阳性率%LI", "阈值", "平均OD", "IOD", "H-score(0-300)",
                            "IHC分(0-4)", "分级", "阳性像素", "组织像素", "手动修正", "来源"])
                src = self._loaded_path or "(当前图层)"
                edited = "是" if self._mask_edited else "否"
                for label, m in self._results:
                    w.writerow([label, self._stain, m["measure"], "%.4f" % m["area"],
                                "" if m.get("count") is None else m["count"],
                                len(self._manual_add) if (label == "全图" and self._cells_edited) else "",
                                len(self._manual_del) if (label == "全图" and self._cells_edited) else "",
                                "" if m.get("total") is None else m["total"],
                                "" if m.get("li") is None else "%.2f" % m["li"], m["thr"],
                                "" if m["mean_od"] is None else "%.4f" % m["mean_od"],
                                "" if m.get("iod") is None else "%.4f" % m["iod"],
                                "" if m.get("h_score") is None else "%.2f" % m["h_score"],
                                "" if m["score"] is None else "%.4f" % m["score"], m["label"],
                                m.get("pos_px", 0), m["px"], edited, src])
            self.status.setText("已导出 %d 行 → %s" % (len(self._results), path))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))

    # ---------------- 生命周期 ----------------
    def stop_threads(self):
        """停后台解卷积线程（quit+限时wait+兜底terminate）。本面板装在 FloatingToolWindow 里、closeEvent 常规不触发，
        由 EditorWindow.closeEvent 退出时显式调用（与图像描摹面板同坑，铁律③：QThread 销毁仍运行会硬崩）。"""
        self._closing = True                          # 先封死：防 pending 防抖回调在 teardown 重建线程
        for tm in ("_count_timer", "_ov_timer", "_thr_timer"):
            t = getattr(self, tm, None)
            if t is not None:
                t.stop()
        for attr in ("_deconv_thread", "_count_thread"):
            th = getattr(self, attr, None)
            if th is not None:
                th.quit()
                if not th.wait(3000):
                    th.terminate(); th.wait(1000)
                setattr(self, attr, None)
        self._deconv_worker = None; self._count_worker = None

    def closeEvent(self, e):
        self.stop_threads()
        super().closeEvent(e)


# ============================ 批量定量（多图审核版） ============================
class _BatchWorker(QtCore.QObject):
    """后台线程跑批量，逐张发进度，完成发结果（避免 168 张大图卡 UI）。"""
    progress = QtCore.Signal(int, int)
    done = QtCore.Signal(list)

    def __init__(self, paths, settings):
        super().__init__(); self._paths = paths; self._s = settings; self._cancel = False

    def cancel(self):
        self._cancel = True

    @QtCore.Slot()
    def run(self):
        res = ib.batch_analyze(self._paths, self._s, progress=lambda i, n: self.progress.emit(i, n),
                               should_cancel=lambda: self._cancel)
        self.done.emit(res)


class _BatchCountWorker(QtCore.QObject):
    """后台跑【逐图复核】的计数+LI（count_and_li=count_cells/classify_nuclei，秒级），主线程不冻结。"""
    done = QtCore.Signal(object)   # dict: idx/token/ok/count/total/li/error

    def __init__(self):
        super().__init__()
        import collections
        self._jobs = collections.deque()   # 队列(非单槽)：不同 idx 的 job 不互相覆盖丢弃→每图都能回填

    def set_job(self, idx, sel, tissue, cs_ch, s, token):
        self._jobs.append((idx, sel, tissue, cs_ch, s, token))

    @QtCore.Slot()
    def run(self):
        while True:
            try:
                idx, sel, tissue, cs_ch, s, token = self._jobs.popleft()   # deque popleft 单操作 GIL 安全
            except IndexError:
                return
            res = {"idx": idx, "token": token}
            try:
                res.update(ib.count_and_li(sel, tissue, cs_ch, s), ok=True)
            except Exception as e:  # noqa: BLE001
                res["ok"] = False; res["error"] = str(e)
            self.done.emit(res)


class _BatchPreviewWorker(QtCore.QObject):
    """后台跑【逐图预览】的解码+解卷积（load_rgb + _compute，大 TIF 秒级），选图不冻 UI。"""
    done = QtCore.Signal(object)   # dict: token/path/ok/rgb/pos/tissue/target/thr_label/cc/ch8/is_dab/cs_ch/error

    def __init__(self):
        super().__init__(); self._job = None

    def set_job(self, path, s, token):
        self._job = (path, s, token)

    @QtCore.Slot()
    def run(self):
        job, self._job = self._job, None
        if job is None:
            return
        path, s, token = job
        res = {"path": path, "token": token}
        try:
            rgb = ib.load_rgb(path)
            pos, tissue, target, thr_label, cc, ch8, is_dab, cs_ch = ib._compute(rgb, s)
            res.update(rgb=rgb, pos=pos, tissue=tissue, target=target, thr_label=thr_label,
                       cc=cc, ch8=ch8, is_dab=is_dab, cs_ch=cs_ch, ok=True)
        except Exception as e:  # noqa: BLE001
            res["ok"] = False; res["error"] = str(e)
        self.done.emit(res)


class IHCBatchDialog(QtWidgets.QDialog):
    """多图批量阳性面积%：统一设置一键跑 → 左列表 + 叠加预览审核 + 汇总表 + 组聚合(模型 vs 对照) + CSV。

    仅限 dlg.exec() 单次模态使用、用完即弃（见 open_batch）。不可改成缓存 + show() 复用——
    _stop_batch_threads 置 _cnt_closing=True 后不复位，复用会使 _ensure_pv/cnt_thread 永久早退、
    逐图预览/计数静默失效；若确需复用须每次打开复位 _cnt_closing=False 并重建线程。"""

    COLS = ["#", "文件", "组", "测量", "阳性面积%", "阳性细胞数", "总核数", "阳性率%", "阈值", "平均OD", "IOD", "H-score", "IHC分", "分级", "阳性px", "组织px"]
    _cnt_trigger = QtCore.Signal()   # 跨线程触发逐图计数 worker.run
    _pv_trigger = QtCore.Signal()    # 跨线程触发逐图预览(解卷积) worker.run

    def __init__(self, parent=None, init_stain="H DAB"):
        super().__init__(parent)
        self.setObjectName("ihcBatchDialog")
        self.setWindowTitle("IHC / 组织化学 批量定量")
        self.resize(1180, 720)
        self._paths = []
        self._results = []
        self._overrides = {}        # path -> {thr_mode, manual_lo, manual_hi, sat_min}
        self._run_settings = None   # 运行批量时的设置快照（预览/覆盖用它，防跑完改染色混结果）
        self._thread = None
        self._worker = None
        self._cur_idx = -1
        self._cur = None            # 当前预览图编辑态 {path,rgb,mask,tissue,target,cc,ch8,is_dab}
        self._edited = {}           # path -> packbits(手动编辑过的掩膜)，重选回显、重跑前清
        self._undo1 = []         # 当前图撤销栈 (packbits)
        self._build_ui(init_stain)
        self._prev_timer = QtCore.QTimer(self); self._prev_timer.setSingleShot(True)
        self._prev_timer.timeout.connect(self._preview_current_threshold)   # 拖全局阈值→实时刷当前图
        self._one_timer = QtCore.QTimer(self); self._one_timer.setSingleShot(True)
        self._one_timer.timeout.connect(self._apply_one_thr)                # 此图阈值滑块防抖
        # 逐图复核计数后台化（勾计数后拖阈值/画笔不冻 UI）
        self._cnt_thread = None; self._cnt_worker = None; self._cnt_token = 0
        self._cnt_tokens = {}       # idx -> 最新 token（防同图旧结果覆盖，允许跨图各自回填）
        self._cnt_pending = None    # (idx, sel, tissue, cs_ch, s)
        self._cnt_closing = False
        self._cnt_timer = QtCore.QTimer(self); self._cnt_timer.setSingleShot(True)
        self._cnt_timer.timeout.connect(self._submit_cnt)
        self._pv_thread = None; self._pv_worker = None; self._pv_token = 0   # 逐图预览解卷积后台
        self._pv_raw = False        # 当前预览是「未算图只看原图」(True) 还是「已算图可编辑叠加」(False)

    def _stop_batch_threads(self):
        """关窗/拒绝前停所有后台线程 + 防抖计时器（QThread 销毁仍运行会硬崩）。"""
        self._cnt_closing = True
        for tm in ("_cnt_timer", "_one_timer", "_prev_timer"):
            t = getattr(self, tm, None)
            if t is not None:
                t.stop()
        if self._worker is not None:
            self._worker.cancel()                 # 协作取消：让 batch_analyze 在当前图边界自然退出（不 terminate 硬杀）
        for attr in ("_thread", "_cnt_thread", "_pv_thread"):
            th = getattr(self, attr, None)
            if th is not None:
                th.quit()
                if not th.wait(5000):             # RUN 线程值得多等(单图算完即退)，降低 terminate 概率
                    th.terminate(); th.wait(1000)
                setattr(self, attr, None)
        self._worker = None; self._cnt_worker = None; self._pv_worker = None

    def closeEvent(self, e):
        self._stop_batch_threads()
        super().closeEvent(e)

    def reject(self):
        self._stop_batch_threads()
        super().reject()

    # ---- UI ----
    def _build_ui(self, init_stain):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(6)
        tc = theme.colors()

        def _push(text, icon=None, primary=False):
            b = QtWidgets.QPushButton(text); b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor); b.setMinimumHeight(27)
            if primary: b.setProperty("primary", True)
            if icon: b.setIcon(icons.tool_icon(icon, tc["text"], 16)); b.setIconSize(QtCore.QSize(16, 16))
            return b

        def _lab(t):
            x = QtWidgets.QLabel(t); x.setObjectName("ihcFieldLabel"); return x

        # 顶栏：选图 + 设置 + 运行
        head = QtWidgets.QFrame(); head.setObjectName("ihcTopBar")
        top = QtWidgets.QHBoxLayout(head); top.setContentsMargins(8, 6, 8, 6); top.setSpacing(6)
        title = QtWidgets.QLabel("IHC / 组织化学批量")
        title.setObjectName("ihcPanelTitle")
        top.addWidget(title)
        b_files = _push("选图片…", "folder", True); b_files.clicked.connect(self._pick_files)
        b_dir = _push("选文件夹…", "folder"); b_dir.clicked.connect(self._pick_dir)
        top.addWidget(b_files); top.addWidget(b_dir)
        top.addWidget(_lab("染色"))
        self.cb_stain = QtWidgets.QComboBox(); self.cb_stain.addItems(list(iq.STAIN_VECTORS.keys()) + [SIRIUS_RED_KEY])
        self.cb_stain.setCurrentText(init_stain); self.cb_stain.currentTextChanged.connect(self._on_stain)
        top.addWidget(self.cb_stain)
        top.addWidget(_lab("测量通道"))
        self.cb_target = QtWidgets.QComboBox(); self.cb_target.setMinimumWidth(92)
        top.addWidget(self.cb_target)
        top.addWidget(_lab("阈值"))
        self.cb_thr = QtWidgets.QComboBox(); self.cb_thr.addItems(["Otsu 自动", "手动"])
        self.cb_thr.currentIndexChanged.connect(self._sync_thr)
        top.addWidget(self.cb_thr)
        self.rg_global = HistRangeSlider(0, 255); self.rg_global.setMinimumWidth(230); self.rg_global.set_values(0, 180)
        self.rg_global.setToolTip("ImageJ 式双头直方图阈值（应用到所有图，与单图一致）。直方图=当前预览图的目标通道分布；拖手柄即转手动。")
        self.rg_global.rangeChanged.connect(self._on_global_range)
        self.sp_sat = QtWidgets.QSpinBox(); self.sp_sat.setRange(0, 100); self.sp_sat.setValue(50); self.sp_sat.setPrefix("灵敏度"); self.sp_sat.setFixedWidth(86)
        top.addWidget(self.rg_global); top.addWidget(self.sp_sat)
        self.chk_bg = QtWidgets.QCheckBox("排除近白背景"); self.chk_bg.setChecked(True); top.addWidget(self.chk_bg)
        # 阳性细胞计数（CD31/Ki67），与单图同口径；勾选后批量结果加「阳性细胞数」列 + CSV
        self.chk_count = QtWidgets.QCheckBox("计数阳性核")
        self.chk_count.setToolTip("批量也数阳性细胞核个数（与单图同口径：分水岭分粘连核+面积过滤）；勾选后结果表/CSV 加「阳性细胞数」列。")
        top.addWidget(self.chk_count)
        self.sp_cell = QtWidgets.QSpinBox(); self.sp_cell.setRange(2, 60); self.sp_cell.setValue(8)
        self.sp_cell.setPrefix("核 "); self.sp_cell.setSuffix(" px"); self.sp_cell.setToolTip("核大小≈最小核半径（与单图一致）")
        top.addWidget(self.sp_cell)
        self.sp_circ = QtWidgets.QDoubleSpinBox(); self.sp_circ.setRange(0.0, 1.0); self.sp_circ.setSingleStep(0.05)
        self.sp_circ.setDecimals(2); self.sp_circ.setValue(0.0); self.sp_circ.setPrefix("圆度≥ ")
        self.sp_circ.setToolTip("圆度过滤(4π×面积/周长²)剔细长非核伪影；0=不过滤（与单图一致）")
        top.addWidget(self.sp_circ)
        self.chk_li = QtWidgets.QCheckBox("阳性率%")
        self.chk_li.setToolTip("算 LI=阳性核/总核(Ki67/ER/PR)：对苏木素复染数总核，结果加「总核数」「阳性率%」列（与单图一致）")
        top.addWidget(self.chk_li)
        self.cb_count_mode = QtWidgets.QComboBox()
        self.cb_count_mode.addItem("团块", "blob"); self.cb_count_mode.addItem("逐核", "nuclei")
        self.cb_count_mode.setToolTip("计数口径：团块=数 DAB+ 色块；逐核=苏木素数所有核逐核判正负(金标准，与单图一致)")
        top.addWidget(self.cb_count_mode)
        top.addStretch(1)
        self.b_run = _push("运行批量", "wand", True); self.b_run.clicked.connect(self._run); top.addWidget(self.b_run)
        self.b_csv = _push("导出 CSV", "download"); self.b_csv.clicked.connect(self._export); top.addWidget(self.b_csv)
        root.addWidget(head)

        self.bar = QtWidgets.QProgressBar(); self.bar.setObjectName("ihcProgress"); self.bar.setVisible(False); root.addWidget(self.bar)

        # 主体：左列表 | 中预览 | 右汇总
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); split.setObjectName("ihcSplit")
        split.setChildrenCollapsible(False)
        self.lst = QtWidgets.QListWidget(); self.lst.setObjectName("ihcBatchList"); self.lst.setMinimumWidth(230)
        self.lst.currentRowChanged.connect(self._on_select)
        self.lst.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst.customContextMenuRequested.connect(self._show_list_menu)
        split.addWidget(self.lst)

        mid = QtWidgets.QFrame(); mid.setObjectName("ihcPreviewPanel")
        mv = QtWidgets.QVBoxLayout(mid); mv.setContentsMargins(8, 8, 8, 8); mv.setSpacing(6)
        mhead = QtWidgets.QHBoxLayout()
        mtitle = QtWidgets.QLabel("预览与修正"); mtitle.setObjectName("ihcSectionTitle")
        mhead.addWidget(mtitle); mhead.addStretch(1)
        mtag = QtWidgets.QLabel("逐张审核"); mtag.setObjectName("ihcTag")
        mhead.addWidget(mtag)
        mv.addLayout(mhead)
        # 工具行：画笔/套索逐张细调（和单图一样）
        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(_lab("工具"))
        self.cb_tool1 = QtWidgets.QComboBox()
        self.cb_tool1.addItem("浏览", "browse"); self.cb_tool1.addItem("画笔", "brush"); self.cb_tool1.addItem("套索", "lasso")
        self.cb_tool1.setToolTip("画笔/套索手动改这张图的阳性选区：左键加 / 右键(或Alt)减。逐张细调，和单图一致。")
        self.cb_tool1.currentIndexChanged.connect(self._on_tool1)
        trow.addWidget(self.cb_tool1)
        self.sp_brush1 = QtWidgets.QSpinBox(); self.sp_brush1.setRange(2, 200); self.sp_brush1.setValue(14)
        self.sp_brush1.setPrefix("笔 "); self.sp_brush1.setSuffix(" px")
        self.sp_brush1.valueChanged.connect(lambda v: setattr(self.view, "brush_radius", int(v)))
        trow.addWidget(self.sp_brush1)
        self.btn_undo1 = _push("撤销", "undo"); self.btn_undo1.setToolTip("撤销这张图上一步画笔/套索（Ctrl+Z）"); self.btn_undo1.clicked.connect(self._undo_one)
        trow.addWidget(self.btn_undo1); trow.addStretch(1)
        mv.addLayout(trow)
        self.view = MaskEditView(); self.view.setObjectName("ihcImageView"); self.view.setMinimumSize(360, 300)
        self.view.tool = "browse"
        self.view.maskStroke.connect(self._on_one_stroke)
        self.view.lassoStroke.connect(self._on_one_lasso)
        self.view.editBegan.connect(self._push_one_undo)
        self.view.peekOriginal.connect(self._peek_one)        # 按住 C 看原图（与单图一致）
        self.view.toggleView.connect(self._toggle_one_view)   # Tab 原图↔叠加（与单图一致）
        QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Undo, self.view, activated=self._undo_one)
        mv.addWidget(self.view, 1)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(_lab("此图阈值"))
        self.sl_one = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.sl_one.setRange(0, 255); self.sl_one.setValue(180)
        self.sl_one.setToolTip("单独覆盖此图的上限/灵敏度（纠正个别图），仅影响这张。")
        self.sl_one.valueChanged.connect(self._on_one_thr)
        prow.addWidget(self.sl_one)
        self.lb_one = _lab(""); prow.addWidget(self.lb_one)
        self.btn_reset_one = _push("还原此图", "trash"); self.btn_reset_one.setToolTip("丢弃此图的阈值覆盖+手动修正，回默认"); self.btn_reset_one.clicked.connect(self._reset_one); prow.addWidget(self.btn_reset_one)
        mv.addLayout(prow)
        split.addWidget(mid)

        right = QtWidgets.QFrame(); right.setObjectName("ihcResultsPanel")
        rv = QtWidgets.QVBoxLayout(right); rv.setContentsMargins(8, 8, 8, 8); rv.setSpacing(6)
        rhead = QtWidgets.QHBoxLayout()
        rtitle = QtWidgets.QLabel("批量结果"); rtitle.setObjectName("ihcSectionTitle")
        rhead.addWidget(rtitle); rhead.addStretch(1)
        rv.addLayout(rhead)
        self.lb_group = QtWidgets.QLabel("组聚合：运行后显示模型 vs 对照"); self.lb_group.setObjectName("ihcBannerInfo"); self.lb_group.setWordWrap(True)
        rv.addWidget(self.lb_group)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS)); self.table.setObjectName("ihcTable")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)  # 列多→按内容+横向滚
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        _setup_result_table(self.table, self._export)
        self.table.currentCellChanged.connect(lambda r, *_: self.lst.setCurrentRow(r) if 0 <= r < self.lst.count() else None)
        rv.addWidget(self.table, 1)
        split.addWidget(right)
        split.setSizes([240, 520, 420])
        root.addWidget(split, 1)

        self.status = QtWidgets.QLabel("选图片或文件夹 → 设染色/阈值 → 运行批量。"); self.status.setObjectName("ihcStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self._on_stain(init_stain)

    # ---- 设置 ----
    def _on_stain(self, name):
        red = (name == SIRIUS_RED_KEY)
        self.cb_target.clear()
        if red:
            self.cb_target.addItem("红色胶原"); self.cb_target.setEnabled(False)
        else:
            self.cb_target.setEnabled(True)
            names = iq.STAIN_CHANNEL_NAMES.get(name, ["Stain1", "Stain2", "Residual"])
            self.cb_target.addItems(names)
            dflt = names.index("DAB") if "DAB" in names else (names.index("Aniline blue") if "Aniline blue" in names else 0)
            self.cb_target.setCurrentIndex(dflt)
        self._sync_thr()

    def _sync_thr(self, *_):
        red = (self.cb_stain.currentText() == SIRIUS_RED_KEY)
        self.cb_thr.setEnabled(not red)
        self.rg_global.setVisible(not red)      # deconv=双头直方图滑块
        self.sp_sat.setVisible(red); self.chk_bg.setEnabled(not red)

    def _on_global_range(self, lo, hi):
        # 拖全局双头滑块：选中已算图时 → 直接当作【此图阈值覆盖】实时更新该图结果行（复用 _apply_one_thr 落地链）；
        # 未选/未算/天狼星红 → 退回纯预览（不写脏数据）。「运行批量」仍把当前全局阈值套用到所有图。
        if self.cb_thr.currentIndex() != 1:
            self.cb_thr.blockSignals(True); self.cb_thr.setCurrentIndex(1); self.cb_thr.blockSignals(False)
            self._sync_thr()
        if (self._cur is not None and 0 <= self._cur_idx < len(self._results)
                and self._base_settings().get("mode") != "red"):
            path = self._cur["path"]
            ov = dict(self._overrides.get(path, {})); ov["thr_mode"] = "manual"
            ov["manual_lo"] = int(lo); ov["manual_hi"] = int(hi)
            self._overrides[path] = ov
            self._edited.pop(path, None)        # 改阈值 → 丢该图笔画（与 _on_one_thr 一致）
            self.sl_one.blockSignals(True); self.sl_one.setValue(int(hi)); self.sl_one.blockSignals(False)  # 同步底部，防数值打架
            self.lb_one.setText("此图 [%d,%d]" % (lo, hi))
            self._one_timer.start(40)           # 复用落地链：结果行 8 列即时更新（停手后台补计数）
            self.status.setText("此图阈值 [%d,%d]：实时更新该图结果行。「运行批量」把当前全局阈值套用到所有图。" % (lo, hi))
        else:
            self._prev_timer.start(40)          # 未选/未算/天狼星红 → 纯预览
            self.status.setText("拖动看【当前图】阈值效果 [%d,%d]；满意后「运行批量」应用到所有图。" % (lo, hi))

    def _preview_current_threshold(self):
        """拖全局双头滑块 → 仅【纯视觉】预览当前图的阈值叠加(不写结果行/不改 _cur 掩膜/不动 override，避免脏数据)；
        数字/落地等点「运行批量」应用到所有图。"""
        c = self._cur
        if c is None or c.get("ch8") is None or self.view._pix_item is None:
            return
        ch8, tissue = c["ch8"], c["tissue"]
        lo, hi = self.rg_global.values()
        if self.cb_thr.currentIndex() != 1:          # Otsu
            hi = iq.otsu_threshold(ch8[tissue]); lo = 0
        preview = (ch8 >= lo) & (ch8 <= hi) & tissue
        pw, ph = c["pw"], c["ph"]                     # 预览叠加走显示代理（大图不卡）
        pm = preview
        if (pw, ph) != (preview.shape[1], preview.shape[0]):
            import cv2
            pm = cv2.resize(preview.astype(np.uint8), (pw, ph), interpolation=cv2.INTER_NEAREST).astype(bool)
        self._cur_show(overlay_pixmap(c["prgb"], pm))
        denom = int(tissue.sum()); area = (int(preview.sum()) / denom * 100.0) if denom else 0.0  # 面积%用全分辨率
        self.lb_one.setText("全局阈值预览 %.2f%%（运行批量应用到所有图）" % area)

    def _settings(self):
        stain = self.cb_stain.currentText()
        red = (stain == SIRIUS_RED_KEY)
        lo, hi = self.rg_global.values()
        return {"mode": "red" if red else "deconv", "stain": stain,
                "target": None if red else self.cb_target.currentText(),
                "thr_mode": "manual" if self.cb_thr.currentIndex() == 1 else "otsu",
                "manual_lo": lo, "manual_hi": hi,
                "sat_min": self.sp_sat.value(), "exclude_bg": self.chk_bg.isChecked(),
                "count_on": self.chk_count.isChecked(), "cell_size": self.sp_cell.value(),
                "cell_circ": self.sp_circ.value(), "li_on": self.chk_li.isChecked(),
                "count_mode": self.cb_count_mode.currentData() or "blob",
                "stain_label": stain}

    def _base_settings(self):
        """已运行过则用运行时快照（预览/覆盖/导出据此，防跑完改染色混结果），否则用当前控件。"""
        return dict(self._run_settings) if self._run_settings else self._settings()

    # ---- 选图 ----
    def _pick_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择图片（多选）", _last_dir("ihc"), "图片 (*.tif *.tiff *.png *.jpg *.jpeg *.bmp)")
        if paths:
            _remember_dir("ihc", paths[0]); self._set_paths(paths)

    def _pick_dir(self):
        import glob
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择图片文件夹", _last_dir("ihc"))
        if d:
            _remember_dir("ihc", d + "/x")
            paths = []
            for ext in ("*.tif", "*.tiff", "*.png", "*.jpg", "*.jpeg", "*.bmp"):
                paths.extend(glob.glob(d + "/" + ext))
            self._set_paths(sorted(paths))

    def _show_list_menu(self, pos):
        row = self.lst.indexAt(pos).row()
        if row < 0:
            return
        self.lst.setCurrentRow(row)
        menu = QtWidgets.QMenu(self.lst)
        c = theme.colors()
        act_copy = menu.addAction(icons.tool_icon("copy", c["text"], 18), "复制文件名")
        act_reset = menu.addAction("还原此图")
        act_reset.setEnabled(0 <= row < len(self._results))
        menu.addSeparator()
        act_remove = menu.addAction(icons.tool_icon("trash", c["danger"], 18), "从批量移除")
        chosen = menu.exec(self.lst.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            name = QtCore.QFileInfo(str(self._paths[row])).fileName() if row < len(self._paths) else self.lst.item(row).text()
            QtWidgets.QApplication.clipboard().setText(name)
        elif chosen is act_reset:
            self._reset_one()
        elif chosen is act_remove:
            self._remove_current(row)

    def _remove_current(self, row=None):
        row = self.lst.currentRow() if row is None else row
        if row < 0:
            return
        path = self._paths[row] if row < len(self._paths) else None
        if path is not None:
            self._overrides.pop(path, None)
            self._edited.pop(path, None)
        if row < len(self._paths):
            self._paths.pop(row)
        if row < len(self._results):
            self._results.pop(row)
        self._cur = None; self._cur_idx = -1
        self._fill_list()
        self._fill_table()
        self._update_group()
        if self.lst.count():
            self.lst.setCurrentRow(min(row, self.lst.count() - 1))
        else:
            self.view.clear()
            self.lb_one.setText("")

    def _invalidate_cnt(self):
        """作废所有逐图计数在途态：停防抖、清 pending、清 per-idx token（旧 done 比对必失败→丢弃）。
        重跑/换图集时调，防旧阈值计数静默覆盖新结果行 / 跨批 idx 复用污染。"""
        self._cnt_timer.stop(); self._cnt_pending = None; self._cnt_tokens.clear()
        self._pv_token += 1                       # 顺带作废在途预览回调（换集时旧图预览不回填）

    def _set_paths(self, paths):
        self._invalidate_cnt()
        self._paths = list(paths); self._overrides.clear(); self._edited.clear(); self._results = []; self._cur = None
        self.table.setRowCount(0); self.lb_group.setText("已选 %d 张 —— 先逐张核对原图，没问题再点「运行批量」。" % len(paths))
        self._fill_list()
        if paths:
            self.lst.setCurrentRow(0)   # 触发 _on_select → 立即预览第一张原图
        self.status.setText("已选 %d 张。点左列表逐张核对原图（看有没有载入失败/损坏）→ 再「运行批量」。" % len(paths))

    # ---- 运行（后台线程）----
    def _run(self):
        if not self._paths:
            QtWidgets.QMessageBox.information(self, "无图片", "先选图片或文件夹。"); return
        if self._thread is not None:
            return
        n_edit = len(self._overrides) + len(self._edited)   # 大声失败：重跑会清掉逐图调整，先警告
        if n_edit and QtWidgets.QMessageBox.question(
                self, "重跑将丢弃逐图调整",
                "已对 %d 张图做过逐图阈值/手动修正。重新运行批量会用当前全局阈值覆盖全部，这些逐图调整将丢失。\n继续？" % n_edit,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.b_run.setEnabled(False); self.bar.setVisible(True); self.bar.setRange(0, len(self._paths)); self.bar.setValue(0)
        self.status.setText("批量处理中…")
        self._run_settings = self._settings()   # 快照：预览/单图覆盖都基于此，跑完改染色不会混进结果
        self._overrides.clear(); self._edited.clear()   # 重跑 = 新全局阈值，丢手动覆盖/编辑
        self._invalidate_cnt()                  # 作废跑前在途逐图计数，防旧阈值 count 覆盖新结果（HIGH）
        self._thread = QtCore.QThread(self)
        self._worker = _BatchWorker(self._paths, self._run_settings)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda i, n: self.bar.setValue(i))
        self._worker.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, results):
        self._results = results
        if self._thread is not None:
            self._thread.quit(); self._thread.wait()
        self._thread = None; self._worker = None
        self.bar.setVisible(False); self.b_run.setEnabled(True)
        self._fill_table(); self._fill_list(); self._update_group()
        ok = sum(1 for r in results if r["ok"]); bad = len(results) - ok
        self.status.setText("完成 %d 张%s。点列表逐张审核叠加；个别不准用「此图阈值」纠正。"
                            % (ok, ("，%d 张失败" % bad) if bad else ""))
        if results:
            self.lst.setCurrentRow(0)

    def _fill_list(self):
        self.lst.blockSignals(True); self.lst.clear()
        if self._results:                       # 已算：显示阳性面积%
            for r in self._results:
                txt = "%s  —  %s" % (("%.2f%%" % r["area_pct"]) if r["ok"] else "失败", r["name"])
                it = QtWidgets.QListWidgetItem(("● " if r["group"] == "模型" else "○ ") + txt)
                it.setForeground(QtGui.QColor("#c06a2a" if r["group"] == "模型" else "#3a7"))
                self.lst.addItem(it)
        else:                                   # 未算：只列文件名供核对
            for pth in self._paths:
                nm = QtCore.QFileInfo(pth).fileName(); grp = ib.group_of(nm)
                it = QtWidgets.QListWidgetItem(("● " if grp == "模型" else "○ ") + "未算  —  " + nm)
                it.setForeground(QtGui.QColor("#c06a2a" if grp == "模型" else "#3a7"))
                self.lst.addItem(it)
        self.lst.blockSignals(False)

    def _set_table_row(self, i, r):
        vals = [str(i + 1), r["name"], r["group"], r.get("target", ""),
                ("%.3f" % r["area_pct"]) if r["ok"] else "失败",
                "—" if r.get("count") is None else str(r["count"]),
                "—" if r.get("total") is None else str(r["total"]),
                "—" if r.get("li") is None else "%.1f" % r["li"],
                r.get("thr_label") or "—",
                "—" if r.get("mean_od") is None else "%.1f" % r["mean_od"],
                "—" if r.get("iod") is None else "%.4g" % r["iod"],
                "—" if r.get("h_score") is None else "%.0f" % r["h_score"],
                "—" if r.get("score") is None else "%.3f" % r["score"], r.get("label", "—"),
                str(r.get("pos_px", 0)), str(r.get("tissue_px", 0))]
        gc = self.COLS.index("分级")
        for c, v in enumerate(vals):
            it = _table_item(v, numeric=(c not in (0, 1, 2, 3, 8, gc)), key=(c == 4))
            if c == gc and r.get("tier"):
                it.setForeground(QtGui.QColor(_TIER_COLOR.get(r["tier"], "#6b7280")))
            self.table.setItem(i, c, it)

    def _fill_table(self):
        self.table.setRowCount(len(self._results))
        for i, r in enumerate(self._results):
            self._set_table_row(i, r)

    def _update_one_row(self, idx):
        """只刷新第 idx 行的表格+列表项（编辑/阈值只动当前图，O(1)，不全表重建避免卡顿）。"""
        if not (0 <= idx < len(self._results)):
            return
        r = self._results[idx]
        self._set_table_row(idx, r)
        it = self.lst.item(idx)
        if it is not None:
            it.setText(("● " if r["group"] == "模型" else "○ ")
                       + ("%s  —  %s" % (("%.2f%%" % r["area_pct"]) if r["ok"] else "失败", r["name"])))
            it.setForeground(QtGui.QColor("#c06a2a" if r["group"] == "模型" else "#3a7"))

    def _update_group(self):
        gs = ib.group_summary(self._results)
        parts = [f"<b>{g}</b> n={v['n']} 均值 {v['mean']:.2f}% ± {v['std']:.2f}" for g, v in gs.items()]
        self.lb_group.setText("组聚合（阳性面积%）：　" + "　|　".join(parts) if parts else "无有效结果")

    # ---- 逐图预览(可编辑：画笔/套索/撤销，和单图一样) + 单图覆盖 ----
    def _on_select(self, row):
        if not (0 <= row < len(self._paths)):
            return
        self._cur_idx = row; self._cur = None; self._undo1 = []
        self._pv_token += 1            # 切图：作废上一张 in-flight 预览回调（防回填到新行）
        path = self._paths[row]
        has_result = row < len(self._results)
        for w in (self.sl_one, self.btn_reset_one, self.cb_tool1, self.btn_undo1, self.sp_brush1):
            w.setEnabled(has_result)
        if not has_result:                              # 未算 → 预览原图核对（不可编辑）；解码+解卷积也走后台
            self.view.tool = "browse"
            self.cb_tool1.blockSignals(True); self.cb_tool1.setCurrentIndex(0); self.cb_tool1.blockSignals(False)
            self.lb_one.setText("载入中…")
            self._submit_preview(path, self._base_settings(), raw=True)
            return
        r = self._results[row]
        if not r["ok"]:
            self.view.clear(); self.lb_one.setText("该图失败：" + r.get("error", "")); return
        ov = self._overrides.get(path, {}); red = (self._base_settings()["mode"] == "red")
        self.sl_one.blockSignals(True)
        if red:
            self.sl_one.setRange(0, 100); self.sl_one.setValue(ov.get("sat_min", self.sp_sat.value()))
            self.lb_one.setText("灵敏度%d" % self.sl_one.value())
        else:
            eff = self._eff_settings(path)      # 两滑块统一从【本图有效阈值】回填，避免未覆盖图串入上一张被覆盖图的值
            elo = int(eff.get("manual_lo", self.rg_global.values()[0]))
            ehi = int(eff.get("manual_hi", self.rg_global.values()[1]))
            self.sl_one.setRange(0, 255); self.sl_one.setValue(ehi)
            self.lb_one.setText("≤%d" % ehi)
            self.rg_global.set_values(elo, ehi)   # set_values 不 emit，无回算环
        self.sl_one.blockSignals(False)
        self._load_cur(row)

    def _ensure_pv_thread(self):
        if self._cnt_closing:
            return
        if self._pv_thread is None:
            self._pv_thread = QtCore.QThread(self)
            self._pv_worker = _BatchPreviewWorker()
            self._pv_worker.moveToThread(self._pv_thread)
            self._pv_worker.done.connect(self._on_preview_done)
            self._pv_trigger.connect(self._pv_worker.run)
            self._pv_thread.start()

    def _submit_preview(self, path, settings, raw):
        """提交逐图预览到后台（解码+解卷积）。raw=True 仅看原图(未算图核对)，False 建可编辑叠加态。"""
        self._cur = None                    # 载入中：清当前态（编辑入口都有 _cur is None 守卫）
        self._pv_token += 1; self._pv_raw = raw
        self._ensure_pv_thread()
        self._pv_worker.set_job(path, settings, self._pv_token)
        self._pv_trigger.emit()

    def _load_cur(self, row):
        """已算图载为可编辑态：解码+解卷积移【后台线程】，选大图不冻 UI；done 回调显示。"""
        path = self._paths[row]
        self.lb_one.setText("载入中…")
        self._submit_preview(path, self._eff_settings(path), raw=False)

    @QtCore.Slot(object)
    def _on_preview_done(self, res):
        if res.get("token") != self._pv_token:   # 过期（已切到别的图）→ 丢弃
            return
        path = res.get("path")
        if not res.get("ok"):
            self.view.clear(); self.lb_one.setText("预览失败:%s" % res.get("error", "")); self._cur = None; return
        rgb, pos, tissue, ch8 = res["rgb"], res["pos"], res["tissue"], res["ch8"]
        if ch8 is not None:
            self.rg_global.set_histogram(np.bincount(ch8[tissue], minlength=256)[:256])
        if self._pv_raw:                    # 未算图：只看原图核对，不建可编辑 _cur
            self.view.set_image(_rgb_to_pixmap(rgb))
            self.lb_one.setText("（先核对，再运行批量）"); return
        if path in self._edited:            # 回显该图手动编辑过的掩膜
            try:
                pos = np.unpackbits(self._edited[path], count=tissue.size).reshape(tissue.shape).astype(bool)
            except Exception:
                pass
        self._cur = {"path": path, "rgb": rgb, "mask": pos.copy(), "tissue": tissue,
                     "target": res["target"], "cc": res["cc"], "ch8": ch8, "is_dab": res["is_dab"],
                     "cs_ch": res["cs_ch"], "thr_label": res["thr_label"], "sr_pre": None}
        self._undo1 = []; self._show_raw_one = False
        self._build_cur_proxy()                         # 显示代理（大图下采样，重绘快）
        self._cur_show(self._cur_pix(), new_image=True) # set_base：scene 全分辨率，ROI/画笔坐标不变
        self.lb_one.setText("此图 %.2f%%" % (self._results[self._cur_idx]["area_pct"]
                                            if 0 <= self._cur_idx < len(self._results)
                                            and self._results[self._cur_idx].get("ok") else 0.0))

    _DISP_MAX = 2048

    def _build_cur_proxy(self):
        """为当前预览图建显示代理（长边>_DISP_MAX 下采样），存 prgb/pw/ph；小图原样。"""
        c = self._cur; rgb = c["rgb"]; h, w = rgb.shape[:2]
        if max(h, w) > self._DISP_MAX:
            import cv2
            sc = self._DISP_MAX / float(max(h, w)); pw, ph = max(1, round(w * sc)), max(1, round(h * sc))
            c["prgb"] = cv2.resize(rgb, (pw, ph), interpolation=cv2.INTER_AREA); c["pw"], c["ph"] = pw, ph
        else:
            c["prgb"] = rgb; c["pw"], c["ph"] = w, h

    def _cur_pix(self, raw=False):
        """当前图显示代理 pixmap（raw=原图 / 否则阳性叠加）。掩膜降采样到代理尺寸。"""
        c = self._cur; prgb = c["prgb"]; pw, ph = c["pw"], c["ph"]
        if raw:
            return _rgb_to_pixmap(prgb)
        mask = c["mask"]
        if (pw, ph) != (mask.shape[1], mask.shape[0]):
            import cv2
            mask = cv2.resize(mask.astype(np.uint8), (pw, ph), interpolation=cv2.INTER_NEAREST).astype(bool)
        return overlay_pixmap(prgb, mask)

    def _cur_show(self, pix, new_image=False):
        """贴显示代理：新图 set_base(scale 撑满,scene=全分辨率,清ROI)；同图只 setPixmap。"""
        v = self.view; c = self._cur
        if new_image or v._pix_item is None:
            v.set_base(pix, c["rgb"].shape[1], c["rgb"].shape[0])
        else:
            v._pix_item.setPixmap(pix)
        v.viewport().update()

    def _show_cur(self):
        if self._cur is None or self.view._pix_item is None:
            return
        self._cur_show(self._cur_pix())

    def _peek_one(self, on):
        """批量预览：按住 C 临时看原图，松开回叠加（走代理，按 C 不卡）。"""
        if self._cur is None or self.view._pix_item is None:
            return
        self._cur_show(self._cur_pix(raw=on))

    def _toggle_one_view(self):
        """批量预览：Tab 在 原图↔叠加 间一键切换。"""
        if self._cur is None or self.view._pix_item is None:
            return
        self._show_raw_one = not getattr(self, "_show_raw_one", False)
        self._cur_show(self._cur_pix(raw=self._show_raw_one))

    def _recompute_cur(self, thr_label=None):
        """从当前(可能手动改过)掩膜重算全套指标 → 更新该图结果行+表+列表。"""
        c = self._cur
        if c is None or not (0 <= self._cur_idx < len(self._results)):
            return
        lbl = thr_label if thr_label is not None else (
            "手动修正" if c["path"] in self._edited else c.get("thr_label", ""))   # 撤到底回原阈值标签
        m = ib.metrics_from(c["mask"], c["tissue"], c["target"], lbl, c["cc"], c["ch8"], c["is_dab"])
        self._results[self._cur_idx].update(**m)   # 快指标即时（area/OD/...）；计数/LI 由后台补
        self._update_one_row(self._cur_idx)      # 只刷当前行(O(1))，画笔每笔不再全表重建
        self._update_group()
        self.lb_one.setText("此图 %.2f%%" % m["area_pct"])
        s = self._base_settings()                # 逐图手改后计数/LI 走【后台线程】，与 full_metrics 同口径，不冻 UI
        if s.get("count_on") and s.get("mode") != "red" and m["pos_px"] > 0:
            self._cnt_pending = (self._cur_idx, c["mask"] & c["tissue"], c["tissue"], c.get("cs_ch"), s)
            self._cnt_timer.start(160)           # 防抖：画笔每笔/拖阈值停手后才算一次

    def _ensure_cnt_thread(self):
        if self._cnt_closing:
            return
        if self._cnt_thread is None:
            self._cnt_thread = QtCore.QThread(self)
            self._cnt_worker = _BatchCountWorker()
            self._cnt_worker.moveToThread(self._cnt_thread)
            self._cnt_worker.done.connect(self._on_cnt_done)
            self._cnt_trigger.connect(self._cnt_worker.run)
            self._cnt_thread.start()

    def _submit_cnt(self):
        if self._cnt_closing or self._cnt_pending is None:
            return
        idx, sel, tissue, cs_ch, s = self._cnt_pending
        self._cnt_token += 1
        self._cnt_tokens[idx] = self._cnt_token   # 该图最新 token（旧的丢弃，允许跨图各自回填）
        self._ensure_cnt_thread()
        self._cnt_worker.set_job(idx, sel, tissue, cs_ch, s, self._cnt_token)
        self._cnt_trigger.emit()

    @QtCore.Slot(object)
    def _on_cnt_done(self, res):
        idx = res.get("idx")
        if self._cnt_tokens.get(idx) != res.get("token"):   # 该图已有更新计数 → 丢弃过期
            return
        if not res.get("ok"):
            self.lb_one.setText("计数失败：%s" % res.get("error", "")); return
        if 0 <= idx < len(self._results):
            self._results[idx].update(count=res.get("count"), total=res.get("total"), li=res.get("li"))
            self._update_one_row(idx); self._update_group()

    # ---- 工具/画笔/套索/撤销（逐张细调，和单图一致）----
    def _on_tool1(self, *_):
        tool = self.cb_tool1.currentData() or "browse"
        self.view._painting = False; self.view._lasso = None   # 切工具清残留笔画态
        self.view.tool = tool
        self.sp_brush1.setEnabled(tool == "brush")
        self.view.setCursor(QtCore.Qt.CursorShape.CrossCursor if tool in ("brush", "lasso")
                            else QtCore.Qt.CursorShape.ArrowCursor)

    def _push_one_undo(self):
        if self._cur is not None:
            self._undo1.append(np.packbits(self._cur["mask"]))
            if len(self._undo1) > 24:
                self._undo1.pop(0)

    def _undo_one(self):
        if not self._undo1 or self._cur is None:
            return
        packed = self._undo1.pop(); t = self._cur["tissue"]
        self._cur["mask"] = np.unpackbits(packed, count=t.size).reshape(t.shape).astype(bool)
        if self._undo1:                                   # 还有更早的笔画 → 仍算手动编辑
            self._edited[self._cur["path"]] = np.packbits(self._cur["mask"])
        else:                                             # 撤回到底 → 回阈值态，不再标"手动修正"
            self._edited.pop(self._cur["path"], None)
        self._show_cur(); self._recompute_cur()

    def _on_one_stroke(self, x, y, r, additive):
        c = self._cur
        if c is None:
            return
        H, W = c["mask"].shape
        x0 = max(0, x - r); x1 = min(W, x + r + 1); y0 = max(0, y - r); y1 = min(H, y + r + 1)
        if x0 >= x1 or y0 >= y1:
            return
        yy, xx = np.ogrid[y0:y1, x0:x1]; circ = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
        sub = c["mask"][y0:y1, x0:x1]
        if additive:
            sub[circ & c["tissue"][y0:y1, x0:x1]] = True
        else:
            sub[circ] = False
        self._edited[c["path"]] = np.packbits(c["mask"])
        if self.view._pix_item is not None:     # 代理整体叠加重绘(~15ms，掩膜全分辨率编辑、显示走代理)
            self._cur_show(self._cur_pix())
        self._recompute_cur()

    def _on_one_lasso(self, pts, additive):
        c = self._cur
        if c is None or len(pts) < 3:
            return
        import cv2
        region = np.zeros(c["mask"].shape, np.uint8)
        cv2.fillPoly(region, [np.array(pts, np.int32).reshape(-1, 1, 2)], 1); region = region.astype(bool)
        if additive:
            c["mask"][region & c["tissue"]] = True
        else:
            c["mask"][region] = False
        self._edited[c["path"]] = np.packbits(c["mask"])
        self._show_cur(); self._recompute_cur()

    def _eff_settings(self, path):
        s = dict(self._run_settings) if self._run_settings else self._settings()   # 用运行时快照，防混染色
        ov = self._overrides.get(path)
        return {**s, **ov} if ov else s

    def _on_one_thr(self, v):
        # 此图阈值覆盖：写 override + 标签即时，防抖后重算（用缓存 ch8 不重解卷积）
        if self._cur is None or not (0 <= self._cur_idx < len(self._results)):
            return
        path = self._cur["path"]; red = (self._base_settings()["mode"] == "red")
        ov = dict(self._overrides.get(path, {})); ov["thr_mode"] = "manual"
        if red:
            ov["sat_min"] = int(v); self.lb_one.setText("灵敏度%d" % v)
        else:
            ov["manual_hi"] = int(v); ov.setdefault("manual_lo", self.rg_global.values()[0]); self.lb_one.setText("≤%d" % v)
        self._overrides[path] = ov
        self._edited.pop(path, None)        # 改阈值 → 丢该图手动编辑
        self._one_timer.start(40)

    def _apply_one_thr(self):
        c = self._cur
        if c is None:
            return
        path = c["path"]; s = self._eff_settings(path)
        try:
            if c.get("ch8") is not None and s.get("thr_mode") == "manual":   # deconv：用缓存通道，不重解卷积
                lo = int(s.get("manual_lo", 0)); hi = int(s.get("manual_hi", 255))
                c["mask"] = (c["ch8"] >= lo) & (c["ch8"] <= hi) & c["tissue"]
                thr_label = "[%d,%d]" % (lo, hi)
            elif s.get("mode") == "red":                                     # 天狼星红：缓存 rgb2lab 预计算，拖灵敏度只施廉价阈值(不重算 rgb2lab，与单图 _sr_pre 一致)
                if c.get("sr_pre") is None:
                    c["sr_pre"] = iq.sirius_red_precompute(c["rgb"])
                sm = int(s.get("sat_min", 50))
                c["mask"] = iq.sirius_red_mask(c["sr_pre"], sm)["red_mask"].copy()
                thr_label = "灵敏度%d" % sm
            else:                                                            # Otsu → 走 _compute
                res = ib._compute(c["rgb"], s)
                c["mask"] = res[0].copy(); thr_label = res[3]
        except Exception:
            return
        c["thr_label"] = thr_label; self._undo1 = []        # 记原阈值标签(撤到底回退用)
        self._show_cur(); self._recompute_cur(thr_label=thr_label)

    def _reset_one(self):
        if not (0 <= self._cur_idx < len(self._results)):
            return
        path = self._paths[self._cur_idx]
        self._overrides.pop(path, None); self._edited.pop(path, None)
        try:
            self._results[self._cur_idx].update(**ib.full_metrics(ib.load_rgb(path), self._base_settings()))
        except Exception:
            return
        self._fill_table(); self._fill_list(); self._on_select(self._cur_idx)

    # ---- 导出 ----
    def _export(self):
        if not self._results:
            QtWidgets.QMessageBox.information(self, "无数据", "先运行批量。"); return
        start = (_last_dir("ihc") + "/ihc_batch.csv") if _last_dir("ihc") else "ihc_batch.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出批量 CSV", start, "CSV (*.csv)")
        if not path:
            return
        _remember_dir("ihc", path)
        try:
            ib.export_csv(self._results, path, self._base_settings())
            self.status.setText("已导出 %d 行 → %s" % (len(self._results), path))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))
