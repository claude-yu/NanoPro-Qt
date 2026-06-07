"""WB 灰度定量分析面板（单图，P1.1）。

设计要点：
- 读图按 ImageJ 口径：调色板(P)图用原始索引值、其余转感知灰度；自动判极性(底亮/底暗)，可手动切换。
- ROI = 矩形选区（在图上拖框），每个 ROI 一行；测量 = wb_quant.measure_roi（已对齐 ImageJ，逐位相等）。
- 背景：无 / 环形(每带本地) / Rolling-ball(全局)。量化法：B 框带 IntDen（默认）/ A 泳道曲线峰面积。
- ImageJ Gel Analyzer 同款键：1=框首泳道 2=框下一泳道 3=测量(Plot Lanes) 4=重测 A=切换量化法；自动检测给初值。
- 数值表 + 泳道密度曲线(QPainter 自绘) + 归一化到内参 + 导出 CSV。
纯 Qt + wb_quant，无 matplotlib；可离屏构造测试。
"""
from __future__ import annotations

import csv
import os
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import wb_quant as wb
import icons
import theme


# ---------- 文件对话框「上次目录」记忆（复用全app QSettings；后续 IHC/荧光面板也用这套）----------
def _last_dir(key: str = "wb") -> str:
    """读上次用的目录（与编辑器 _last_dir 同一存储 dir/{key}）。"""
    return QtCore.QSettings("NanoPro", "SciEditQt").value("dir/%s" % key, "") or ""


def _remember_dir(key: str, path: str):
    if path:
        QtCore.QSettings("NanoPro", "SciEditQt").setValue("dir/%s" % key, QtCore.QFileInfo(path).absolutePath())


def _start_path(key: str, name: str = "") -> str:
    d = _last_dir(key)
    return (d + "/" + name) if (d and name) else (d or name)


# ---------- 读图（ImageJ 口径）----------
def _table_item(text, *, numeric: bool = False, key: bool = False, muted: bool = False) -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(str(text))
    align = QtCore.Qt.AlignmentFlag.AlignVCenter
    align |= QtCore.Qt.AlignmentFlag.AlignRight if numeric else QtCore.Qt.AlignmentFlag.AlignCenter
    item.setTextAlignment(align)
    if key:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    if muted:
        item.setForeground(QtGui.QColor(theme.colors()["muted"]))
    return item


def _table_to_tsv(table: QtWidgets.QTableWidget, selected_only: bool = False) -> str:
    if selected_only and table.selectedIndexes():
        indexes = sorted(table.selectedIndexes(), key=lambda x: (x.row(), x.column()))
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
        out = []
        for r in rows:
            out.append("\t".join((table.item(r, c).text() if table.item(r, c) else "") for c in cols))
        return "\n".join(out)
    headers = [table.horizontalHeaderItem(c).text() if table.horizontalHeaderItem(c) else "" for c in range(table.columnCount())]
    out = ["\t".join(headers)] if headers else []
    for r in range(table.rowCount()):
        out.append("\t".join((table.item(r, c).text() if table.item(r, c) else "") for c in range(table.columnCount())))
    return "\n".join(out)


def _copy_table(table: QtWidgets.QTableWidget, selected_only: bool = False):
    text = _table_to_tsv(table, selected_only)
    if text:
        QtWidgets.QApplication.clipboard().setText(text)


def _show_table_menu(table: QtWidgets.QTableWidget, pos, export_cb=None):
    menu = QtWidgets.QMenu(table)
    c = theme.colors()
    selected = bool(table.selectedIndexes())
    act_sel = menu.addAction(icons.tool_icon("copy", c["text"], 18), "复制选中")
    act_sel.setEnabled(selected)
    act_all = menu.addAction(icons.tool_icon("copy", c["text"], 18), "复制整表")
    if export_cb is not None:
        menu.addSeparator()
        act_export = menu.addAction("导出 CSV...")
    else:
        act_export = None
    chosen = menu.exec(table.viewport().mapToGlobal(pos))
    if chosen is act_sel:
        _copy_table(table, True)
    elif chosen is act_all:
        _copy_table(table, False)
    elif act_export is not None and chosen is act_export:
        export_cb()


def _setup_result_table(table: QtWidgets.QTableWidget, export_cb=None):
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
    table.customContextMenuRequested.connect(lambda pos: _show_table_menu(table, pos, export_cb))


def load_signal_and_pixmap(path, weighted: bool = False):
    """返回 (measure_array float 0-255, display_pixmap, polarity_auto)。ImageJ Image>Type>8-bit 同口径：
    调色板图=原始索引；16/32-bit 按显示范围(min→0,max→255)缩放；RGB→(R+G+B)/3 非加权(weighted=True 切加权)。"""
    from PIL import Image
    im = Image.open(path)
    if im.mode == "P":
        arr = np.asarray(im).astype(np.float64)          # 原始索引，ImageJ 同口径
    elif im.mode in ("I", "I;16", "F"):                  # 16/32-bit → 8-bit（Scale When Converting）
        raw = np.asarray(im).astype(np.float64)
        mn, mx = float(raw.min()), float(raw.max())
        arr = np.clip(np.floor((raw - mn) * (256.0 / (mx - mn + 1)) + 0.5), 0, 255) if mx > mn else np.zeros_like(raw)
    else:
        arr = wb.to_gray(np.asarray(im.convert("RGB")), weighted=weighted)
    disp = im.convert("RGB")
    qimg = QtGui.QImage(disp.tobytes(), disp.width, disp.height,
                        disp.width * 3, QtGui.QImage.Format.Format_RGB888).copy()
    pol = "light_on_dark" if float(np.median(arr)) < 128 else "dark_on_light"
    return arr, QtGui.QPixmap.fromImage(qimg), pol


def array_to_signal(arr, polarity):
    """measure_array → 信号(亮=带)。沿用 wb_quant.to_signal（dark_on_light 反相）。"""
    return wb.to_signal(arr, polarity)


