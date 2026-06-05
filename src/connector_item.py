"""智能连接线（BioRender 式）：两端绑定到图层(uid)，端点贴各自外框边缘，终点画箭头；
绑定的对象移动/缩放时由 editor._refresh_connectors() 调 update_path 自动跟随。

形状可切换：直线 straight / 曲线 curved（BioRender 推荐）/ 折线 elbow。可设虚线、颜色、线宽。
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


def edge_anchor(rect: QtCore.QRectF, toward: QtCore.QPointF) -> QtCore.QPointF:
    """选 rect 的【边中点】(上/下/左/右 四个中点)里【朝向 toward 的那个】→ 连线落在边的正中
    （对齐 BioRender：连到框边的中心，而非随意的交点）。横向跨度大走左右中点，否则走上下中点。"""
    c = rect.center()
    dx = toward.x() - c.x()
    dy = toward.y() - c.y()
    if abs(dx) >= abs(dy):
        return QtCore.QPointF(rect.right() if dx >= 0 else rect.left(), c.y())  # 右/左 边中点
    return QtCore.QPointF(c.x(), rect.bottom() if dy >= 0 else rect.top())       # 下/上 边中点


def anchor_points(rect: QtCore.QRectF):
    """框的 4 个【边中点】(上/右/下/左)——悬停时画成蓝点，让用户看到能连到哪（BioRender 式锚点）。"""
    c = rect.center()
    return [QtCore.QPointF(c.x(), rect.top()), QtCore.QPointF(rect.right(), c.y()),
            QtCore.QPointF(c.x(), rect.bottom()), QtCore.QPointF(rect.left(), c.y())]


def _arrow_head(qp: QtGui.QPainterPath, tip: QtCore.QPointF, tail: QtCore.QPointF, size: float = 13.0):
    """在 tip 处画指向 tip→远离 tail 的实心箭头三角形（几何同形状工具 sh_arrow）。"""
    line = QtCore.QLineF(tip, tail)
    L = line.length()
    if L < 1.0:
        return
    ang = math.acos(max(-1.0, min(1.0, line.dx() / L)))
    if line.dy() >= 0:
        ang = (math.pi * 2.0) - ang
    s = max(8.0, min(20.0, size))
    a1 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi / 3.0) * s, math.cos(ang + math.pi / 3.0) * s)
    a2 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi - math.pi / 3.0) * s, math.cos(ang + math.pi - math.pi / 3.0) * s)
    qp.moveTo(tip)
    qp.lineTo(a1)
    qp.lineTo(a2)
    qp.closeSubpath()


def _elbow_knee(p1: QtCore.QPointF, p2: QtCore.QPointF) -> QtCore.QPointF:
    """折线拐点：水平跨度大 → 先水平后垂直；否则先垂直后水平（直角连线，像流程图）。"""
    if abs(p2.x() - p1.x()) >= abs(p2.y() - p1.y()):
        return QtCore.QPointF(p2.x(), p1.y())
    return QtCore.QPointF(p1.x(), p2.y())


def build_connector_path(p1: QtCore.QPointF, p2: QtCore.QPointF,
                         shape: str = "straight", arrow: float = 13.0) -> QtGui.QPainterPath:
    """按形状构造 p1→p2 连线 + p2 端实心箭头，烘焙进一条 QPainterPath（scene 坐标）。
    shape: straight 直线 / curved 平滑曲线（中点法向外凸）/ elbow 直角折线。箭头沿末端切线。"""
    qp = QtGui.QPainterPath()
    if shape == "curved":
        mid = QtCore.QPointF((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
        dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
        L = math.hypot(dx, dy)
        if L >= 1.0:
            nx, ny = -dy / L, dx / L                 # 法向单位向量
            off = min(70.0, L * 0.22)                # 外凸量随距离，封顶
            ctrl = QtCore.QPointF(mid.x() + nx * off, mid.y() + ny * off)
        else:
            ctrl = mid
        qp.moveTo(p1)
        qp.quadTo(ctrl, p2)
        _arrow_head(qp, p2, ctrl, arrow)             # 箭头沿曲线末端切线（指向 ctrl）
    elif shape == "elbow":
        knee = _elbow_knee(p1, p2)
        qp.moveTo(p1)
        qp.lineTo(knee)
        qp.lineTo(p2)
        _arrow_head(qp, p2, knee, arrow)             # 箭头沿最后一段方向
    else:  # straight
        qp.moveTo(p1)
        qp.lineTo(p2)
        _arrow_head(qp, p2, p1, arrow)
    return qp


class ConnectorItem(QtWidgets.QGraphicsPathItem):
    """连接线图元：存两端图层 uid + 形状/颜色/虚线/线宽；update_path() 按两对象当前外框重算并跟随。"""

    def __init__(self, editor, src_uid, dst_uid, color="#333333", width: float = 2.0, shape: str = "straight"):
        super().__init__()
        self._editor = editor
        self.src_uid = src_uid
        self.dst_uid = dst_uid
        self.kind = "connector"
        self.line_shape = shape   # 注意：不能叫 self.shape——会覆盖 QGraphicsItem.shape() 碰撞检测方法
        self.dashed = False
        self.width = width
        self.color = QtGui.QColor(color)
        self.setZValue(5_000_000.0)                  # 浮在对象之上，箭头可见
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self._apply_pen()

    def _apply_pen(self):
        pen = QtGui.QPen(self.color, self.width)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        if self.dashed:
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setBrush(QtGui.QBrush(self.color))      # 箭头三角形实心填充

    def set_shape(self, shape: str):
        self.line_shape = shape
        self.update_path()

    def set_color(self, color):
        self.color = QtGui.QColor(color)
        self._apply_pen()

    def set_dashed(self, on: bool):
        self.dashed = bool(on)
        self._apply_pen()

    def update_path(self) -> bool:
        """按两端对象当前外框重算路径。端点对象已不存在 → 返回 False（调用方删除本连接线）。"""
        ra = self._editor._connector_rect(self.src_uid)
        rb = self._editor._connector_rect(self.dst_uid)
        if ra is None or rb is None:
            return False
        p1 = edge_anchor(ra, rb.center())  # 落在 A 朝向 B 的那条边的【正中】
        p2 = edge_anchor(rb, ra.center())  # 落在 B 朝向 A 的那条边的【正中】
        self.setPath(build_connector_path(p1, p2, self.line_shape))
        return True
