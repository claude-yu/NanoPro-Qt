"""智能连接线（BioRender 式）：两端绑定到图层(uid)，端点贴各自外框边缘，终点画箭头；
绑定的对象移动/缩放时由 editor._refresh_connectors() 调 update_path 自动跟随。

几何函数纯 Qt 无副作用、可 headless 单测（edge_point / build_connector_path）。
箭头几何照搬形状工具(_shape_path 的 sh_arrow)，保持全软件箭头观感一致。
"""
from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets


def edge_point(rect: QtCore.QRectF, toward: QtCore.QPointF) -> QtCore.QPointF:
    """从 rect 中心朝 toward 方向的射线与 rect 边界的交点。
    连接线端点贴在对象框【边缘】而非中心 → 线不扎进对象里，像 PS/BioRender。"""
    c = rect.center()
    dx = toward.x() - c.x()
    dy = toward.y() - c.y()
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return QtCore.QPointF(rect.right(), c.y())
    hw = max(1e-6, rect.width() / 2.0)
    hh = max(1e-6, rect.height() / 2.0)
    sx = hw / abs(dx) if abs(dx) > 1e-9 else float("inf")  # 射线触及左右竖边的缩放系数
    sy = hh / abs(dy) if abs(dy) > 1e-9 else float("inf")  # 触及上下横边
    s = min(sx, sy)                                          # 先到的那条边
    return QtCore.QPointF(c.x() + dx * s, c.y() + dy * s)


def build_connector_path(p1: QtCore.QPointF, p2: QtCore.QPointF, arrow: float = 13.0) -> QtGui.QPainterPath:
    """直线 p1→p2 + p2 端实心箭头三角形，烘焙进一条 QPainterPath（scene 坐标）。"""
    qp = QtGui.QPainterPath()
    qp.moveTo(p1)
    qp.lineTo(p2)
    line = QtCore.QLineF(p2, p1)  # 尖端→尾（与 _shape_path sh_arrow 同方向约定）
    L = line.length()
    if L >= 1.0:
        ang = math.acos(max(-1.0, min(1.0, line.dx() / L)))
        if line.dy() >= 0:
            ang = (math.pi * 2.0) - ang
        size = max(8.0, min(20.0, arrow))
        a1 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi / 3.0) * size,
                                        math.cos(ang + math.pi / 3.0) * size)
        a2 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi - math.pi / 3.0) * size,
                                        math.cos(ang + math.pi - math.pi / 3.0) * size)
        qp.moveTo(p2)
        qp.lineTo(a1)
        qp.lineTo(a2)
        qp.closeSubpath()
    return qp


class ConnectorItem(QtWidgets.QGraphicsPathItem):
    """连接线图元：存两端图层 uid + 样式；update_path() 按两对象当前外框重算并自动跟随。"""

    def __init__(self, editor, src_uid, dst_uid, color: str = "#333333", width: float = 2.0):
        super().__init__()
        self._editor = editor
        self.src_uid = src_uid
        self.dst_uid = dst_uid
        self.kind = "connector"
        col = QtGui.QColor(color)
        pen = QtGui.QPen(col, width)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        self.setPen(pen)
        self.setBrush(QtGui.QBrush(col))            # 箭头三角形实心填充
        self.setZValue(5_000_000.0)                  # 浮在对象之上，箭头可见
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    def update_path(self) -> bool:
        """按两端对象当前外框重算路径。端点对象已不存在 → 返回 False（调用方删除本连接线）。"""
        ra = self._editor._connector_rect(self.src_uid)
        rb = self._editor._connector_rect(self.dst_uid)
        if ra is None or rb is None:
            return False
        p1 = edge_point(ra, rb.center())
        p2 = edge_point(rb, ra.center())
        self.setPath(build_connector_path(p1, p2))
        return True
