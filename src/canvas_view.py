"""QGraphicsView 画布：滚轮缩放、工具(移动/画笔/橡皮)、中键平移、左上角 FPS 叠加。

性能要点：缩放只动视图变换、拖动只动 item 位置；画笔事件映射到场景坐标后交给窗口在
选中图层的 QImage 上绘制（不复制整幅数组、不走 base64）。重绘频率(paintEvent)=交互 FPS。
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

import icons
import theme


class CanvasView(QtWidgets.QGraphicsView):
    fpsChanged = QtCore.Signal(float)
    zoomChanged = QtCore.Signal(float)
    paintPress = QtCore.Signal(QtCore.QPointF)
    paintMove = QtCore.Signal(QtCore.QPointF)
    paintRelease = QtCore.Signal()
    textClick = QtCore.Signal(QtCore.QPointF)       # 文字工具单击放置（保留）
    textBox = QtCore.Signal(QtCore.QPointF, QtCore.QPointF)  # 文字工具拖框（起点,终点）→ 定宽文本框
    editRequested = QtCore.Signal(QtCore.QPointF)   # 双击（编辑文字层）
    escPressed = QtCore.Signal()                    # Esc 取消选区
    nudge = QtCore.Signal(int, int)                 # 方向键微移当前层
    deleteRequested = QtCore.Signal()               # Del 删除当前层
    extractRequested = QtCore.Signal()              # Enter 抠出选区（提交）
    layerViaCopy = QtCore.Signal()                  # Ctrl+J 图层 via copy（PS：选区→复制到新层 / 无选区→复制当前层）
    assetDropped = QtCore.Signal(QtCore.QPointF, str)  # 素材库拖到画布：(场景坐标, 本地图片路径)
    cursorScene = QtCore.Signal(QtCore.QPointF)     # 鼠标在画布中的当前场景位置（标尺游标线用）
    measureChanged = QtCore.Signal(QtCore.QPointF, QtCore.QPointF)  # 测量工具：起点、终点场景坐标（拉一条线时实时发）
    # B5 钢笔：press(场景点,Alt是否按下) / dragTo(场景点拖动中) / release(场景点松手) / commit(结束) / hover(光标场景点)
    penPress = QtCore.Signal(QtCore.QPointF, bool)
    penDragTo = QtCore.Signal(QtCore.QPointF)
    penRelease = QtCore.Signal(QtCore.QPointF)
    penCommit = QtCore.Signal()                      # Enter/双击结束钢笔
    penHover = QtCore.Signal(QtCore.QPointF)         # 钢笔工具下移动 → 橡皮筋预览
    # B5 锚点：在 path 段上 Alt+单击 / 双击空段 → 加锚（场景点, Alt 是否按下）
    nodeClick = QtCore.Signal(QtCore.QPointF, bool)
    nodeDoubleClick = QtCore.Signal(QtCore.QPointF)
    connectorHover = QtCore.Signal(QtCore.QPointF)  # 连接线工具：悬停场景点 → 显示对象边中点锚点

    # 走 paintPress/Move/Release 的工具：选区(brush/wand/lasso/rect/rectsel) + 拖框(erase/crop) + 像素(draw/eraser)
    _PAINT_TOOLS = ("brush", "draw", "eraser", "wand", "lasso", "rect", "rectsel", "erase", "crop",
                    "sh_rect", "sh_ellipse", "sh_line", "sh_arrow",  # 形状工具走拖框 press/move/release
                    "connector")  # 智能连接线：从对象拖到对象（press/move/release）

    def __init__(self, scene: QtWidgets.QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(
            QtGui.QPainter.RenderHint.Antialiasing
            | QtGui.QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)  # 接收空格键做临时平移
        self._out_color = QtGui.QColor("#2a2e3a")
        self._checker = self._make_checker()
        self.apply_theme()
        self._zoom = 1.0
        self._frames: list[float] = []
        self._fps_hud = False   # FPS 浮标默认关（开发脚手架）；调试菜单可开（审核 MEDIUM）
        self._tool = "move"
        self._panning = False
        self._space = False
        self._text_press = None  # 文字工具拖框起点
        self._pan_last = QtCore.QPoint()
        self._zoom_mode = "in"  # 放大镜 / 缩小镜
        self._zoom_cursor_in = QtGui.QCursor(icons.tool_pixmap("zoom", "#111827", 26), 11, 11)
        self._zoom_cursor_out = QtGui.QCursor(icons.tool_pixmap("zoom_out", "#111827", 26), 11, 11)
        self._cursor_vp = None     # 最近鼠标位置(视口坐标)，画笔光圈用
        self._brush_radius = 0.0   # 画笔/橡皮半径(图像 px)，>0 才画光圈
        self._guides_v: list[float] = []  # 竖直参考线的 scene x
        self._guides_h: list[float] = []  # 水平参考线的 scene y
        self._guides_visible = True
        self._measure_start: QtCore.QPointF | None = None  # 测量线起点(场景坐标)，None=无测量线
        self._measure_end: QtCore.QPointF | None = None    # 测量线终点(场景坐标)
        self.viewport().setMouseTracking(True)  # 无按键也收 mouseMove → 悬停光圈/光标
        self.setAcceptDrops(True)  # 接收素材库/文件管理器拖入的图片 → 在 drop 处建图层

    def set_brush_radius(self, r: float):
        self._brush_radius = max(0.0, float(r))
        if self._tool in ("brush", "draw", "eraser"):
            self.viewport().update()

    def _cursor_for_tool(self):
        if self._tool == "move":
            return QtCore.Qt.CursorShape.ArrowCursor
        if self._tool == "hand":
            return QtCore.Qt.CursorShape.OpenHandCursor
        if self._tool == "zoom":
            return self._zoom_cursor_in if self._zoom_mode == "in" else self._zoom_cursor_out
        if self._tool == "text":
            return QtCore.Qt.CursorShape.IBeamCursor
        return QtCore.Qt.CursorShape.CrossCursor

    def set_zoom_mode(self, mode: str):  # "in"=放大镜 / "out"=缩小镜
        self._zoom_mode = mode
        if self._tool == "zoom":
            self.viewport().setCursor(self._cursor_for_tool())

    # 工具：move=拖动图层项；brush/eraser=在选中图层上绘制；zoom=放大镜；hand/空格=平移。
    # 一律 NoDrag：移动工具下，按住可移动图层项即可拖动（RubberBandDrag 会抢左键拖拽，导致拖不动图层）。
    def set_tool(self, tool: str):
        self._tool = tool
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.viewport().setCursor(self._cursor_for_tool())
        if tool != "measure" and (self._measure_start is not None or self._measure_end is not None):
            self.clear_measure()  # 离开测量工具：只清画布上的测量线显示（数值保留在编辑器侧供拉直用）

    def clear_measure(self):
        self._measure_start = None
        self._measure_end = None
        self.viewport().update()

    @staticmethod
    def _make_checker() -> QtGui.QBrush:
        c = theme.colors()
        cell = 10
        tile = QtGui.QPixmap(cell * 2, cell * 2)
        tile.fill(QtGui.QColor(c["checker_b"]))
        p = QtGui.QPainter(tile)
        p.fillRect(0, 0, cell, cell, QtGui.QColor(c["checker_a"]))
        p.fillRect(cell, cell, cell, cell, QtGui.QColor(c["checker_a"]))
        p.end()
        return QtGui.QBrush(tile)

    def apply_theme(self):
        c = theme.colors()
        self._out_color = QtGui.QColor(c["canvas_out"])
        self._checker = self._make_checker()
        self.setBackgroundBrush(self._out_color)
        self.viewport().update()

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF):
        painter.fillRect(rect, self._out_color)  # 画布外
        sr = self.sceneRect()
        if sr.isValid() and sr.width() > 0:
            painter.fillRect(sr.intersected(rect), self._checker)  # 画布内：棋盘格=透明
            if getattr(self, "_grid_on", False):
                self._draw_grid(painter, sr, rect)

    def _draw_grid(self, painter: QtGui.QPainter, sr: QtCore.QRectF, rect: QtCore.QRectF):
        import math
        g = max(4, int(getattr(self, "_grid_size", 20)))
        area = sr.intersected(rect)
        if area.isEmpty():
            return
        pen = QtGui.QPen(QtGui.QColor(120, 120, 140, 70)); pen.setCosmetic(True); pen.setWidth(0)
        painter.setPen(pen)
        x = math.floor(area.left() / g) * g
        while x <= area.right():
            painter.drawLine(QtCore.QLineF(x, area.top(), x, area.bottom())); x += g
        y = math.floor(area.top() / g) * g
        while y <= area.bottom():
            painter.drawLine(QtCore.QLineF(area.left(), y, area.right(), y)); y += g

    def set_grid(self, on: bool):
        self._grid_on = bool(on); self.viewport().update()

    def set_grid_size(self, px: int):
        self._grid_size = max(4, int(px)); self.viewport().update()

    # —— 参考线（从标尺拖入）：列表存 scene 坐标，drawForeground 直接画 scene 线 ——
    def add_guide(self, orient: str, scene_pos: float):
        (self._guides_v if orient == "v" else self._guides_h).append(float(scene_pos))
        self.viewport().update()

    def clear_guides(self):
        self._guides_v = []
        self._guides_h = []
        self.viewport().update()

    def set_guides_visible(self, on: bool):
        self._guides_visible = bool(on)
        self.viewport().update()

    def drawForeground(self, painter: QtGui.QPainter, rect: QtCore.QRectF):
        super().drawForeground(painter, rect)
        # 常态（无任何参考线/智能吸附线/测量线）每帧只走一个分支即返回，省掉下面的 pen 构造与遍历
        _ed = getattr(self, "_editor", None)
        _ag = getattr(_ed, "_active_guides", None) if _ed is not None else None
        _anchors = getattr(_ed, "_conn_hover_anchors", None) if _ed is not None else None
        if (not (self._guides_visible and (self._guides_v or self._guides_h))
                and not _ag and self._measure_start is None and not _anchors):
            return
        if self._guides_visible:  # 用户参考线（从标尺拖出）：主题描边色细实线
            pen = QtGui.QPen(QtGui.QColor(theme.colors()["outline"]), 0)
            pen.setCosmetic(True)  # 0 宽 cosmetic → 任意缩放都是 1px 细线
            painter.setPen(pen)
            for x in self._guides_v:
                painter.drawLine(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
            for y in self._guides_h:
                painter.drawLine(QtCore.QLineF(rect.left(), y, rect.right(), y))
        # 智能参考线（拖动时实时吸附命中）：洋红虚线，画在命中跨度上（AI/Illustrator 习惯）
        ed = getattr(self, "_editor", None)
        guides = getattr(ed, "_active_guides", None) if ed is not None else None
        if guides:
            pen = QtGui.QPen(QtGui.QColor("#ff00ff"), 0, QtCore.Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            for g in guides:
                a, b = g["span"]
                if g["orient"] == "v":
                    painter.drawLine(QtCore.QLineF(g["pos"], a, g["pos"], b))
                else:
                    painter.drawLine(QtCore.QLineF(a, g["pos"], b, g["pos"]))
        # 测量线（标尺工具）：洋红 cosmetic 实线 + 两端小方块（任意缩放都 1px 细线）
        if self._measure_start is not None and self._measure_end is not None:
            p0, p1 = self._measure_start, self._measure_end
            pen = QtGui.QPen(QtGui.QColor("#ff2d95"), 0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QtCore.QLineF(p0, p1))
            painter.setBrush(QtGui.QColor("#ff2d95"))
            r = 3.0 / max(self._zoom, 1e-6)  # 端点方块在屏幕上恒约 6px
            for pe in (p0, p1):
                painter.drawRect(QtCore.QRectF(pe.x() - r, pe.y() - r, 2 * r, 2 * r))
        # 连接线工具：悬停对象的 4 个边中点锚点（蓝点，BioRender 式 → 让用户看到能连到哪、连在边中心）
        if self._tool == "connector" and _anchors:
            apen = QtGui.QPen(QtGui.QColor("#1a73e8"), 0); apen.setCosmetic(True)
            painter.setPen(apen); painter.setBrush(QtGui.QColor(255, 255, 255))
            ar = 5.0 / max(self._zoom, 1e-6)  # 锚点在屏幕上恒约 10px
            for a in _anchors:
                painter.drawEllipse(a, ar, ar)           # 白心
                painter.setBrush(QtGui.QColor("#1a73e8"))
                painter.drawEllipse(a, ar * 0.5, ar * 0.5)  # 蓝芯
                painter.setBrush(QtGui.QColor(255, 255, 255))

    def current_zoom(self) -> float:
        return self._zoom

    def sync_zoom(self):
        self._zoom = self.transform().m11()

    def _apply_zoom(self, factor: float):
        nz = self._zoom * factor
        if 0.02 <= nz <= 40.0:
            self._zoom = nz
            self.scale(factor, factor)
            self.zoomChanged.emit(self._zoom)

    def wheelEvent(self, e: QtGui.QWheelEvent):
        mods = e.modifiers()
        if mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier):
            self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            self._apply_zoom(1.25 if e.angleDelta().y() > 0 else 1.0 / 1.25)  # Ctrl/Alt+滚轮 = 缩放
            return
        dy = e.angleDelta().y()
        dx = e.angleDelta().x()
        if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:  # Shift+滚轮 = 水平滚动
            hb = self.horizontalScrollBar(); hb.setValue(hb.value() - dy)
        else:  # 普通滚轮 = 上下滚动画布（PS 行为）
            vb = self.verticalScrollBar(); vb.setValue(vb.value() - dy)
            if dx:
                hb = self.horizontalScrollBar(); hb.setValue(hb.value() - dx)

    def _btn_zoom(self, factor: float):  # 按钮缩放以视图中心为锚点（鼠标在按钮上，不能用 AnchorUnderMouse）
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._apply_zoom(factor)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def zoom_in(self):
        self._btn_zoom(1.25)

    def zoom_out(self):
        self._btn_zoom(1.0 / 1.25)

    def zoom_actual(self):
        if self._zoom > 0:
            self._btn_zoom(1.0 / self._zoom)  # 复位到 100%

    def fit(self, rect: QtCore.QRectF):
        self.fitInView(rect, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.sync_zoom()
        self.zoomChanged.emit(self._zoom)

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        left = e.button() == QtCore.Qt.MouseButton.LeftButton
        # 平移：中键，或 抓手工具/按住空格 时左键拖
        if e.button() == QtCore.Qt.MouseButton.MiddleButton or (left and (self._tool == "hand" or self._space)):
            self._panning = True
            self._pan_last = e.position().toPoint()
            self.viewport().setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            return
        if self._tool == "text" and left:
            self._text_press = self.mapToScene(e.position().toPoint())  # 记起点，松手时按拖拽距离决定定宽框/默认框
            return
        if self._tool == "zoom" and e.button() == QtCore.Qt.MouseButton.LeftButton:
            # 放大镜：按当前模式缩放，以点击点为锚；按住 Alt 临时反转（PS 行为）
            self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            zoom_in = (self._zoom_mode == "in")
            if e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier:
                zoom_in = not zoom_in
            self._apply_zoom(1.25 if zoom_in else 1.0 / 1.25)
            return
        if self._tool == "pen" and left:
            alt = bool(e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier)
            self.penPress.emit(self.mapToScene(e.position().toPoint()), alt)
            return  # 钢笔自管事件，不穿透（不触发图层选择/拖动）
        if self._tool == "node" and left:
            # Alt+点 = 在 path 段上加锚（不让位给 handle 拖动）；非 Alt 点交 super
            # （命中 AnchorHandle/CtrlHandle 则其 ItemIsMovable 自处理拖动；点空白命中 path 段不加锚，仅在 Alt 时加）。
            if e.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier:
                self.nodeClick.emit(self.mapToScene(e.position().toPoint()), True)
                return
            super().mousePressEvent(e)
            return
        if self._tool == "measure" and left:
            sp = self.mapToScene(e.position().toPoint())
            self._measure_start = sp; self._measure_end = sp
            self.measureChanged.emit(sp, sp)
            self.viewport().update()
            return  # 不穿透到 super → 不触发图层拖动
        if self._tool in self._PAINT_TOOLS and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.paintPress.emit(self.mapToScene(e.position().toPoint()))
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e: QtGui.QMouseEvent):
        if self._tool in ("move", "text") and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.editRequested.emit(self.mapToScene(e.position().toPoint()))
            return
        if self._tool == "pen" and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.penCommit.emit()  # 双击结束开放路径
            return
        if self._tool == "node" and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.nodeDoubleClick.emit(self.mapToScene(e.position().toPoint()))
            return
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        if e.key() == QtCore.Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = True
            if not self._panning:
                self.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            return
        if e.key() == QtCore.Qt.Key.Key_Escape:
            self.escPressed.emit()
            return
        if e.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
            return
        mods = e.modifiers()
        if e.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and not (mods & QtCore.Qt.KeyboardModifier.ControlModifier):
            if self._tool == "pen":
                self.penCommit.emit(); return  # 钢笔工具下 Enter = 结束路径（不抠选区）
            self.extractRequested.emit(); return  # Enter 抠出选区
        if (mods & QtCore.Qt.KeyboardModifier.ControlModifier) and e.key() == QtCore.Qt.Key.Key_J:
            self.layerViaCopy.emit(); return  # Ctrl+J PS 图层 via copy（选区→复制到新层 / 无选区→复制当前层）
        # Ctrl+C/Ctrl+V 复制粘贴图层；就地编辑文字时让位给文本默认复制粘贴（不抢占）
        ed = getattr(self, "_editor", None)
        sc = self.scene()
        fi = sc.focusItem() if sc is not None else None
        editing_text = (isinstance(fi, QtWidgets.QGraphicsTextItem)
                        and bool(fi.textInteractionFlags()
                                 & QtCore.Qt.TextInteractionFlag.TextEditorInteraction))
        if ed is not None and not editing_text:
            if e.matches(QtGui.QKeySequence.StandardKey.Copy):
                ed.copy_to_clipboard(); e.accept(); return
            if e.matches(QtGui.QKeySequence.StandardKey.Paste):
                ed.paste_from_clipboard(); e.accept(); return
        if self._tool == "move":  # 方向键微移当前层（Shift = 10px，PS 行为）
            step = 10 if (e.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier) else 1
            d = {QtCore.Qt.Key.Key_Left: (-step, 0), QtCore.Qt.Key.Key_Right: (step, 0),
                 QtCore.Qt.Key.Key_Up: (0, -step), QtCore.Qt.Key.Key_Down: (0, step)}.get(e.key())
            if d:
                self.nudge.emit(*d)
                return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e: QtGui.QKeyEvent):
        if e.key() == QtCore.Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = False
            if not self._panning:
                self.viewport().setCursor(self._cursor_for_tool())
            return
        super().keyReleaseEvent(e)

    def contextMenuEvent(self, e: QtGui.QContextMenuEvent):
        # 放大镜右键 = 缩小（以光标处为锚，PS 放大镜逻辑：左键放大/右键缩小）
        if self._tool == "zoom":
            self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            self._apply_zoom(1.0 / 1.25)
            e.accept()
            return
        ed = getattr(self, "_editor", None)
        if ed is None:
            e.accept(); return
        # 右键命中连接线 → 弹连接线菜单（形状/颜色/虚线/删除），不弹画布菜单
        if hasattr(ed, "_connector_menu_at") and ed._connector_menu_at(self.mapToScene(e.pos()), e.globalPos()):
            e.accept(); return
        # 画布右键菜单：复制 / 粘贴 / 删除（不弹主窗口的工具栏/停靠切换菜单）
        m = QtWidgets.QMenu(self)
        has_layer = getattr(ed, "active", None) is not None
        has_clip = not QtWidgets.QApplication.clipboard().image().isNull()
        a_copy = m.addAction("复制图层"); a_copy.setEnabled(has_layer)
        a_copy.triggered.connect(ed.copy_to_clipboard)
        a_paste = m.addAction("粘贴"); a_paste.setEnabled(has_clip)
        a_paste.triggered.connect(ed.paste_from_clipboard)
        m.addSeparator()
        tm = m.addMenu("翻转 / 旋转")  # 作用于选中矢量元素或活动层
        tm.addAction("水平翻转").triggered.connect(lambda: ed._flip_objects(True))
        tm.addAction("垂直翻转").triggered.connect(lambda: ed._flip_objects(False))
        tm.addAction("顺时针 90°").triggered.connect(lambda: ed._rotate_objects(90))
        tm.addAction("逆时针 90°").triggered.connect(lambda: ed._rotate_objects(270))
        m.addSeparator()
        # 裁掉透明边：把当前图片层裁到真正的图（解决素材四周大方框）；不透明水印裁不掉，提示用裁剪工具
        _act = getattr(ed, "active", None)
        _is_raster = bool(_act and _act.get("kind") != "vector" and _act.get("image") is not None)
        a_trim = m.addAction("✂ 裁掉透明边（裁到内容）"); a_trim.setEnabled(_is_raster)
        a_trim.setToolTip("把当前图片层四周透明留白裁掉，使框=真正的图")
        a_trim.triggered.connect(ed._trim_active_layer)
        a_del = m.addAction("删除图层"); a_del.setEnabled(has_layer)
        a_del.triggered.connect(ed._delete_active)
        m.exec(e.globalPos())
        e.accept()

    def _ring_rect(self, vp_pt):
        # 画笔光圈的视口包围矩形（含 padding），供局部重绘。
        if vp_pt is None or not self._brush_radius:
            return None
        r = max(1.0, self._brush_radius * self._zoom); pad = 4
        s = int(2 * (r + pad))
        return QtCore.QRect(int(vp_pt.x() - r - pad), int(vp_pt.y() - r - pad), s, s)

    def _update_ring(self, old_vp):
        # 只重绘光圈【新+旧】两小块，而非整视口（每次 mousemove 全场刷很贵，实测局部快 5-7×·审核 MEDIUM）。
        # FPS HUD 开时退回全刷，保证 (10,22) 处的 FPS 文字仍每帧刷新（仅调试模式，可接受）。
        if self._fps_hud:
            self.viewport().update(); return
        rect = self._ring_rect(self._cursor_vp)
        if rect is None:
            self.viewport().update(); return
        old = self._ring_rect(old_vp)
        if old is not None:
            rect = rect.united(old)
        self.viewport().update(rect)

    def mouseMoveEvent(self, e: QtGui.QMouseEvent):
        old_vp = self._cursor_vp  # 旧光标位：用于只重绘光圈新旧两小块（审核 MEDIUM）
        self._cursor_vp = e.position().toPoint()
        ed = getattr(self, "_editor", None)  # 标尺隐藏时整条信号+mapToScene 都省掉
        if ed is not None and getattr(ed, "_rulers_visible", False):
            self.cursorScene.emit(self.mapToScene(self._cursor_vp))  # 标尺游标线跟随
        if self._panning:
            p = e.position().toPoint()
            d = p - self._pan_last
            self._pan_last = p
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            return
        if self._tool == "connector":  # 连接线工具：移动即更新悬停对象的边中点锚点(不 return,拖动还要走 paintMove 画预览)
            self.connectorHover.emit(self.mapToScene(e.position().toPoint()))
        if self._tool == "pen":
            sp = self.mapToScene(e.position().toPoint())
            if e.buttons() & QtCore.Qt.MouseButton.LeftButton:
                self.penDragTo.emit(sp)   # 按下拖动 → 拉控制柄
            else:
                self.penHover.emit(sp)    # 无按键 → 橡皮筋预览到光标
            return
        if self._tool == "measure" and (e.buttons() & QtCore.Qt.MouseButton.LeftButton) and self._measure_start is not None:
            sp = self.mapToScene(e.position().toPoint())
            self._measure_end = sp
            self.measureChanged.emit(self._measure_start, sp)
            self.viewport().update()
            return
        if self._tool in self._PAINT_TOOLS and (e.buttons() & QtCore.Qt.MouseButton.LeftButton):
            self.paintMove.emit(self.mapToScene(e.position().toPoint()))
            if self._brush_radius:
                self._update_ring(old_vp)  # 笔触时光圈跟随（只刷光圈区域）
            return
        if self._tool in ("brush", "draw", "eraser") and self._brush_radius:
            self._update_ring(old_vp)      # 悬停光圈跟随（只刷光圈区域）
        elif self._tool == "move" and not e.buttons():
            self._update_move_cursor(e.position().toPoint())  # 悬停光标：手柄→双向箭头/对象→移动
        super().mouseMoveEvent(e)

    def _update_move_cursor(self, vp_pt):
        it = self.itemAt(vp_pt)
        nm = type(it).__name__ if it is not None else ""
        if nm == "ResizeHandle":
            self.viewport().setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif nm == "ImageLayerItem":
            self.viewport().setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        else:
            self.viewport().setCursor(QtCore.Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        if self._panning:  # 平移可由 中键/左键(抓手·空格) 发起 → 松开任意键都停止
            self._panning = False
            self.viewport().setCursor(
                QtCore.Qt.CursorShape.OpenHandCursor if self._space else self._cursor_for_tool())
            return
        if self._tool == "text" and self._text_press is not None and e.button() == QtCore.Qt.MouseButton.LeftButton:
            p0 = self._text_press; self._text_press = None
            self.textBox.emit(p0, self.mapToScene(e.position().toPoint()))  # 起点+终点 → 编辑器决定定宽/默认
            return
        if self._tool == "pen" and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.penRelease.emit(self.mapToScene(e.position().toPoint()))
            return
        if self._tool == "node" and e.button() == QtCore.Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(e)  # 让 handle 拖动正常收尾
            return
        if self._tool == "measure" and e.button() == QtCore.Qt.MouseButton.LeftButton and self._measure_start is not None:
            sp = self.mapToScene(e.position().toPoint())
            self._measure_end = sp
            self.measureChanged.emit(self._measure_start, sp)
            self.viewport().update()
            return
        if self._tool in self._PAINT_TOOLS and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.paintRelease.emit()
            return
        super().mouseReleaseEvent(e)

    # —— 拖放：素材库缩略图 / 文件管理器图片拖到画布 → drop 处建图层 ——
    @staticmethod
    def _accepts_drop(md: QtCore.QMimeData) -> bool:
        return md.hasUrls() or md.hasFormat("application/x-nanopro-asset")

    def dragEnterEvent(self, e: QtGui.QDragEnterEvent):
        if self._accepts_drop(e.mimeData()):
            e.acceptProposedAction(); return
        super().dragEnterEvent(e)

    def dragMoveEvent(self, e: QtGui.QDragMoveEvent):
        if self._accepts_drop(e.mimeData()):
            e.acceptProposedAction(); return
        super().dragMoveEvent(e)

    def dropEvent(self, e: QtGui.QDropEvent):
        md = e.mimeData()
        path = ""
        if md.hasFormat("application/x-nanopro-asset"):  # 优先自定义格式（素材库内部拖拽）
            path = bytes(md.data("application/x-nanopro-asset")).decode("utf-8", "ignore")
        elif md.hasUrls():
            path = md.urls()[0].toLocalFile()
        if path:
            self.assetDropped.emit(self.mapToScene(e.position().toPoint()), path)
            e.acceptProposedAction(); return
        super().dropEvent(e)

    def set_fps_hud(self, on: bool):
        self._fps_hud = bool(on)
        self.viewport().update()

    def paintEvent(self, e: QtGui.QPaintEvent):
        super().paintEvent(e)
        fps = 0.0
        if self._fps_hud:  # 默认关：不每帧算 FPS/emit/drawText（审核 MEDIUM 性能）
            now = time.perf_counter()
            self._frames.append(now)
            cutoff = now - 1.0
            while self._frames and self._frames[0] < cutoff:
                self._frames.pop(0)
            fps = (len(self._frames) - 1) / (now - self._frames[0]) if len(self._frames) > 1 else 0.0
            self.fpsChanged.emit(fps)
        ring = self._tool in ("brush", "draw", "eraser") and self._brush_radius and self._cursor_vp is not None
        if not (self._fps_hud or ring):  # 既无 HUD 又无画笔光圈 → 不建 QPainter（空闲帧零额外开销）
            return
        p = QtGui.QPainter(self.viewport())
        if self._fps_hud:
            p.setPen(QtGui.QColor(theme.colors()["hud"]))
            font = p.font(); font.setBold(True); font.setPointSize(11); p.setFont(font)
            p.drawText(10, 22, f"FPS {fps:5.1f}   zoom {self._zoom * 100:4.0f}%   [{self._tool}]")
        # 画笔/橡皮尺寸光圈（蓝+白双环，移植 features.js:1164-1184）：半径=画笔半径×缩放
        if ring:
            r = max(1.0, self._brush_radius * self._zoom)
            c = QtCore.QPointF(self._cursor_vp)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            pen = QtGui.QPen(QtGui.QColor(59, 130, 246, 242), 1.5); pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            p.setPen(pen); p.drawEllipse(c, r, r)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 200), 1.0)); p.drawEllipse(c, r, r)
        p.end()
