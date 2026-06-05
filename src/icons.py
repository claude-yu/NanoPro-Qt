"""用 QPainter 矢量生成工具/眼睛图标 —— 不依赖外部图标文件，风格随主题色统一。

避免 SVG/PNG 资源依赖；图标在内存里画成 QPixmap→QIcon，描边色可传入以适配深色主题。
"""
from __future__ import annotations

import functools
import math

from PySide6 import QtCore, QtGui


def _canvas(size: int) -> QtGui.QPixmap:
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    return pix


def _pen_painter(pix: QtGui.QPixmap, color: str, width: float) -> QtGui.QPainter:
    pt = QtGui.QPainter(pix)
    pt.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(color), width)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    pt.setPen(pen)
    return pt


def _line(pt: QtGui.QPainter, x1, y1, x2, y2):
    pt.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))


def save_chevron_png(path: str, color: str = "#9aa0ad", size: int = 20, up: bool = False) -> bool:
    """画一个小 V 形箭头存成 PNG（QComboBox 下拉 / QSpinBox 上下按钮的 ::up-arrow/::down-arrow 用）。
    up=True 朝上、False 朝下。"""
    pix = _canvas(size)
    pt = _pen_painter(pix, color, max(1.6, size * 0.09))
    s = size
    if up:
        _line(pt, s * 0.28, s * 0.60, s * 0.50, s * 0.38)
        _line(pt, s * 0.50, s * 0.38, s * 0.72, s * 0.60)
    else:
        _line(pt, s * 0.28, s * 0.40, s * 0.50, s * 0.62)
        _line(pt, s * 0.50, s * 0.62, s * 0.72, s * 0.40)
    pt.end()
    return bool(pix.save(path, "PNG"))


