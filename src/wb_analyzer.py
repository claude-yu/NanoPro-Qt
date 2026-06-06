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
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import wb_quant as wb
import theme


# ---------- 读图（ImageJ 口径）----------
def load_signal_and_pixmap(path):
    """返回 (measure_array float, display_pixmap, polarity_auto)。
    measure_array：调色板图=原始索引（与 ImageJ 测量一致），其余=感知灰度。display=人眼可见 RGB。"""
    from PIL import Image
    im = Image.open(path)
    if im.mode == "P":
        arr = np.asarray(im).astype(np.float64)          # 原始索引，ImageJ 同口径
    elif im.mode in ("I", "I;16", "F"):
        arr = np.asarray(im).astype(np.float64)
    else:
        arr = wb.to_gray(np.asarray(im.convert("RGB")))  # 感知灰度
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
        self.sel = -1
        self._drag0 = None
        self._rubber = None

    def set_image(self, pixmap: QtGui.QPixmap):
        self.scene().clear()
        self._pix_item = self.scene().addPixmap(pixmap)
        self.scene().setSceneRect(QtCore.QRectF(pixmap.rect()))
        self.rois.clear(); self.sel = -1
        self.fitInView(self.scene().sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.viewport().update()

    def set_rois(self, rois):
        self.rois = [tuple(int(v) for v in r) for r in rois]
        self.sel = -1
        self.viewport().update()

    # 缩放：滚轮缩放（以光标为中心）
    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(f, f)

    def mousePressEvent(self, e):
        if self._pix_item is None or e.button() != QtCore.Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        sp = self.mapToScene(e.position().toPoint())
        # 命中已有 ROI → 选中（不新建）
        for i, (x0, y0, x1, y1) in enumerate(self.rois):
            if x0 <= sp.x() <= x1 and y0 <= sp.y() <= y1:
                self.sel = i; self.roiPicked.emit(i); self.viewport().update(); return
        self._drag0 = sp
        self._rubber = QtCore.QRectF(sp, sp)

    def mouseMoveEvent(self, e):
        if self._drag0 is not None:
            sp = self.mapToScene(e.position().toPoint())
            self._rubber = QtCore.QRectF(self._drag0, sp).normalized()
            self.viewport().update()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag0 is not None and self._rubber is not None:
            r = self._rubber.normalized()
            br = self.scene().sceneRect()
            x0 = int(max(br.left(), min(r.left(), br.right())))
            y0 = int(max(br.top(), min(r.top(), br.bottom())))
            x1 = int(max(br.left(), min(r.right(), br.right())))
            y1 = int(max(br.top(), min(r.bottom(), br.bottom())))
            if x1 - x0 >= 3 and y1 - y0 >= 3:
                self.rois.append((x0, y0, x1, y1))
                self.sel = len(self.rois) - 1
                self.roiAdded.emit()
            self._drag0 = None; self._rubber = None
            self.viewport().update()
        else:
            super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace) and 0 <= self.sel < len(self.rois):
            self.rois.pop(self.sel); self.sel = -1
            self.roiAdded.emit(); self.viewport().update(); return
        super().keyPressEvent(e)

    def drawForeground(self, painter: QtGui.QPainter, rect):
        painter.save()
        c = theme.colors()
        for i, (x0, y0, x1, y1) in enumerate(self.rois):
            sel = (i == self.sel)
            pen = QtGui.QPen(QtGui.QColor(c["accent"] if sel else "#27c08a"), 0)
            pen.setCosmetic(True); pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(26, 138, 255, 40) if sel else QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
            # 序号标签
            painter.setPen(QtGui.QColor("#ffffff"))
            f = painter.font(); f.setPointSize(9); f.setBold(True); painter.setFont(f)
            ly = (y0 - 3) if y0 > 12 else (y0 + 13)  # 贴顶时标签画到框内，避免被场景裁掉
            painter.drawText(QtCore.QPointF(x0 + 2, ly), str(i + 1))
        if self._rubber is not None:
            pen = QtGui.QPen(QtGui.QColor(c["accent"]), 0); pen.setCosmetic(True)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine); pen.setWidthF(1.5)
            painter.setPen(pen); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(self._rubber)
        painter.restore()