# ---------- ROI 图像视图（拖框选区）----------
class ROIView(QtWidgets.QGraphicsView):
    roiAdded = QtCore.Signal()
    roiPicked = QtCore.Signal(int)   # 选中第 i 个 ROI（-1=无）

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self._pix_item = None
        self.rois: list[tuple[int, int, int, int]] = []   # 图像坐标 (x0,y0,x1,y1)
        self.roi_ids: list[int] = []      # 与 rois 平行的稳定 id（增删不变，内参定位用）
        self._next_id = 1
        self.markers: set[int] = set()    # 标为 Marker 的 ROI 下标（照测照显，但不计入定量/归一化）
        self.sel = -1
        self._drag0 = None
        self._rubber = None
        self._mode = None       # None/'draw'/'move'/'resize'
        self._edit_idx = -1
        self._edit_orig = None  # 编辑前的 (x0,y0,x1,y1)
        self._edit_start = None # 移动起点 scene pos
        self._edit_handle = None

    def _new_id(self):
        i = self._next_id; self._next_id += 1; return i

    _HANDLES = ("tl", "t", "tr", "l", "r", "bl", "b", "br")

    def _handle_points(self, rect):
        x0, y0, x1, y1 = rect; mx = (x0 + x1) / 2; my = (y0 + y1) / 2
        return {"tl": (x0, y0), "t": (mx, y0), "tr": (x1, y0), "l": (x0, my), "r": (x1, my),
                "bl": (x0, y1), "b": (mx, y1), "br": (x1, y1)}

    def _scene_tol(self, px=8):
        s = abs(self.transform().m11()) or 1.0
        return px / s

    def _handle_at(self, rect, sp):
        tol = self._scene_tol()
        for k, (hx, hy) in self._handle_points(rect).items():
            if abs(sp.x() - hx) <= tol and abs(sp.y() - hy) <= tol:
                return k
        return None

    def set_image(self, pixmap: QtGui.QPixmap):
        self.scene().clear()
        self._pix_item = self.scene().addPixmap(pixmap)
        self.scene().setSceneRect(QtCore.QRectF(pixmap.rect()))
        self.rois.clear(); self.roi_ids.clear(); self.markers.clear(); self.sel = -1
        self.fitInView(self.scene().sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.viewport().update()

    def clear(self):
        """清空画布与所有框（载入失败图时用，防上一张的框残留）。"""
        self.scene().clear(); self._pix_item = None
        self.rois.clear(); self.roi_ids.clear(); self.markers.clear(); self.sel = -1
        self.viewport().update()

    def set_rois(self, rois):
        self.rois = [tuple(int(v) for v in r) for r in rois]
        self.roi_ids = [self._new_id() for _ in self.rois]
        self.markers.clear()
        self.sel = -1
        self.viewport().update()

    # 缩放：滚轮缩放（以光标为中心）
    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(f, f)

    def zoom_by(self, f):
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.scale(f, f)

    def zoom_fit(self):
        if self._pix_item is not None:
            self.resetTransform()
            self.fitInView(self.scene().sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, e):
        if self._pix_item is None or e.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        sp = self.mapToScene(e.position().toPoint())
        # 1) 选中框的 8 手柄 → 缩放
        if 0 <= self.sel < len(self.rois):
            h = self._handle_at(self.rois[self.sel], sp)
            if h:
                self._mode = "resize"; self._edit_idx = self.sel
                self._edit_orig = self.rois[self.sel]; self._edit_handle = h
                return
        # 2) 框内部 → 选中 + 移动（取最上层命中）
        for i in range(len(self.rois) - 1, -1, -1):
            x0, y0, x1, y1 = self.rois[i]
            if x0 <= sp.x() <= x1 and y0 <= sp.y() <= y1:
                self.sel = i; self.roiPicked.emit(i)
                self._mode = "move"; self._edit_idx = i
                self._edit_orig = self.rois[i]; self._edit_start = sp
                self.viewport().update(); return
        # 3) 空白 → 画新框
        self._mode = "draw"; self._drag0 = sp; self._rubber = QtCore.QRectF(sp, sp)

    def mouseMoveEvent(self, e):
        sp = self.mapToScene(e.position().toPoint())
        if self._mode == "draw" and self._drag0 is not None:
            self._rubber = QtCore.QRectF(self._drag0, sp).normalized()
            self.viewport().update(); return
        if self._mode == "resize":
            self.rois[self._edit_idx] = self._apply_resize(self._edit_orig, self._edit_handle, sp)
            self.viewport().update(); return
        if self._mode == "move":
            br = self.scene().sceneRect()
            x0, y0, x1, y1 = self._edit_orig
            dx = sp.x() - self._edit_start.x(); dy = sp.y() - self._edit_start.y()
            w = x1 - x0; h = y1 - y0
            nx0 = min(max(br.left(), x0 + dx), br.right() - w)
            ny0 = min(max(br.top(), y0 + dy), br.bottom() - h)
            self.rois[self._edit_idx] = (int(nx0), int(ny0), int(nx0 + w), int(ny0 + h))
            self.viewport().update(); return
        # 悬停光标：手柄→方向拉伸箭头；框内→移动手形
        h = self._handle_at(self.rois[self.sel], sp) if 0 <= self.sel < len(self.rois) else None
        if h:
            self.setCursor(self._cursor_for_handle(h))
        elif self._inside_any(sp) is not None:
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def _cursor_for_handle(self, h):
        cs = QtCore.Qt.CursorShape
        return {"l": cs.SizeHorCursor, "r": cs.SizeHorCursor,
                "t": cs.SizeVerCursor, "b": cs.SizeVerCursor,
                "tl": cs.SizeFDiagCursor, "br": cs.SizeFDiagCursor,
                "tr": cs.SizeBDiagCursor, "bl": cs.SizeBDiagCursor}.get(h, cs.SizeAllCursor)

    def _inside_any(self, sp):
        for i in range(len(self.rois) - 1, -1, -1):
            x0, y0, x1, y1 = self.rois[i]
            if x0 <= sp.x() <= x1 and y0 <= sp.y() <= y1:
                return i
        return None

    def _apply_resize(self, rect, h, sp):
        br = self.scene().sceneRect()
        x0, y0, x1, y1 = rect
        sx = min(max(br.left(), sp.x()), br.right()); sy = min(max(br.top(), sp.y()), br.bottom())
        if "l" in h: x0 = sx
        if "r" in h: x1 = sx
        if "t" in h: y0 = sy
        if "b" in h: y1 = sy
        x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))   # 拖过头自动翻正
        if x1 - x0 < 3: x1 = x0 + 3
        if y1 - y0 < 3: y1 = y0 + 3
        return (int(x0), int(y0), int(x1), int(y1))

    def mouseReleaseEvent(self, e):
        if self._mode == "draw" and self._rubber is not None:
            r = self._rubber.normalized(); br = self.scene().sceneRect()
            x0 = int(max(br.left(), min(r.left(), br.right())))
            y0 = int(max(br.top(), min(r.top(), br.bottom())))
            x1 = int(max(br.left(), min(r.right(), br.right())))
            y1 = int(max(br.top(), min(r.bottom(), br.bottom())))
            if x1 - x0 >= 3 and y1 - y0 >= 3:
                self.rois.append((x0, y0, x1, y1))
                self.roi_ids.append(self._new_id())
                self.sel = len(self.rois) - 1
                self.roiAdded.emit()
            self._mode = None; self._drag0 = None; self._rubber = None
            self.viewport().update(); return
        if self._mode in ("move", "resize"):
            self._mode = None
            self.roiAdded.emit()   # 框改了 → 重测/重画
            self.viewport().update(); return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace) and 0 <= self.sel < len(self.rois):
            d = self.sel
            self.rois.pop(d)
            if d < len(self.roi_ids):
                self.roi_ids.pop(d)
            self.markers = {(i if i < d else i - 1) for i in self.markers if i != d}  # 删后下标重排
            self.sel = -1
            self.roiAdded.emit(); self.viewport().update(); return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        if self._pix_item is None:
            return
        sp = self.mapToScene(e.pos())
        hit = -1
        for i, (x0, y0, x1, y1) in enumerate(self.rois):
            if x0 <= sp.x() <= x1 and y0 <= sp.y() <= y1:
                hit = i; break
        if hit < 0:
            return
        menu = QtWidgets.QMenu(self)
        is_m = hit in self.markers
        act = menu.addAction("取消 Marker" if is_m else "标记为 Marker（不计入定量）")
        act_del = menu.addAction("删除此 ROI")
        chosen = menu.exec(e.globalPos())
        if chosen is act:
            self.markers.discard(hit) if is_m else self.markers.add(hit)
            self.roiAdded.emit(); self.viewport().update()
        elif chosen is act_del:
            self.rois.pop(hit)
            if hit < len(self.roi_ids):
                self.roi_ids.pop(hit)
            self.markers = {(i if i < hit else i - 1) for i in self.markers if i != hit}
            self.sel = -1
            self.roiAdded.emit(); self.viewport().update()

    def drawForeground(self, painter: QtGui.QPainter, rect):
        painter.save()
        c = theme.colors()
        for i, (x0, y0, x1, y1) in enumerate(self.rois):
            sel = (i == self.sel)
            is_m = i in self.markers
            col = c["accent"] if sel else ("#e8913c" if is_m else "#27c08a")   # Marker=橙色虚线
            pen = QtGui.QPen(QtGui.QColor(col), 0)
            pen.setCosmetic(True); pen.setWidthF(2.0)
            if is_m:
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(26, 138, 255, 40) if sel else QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
            # 序号标签（Marker 标 M）
            painter.setPen(QtGui.QColor("#e8913c") if is_m else QtGui.QColor("#ffffff"))
            f = painter.font(); f.setPointSize(9); f.setBold(True); painter.setFont(f)
            ly = (y0 - 3) if y0 > 12 else (y0 + 13)  # 贴顶时标签画到框内，避免被场景裁掉
            painter.drawText(QtCore.QPointF(x0 + 2, ly), ("M" if is_m else str(i + 1)))
            if sel:   # 选中框画 8 个缩放手柄（白底蓝边小方块）
                hs = self._scene_tol(4)
                painter.setBrush(QtGui.QColor("#ffffff"))
                hp = QtGui.QPen(QtGui.QColor(c["accent"]), 0); hp.setCosmetic(True); hp.setWidthF(1.5)
                painter.setPen(hp)
                for (hx, hy) in self._handle_points((x0, y0, x1, y1)).values():
                    painter.drawRect(QtCore.QRectF(hx - hs, hy - hs, hs * 2, hs * 2))
        if self._rubber is not None:
            pen = QtGui.QPen(QtGui.QColor(c["accent"]), 0); pen.setCosmetic(True)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine); pen.setWidthF(1.5)
            painter.setPen(pen); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(self._rubber)
        painter.restore()


