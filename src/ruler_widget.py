"""标尺部件：横/纵两条轻量 QWidget 标尺，无状态地跟随 CanvasView 的 transform。

PS 式：刻度=场景像素（图像 px），随缩放/滚动实时换算。不存任何持久状态，纯派生自 view
（zoom + 滚动条），外部在 zoom/scroll 变化时调 sync() 触发重绘。从标尺上按下拖出参考线：
press 进入拖拽，move/release 把游标映射到 view.viewport() → mapToScene → 注入的 on_drop 回调
（EditorWindow 落定参考线）。
"""
from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

import theme

RULER_THICK = 20  # 标尺厚度(px)：横尺高 / 纵尺宽

# 主刻度候选步长(场景像素)：挑使相邻主刻度屏幕间距≈60–100px 的最小步长
_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)


class RulerWidget(QtWidgets.QWidget):
    def __init__(self, view, orientation: QtCore.Qt.Orientation, parent=None):
        super().__init__(parent)
        self._view = view
        self._orient = orientation
        self._mouse_scene = None  # 鼠标在画布中的当前像素位置（画游标线用），None=不画
        self.on_drop = None       # EditorWindow 注入：on_drop(orient:str, vp_pt:QPoint, final:bool)
        self._dragging = False
        # 性能：把"刻度层"缓存成 QPixmap（只在缩放/滚动/尺寸变化时重建），光标线单独叠加。
        # 鼠标移动只刷光标所在窄带，不重算整条刻度（原来每次移动都全量重绘 + 每刻度 mapFromScene + 旋转文字）。
        self._tick_cache = None        # QPixmap：缓存的刻度+底色层（不含光标线）
        self._cache_key = None         # (s0, zoom, w, h, dpr) 变了才重建缓存
        self._cursor_px = None         # 上次光标屏幕坐标，用于局部失效旧/新两条窄带
        self._font = QtGui.QFont(); self._font.setPointSize(8)  # 复用，不每帧重建
        self.setMouseTracking(True)
        if orientation == QtCore.Qt.Orientation.Horizontal:
            self.setFixedHeight(RULER_THICK)
        else:
            self.setFixedWidth(RULER_THICK)

    # 几何变化（缩放/滚动/尺寸）→ 刻度缓存失效 + 整条重绘
    def sync(self):
        self._tick_cache = None
        self.update()

    def set_cursor(self, scene_val):
        """只更新光标线：不动刻度缓存，仅失效旧/新光标两条窄带（鼠标移动热路径）。"""
        self._mouse_scene = scene_val
        horizontal = self._orient == QtCore.Qt.Orientation.Horizontal
        new_px = None
        if scene_val is not None:
            s0, _, zoom = self._visible_span()
            new_px = (scene_val - s0) * zoom

        def band(px):
            if px is None:
                return None
            if horizontal:
                return QtCore.QRect(int(px) - 1, 0, 3, self.height())
            return QtCore.QRect(0, int(px) - 1, self.width(), 3)

        r = None
        for px in (self._cursor_px, new_px):
            b = band(px)
            if b is not None:
                r = b if r is None else r.united(b)
        self._cursor_px = new_px
        if r is not None:
            self.update(r)

    def _orient_str(self) -> str:
        # PS 式：上(横)标尺拉出横向参考线("h")，左(纵)标尺拉出竖向参考线("v")
        return "h" if self._orient == QtCore.Qt.Orientation.Horizontal else "v"

    @staticmethod
    def _nice_step(zoom: float) -> int:
        # 选使"相邻主刻度屏幕间距≈60–100px"的最小步长；极端缩放兜底用最大步长
        for s in _STEPS:
            if s * zoom >= 60:
                return s
        return _STEPS[-1]

    def _visible_span(self):
        """返回 (s0, s1, zoom)：viewport 两端对应的 scene 坐标 + 缩放。单轴、无旋转/错切，
        故屏幕坐标 = (scene - s0) * zoom（免去 per-tick mapFromScene 的矩阵求逆）。"""
        view = self._view
        vp = view.viewport()
        zoom = max(1e-6, view.current_zoom())
        if self._orient == QtCore.Qt.Orientation.Horizontal:
            s0 = view.mapToScene(QtCore.QPoint(0, 0)).x()
            s1 = view.mapToScene(QtCore.QPoint(vp.width(), 0)).x()
        else:
            s0 = view.mapToScene(QtCore.QPoint(0, 0)).y()
            s1 = view.mapToScene(QtCore.QPoint(0, vp.height())).y()
        return s0, s1, zoom

    def _render_ticks(self, s0, s1, zoom, w, h, dpr):
        """把刻度+底色渲染进一张 QPixmap（不含光标线）。仅几何变化时调用一次。"""
        c = theme.colors()
        pm = QtGui.QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QtGui.QColor(c["menu_bar"]))
        p = QtGui.QPainter(pm)
        p.setFont(self._font)
        p.setPen(QtGui.QPen(QtGui.QColor(c["muted"]), 1))
        horizontal = self._orient == QtCore.Qt.Orientation.Horizontal
        thick = h if horizontal else w
        step = self._nice_step(zoom)
        lo, hi = (s0, s1) if s0 <= s1 else (s1, s0)
        v = math.floor(lo / step) * step
        while v <= hi + step:
            px = (v - s0) * zoom  # 手算屏幕坐标，免 per-tick mapFromScene
            if horizontal:
                if -1 <= px <= w + 1:
                    p.drawLine(int(px), thick - 7, int(px), thick)
                    p.drawText(int(px) + 2, thick - 8, str(int(v)))
            else:
                if -1 <= px <= h + 1:
                    p.drawLine(thick - 7, int(px), thick, int(px))
                    p.save(); p.translate(thick - 8, int(px) - 2); p.rotate(-90)
                    p.drawText(0, 0, str(int(v))); p.restore()
            v += step
        p.end()
        return pm

    def paintEvent(self, e: QtGui.QPaintEvent):
        dpr = self.devicePixelRatioF()
        s0, s1, zoom = self._visible_span()
        w, h = self.width(), self.height()
        key = (round(s0, 1), round(zoom, 4), w, h, round(dpr, 2))
        if self._tick_cache is None or self._cache_key != key:
            self._tick_cache = self._render_ticks(s0, s1, zoom, w, h, dpr)
            self._cache_key = key
        p = QtGui.QPainter(self)
        p.drawPixmap(0, 0, self._tick_cache)  # 受 e.rect() 裁剪；局部失效时只 blit 那条窄带
        # 鼠标游标线（accent 色）叠在缓存之上
        if self._mouse_scene is not None:
            px = (self._mouse_scene - s0) * zoom
            p.setPen(QtGui.QPen(QtGui.QColor(theme.colors()["accent"]), 1))
            if self._orient == QtCore.Qt.Orientation.Horizontal:
                if 0 <= px <= w:
                    p.drawLine(int(px), 0, int(px), h)
            else:
                if 0 <= px <= h:
                    p.drawLine(0, int(px), w, int(px))
            # 记下"实际绘制处"的屏幕坐标：full 重绘(缩放/滚动)后光标线位置会变，
            # 若不回写 _cursor_px，下次 set_cursor 的旧带会用过期值 → 残留 1px 残影(审核 LOW)。
            self._cursor_px = int(px)
        p.end()

    # —— 从标尺拖出参考线 ——
    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.MouseButton.LeftButton and self.on_drop is not None:
            self._dragging = True
            self._emit_drop(e, final=False)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QtGui.QMouseEvent):
        if self._dragging:
            self._emit_drop(e, final=False)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        if self._dragging and e.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = False
            self._emit_drop(e, final=True)
            return
        super().mouseReleaseEvent(e)

    def _emit_drop(self, e: QtGui.QMouseEvent, final: bool):
        # 把标尺局部坐标映射到 view.viewport() 局部坐标，交给 EditorWindow 求 scene 值
        gp = self.mapToGlobal(e.position().toPoint())
        vp = self._view.viewport()
        vp_pt = vp.mapFromGlobal(gp)
        self.on_drop(self._orient_str(), vp_pt, final)
