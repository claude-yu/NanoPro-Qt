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
    """QImage → (H,W,3) RGB，透明像素【合成到白底】（不能直接丢 alpha：透明像素 RGB 多为 0=黑，
    会让透明图层/抠图素材描摹出黑背景，用户反馈）。透明→白 → 下游描摹按近白当背景正确处理。"""
    q = qimg.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    w, h = q.width(), q.height()
    arr = np.frombuffer(q.constBits(), np.uint8).reshape(h, w, 4)
    rgb = arr[..., :3].astype(np.float32)
    a = arr[..., 3:4].astype(np.float32) / 255.0
    comp = rgb * a + 255.0 * (1.0 - a)        # 在白底上 alpha 合成（透明→白）
    return np.ascontiguousarray(np.clip(comp, 0, 255).astype(np.uint8))


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

    PREVIEW_DIM = 1400  # 预览缩放档长边。480 太低→白底图下采样把图标色混进白底=预览发白褪色(误导,实际全分辨率应用是鲜艳的)；
                        # 提到 1400→典型 1000-2000px 图预览≈全分辨率(所见即所得)，仍后台+防抖 ~400ms 可接受
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
        self._engine = "vtracer"    # 引擎：vtracer(软边渐变保真) / crisp(自研三层硬边,扁平图标锐利)；由预设设定
        self._text_remove = False   # 描摹前删文字：开关
        self._text_boxes = []       # MSER 检测的文字行框 [(x0,y0,x1,y1)]（图像坐标），点框可删误判
        self._text_map = None       # (scale, ox, oy) 预览 QLabel→图像坐标映射（点框删用）
        self._draw_rect = None      # 拖框补标文字中的临时框（图像坐标）
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(320)
        self._debounce.timeout.connect(lambda: self._request("preview"))
        self._build_ui()
        self.preview.installEventFilter(self)   # 点预览红框删文字框
        self._apply_preset(0)                   # 开面板即用「清晰」预设(自动色+杂色16,保真不褪色)，不让用户落在旧的受限默认

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)  # 对齐 WB/IHC/AI 面板 6px 节奏

        # 左：预览 + 视图模式
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)
        self.preview = QtWidgets.QLabel("先「用当前图层」或「载入图片…」")
        self.preview.setObjectName("traceImageView")  # 走 theme.py 统一样式，与 WB/IHC 量化区一致(surface_sunken/hairline/2px)
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 420)
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
        self.lbl_src.setObjectName("optionLabel")  # muted 11px（走 theme，切主题自动重着色）
        right.addWidget(self.lbl_src)

        # 适用范围提示：描摹擅长扁平图标/线稿；3D 写实图描了会变扁平发糊，该走抠图。
        tip = QtWidgets.QLabel("💡 描摹擅长扁平图标/线稿(锐利·可改色改节点)。\n"
                               "3D 写实图(球棍/渐变/照片)描了会变扁平发糊 → 请改用「抠图/拆解」保真出 PNG。")
        tip.setWordWrap(True)
        tip.setObjectName("optionLabel")  # muted 11px（走 theme）
        right.addWidget(tip)

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
        self.cmb_palette.setCurrentIndex(0)  # 默认自动：保真不褪色（v1.18 实测推翻"受限"——受限调板在白底为主的图上把
                                             # 24 色预算耗在白/浅背景→饱和的图标色被映射成苍白均值=发白褪色）
        self.cmb_palette.setToolTip("自动=引擎自适应分层(全色保真·扁平图首选)；受限=量化到下方「颜色数」(白底图易发白褪色,慎用)")
        form.addRow("调板", self.cmb_palette)

        self._colors_label = QtWidgets.QLabel("颜色数")
        self.sld_colors, colors_box = self._slider(0, 64, 24, "受限调板下=量化到精确色数；黑白模式下=二值阈值(0-255)")
        form.addRow(self._colors_label, colors_box)

        self.sld_paths, paths_box = self._slider(0, 100, 50, "高=更贴合原图/更多锚点；低=更简化")
        form.addRow("路径", paths_box)

        self.sld_corners, corners_box = self._slider(0, 100, 50, "高=更多尖角；低=更平滑")
        form.addRow("边角", corners_box)

        self.sld_noise, noise_box = self._slider(0, 100, 6, "去除小于该像素面积的杂色斑点。默认6（太大会把细描边/箭头/字当噪点删掉=糊；要更干净少路径可调大，要找回细线调小）")
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

        # 输出选项：4 复选 + 忽略色 收进「输出选项」分组框（QGroupBox 走 theme.py token，深浅主题自动跟随）
        self.chk_snap = QtWidgets.QCheckBox("将曲线与线条对齐")
        self.chk_snap.setToolTip("把近水平/垂直的线段吸附成正交直线（坐标轴/直边更整洁）")
        self.chk_transparent = QtWidgets.QCheckBox("透明度（忽略白底）")
        self.chk_transparent.setToolTip("丢弃近白底色块 → 透明背景，便于叠加到其它图层")
        self.chk_group = QtWidgets.QCheckBox("描完打组")
        self.chk_group.setToolTip("勾上：描摹结果包成一个组（整体移动/管理）；不勾(默认)：每个图形元素是独立对象，\n应用后可直接逐个拖动调整，无需先撤组。")
        self.chk_group.setChecked(False)   # 默认不打组 → 描完每个元素直接可拖（用户要的所见即所得编辑）
        self.chk_group_elem = QtWidgets.QCheckBox("逐素材分组（单击拖整个素材）")
        self.chk_group_elem.setToolTip("勾上(默认)：把同一素材的多条颜色路径按空间连通域聚成一组 → 单击拖动=整个素材(图标/箭头)，\n而非单条颜色块；满画布底色自动识别为背景组并锁定。不勾：每条颜色路径独立。")
        self.chk_group_elem.setChecked(True)
        self.btn_ignore = QtWidgets.QPushButton("忽略色…")
        self.btn_ignore.setToolTip("吸取/选一个要忽略的颜色（描摹时不为该色生成路径）；再次点同色可清除")
        self.btn_ignore.clicked.connect(self._pick_ignore_color)
        opt_box = QtWidgets.QGroupBox("输出选项")
        opt_grid = QtWidgets.QGridLayout(opt_box)
        opt_grid.setContentsMargins(8, 4, 8, 6); opt_grid.setSpacing(6)
        opt_grid.addWidget(self.chk_group_elem, 0, 0)
        opt_grid.addWidget(self.chk_group, 0, 1)
        opt_grid.addWidget(self.chk_snap, 1, 0)
        trow = QtWidgets.QHBoxLayout(); trow.setContentsMargins(0, 0, 0, 0); trow.setSpacing(6)
        trow.addWidget(self.chk_transparent); trow.addWidget(self.btn_ignore)
        opt_grid.addLayout(trow, 1, 1)
        right.addWidget(opt_box)

        # 参数变化 → debounce 预览
        for w in (self.cmb_mode, self.cmb_palette, self.cmb_method, self.cmb_curve, self.cmb_create):
            w.currentIndexChanged.connect(self._on_param_change)
        for s in (self.sld_colors, self.sld_paths, self.sld_corners, self.sld_noise):
            s.valueChanged.connect(self._on_param_change)
        self.spin_stroke.valueChanged.connect(self._on_param_change)
        self.chk_snap.toggled.connect(self._on_param_change)
        self.chk_transparent.toggled.connect(self._on_param_change)
        # 「描完打组」只在应用时影响产物(是否包 <g>)，不改预览渲染 → 不触发重预览
        # 模式/调板/创建 改变 → 同步控件可用性与「颜色数↔阈值」语义
        self.cmb_mode.currentIndexChanged.connect(self._sync_controls)
        self.cmb_palette.currentIndexChanged.connect(self._sync_controls)
        self.cmb_create.currentIndexChanged.connect(self._sync_controls)
        self._sync_controls()

        right.addStretch(1)
        self.status = QtWidgets.QLabel("就绪。")
        self.status.setObjectName("traceStatus")  # 走 theme QLabel#traceStatus（正常=muted / [warn]=ihc_accent），深浅主题自动跟随
        self.status.setWordWrap(True)
        right.addWidget(self.status)

        # 描摹前删文字：检测文字行 → 红框给用户看 → 点框删误判 → 描摹时 inpaint 抹除（守住"每个素材都能描边"）
        trow = QtWidgets.QHBoxLayout(); trow.setSpacing(6)
        self.btn_detect = QtWidgets.QPushButton("检测文字")
        self.btn_detect.setToolTip("MSER 检测横排文字行并画红框；点框删误判；勾「描摹前删文字」后描摹会 inpaint 抹掉这些框\n（孤立小素材不成行→不框→照常描；删错点框移除即可，不动源数据可逆）")
        self.btn_detect.clicked.connect(self._detect_text)
        self.chk_rmtext = QtWidgets.QCheckBox("描摹前删文字")
        self.chk_rmtext.setToolTip("勾上：描摹会先把确认的文字框 inpaint 抹掉再描，结果不含糊文字。需先「检测文字」并核对红框。")
        self.chk_rmtext.toggled.connect(self._on_rmtext_toggled)
        self.btn_clrtext = QtWidgets.QPushButton("清空框")
        self.btn_clrtext.setToolTip("清掉所有文字框（不删文字）")
        self.btn_clrtext.clicked.connect(self._clear_text)
        trow.addWidget(self.btn_detect); trow.addWidget(self.chk_rmtext); trow.addWidget(self.btn_clrtext); trow.addStretch(1)
        right.addLayout(trow)

        self.btn_apply = QtWidgets.QPushButton("应用为矢量层（全分辨率）")
        self.btn_apply.setProperty("primary", True)  # 主 CTA → 走 theme QPushButton[primary]，对齐 WB/IHC/AI 面板
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
    # 锐利标定(2026-06-10 ultracode 5-agent 研究+实测裁决,推翻杂色16)：糊的头号根因=杂色太大(16)在描摹前把细描边/
    # 箭头/字形当噪点删了——admet 实测暗线稿恢复率 杂色16=33% → 杂色6=63%。锐利档=【杂色6+多边形(polygon 硬边·SVG小10倍·
    # 拖动不卡)+相邻(abutting 消层间软缝)】。杂色6 路径会多(~960,正常,保细节的代价);polygon 锚点/体积反而最小。
    PRESETS = [
        ("AI锐利", "AI级锐利·平滑曲线（外部 potrace 逐色：全局曲线优化+自适应角点，对标 Illustrator 图像描摹——95%贝塞尔曲线、暗线恢复0.94、渐变填充平滑无发花、孔洞正确、文字可读·扁平 BioRender 图标首选）", dict(engine="potrace", mode=0, palette=0, colors=20, noise=6, paths=50, corners=85, curve=1, method=1, create=0)),
        ("快速硬边", "快速硬边（自研三层 cv2，纯多边形·不需外部引擎·0.2s 快，但渐变区会发花·锚点多）", dict(engine="crisp", mode=0, palette=0, colors=24, noise=6, paths=50, corners=85, curve=1, method=1, create=0)),
        ("平滑", "平滑高保真（vtracer 自动色+样条曲线，边缘圆润·色彩最饱满·保渐变细节，适合柔和阴影/渐变渲染图/照片）", dict(engine="vtracer", mode=0, palette=0, colors=0, noise=4, paths=60, corners=50, curve=0, method=0, create=0)),
        ("低色", "低色简化（受限 8 色，海报扁平风）", dict(mode=0, palette=1, colors=8, noise=8, paths=40, corners=60, curve=1, method=1, create=0)),
        ("灰度", "灰度", dict(mode=1, palette=0, colors=0, noise=6, paths=50, curve=0, create=0)),
        ("黑白", "黑白（二值）", dict(mode=2, paths=50, curve=0, create=0)),
        ("线稿", "线稿轮廓（黑白描边，自动走自研引擎）", dict(mode=2, paths=60, curve=0, create=1)),
    ]

    def _preset_bar(self):
        w = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(w)
        grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(6)  # 两行布局 → 中文预设名宽度翻倍可读、不挤不截断
        for i, (name, tip, _cfg) in enumerate(self.PRESETS):
            b = QtWidgets.QToolButton(); b.setText(name)
            b.setToolTip(f"一键预设：{tip}")
            b.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, idx=i: self._apply_preset(idx))
            grid.addWidget(b, i // 4, i % 4)   # 4 列两行（7 个 → 4+3）
        return w

    def _apply_preset(self, idx):
        cfg = self.PRESETS[idx][2]
        ctrls = [self.cmb_mode, self.cmb_palette, self.sld_colors, self.sld_noise,
                 self.sld_paths, self.sld_corners, self.cmb_curve, self.cmb_create, self.cmb_method]
        self._engine = cfg.get("engine", "vtracer")   # 预设指定引擎（crisp=自研硬边 / vtracer=软边渐变保真）
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
            if "method" in cfg: self.cmb_method.setCurrentIndex(cfg["method"])
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
        self._text_boxes = []; self._text_map = None; self._draw_rect = None   # 新图清旧文字框（坐标对不上新图）
        if getattr(self, "chk_rmtext", None) is not None:
            self.chk_rmtext.blockSignals(True); self.chk_rmtext.setChecked(False); self.chk_rmtext.blockSignals(False)
        self._text_remove = False
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
            engine=self._engine,
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
            group=self.chk_group.isChecked(),   # 旧「描完打组」：全包一个 <g>
            group_elements=self.chk_group_elem.isChecked(),  # 逐素材分组（优先于 group）→ 单击拖整素材
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
        src = self._rgb
        if self._text_remove and self._text_boxes:        # 描摹前删文字：inpaint 抹除确认的文字框
            try:
                import image_trace as itr2
                src = itr2.remove_text(self._rgb, self._text_boxes)
            except Exception:  # noqa: BLE001 —— 抹除失败则用原图描，不致崩
                src = self._rgb
        self._worker.set_job(np.ascontiguousarray(src), params, full)
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
        # 引导：用户常以为描完不能改→点魔棒撞栅格提示。明确告知用矢量工具，想用栅格工具可右键栅格化。
        try:
            if hasattr(self.editor, "_toast"):
                self.editor._toast("描摹完成：用[移动]工具点选元素改色/拖动、[锚点]工具改节点；想用魔棒/套索→右键该层「栅格化为像素层」")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 预览 / 状态 ----------------
    def _on_view_change(self, idx):
        self._view_mode = idx
        self._render_view()

    def resizeEvent(self, e):
        # 窗口缩放 → 按新尺寸重渲当前视图（QLabel.setPixmap 不自动重缩；仿 chat_panel）
        super().resizeEvent(e)
        self._render_view()

    def _with_boxes(self, pix):
        """在显示 pixmap 上画文字红框 + 在绘的临时框 + 记录 QLabel→图像坐标映射（供点删/拖框补标）。"""
        self._text_map = None
        if self._rgb is None:
            return pix
        W = self._rgb.shape[1]
        s = pix.width() / float(W) if W else 1.0
        pm = QtGui.QPixmap(pix); p = QtGui.QPainter(pm)
        pen = QtGui.QPen(QtGui.QColor("#ff1744")); pen.setWidth(2); p.setPen(pen)
        p.setBrush(QtGui.QColor(255, 23, 68, 38))
        for (x0, y0, x1, y1) in self._text_boxes:
            p.drawRect(int(x0 * s), int(y0 * s), int((x1 - x0) * s), int((y1 - y0) * s))
        dr = getattr(self, "_draw_rect", None)           # 拖框补标中的临时框（虚线）
        if dr is not None:
            pen2 = QtGui.QPen(QtGui.QColor("#2979ff")); pen2.setWidth(2)
            pen2.setStyle(QtCore.Qt.PenStyle.DashLine); p.setPen(pen2); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            x0, y0, x1, y1 = dr
            p.drawRect(int(min(x0, x1) * s), int(min(y0, y1) * s), int(abs(x1 - x0) * s), int(abs(y1 - y0) * s))
        p.end()
        lw, lh = self.preview.width(), self.preview.height()
        self._text_map = (s, (lw - pm.width()) / 2.0, (lh - pm.height()) / 2.0)
        return pm

    def _render_view(self):
        """按当前视图模式渲染预览：0描摹结果/1带轮廓/2轮廓/3轮廓带源图/4源图像。"""
        if self._rgb is None:
            return
        fw = max(100, self.preview.width() - 8)
        fh = max(100, self.preview.height() - 8)
        if self._text_remove or self._text_boxes:          # 文字框确认模式：源图 + 红框（点删误判 / 拖框补漏检）
            self.preview.setPixmap(self._with_boxes(_rgb_to_pixmap(self._rgb, fw, fh)))
            return
        self._text_map = None
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

    # ---------------- 描摹前删文字（MSER 检测 → 预览红框 → 点框删误判 → inpaint 抹除）----------------
    def _detect_text(self):
        if self._rgb is None:
            self.status.setText("先载入图片再检测文字。"); return
        try:
            import image_trace as itr2
            self._text_boxes = itr2.detect_text_regions(self._rgb)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"文字检测失败：{e}"); return
        if not self._text_boxes:
            self.status.setText("没检测到成行文字（孤立小素材不会被框/删）。"); return
        self.chk_rmtext.blockSignals(True); self.chk_rmtext.setChecked(True); self.chk_rmtext.blockSignals(False)
        self._text_remove = True
        self._render_view()
        self.status.setText("检测到 %d 个文字行（红框）。点框可删误判；勾「描摹前删文字」已开 → 描摹会 inpaint 抹掉这些框。"
                            % len(self._text_boxes))

    def _clear_text(self):
        self._text_boxes = []; self._text_map = None; self._draw_rect = None
        self.chk_rmtext.blockSignals(True); self.chk_rmtext.setChecked(False); self.chk_rmtext.blockSignals(False)
        self._text_remove = False
        self._render_view()
        self.status.setText("已清空文字框（不删文字）。")

    def _on_rmtext_toggled(self, on):
        self._text_remove = bool(on)
        if on and not self._text_boxes and self._rgb is not None:
            self._detect_text()        # 勾上但还没检测 → 顺手检测
        else:
            self._render_view()

    def _ev_img_xy(self, ev):
        """预览鼠标坐标 → 图像坐标（按 _text_map 映射）。无映射返回 None。"""
        if not self._text_map:
            return None
        s, ox, oy = self._text_map
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        return ((pos.x() - ox) / s, (pos.y() - oy) / s)

    def eventFilter(self, obj, ev):
        # 文字框确认模式：点红框→删该框（误判，可逆）；空白处拖框→补标漏检文字
        if obj is self.preview and (self._text_remove or self._text_boxes):
            et = ev.type()
            if et == QtCore.QEvent.Type.MouseButtonPress:
                xy = self._ev_img_xy(ev)
                if xy is not None:
                    ix, iy = xy
                    for i, (x0, y0, x1, y1) in enumerate(self._text_boxes):
                        if x0 <= ix <= x1 and y0 <= iy <= y1:          # 命中已有框 → 删
                            self._text_boxes.pop(i); self._render_view()
                            self.status.setText("已移除该框（剩 %d 个）。" % len(self._text_boxes)); return True
                    self._draw_rect = (ix, iy, ix, iy)               # 空白处 → 开始拖框
                    return True
            elif et == QtCore.QEvent.Type.MouseMove and getattr(self, "_draw_rect", None):
                xy = self._ev_img_xy(ev)
                if xy is not None:
                    self._draw_rect = (self._draw_rect[0], self._draw_rect[1], xy[0], xy[1]); self._render_view()
                return True
            elif et == QtCore.QEvent.Type.MouseButtonRelease and getattr(self, "_draw_rect", None):
                x0, y0, x1, y1 = self._draw_rect; self._draw_rect = None
                bx = (int(min(x0, x1)), int(min(y0, y1)), int(max(x0, x1)), int(max(y0, y1)))
                if bx[2] - bx[0] >= 4 and bx[3] - bx[1] >= 4:        # 太小的忽略（误点）
                    self._text_boxes.append(bx)
                    self.status.setText("已补标文字框（共 %d 个）。" % len(self._text_boxes))
                self._render_view(); return True
        return super().eventFilter(obj, ev)

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
        if warns:
            self.status.setText(msg + "\n⚠ " + "；".join(warns))
            self.status.setProperty("warn", True)
        else:
            self.status.setText(msg)
            self.status.setProperty("warn", False)
        self.status.style().unpolish(self.status); self.status.style().polish(self.status)  # property 变 → 重着色(走 theme token)

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
