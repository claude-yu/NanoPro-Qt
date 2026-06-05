"""ImageLayerItem：直接绘制 QImage 的图层项。

画笔改 QImage 后只 `update(脏矩形)` → 不做整图 QImage→QPixmap 转换、只重绘局部区域，
大图涂抹也流畅（修复 setPixmap 整图转换导致的卡顿）。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class ImageLayerItem(QtWidgets.QGraphicsItem):
    def __init__(self, image: QtGui.QImage):
        super().__init__()
        self._image = image
        self._mask = None             # 非破坏蒙版（uint8 HxW，255 露 / 0 藏）；None=无蒙版
        self._display_image = None     # 应用蒙版后的缓存显示图（懒构建）；蒙版或原图变了置空
        self._move_cb = None          # 拖动开始前回调（用于撤销抓快照）
        self._press_cb = None         # 被按下时回调（用于设为激活层）
        self._snap_cb = None          # 落点吸附回调 (QPointF)->QPointF（参考线/画布边/中线/其它层）
        self._release_cb = None       # 松手回调（清智能参考线洋红虚线）
        self._moved_this_drag = False
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        # 默认设备坐标缓存：拖动/缩放只贴缓存位图，不每帧重算 drawImage → 多图层拖动丝滑
        self.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def itemChange(self, change, value):
        # 一次拖动里只在「位置首次变化」时通知一次（此刻 pos() 仍是移动前的位置）
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            if not self._moved_this_drag:
                self._moved_this_drag = True
                if self._move_cb:
                    self._move_cb()
            if self._snap_cb is not None:
                value = self._snap_cb(value)  # value=即将生效的新 pos，返回吸附后的 pos（Qt 用返回值作最终 pos）
        return super().itemChange(change, value)

    def mousePressEvent(self, e):
        self._moved_this_drag = False
        if self._press_cb:
            self._press_cb()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._moved_this_drag = False
        if self._release_cb:
            self._release_cb()  # 清掉拖动期间显示的智能参考线
        super().mouseReleaseEvent(e)

    def set_painting(self, on: bool):
        """描笔期间关缓存(脏矩形局部刷新更快)，描完恢复缓存(拖动/缩放丝滑)。"""
        self.setCacheMode(
            QtWidgets.QGraphicsItem.CacheMode.NoCache if on
            else QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache
        )

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, self._image.width(), self._image.height())

    def contains(self, point: QtCore.QPointF) -> bool:
        # PS 式自动选层：只在不透明像素处命中，点透明区会穿透到下层
        x, y = int(point.x()), int(point.y())
        if 0 <= x < self._image.width() and 0 <= y < self._image.height():
            return self._image.pixelColor(x, y).alpha() > 8
        return False

    def _effective(self) -> QtGui.QImage:
        """实际要画的图：无蒙版=原图；有蒙版=应用蒙版后的缓存图（与导出/缩略图同一函数，永不分叉）。"""
        if self._mask is None:
            return self._image
        if self._display_image is None:
            import image_ops
            self._display_image = image_ops.masked_qimage(self._image, self._mask)
        return self._display_image

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        painter.drawImage(0, 0, self._effective())  # 视图已开 SmoothPixmapTransform，缩放平滑

    def image(self) -> QtGui.QImage:
        return self._image

    def set_mask(self, mask):
        """设/清非破坏蒙版（None=清）。原 image 像素不动，只改透明度显示。"""
        self._mask = mask
        self._display_image = None  # 蒙版变 → 重建显示图
        self.update()

    def set_image(self, image: QtGui.QImage):
        if image.size() != self._image.size():
            self.prepareGeometryChange()
        self._image = image
        self._display_image = None  # 原图变 → 重建显示图
        self.update()