def tool_icon(name: str, color: str = "#cdd2db", size: int = 22) -> QtGui.QIcon:
    pix = _canvas(size)
    pt = _pen_painter(pix, color, 2.0)
    s = size
    c = s / 2
    if name == "move":
        a = 3.2
        _line(pt, c, 3, c, s - 3)
        _line(pt, 3, c, s - 3, c)
        _line(pt, c, 3, c - a, 3 + a); _line(pt, c, 3, c + a, 3 + a)
        _line(pt, c, s - 3, c - a, s - 3 - a); _line(pt, c, s - 3, c + a, s - 3 - a)
        _line(pt, 3, c, 3 + a, c - a); _line(pt, 3, c, 3 + a, c + a)
        _line(pt, s - 3, c, s - 3 - a, c - a); _line(pt, s - 3, c, s - 3 - a, c + a)
    elif name == "brush":
        _line(pt, s - 5, 5, 8.5, s - 8.5)
        pt.setBrush(QtGui.QColor(color))
        pt.drawEllipse(QtCore.QPointF(6.5, s - 6.5), 3.0, 3.0)
    elif name == "eraser":
        pt.save()
        pt.translate(c, c); pt.rotate(-35); pt.translate(-c, -c)
        pt.drawRoundedRect(QtCore.QRectF(s * 0.20, s * 0.38, s * 0.60, s * 0.30), 2.5, 2.5)
        _line(pt, s * 0.50, s * 0.38, s * 0.50, s * 0.68)
        pt.restore()
    elif name == "wand":
        _line(pt, s - 6, 6, 8, s - 8)

        def spark(cx, cy, r):
            _line(pt, cx - r, cy, cx + r, cy)
            _line(pt, cx, cy - r, cx, cy + r)

        spark(s - 4.5, 4.5, 2.6); spark(s - 9, 9.5, 1.8); spark(5.5, 5.5, 1.8)
    elif name == "lasso":
        pt.drawEllipse(QtCore.QRectF(s * 0.18, s * 0.16, s * 0.62, s * 0.44))  # 套索环
        _line(pt, s * 0.32, s * 0.58, s * 0.28, s * 0.82)                      # 垂下的绳
        pt.setBrush(QtGui.QColor(color)); pt.drawEllipse(QtCore.QPointF(s * 0.28, s * 0.82), 1.8, 1.8)
    elif name == "rect":
        pen = pt.pen(); pen.setStyle(QtCore.Qt.PenStyle.DashLine); pt.setPen(pen)
        pt.drawRect(QtCore.QRectF(s * 0.18, s * 0.22, s * 0.64, s * 0.56))
    elif name == "rectsel":  # 矩形选框选区：虚线方框 + 框内中心小十字（marquee 选区语义，区别于 rect 纯框）
        pen = pt.pen(); pen.setStyle(QtCore.Qt.PenStyle.DashLine); pt.setPen(pen)
        pt.drawRect(QtCore.QRectF(s * 0.16, s * 0.20, s * 0.68, s * 0.60))
        pen.setStyle(QtCore.Qt.PenStyle.SolidLine); pt.setPen(pen)
        _line(pt, c - s * 0.10, c, c + s * 0.10, c)  # 中心十字
        _line(pt, c, c - s * 0.10, c, c + s * 0.10)
    elif name == "draw":  # 铅笔（绘制：在图层上画像素）
        _line(pt, s * 0.30, s * 0.72, s * 0.66, s * 0.36)   # 笔身
        _line(pt, s * 0.60, s * 0.30, s * 0.72, s * 0.42)   # 笔尾
        pt.setBrush(QtGui.QColor(color))                     # 笔尖三角
        tip = QtGui.QPolygonF([QtCore.QPointF(s * 0.22, s * 0.80),
                               QtCore.QPointF(s * 0.30, s * 0.72),
                               QtCore.QPointF(s * 0.34, s * 0.76)])
        pt.drawPolygon(tip)
    elif name == "crop":  # 裁剪：两个交叉 L 标记
        _line(pt, s * 0.32, s * 0.16, s * 0.32, s * 0.74); _line(pt, s * 0.16, s * 0.32, s * 0.74, s * 0.32)
        _line(pt, s * 0.68, s * 0.26, s * 0.68, s * 0.84); _line(pt, s * 0.26, s * 0.68, s * 0.84, s * 0.68)
    elif name == "erase":  # 矩形挖洞/填底：虚线框 + 内部斜线（表示填充覆盖）
        pen = pt.pen(); pen.setStyle(QtCore.Qt.PenStyle.DashLine); pt.setPen(pen)
        pt.drawRect(QtCore.QRectF(s * 0.20, s * 0.24, s * 0.60, s * 0.52))
        pen.setStyle(QtCore.Qt.PenStyle.SolidLine); pt.setPen(pen)
        _line(pt, s * 0.30, s * 0.66, s * 0.64, s * 0.32)
        _line(pt, s * 0.44, s * 0.70, s * 0.72, s * 0.42)
    elif name == "node":  # 锚点工具：一段曲线 + 两个锚点方块 + 控制柄
        path = QtGui.QPainterPath()
        path.moveTo(s * 0.20, s * 0.70)
        path.cubicTo(s * 0.35, s * 0.30, s * 0.65, s * 0.30, s * 0.80, s * 0.70)
        pt.drawPath(path)
        pt.setBrush(QtGui.QColor(color))  # 两端锚点实心小方块
        for (px, py) in ((s * 0.20, s * 0.70), (s * 0.80, s * 0.70)):
            pt.drawRect(QtCore.QRectF(px - s * 0.07, py - s * 0.07, s * 0.14, s * 0.14))
    elif name == "pen":  # 钢笔工具：笔尖三角 + 笔身
        pt.setBrush(QtGui.QColor(color))
        nib = QtGui.QPolygonF([QtCore.QPointF(s * 0.28, s * 0.80),
                               QtCore.QPointF(s * 0.40, s * 0.40),
                               QtCore.QPointF(s * 0.52, s * 0.52),
                               QtCore.QPointF(s * 0.30, s * 0.78)])
        pt.drawPolygon(nib)
        pt.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        _line(pt, s * 0.42, s * 0.38, s * 0.70, s * 0.18)  # 笔身上挑
        _line(pt, s * 0.24, s * 0.84, s * 0.30, s * 0.78)  # 笔尖墨点
    elif name == "measure":  # 标尺/测量：一把斜尺 + 刻度
        pt.save()
        pt.translate(c, c); pt.rotate(-35); pt.translate(-c, -c)
        body = QtCore.QRectF(s * 0.16, s * 0.42, s * 0.68, s * 0.20)
        pt.drawRect(body)
        for i in range(1, 6):  # 5 道刻度（中间一道长，余短）
            tx = body.left() + body.width() * i / 6.0
            tlen = body.height() * (0.6 if i == 3 else 0.36)
            _line(pt, tx, body.top(), tx, body.top() + tlen)
        pt.restore()
    elif name == "star":  # 实心五角星（✨ 插件图标）
        pt.setBrush(QtGui.QColor(color))
        cx, cy, R, r = s / 2, s / 2, s * 0.42, s * 0.17
        poly = QtGui.QPolygonF()
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rad = R if i % 2 == 0 else r
            poly.append(QtCore.QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        pt.drawPolygon(poly)
    elif name == "undo":  # 逆时针回转箭头
        pt.drawArc(QtCore.QRectF(s * 0.24, s * 0.26, s * 0.52, s * 0.52), 35 * 16, 250 * 16)
        pt.setBrush(QtGui.QColor(color))
        pt.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(s * 0.22, s * 0.30), QtCore.QPointF(s * 0.42, s * 0.30),
                                        QtCore.QPointF(s * 0.30, s * 0.46)]))
    elif name == "redo":  # 顺时针回转箭头（undo 镜像）
        pt.drawArc(QtCore.QRectF(s * 0.24, s * 0.26, s * 0.52, s * 0.52), -105 * 16, 250 * 16)
        pt.setBrush(QtGui.QColor(color))
        pt.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(s * 0.78, s * 0.30), QtCore.QPointF(s * 0.58, s * 0.30),
                                        QtCore.QPointF(s * 0.70, s * 0.46)]))
    elif name == "group":  # 虚线外框(组) + 两个实心小块
        pen = pt.pen(); pen.setStyle(QtCore.Qt.PenStyle.DashLine); pt.setPen(pen)
        pt.drawRoundedRect(QtCore.QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68), 3, 3)
        pen.setStyle(QtCore.Qt.PenStyle.SolidLine); pt.setPen(pen)
        pt.setBrush(QtGui.QColor(color))
        pt.drawRect(QtCore.QRectF(s * 0.27, s * 0.27, s * 0.20, s * 0.20))
        pt.drawRect(QtCore.QRectF(s * 0.53, s * 0.53, s * 0.20, s * 0.20))
    elif name == "ungroup":  # 两个分开的块（无外框 → 已拆散）
        pt.setBrush(QtGui.QColor(color))
        pt.drawRect(QtCore.QRectF(s * 0.18, s * 0.18, s * 0.26, s * 0.26))
        pt.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        pt.drawRect(QtCore.QRectF(s * 0.56, s * 0.56, s * 0.26, s * 0.26))
    elif name == "new_layer":  # 新建图层：方框（页面）+ 中心 + 号
        pt.drawRect(QtCore.QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.60))
        _line(pt, c - s * 0.14, c, c + s * 0.14, c)
        _line(pt, c, c - s * 0.14, c, c + s * 0.14)
    elif name == "trash":  # 删除：垃圾桶（盖 + 桶身 + 两道竖纹）
        _line(pt, s * 0.24, s * 0.30, s * 0.76, s * 0.30)            # 桶盖横杠
        _line(pt, s * 0.42, s * 0.22, s * 0.58, s * 0.22)           # 盖把手
        path = QtGui.QPainterPath()
        path.moveTo(s * 0.30, s * 0.30)
        path.lineTo(s * 0.34, s * 0.80); path.lineTo(s * 0.66, s * 0.80)
        path.lineTo(s * 0.70, s * 0.30)
        pt.drawPath(path)
        _line(pt, s * 0.44, s * 0.40, s * 0.45, s * 0.72)           # 竖纹
        _line(pt, s * 0.56, s * 0.40, s * 0.55, s * 0.72)
    elif name == "adjust":  # 亮度/对比度：圆 + 右半填黑（半黑半白，PS 调整图层语义）
        pt.drawEllipse(QtCore.QPointF(c, c), s * 0.30, s * 0.30)
        pt.setBrush(QtGui.QColor(color))
        path = QtGui.QPainterPath()
        path.moveTo(c, c - s * 0.30)
        path.arcTo(QtCore.QRectF(c - s * 0.30, c - s * 0.30, s * 0.60, s * 0.60), 90, -180)
        path.closeSubpath()
        pt.drawPath(path)
    elif name == "mask":  # 图层蒙版：图层方框 + 框内实心圆（框内露/框外藏的语义）
        pt.drawRect(QtCore.QRectF(s * 0.18, s * 0.22, s * 0.64, s * 0.56))
        pt.setBrush(QtGui.QColor(color))
        pt.drawEllipse(QtCore.QPointF(c, c), s * 0.15, s * 0.15)
    elif name == "hand":
        for fx, top in ((0.36, 0.30), (0.50, 0.22), (0.64, 0.24), (0.76, 0.32)):  # 四指
            _line(pt, s * fx, s * top, s * fx, s * 0.60)
        pt.drawArc(QtCore.QRectF(s * 0.30, s * 0.42, s * 0.50, s * 0.46), 200 * 16, 320 * 16)  # 掌
        _line(pt, s * 0.32, s * 0.54, s * 0.22, s * 0.66)  # 拇指
    elif name == "text":
        _line(pt, s * 0.24, s * 0.26, s * 0.76, s * 0.26)  # 顶横
        _line(pt, s * 0.50, s * 0.26, s * 0.50, s * 0.76)  # 竖
        _line(pt, s * 0.40, s * 0.76, s * 0.60, s * 0.76)  # 底脚
    elif name in ("zoom", "zoom_out"):
        cx, cy, rr = s * 0.42, s * 0.42, s * 0.26
        pt.drawEllipse(QtCore.QPointF(cx, cy), rr, rr)         # 镜片
        _line(pt, cx + rr * 0.72, cy + rr * 0.72, s - 3, s - 3)  # 手柄
        _line(pt, cx - rr * 0.45, cy, cx + rr * 0.45, cy)      # 横线（+ 与 − 共用）
        if name == "zoom":
            _line(pt, cx, cy - rr * 0.45, cx, cy + rr * 0.45)  # 竖线 → +（放大镜）
    elif name in ("align_left", "align_hcenter", "align_right",
                  "align_top", "align_vcenter", "align_bottom"):
        # 基准线（实线）+ 两个对齐到该线的小方块。横向对齐画竖基准线 + 横排两块；纵向反之。
        pt.setBrush(QtGui.QColor(color))
        if name in ("align_left", "align_hcenter", "align_right"):
            gx = {"align_left": s * 0.22, "align_hcenter": c, "align_right": s * 0.78}[name]
            _line(pt, gx, s * 0.12, gx, s * 0.88)  # 竖基准线
            bw1, bw2 = s * 0.40, s * 0.26          # 两块不同宽，体现"对齐到线"
            if name == "align_left":
                pt.drawRect(QtCore.QRectF(gx, s * 0.22, bw1, s * 0.20))
                pt.drawRect(QtCore.QRectF(gx, s * 0.56, bw2, s * 0.20))
            elif name == "align_right":
                pt.drawRect(QtCore.QRectF(gx - bw1, s * 0.22, bw1, s * 0.20))
                pt.drawRect(QtCore.QRectF(gx - bw2, s * 0.56, bw2, s * 0.20))
            else:  # hcenter
                pt.drawRect(QtCore.QRectF(gx - bw1 / 2, s * 0.22, bw1, s * 0.20))
                pt.drawRect(QtCore.QRectF(gx - bw2 / 2, s * 0.56, bw2, s * 0.20))
        else:
            gy = {"align_top": s * 0.22, "align_vcenter": c, "align_bottom": s * 0.78}[name]
            _line(pt, s * 0.12, gy, s * 0.88, gy)  # 横基准线
            bh1, bh2 = s * 0.40, s * 0.26
            if name == "align_top":
                pt.drawRect(QtCore.QRectF(s * 0.22, gy, s * 0.20, bh1))
                pt.drawRect(QtCore.QRectF(s * 0.56, gy, s * 0.20, bh2))
            elif name == "align_bottom":
                pt.drawRect(QtCore.QRectF(s * 0.22, gy - bh1, s * 0.20, bh1))
                pt.drawRect(QtCore.QRectF(s * 0.56, gy - bh2, s * 0.20, bh2))
            else:  # vcenter
                pt.drawRect(QtCore.QRectF(s * 0.22, gy - bh1 / 2, s * 0.20, bh1))
                pt.drawRect(QtCore.QRectF(s * 0.56, gy - bh2 / 2, s * 0.20, bh2))
    elif name in ("dist_h", "dist_v"):
        # 三块等间距分布；两条短端线表示分布范围。
        pt.setBrush(QtGui.QColor(color))
        if name == "dist_h":
            for bx in (s * 0.18, s * 0.42, s * 0.66):
                pt.drawRect(QtCore.QRectF(bx, s * 0.30, s * 0.16, s * 0.40))
        else:
            for by in (s * 0.18, s * 0.42, s * 0.66):
                pt.drawRect(QtCore.QRectF(s * 0.30, by, s * 0.40, s * 0.16))
    pt.end()
    return QtGui.QIcon(pix)