# ---------- 泳道密度曲线（QPainter 自绘）----------
class ProfilePlot(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self._prof = np.zeros(0)
        self._base = 0.0
        self._title = ""

    def set_profile(self, prof, baseline=0.0, title=""):
        self._prof = np.asarray(prof, np.float64)
        self._base = float(baseline)
        self._title = title
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        c = theme.colors()
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(c["base"]))
        m = 10
        prof = self._prof
        if prof.size < 2:
            p.setPen(QtGui.QColor(c["muted"]))
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "选中一个 ROI 查看泳道密度曲线")
            p.end(); return
        pmin, pmax = float(prof.min()), float(prof.max())
        span = (pmax - pmin) or 1.0
        n = prof.size
        def X(i): return m + (W - 2 * m) * i / (n - 1)
        def Y(v): return H - m - (H - 2 * m) * (v - pmin) / span
        # 基线
        yb = Y(self._base)
        p.setPen(QtGui.QPen(QtGui.QColor(c["muted"]), 1, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(QtCore.QPointF(m, yb), QtCore.QPointF(W - m, yb))
        # 峰面积填充（曲线与基线间 >0 部分）
        fill = QtGui.QPainterPath(); fill.moveTo(X(0), yb)
        for i in range(n):
            fill.lineTo(X(i), Y(max(prof[i], self._base)))
        fill.lineTo(X(n - 1), yb); fill.closeSubpath()
        p.fillPath(fill, QtGui.QColor(26, 138, 255, 70))
        # 曲线
        path = QtGui.QPainterPath(); path.moveTo(X(0), Y(prof[0]))
        for i in range(1, n):
            path.lineTo(X(i), Y(prof[i]))
        p.setPen(QtGui.QPen(QtGui.QColor(c["accent"]), 1.6)); p.drawPath(path)
        if self._title:
            p.setPen(QtGui.QColor(c["muted"]))
            p.drawText(QtCore.QRectF(m, 2, W - 2 * m, 16), QtCore.Qt.AlignmentFlag.AlignLeft, self._title)
        p.end()


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
        self._control = -1      # 内参 ROI 行
        self._method = "box"    # box=框带IntDen / lane=泳道峰面积
        self._results = []      # 每 ROI dict
        self._build_ui()

    # ---- UI ----
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(6)

        # 顶栏
        bar = QtWidgets.QHBoxLayout(); bar.setSpacing(6)
        b_open = QtWidgets.QPushButton("载入图片…"); b_open.clicked.connect(self.open_file)
        b_layer = QtWidgets.QPushButton("用当前图层"); b_layer.clicked.connect(self.use_active_layer)
        bar.addWidget(b_open); bar.addWidget(b_layer)
        bar.addSpacing(8)
        bar.addWidget(QtWidgets.QLabel("极性"))
        self.cb_pol = QtWidgets.QComboBox()
        self.cb_pol.addItems(["自动", "暗带/浅底", "亮带/深底"])
        self.cb_pol.currentIndexChanged.connect(self._on_polarity_changed)
        bar.addWidget(self.cb_pol)
        bar.addWidget(QtWidgets.QLabel("背景"))
        self.cb_bg = QtWidgets.QComboBox()
        self.cb_bg.addItems(["无", "环形(本地)", "Rolling-ball(全局)"])
        self.cb_bg.currentIndexChanged.connect(lambda *_: self.measure())
        bar.addWidget(self.cb_bg)
        self.sp_ring = QtWidgets.QSpinBox(); self.sp_ring.setRange(2, 60); self.sp_ring.setValue(10)
        self.sp_ring.setPrefix("环 "); self.sp_ring.setSuffix(" px"); self.sp_ring.setToolTip("环形背景宽度")
        self.sp_ring.valueChanged.connect(lambda *_: self.measure())
        bar.addWidget(self.sp_ring)
        bar.addStretch(1)
        root.addLayout(bar)

        # 第二行：动作（含 ImageJ 键提示）
        bar2 = QtWidgets.QHBoxLayout(); bar2.setSpacing(6)
        for txt, cb, tip in (
            ("自动检测泳道", self.auto_detect, "竖直投影自动分泳道并定带，给初值（仍可手动改框）"),
            ("测量 (3)", self.measure, "对所有 ROI 测量 → 表格+曲线（ImageJ Plot Lanes）"),
            ("清空 ROI", self.clear_rois, "删除所有选区"),
        ):
            b = QtWidgets.QPushButton(txt); b.setToolTip(tip); b.clicked.connect(cb); bar2.addWidget(b)
        self.b_method = QtWidgets.QPushButton("量化法：框带 IntDen (A)")
        self.b_method.setToolTip("A 切换：框带 IntDen（默认，对齐 ImageJ）/ 泳道曲线峰面积")
        self.b_method.clicked.connect(self.toggle_method)
        bar2.addWidget(self.b_method)
        bar2.addStretch(1)
        self.b_csv = QtWidgets.QPushButton("导出 CSV"); self.b_csv.clicked.connect(self.export_csv)
        bar2.addWidget(self.b_csv)
        root.addLayout(bar2)

        # 主体：左图右(表+曲线)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.view = ROIView()
        self.view.roiAdded.connect(self.measure)
        self.view.roiPicked.connect(self._on_roi_picked)
        split.addWidget(self.view)

        right = QtWidgets.QWidget(); rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(6)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._on_table_sel)
        rv.addWidget(self.table, 1)
        # 内参选择
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("内参(归一化基准)"))
        self.cb_ctrl = QtWidgets.QComboBox(); self.cb_ctrl.addItem("无")
        self.cb_ctrl.currentIndexChanged.connect(self._on_control_changed)
        crow.addWidget(self.cb_ctrl); crow.addStretch(1)
        rv.addLayout(crow)
        self.plot = ProfilePlot(); rv.addWidget(self.plot)
        split.addWidget(right)
        split.setSizes([620, 420])
        root.addWidget(split, 1)

        self.status = QtWidgets.QLabel("载入一张 WB 图，拖框选泳道/带 → 测量。键：1 框首泳道 · 2 下一泳道 · 3 测量 · 4 重测 · A 切换量化法")
        self.status.setStyleSheet("color:%s;" % theme.colors()["muted"])
        root.addWidget(self.status)

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
            self, "选择 WB 图片", "", "图片 (*.tif *.tiff *.png *.jpg *.jpeg *.bmp)")
        if path:
            self.load_path(path)

    def load_path(self, path):
        try:
            arr, pix, pol = load_signal_and_pixmap(path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "载入失败", str(ex)); return
        self._arr = arr
        self._auto_pol = pol
        self.view.set_image(pix)
        self._apply_polarity()
        self.table.setRowCount(0); self._results = []; self.cb_ctrl.clear(); self.cb_ctrl.addItem("无")
        self.plot.set_profile([])
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
        self._arr = wb.to_gray(a[..., :3])
        self._auto_pol = "light_on_dark" if float(np.median(self._arr)) < 128 else "dark_on_light"
        self.view.set_image(QtGui.QPixmap.fromImage(img))
        self._apply_polarity()
        self.table.setRowCount(0); self._results = []
        self.status.setText("已用当前图层 %dx%d。拖框选泳道/带 → 测量。" % (w, h))

    # ---- 极性 / 量化法 ----
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

    # ---- 自动检测 ----
    def auto_detect(self):
        if self._signal is None:
            return
        base = float(np.median(self._signal))
        net = np.clip(self._signal - base, 0, None)
        lanes = wb.detect_lanes(net, smooth=9, thr_frac=0.25, min_w=10)
        rois = []
        H = net.shape[0]
        for (x0, x1) in lanes:
            bands = wb.detect_bands(net, (x0, 0, x1, H), smooth=7, thr_frac=0.3, min_h=5)
            if bands:
                y0, y1 = max(bands, key=lambda b: b[1] - b[0])
                rois.append((x0, y0, x1, y1))
        if not rois:
            self.status.setText("自动检测未找到泳道/带——请手动拖框，或调整极性。"); return
        self.view.set_rois(rois)
        self.status.setText("自动检测到 %d 条带（初值，可手动微调框）。" % len(rois))
        self.measure()

    def clear_rois(self):
        self.view.set_rois([]); self.table.setRowCount(0); self._results = []
        self.cb_ctrl.clear(); self.cb_ctrl.addItem("无"); self.plot.set_profile([])

    # ---- 测量 ----
    def measure(self):
        if self._signal is None or not self.view.rois:
            self.table.setRowCount(0); self._results = []
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
            results.append({"i": i + 1, "area": r["area"], "mean": r["mean"],
                            "raw": raw, "intden": r["intden"], "peak_area": pa,
                            "value": value, "prof": prof, "base": base, "rect": (x0, y0, x1, y1)})
        self._results = results
        self._refresh_table()
        if self._bg_warning:
            self.status.setText(self._bg_warning)
        if 0 <= self.view.sel < len(results):
            self._show_profile(self.view.sel)

    def _norm_values(self):
        vals = np.array([r["value"] for r in self._results], float)
        if 0 <= self._control < len(vals) and vals[self._control] != 0:
            return vals / vals[self._control]
        return np.full(len(vals), np.nan)

    def _refresh_table(self):
        norms = self._norm_values()
        self.table.blockSignals(True)   # 逐格 setItem 不触发 itemSelectionChanged（防重建中 re-entrant）
        try:
            self.table.setRowCount(len(self._results))
            for row, r in enumerate(self._results):
                cells = [str(r["i"]), str(r["area"]), "%.2f" % r["mean"],
                         "%.0f" % r["raw"],
                         "%.0f" % (r["peak_area"] if self._method == "lane" else r["intden"]),
                         ("%.3f" % norms[row]) if not np.isnan(norms[row]) else "—"]
                for col, txt in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(txt)
                    it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                    if col == 4:
                        f = it.font(); f.setBold(True); it.setFont(f)
                    self.table.setItem(row, col, it)
        finally:
            self.table.blockSignals(False)
        # 内参下拉同步
        cur = self.cb_ctrl.currentIndex()
        self.cb_ctrl.blockSignals(True)
        self.cb_ctrl.clear(); self.cb_ctrl.addItem("无")
        for r in self._results:
            self.cb_ctrl.addItem("ROI %d" % r["i"])
        self.cb_ctrl.setCurrentIndex(cur if 0 <= cur <= len(self._results) else 0)
        self.cb_ctrl.blockSignals(False)
        unit = "泳道峰面积" if self._method == "lane" else "框带 IntDen"
        bgname = ["无", "环形", "Rolling-ball"][self.cb_bg.currentIndex()]
        self.status.setText("测量 %d 个 ROI · 量化法=%s · 背景=%s · 极性=%s"
                            % (len(self._results), unit, bgname,
                               "暗带/浅底" if self._polarity == "dark_on_light" else "亮带/深底"))

    # ---- 选中联动 ----
    def _on_roi_picked(self, i):
        self.table.blockSignals(True)   # 视图点选已设 view.sel；编程式选表行不再回灌 _on_table_sel
        try:
            if 0 <= i < self.table.rowCount():
                self.table.selectRow(i)
        finally:
            self.table.blockSignals(False)
        self._show_profile(i)

    def _on_table_sel(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            i = rows[0].row()
            self.view.sel = i; self.view.viewport().update()
            self._show_profile(i)

    def _show_profile(self, i):
        if 0 <= i < len(self._results):
            r = self._results[i]
            self.plot.set_profile(r["prof"], r["base"],
                                  "ROI %d 泳道密度曲线（峰面积=%.0f）" % (r["i"], r["peak_area"]))

    def _on_control_changed(self, idx):
        self._control = idx - 1
        self._refresh_table()

    # ---- 导出 ----
    def export_csv(self):
        if not self._results:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 CSV", "wb_quant.csv", "CSV (*.csv)")
        if not path:
            return
        norms = self._norm_values()
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["ROI", "x0", "y0", "x1", "y1", "Area", "Mean", "RawIntDen",
                            "IntDen", "PeakArea", "Value", "Normalized",
                            "method", "background", "polarity"])
                unit = "lane_peak_area" if self._method == "lane" else "box_intden"
                bgname = ["none", "ring", "rolling_ball"][self.cb_bg.currentIndex()]
                for row, r in enumerate(self._results):
                    x0, y0, x1, y1 = r["rect"]
                    w.writerow([r["i"], x0, y0, x1, y1, r["area"], "%.4f" % r["mean"],
                                "%.1f" % r["raw"], "%.1f" % r["intden"], "%.1f" % r["peak_area"],
                                "%.1f" % r["value"],
                                ("%.4f" % norms[row]) if not np.isnan(norms[row]) else "",
                                unit, bgname, self._polarity])
            self.status.setText("已导出 %d 行 → %s" % (len(self._results), path))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "导出失败", str(ex))
