"""image_trace_panel —— 图像描摹（位图→彩色矢量 SVG）侧面板（对齐 Illustrator 图像描摹）。

仿 ihc_analyzer.IHCAnalyzerPanel：参数控件 + 后台线程描摹 + 低分辨率实时预览(debounce) + 「应用为矢量层」。
应用走 editor.import_svg(临时svg) → 复用整条矢量层登记 + 撤销 + SVG/PDF 导出基础设施（image_trace.py 出 SVG）。

线程模型：单常驻 QThread + 任务合并（拖滑块时只跑最后一帧；full 应用优先于 preview）。closeEvent 先 quit/wait
（与 ihc/seg worker 同坑：QThread 销毁仍运行会硬崩）。预览用 max_dim 缩放档秒级反馈，应用用原分辨率。
fail-loud：状态栏报 引擎/路径/色数/耗时 + degraded/banding/empty/diy_drops 警示（铁律②，不静默）。
"""
from __future__ import annotations

import os
import re
import tempfile

import numpy as np
from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

import image_trace as itr
import theme


def _qimage_to_rgb(qimg) -> np.ndarray:
    q = qimg.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    w, h = q.width(), q.height()
    a = np.frombuffer(q.constBits(), np.uint8).reshape(h, w, 4)[..., :3].copy()
    return np.ascontiguousarray(a)


def _svg_to_pixmap(svg: str, fit_w: int, fit_h: int, transparent: bool = False) -> QtGui.QPixmap:
    """SVG 字符串 → QPixmap（按原始尺寸渲染后等比缩放进 fit 框）。transparent=True → 透明底（供叠层）。"""
    r = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    sz = r.defaultSize()
    w, h = max(1, sz.width()), max(1, sz.height())
    img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32)
    img.fill(QtCore.Qt.GlobalColor.transparent if transparent else QtCore.Qt.GlobalColor.white)
    p = QtGui.QPainter(img)
    r.render(p)
    p.end()
    pm = QtGui.QPixmap.fromImage(img)
    if fit_w > 0 and fit_h > 0:
        pm = pm.scaled(fit_w, fit_h, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                       QtCore.Qt.TransformationMode.SmoothTransformation)
    return pm


_OUTLINE_RE = re.compile(r'fill="#[0-9A-Fa-f]{6}"')


def _svg_to_outline(svg: str) -> str:
    """把彩色 SVG 转成线框版（各 path fill=none + 深色细描边），供「轮廓」视图核对锚点贴合。"""
    return _OUTLINE_RE.sub('fill="none" stroke="#16314f" stroke-width="0.6"', svg)


def _overlay(base: QtGui.QPixmap, over: QtGui.QPixmap) -> QtGui.QPixmap:
    """over 透明层叠到 base 上。"""
    res = QtGui.QPixmap(base.size())
    res.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(res)
    p.drawPixmap(0, 0, base)
    p.drawPixmap(0, 0, over)
    p.end()
    return res


def _rgb_to_pixmap(rgb: np.ndarray, fit_w: int, fit_h: int) -> QtGui.QPixmap:
    h, w = rgb.shape[:2]
    img = QtGui.QImage(np.ascontiguousarray(rgb).data, w, h, 3 * w,
                       QtGui.QImage.Format.Format_RGB888).copy()
    pm = QtGui.QPixmap.fromImage(img)
    if fit_w > 0 and fit_h > 0:
        pm = pm.scaled(fit_w, fit_h, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                       QtCore.Qt.TransformationMode.SmoothTransformation)
    return pm


class _TraceWorker(QtCore.QObject):
    done = QtCore.Signal(object, dict, bool)   # svg, stats, is_full
    failed = QtCore.Signal(str, bool)

    def __init__(self):
        super().__init__()
        self._job = None

    def set_job(self, rgb, params, full):
        self._job = (rgb, params, full)

    @QtCore.Slot()
    def run(self):
        job = self._job
        if job is None:
            return
        rgb, params, full = job
        try:
            svg, stats = itr.trace_to_svg(rgb, params)
            self.done.emit(svg, stats, full)
        except Exception as e:  # noqa: BLE001 —— 引擎异常上浮到 UI，不静默
            self.failed.emit(str(e), full)