def tool_pixmap(name: str, color: str = "#111827", size: int = 26) -> QtGui.QPixmap:
    return tool_icon(name, color, size).pixmap(QtCore.QSize(size, size))


def paint_flyout_badge(pt: QtGui.QPainter, w: int, h: int, bg: str = "#1a8aff", fg: str = "#ffffff"):
    """在按钮右下角画一个小写 z 角标（PS 工具组标志）：accent 圆角底 + 白色 z。
    比小三角更醒目，一眼看出这是个可右键换工具的工具组。pt 已 begin，调用方负责 end。"""
    pt.save()
    pt.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    sz = 11.0
    x = w - sz - 1.0
    y = h - sz - 1.0
    pt.setPen(QtCore.Qt.PenStyle.NoPen)
    pt.setBrush(QtGui.QColor(bg))
    pt.drawRoundedRect(QtCore.QRectF(x, y, sz, sz), 3.0, 3.0)
    f = pt.font(); f.setPixelSize(9); f.setBold(True)
    pt.setFont(f)
    pt.setPen(QtGui.QColor(fg))
    pt.drawText(QtCore.QRectF(x, y - 0.5, sz, sz), QtCore.Qt.AlignmentFlag.AlignCenter, "z")
    pt.restore()


def paint_flyout_triangle(pt: QtGui.QPainter, w: int, h: int, color: str = "#cdd2db"):
    """在按钮右下角画一个实心小三角（PS 工具组标志）。pt 已 begin，调用方负责 end。"""
    pt.save()
    pt.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    pt.setPen(QtCore.Qt.PenStyle.NoPen)
    pt.setBrush(QtGui.QColor(color))
    m = 2.0  # 距右下角内缩
    side = 4.0
    poly = QtGui.QPolygonF([
        QtCore.QPointF(w - m, h - m),
        QtCore.QPointF(w - m - side, h - m),
        QtCore.QPointF(w - m, h - m - side),
    ])
    pt.drawPolygon(poly)
    pt.restore()