# ---------- 泳道密度曲线（QPainter 自绘，ImageJ Plot Lanes 式：所有泳道竖向堆叠）----------
class ProfilePlot(QtWidgets.QWidget):
    """box 单曲线(set_profile) 或 凝胶多泳道堆叠(set_lanes)：所有泳道曲线竖向堆叠（ImageJ Plot Lanes），
    每条一条直线基线(橙锚点拖)+绿分隔线(拖/双击加/右键删)，每段=一个峰，面积实时算；每条都可独立调。"""
    baselineChanged = QtCore.Signal()   # 基线端或分隔线改动后（lane dict 已就地改）

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self._prof = np.zeros(0)
        self._base = 0.0
        self._title = ""
        self._lanes = None      # 所有泳道 [{prof,bl,br,dividers,lane}]；None=box 单曲线
        self._label_pct = False
        self._drag = None       # (li, 'bl'|'br'|('div',k))
        self._geoms = []        # 每泳道渲染几何 (xl,xr,ytop,ybot,n,pmin,span)，鼠标命中用

    def set_label_peaks(self, on):
        self._label_pct = bool(on); self.update()

    def set_profile(self, prof, baseline=0.0, title=""):
        self._prof = np.asarray(prof, np.float64)
        self._base = float(baseline)
        self._title = title
        self._lanes = None
        self._drag = None
        self.update()

    def set_lanes(self, lanes, title=""):
        """显示所有泳道（竖向堆叠，每条可独立调基线/分隔线）。"""
        self._lanes = list(lanes or [])
        self._title = title
        self._drag = None
        self.update()

    def set_active_lane(self, lane, title=""):   # 兼容旧调用
        self.set_lanes([lane] if lane else [], title)

    # ---- 每泳道几何 / 命中 ----
    TITLE_H = 18   # 顶部标题条高度，避免标题与第一条泳道标签重叠

    def _compute_geoms(self, W, H, m):
        geoms = []
        lanes = self._lanes or []
        N = len(lanes)
        if N == 0:
            return geoms
        top = self.TITLE_H if self._title else 4
        rowH = max(1.0, (H - top)) / N
        for li, ln in enumerate(lanes):
            prof = ln["prof"]; n = prof.size
            pmin = float(prof.min()) if n else 0.0
            span = (float(prof.max()) - pmin) if n else 1.0
            span = span or 1.0
            ytop = top + li * rowH + 13       # +13 给每行左上的「泳道 N」标签留位
            ybot = top + (li + 1) * rowH - 5
            geoms.append((m, W - m, ytop, ybot, n, pmin, span))
        return geoms

    @staticmethod
    def _xy(geom):
        xl, xr, ytop, ybot, n, pmin, span = geom
        def X(i): return xl + (xr - xl) * i / max(1, n - 1)
        def Y(v): return ybot - (ybot - ytop) * (v - pmin) / span
        return X, Y

    def _lane_at_y(self, y):
        for li, g in enumerate(self._geoms):
            if g[2] - 8 <= y <= g[3] + 6:
                return li
        return -1

    def _x_to_i(self, li, x):
        if not (0 <= li < len(self._geoms)):
            return 0
        xl, xr, _, _, n, _, _ = self._geoms[li]
        return int(round((x - xl) / max(1, (xr - xl)) * (n - 1)))

    def _hit(self, pos):
        """返回 (li, 'bl'/'br'/('div',k)) 或 None。"""
        for li, g in enumerate(self._geoms):
            if not (g[2] - 8 <= pos.y() <= g[3] + 8):
                continue
            ln = self._lanes[li]; prof = ln["prof"]; n = g[4]
            X, Y = self._xy(g)
            for key, idx in (("bl", int(ln["bl"])), ("br", int(ln["br"]))):
                idx = max(0, min(n - 1, idx))
                if abs(pos.x() - X(idx)) <= 8 and abs(pos.y() - Y(prof[idx])) <= 10:
                    return (li, key)
            for k, d in enumerate(ln["dividers"]):
                if abs(pos.x() - X(int(d))) <= 6:
                    return (li, ("div", k))
        return None

    def mousePressEvent(self, e):
        if self._lanes and e.button() == QtCore.Qt.MouseButton.LeftButton:
            h = self._hit(e.position())
            if h is not None:
                self._drag = h
                self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and self._lanes:
            import gel_analyzer as ga
            li, key = self._drag
            ln = self._lanes[li]; n = ln["prof"].size
            i = max(0, min(n - 1, self._x_to_i(li, e.position().x())))
            free = bool(e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier)
            if key == "bl":
                ln["bl"] = max(0, min(i, int(ln["br"]) - 1))
            elif key == "br":
                ln["br"] = min(n - 1, max(i, int(ln["bl"]) + 1))
            else:
                k = key[1]
                if not free:
                    i = ga.nearest_valley(ln["prof"], i, 8)
                lo = max(int(ln["bl"]) + 1, (ln["dividers"][k - 1] + 1) if k > 0 else 0)
                hi = min(int(ln["br"]) - 1, (ln["dividers"][k + 1] - 1) if k + 1 < len(ln["dividers"]) else n - 1)
                ln["dividers"][k] = int(max(lo, min(i, hi)))
            self.baselineChanged.emit(); self.update()
            return
        if self._lanes and self._hit(e.position()):
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None; self.unsetCursor()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self._lanes:
            import gel_analyzer as ga
            li = self._lane_at_y(e.position().y())
            if 0 <= li < len(self._lanes):
                ln = self._lanes[li]
                i = max(0, min(ln["prof"].size - 1, self._x_to_i(li, e.position().x())))
                if not (e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier):
                    i = ga.nearest_valley(ln["prof"], i, 8)
                a, b = sorted((int(ln["bl"]), int(ln["br"])))
                if a < i < b and all(abs(i - int(d)) > 4 for d in ln["dividers"]):
                    ln["dividers"].append(i); ln["dividers"].sort()
                    self.baselineChanged.emit(); self.update()
                    return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e):
        if not self._lanes:
            return
        h = self._hit(e.pos() if hasattr(e, "pos") else e.position())
        if h is not None and isinstance(h[1], tuple) and h[1][0] == "div":
            menu = QtWidgets.QMenu(self)
            if menu.addAction("删除此分隔线") == menu.exec(e.globalPos()):
                del self._lanes[h[0]]["dividers"][h[1][1]]
                self.baselineChanged.emit(); self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        c = theme.colors()
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(c["base"]))
        m = 10
        if self._lanes:
            self._paint_lanes(p, c, W, H, m)
        elif self._prof.size >= 2:
            self._paint_single(p, c, W, H, m)
        else:
            p.setPen(QtGui.QColor(c["muted"]))
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "框住条带 → 凝胶分析：每条泳道一条曲线竖向堆叠；拖橙锚点调基线、拖绿线分峰、双击加线、右键删")
        if self._title:
            p.setPen(QtGui.QColor(c["muted"]))
            p.drawText(QtCore.QRectF(m, 2, W - 2 * m, 16), QtCore.Qt.AlignmentFlag.AlignLeft, self._title)
        p.end()

    def _paint_single(self, p, c, W, H, m):
        prof = self._prof
        pmin, pmax = float(prof.min()), float(prof.max()); span = (pmax - pmin) or 1.0
        n = prof.size
        def X(i): return m + (W - 2 * m) * i / (n - 1)
        def Y(v): return H - m - (H - 2 * m) * (v - pmin) / span
        yb = Y(self._base)
        p.setPen(QtGui.QPen(QtGui.QColor(c["muted"]), 1, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(QtCore.QPointF(m, yb), QtCore.QPointF(W - m, yb))
        fill = QtGui.QPainterPath(); fill.moveTo(X(0), yb)
        for i in range(n):
            fill.lineTo(X(i), Y(max(prof[i], self._base)))
        fill.lineTo(X(n - 1), yb); fill.closeSubpath()
        p.fillPath(fill, QtGui.QColor(26, 138, 255, 70))
        path = QtGui.QPainterPath(); path.moveTo(X(0), Y(prof[0]))
        for i in range(1, n):
            path.lineTo(X(i), Y(prof[i]))
        p.setPen(QtGui.QPen(QtGui.QColor(c["accent"]), 1.6)); p.drawPath(path)

    def _paint_lanes(self, p, c, W, H, m):
        import gel_analyzer as ga
        self._geoms = self._compute_geoms(W, H, m)
        lanes = self._lanes
        # 全部泳道所有峰总量（Label Peaks % 分母；缺省退各泳道自身）
        grand = 0.0
        cache = []
        for li, ln in enumerate(lanes):
            g = self._geoms[li]; n = g[4]
            if n < 2:
                cache.append(None); continue
            prof = ln["prof"]
            bl = max(0, min(n - 1, int(ln["bl"]))); br = max(0, min(n - 1, int(ln["br"])))
            pk = ga.lane_peaks(prof, bl, br, ln["dividers"])
            cache.append((bl, br, pk))
            grand += sum(max(0.0, q["area"]) for q in pk)
        grand = grand or 1.0
        fl = p.font(); fl.setPointSize(8); fl.setBold(True)
        for li, ln in enumerate(lanes):
            g = self._geoms[li]
            if cache[li] is None:
                continue
            bl, br, peaks = cache[li]
            prof = ln["prof"]; n = g[4]
            X, Y = self._xy(g)
            tot = ln.get("_pct_total") or grand
            denom = (br - bl) or 1
            def baseval(i): return prof[bl] + (prof[br] - prof[bl]) * (i - bl) / denom
            for q in peaks:                                    # 各峰段填充
                l, r = q["l"], q["r"]
                fb = QtGui.QPainterPath(); fb.moveTo(X(l), Y(baseval(l)))
                for i in range(l, r + 1):
                    fb.lineTo(X(i), Y(max(prof[i], baseval(i))))
                fb.lineTo(X(r), Y(baseval(r))); fb.closeSubpath()
                p.fillPath(fb, QtGui.QColor(26, 138, 255, 70))
            path = QtGui.QPainterPath(); path.moveTo(X(0), Y(prof[0]))   # 曲线
            for i in range(1, n):
                path.lineTo(X(i), Y(prof[i]))
            p.setPen(QtGui.QPen(QtGui.QColor(c["accent"]), 1.4)); p.drawPath(path)
            p.setPen(QtGui.QPen(QtGui.QColor(c["muted"]), 1, QtCore.Qt.PenStyle.DashLine))   # 基线
            p.drawLine(QtCore.QPointF(X(bl), Y(prof[bl])), QtCore.QPointF(X(br), Y(prof[br])))
            p.setPen(QtGui.QPen(QtGui.QColor("#27c08a"), 1))    # 分隔竖线
            for d in ln["dividers"]:
                d = int(d)
                if bl < d < br:
                    p.drawLine(QtCore.QPointF(X(d), Y(baseval(d))), QtCore.QPointF(X(d), Y(prof[d])))
            p.setFont(fl)
            p.setPen(QtGui.QColor(c["muted"]))                 # 泳道号（行左上）
            p.drawText(QtCore.QPointF(g[0] + 1, g[2] - 3), "泳道 %d" % ln.get("lane", li + 1))
            p.setPen(QtGui.QColor(c["text"]))                  # 峰号 + %
            for k, q in enumerate(peaks):
                lab = "%d" % (k + 1)
                if self._label_pct:
                    lab += " %.1f%%" % (max(0.0, q["area"]) / tot * 100)
                p.drawText(QtCore.QPointF(X(q["peak"]) - 8, Y(prof[q["peak"]]) - 4), lab)
            p.save()                                           # 基线两端橙锚点
            p.setPen(QtGui.QPen(QtGui.QColor("#e8913c"), 1)); p.setBrush(QtGui.QColor("#e8913c"))
            for idx in (bl, br):
                p.drawRect(QtCore.QRectF(X(idx) - 3, Y(prof[idx]) - 3, 6, 6))
            p.restore()
            if li < len(lanes) - 1:                            # 行间分隔线
                p.setPen(QtGui.QPen(QtGui.QColor(c["border"]), 1))
                p.drawLine(QtCore.QPointF(g[0], g[3] + 3), QtCore.QPointF(g[1], g[3] + 3))


# ---------- 主面板 ----------
class WBAnalyzerPanel(QtWidgets.QWidget):
    """单图 WB 灰度定量。"""

    COLS = ["#", "Area", "Mean", "RawIntDen", "IntDen", "归一化"]

    def __init__(self, editor=None):
        super().__init__()
        self.editor = editor
        self._arr = None        # measure_array
        self._signal = None     # 信号(亮=带)
        self._bgimg = None      # rolling-ball 背景图(可空)
        self._bg_warning = ""
        self._polarity = "dark_on_light"
        self._rgb_weighted = False   # RGB→灰度：False=(R+G+B)/3(ImageJ 默认) / True=加权亮度
        self._loaded_path = None     # 记住路径，切换 RGB 模式时重载
        self._control = None    # 内参标识 cid（("r",id)/("b",band)），None=无
        self._method = "box"    # box=框带IntDen / lane=泳道峰面积
        self._gel_mode = False  # 凝胶分析模式（每框一泳道）
        self._gel_prof = None
        self._gel_lanes = None
        self._gel_active = 0
        self._gel_horizontal = None  # 凝胶 profile 轴向：None=按框长宽比自动 / True=横排(沿x) / False=竖排
        self._gel_template = None   # ImageJ Select First Lane 记下的模板框尺寸
        self._results = []      # 每 ROI dict
        self._build_ui()

    # ---- UI ----
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(6)
        tc = theme.colors()

        def _qpush(text: str, icon_name: str | None = None, primary: bool = False) -> QtWidgets.QPushButton:
            btn = QtWidgets.QPushButton(text)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(27)
            if primary:
                btn.setProperty("primary", True)
            if icon_name:
                btn.setIcon(icons.tool_icon(icon_name, tc["text"], 16))
                btn.setIconSize(QtCore.QSize(16, 16))
            return btn

        def _qtool(icon_name: str, text: str, tip: str) -> QtWidgets.QToolButton:
            btn = QtWidgets.QToolButton()
            btn.setObjectName("wbZoomButton")
            btn.setText(text)
            btn.setToolTip(tip)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.setIcon(icons.tool_icon(icon_name, tc["text"], 16))
            btn.setIconSize(QtCore.QSize(16, 16))
            return btn

        def _label(text: str) -> QtWidgets.QLabel:
            lab = QtWidgets.QLabel(text)
            lab.setObjectName("wbFieldLabel")
            return lab

        # 顶栏
        top = QtWidgets.QFrame()
        top.setObjectName("wbTopBar")
        bar = QtWidgets.QHBoxLayout(top); bar.setContentsMargins(8, 6, 8, 6); bar.setSpacing(6)
        title = QtWidgets.QLabel("WB 灰度定量")
        title.setObjectName("wbPanelTitle")
        bar.addWidget(title)
        bar.addSpacing(4)
        b_open = _qpush("载入图片…", "folder", True); b_open.clicked.connect(self.open_file)
        b_layer = _qpush("用当前图层", "import_image"); b_layer.clicked.connect(self.use_active_layer)
        bar.addWidget(b_open); bar.addWidget(b_layer)
        bar.addSpacing(8)
        bar.addWidget(_label("极性"))
        self.cb_pol = QtWidgets.QComboBox()
        self.cb_pol.addItems(["自动", "暗带/浅底", "亮带/深底"])
        self.cb_pol.currentIndexChanged.connect(self._on_polarity_changed)
        bar.addWidget(self.cb_pol)
        bar.addWidget(_label("RGB灰度"))
        self.cb_rgb = QtWidgets.QComboBox()
        self.cb_rgb.addItems(["(R+G+B)/3", "加权亮度"])   # ImageJ 默认非加权
        self.cb_rgb.setToolTip("RGB 图转 8-bit 灰度方式。(R+G+B)/3=ImageJ Image>Type>8-bit 默认；加权=0.299R+0.587G+0.114B。仅影响彩色图。")
        self.cb_rgb.currentIndexChanged.connect(self._on_rgb_changed)
        bar.addWidget(self.cb_rgb)
        bar.addWidget(_label("背景"))
        self.cb_bg = QtWidgets.QComboBox()
        self.cb_bg.addItems(["无", "环形(本地)", "Rolling-ball(全局)"])
        self.cb_bg.currentIndexChanged.connect(lambda *_: self.measure())
        bar.addWidget(self.cb_bg)
        self.sp_ring = QtWidgets.QSpinBox(); self.sp_ring.setRange(2, 60); self.sp_ring.setValue(10)
        self.sp_ring.setPrefix("环 "); self.sp_ring.setSuffix(" px"); self.sp_ring.setToolTip("环形背景宽度")
        self.sp_ring.valueChanged.connect(lambda *_: self.measure())
        bar.addWidget(self.sp_ring)
        bar.addStretch(1)
        # 缩放：适应窗口(纯文字) / 放大(🔍+) / 缩小(🔍-)（滚轮也可缩放）。适应用文字，避免和放大镜重复
        bz = QtWidgets.QToolButton(); bz.setObjectName("wbZoomButton"); bz.setText("适应")
        bz.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        bz.setToolTip("适应窗口（滚轮缩放/拖滚动条平移）"); bz.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        bz.clicked.connect(lambda: self.view.zoom_fit()); bar.addWidget(bz)
        bzi = _qtool("zoom", "", "放大")
        bzi.clicked.connect(lambda: self.view.zoom_by(1.25)); bar.addWidget(bzi)
        bzo = _qtool("zoom_out", "", "缩小")
        bzo.clicked.connect(lambda: self.view.zoom_by(0.8)); bar.addWidget(bzo)
        root.addWidget(top)

        # 第二行：动作（含 ImageJ 键提示）
        action = QtWidgets.QFrame()
        action.setObjectName("wbActionBar")
        bar2 = QtWidgets.QHBoxLayout(action); bar2.setContentsMargins(8, 6, 8, 6); bar2.setSpacing(6)
        for txt, icon_name, cb, tip, primary in (
            ("智能检测条带", "wand", self.auto_detect,
             "自动把每条带各框一个框、出曲线。框可单独拖角/边缩放微调、点别的框切换曲线；数不对填「带数」。", True),
            ("测量 (3)", "measure", self.measure, "对所有框测量 → 表格+曲线", False),
            ("清空 ROI", "trash", self.clear_rois, "删除所有选区", False),
        ):
            b = _qpush(txt, icon_name, primary); b.setToolTip(tip); b.clicked.connect(cb); bar2.addWidget(b)
        self.b_method = _qpush("量化法：框带 IntDen (A)", "adjust")
        self.b_method.setToolTip("A 切换：框带 IntDen（默认，对齐 ImageJ）/ 泳道曲线峰面积")
        self.b_method.clicked.connect(self.toggle_method)
        bar2.addWidget(self.b_method)
        bar2.addSpacing(10)
        # 凝胶分析（复刻 ImageJ Gel Analyzer）：框一条整泳道 → 多峰曲线 + 峰脚直线基线 + 每带 Area
        self.b_gel = _qpush("凝胶分析", "measure")
        self.b_gel.setToolTip("ImageJ Gel Analyzer 复刻：框住整条泳道(含所有带)→ 密度曲线分峰、峰脚直线扣背景、逐带积分 Area。归一化留 Excel。")
        self.b_gel.clicked.connect(self.gel_analyze)
        bar2.addWidget(self.b_gel)
        bar2.addWidget(_label("带数"))
        self.sp_nbands = QtWidgets.QSpinBox(); self.sp_nbands.setRange(0, 50); self.sp_nbands.setValue(0)
        self.sp_nbands.setToolTip("0=自动分峰；填具体数=只取突出度最高的 N 个峰（已知几条带时更稳）")
        self.sp_nbands.setSpecialValueText("自动")
        bar2.addWidget(self.sp_nbands)
        bar2.addStretch(1)
        self.b_batch = _qpush("批量定量…", "copy")
        self.b_batch.setToolTip("多张 WB 图一次跑完：每图自动框泳道→凝胶分析→每带 Area→汇总表+CSV（解决 ImageJ 开一堆窗口）")
        self.b_batch.clicked.connect(self.open_batch)
        bar2.addWidget(self.b_batch)
        self.b_csv = _qpush("导出 CSV", "download"); self.b_csv.clicked.connect(self.export_csv)
        bar2.addWidget(self.b_csv)
        root.addWidget(action)

        # 主体：左图右(表+曲线)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.setObjectName("wbSplit")
        self.view = ROIView()
        self.view.setObjectName("wbImageView")
        self.view.roiAdded.connect(self._on_rois_changed)
        self.view.roiPicked.connect(self._on_roi_picked)
        split.addWidget(self.view)

        right = QtWidgets.QFrame(); right.setObjectName("wbResultsPanel")
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8); rv.setSpacing(6)
        rhead = QtWidgets.QHBoxLayout()
        rtitle = QtWidgets.QLabel("结果与曲线")
        rtitle.setObjectName("wbSectionTitle")
        rhead.addWidget(rtitle)
        rhead.addStretch(1)
        rtag = QtWidgets.QLabel("ImageJ 口径")
        rtag.setObjectName("wbTag")
        rhead.addWidget(rtag)
        rv.addLayout(rhead)
        # 竖向 splitter：上=数值表，下=内参+密度曲线 → 拖中间边界可把曲线面板拉大
        vsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        vsplit.setObjectName("wbVSplit"); vsplit.setChildrenCollapsible(False)
        vsplit.setHandleWidth(10)        # 拖柄加宽便于鼠标抓取（默认太细抓不住）
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setObjectName("wbTable")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        _setup_result_table(self.table, self.export_csv)
        self.table.itemSelectionChanged.connect(self._on_table_sel)
        self.empty_results = QtWidgets.QLabel("载入图像并测量后，结果会显示在这里。")
        self.empty_results.setObjectName("analysisEmpty")
        self.empty_results.setWordWrap(True)
        rv.addWidget(self.empty_results)
        vsplit.addWidget(self.table)
        bottom = QtWidgets.QWidget(); bv = QtWidgets.QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0); bv.setSpacing(6)
        crow = QtWidgets.QHBoxLayout()           # 内参选择
        crow.addWidget(_label("内参"))
        self.cb_ctrl = QtWidgets.QComboBox(); self.cb_ctrl.addItem("无", None)
        self.cb_ctrl.setToolTip("选择归一化基准；导出 CSV 中保留 Normalized 列。")
        self.cb_ctrl.currentIndexChanged.connect(self._on_control_changed)
        crow.addWidget(self.cb_ctrl); crow.addStretch(1)
        bv.addLayout(crow)
        self.plot = ProfilePlot(); self.plot.setObjectName("wbPlot"); bv.addWidget(self.plot, 1)
        self.plot.baselineChanged.connect(self._on_baseline_dragged)
        vsplit.addWidget(bottom)
        vsplit.setSizes([300, 280])
        rv.addWidget(vsplit, 1)
        split.addWidget(right)
        split.setSizes([620, 420])
        root.addWidget(split, 1)

        self.status = QtWidgets.QLabel("载入 WB 图 → 框泳道 → 测量。ImageJ 凝胶键：Ctrl+1 选首泳道·Ctrl+2 选下条·Ctrl+3 画曲线(Plot Lanes)·Ctrl+4 重画·Ctrl+5 标峰%。框带键：1/2 框选·3 测量·A 切量化法")
        self.status.setObjectName("wbStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # ImageJ Gel Analyzer 同款快捷键 Ctrl+1~Ctrl+5
        for seq, fn in (("Ctrl+1", self.gel_select_first), ("Ctrl+2", self.gel_select_next),
                        ("Ctrl+3", self.gel_analyze), ("Ctrl+4", self.gel_replot),
                        ("Ctrl+5", self.toggle_label_peaks)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    # ---- ImageJ 键位 ----
    def keyPressEvent(self, e):
        k = e.key()
        if k == QtCore.Qt.Key.Key_3:
            self.measure()
        elif k == QtCore.Qt.Key.Key_4:
            self.measure()  # Re-plot
        elif k == QtCore.Qt.Key.Key_A:
            self.toggle_method()
        elif k in (QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2):
            self.view.setFocus()  # 进入框选（提示用户拖框）
            self.status.setText("拖框画泳道/带；松开即测量。再按一次或直接拖下一个。")
        elif k in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):
            self.view.keyPressEvent(e)  # 焦点在按钮时也能删选中的 ROI
        else:
            super().keyPressEvent(e)

    # ---- 载入 ----
    def open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 WB 图片", _last_dir("wb"), "图片 (*.tif *.tiff *.png *.jpg *.jpeg *.bmp)")
        if path:
            _remember_dir("wb", path)      # 记住目录，下次从这里打开
            self.load_path(path)

    def load_path(self, path):
        try:
            arr, pix, pol = load_signal_and_pixmap(path, weighted=self._rgb_weighted)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "载入失败", str(ex)); return
        self._loaded_path = path
        self._arr = arr
        self._auto_pol = pol
        self.view.set_image(pix)
        self._apply_polarity()
        self._reset_results_state()
        self.status.setText("已载入 %dx%d，自动极性=%s。拖框选泳道/带 → 测量。"
                             % (arr.shape[1], arr.shape[0], "暗带/浅底" if pol == "dark_on_light" else "亮带/深底"))

    def use_active_layer(self):
        if self.editor is None:
            return
        layer = getattr(self.editor, "active", None)
        img = layer.get("image") if isinstance(layer, dict) else None
        if img is None or img.isNull():
            QtWidgets.QMessageBox.information(self, "无图层", "没有可用的活动图层图像。请用「载入图片」。"); return
        # QImage → numpy 灰度（.copy() 立即拥有数据，不依赖 qimg 生命周期）
        qimg = img.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        w, h = qimg.width(), qimg.height()
        a = np.frombuffer(qimg.constBits(), np.uint8).reshape(h, w, 4).copy()
        self._loaded_path = None
        self._arr = wb.to_gray(a[..., :3], weighted=self._rgb_weighted)
        self._auto_pol = "light_on_dark" if float(np.median(self._arr)) < 128 else "dark_on_light"
        self.view.set_image(QtGui.QPixmap.fromImage(img))
        self._apply_polarity()
        self._reset_results_state()
        self.status.setText("已用当前图层 %dx%d。拖框选泳道/带 → 测量。" % (w, h))

    # ---- 极性 / 量化法 ----
    def _on_rgb_changed(self, *_):
        self._rgb_weighted = (self.cb_rgb.currentIndex() == 1)
        if not self._loaded_path:    # 仅彩色图受影响
            return
        rois = list(self.view.rois); markers = set(self.view.markers)   # 重载会清框→先快照
        self.load_path(self._loaded_path)
        if rois:
            self.view.set_rois(rois)
            self.view.markers = {i for i in markers if i < len(rois)}   # set_rois 清了 markers，恢复
            self._on_rois_changed()

    def _on_polarity_changed(self, *_):
        self._apply_polarity(); self.measure()

    def _apply_polarity(self):
        if self._arr is None:
            return
        idx = self.cb_pol.currentIndex()
        pol = getattr(self, "_auto_pol", "dark_on_light") if idx == 0 else \
            ("dark_on_light" if idx == 1 else "light_on_dark")
        self._polarity = pol
        self._signal = array_to_signal(self._arr, pol)
        self._bgimg = None

    def toggle_method(self):
        self._method = "lane" if self._method == "box" else "box"
        self.b_method.setText("量化法：%s (A)" % ("泳道峰面积" if self._method == "lane" else "框带 IntDen"))
        self.measure()

    # ---- 自动检测：每条带各框一个框（可单独拖角/边缩放）----
    def auto_detect(self):
        if self._signal is None or self._arr is None:
            return
        import wb_batch as wbb
        import gel_analyzer as ga
        region = wbb.auto_lane_rect(self._arr, self._polarity)   # 整排带紧致区
        rx0, ry0, rx1, ry1 = region
        horiz = (rx1 - rx0) >= (ry1 - ry0)                       # 排列方向：宽>高=横排带(沿x)，否则竖排(沿y)
        prof = ga.gel_profile(self._arr, region, horizontal=horiz, polarity=self._polarity)
        n_total = int(self.sp_nbands.value()) or None
        bands = ga.find_bands(prof, n_bands=n_total)             # 沿排列方向找各带峰(谷到谷)
        if not bands:
            self.status.setText("自动检测未找到条带——请手动拖框，或调极性/填带数。"); return
        sig = self._signal; med = float(np.median(sig))
        pad = 2
        boxes = []
        for (l, pk, r) in bands:
            l = int(l); r = int(r)
            if horiz:                                            # 带沿 x：x=谷到谷，y=该带紧致高度
                ax0 = rx0 + l; ax1 = rx0 + r
                sub = np.clip(sig[ry0:ry1, ax0:ax1] - med, 0, None)
                if sub.size and sub.max() > 0 and (rows := np.where(sub.max(axis=1) > sub.max() * 0.4)[0]).size:
                    by0 = ry0 + max(0, int(rows.min()) - pad); by1 = ry0 + min(ry1 - ry0, int(rows.max()) + 1 + pad)
                else:
                    by0, by1 = ry0, ry1
            else:                                                # 带沿 y：y=谷到谷，x=该带紧致宽度
                by0 = ry0 + l; by1 = ry0 + r
                sub = np.clip(sig[by0:by1, rx0:rx1] - med, 0, None)
                if sub.size and sub.max() > 0 and (cols := np.where(sub.max(axis=0) > sub.max() * 0.4)[0]).size:
                    ax0 = rx0 + max(0, int(cols.min()) - pad); ax1 = rx0 + min(rx1 - rx0, int(cols.max()) + 1 + pad)
                else:
                    ax0, ax1 = rx0, rx1
            if ax1 - ax0 >= 4 and by1 - by0 >= 4:
                boxes.append((ax0, by0, ax1, by1))
        if not boxes:
            self.status.setText("自动检测未框出条带——请手动拖框。"); return
        self._gel_horizontal = horiz                            # 记排列方向，gel_analyze 据此 profile 每框
        self.view.set_rois(boxes); self.view.sel = 0            # 每带一框，选中第1个露手柄
        self.gel_analyze()                                      # 每框=一泳道，各自一条曲线
        self.status.setText("自动检测 %d 条带，每带一个框（已选第1个，拖角/边缩放微调、点别的框切换曲线；数不对填「带数」）。" % len(boxes))

    def clear_rois(self):
        self.view.set_rois([])
        self._reset_results_state()
        self.plot.set_profile([])

    def _reset_results_state(self):
        """清空测量结果/内参/凝胶态（载图/清空/换层共用，防 _control 残留导致静默错配）。"""
        self.table.setRowCount(0)
        self._results = []
        self._control = None
        self._gel_mode = False
        self._gel_prof = None
        self._gel_horizontal = None    # 清空/载图→轴向回自动
        self.cb_ctrl.blockSignals(True)
        self.cb_ctrl.clear(); self.cb_ctrl.addItem("无", None)
        self.cb_ctrl.blockSignals(False)
        self.plot.set_profile([])
        self._sync_empty_results()

    def _sync_empty_results(self):
        if hasattr(self, "empty_results"):
            self.empty_results.setVisible(self.table.rowCount() == 0)

    def _on_rois_changed(self):
        """ROI 增/删/拖动/缩放后：凝胶模式→重画每泳道曲线；否则→框带测量。"""
        if self._gel_mode and self.view.rois:
            self.gel_analyze()
        else:
            self.measure()

    # ---- ImageJ Gel Analyzer 同款 Ctrl+1~5 ----
    def gel_select_first(self):
        """Ctrl+1 Select First Lane：把当前框设为第1道并清掉其它框（=ImageJ 硬重置，'再按取消'语义），记尺寸为模板。"""
        boxes = self.view.rois
        self.view.setFocus()
        if not boxes:
            self.status.setText("Ctrl+1：先拖一个框罩住第一条带（或一排带），再按 Ctrl+1 定为第 1 道。")
            return
        idx = self.view.sel if 0 <= self.view.sel < len(boxes) else len(boxes) - 1
        tmpl = boxes[idx]
        tw = tmpl[2] - tmpl[0]; th = tmpl[3] - tmpl[1]
        if tw >= 2 * th:                      # ImageJ：宽>2×高 → 弹「泳道真是水平的吗」确认
            r = QtWidgets.QMessageBox.question(
                self, "Gel Analyzer",
                "Are the lanes really horizontal?\n\nImageJ 假定选区宽度超过高度两倍时泳道是水平的（一排带横向排列）。",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.view.set_rois([tmpl])            # 清掉其它框，只留第 1 道
        self.view.sel = 0
        self._gel_template = tmpl
        self._gel_mode = False
        self.view.viewport().update()
        self.status.setText("Lane 1 selected（第 1 道已定，尺寸记为模板）。移到下条带按 Ctrl+2，框完按 Ctrl+3 画曲线。")

    def gel_select_next(self):
        """Ctrl+2 Select Next Lane：仅多个独立样本道时用（如一块胶跑了好几列样本）。
        单条 WB 带（一排带）不需要按 Ctrl+2——直接 Ctrl+1→Ctrl+3 即可，曲线会自动按峰拆分。"""
        self.view.setFocus()
        tmpl = self._gel_template
        if tmpl is None or not self.view.rois:
            self.status.setText("先按 Ctrl+1 定第 1 道。注：单条带不用 Ctrl+2，Ctrl+1→Ctrl+3 即可；Ctrl+2 只用于多列样本。")
            return
        tw = tmpl[2] - tmpl[0]; th = tmpl[3] - tmpl[1]
        last = self.view.rois[-1]
        nx0 = last[2] + 6                      # 同尺寸、紧贴上一道右侧、与模板同一行(y)
        nbox = (nx0, tmpl[1], nx0 + tw, tmpl[1] + th)
        self.view.rois.append(nbox); self.view.roi_ids.append(self.view._new_id())
        self.view.sel = len(self.view.rois) - 1
        self.view.viewport().update()
        self.status.setText("Lane %d selected（同尺寸已加，可拖到目标条带）。继续 Ctrl+2 或 Ctrl+3 画曲线。"
                            % len(self.view.rois))

    def gel_replot(self):
        """Ctrl+4 Re-plot Lanes：用当前设置重跑凝胶分析。"""
        self.gel_analyze()

    def toggle_label_peaks(self):
        """Ctrl+5 Label Peaks：切换在峰顶标各带占总量 %。"""
        on = not getattr(self.plot, "_label_pct", False)
        self.plot.set_label_peaks(on)
        self.status.setText("Ctrl+5 标峰%s：峰顶显示各带占总量百分比。" % ("：开" if on else "：关"))

    # ---- 批量定量 ----
    def open_batch(self):
        if not hasattr(self, "_batch_dlg") or self._batch_dlg is None:
            self._batch_dlg = BatchDialog(self)
        self._batch_dlg.show(); self._batch_dlg.raise_(); self._batch_dlg.activateWindow()

    # ---- 凝胶分析（ImageJ Plot Lanes：每框=一泳道一条曲线，曲线上基线+分隔线分峰）----
    def gel_analyze(self):
        if self._arr is None:
            QtWidgets.QMessageBox.information(self, "无图", "先载入一张 WB 图。"); return
        boxes = list(self.view.rois)
        if not boxes:
            QtWidgets.QMessageBox.information(self, "先框泳道",
                "像 ImageJ 一样框泳道：①一个框罩住一排带（带数填几条）②或每条带各一个框。可点「自动检测泳道」生成后拖动/缩放微调，再 Ctrl+3。")
            return
        import gel_analyzer as ga
        n_total = int(self.sp_nbands.value()) or None
        nonm = [i for i in range(len(boxes)) if i not in self.view.markers]
        single = len(nonm) == 1
        # 调和：框没动的泳道沿用用户调过的 bl/br/分隔线，只对新框/改了的框重新自动分峰
        prev_by_id = {ln["roi_id"]: ln for ln in (self._gel_lanes or [])}
        lanes = []
        for i in nonm:
            box = boxes[i]
            prof = ga.gel_profile(self._arr, box, horizontal=self._gel_horizontal, polarity=self._polarity)
            if prof.size < 2:
                continue
            roi_id = self.view.roi_ids[i] if i < len(self.view.roi_ids) else i
            prev = prev_by_id.get(roi_id)
            if prev is not None and prev.get("box") == box and prev["prof"].size == prof.size:
                lane = {"box": box, "roi_id": roi_id, "prof": prof,    # 框未变→保留手动调整
                        "bl": max(0, min(int(prev["bl"]), prof.size - 1)),
                        "br": max(0, min(int(prev["br"]), prof.size - 1)),
                        "dividers": [int(d) for d in prev["dividers"] if 0 < int(d) < prof.size - 1],
                        "lane": len(lanes) + 1}
            else:                                                     # 新框/改了→自动放分隔线
                # 单框→按「带数」在框内分峰；多框→每框就是一条带(1 个峰，不再内部拆)
                bands = ga.find_bands(prof, n_bands=(n_total if single else 1))
                peaks_pos = [b[1] for b in bands] if bands else [int(np.argmax(prof))]
                dividers = []
                for k in range(len(peaks_pos) - 1):
                    a2, b2 = peaks_pos[k], peaks_pos[k + 1]
                    dividers.append(a2 + int(np.argmin(prof[a2:b2 + 1])))   # 相邻峰间谷底
                lane = {"box": box, "roi_id": roi_id, "prof": prof,
                        "bl": 0, "br": int(prof.size - 1), "dividers": dividers,
                        "lane": len(lanes) + 1}
            lanes.append(lane)
        if not lanes:
            self.status.setText("框内出不了曲线——框太小或极性不对。"); return
        self._gel_mode = True
        self._gel_lanes = lanes
        self._gel_active = self._active_lane_for_sel()
        self._rebuild_gel_results()
        self.plot.set_lanes(lanes, "密度曲线 · %d 条泳道" % len(lanes))   # 全部泳道竖向堆叠（短标题，操作说明在状态栏）
        self._refresh_table()

    def _active_lane_for_sel(self):
        """当前选中框对应的泳道下标（点哪个框看哪条曲线）；默认 0。"""
        if 0 <= self.view.sel < len(self.view.roi_ids):
            rid = self.view.roi_ids[self.view.sel]
            for li, lane in enumerate(self._gel_lanes):
                if lane["roi_id"] == rid:
                    return li
        return 0

    def _rebuild_gel_results(self):
        """从各泳道(prof/bl/br/dividers)算出所有峰 → 扁平成 self._results（每峰一行）。"""
        import gel_analyzer as ga
        results = []; gi = 0
        for lane in self._gel_lanes:
            peaks = ga.lane_peaks(lane["prof"], lane["bl"], lane["br"], lane["dividers"])
            lane["peaks"] = peaks
            for k, pk in enumerate(peaks):
                gi += 1
                results.append({"i": gi, "lane_no": lane["lane"], "band_no": k + 1,
                                "cid": "r%d_%d" % (lane["roi_id"], k), "value": pk["area"],
                                "pk": pk, "lane": lane, "rect": lane["box"]})
        # Label Peaks %：分母=所有泳道所有峰总面积（对齐 ImageJ，非单泳道）
        grand = sum(max(0.0, r["pk"]["area"]) for r in results) or 1.0
        for lane in self._gel_lanes:
            lane["_pct_total"] = grand
        self._results = results

    def _on_baseline_dragged(self):
        """曲线上改基线/分隔线后：重算各峰 → 刷新表（框不变）。"""
        if not self._gel_mode:
            return
        self._rebuild_gel_results()
        self._refresh_table()

    def _refresh_gel_table(self):
        norms = self._norm_values()
        self.table.blockSignals(True)
        try:
            self.table.setHorizontalHeaderLabels(["泳道", "带", "Area", "宽", "PeakH", "归一化"])
            self.table.setRowCount(len(self._results))
            for row, r in enumerate(self._results):
                pk = r["pk"]
                cells = [str(r["lane_no"]), str(r["band_no"]), "%.1f" % pk["area"],
                         str(pk["width"]), "%.1f" % pk["peak_h"],
                         ("%.3f" % norms[row]) if not np.isnan(norms[row]) else "—"]
                for col, txt in enumerate(cells):
                    it = _table_item(txt, numeric=True, key=(col == 2))
                    self.table.setItem(row, col, it)
        finally:
            self.table.blockSignals(False)
        self._sync_empty_results()
        self._sync_control_combo()
        nlanes = len(self._gel_lanes) if self._gel_lanes else 0
        self.status.setText("凝胶分析 %d 泳道 / %d 峰 · 曲线上拖橙锚点调基线、拖绿线/双击/右键调分峰 · 框可拖动缩放 · 极性=%s"
                            % (nlanes, len(self._results),
                               "暗带/浅底" if self._polarity == "dark_on_light" else "亮带/深底"))

    # ---- 测量 ----
    def measure(self):
        self._gel_mode = False; self._gel_prof = None   # 普通框带/泳道测量 → 退出凝胶模式
        if self._signal is None or not self.view.rois:
            self.table.setRowCount(0); self._results = []
            self._sync_empty_results()
            return
        sig = self._signal
        bg_mode = self.cb_bg.currentIndex()
        self._bg_warning = ""
        net = sig
        if bg_mode == 2:  # rolling-ball 全局
            try:
                self._bgimg = wb.rolling_ball_bg(sig, radius=50.0)
                net = np.clip(sig - self._bgimg, 0, None)
            except Exception as ex:
                self._bg_warning = "Rolling-ball 不可用：%s；已退回无背景。" % ex
                if self.cb_bg.currentIndex() == 2:
                    self.cb_bg.blockSignals(True)
                    self.cb_bg.setCurrentIndex(0)
                    self.cb_bg.blockSignals(False)
                bg_mode = 0
                net = sig
        results = []
        for i, (x0, y0, x1, y1) in enumerate(self.view.rois):
            m = wb.rect_mask(sig.shape, x0, y0, x1, y1)
            if bg_mode == 1:      # 环形本地背景（在原信号上估，measure 传 bg）
                bg = wb.ring_background(sig, m, ring_px=int(self.sp_ring.value()))
                r = wb.measure_roi(sig, m, bg=bg)
                net_for_lane = np.clip(sig - bg, 0, None)  # A 法同口径：扣同一环背景
            else:                 # 无 / rolling-ball(已在 net 扣)
                r = wb.measure_roi(net, m, bg=0.0)
                net_for_lane = net
            raw = wb.measure_roi(sig, m, bg=0.0)["intden"]
            # A 法：泳道曲线峰面积（与 B 法同背景口径）
            prof = wb.lane_profile(net_for_lane, (x0, y0, x1, y1), vertical=True)
            base = min(prof[0], prof[-1]) if prof.size else 0.0
            pa = wb.peak_area(prof, base)
            value = pa if self._method == "lane" else r["intden"]
            cid = "r%d" % (self.view.roi_ids[i] if i < len(self.view.roi_ids) else -i - 1)  # 字符串 cid：findData 可靠匹配
            results.append({"i": i + 1, "cid": cid, "area": r["area"], "mean": r["mean"],
                            "raw": raw, "intden": r["intden"], "peak_area": pa,
                            "value": value, "prof": prof, "base": base, "rect": (x0, y0, x1, y1),
                            "marker": i in self.view.markers})
        self._results = results
        self._refresh_table()
        if self._bg_warning:
            self.status.setText(self._bg_warning)
        if 0 <= self.view.sel < len(results):
            self._show_profile(self.view.sel)

    def _norm_values(self):
        # 内参以稳定标识 cid（增删/重测/标 Marker 后不漂移）定位，找不到→不归一化（不静默错配）
        vals = np.array([r["value"] for r in self._results], float)
        out = np.full(len(vals), np.nan)
        ctrl_pos = next((i for i, r in enumerate(self._results)
                         if r.get("cid") == self._control and not r.get("marker")), -1)
        if ctrl_pos >= 0 and vals[ctrl_pos] != 0:
            out = vals / vals[ctrl_pos]
        for i, r in enumerate(self._results):   # Marker 不参与归一化（恒显「Marker」）
            if r.get("marker"):
                out[i] = np.nan
        return out

    def _sync_control_combo(self):
        cur = self.cb_ctrl.currentData()   # itemData = r["cid"]（稳定标识）
        self.cb_ctrl.blockSignals(True)
        self.cb_ctrl.clear(); self.cb_ctrl.addItem("无", None)
        for r in self._results:
            if r.get("marker"):           # Marker 不作内参候选
                continue
            self.cb_ctrl.addItem("%s %d" % ("带" if self._gel_mode else "ROI", r["i"]), r["cid"])
        pos = self.cb_ctrl.findData(cur) if cur is not None else 0
        if pos < 0:                       # 原内参被删/被标 Marker → 退回「无」并清状态（防静默错配）
            self._control = None
        self.cb_ctrl.setCurrentIndex(pos if pos >= 0 else 0)
        self.cb_ctrl.blockSignals(False)

    def _refresh_table(self):
        if self._gel_mode:
            return self._refresh_gel_table()
        norms = self._norm_values()
        self.table.blockSignals(True)   # 逐格 setItem 不触发 itemSelectionChanged（防重建中 re-entrant）
        try:
            self.table.setHorizontalHeaderLabels(self.COLS)   # 从凝胶模式切回时还原表头
            self.table.setRowCount(len(self._results))
            for row, r in enumerate(self._results):
                is_m = r.get("marker")
                norm_txt = "Marker" if is_m else (("%.3f" % norms[row]) if not np.isnan(norms[row]) else "—")
                cells = [("M%d" % r["i"]) if is_m else str(r["i"]), str(r["area"]), "%.2f" % r["mean"],
                         "%.0f" % r["raw"],
                         "%.0f" % (r["peak_area"] if self._method == "lane" else r["intden"]),
                         norm_txt]
                for col, txt in enumerate(cells):
                    it = _table_item(txt, numeric=(col > 0), key=(col == 4))
                    if is_m:
                        it.setForeground(QtGui.QColor("#e8913c"))
                    self.table.setItem(row, col, it)
        finally:
            self.table.blockSignals(False)
        self._sync_empty_results()
        self._sync_control_combo()
        unit = "泳道峰面积" if self._method == "lane" else "框带 IntDen"
        bgname = ["无", "环形", "Rolling-ball"][self.cb_bg.currentIndex()]
        nm = sum(1 for r in self._results if r.get("marker"))
        mtxt = "（%d 个 Marker 不计入）" % nm if nm else ""
        self.status.setText("测量 %d 个 ROI%s · 量化法=%s · 背景=%s · 极性=%s · 右键 ROI 可标 Marker"
                            % (len(self._results), mtxt, unit, bgname,
                               "暗带/浅底" if self._polarity == "dark_on_light" else "亮带/深底"))

    # ---- 选中联动 ----
    def _on_roi_picked(self, i):
        if self._gel_mode and self._gel_lanes:   # 全显模式：点框只高亮框，曲线不切（已全显）
            return
        self.table.blockSignals(True)   # 视图点选已设 view.sel；编程式选表行不再回灌 _on_table_sel
        try:
            if 0 <= i < self.table.rowCount():
                self.table.selectRow(i)
        finally:
            self.table.blockSignals(False)
        self._show_profile(i)

    def _on_table_sel(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if self._gel_mode:           # 选峰行→高亮其所在框（曲线全显，不切）
            if 0 <= i < len(self._results):
                rid = self._results[i]["lane"]["roi_id"]
                for bi, b_id in enumerate(self.view.roi_ids):
                    if b_id == rid:
                        self.view.sel = bi; self.view.viewport().update(); break
            return
        self.view.sel = i; self.view.viewport().update()
        self._show_profile(i)

    def _show_profile(self, i):
        if self._gel_mode:
            return   # 凝胶模式保持整条多峰曲线，不切到单 ROI
        if 0 <= i < len(self._results):
            r = self._results[i]
            self.plot.set_profile(r["prof"], r["base"],
                                  "ROI %d 泳道密度曲线（峰面积=%.0f）" % (r["i"], r["peak_area"]))

    def _on_control_changed(self, idx):
        self._control = self.cb_ctrl.currentData()   # = r["cid"] 标识，或 None（无）
        self._refresh_table()

    # ---- 导出 ----
    def export_csv(self):
        if not self._results:
            return
        default = "gel_areas.csv" if self._gel_mode else "wb_quant.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 CSV", _start_path("wb_csv", default), "CSV (*.csv)")
        if not path:
            return
        _remember_dir("wb_csv", path)
        norms = self._norm_values()
        if self._gel_mode:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["Lane", "Band", "Area", "Peak", "Left", "Right", "Width", "PeakHeight",
                                "box_x0", "box_y0", "box_x1", "box_y1", "Normalized", "polarity"])
                    for row, r in enumerate(self._results):
                        pk = r["pk"]; bx = r["lane"]["box"]
                        w.writerow([r["lane_no"], r["band_no"], "%.3f" % pk["area"], pk["peak"],
                                    pk["l"], pk["r"], pk["width"], "%.3f" % pk["peak_h"],
                                    bx[0], bx[1], bx[2], bx[3],
                                    ("%.4f" % norms[row]) if not np.isnan(norms[row]) else "",
                                    self._polarity])
                self.status.setText("已导出 %d 峰 → %s（归一化可留 Excel 算）" % (len(self._results), path))
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["ROI", "x0", "y0", "x1", "y1", "Area", "Mean", "RawIntDen",
                            "IntDen", "PeakArea", "Value", "Normalized", "is_marker",
                            "method", "background", "polarity"])
                unit = "lane_peak_area" if self._method == "lane" else "box_intden"
                bgname = ["none", "ring", "rolling_ball"][self.cb_bg.currentIndex()]
                for row, r in enumerate(self._results):
                    x0, y0, x1, y1 = r["rect"]
                    w.writerow([r["i"], x0, y0, x1, y1, r["area"], "%.4f" % r["mean"],
                                "%.1f" % r["raw"], "%.1f" % r["intden"], "%.1f" % r["peak_area"],
                                "%.1f" % r["value"],
                                ("%.4f" % norms[row]) if not np.isnan(norms[row]) else "",
                                "1" if r.get("marker") else "0",
                                unit, bgname, self._polarity])
            self.status.setText("已导出 %d 行 → %s" % (len(self._results), path))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))