class ImageTracePanel(QtWidgets.QWidget):
    """单图「图像描摹」：位图 → 彩色矢量，应用为矢量层（可继续编辑/导出 SVG·PDF）。"""

    PREVIEW_DIM = 480   # 预览缩放档长边（秒级反馈）
    _trigger = QtCore.Signal()  # 跨线程触发 worker.run

    def __init__(self, editor=None):
        super().__init__()
        self.editor = editor
        self._rgb = None            # 源 RGB uint8 (H,W,3)
        self._src_name = None
        self._thread = None
        self._worker = None
        self._busy = False
        self._pending = None        # None / 'preview' / 'full'（合并请求，full 优先）
        self._last_svg = None       # 最近一次预览 SVG（供视图模式切换复用）
        self._view_mode = 0         # 视图：0描摹结果/1带轮廓/2轮廓/3轮廓带源图/4源图像
        self._custom_palette = []   # 文档库自定义色板（hex，预留）
        self._ignore_palette = []   # 忽略指定颜色（hex 列表，含吸管取色）
        self._colors_is_threshold = False  # 黑白模式下「颜色数」滑块复用为「阈值」
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(320)
        self._debounce.timeout.connect(lambda: self._request("preview"))
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        tc = theme.colors()
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # 左：预览 + 视图模式
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(4)
        self.preview = QtWidgets.QLabel("先「用当前图层」或「载入图片…」")
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 420)
        self.preview.setStyleSheet(
            f"background:{tc['panel']};border:1px solid {tc['border']};border-radius:4px;color:{tc['muted']};")
        left.addWidget(self.preview, 1)
        vrow = QtWidgets.QHBoxLayout()
        vrow.addWidget(QtWidgets.QLabel("视图"))
        self.cmb_view = QtWidgets.QComboBox()
        self.cmb_view.addItems(["描摹结果", "描摹结果（带轮廓）", "轮廓", "轮廓（带源图像）", "源图像"])
        self.cmb_view.setToolTip("预览显示模式（仅影响核对，不改产物）：填色结果 / 叠路径线框 / 纯线框 / 线框叠原图 / 原始位图")
        self.cmb_view.currentIndexChanged.connect(self._on_view_change)
        vrow.addWidget(self.cmb_view, 1)
        left.addLayout(vrow)
        root.addLayout(left, 1)

        # 右：参数 + 操作
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        right.setContentsMargins(0, 0, 0, 0)

        srow = QtWidgets.QHBoxLayout()
        b_layer = QtWidgets.QPushButton("用当前图层")
        b_layer.setToolTip("把当前活动栅格图层作为描摹源")
        b_layer.clicked.connect(self.use_active_layer)
        b_open = QtWidgets.QPushButton("载入图片…")
        b_open.clicked.connect(self.open_image)
        srow.addWidget(b_layer); srow.addWidget(b_open)
        right.addLayout(srow)
        self.lbl_src = QtWidgets.QLabel("（未载入）")
        self.lbl_src.setStyleSheet(f"color:{tc['muted']};")
        right.addWidget(self.lbl_src)

        # 顶部一键预设条（6 个，对齐 AI 图标条）
        right.addWidget(self._preset_bar())

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        form.setSpacing(6)

        # 引擎不暴露给用户：正常 fill 走 vtracer（质量最好）；描边/线稿 或 vtracer 缺失时引擎侧自动降级 diy。
        # （自研引擎对渐变渲染图会产生大量噪点碎块、又重又卡，不该让用户主动选到。）
        self.cmb_mode = QtWidgets.QComboBox()
        self.cmb_mode.addItems(["彩色", "灰度", "黑白"])
        form.addRow("模式", self.cmb_mode)

        self.cmb_palette = QtWidgets.QComboBox()
        self.cmb_palette.addItems(["自动", "受限"])
        self.cmb_palette.setCurrentIndex(1)  # 默认受限：扁平科研图边缘最干净、路径少不卡（实测，对齐 AI 流程）
        self.cmb_palette.setToolTip("受限=量化到下方「颜色数」(边缘干净·不卡·扁平图首选)；自动=引擎自适应分层(保渐变但路径多)")
        form.addRow("调板", self.cmb_palette)

        self._colors_label = QtWidgets.QLabel("颜色数")
        self.sld_colors, colors_box = self._slider(0, 64, 24, "受限调板下=量化到精确色数；黑白模式下=二值阈值(0-255)")
        form.addRow(self._colors_label, colors_box)

        self.sld_paths, paths_box = self._slider(0, 100, 50, "高=更贴合原图/更多锚点；低=更简化")
        form.addRow("路径", paths_box)

        self.sld_corners, corners_box = self._slider(0, 100, 50, "高=更多尖角；低=更平滑")
        form.addRow("边角", corners_box)

        self.sld_noise, noise_box = self._slider(0, 100, 10, "去除小于该像素面积的杂色斑点（清掉抗锯齿碎边，默认10）")
        form.addRow("杂色", noise_box)

        self.cmb_method = QtWidgets.QComboBox()
        self.cmb_method.addItems(["重叠（堆叠）", "相邻（挖洞）"])
        self.cmb_method.setToolTip("重叠=色块分层堆叠；相邻=边对边挖洞不重叠")
        form.addRow("方法", self.cmb_method)

        self.cmb_curve = QtWidgets.QComboBox()
        self.cmb_curve.addItems(["样条（平滑）", "多边形（尖角）", "像素"])
        form.addRow("曲线", self.cmb_curve)

        self.cmb_create = QtWidgets.QComboBox()
        self.cmb_create.addItems(["填色", "描边（仅自研，vtracer 会降级）"])
        form.addRow("创建", self.cmb_create)

        self.spin_stroke = QtWidgets.QDoubleSpinBox()
        self.spin_stroke.setRange(0.1, 50.0); self.spin_stroke.setValue(1.0); self.spin_stroke.setSingleStep(0.5)
        self.spin_stroke.setToolTip("描边创建模式的描边宽（创建=描边时生效）")
        form.addRow("描边宽", self.spin_stroke)
        right.addLayout(form)

        # 选项复选框（对齐 AI：将曲线与线条对齐 / 透明度）
        self.chk_snap = QtWidgets.QCheckBox("将曲线与线条对齐")
        self.chk_snap.setToolTip("把近水平/垂直的线段吸附成正交直线（坐标轴/直边更整洁）")
        self.chk_transparent = QtWidgets.QCheckBox("透明度（忽略白底）")
        self.chk_transparent.setToolTip("丢弃近白底色块 → 透明背景，便于叠加到其它图层")
        right.addWidget(self.chk_snap)
        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(self.chk_transparent, 1)
        self.btn_ignore = QtWidgets.QPushButton("忽略色…")
        self.btn_ignore.setToolTip("吸取/选一个要忽略的颜色（描摹时不为该色生成路径）；再次点同色可清除")
        self.btn_ignore.clicked.connect(self._pick_ignore_color)
        trow.addWidget(self.btn_ignore)
        right.addLayout(trow)

        # 参数变化 → debounce 预览
        for w in (self.cmb_mode, self.cmb_palette, self.cmb_method, self.cmb_curve, self.cmb_create):
            w.currentIndexChanged.connect(self._on_param_change)
        for s in (self.sld_colors, self.sld_paths, self.sld_corners, self.sld_noise):
            s.valueChanged.connect(self._on_param_change)
        self.spin_stroke.valueChanged.connect(self._on_param_change)
        self.chk_snap.toggled.connect(self._on_param_change)
        self.chk_transparent.toggled.connect(self._on_param_change)
        # 模式/调板/创建 改变 → 同步控件可用性与「颜色数↔阈值」语义
        self.cmb_mode.currentIndexChanged.connect(self._sync_controls)
        self.cmb_palette.currentIndexChanged.connect(self._sync_controls)
        self.cmb_create.currentIndexChanged.connect(self._sync_controls)
        self._sync_controls()

        right.addStretch(1)
        self.status = QtWidgets.QLabel("就绪。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{tc['muted']};")
        right.addWidget(self.status)

        self.btn_apply = QtWidgets.QPushButton("应用为矢量层（全分辨率）")
        self.btn_apply.setToolTip("用原始分辨率重新描摹并登记为可编辑矢量层（可继续改色/改节点，导出 SVG/PDF）")
        self.btn_apply.clicked.connect(lambda: self._request("full"))
        self.btn_apply.setEnabled(False)
        right.addWidget(self.btn_apply)

        wrap = QtWidgets.QWidget()
        wrap.setLayout(right)
        wrap.setFixedWidth(320)
        root.addWidget(wrap)

    def _slider(self, lo, hi, val, tip):
        box = QtWidgets.QHBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        s = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        s.setRange(lo, hi); s.setValue(val); s.setToolTip(tip)
        lab = QtWidgets.QLabel(str(val))
        lab.setFixedWidth(28)
        lab.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        s.valueChanged.connect(lambda v: lab.setText(str(v)))
        s._val_label = lab  # 供 blockSignals 后手动刷新（预设/同步切换时 valueChanged 被屏蔽）
        box.addWidget(s, 1); box.addWidget(lab)
        cont = QtWidgets.QWidget(); cont.setLayout(box)
        return s, cont

    def _refresh_slider_labels(self):
        for s in (self.sld_colors, self.sld_paths, self.sld_corners, self.sld_noise):
            lab = getattr(s, "_val_label", None)
            if lab is not None:
                lab.setText(str(s.value()))

    # 6 个一键预设（对齐 AI 顶部图标条）：(短名, 说明, 一组控件设定)。引擎不在预设里（固定 vtracer，描边自动降级）。
    # 实测：扁平科研图「受限色 + 高杂色」边缘最干净、路径少不卡、对齐 AI 流程 → 设为默认「清晰」。
    PRESETS = [
        ("清晰", "清晰描摹（受限24色+去杂色，边缘干净·路径少不卡·扁平图首选）", dict(mode=0, palette=1, colors=24, noise=10, paths=50, corners=70, curve=0, create=0)),
        ("高保真", "高保真（自动色，保留渐变细节，路径多更重，适合带柔和阴影的渲染图）", dict(mode=0, palette=0, colors=0, noise=4, paths=60, corners=50, curve=0, create=0)),
        ("低色", "低色简化（受限 8 色，海报扁平风）", dict(mode=0, palette=1, colors=8, noise=12, paths=40, corners=60, curve=0, create=0)),
        ("灰度", "灰度", dict(mode=1, palette=0, colors=0, noise=8, paths=50, curve=0, create=0)),
        ("黑白", "黑白（二值）", dict(mode=2, paths=50, curve=0, create=0)),
        ("线稿", "线稿轮廓（黑白描边，自动走自研引擎）", dict(mode=2, paths=60, curve=0, create=1)),
    ]

    def _preset_bar(self):
        w = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0); row.setSpacing(3)
        for i, (name, tip, _cfg) in enumerate(self.PRESETS):
            b = QtWidgets.QToolButton(); b.setText(name)
            b.setToolTip(f"一键预设：{tip}")
            b.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, idx=i: self._apply_preset(idx))
            row.addWidget(b, 1)
        return w

    def _apply_preset(self, idx):
        cfg = self.PRESETS[idx][2]
        ctrls = [self.cmb_mode, self.cmb_palette, self.sld_colors, self.sld_noise,
                 self.sld_paths, self.sld_corners, self.cmb_curve, self.cmb_create]
        for c in ctrls:
            c.blockSignals(True)
        try:
            if "mode" in cfg: self.cmb_mode.setCurrentIndex(cfg["mode"])
            if "palette" in cfg: self.cmb_palette.setCurrentIndex(cfg["palette"])
            if "colors" in cfg: self.sld_colors.setValue(cfg["colors"])
            if "noise" in cfg: self.sld_noise.setValue(cfg["noise"])
            if "paths" in cfg: self.sld_paths.setValue(cfg["paths"])
            if "corners" in cfg: self.sld_corners.setValue(cfg["corners"])
            if "curve" in cfg: self.cmb_curve.setCurrentIndex(cfg["curve"])
            if "create" in cfg: self.cmb_create.setCurrentIndex(cfg["create"])
        finally:
            for c in ctrls:
                c.blockSignals(False)
        self._sync_controls()
        # 复位 colors：_sync_controls 在「黑白阈值态→彩色」过渡时会把滑块重置(0→受限补16)，
        # 抹掉预设显式色数(清晰24/低色8 实得16) → 同步后再写一遍 cfg colors（此时标志/range 已正确）。
        if "colors" in cfg:
            self.sld_colors.blockSignals(True)
            self.sld_colors.setValue(cfg["colors"])
            self.sld_colors.blockSignals(False)
        self._refresh_slider_labels()
        self._request("preview")  # 末尾统一一次预览（避免连发多帧）

    def _sync_controls(self, *_):
        """据 模式/调板/创建 同步控件可用性 + 「颜色数↔阈值」语义（黑白模式滑块复用为二值阈值）。"""
        mode_idx = self.cmb_mode.currentIndex()  # 0彩 1灰 2黑白
        self.sld_colors.blockSignals(True)
        if mode_idx == 2:  # 黑白：颜色数滑块 → 阈值 0–255
            self._colors_label.setText("阈值")
            if not self._colors_is_threshold:
                self.sld_colors.setRange(0, 255); self.sld_colors.setValue(128)
                self._colors_is_threshold = True
            self.sld_colors.setEnabled(True)
            self.cmb_palette.setEnabled(False)
        else:
            self._colors_label.setText("颜色数")
            if self._colors_is_threshold:
                self.sld_colors.setRange(0, 64); self.sld_colors.setValue(0)
                self._colors_is_threshold = False
            self.cmb_palette.setEnabled(True)
            limited = self.cmb_palette.currentIndex() == 1  # 受限
            self.sld_colors.setEnabled(limited)
            if limited and self.sld_colors.value() == 0:
                self.sld_colors.setValue(16)
        self.sld_colors.blockSignals(False)
        self._refresh_slider_labels()  # valueChanged 被 block，手动刷新数值标签
        self.spin_stroke.setEnabled(self.cmb_create.currentIndex() == 1)

    def _pick_ignore_color(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor("#ffffff"), self, "选择要忽略的颜色")
        if not c.isValid():
            return
        hexc = c.name().upper()
        if hexc in self._ignore_palette:
            self._ignore_palette.remove(hexc)
        else:
            self._ignore_palette.append(hexc)
        n = len(self._ignore_palette)
        self.btn_ignore.setText(f"忽略色({n})" if n else "忽略色…")
        self._request("preview")

    # ---------------- 源载入 ----------------
    def use_active_layer(self):
        if self.editor is None:
            return
        layer = getattr(self.editor, "active", None)
        img = layer.get("image") if isinstance(layer, dict) else None
        if img is None or img.isNull():
            QtWidgets.QMessageBox.information(self, "无图层", "没有可用的活动栅格图层。请用「载入图片…」。")
            return
        self._src_name = layer.get("name") if isinstance(layer, dict) else None
        self._set_rgb(_qimage_to_rgb(img))

    def open_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "载入图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)")
        if not path:
            return
        qimg = QtGui.QImage(path)
        if qimg.isNull():
            QtWidgets.QMessageBox.warning(self, "载入失败", f"无法读取：{path}")
            return
        self._src_name = os.path.splitext(os.path.basename(path))[0]
        self._set_rgb(_qimage_to_rgb(qimg))

    def _set_rgb(self, rgb):
        self._rgb = rgb
        self._last_svg = None
        h, w = rgb.shape[:2]
        self.lbl_src.setText(f"{self._src_name or '图片'} · {w}×{h}")
        self.btn_apply.setEnabled(True)
        self._render_view()  # 先显原图（视图为源图像/无 svg 时回退原图）
        self._request("preview")

    # ---------------- 参数 / 请求调度 ----------------
    def _on_param_change(self, *_):
        # 任意参数变化 → debounce 触发预览。描边仅自研支持：选描边 + vtracer 时引擎侧
        # trace_to_svg 自动降级 diy 并写 stats.degraded，经 _show_stats 上浮到状态栏（cmb_create 文案已静态明示）。
        self._debounce.start()

    def _params(self, full: bool) -> itr.TraceParams:
        mode = ["color", "gray", "bw"][self.cmb_mode.currentIndex()]
        n_colors = 0
        bw_threshold = 128
        if mode == "bw":
            bw_threshold = self.sld_colors.value()           # 黑白：滑块=二值阈值
        elif self.cmb_palette.currentIndex() == 1:           # 受限调板：滑块=色数
            n_colors = max(2, self.sld_colors.value())
        # 自动调板(idx0)：n_colors=0
        # 引擎固定 vtracer；create=stroke 时 trace_to_svg 内部自动降级 diy（描边仅 diy 支持）并写 stats.degraded。
        return itr.TraceParams(
            engine="vtracer",
            mode=mode,
            n_colors=n_colors,
            paths=float(self.sld_paths.value()),
            corners=float(self.sld_corners.value()),
            noise=self.sld_noise.value(),
            method="overlapping" if self.cmb_method.currentIndex() == 0 else "abutting",
            curve=["spline", "polygon", "pixel"][self.cmb_curve.currentIndex()],
            create="fill" if self.cmb_create.currentIndex() == 0 else "stroke",
            stroke_width=self.spin_stroke.value(),
            ignore_white=self.chk_transparent.isChecked(),
            ignore_colors=list(self._ignore_palette) if self._ignore_palette else None,
            snap_lines=self.chk_snap.isChecked(),
            bw_threshold=bw_threshold,
            max_dim=0 if full else self.PREVIEW_DIM,
        )

    def _request(self, kind: str):
        if self._rgb is None:
            return
        if self._busy:  # 合并：full 优先于 preview
            self._pending = "full" if (kind == "full" or self._pending == "full") else "preview"
            return
        self._start(self._params(kind == "full"), kind == "full")

    def _ensure_thread(self):
        if self._thread is None:
            self._thread = QtCore.QThread(self)
            self._worker = _TraceWorker()
            self._worker.moveToThread(self._thread)
            self._worker.done.connect(self._on_done)
            self._worker.failed.connect(self._on_failed)
            self._trigger.connect(self._worker.run)
            self._thread.start()

    def _start(self, params, full):
        self._ensure_thread()
        self._busy = True
        self.btn_apply.setEnabled(not full and self._rgb is not None)
        self.status.setText("描摹中…（全分辨率）" if full else "预览中…")
        self._worker.set_job(np.ascontiguousarray(self._rgb), params, full)
        self._trigger.emit()

    # ---------------- 结果回调 ----------------
    @QtCore.Slot(object, dict, bool)
    def _on_done(self, svg, stats, full):
        # _busy 保持到收尾后才复位：_apply_svg→import_svg 内部 fit_view/弹框可能 processEvents，
        # 若此间 debounce 触发 _request 而 _busy 已 False 会重入 _start（重入风险）→ 故先干完活再复位。
        self._show_stats(stats)
        if full:
            self._apply_svg(svg, stats)
        else:
            self._last_svg = svg
            self._render_view()  # 按当前视图模式渲染（描摹结果/带轮廓/轮廓/带源图/源图像）
        self._busy = False
        self.btn_apply.setEnabled(self._rgb is not None)
        # 处理挂起请求（取当前最新参数）
        if self._pending is not None:
            kind = self._pending
            self._pending = None
            self._request(kind)

    @QtCore.Slot(str, bool)
    def _on_failed(self, msg, full):
        self._busy = False
        self.btn_apply.setEnabled(self._rgb is not None)
        self.status.setText(f"描摹失败：{msg}")
        if self._pending is not None:
            kind = self._pending; self._pending = None; self._request(kind)

    def _apply_svg(self, svg, stats):
        if stats.get("empty"):
            QtWidgets.QMessageBox.warning(self, "描摹为空", stats.get("degraded") or "产物 0 路径，请放宽阈值。")
            return
        if self.editor is None or not hasattr(self.editor, "import_svg"):
            self.status.setText("无编辑器，无法应用。")
            return
        # 写临时 SVG → import_svg 登记为矢量层（复用撤销/导出基础设施）
        fd, path = tempfile.mkstemp(suffix=".svg", prefix="trace_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(svg)
            self.editor.import_svg(path)
        except Exception as e:  # noqa: BLE001 —— 写盘/导入失败 fail-loud 报给用户，不让异常裸穿 Qt 槽
            QtWidgets.QMessageBox.warning(self, "应用失败", f"无法应用为矢量层：{e}")
            self.status.setText(f"应用失败：{e}")
            return
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        # 溯源字段指回临时文件（已删）会悬空 → 置 None（产物源是位图描摹，非 SVG 文件）
        try:
            layers = getattr(self.editor, "layers", None)
            if layers and isinstance(layers[-1], dict):
                layers[-1]["svg_path"] = None
        except Exception:  # noqa: BLE001 —— 溯源清理失败不影响应用结果
            pass
        self.status.setText(f"已应用为矢量层 · {stats['engine']} · {stats['n_paths']} 路径。可继续改色/改节点，导出 SVG/PDF。")

    # ---------------- 预览 / 状态 ----------------
    def _on_view_change(self, idx):
        self._view_mode = idx
        self._render_view()

    def resizeEvent(self, e):
        # 窗口缩放 → 按新尺寸重渲当前视图（QLabel.setPixmap 不自动重缩；仿 chat_panel）
        super().resizeEvent(e)
        self._render_view()

    def _render_view(self):
        """按当前视图模式渲染预览：0描摹结果/1带轮廓/2轮廓/3轮廓带源图/4源图像。"""
        if self._rgb is None:
            return
        fw = max(100, self.preview.width() - 8)
        fh = max(100, self.preview.height() - 8)
        m = self._view_mode
        try:
            if m == 4 or (m != 4 and not self._last_svg):  # 源图像 / 尚无描摹结果 → 显原图
                self.preview.setPixmap(_rgb_to_pixmap(self._rgb, fw, fh))
                return
            if m == 0:                                      # 描摹结果（填色）
                self.preview.setPixmap(_svg_to_pixmap(self._last_svg, fw, fh))
            elif m == 1:                                    # 描摹结果（带轮廓）
                base = _svg_to_pixmap(self._last_svg, fw, fh)
                over = _svg_to_pixmap(_svg_to_outline(self._last_svg), fw, fh, transparent=True)
                self.preview.setPixmap(_overlay(base, over))
            elif m == 2:                                    # 纯轮廓线框
                self.preview.setPixmap(_svg_to_pixmap(_svg_to_outline(self._last_svg), fw, fh))
            elif m == 3:                                    # 轮廓叠源图（核对贴合）
                base = _rgb_to_pixmap(self._rgb, fw, fh)
                over = _svg_to_pixmap(_svg_to_outline(self._last_svg), fw, fh, transparent=True)
                self.preview.setPixmap(_overlay(base, over))
        except Exception as e:  # noqa: BLE001 —— 预览渲染失败不致崩面板
            self.status.setText(f"预览渲染失败：{e}")

    def _show_stats(self, stats: dict):
        parts = [stats.get("engine", "?")]
        na = stats.get("n_colors_actual")
        parts.append(f"{na}色" if na else "自动色")
        parts.append(f"{stats.get('n_paths', 0)}路径")
        if stats.get("n_anchors"):
            parts.append(f"{stats['n_anchors']}锚点")
        parts.append(f"{stats.get('elapsed_ms', 0):.0f}ms")
        if stats.get("scale", 1.0) < 1.0:
            parts.append(f"预览@{int(stats['scale'] * 100)}%")
        msg = " · ".join(parts)
        warns = []
        for key in ("degraded", "banding_warn"):
            if stats.get(key):
                warns.append(stats[key])
        if stats.get("empty") and not stats.get("degraded"):  # 空产物独立报（防未来 empty 与 degraded 解耦漏报）
            warns.append("产物 0 路径（请放宽阈值/换引擎）")
        if stats.get("ignored_paths"):
            warns.append(f"忽略色丢弃 {stats['ignored_paths']} 个色块")
        if stats.get("snapped_lines"):
            warns.append(f"对齐直线 {stats['snapped_lines']} 段")
        if stats.get("diy_drops"):
            d = stats["diy_drops"]
            dropped = d.get("dropped_small_contours", 0) + d.get("empty_color_blocks", 0)
            if dropped:
                warns.append(f"自研引擎丢弃 {dropped} 个小色块/斑点")
            spx = d.get("removed_speckle_px", 0)   # 像素量纲单独报（去噪开运算抹掉的）
            if spx:
                warns.append(f"去噪抹除 {spx} 像素")
        tc = theme.colors()
        if warns:
            self.status.setText(msg + "\n⚠ " + "；".join(warns))
            self.status.setStyleSheet(f"color:{tc.get('warn', '#c08a2a')};")
        else:
            self.status.setText(msg)
            self.status.setStyleSheet(f"color:{tc['muted']};")

    # ---------------- 生命周期 ----------------
    def stop_thread(self):
        """停常驻描摹线程（quit + 限时 wait + 兜底 terminate）。

        【必须由宿主显式调用】——本面板是 FloatingToolWindow 的子 widget，浮窗关闭只 hide() 不 close()，
        故 closeEvent 在正常使用中不触发；EditorWindow.closeEvent 退出时对本面板调用此法（仿 _seg_worker，铁律③）。
        wait 限时：全分辨率描摹是不可中断的阻塞计算，无限 wait 会卡死退出 → 限 3s 后 terminate 兜底（纯计算无网络，可接受）。
        """
        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(3000):
                self._thread.terminate(); self._thread.wait(1000)
            self._thread = None; self._worker = None

    def closeEvent(self, e):
        self.stop_thread()  # 直接关本 widget 时也停（兜底；常规关窗走宿主 stop_thread）
        super().closeEvent(e)