@functools.lru_cache(maxsize=64)  # 图层行每次刷新都取眼睛/锁图标 → 按 (状态,色,尺寸) 缓存，免反复重画 SVG
def lock_icon(locked: bool = True, color: str = "#cdd2db", size: int = 18) -> QtGui.QIcon:
    pix = _canvas(size)
    pt = _pen_painter(pix, color if locked else "#8a90a0", 1.6)
    s = size
    pt.drawRoundedRect(QtCore.QRectF(s * 0.26, s * 0.46, s * 0.48, s * 0.36), 2.0, 2.0)  # 锁体
    if locked:  # 闭合锁梁
        pt.drawArc(QtCore.QRectF(s * 0.34, s * 0.22, s * 0.32, s * 0.40), 0, 180 * 16)
        _line(pt, s * 0.34, s * 0.42, s * 0.34, s * 0.46)
        _line(pt, s * 0.66, s * 0.42, s * 0.66, s * 0.46)
    else:       # 打开的锁梁
        pt.drawArc(QtCore.QRectF(s * 0.30, s * 0.18, s * 0.32, s * 0.40), 20 * 16, 160 * 16)
        _line(pt, s * 0.60, s * 0.34, s * 0.60, s * 0.46)
    pt.end()
    return QtGui.QIcon(pix)


@functools.lru_cache(maxsize=64)  # 同上：眼睛图标按 (开合,色,尺寸) 缓存
def eye_icon(open_: bool, color: str = "#cdd2db", size: int = 18) -> QtGui.QIcon:
    pix = _canvas(size)
    pt = _pen_painter(pix, color if open_ else "#6b7180", 1.6)
    s = size
    rect = QtCore.QRectF(s * 0.12, s * 0.26, s * 0.76, s * 0.48)
    if open_:
        pt.drawEllipse(rect)
        pt.setBrush(QtGui.QColor(color))
        pt.drawEllipse(QtCore.QPointF(s / 2, s / 2), s * 0.12, s * 0.12)
    else:
        pt.drawArc(rect, 200 * 16, 140 * 16)  # 闭眼：下弧
        _line(pt, s * 0.20, s * 0.30, s * 0.80, s * 0.72)  # 斜划线
    pt.end()
    return QtGui.QIcon(pix)