# ---------- 批量定量对话框（多图 → 汇总表 + CSV）----------
class BatchDialog(QtWidgets.QDialog):
    """多张 WB 图批量定量（可视化审核版）：左=图片列表，中=选中图+可编辑检测框预览，右=汇总表。
    每张图都能看一眼检测对不对、单独改「带数」重检测、拖框微调，结果实时进表；→ 长/宽表 CSV，归一化留 Excel。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("wbBatchDialog")
        self.setWindowTitle("WB 批量定量")
        self.resize(1080, 640)
        self._items: list[dict] = []   # 每图 {path,name,arr,pol,pixmap,boxes,horiz,n_bands,areas,ok,error}
        self._cur = -1
        v = QtWidgets.QVBoxLayout(self); v.setContentsMargins(10, 10, 10, 10); v.setSpacing(8)
        tc = theme.colors()

        def _ic(btn, name):
            btn.setIcon(icons.tool_icon(name, tc["text"], 16)); btn.setIconSize(QtCore.QSize(16, 16)); return btn

        head = QtWidgets.QFrame(); head.setObjectName("wbTopBar")
        bar = QtWidgets.QHBoxLayout(head); bar.setContentsMargins(8, 6, 8, 6); bar.setSpacing(6)
        t = QtWidgets.QLabel("WB 批量定量"); t.setObjectName("wbPanelTitle"); bar.addWidget(t)
        b_pick = _ic(QtWidgets.QPushButton("选择图片…"), "folder"); b_pick.clicked.connect(self.pick_files); bar.addWidget(b_pick)
        lab = QtWidgets.QLabel("默认带数"); lab.setObjectName("wbFieldLabel"); bar.addWidget(lab)
        self.sp_n = QtWidgets.QSpinBox(); self.sp_n.setRange(0, 50); self.sp_n.setValue(0)
        self.sp_n.setSpecialValueText("自动"); self.sp_n.setToolTip("批量默认带数；每张图还可单独改")
        bar.addWidget(self.sp_n)
        self.b_run = _ic(QtWidgets.QPushButton("运行批量"), "measure"); self.b_run.setProperty("primary", True)
        self.b_run.clicked.connect(self.run); self.b_run.setEnabled(False); bar.addWidget(self.b_run)
        bar.addStretch(1)
        self.b_long = _ic(QtWidgets.QPushButton("导出长表 CSV"), "download"); self.b_long.clicked.connect(lambda: self.export("long"))
        self.b_wide = _ic(QtWidgets.QPushButton("导出宽表 CSV"), "download"); self.b_wide.clicked.connect(lambda: self.export("wide"))
        self.b_long.setEnabled(False); self.b_wide.setEnabled(False)
        bar.addWidget(self.b_long); bar.addWidget(self.b_wide)
        v.addWidget(head)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        # 左：图片列表
        self.lst = QtWidgets.QListWidget(); self.lst.setObjectName("wbBatchList")
        self.lst.setMinimumWidth(170); self.lst.currentRowChanged.connect(self._select_image)
        self.lst.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst.customContextMenuRequested.connect(self._show_list_menu)
        split.addWidget(self.lst)
        # 中：预览（可编辑框）+ 单图控制
        mid = QtWidgets.QWidget(); mv = QtWidgets.QVBoxLayout(mid); mv.setContentsMargins(0, 0, 0, 0); mv.setSpacing(6)
        self.view = ROIView(); self.view.setObjectName("wbImageView")
        self.view.roiAdded.connect(self._on_preview_edited)
        mv.addWidget(self.view, 1)
        crow = QtWidgets.QHBoxLayout()
        self.lbl_cur = QtWidgets.QLabel("选张图核对检测框"); self.lbl_cur.setObjectName("wbFieldLabel")
        crow.addWidget(self.lbl_cur); crow.addStretch(1)
        crow.addWidget(QtWidgets.QLabel("此图带数"))
        self.sp_cur = QtWidgets.QSpinBox(); self.sp_cur.setRange(0, 50); self.sp_cur.setSpecialValueText("自动")
        self.sp_cur.setToolTip("改这张图的带数"); crow.addWidget(self.sp_cur)
        self.b_redetect = _ic(QtWidgets.QPushButton("重检测此图"), "wand")
        self.b_redetect.clicked.connect(self._redetect_current); crow.addWidget(self.b_redetect)
        mv.addLayout(crow)
        split.addWidget(mid)
        # 右：汇总表
        self.table = QtWidgets.QTableWidget(0, 2); self.table.setObjectName("wbTable")
        self.table.setHorizontalHeaderLabels(["图片", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        _setup_result_table(self.table)
        self.table.itemSelectionChanged.connect(self._on_table_pick)
        split.addWidget(self.table)
        split.setSizes([190, 560, 330])
        v.addWidget(split, 1)

        self.status = QtWidgets.QLabel("选择多张 WB 图 → 运行批量。每图自动框各带；点列表逐张核对，框不准就拖角缩放或改带数「重检测此图」。")
        self.status.setObjectName("wbStatus"); self.status.setWordWrap(True)
        v.addWidget(self.status)

    # ---- 选图 ----
    def pick_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择多张 WB 图", _last_dir("wb"), "图片 (*.tif *.tiff *.png *.jpg *.jpeg *.bmp)")
        if paths:
            _remember_dir("wb", paths[0])
            self._paths = list(paths)
            self.b_run.setEnabled(True)
            self._preview_paths()
            self.status.setText("已选 %d 张 —— 先点左列表逐张核对原图（看有没有载入失败/损坏），再点「运行批量」检测。" % len(paths))

    def _show_list_menu(self, pos):
        row = self.lst.indexAt(pos).row()
        if row < 0:
            return
        self.lst.setCurrentRow(row)
        menu = QtWidgets.QMenu(self.lst)
        c = theme.colors()
        act_copy = menu.addAction(icons.tool_icon("copy", c["text"], 18), "复制文件名")
        act_redetect = menu.addAction("重检测此图")
        act_redetect.setEnabled(0 <= row < len(self._items))
        menu.addSeparator()
        act_remove = menu.addAction(icons.tool_icon("trash", c["danger"], 18), "从批量移除")
        chosen = menu.exec(self.lst.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            name = os.path.basename(str(self._paths[row])) if row < len(self._paths) else self.lst.item(row).text()
            QtWidgets.QApplication.clipboard().setText(name)
        elif chosen is act_redetect:
            self._redetect_current()
        elif chosen is act_remove:
            self._remove_current(row)

    def _remove_current(self, row=None):
        row = self.lst.currentRow() if row is None else row
        if row < 0:
            return
        if row < len(self._paths):
            self._paths.pop(row)
        if row < len(self._items):
            self._items.pop(row)
        self._cur = -1
        if self._items:
            self._fill_list()
            self._fill_table()
        else:
            self._preview_paths()
        if self.lst.count():
            self.lst.setCurrentRow(min(row, self.lst.count() - 1))
        self.b_run.setEnabled(bool(self._paths))
        self.b_long.setEnabled(bool(self._items))
        self.b_wide.setEnabled(any(x["boxes"] for x in self._items))

    def _preview_paths(self):
        """选完图：列出文件名 + 预览原图供核对（尚未检测）。"""
        self._items = []; self._cur = -1
        self.lst.blockSignals(True); self.lst.clear()
        for p in self._paths:
            self.lst.addItem("○ 未检测  —  " + os.path.basename(str(p)))
        self.lst.blockSignals(False)
        self.table.setRowCount(0)
        self.b_long.setEnabled(False); self.b_wide.setEnabled(False)
        if self._paths:
            self.lst.setCurrentRow(0)   # 触发 _select_image → 预览首张原图

    def _preview_raw(self, path):
        """未检测时：直接显示原图供核对（载入失败明确报错）。"""
        self.sp_cur.setEnabled(False); self.b_redetect.setEnabled(False)
        try:
            _arr, pix, _pol = load_signal_and_pixmap(path)
        except Exception as ex:
            self.view.clear(); self.lbl_cur.setText("⚠ 打不开/已损坏：%s" % ex); return
        self.view.set_image(pix); self.view.viewport().update()
        self.lbl_cur.setText("原图核对（未检测）· %s" % os.path.basename(str(path)))

    # ---- 批量检测 ----
    def run(self):
        if not getattr(self, "_paths", None) or self.b_run.text().endswith("…"):
            return
        import wb_batch
        n = int(self.sp_n.value()) or None
        self.b_run.setEnabled(False); self.b_long.setEnabled(False); self.b_wide.setEnabled(False)
        self.b_run.setText("运行中…"); QtWidgets.QApplication.processEvents()
        items = []
        for i, path in enumerate(self._paths):
            try:
                self.status.setText("检测中 %d/%d：%s" % (i + 1, len(self._paths), os.path.basename(path)))
                QtWidgets.QApplication.processEvents()
            except RuntimeError:
                return
            items.append(self._analyze_path(path, n))
        try:
            self.b_run.setText("运行批量"); self.b_run.setEnabled(True)
        except RuntimeError:
            return
        self._items = items; self._cur = -1
        self._fill_list(); self._fill_table()
        if items:
            self.lst.setCurrentRow(0)
        ok = sum(1 for it in items if it["ok"])
        self.b_long.setEnabled(bool(items)); self.b_wide.setEnabled(any(it["boxes"] for it in items))
        self.status.setText("完成：%d 张成功 / %d 张失败。逐张核对检测框；不对就拖框或改带数重检测。归一化在 Excel 用宽表。"
                            % (ok, len(items) - ok))

    def _analyze_path(self, path, n):
        import wb_batch
        name = os.path.basename(str(path))
        try:
            arr, pixmap, pol = load_signal_and_pixmap(path)
        except Exception as ex:
            return {"path": path, "name": name, "arr": None, "pol": "", "pixmap": None,
                    "boxes": [], "horiz": True, "n_bands": n, "areas": [], "ok": False, "error": "载入失败:%s" % ex}
        try:
            boxes, horiz, pol2 = wb_batch.detect_band_boxes(arr, pol, n_bands=n)
            areas = wb_batch.boxes_to_areas(arr, boxes, horiz, pol2)
            return {"path": path, "name": name, "arr": arr, "pol": pol2, "pixmap": pixmap,
                    "boxes": boxes, "horiz": horiz, "n_bands": n, "areas": areas,
                    "ok": bool(boxes), "error": "" if boxes else "未检测到条带"}
        except Exception as ex:
            return {"path": path, "name": name, "arr": arr, "pol": pol, "pixmap": pixmap,
                    "boxes": [], "horiz": True, "n_bands": n, "areas": [], "ok": False, "error": "分析失败:%s" % ex}

    # ---- 选中某张图 → 预览 ----
    def _select_image(self, idx):
        if idx < 0:
            return
        if idx >= len(self._items):       # 还没运行批量 → 预览原图核对
            if 0 <= idx < len(getattr(self, "_paths", [])):
                self._cur = idx; self._preview_raw(self._paths[idx])
            return
        self._cur = idx; it = self._items[idx]
        self.sp_cur.setEnabled(True); self.b_redetect.setEnabled(True)
        if it["pixmap"] is not None:
            self.view.set_image(it["pixmap"]); self.view.set_rois(it["boxes"]); self.view.sel = 0 if it["boxes"] else -1
        else:
            self.view.clear()             # 失败图：清空画布，不留上一张的框
        self.view.viewport().update()
        self.sp_cur.blockSignals(True); self.sp_cur.setValue(it["n_bands"] or 0); self.sp_cur.blockSignals(False)
        self.lbl_cur.setText("%s · %d 框（拖角/边缩放、拖框移动、Delete 删；改完自动更新表）" % (it["name"], len(it["boxes"])))
        # 同步表格选中
        self.table.blockSignals(True); self.table.selectRow(idx); self.table.blockSignals(False)

    def _on_table_pick(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            r = rows[0].row()
            if r != self._cur:
                self.lst.setCurrentRow(r)

    # ---- 预览里改了框 → 重算这张图 ----
    def _on_preview_edited(self):
        if not (0 <= self._cur < len(self._items)):
            return
        import wb_batch
        it = self._items[self._cur]
        if it["arr"] is None:             # 失败图无数据：不把残留框写回，避免污染
            return
        it["boxes"] = list(self.view.rois)
        it["areas"] = wb_batch.boxes_to_areas(it["arr"], it["boxes"], it["horiz"], it["pol"])
        it["ok"] = bool(it["boxes"]); it["error"] = "" if it["boxes"] else "无框"
        self.lbl_cur.setText("%s · %d 框" % (it["name"], len(it["boxes"])))
        self._fill_table(); self.table.blockSignals(True); self.table.selectRow(self._cur); self.table.blockSignals(False)
        self.b_wide.setEnabled(any(x["boxes"] for x in self._items))

    def _redetect_current(self):
        if not (0 <= self._cur < len(self._items)):
            return
        import wb_batch
        it = self._items[self._cur]
        if it["arr"] is None:
            return
        n = int(self.sp_cur.value()) or None
        it["n_bands"] = n
        boxes, horiz, pol = wb_batch.detect_band_boxes(it["arr"], it["pol"] or "auto", n_bands=n)
        it["boxes"] = boxes; it["horiz"] = horiz; it["pol"] = pol
        it["areas"] = wb_batch.boxes_to_areas(it["arr"], boxes, horiz, pol)
        it["ok"] = bool(boxes); it["error"] = "" if boxes else "未检测到条带"
        self.view.set_rois(boxes); self.view.sel = 0 if boxes else -1; self.view.viewport().update()
        self.lbl_cur.setText("%s · %d 框（重检测）" % (it["name"], len(boxes)))
        self._fill_table(); self.table.blockSignals(True); self.table.selectRow(self._cur); self.table.blockSignals(False)

    # ---- 列表 / 汇总表 ----
    def _fill_list(self):
        self.lst.blockSignals(True); self.lst.clear()
        for it in self._items:
            mark = "✓" if it["ok"] else "✗"
            item = QtWidgets.QListWidgetItem("%s %s（%d 带）" % (mark, it["name"], len(it["boxes"])))
            if not it["ok"]:
                item.setForeground(QtGui.QColor("#e8913c"))
            self.lst.addItem(item)
        self.lst.blockSignals(False)

    def _fill_table(self):
        maxb = max((len(it["boxes"]) for it in self._items), default=0)
        cols = ["图片", "状态"] + ["Band%d" % (i + 1) for i in range(maxb)]
        self.table.setColumnCount(len(cols)); self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setRowCount(len(self._items))
        for row, it in enumerate(self._items):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(it["name"]))
            st = "✓ %d 带" % len(it["boxes"]) if it["ok"] else ("✗ " + it["error"])
            cst = _table_item(st)
            if not it["ok"]:
                cst.setForeground(QtGui.QColor("#e8913c"))
            self.table.setItem(row, 1, cst)
            for i in range(maxb):
                txt = "%.1f" % it["areas"][i] if i < len(it["areas"]) else ""
                c = _table_item(txt, numeric=True, key=bool(txt))
                self.table.setItem(row, 2 + i, c)
        # 列表带数同步刷新
        self._refresh_list_counts()

    def _refresh_list_counts(self):
        for row, it in enumerate(self._items):
            li = self.lst.item(row)
            if li is not None:
                li.setText("%s %s（%d 带）" % ("✓" if it["ok"] else "✗", it["name"], len(it["boxes"])))

    def _results_for_export(self):
        res = []
        for it in self._items:
            bands = [{"band": i + 1, "area": float(a), "peak": 0, "left": 0, "right": 0, "width": 0}
                     for i, a in enumerate(it["areas"])]
            res.append({"path": it["path"], "name": it["name"], "ok": it["ok"], "error": it["error"],
                        "lane_rect": None, "polarity": it["pol"], "bands": bands})
        return res

    def export(self, kind):
        if not self._items:
            return
        import wb_batch
        default = "wb_batch_%s.csv" % kind
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 %s 表 CSV" % kind, _start_path("wb_csv", default), "CSV (*.csv)")
        if not path:
            return
        _remember_dir("wb_csv", path)
        try:
            res = self._results_for_export()
            (wb_batch.export_long_csv if kind == "long" else wb_batch.export_wide_csv)(res, path)
            self.status.setText("已导出 %s 表 → %s（已含你逐张核对/调整后的结果）" % (kind, path))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))
