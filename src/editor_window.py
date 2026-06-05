"""主窗口（PS 式布局）：左=竖排工具栏(移动/画笔/橡皮/魔棒)，中=画布，右=功能面板(画笔/抠图/图层)。

阶段2 验证：导入图、缩放/平移、图层上画笔/橡皮、OpenCV 魔棒选区→去背景/抠出、合成导出 PNG、多图层压力测试。
图层 = QImage(RGBA) + ImageLayerItem(直接绘 QImage，画笔只重绘脏矩形)；OpenCV 经 image_ops 在 numpy 上算。
"""
from __future__ import annotations

import math
import os
import random
import time

import numpy as np
import PySide6QtAds as ads
from PySide6 import QtCore, QtGui, QtWidgets

import ai_panel
import asset_lib
import chat_panel
import config
import icons
import image_ops
import pdf_import
import seg_client
import svg_io
import theme
from canvas_view import CanvasView
from editor_assets import AssetsMixin
from editor_connectors import ConnectorsMixin
from editor_export import ExportMixin
from editor_layers import LayersMixin
from editor_selection import SelectionMixin
from editor_text import TextMixin
from editor_vector import VectorMixin
from layer_item import ImageLayerItem
from ruler_widget import RULER_THICK, RulerWidget

DEFAULT_CANVAS = (2000, 1400)
HISTORY_CAP = 15  # 撤销步数上限（每步复制各层图像，过多会吃内存）


def app_icon_path():
    """NanoPro.ico 的绝对路径（开发=仓库根；打包=_MEIPASS 根，需在 spec datas 里带上）；缺失返回 None。"""
    import sys
    base = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base:
        p = os.path.join(base, "NanoPro.ico")
        if os.path.exists(p):
            return p
    return None
LAYOUT_VERSION = 7  # 面板布局存档版本；改这个数会让旧存档失效、回到默认停靠（v5: 右面板加最小宽+合理初始宽+布局记忆迁 ~/.sciedit/layout.json；v6: 新增「历史记录」停靠面板；v7: 新增「矢量属性」停靠面板，作废旧存档以免新面板被旧 dock_state 隐藏）
CANVAS_PRESETS = [  # 新建空白预设
    ("1K  1024×1024", (1024, 1024)),
    ("2K  2048×2048", (2048, 2048)),
    ("4K  4096×4096", (4096, 4096)),
    ("A4  300dpi 2480×3508", (2480, 3508)),
    ("A3  300dpi 3508×4961", (3508, 4961)),
    ("1080p  1920×1080", (1920, 1080)),
    ("自定义…", None),
]


class _WheelGuard(QtCore.QObject):
    """全局事件过滤器：未聚焦的下拉框/数字框忽略滚轮——避免用户滚动面板时误改参数（分辨率/比例/张数/字号等），
    对齐 PS（滚轮不会动下拉值，除非你点开它）。同时把滚轮转发给最近的滚动区，让面板照常上下滚。"""

    def eventFilter(self, obj, ev):
        if (ev.type() == QtCore.QEvent.Type.Wheel
                and isinstance(obj, (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox))
                and not obj.hasFocus()):
            w = obj.parentWidget()
            while w is not None and not isinstance(w, QtWidgets.QAbstractScrollArea):
                w = w.parentWidget()
            if w is not None:
                try:
                    QtWidgets.QApplication.sendEvent(w.viewport(), ev)  # 转发 → 面板滚动
                except Exception:
                    pass
            return True  # 吃掉原事件 → 下拉/数字值不变
        return False


def _swatch_css(bg: str, fg: str) -> str:
    """色板按钮统一样式：动态底色 + 文字色 + 保留描边/圆角（不再退化成方块）。三处色板共用。"""
    bd = theme.colors()["button_border"]
    return (f"QPushButton{{background:{bg}; color:{fg}; border:1px solid {bd};"
            f" border-radius:6px; min-height:18px;}}")


class LayerRow(QtWidgets.QWidget):
    """图层面板一行（PS 式）：👁 显隐 + 大缩略图 + 名称 + 右侧锁；双击重命名；激活层高亮。
    层级调整(▲▼)/删除/勾选打组收进【右键菜单】，常用操作走面板底部图标栏（更贴 PS）。"""

    THUMB = 56  # PS 式大缩略图

    def __init__(self, editor, layer: dict, thumb: QtGui.QPixmap, indent: bool = False, marked: bool = False):
        super().__init__()
        self.setObjectName("layerRow")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self._editor = editor
        self._layer = layer
        self._marked = marked
        c = theme.colors()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(18 if indent else 6, 3, 6, 3)  # 组成员缩进
        lay.setSpacing(6)
        self.eye = QtWidgets.QToolButton()
        self.eye.setAutoRaise(True); self.eye.setCheckable(True)
        self.eye.setChecked(layer.get("visible", True))
        self.eye.setIconSize(QtCore.QSize(16, 16)); self.eye.setFixedSize(22, 24)
        self.eye.setIcon(icons.eye_icon(self.eye.isChecked(), c["text"]))
        self.eye.setToolTip("显示 / 隐藏该层（隐藏的层不导出）")
        self.eye.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.eye.toggled.connect(lambda v: self.eye.setIcon(icons.eye_icon(v, theme.colors()["text"])))
        self.eye.clicked.connect(lambda *_: editor._set_layer_visible(layer, self.eye.isChecked()))
        thumb_lbl = QtWidgets.QLabel()
        thumb_lbl.setObjectName("layerThumb")  # 边框/底色/圆角走主题 QSS(#layerThumb)
        thumb_lbl.setFixedSize(self.THUMB, self.THUMB)
        thumb_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        thumb_lbl.setPixmap(thumb)
        mk_txt = "  ◼" if marked else ""  # 已勾选打组的层在名称后加标记（◻ 行内按钮已撤，勾选走右键菜单）
        self.name_lbl = QtWidgets.QLabel(layer["name"] + mk_txt)
        self.name_lbl.setMinimumWidth(20)  # 名称可被压缩，给右侧锁让位
        self.name_lbl.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.lock = QtWidgets.QToolButton()
        self.lock.setAutoRaise(True); self.lock.setCheckable(True)
        self.lock.setChecked(layer.get("locked", False))
        self.lock.setIconSize(QtCore.QSize(16, 16)); self.lock.setFixedSize(22, 24)
        self.lock.setIcon(icons.lock_icon(self.lock.isChecked(), c["text"]))
        self.lock.setToolTip("锁定该层（锁后不能移动/涂改，常用于锁住底图）")
        self.lock.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.lock.toggled.connect(lambda v: self.lock.setIcon(icons.lock_icon(v, theme.colors()["text"])))
        self.lock.clicked.connect(lambda *_: editor._set_layer_locked(layer, self.lock.isChecked()))
        lay.addWidget(self.eye)
        lay.addWidget(thumb_lbl)
        lay.addWidget(self.name_lbl, 1)
        lay.addWidget(self.lock)  # 锁紧贴右边（PS 锁在行尾）

    def mousePressEvent(self, e):
        # Ctrl 点击图层 → 载入该层像素为选区（PS 载入图层选区）；Ctrl+Shift 加选 / Ctrl+Alt 减选
        mods = e.modifiers()
        if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
            mode = ("add" if mods & QtCore.Qt.KeyboardModifier.ShiftModifier
                    else "subtract" if mods & QtCore.Qt.KeyboardModifier.AltModifier else "new")
            ed, layer = self._editor, self._layer
            QtCore.QTimer.singleShot(0, lambda: ed._load_layer_as_selection(layer, mode))  # 延后执行：避免在本行事件里 _refresh_layers 销毁自身
            e.accept(); return
        # 普通左键：本行接管（setItemWidget 让列表收不到 press，原生拖拽失效）→ 先选中本行 + 记录拖拽起点。
        # Shift（范围多选）交回列表默认逻辑。
        if e.button() == QtCore.Qt.MouseButton.LeftButton and not (mods & QtCore.Qt.KeyboardModifier.ShiftModifier):
            self._select_self()
            self._press_pos = e.position().toPoint()
            e.accept(); return
        self._press_pos = None
        super().mousePressEvent(e)

    def _select_self(self):
        """把本行设为列表当前项（触发 currentRowChanged → _on_layer_row 激活该层）。"""
        lst = self._editor.layer_list
        for i in range(lst.count()):
            if lst.itemWidget(lst.item(i)) is self:
                lst.setCurrentItem(lst.item(i)); break

    def mouseMoveEvent(self, e):
        # 左键按住并拖过阈值 → 行内自起 QDrag 做图层重排（弥补 setItemWidget 吞掉的列表原生拖拽）
        if (e.buttons() & QtCore.Qt.MouseButton.LeftButton) and getattr(self, "_press_pos", None) is not None:
            if (e.position().toPoint() - self._press_pos).manhattanLength() >= QtWidgets.QApplication.startDragDistance():
                self._press_pos = None
                self._start_layer_drag()
                return
        super().mouseMoveEvent(e)

    def _start_layer_drag(self):
        lst = self._editor.layer_list
        uid = self._layer.get("uid")
        if uid is None:
            return
        lst._drag_uid = uid  # dropEvent 用它定位源行（最稳，不靠 currentItem）
        md = QtCore.QMimeData()
        md.setData(lst.LAYER_MIME, str(uid).encode("utf-8"))  # 让列表 dragEnter/Move 放行本次拖拽
        drag = QtGui.QDrag(self)
        drag.setMimeData(md)
        pm = self.grab()  # 拖拽影像=本行外观（像 PS 拖着一行走）
        drag.setPixmap(pm)
        drag.setHotSpot(QtCore.QPoint(20, pm.height() // 2))
        drag.exec(QtCore.Qt.DropAction.MoveAction)

    def contextMenuEvent(self, e):
        # 右键菜单：上移/下移层级、勾选打组、重命名、删除（行内按钮收进这里，对齐 PS 靠拖拽+底栏）
        ed, layer = self._editor, self._layer
        m = QtWidgets.QMenu(self)
        m.addAction("置顶", lambda: ed._layer_z("front", layer))
        m.addAction("上移一层", lambda: ed._move_layer(layer, +1))
        m.addAction("下移一层", lambda: ed._move_layer(layer, -1))
        m.addAction("置底", lambda: ed._layer_z("back", layer))
        m.addSeparator()
        act_mark = m.addAction("取消勾选打组" if self._marked else "勾选以打组")
        act_mark.triggered.connect(lambda *_: ed._toggle_mark(layer))
        m.addAction("重命名…", lambda: ed._rename_layer(layer))
        if layer.get("kind") != "vector":  # 非破坏图层蒙版（栅格/图片/文字层）
            m.addSeparator()
            m.addAction("从选区生成蒙版", lambda: ed._mask_from_selection(layer))
            if layer.get("mask") is not None:
                m.addAction("删除蒙版", lambda: ed._delete_mask(layer))
        m.addSeparator()
        m.addAction("删除该层", lambda: ed._delete_specific_layer(layer))
        m.exec(e.globalPos())

    def mouseDoubleClickEvent(self, e):
        self._editor._rename_layer(self._layer)

    def set_active(self, on: bool):
        # 高亮走主题 QSS(#layerRow[active])：设动态属性 + repolish。
        # 始终占 3px 左边框（QSS 里 transparent 占位），仅换色 → 切换不抖动；切深浅主题也自动重着色。
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class GroupHeaderRow(QtWidgets.QWidget):
    """图层面板的组头行：▾/▸ 折叠 + 👁 整组显隐 + 组名(成员数)。"""

    def __init__(self, editor, gid: str, name: str, count: int, collapsed: bool, any_visible: bool):
        super().__init__()
        self.setObjectName("groupHeader")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        c = theme.colors()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2); lay.setSpacing(4)
        fold = QtWidgets.QToolButton(); fold.setAutoRaise(True); fold.setFixedSize(20, 22)
        fold.setText("▸" if collapsed else "▾"); fold.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        fold.clicked.connect(lambda *_: editor._toggle_collapse(gid))
        eye = QtWidgets.QToolButton(); eye.setAutoRaise(True); eye.setCheckable(True); eye.setChecked(any_visible)
        eye.setIconSize(QtCore.QSize(16, 16)); eye.setFixedSize(22, 22)
        eye.setIcon(icons.eye_icon(any_visible, c["text"])); eye.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        eye.clicked.connect(lambda *_: editor._set_group_visible(gid, not any_visible))
        lbl = QtWidgets.QLabel(f"▣ {name} ({count})"); lbl.setObjectName("groupName")  # 加粗走主题 QSS
        lay.addWidget(fold); lay.addWidget(eye); lay.addWidget(lbl, 1)
        # 背景/圆角走主题 QSS(#groupHeader)，不再内联 → 切深浅主题自动跟随


class InlineTextEdit(QtWidgets.QTextEdit):
    """画布上就地打字的文本框：Ctrl+Enter 或失焦=完成，Esc=取消，回车=换行（像 NanoPro）。"""

    def __init__(self, on_commit, on_cancel, parent):
        super().__init__(parent)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Escape:
            self._on_cancel(); return
        if e.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) \
                and (e.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
            self._on_commit(); return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._on_commit()


class ResizeHandle(QtWidgets.QGraphicsRectItem):
    """激活层右下角缩放手柄：作激活层子项(跟随位置/缩放)，自身屏幕恒定大小；拖动等比缩放该层。"""

    def __init__(self, editor):
        super().__init__(-7, -7, 14, 14)
        self._editor = editor
        c = theme.colors()
        self.setBrush(QtGui.QColor(c["accent"]))
        self.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.5))
        self.setZValue(10002)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.LeftButton)
        self.hide()

    def mousePressEvent(self, e):
        self._editor._begin_resize(); e.accept()

    def mouseMoveEvent(self, e):
        self._editor._do_resize(e.scenePos()); e.accept()

    def mouseReleaseEvent(self, e):
        self._editor._end_resize(); e.accept()


class AssetListWidget(QtWidgets.QListWidget):
    """本地素材库缩略图列表：把当前项的本地路径作为拖拽数据，拖到画布建图层。
    路径存在 item 的 UserRole；拖出时附 file:// URL + 自定义格式 application/x-nanopro-asset。"""

    def startDrag(self, actions):
        # IconMode 下 Qt 不保证拖前已选中手指下那项 → 用光标位置兜底，避免拖错图
        it = self.itemAt(self.mapFromGlobal(QtGui.QCursor.pos())) or self.currentItem()
        if it is None:
            return
        path = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path:
            return
        md = QtCore.QMimeData()
        md.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
        md.setData("application/x-nanopro-asset", str(path).encode("utf-8"))
        drag = QtGui.QDrag(self)
        drag.setMimeData(md)
        ic = it.icon()
        if not ic.isNull():
            drag.setPixmap(ic.pixmap(self.iconSize()))
        drag.exec(QtCore.Qt.DropAction.CopyAction)


class PluginStar(QtWidgets.QToolButton):
    """右侧悬浮的 ✨ 插件星：点击开/关插件面板，可拖动（移植 ai.js 的 ✨ 轨道图标交互）。"""

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.setText("✨")
        self.setToolTip("插件 · AI 生成（点击开/关，可拖动）")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 38)
        self.setObjectName("pluginStar")  # 配色走主题 QSS(#pluginStar)，切深浅主题随强调色变
        self._on_click = on_click
        self._press = None; self._orig = None; self._moved = False

    def reposition(self):  # 贴右边缘、垂直靠上 1/3 处
        p = self.parentWidget()
        if p:
            self.move(p.width() - self.width() - 10, max(60, p.height() // 3))
            self.raise_()

    def mousePressEvent(self, e):
        self._press = e.globalPosition().toPoint(); self._orig = self.pos(); self._moved = False; e.accept()

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        d = e.globalPosition().toPoint() - self._press
        if self._moved or d.manhattanLength() > 4:
            self._moved = True
            p = self.parentWidget(); np = self._orig + d
            np.setX(max(0, min(np.x(), p.width() - self.width())))
            np.setY(max(0, min(np.y(), p.height() - self.height())))
            self.move(np)
        e.accept()

    def mouseReleaseEvent(self, e):
        if not self._moved and self._on_click:
            self._on_click()  # 点击(未拖动)→开/关面板
        self._press = None; e.accept()


class FloatingToolWindow(QtWidgets.QWidget):
    """PS 式自由浮动工具窗（插件面板用）。不进 ADS 停靠系统 → 拖动绝不弹落点 overlay。
    关闭(X)=隐藏收回 ✨ 星、不销毁，内容/状态保留，下次点 ✨ 星原样弹出。"""

    def __init__(self, parent, title, icon=None):
        # Tool 窗：浮在主窗之上、不占任务栏、细标题栏——与 PS 浮动面板一致
        super().__init__(parent, QtCore.Qt.WindowType.Tool)
        self.setWindowTitle(title)
        if icon is not None:
            self.setWindowIcon(icon)
        self._v = QtWidgets.QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)

    def set_content(self, w):
        self._v.addWidget(w)

    def closeEvent(self, e):
        e.ignore()  # 关闭=收回(不销毁)
        self.hide()


class FlyoutToolButton(QtWidgets.QToolButton):
    """PS 式工具组按钮（flyout）：把若干同槽工具收进一个按钮。
    左键=触发【上次选中】的工具（=当前 defaultAction）；右键=弹小菜单（图标+名称）选用哪个；
    选中后按钮图标换成所选工具；右下角画小三角表示这是一个工具组。
    互斥/选中态由各 QAction 自身（已加进外层 QActionGroup）维持，本按钮只跟随显示。"""

    def __init__(self, actions, color="#cdd2db", parent=None):
        super().__init__(parent)
        self._actions = list(actions)  # 组内 QAction（首个=默认）
        self._tri_color = color
        self.setAutoRaise(True)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        # 不挂 setMenu → 左键短按只触发 defaultAction，不弹箭头菜单；弹菜单只走右键 contextMenuEvent
        self.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.DelayedPopup)
        if self._actions:
            self.setDefaultAction(self._actions[0])

    def set_current(self, action):
        # 选中组内某工具 → 该 action 成为默认动作（按钮图标随之变成所选工具）
        if action in self._actions:
            self.setDefaultAction(action)

    def contextMenuEvent(self, e):
        menu = QtWidgets.QMenu(self)
        menu.setToolTipsVisible(True)  # 悬停显示各工具中文说明（PS 式）
        for a in self._actions:
            menu.addAction(a)  # QAction 自带 icon + 中文名(text) + 长说明(toolTip) + triggered→set_tool
        menu.exec(e.globalPos())
        e.accept()

    def paintEvent(self, e):
        super().paintEvent(e)
        pt = QtGui.QPainter(self)
        c = theme.colors()  # 实时取主题色 → 切深浅主题角标随强调色变（不残留旧色）
        icons.paint_flyout_badge(pt, self.width(), self.height(), c["accent"], c["on_accent"])
        pt.end()


class DragLayerList(QtWidgets.QListWidget):
    """图层面板列表：支持鼠标拖拽排序（像 PS/Illustrator 拖行调层级）。
    因每行用 setItemWidget(LayerRow) 自绘，不能用 Qt 默认 InternalMove（它序列化重建 item 会丢 widget）→
    重写 dropEvent：不调 super().dropEvent，改回调 editor._reorder_layer(src_uid, dst_uid, before) 重排
    self.layers + 重设 zValue + 入撤销 + _refresh_layers。"""

    LAYER_MIME = "application/x-nanopro-layer"  # 行内自起的图层重排拖拽标识

    def __init__(self, editor):
        super().__init__()
        self._editor = editor
        self._drag_uid = None  # 拖拽起手记下被拖行的 uid（不靠 currentItem，避免 _refresh_layers 重建后失准）
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)

    def startDrag(self, actions):
        it = self.itemAt(self.mapFromGlobal(QtGui.QCursor.pos())) or self.currentItem()
        self._drag_uid = it.data(QtCore.Qt.ItemDataRole.UserRole) if it is not None else None
        super().startDrag(actions)

    def dragEnterEvent(self, e):
        # setItemWidget(LayerRow) 会吞掉列表自带拖拽 → 改由 LayerRow 自起 QDrag(带本格式)；这里放行它
        if e.mimeData().hasFormat(self.LAYER_MIME):
            e.setDropAction(QtCore.Qt.DropAction.MoveAction); e.accept()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(self.LAYER_MIME):
            e.setDropAction(QtCore.Qt.DropAction.MoveAction); e.accept()  # 显示放置指示并允许 drop
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        pos = e.position().toPoint()
        dst = self.itemAt(pos)
        # 源行：优先用 startDrag 记下的 uid（稳）；兜底 currentItem
        src_uid = self._drag_uid
        if src_uid is None:
            cur = self.currentItem()
            src_uid = cur.data(QtCore.Qt.ItemDataRole.UserRole) if cur is not None else None
        if dst is None:
            e.ignore(); return
        dst_uid = dst.data(QtCore.Qt.ItemDataRole.UserRole)
        if src_uid is not None and dst_uid is not None and src_uid == dst_uid:
            e.ignore(); return
        if src_uid is None or dst_uid is None:  # 组头行无 uid → 不参与拖拽
            e.ignore(); return
        # 落点在目标行的上半还是下半（决定插到目标之前还是之后）。注意列表是「顶层在上」倒序显示，
        # 之前/之后的语义交由 editor._reorder_layer 统一处理（它按显示顺序解释 before）。
        rect = self.visualItemRect(dst)
        before = pos.y() < rect.center().y()
        e.accept()  # 不调 super().dropEvent，避免 Qt 自行搬运毁 widget
        self._drag_uid = None
        self._editor._reorder_layer(src_uid, dst_uid, before)


class EditorWindow(QtWidgets.QMainWindow, ConnectorsMixin, ExportMixin, AssetsMixin, TextMixin, LayersMixin, SelectionMixin, VectorMixin):
    _windows: list = []  # 持有多窗口引用，防止被 GC

    def __init__(self):
        super().__init__()
        EditorWindow._windows.append(self)
        self.setWindowTitle("SciEdit 科研图编辑器")
        _ic = app_icon_path()  # 任务栏/标题栏图标
        if _ic:
            self.setWindowIcon(QtGui.QIcon(_ic))
        self.resize(1360, 860)
        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = CanvasView(self.scene, self)
        self.view._editor = self  # drawForeground 读 _active_guides 用（比 parent() 在 reparent 后更稳）
        _app = QtWidgets.QApplication.instance()  # 全局滚轮拦截：滚轮不再误改下拉/数字框的值
        if _app is not None and not getattr(_app, "_wheel_guard", None):
            _app._wheel_guard = _WheelGuard(_app)
            _app.installEventFilter(_app._wheel_guard)
        # Qt-ADS：中央画布 + 右侧可自由拖拽/浮动/换组/分列的停靠面板（像 PS）
        # 配置须在创建 manager 前设(静态)。关键：强制浮动窗用 ADS 自带标题栏——
        # 否则 Windows 用系统原生标题栏，拖它不触发停靠，浮出去就吸不回来。
        _F = ads.CDockManager.eConfigFlag
        ads.CDockManager.setConfigFlag(_F.FloatingContainerForceQWidgetTitleBar, True)
        ads.CDockManager.setConfigFlag(_F.OpaqueSplitterResize, True)
        ads.CDockManager.setConfigFlag(_F.DockAreaHasUndockButton, True)
        ads.CDockManager.setConfigFlag(_F.FocusHighlighting, False)
        self.dock_manager = ads.CDockManager(self)
        self.setCentralWidget(self.dock_manager)
        # 画布外包一层容器：QGridLayout 把横/纵标尺贴在 view 上下左，左上角占位（PS 式标尺）
        _canvas_wrap = QtWidgets.QWidget()
        _grid = QtWidgets.QGridLayout(_canvas_wrap)
        _grid.setContentsMargins(0, 0, 0, 0)
        _grid.setSpacing(0)
        self._ruler_top = RulerWidget(self.view, QtCore.Qt.Orientation.Horizontal, _canvas_wrap)
        self._ruler_left = RulerWidget(self.view, QtCore.Qt.Orientation.Vertical, _canvas_wrap)
        self._ruler_corner = QtWidgets.QFrame(_canvas_wrap)
        self._ruler_corner.setFixedSize(RULER_THICK, RULER_THICK)
        self._ruler_corner.setStyleSheet("background:%s;" % theme.colors()["menu_bar"])
        self._ruler_top.on_drop = self._on_ruler_drop
        self._ruler_left.on_drop = self._on_ruler_drop
        _grid.addWidget(self._ruler_corner, 0, 0)
        _grid.addWidget(self._ruler_top, 0, 1)
        _grid.addWidget(self._ruler_left, 1, 0)
        _grid.addWidget(self.view, 1, 1)
        _central = ads.CDockWidget(self.dock_manager, "画布")
        _central.setWidget(_canvas_wrap)
        _central.setFeature(ads.CDockWidget.DockWidgetFeature.NoTab, True)
        self.dock_manager.setCentralWidget(_central)
        self._central_dw = _central  # 持引用：其 dockAreaWidget 是 root 横向 splitter 直接子项，用于设默认右列宽
        self._rulers_visible = config.get_show_rulers()
        for r in (self._ruler_top, self._ruler_left, self._ruler_corner):
            r.setVisible(self._rulers_visible)
        self._ads_base_css = self.dock_manager.styleSheet()  # ADS 默认样式（保留按钮图标等）
        self._apply_ads_theme()

        self.layers: list[dict] = []
        self.connectors: list = []     # 智能连接线（ConnectorItem，绑定两端图层 uid；对象移动自动跟随）
        self._thumb_cache: dict = {}   # uid → (image.cacheKey(), QPixmap)：图层缩略图缓存，免每次刷新都全分辨率重缩放
        self.active: dict | None = None
        self.selected_layers: list[dict] = []  # 对齐/分布的选择集；约定 active 始终在内，单选时 ==[active]
        self._suspend_sel_sync = False          # 守卫：避免 _refresh_layers 重选与选择回调互相递归
        self._suspend_opacity_ui = False        # 守卫：同步不透明度滑块到 active 层时屏蔽 valueChanged 回写
        self._active_guides: list = []          # 拖动时智能参考线（洋红虚线）：[{orient,pos,span}]，drawForeground 读取
        self.canvas_size: tuple[int, int] | None = None
        self.brush_color = QtGui.QColor("#e23b3b")
        self.text_color = QtGui.QColor("#000000")
        self._text_editor = None       # 画布上就地打字的 QTextEdit
        self._text_edit_layer = None   # 正在编辑的文字层（None=新建）
        self._suspend_text_live = False  # 载入面板时抑制即时重渲染（防回填触发）
        self._text_live_pushed = False   # 当前文字层本轮样式微调是否已记过一次历史
        self._suspend_vec_sel = False    # B2 守卫：回填矢量属性面板/批量 removeItem 时抑制 selectionChanged 递归
        self._vec_live_pushed = False    # B3：矢量描边宽/字体字号/配色 spin 本轮连续 valueChanged 是否已 push 过一次历史
        self._vec_text_edit_before = None  # B3：内联改字进入前的文本（空编辑回退判定）
        # B5：锚点 overlay（独立于 layer 体系的临时 scene item，绝不入 self.layers/快照）。
        # None=未激活；激活时 = {"items":[...], "target":path_item, "layer":vlayer,
        #   "subpaths":[...path_to_anchors...], "sel":set((sp_i,a_i)), "drag_pushed":bool}
        self._node_overlay = None
        # B5：钢笔状态。None=未激活；激活时 = {"anchors":[Anchor...], "preview_items":[...],
        #   "rubber":path_item, "dots":[...], "press_pos":QPointF|None, "dragging":bool}
        self._pen_state = None
        self._text_box_w = None          # 当前编辑的定宽文本框宽度（None=按内容自适应）
        self.source_dpi = None           # 导入底图的 DPI（pHYs），None=未知
        self.source_name = None          # 导入底图文件名（不含扩展名），导出默认名用
        # 图层分组（视图层分组，移植 layers.js groupMap/collapsed/marked）
        self._layer_uid = 0              # 图层稳定 id 计数（供分组跨撤销/刷新稳定引用）
        self._group_seq = 0              # 组 id 计数
        self._group_names = {}           # gid → 组名
        self._collapsed = set()          # 已折叠的 gid
        self._marked = set()             # 已勾选待打组的图层 uid
        self._text_scene_pos = QtCore.QPointF()
        self.selection_mask: np.ndarray | None = None
        # 历史时间线（PS 历史面板）：双撤销/重做栈物理合并成一条线性快照列表 + 当前指针。
        # 每条 = {"snap": <_snapshot()>, "label": str}。_hist_index = 旧"撤销栈长-1" = 当前指针。
        # 撤销区 = _history[:_hist_index+1]（指针含本身，含初始基线）；重做区 = _history[_hist_index+1:]（按 redo 消费顺序，下一个待重做在最前）。
        # undo = 指针-1，redo = 指针+1，做新操作 = 截断 _history[_hist_index+1:] 后 append（语义等价旧 _history.append + _redo.clear）。
        self._history: list[dict] = []
        self._hist_index: int = -1   # -1 = 尚无历史（等价旧 _history 为空）
        self._suspend_history = False
        self._seg_worker = None          # AI 抠图/拆解后台线程（仿 ai_panel 的 self._worker=None）
        self._seg_dialog = None          # AI 抠图忙碌进度弹窗（QProgressDialog；done/取消/失败三路都要关）
        self._seg_epoch = 0              # 辨别"被取消/被新一次抠图顶替的旧 worker"（取消时自增使在途 worker 失效）
        # 选区引擎状态
        self._sel_mode = "new"           # new / add / subtract
        self._last_selection = None      # 最后一次取消掉的选区，供"重新选择"(Shift+Ctrl+D)恢复
        self._sel_points: list = []      # 套索拖动中的场景点
        self._rect_p0 = None             # 矩形起点
        self._brush_mask = None          # 选区画笔累积掩码（层内坐标）
        self._brush_last = None          # 选区画笔上一采样点
        self._brush_preview = None       # 涂抹中的实时预览描边（active item 子项，松手前可见）
        self._brush_preview_clock = QtCore.QElapsedTimer()  # 节流计时（move 高频时跳过大多数轮廓重算）
        self._sel_preview = None         # 拖动中的预览路径项
        self._ants = None                # 蚂蚁线（黑虚线，动画）
        self._ants_base = None           # 蚂蚁线底（白实线）
        self._ants_offset = 0
        self._ants_timer = QtCore.QTimer(self)
        self._ants_timer.timeout.connect(self._tick_ants)
        self.assets: list[QtGui.QImage] = []   # 素材库（抠出物）
        self._outline = QtWidgets.QGraphicsRectItem()  # 当前激活层青色虚线框（作子项跟随该层）
        _pen = QtGui.QPen(QtGui.QColor("#22d3ee"), 0, QtCore.Qt.PenStyle.DashLine)
        _pen.setCosmetic(True)
        self._outline.setPen(_pen)
        self._outline.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        self._outline.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._outline.setZValue(1)
        self._outline.hide()
        self._resize_handle = ResizeHandle(self)  # 激活层右下角缩放手柄
        self._resizing = False
        self._resize_pushed = False  # 本次缩放是否已记历史（真正拖动才记，裸点手柄不记，对齐 didChange）
        self._last_scene = QtCore.QPointF()

        self._build_menu()
        self._build_top_bar()
        self._build_left_tools()
        self._build_right_dock()
        self._build_options_bar()  # PS 式【唯一】顶部上下文选项栏：撤销/重做(左)+按工具切页（需 right_dock 的滑块已存在以建镜像）
        self._build_statusbar()
        self.view.fpsChanged.connect(lambda f: self.fps_label.setText(f"FPS {f:5.1f}"))
        self.view.zoomChanged.connect(self._update_zoom_label)
        # 标尺同步：缩放 + 两条滚动条 + 鼠标游标
        self.view.zoomChanged.connect(self._sync_rulers)             # 缩放 → 两条标尺都重建
        self.view.horizontalScrollBar().valueChanged.connect(self._sync_ruler_h)  # 横滚 → 只刷上标尺
        self.view.verticalScrollBar().valueChanged.connect(self._sync_ruler_v)    # 竖滚 → 只刷左标尺
        self.view.cursorScene.connect(self._on_cursor_scene)
        self.view.paintPress.connect(self._paint_press)
        self.view.paintMove.connect(self._paint_move)
        self.view.paintRelease.connect(self._paint_end)
        self.view.textBox.connect(self._place_text_box)
        self.view.editRequested.connect(self._edit_text_at)
        self.view.escPressed.connect(self._clear_selection)
        self.view.nudge.connect(self._nudge_active)
        self.view.deleteRequested.connect(self.delete_layer)
        self.view.extractRequested.connect(self._extract_shortcut)
        self.view.layerViaCopy.connect(self._layer_via_copy)
        self.view.assetDropped.connect(self._place_asset)  # 素材库拖到画布 → drop 处建图层
        self.view.measureChanged.connect(self._on_measure_changed)  # 测量线变化 → 更新选项栏读数
        # B5 钢笔 / 锚点工具事件
        self.view.penPress.connect(self._pen_press)
        self.view.penDragTo.connect(self._pen_drag_to)
        self.view.penRelease.connect(self._pen_release)
        self.view.penCommit.connect(self._pen_commit)
        self.view.penHover.connect(self._pen_hover)
        self.view.nodeClick.connect(self._node_click)
        self.view.nodeDoubleClick.connect(self._node_double_click)
        # B2：矢量元素级选择（与【层】级 self.active 解耦）→ 刷新「矢量属性」面板。QGraphicsView move 工具点选自然填 scene.selectedItems()。
        self.scene.selectionChanged.connect(self._on_vec_selection_changed)
        self.set_tool("move")
        self._load_asset_dir()  # 启动时若已连接过素材文件夹则自动加载（仿 _load_conn 时机）
        self._plugin_star = PluginStar(self, self._toggle_ai_panel)  # ✨ 插件悬浮星（点开 AI，可拖动）
        self._plugin_star.show()
        self._restore_session()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_plugin_star"):
            self._plugin_star.reposition()

    def showEvent(self, e):
        super().showEvent(e)
        if hasattr(self, "_plugin_star"):
            self._plugin_star.reposition()

    # ---------- 布局记忆：~/.sciedit/layout.json（base64，纯几何/拓扑，非敏感）----------
    @staticmethod
    def _ba_to_b64(ba: QtCore.QByteArray) -> str:
        return bytes(ba.toBase64()).decode("ascii")

    @staticmethod
    def _b64_to_ba(s: str) -> QtCore.QByteArray:
        return QtCore.QByteArray.fromBase64(QtCore.QByteArray(s.encode("ascii")))

    def _restore_session(self):  # 恢复窗口大小（几何可在 show 前恢复）；停靠/浮窗 show 后恢复
        self._applied_default_split = False  # 守卫：默认右列宽只设一次，不覆盖已恢复/用户拖动的宽度
        data = config.load_layout()
        self._layout_data = data if data.get("version") == LAYOUT_VERSION else {}
        geo = self._layout_data.get("geometry")
        if geo:
            try:
                self.restoreGeometry(self._b64_to_ba(geo))
            except Exception as exc:  # fail-loud：几何损坏不静默，用默认窗口大小
                self._layout_data = {}
                msg = str(exc)  # 先取出字符串：except 退出后 exc 被删，延迟 lambda 直接引用会 NameError(审核)
                QtCore.QTimer.singleShot(0, lambda m=msg: self.op_label.setText(f"窗口几何存档损坏，已用默认（{m}）"))
        # 停靠状态必须在窗口 show() 之后恢复，否则 ADS 浮动/标签容器会错位（素材库被弹成浮动）
        QtCore.QTimer.singleShot(0, self._restore_dock_state)

    def _right_area_spans(self):
        # 收集右侧【可见·已停靠·非浮动】面板 dock area 的全局 (left_x, right_x)，按 area 去重。
        # 给右区宽度/双列判定共用；浮动面板(顶层窗口非主窗)不计入右区列。
        spans = {}
        for dw in getattr(self, "_dw_panels", []):
            try:
                if dw.isClosed() or not dw.isVisible():
                    continue
                area = dw.dockAreaWidget()
                if area is None or area.window() is not self:
                    continue
                gx = area.mapToGlobal(QtCore.QPoint(0, 0)).x()
                spans[id(area)] = (gx, gx + area.width())
            except Exception:
                continue
        return list(spans.values())

    def _right_region_width(self) -> int:
        # 整个右区宽度 = 右侧所有面板 dock area 全局 x 跨度（min left → max right）。
        # 单列=一列宽；双列=两列合计。比量单个 _dw_sel area 宽稳健（不把双列的半宽列误判过窄）。
        spans = self._right_area_spans()
        if not spans:
            return 0  # 取不到 → 返回 0 触发默认兜底，而非误判存活
        return int(max(r for _, r in spans) - min(l for l, _ in spans))

    def _restore_dock_state(self):
        data = self._layout_data
        st = data.get("dock_state")
        restored = False
        if st:
            try:
                if self.dock_manager.restoreState(self._b64_to_ba(st)):
                    restored = True
            except Exception as exc:  # fail-loud：存档损坏 → 回退默认并提示，不留空白布局
                self.op_label.setText(f"布局存档损坏，已用默认（{exc}）")
        if restored:
            # 健全性检查：若恢复后【整个右区】退化到过窄（旧坏存档/跨分辨率），回退到修好的默认宽。
            # 注意：必须量整个右区宽，不能量 _dw_sel 所在单个 dock area 宽——
            # 双列时 _dw_sel 只占其中一列(约半宽)，量单列会对所有双列存档恒判"过窄"而误伤双列。
            right_w = self._right_region_width()
            if right_w < 280:  # 最小宽 300 已基本兜底，此处再防跨分辨率/ADS 异常退化到近最小宽
                restored = False
                self.op_label.setText("检测到右面板过窄，已恢复合理默认宽度")
        if not restored:
            self._apply_default_right_width()  # 走"修好的新默认"：右列 ~320px
        self._restore_float_windows(data)

    def _apply_default_right_width(self):
        # 给右列一个合理初始宽度（central 区是 root 横向 splitter 直接子项）。只做一次，避免覆盖记忆/用户宽度。
        if getattr(self, "_applied_default_split", False):
            return
        try:
            area = self._central_dw.dockAreaWidget()
            # 右列合理默认宽 320。注：双列由用户存档的真实列宽恢复（健全性检查口径已修，restored 保持 True
            # → 根本不进本函数）；本函数只在冷启动/重置/右区真过窄回退时跑，给单列 320。不再按"双列"
            # 设 640——离屏无法可靠区分默认单列与真双列(几何在 offscreen 不准)，盲设 640 会误挤画布(审核 CRITICAL)。
            right = 320
            left = max(400, self.width() - right - 40)  # 留些余量给左工具栏/边框
            self.dock_manager.setSplitterSizes(area, [left, right])
            self._applied_default_split = True
        except Exception as exc:  # fail-loud：设宽失败提示但不阻塞
            self.op_label.setText(f"设默认面板宽度失败：{exc}")

    def _restore_float_windows(self, data: dict):
        # 恢复 ✨ AI 生成 / 对话 浮窗的几何与可见性；落屏外则夹回可视范围
        for key, win, pos_flag in (
            ("ai", getattr(self, "_ai_window", None), "_ai_positioned"),
        ):  # 对话已并入 AI 浮窗标签页，随 ai 条目一并持久化（不再有独立对话浮窗）
            info = data.get(key)
            if not isinstance(info, dict) or win is None:
                continue
            g = info.get("geom")
            if g:
                try:
                    win.restoreGeometry(self._b64_to_ba(g))
                    self._clamp_into_screen(win)
                    setattr(self, pos_flag, True)  # 已有记忆位置 → 别再弹回 ✨ 星旁覆盖它
                except Exception:
                    continue
            if info.get("visible"):
                win.show(); win.raise_()

    def _clamp_into_screen(self, win):
        # 跨多显示器/分辨率变化时浮窗可能落屏外：夹回主屏可视范围（照 _position_ai_float 的夹取）
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is None:
            return
        x = max(scr.left() + 8, min(win.x(), scr.right() - win.width() - 8))
        y = max(scr.top() + 8, min(win.y(), scr.bottom() - win.height() - 8))
        win.move(int(x), int(y))

    def closeEvent(self, e):  # 记住窗口大小 + 面板排布 + 浮窗位置 → ~/.sciedit/layout.json
        data = {"version": LAYOUT_VERSION}
        try:
            data["geometry"] = self._ba_to_b64(self.saveGeometry())
            data["dock_state"] = self._ba_to_b64(self.dock_manager.saveState())
            for key, win in (("ai", getattr(self, "_ai_window", None)),):  # 对话已并入 ai 标签页
                if win is not None:
                    data[key] = {"visible": bool(win.isVisible()),
                                 "geom": self._ba_to_b64(win.saveGeometry())}
            if not config.save_layout(data):  # fail-loud：写盘失败不静默吞
                print("[layout] 写 ~/.sciedit/layout.json 失败（布局未保存）")
        except Exception as exc:
            print(f"[layout] 保存布局异常（已忽略，不阻塞关窗）：{exc}")
        # 退出前停 AI 抠图 worker：SegWorker 是 parented running QThread，销毁仍 run 的会硬崩(审核 HIGH)。
        # SegWorker 无 stop()(单发阻塞网络)，只 wait(最坏等到超时) + 兜底 terminate。
        w = getattr(self, "_seg_worker", None)
        if w is not None and w.isRunning():
            if not w.wait(3000):
                w.terminate(); w.wait(1000)
        super().closeEvent(e)

    def createPopupMenu(self):
        # 禁用主窗口右键弹出的"工具栏/停靠切换"菜单；面板显隐改用「视图」菜单。
        return None

    def _apply_ads_theme(self):  # ADS 面板样式跟随 app 主题：默认样式 + 主题色覆盖
        self.dock_manager.setStyleSheet(self._ads_base_css + theme.ads_qss(theme.colors()))

    # ---------- 菜单 ----------
    def _build_menu(self):
        m = self.menuBar().addMenu("文件")
        m.addAction("新建空白…", self.new_blank).setShortcut("Ctrl+N")
        m.addAction("画布尺寸…（保留内容）", self.canvas_size_dialog)
        m.addSeparator()
        m.addAction("导入图片…", self.import_image).setShortcut("Ctrl+O")
        m.addAction("导入元素…", self.import_element)
        m.addAction("导入矢量图 (SVG)…", self.import_svg)
        m.addAction("导入 PDF（矢量）…", self.import_pdf)
        m.addAction("新建透明图层", self.new_transparent_layer).setShortcut("Ctrl+Shift+N")
        m.addSeparator()
        m.addAction("导出 PNG…", self.export_png).setShortcut("Ctrl+E")
        m.addAction("导出 TIFF…（投稿）", self.export_tiff)
        m.addAction("导出 SVG…", self.export_svg)
        m.addAction("导出 PDF（矢量）…", self.export_pdf).setShortcut("Ctrl+Shift+E")
        m.addSeparator()
        m.addAction("保存工程…", self.save_project).setShortcut("Ctrl+S")
        m.addAction("加载工程…", self.load_project).setShortcut("Ctrl+Shift+O")
        m.addSeparator()
        m.addAction("适应窗口", self.fit_view).setShortcut("Ctrl+0")
        self._debug_menu = m.addMenu("调试")  # 开发工具收进子菜单，不混在文件操作里
        self._debug_menu.addAction("压力测试：放 N 个图层并连续动", self.stress_test)

        em = self.menuBar().addMenu("编辑")
        c = theme.colors()["text"]
        self._undo_act = em.addAction(icons.tool_icon("undo", c), "撤销", self.undo)
        self._undo_act.setShortcut("Ctrl+Z"); self._undo_act.setToolTip("撤销 (Ctrl+Z)")
        self._redo_act = em.addAction(icons.tool_icon("redo", c), "重做", self.redo)
        self._redo_act.setShortcuts([QtGui.QKeySequence("Ctrl+Y"), QtGui.QKeySequence("Ctrl+Shift+Z")])
        self._redo_act.setToolTip("重做 (Ctrl+Y / Ctrl+Shift+Z)")
        self._undo_act.setEnabled(False)
        self._redo_act.setEnabled(False)
        em.addSeparator()
        re_act = em.addAction("重新选择", self.reselect)  # PS Reselect：恢复上次取消的选区
        re_act.setShortcut("Ctrl+Shift+D")
        re_act.setToolTip("重新选择：恢复最后一次取消掉的选区 (Ctrl+Shift+D)")
        em.addSeparator()
        # Ctrl+C/Ctrl+V 不在此注册快捷键（会抢占就地编辑文字时的复制粘贴）→ 实际按键在 CanvasView.keyPressEvent 处理
        cp_act = em.addAction("复制图层 (Ctrl+C)", self.copy_to_clipboard)
        cp_act.setToolTip("把当前图层复制为图片到剪贴板（可粘回 / 粘到外部应用）")
        pa_act = em.addAction("粘贴 (Ctrl+V)", self.paste_from_clipboard)
        pa_act.setToolTip("把剪贴板里的图片作为新图层粘到画布（外部复制的图也能粘进来）")
        em.addSeparator()
        zsub = em.addMenu("图层层级")  # 置顶/上移/下移/置底（作用活动层）
        _za = zsub.addAction("置顶", lambda: self._layer_z("front")); _za.setShortcut("Ctrl+Shift+]")
        _za = zsub.addAction("上移一层", lambda: self._layer_z("forward")); _za.setShortcut("Ctrl+]")
        _za = zsub.addAction("下移一层", lambda: self._layer_z("backward")); _za.setShortcut("Ctrl+[")
        _za = zsub.addAction("置底", lambda: self._layer_z("back")); _za.setShortcut("Ctrl+Shift+[")

        img_menu = self.menuBar().addMenu("图像")
        img_menu.addAction("亮度/对比度…", self.brightness_contrast_dialog)
        img_menu.addSeparator()
        # 翻转/旋转：作用于选中的矢量元素，或活动图层（矢量绕中心 QTransform；栅格转像素）
        img_menu.addAction("水平翻转", lambda: self._flip_objects(True))
        img_menu.addAction("垂直翻转", lambda: self._flip_objects(False))
        img_menu.addAction("顺时针旋转 90°", lambda: self._rotate_objects(90))
        img_menu.addAction("逆时针旋转 90°", lambda: self._rotate_objects(270))

        view = self.menuBar().addMenu("视图")
        self._view_menu = view
        tgrp = QtGui.QActionGroup(self)
        tgrp.setExclusive(True)
        self._theme_actions = {}
        for name, label in (("dark", "深色主题"), ("light", "浅色主题")):
            a = QtGui.QAction(label, self, checkable=True)
            a.setChecked(theme.current() == name)
            a.triggered.connect(lambda _=False, n=name: self._switch_theme(n))
            tgrp.addAction(a)
            view.addAction(a)
            self._theme_actions[name] = a

        view.addSeparator()
        self._act_rulers = QtGui.QAction("显示标尺", self, checkable=True)
        self._act_rulers.setChecked(config.get_show_rulers())
        self._act_rulers.setShortcut("Ctrl+R")
        self._act_rulers.triggered.connect(self._toggle_rulers)
        view.addAction(self._act_rulers)
        self._act_guides = QtGui.QAction("显示参考线", self, checkable=True)
        self._act_guides.setChecked(True)
        self._act_guides.triggered.connect(self._toggle_guides)
        view.addAction(self._act_guides)
        view.addSeparator()
        self._snap_grid = False
        self._act_grid = QtGui.QAction("显示网格", self, checkable=True)
        self._act_grid.setShortcut("Ctrl+'")
        self._act_grid.triggered.connect(lambda on: self.view.set_grid(on))
        view.addAction(self._act_grid)
        self._act_snapgrid = QtGui.QAction("吸附到网格", self, checkable=True)
        self._act_snapgrid.triggered.connect(lambda on: setattr(self, "_snap_grid", bool(on)))
        view.addAction(self._act_snapgrid)
        gsub = view.addMenu("网格大小")
        ggrp = QtGui.QActionGroup(self); ggrp.setExclusive(True)
        for _px in (10, 20, 25, 50):
            _ga = QtGui.QAction("%d px" % _px, self, checkable=True); _ga.setChecked(_px == 20)
            _ga.triggered.connect(lambda _=False, p=_px: self.view.set_grid_size(p))
            ggrp.addAction(_ga); gsub.addAction(_ga)
        view.addAction("清除参考线", self._clear_guides)
        view.addSeparator()
        # FPS 性能浮标：默认关，放「视图」菜单里方便随时勾选/取消（诊断卡顿用）
        self._fps_act = QtGui.QAction("显示 FPS 性能浮标", self, checkable=True)
        self._fps_act.setChecked(False)
        self._fps_act.setToolTip("在画布左上角显示实时帧率，用于诊断卡顿；默认关闭，取消勾选即隐藏")
        self._fps_act.toggled.connect(self._toggle_fps_hud)
        view.addAction(self._fps_act)

        # 「插件」菜单：可扩展的增效工具目录（仿 PS 增效工具）。后期新工具加进 self._plugins 即可。
        plug = self.menuBar().addMenu("插件")
        self._plugins = [
            ("✨ AI 生成 / 对话", "生成式 AI 绘图（文生图/图生图）+ AI 对话生成提示词（同一浮窗标签切换）", self._toggle_ai_panel),
        ]
        for name, tip, cb in self._plugins:
            act = plug.addAction(name, cb); act.setToolTip(tip)
        plug.addSeparator()
        act_seg = plug.addAction("AI 抠图设置…", self._ai_seg_settings_dialog)
        act_seg.setToolTip("配置 AI 分割/抠图后端（HTTP image-edit 兼容 / 本地 rembg）")
        plug.addSeparator()
        more = plug.addAction("（更多工具将并入此处…）"); more.setEnabled(False)

    def _toggle_ai_panel(self):
        # 开/关 AI 浮窗（点 ✨ 星 或 插件菜单）。自由浮动工具窗，拖动不弹 ADS 落点 overlay。
        if not hasattr(self, "_ai_window"):
            return
        w = self._ai_window
        if w.isVisible():
            w.hide()
            return
        if w.height() < 580 or w.width() < 320:  # 够大以显示全部控件（含底部文生图/图生图按钮）
            w.resize(max(360, w.width()), max(640, w.height()))
        if not self._ai_positioned:  # 仅首次移到 ✨ 星旁；之后保留用户拖动后的位置
            self._position_ai_float()
            self._ai_positioned = True
        w.show(); w.raise_(); w.activateWindow()

    def _position_ai_float(self):
        # 把 AI 浮窗移到 ✨ 星左侧，并夹在屏幕可视范围内
        win = self._ai_window
        wd = win.width()
        sg = self._plugin_star.mapToGlobal(QtCore.QPoint(0, 0))
        x, y = sg.x() - wd - 8, sg.y()
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is not None:
            x = max(scr.left() + 8, min(x, scr.right() - wd - 8))
            y = max(scr.top() + 8, min(y, scr.bottom() - win.height() - 8))
        win.move(int(x), int(y))

    def _open_ai_panel_focus_prompt(self):
        # 供 chat 的「用此提示词」调用：确保 AI 浮窗弹出 → 切到「生图」标签 → 光标放到 prompt
        if not hasattr(self, "_ai_window"):
            return
        if not self._ai_window.isVisible():
            self._toggle_ai_panel()
        if getattr(self, "_ai_tabs", None) and getattr(self, "_ai_panel", None):
            self._ai_tabs.setCurrentWidget(self._ai_panel)  # 切到「生图」标签，prompt 已填好
        if getattr(self, "_ai_panel", None):
            self._ai_panel.prompt.setFocus()

    def _switch_theme(self, name: str):
        theme.apply(QtWidgets.QApplication.instance(), name)
        theme.save(name)  # 记住选择，下次启动沿用
        self.view.apply_theme()
        c = theme.colors()["text"]
        for tool, act in self._tool_actions.items():  # 重画工具图标以适配新主题色
            act.setIcon(icons.tool_icon(tool, c))
        if hasattr(self, "_zoom_in_btn"):
            self._zoom_in_btn.setIcon(icons.tool_icon("zoom", c))
            self._zoom_out_btn.setIcon(icons.tool_icon("zoom_out", c))
        if hasattr(self, "dock_manager"):
            self._apply_ads_theme()  # ADS 面板深/浅跟随
        if hasattr(self, "_ruler_corner"):  # 标尺角块静态色跟随主题；刻度色 paintEvent 内自取，sync 即可
            self._ruler_corner.setStyleSheet("background:%s;" % theme.colors()["menu_bar"])
            self._sync_rulers()
        self._refresh_layers()

    def _toggle_fps_hud(self, on: bool):
        self.view.set_fps_hud(on)            # 画布 HUD 文字 + 每帧 FPS 计算/emit 一起开关
        if hasattr(self, "fps_label"):
            self.fps_label.setVisible(on)    # 状态栏 FPS 标签同步（默认关时不占位）

    # ---------- 放大镜工具选项（zoom 页用，无独立顶栏；缩放%/撤销重做已迁底部状态栏/选项栏左侧）----------
    def _build_top_bar(self):
        # PS/AI 顶部只有一条上下文选项栏：原「视图」QToolBar（撤销/重做 + 缩放控件）已拆除——
        # 撤销/重做 收进 _build_options_bar 左侧紧凑图标；缩放控件（−/zoom_label/+/适应/1:1）迁进 _build_statusbar。
        # 本方法仅保留放大镜工具选项（放大镜 / 缩小镜 切换，PS 选项栏风格）的控件构造。
        # 控件在此创建，但不加入任何栏——由 _build_options_bar 把整个 _zoom_opts re-parent 进 zoom 页。
        self._zoom_opts = QtWidgets.QWidget()
        zl = QtWidgets.QHBoxLayout(self._zoom_opts)
        zl.setContentsMargins(0, 0, 0, 0); zl.setSpacing(4)
        c = theme.colors()["text"]
        self._zoom_in_btn = QtWidgets.QToolButton()
        self._zoom_in_btn.setText(" 放大镜"); self._zoom_in_btn.setIcon(icons.tool_icon("zoom", c))
        self._zoom_in_btn.setCheckable(True); self._zoom_in_btn.setChecked(True)
        self._zoom_in_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._zoom_out_btn = QtWidgets.QToolButton()
        self._zoom_out_btn.setText(" 缩小镜"); self._zoom_out_btn.setIcon(icons.tool_icon("zoom_out", c))
        self._zoom_out_btn.setCheckable(True)
        self._zoom_out_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        grp = QtWidgets.QButtonGroup(self); grp.setExclusive(True)
        grp.addButton(self._zoom_in_btn); grp.addButton(self._zoom_out_btn)
        self._zoom_in_btn.clicked.connect(lambda: self.view.set_zoom_mode("in"))
        self._zoom_out_btn.clicked.connect(lambda: self.view.set_zoom_mode("out"))
        zl.addWidget(QtWidgets.QLabel("缩放："))
        zl.addWidget(self._zoom_in_btn); zl.addWidget(self._zoom_out_btn)
        # 不在此 addWidget——交给 _build_options_bar 把 _zoom_opts re-parent 进 zoom 页。

    def _update_zoom_label(self, z: float):
        self.zoom_label.setText(f"{z * 100:.0f}%")

    # ---------- 上下文选项栏（PS 式：唯一顶部栏，QStackedWidget 按工具切页）----------
    def _mirror_slider(self, src: QtWidgets.QSlider, unit: str = ""):
        """造一个镜像滑块，与 src 双向同步（blockSignals 防递归）。src 仍是业务唯一消费源，镜像只是第二个输入口。"""
        m = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        m.setRange(src.minimum(), src.maximum()); m.setValue(src.value())
        m.setToolTip(src.toolTip()); m.setFixedWidth(120)
        lbl = QtWidgets.QLabel(f"{src.value()}{unit}"); lbl.setMinimumWidth(34)
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        def m_to_src(v):
            lbl.setText(f"{v}{unit}")
            if src.value() != v:
                src.setValue(v)  # 触发 src 的业务 valueChanged（src 未被 block，正常生效）

        def src_to_m(v):
            lbl.setText(f"{v}{unit}")
            if m.value() != v:
                m.blockSignals(True); m.setValue(v); m.blockSignals(False)

        m.valueChanged.connect(m_to_src)
        src.valueChanged.connect(src_to_m)
        box = QtWidgets.QWidget(); h = QtWidgets.QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0); h.setSpacing(4)
        h.addWidget(m, 1); h.addWidget(lbl)
        return box

    @staticmethod
    def _opts_page():
        w = QtWidgets.QWidget(); h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(6, 0, 6, 0); h.setSpacing(8)
        return w, h

    def _build_options_bar(self):
        c = theme.colors()["text"]
        bar = QtWidgets.QToolBar("选项")
        bar.setMovable(False); bar.setFloatable(False)
        bar.setObjectName("optionsBar")
        # PS/AI 式单条上下文选项栏：删除原「视图」栏后这是唯一的顶部 QToolBar（不再 addToolBarBreak 换行）。
        bar.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)  # 仅影响直接 addAction 挂到 bar 的撤销/重做
        bar.setIconSize(QtCore.QSize(16, 16))                                  # icon-only 紧凑（PS 工具栏图标风）
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, bar)
        # 撤销/重做：选项栏最左侧 icon-only 紧凑按钮（复用菜单同一 _undo_act/_redo_act，enable 态/快捷键全自动一致）
        bar.addAction(self._undo_act)
        bar.addAction(self._redo_act)
        bar.addSeparator()
        self._opts_stack = QtWidgets.QStackedWidget()
        self._opts_stack.setFixedHeight(34)
        bar.addWidget(self._opts_stack)
        self._opt_pages: dict[str, int] = {}

        def add_page(name: str, w: QtWidgets.QWidget):
            self._opt_pages[name] = self._opts_stack.addWidget(w)

        # 工具 → 页 映射（沿用 _raise_tool_panel 字典风格）
        self._tool_opt_page = {
            "move": "move", "lasso": "select", "brush": "select", "wand": "select",
            "rectsel": "select", "rect": "select", "erase": "erase", "crop": "crop",
            "draw": "paint", "eraser": "paint", "text": "text", "zoom": "zoom",
            "measure": "measure", "hand": "blank",
            "node": "node", "pen": "pen",
        }

        # blank（默认空页，hand 等用）
        blank = QtWidgets.QWidget()
        add_page("blank", blank)
        self._blank_idx = self._opt_pages["blank"]

        # move 页：对齐/分布 8 按钮（诞生地从顶栏迁到这里，clicked→_align 不变）+ 自动选择占位
        mw, mh = self._opts_page()
        mh.addWidget(QtWidgets.QLabel("对齐/分布："))
        for _ak, _atip in (
            ("align_left", "左对齐"), ("align_hcenter", "水平居中"), ("align_right", "右对齐"),
            ("align_top", "顶对齐"), ("align_vcenter", "垂直居中"), ("align_bottom", "底对齐"),
            ("dist_h", "水平分布（≥3 层，按中心等间距）"), ("dist_v", "垂直分布（≥3 层，按中心等间距）"),
        ):
            ab = QtWidgets.QToolButton()
            ab.setIcon(icons.tool_icon(_ak, c))
            ab.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
            ab.setToolTip(_atip); ab.setAutoRaise(True)
            ab.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            ab.clicked.connect(lambda _checked=False, k=_ak: self._align(k))
            mh.addWidget(ab)
        # 用纯提示标签传达当前固定行为，替代旧的"永久置灰打勾"占位复选框（读作坏控件·审核 LOW）。
        _auto_hint = QtWidgets.QLabel("点击即选中所在图层")
        _auto_hint.setObjectName("hint")
        mh.addWidget(_auto_hint)
        mh.addStretch(1)
        add_page("move", mw)

        # select 页：羽化 + 容差 + 画笔大小 + 选区模式(镜像) + 剪切模式(镜像)
        sw, sh = self._opts_page()
        sh.addWidget(QtWidgets.QLabel("羽化")); sh.addWidget(self._mirror_slider(self.feather_slider, "px"))
        sh.addWidget(QtWidgets.QLabel("容差")); sh.addWidget(self._mirror_slider(self.tol_slider))
        sh.addWidget(QtWidgets.QLabel("画笔")); sh.addWidget(self._mirror_slider(self.size_slider, "px"))
        sh.addWidget(QtWidgets.QLabel("模式"))
        self._mode_btns_m = {}
        mode_lbl = {"new": "新建", "add": "＋加", "subtract": "－减"}
        for key in ("new", "add", "subtract"):
            b = QtWidgets.QToolButton(); b.setText(mode_lbl[key]); b.setCheckable(True)
            b.setChecked(key == self._sel_mode)
            b.setToolTip(self._mode_btns[key].toolTip())
            b.clicked.connect(lambda _=False, k=key: self._set_sel_mode(k))  # 同一入口 → 与右侧原按钮组同步
            sh.addWidget(b); self._mode_btns_m[key] = b
        # 剪切模式镜像：与右侧 hole_check 双向同步（共享同一状态）
        self._hole_check_m = QtWidgets.QCheckBox("剪切")
        self._hole_check_m.setToolTip(self.hole_check.toolTip())
        self._hole_check_m.setChecked(self.hole_check.isChecked())
        self._hole_check_m.toggled.connect(lambda v: self.hole_check.isChecked() != v and self.hole_check.setChecked(v))
        self.hole_check.toggled.connect(lambda v: self._hole_check_m.isChecked() != v and self._hole_check_m.setChecked(v))
        sh.addWidget(self._hole_check_m)
        sh.addStretch(1)
        add_page("select", sw)

        # crop 页：当前裁剪零选项 → 放提示（比例/拉直等为新功能，本任务不实现，避免假按钮）
        cw, ch = self._opts_page()
        ch.addWidget(QtWidgets.QLabel("裁剪：在选中层上拖框，松开即裁到框内（无额外选项）"))
        ch.addStretch(1)
        add_page("crop", cw)

        # erase 页：矩形挖洞工具不读羽化/容差/选区模式 → 给专属说明页，别再误显 select 页那些无关控件(审核 LOW L5)
        ew, eh = self._opts_page()
        eh.addWidget(QtWidgets.QLabel("矩形挖洞：在选中层上拖框，松开即用框边缘的背景色填充该区（无额外选项）"))
        eh.addStretch(1)
        add_page("erase", ew)

        # paint 页（draw/eraser）：画笔大小镜像 + 画笔颜色镜像入口
        pw, ph = self._opts_page()
        ph.addWidget(QtWidgets.QLabel("画笔大小")); ph.addWidget(self._mirror_slider(self.size_slider, "px"))
        self._color_btn_m = QtWidgets.QPushButton("画笔颜色")  # 复用同一槽 _pick_color，无状态复制
        self._color_btn_m.setToolTip("绘制/画笔工具用的颜色")
        self._color_btn_m.clicked.connect(self._pick_color)
        ph.addWidget(self._color_btn_m)
        ph.addStretch(1)
        add_page("paint", pw)

        # text 页：常用字体 + 字号 + 颜色 镜像（原控件留在右侧文字属性面板不动）
        tw, th = self._opts_page()
        th.addWidget(QtWidgets.QLabel("字体"))
        self._quick_font_m = QtWidgets.QComboBox()
        for i in range(self.quick_font.count()):
            self._quick_font_m.addItem(self.quick_font.itemText(i), self.quick_font.itemData(i))
        self._quick_font_m.setToolTip(self.quick_font.toolTip())
        self._quick_font_m.currentIndexChanged.connect(
            lambda i: self.quick_font.currentIndex() != i and self.quick_font.setCurrentIndex(i))
        self.quick_font.currentIndexChanged.connect(
            lambda i: self._quick_font_m.currentIndex() != i and self._quick_font_m.setCurrentIndex(i))
        th.addWidget(self._quick_font_m)
        th.addWidget(QtWidgets.QLabel("字号"))
        self._fontsize_combo_m = self._make_size_combo()  # 选项栏镜像；与面板字号下拉经 _sync_fontsize 双向同步
        th.addWidget(self._fontsize_combo_m)
        self._text_color_btn_m = QtWidgets.QPushButton("文字颜色")  # 复用 _pick_text_color
        self._text_color_btn_m.setToolTip("文字颜色")
        self._text_color_btn_m.clicked.connect(self._pick_text_color)
        th.addWidget(self._text_color_btn_m)
        th.addStretch(1)
        add_page("text", tw)

        # zoom 页：直接 re-parent _zoom_opts（QButtonGroup/clicked 信号全保留）
        zw, zh = self._opts_page()
        self._zoom_opts.setParent(None)
        zh.addWidget(self._zoom_opts)
        zh.addStretch(1)
        add_page("zoom", zw)

        # measure 页：读数(X/Y/W/H/A/L1/L2) + 使用测量比例 + 设比例… + 拉直图层 + 清除
        self._measure_p0 = None  # 最近测量线起点(场景坐标)
        self._measure_p1 = None  # 最近测量线终点
        self._measure_angle = 0.0  # 最近测量角度(度)，供拉直用
        self._measure_scale = None  # (units_per_px, unit_str) 或 None
        mxw, mxh = self._opts_page()
        self._measure_label = QtWidgets.QLabel(self._format_measure(None, None))
        self._measure_label.setStyleSheet("font-family:'Consolas','Menlo',monospace;")  # 等宽 → 读数变化不抖
        mxh.addWidget(self._measure_label)
        self._measure_scale_chk = QtWidgets.QCheckBox("使用测量比例")
        self._measure_scale_chk.setToolTip("勾选后长度/宽高按已设比例换算成真实单位（需先「设比例…」）")
        self._measure_scale_chk.toggled.connect(self._on_measure_scale_toggled)
        mxh.addWidget(self._measure_scale_chk)
        mb_scale = QtWidgets.QPushButton("设比例…")
        mb_scale.setToolTip("输入「当前测量线 = 多少真实单位」，之后长度按比例显示真实尺寸（如 50 µm）")
        mb_scale.clicked.connect(self._set_measure_scale)
        mxh.addWidget(mb_scale)
        mb_straight = QtWidgets.QPushButton("拉直图层")
        mb_straight.setToolTip("按测量线角度旋转活动层，使该线变水平（位图重采样，走撤销历史）")
        mb_straight.clicked.connect(self._straighten_active)
        mxh.addWidget(mb_straight)
        mb_clear = QtWidgets.QPushButton("清除")
        mb_clear.setToolTip("清掉测量线与读数")
        mb_clear.clicked.connect(self._clear_measure)
        mxh.addWidget(mb_clear)
        mxh.addStretch(1)
        add_page("measure", mxw)

        # node 页（锚点工具）：提示 + 增/删锚说明（增删走画布交互，无额外控件）
        nw, nh = self._opts_page()
        self._node_hint = QtWidgets.QLabel(
            "锚点：先用「移动」选中一个矢量 path，再切锚点工具。拖锚移点 · 拖柄改曲率 · Alt/双击段上加锚 · 选锚 Del 删")
        nh.addWidget(self._node_hint)
        nh.addStretch(1)
        add_page("node", nw)

        # pen 页（钢笔工具）：提示
        pnw, pnh = self._opts_page()
        pnh.addWidget(QtWidgets.QLabel(
            "钢笔：单击=角点 · 拖拽=平滑曲线锚 · Enter/双击=结束 · 点回起点=闭合 · Esc=取消"))
        pnh.addStretch(1)
        add_page("pen", pnw)

        # 选项栏所有按钮统一手型光标
        for btn in self._opts_stack.findChildren(QtWidgets.QAbstractButton):
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    # ---------- 左：竖排工具栏 ----------
    def _build_left_tools(self):
        tb = QtWidgets.QToolBar("工具")
        tb.setOrientation(QtCore.Qt.Orientation.Vertical)
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QtCore.QSize(22, 22))
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(QtCore.Qt.ToolBarArea.LeftToolBarArea, tb)
        grp = QtGui.QActionGroup(self)
        grp.setExclusive(True)
        self._tool_actions = {}
        # 按语义分 5 组（导航 / 选区 / 裁切 / 绘制·文字 / 缩放），组间加分隔线（对齐 PS 工具栏分段）
        # PS 式工具组（flyout）：同槽工具收进一个 FlyoutToolButton（右下角小三角 + 右键弹菜单选用哪个 +
        # 左键用上次选的）。本项目两个 flyout 槽（对齐 PS 选区族 / 裁切族分属不同 flyout）：
        #   select = [rectsel, lasso, wand, brush]（"画选区"族，默认 rectsel 矩形选框，最常用）
        #   cut    = [rect, erase, crop]          （"切图"族，默认 rect 矩形抠出）
        # 关键：flyout 成员按【遇到顺序】累积、按钮插在【首个遇到的成员位置】、组内首个=默认动作——故须把
        # rectsel 排在 lasso/wand/brush 之前、rect 排在 erase/crop 之前，让 select 按钮默认 rectsel、cut 按钮默认 rect。
        TOOL_GROUPS = [
            [("move", "移动 / 选择对象"), ("hand", "抓手 / 平移视图（也可按住空格键拖动）")],
            # 选区组（select 槽：画选区族，默认 rectsel 在组首）
            [("rectsel", "矩形选框：拖框生成矩形选区（Shift 加选 / Alt 减选；可作 GrabCut 种子/删除选区，不直接抠出）"),
             ("lasso", "套索：手绘闭环选区（Shift 加选 / Alt 减选）"),
             ("wand", "魔棒：点击按颜色选区（Shift 加选 / Alt 减选 / 默认新建）"),
             ("brush", "选区画笔：涂抹累积选区（Shift 加选 / Alt 减选）")],
            # 裁切组（cut 槽：切图族，默认 rect 在组首）
            [("rect", "矩形抠出：拖框直接抠成可移动层（勾剪切模式则原位填底）"),
             ("erase", "矩形挖洞：拖框→用背景色填充覆盖该区"),
             ("crop", "裁剪：在选中层上拖框，把该层裁到框内")],
            [("draw", "绘制：在选中层上用画笔颜色画像素"),
             ("eraser", "橡皮擦：在选中层上擦成透明像素"),
             ("text", "文字：拖框=定宽文本框/单击=放置；移动工具双击可改")],
            [("pen", "钢笔：点击加角点 / 拖拽加平滑锚 → 画新矢量路径（Enter/双击结束·回起点闭合·Esc 取消）"),
             ("node", "锚点：选中单个矢量 path 后编辑锚点（拖锚移点·拖柄改曲率·Alt/双击段上加锚·选锚 Del 删）")],
            # 形状组（shape 槽：画矢量形状，默认 sh_rect 在组首）
            [("sh_rect", "矩形：拖框画矩形（Shift = 正方形）→ 可移动/改色的矢量形状"),
             ("sh_ellipse", "椭圆：拖框画椭圆 / 圆（Shift = 正圆）"),
             ("sh_line", "直线：拖画直线（Shift 吸 0/45/90°）"),
             ("sh_arrow", "箭头：拖画带箭头的线（Shift 吸角度）—— 通路/流程图常用")],
            [("connector", "智能连接线：从一个对象【拖到】另一个对象建立带箭头的连线；"
                           "移动/缩放任一对象，连线两端自动跟随 —— 机制/通路图核心（BioRender 式）")],
            [("measure", "测量：拖画测量线，选项栏读 X/Y/W/H/角度/长度；可设比例换算真实尺寸、按角度拉直活动层"),
             ("zoom", "缩放 / 放大镜（左键放大·右键或 Alt 缩小）")],
        ]
        # flyout 工具组：tool → 槽 id；同槽工具收进一个 FlyoutToolButton（右下角小三角 + 右键弹菜单选用哪个 +
        # 左键用上次选的）。第一遍建好所有 QAction 并按槽收集成员，第二遍布局工具栏（flyout 按钮拿到【完整】成员列表）。
        FLYOUT_OF = {"rectsel": "select", "lasso": "select", "wand": "select", "brush": "select",
                     "rect": "cut", "erase": "cut", "crop": "cut",
                     "sh_rect": "shape", "sh_ellipse": "shape", "sh_line": "shape", "sh_arrow": "shape"}
        self._flyout_btns = {}     # 槽 id → FlyoutToolButton（set_tool 末尾据此同步按钮图标）
        self._tool_flyout = dict(FLYOUT_OF)  # tool → 槽 id（供 _sync_flyout 反查）
        flyout_members = {}        # 槽 id → [QAction]（按遇到顺序累积，组内首个=默认）

        # 工具中文短名：flyout 弹出菜单显示「图标 + 名称」(PS 式)，长说明走 toolTip。
        # 工具栏本体是 IconOnly，故设 text 不会让竖排工具栏冒出文字。
        TOOL_NAMES = {
            "move": "移动", "hand": "抓手",
            "rectsel": "矩形选框", "lasso": "套索", "wand": "魔棒", "brush": "选区画笔",
            "rect": "矩形抠出", "erase": "矩形挖洞", "crop": "裁剪",
            "draw": "画笔", "eraser": "橡皮擦", "text": "文字",
            "pen": "钢笔", "node": "锚点", "measure": "测量", "zoom": "缩放",
            "sh_rect": "矩形", "sh_ellipse": "椭圆", "sh_line": "直线", "sh_arrow": "箭头",
            "connector": "连接线",
        }

        def make_action(tool, tip):
            a = QtGui.QAction(self)
            a.setCheckable(True)
            a.setText(TOOL_NAMES.get(tool, tool))  # flyout 菜单显示中文名
            a.setIcon(icons.tool_icon(tool, theme.colors()["text"]))
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, t=tool: self.set_tool(t))
            grp.addAction(a)  # 仍进 QActionGroup → 与其它工具互斥、set_tool 的 setChecked 生效
            self._tool_actions[tool] = a
            return a

        # 第一遍：建所有 action + 收集 flyout 成员（完整成员表，避免 flyout 按钮只拿到首个）
        for group in TOOL_GROUPS:
            for tool, tip in group:
                a = make_action(tool, tip)
                slot = FLYOUT_OF.get(tool)
                if slot is not None:
                    flyout_members.setdefault(slot, []).append(a)
        # 第二遍：布局工具栏。flyout 槽在其首工具位置插一个按钮（用完整成员表）；其余工具按原顺序 addAction。
        for gi, group in enumerate(TOOL_GROUPS):
            if gi:
                tb.addSeparator()
            for tool, tip in group:
                slot = FLYOUT_OF.get(tool)
                if slot is None:
                    tb.addAction(self._tool_actions[tool])
                    continue
                if slot not in self._flyout_btns:  # 槽内第一个成员处插入 flyout 按钮（位置=该槽首工具的位置）
                    btn = FlyoutToolButton(flyout_members[slot], theme.colors()["text"])
                    btn.setIconSize(QtCore.QSize(22, 22))
                    self._flyout_btns[slot] = btn
                    tb.addWidget(btn)
        self._tool_actions["move"].setChecked(True)

    def _sync_flyout(self, tool: str):
        # set_tool 后：若该工具属某 flyout 槽 → 让按钮的默认动作/图标跟到所选工具（满足「选中后按钮图标换成所选工具」）。
        slot = getattr(self, "_tool_flyout", {}).get(tool)
        if slot is None:
            return
        btn = self._flyout_btns.get(slot)
        act = self._tool_actions.get(tool)
        if btn is not None and act is not None:
            btn.set_current(act)

    @staticmethod
    def _hint(text: str) -> QtWidgets.QLabel:
        """统一的面板提示标签：objectName=hint → 走主题 QSS(QLabel#hint)，深浅主题自动跟随、对比达标。"""
        lbl = QtWidgets.QLabel(text); lbl.setObjectName("hint"); lbl.setWordWrap(True)
        return lbl

    # ---------- 右：功能面板 ----------
    def _build_right_dock(self):
        def slider(lo, hi, val, tip="", unit=""):
            s = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            s.setRange(lo, hi); s.setValue(val)
            if tip:
                s.setToolTip(tip)
            lbl = QtWidgets.QLabel(f"{val}{unit}"); lbl.setMinimumWidth(38)
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            s.valueChanged.connect(lambda v: lbl.setText(f"{v}{unit}"))
            box = QtWidgets.QWidget(); h = QtWidgets.QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            h.addWidget(s, 1); h.addWidget(lbl)
            return s, box

        def make_panel():  # 一个面板内容 = 普通 QWidget + 竖直布局（之后塞进标签页）
            w = QtWidgets.QWidget(); lay = QtWidgets.QVBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(7)
            # 关键：给最小宽度，否则 ADS 能把停靠面板压到 0（文字属性的输入框/字体下拉被裁掉看不见）
            # 文字属性那张含 QFormLayout+字体下拉，300 才够；其余面板取同值最省事。
            w.setMinimumWidth(300)
            return w, lay

        # —— 选区与拆解 ——
        w_sel, sl = make_panel()
        form = QtWidgets.QFormLayout(); form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.size_slider, b1 = slider(1, 300, 24, tip="画笔/绘制/橡皮的直径", unit="px"); form.addRow("画笔大小", b1)
        self.size_slider.valueChanged.connect(lambda v: self.view.set_brush_radius(v / 2.0))  # 画笔光圈跟随大小
        self.tol_slider, b2 = slider(0, 255, 30, tip="魔棒/去背景的颜色容差：越大越能把相近颜色当成同一片（0–255）"); form.addRow("容差", b2)
        self.feather_slider, b3 = slider(0, 20, 2, tip="选区边缘羽化半径，让抠图边缘更柔和", unit="px"); form.addRow("羽化", b3)
        self.color_btn = QtWidgets.QPushButton("画笔颜色"); self.color_btn.clicked.connect(self._pick_color)
        self.color_btn.setToolTip("绘制/画笔工具用的颜色")
        self._refresh_color_btn(); form.addRow("颜色", self.color_btn)
        sl.addLayout(form)
        mode_row = QtWidgets.QHBoxLayout(); mode_row.addWidget(QtWidgets.QLabel("选区模式"))
        mgrp = QtWidgets.QButtonGroup(self); mgrp.setExclusive(True); self._mode_btns = {}
        mode_tips = {"new": "新建：替换原有选区（PS 默认）", "add": "加选：在已有选区上叠加（≡ 按住 Shift 框选）",
                     "subtract": "减选：从已有选区减去（≡ 按住 Alt 框选）"}
        for key, label in (("new", "新建"), ("add", "＋加选"), ("subtract", "－减选")):
            b = QtWidgets.QToolButton(); b.setText(label); b.setCheckable(True); b.setChecked(key == "new")
            b.setToolTip(mode_tips[key])
            b.clicked.connect(lambda _=False, k=key: self._set_sel_mode(k))  # 经统一入口 → 同步镜像按钮
            mgrp.addButton(b); mode_row.addWidget(b); self._mode_btns[key] = b
        mode_row.addStretch(1); sl.addLayout(mode_row)
        self.hole_check = QtWidgets.QCheckBox("剪切模式（抠到图层时原位填底色覆盖）")
        self.hole_check.setToolTip("勾选后：Ctrl+J / 矩形抠出会在原层留下底色填充；不勾则原层不变（复制）")
        sl.addWidget(self.hole_check)
        # 主操作：选区/激活层 → 加入素材库（科研抠图收集）。抠到画布图层用 Ctrl+J（PS 通过拷贝的图层）。
        b_asset = QtWidgets.QPushButton("加入素材库"); b_asset.setProperty("primary", True)
        b_asset.setToolTip("把当前选区（或整个激活层）裁出存进右侧素材库，方便反复取用；抠到画布图层请按 Ctrl+J")
        b_asset.clicked.connect(self.add_to_assets); sl.addWidget(b_asset)
        r1 = QtWidgets.QHBoxLayout()
        b_bg = QtWidgets.QPushButton("去背景")
        b_bg.setToolTip("自动检测背景 → 前景生成透明素材入库（有选区则只取选区内；不改源层）")
        b_bg.clicked.connect(self.do_remove_bg)
        b_del = QtWidgets.QPushButton("删除选区")
        b_del.setToolTip("把选区内像素抹成透明（从当前层删除选中内容）")
        b_del.clicked.connect(self.do_delete_selection)
        r1.addWidget(b_bg); r1.addWidget(b_del); sl.addLayout(r1)
        b_auto = QtWidgets.QPushButton("自动拆解（按背景拆成多个素材）")
        b_auto.setToolTip("按背景色自动把整层拆成多个独立透明素材，一次性入库")
        b_auto.clicked.connect(self.do_auto_decompose)
        sl.addWidget(b_auto)
        b_grabcut = QtWidgets.QPushButton("GrabCut 抠图")
        b_grabcut.setToolTip("用当前选区作前景种子，对非纯色背景抠主体（生成透明素材入库）")
        b_grabcut.clicked.connect(self.do_grabcut)
        sl.addWidget(b_grabcut)
        b_aiseg = QtWidgets.QPushButton("AI 抠图/拆解（彩色复杂图）")
        b_aiseg.setToolTip("用 AI 分割后端抠出前景/拆成多个元素，结果入素材库；适合魔棒拆不动的彩色 biorender 图。需先在弹出设置里配置后端")
        b_aiseg.clicked.connect(self.do_ai_segment)
        sl.addWidget(b_aiseg)
        sl.addWidget(self._hint("起选区：套索/选区画笔涂 · 魔棒按颜色点 · Ctrl 点图层=载入该层为选区。\n"
                                "连续精修：套索/选区画笔/魔棒 按 Shift 加选 / Alt 减选（Ctrl+Shift/Ctrl+Alt 点图层=加/减该层）。\n"
                                "Esc 取消 · Ctrl+Shift+D 重新选择 · 选区→加入素材库 / Ctrl+J 抠到新图层。"))
        sl.addStretch(1)

        # —— 文字属性 ——
        w_text, tl = make_panel()
        tf = QtWidgets.QFormLayout()
        # 「内容」框已撤——文字内容在画布上就地打字（文字工具点/拖框，或移动工具双击重编辑），面板只管样式
        self.font_combo = QtWidgets.QFontComboBox(); tf.addRow("字体", self.font_combo)
        self.font_combo.setToolTip("字体；改动即时套用到当前选中的文字层")
        self.quick_font = QtWidgets.QComboBox()
        for _qf_name, _qf_family in (
            ("Arial", "Arial"), ("Times New Roman", "Times New Roman"),
            ("宋体 SimSun", "SimSun"), ("黑体 SimHei", "SimHei"),
            ("楷体 KaiTi", "KaiTi"), ("仿宋 FangSong", "FangSong"),
            ("微软雅黑 Microsoft YaHei", "Microsoft YaHei"),
        ):
            self.quick_font.addItem(_qf_name, _qf_family)
        self.quick_font.setToolTip("常用中文/英文字体快捷；选择后即时套用到当前选中的文字层")
        self.quick_font.currentIndexChanged.connect(self._on_quick_font_changed)
        tf.addRow("常用字体", self.quick_font)
        self.fontsize_combo = self._make_size_combo()  # 可下拉选预设 + 手输（PS 式）
        tf.addRow("字号", self.fontsize_combo)
        self.fontrot_spin = QtWidgets.QSpinBox(); self.fontrot_spin.setRange(-360, 360); self.fontrot_spin.setValue(0)
        self.fontrot_spin.setToolTip("旋转角度(°)，绕文字中心旋转，-360~360")
        tf.addRow("旋转", self.fontrot_spin)
        self.text_color_btn = QtWidgets.QPushButton("文字颜色"); self.text_color_btn.clicked.connect(self._pick_text_color)
        self.text_color_btn.setToolTip("文字颜色")
        self._refresh_text_color_btn(); tf.addRow("颜色", self.text_color_btn)
        wrow = QtWidgets.QHBoxLayout()
        self.bold_check = QtWidgets.QCheckBox("加粗"); self.bold_check.setToolTip("加粗（与细体互斥）")
        self.thin_check = QtWidgets.QCheckBox("细体"); self.thin_check.setToolTip("细体/Light 字重（与加粗互斥）")
        wrow.addWidget(self.bold_check); wrow.addWidget(self.thin_check); wrow.addStretch(1)
        tf.addRow("字重", wrow)
        # M6: 字体/字号/旋转/字重改动 → 即时作用于选中文字层；加粗⇄细体互斥（对齐 onBoldWeightToggle）
        # 字号下拉的即时套用走 _sync_fontsize（在 _make_size_combo 里已连 currentTextChanged）
        self.font_combo.currentFontChanged.connect(lambda *_: self._text_live_update())
        self.fontrot_spin.valueChanged.connect(lambda *_: self._text_live_update())
        self.bold_check.toggled.connect(self._on_bold_toggled)
        self.thin_check.toggled.connect(self._on_thin_toggled)
        tl.addLayout(tf)
        # 「添加文字」按钮已撤（与画布上文字工具/双击就地打字重复）——这里只保留「应用到选中」
        b_appt = QtWidgets.QPushButton("应用到选中"); b_appt.setProperty("primary", True)
        b_appt.setToolTip("把上方字体/字号/颜色/字重套用到当前选中的文字层（不改文字内容）")
        b_appt.clicked.connect(self._apply_text)
        tl.addWidget(b_appt)
        tl.addWidget(self._hint("文字内容在画布上输入：文字工具拖框=定宽文本框（自动换行）·单击=默认框/重编辑；移动工具双击文字层也可重编辑。上面只调样式。"))
        tl.addStretch(1)

        # —— 图层 ——
        w_layer, ll = make_panel()
        ltop = QtWidgets.QHBoxLayout()
        b_newl = QtWidgets.QPushButton("＋ 新建白色层")
        b_newl.setToolTip("在当前画布上新建一张白色图层（透明层请用「文件→新建透明图层」）")
        b_newl.clicked.connect(self.new_white_layer)
        ltop.addWidget(b_newl, 1)  # 打组/解组已移到下方 PS 式底部图标栏
        ll.addLayout(ltop)
        # 对齐/分布按钮已移到顶部「视图」工具栏（PS 选项栏风格）；图层面板只保留多选用于对齐
        self.layer_list = DragLayerList(self); self.layer_list.setObjectName("layerList")  # QSS:图层行自绘高亮,选中态透明；支持拖拽排序
        self.layer_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)  # Ctrl/Shift 多选用于对齐
        self.layer_list.currentRowChanged.connect(self._on_layer_row)
        self.layer_list.itemSelectionChanged.connect(self._on_layer_selection_changed)
        self.layer_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # 行宽=面板宽,✕不被挤出
        ll.addWidget(self.layer_list, 1)
        # —— 不透明度滑块（作用于当前激活层；滑动预览，松手入历史，仿亮度/对比度）——
        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(QtWidgets.QLabel("不透明度"))
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100); self.opacity_slider.setValue(100)
        self.opacity_slider.setToolTip("当前激活图层的整体不透明度（0=全透明，100=不透明）")
        self.opacity_lbl = QtWidgets.QLabel("100%"); self.opacity_lbl.setMinimumWidth(40)
        self.opacity_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.opacity_slider.valueChanged.connect(self._on_opacity_preview)   # 滑动预览（不入历史）
        self.opacity_slider.sliderReleased.connect(self._on_opacity_commit)  # 松手提交（入历史）
        op_row.addWidget(self.opacity_slider, 1); op_row.addWidget(self.opacity_lbl)
        ll.addLayout(op_row)
        # —— PS 式底部图标工具栏：新建层 / 删除当前层 / 打组 / 解组 / 亮度对比度 ——
        bbar = QtWidgets.QHBoxLayout(); bbar.setSpacing(2)
        tc = theme.colors()["text"]
        for _ic, _tip, _slot in (
            ("new_layer", "新建白色图层", self.new_white_layer),
            ("trash", "删除当前激活图层", self._delete_active),
            ("group", "打组：把勾选/多选的图层归为一组", self.do_group),
            ("ungroup", "解组：拆散当前组", self.do_ungroup),
            ("adjust", "亮度 / 对比度（图像>调整，作用于当前层）", self.brightness_contrast_dialog),
            ("mask", "图层蒙版：先在该层取选区，再点此从选区生成蒙版（非破坏·选区内露外藏·原图不动；删除走图层右键）", self._mask_from_selection),
        ):
            tbtn = QtWidgets.QToolButton()
            tbtn.setIcon(icons.tool_icon(_ic, tc)); tbtn.setIconSize(QtCore.QSize(20, 20))
            tbtn.setAutoRaise(True); tbtn.setToolTip(_tip)
            tbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            tbtn.clicked.connect(lambda _=False, fn=_slot: fn())
            bbar.addWidget(tbtn)
        bbar.addStretch(1)
        ll.addLayout(bbar)
        exp_row = QtWidgets.QHBoxLayout()
        b_expng = QtWidgets.QPushButton("导出 PNG…"); b_expng.setProperty("primary", True)
        b_expng.setToolTip("把所有可见图层合成导出为 PNG（隐藏层不导出）"); b_expng.clicked.connect(self.export_png)
        b_extiff = QtWidgets.QPushButton("导出 TIFF…"); b_extiff.setToolTip("投稿期刊常要求 TIFF（自动写 300DPI 元数据）"); b_extiff.clicked.connect(self.export_tiff)
        exp_row.addWidget(b_expng); exp_row.addWidget(b_extiff); ll.addLayout(exp_row)
        ll.addWidget(self._hint("点选高亮、👁 显隐、双击重命名、拖拽行调层级；右键菜单：上/下移、勾选打组、删除、蒙版。底栏图标：新建/删除/打组/解组/亮度对比度/蒙版。隐藏的层不导出。"))

        # —— 素材库（BioRender 式：顶部 Tab 切「本地库/抠出素材」，只显一个，搜索置顶，次要操作收进 ⚙）——
        w_assets, al = make_panel()
        al.setSpacing(6)
        self.asset_tabbar = QtWidgets.QTabBar()  # 顶部分段：本地素材库 / 抠出素材（互不抢空间）
        self.asset_tabbar.setObjectName("assetTabs")
        self.asset_tabbar.setExpanding(True); self.asset_tabbar.setDrawBase(False)
        self.asset_tabbar.addTab("本地素材库"); self.asset_tabbar.addTab("抠出素材 0")
        al.addWidget(self.asset_tabbar)
        self.asset_stack = QtWidgets.QStackedWidget()
        self.asset_tabbar.currentChanged.connect(self.asset_stack.setCurrentIndex)
        al.addWidget(self.asset_stack, 1)

        # ===== 页0：本地素材库 =====
        _page_local = QtWidgets.QWidget()
        _pl = QtWidgets.QVBoxLayout(_page_local); _pl.setContentsMargins(0, 0, 0, 0); _pl.setSpacing(6)
        _srow = QtWidgets.QHBoxLayout()  # 搜索 + ⚙ 菜单（连接/索引/按分类导出/拆分合集 全收进来）
        self.asset_search = QtWidgets.QLineEdit()
        self.asset_search.setPlaceholderText("🔍 搜索素材名（跨全部分类）…")
        self.asset_search.setClearButtonEnabled(True)
        self._asset_filter = ""; self._asset_cur_items = []; self._asset_all_items = []
        self._search_timer = QtCore.QTimer(self); self._search_timer.setSingleShot(True); self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_asset_search)
        self.asset_search.textChanged.connect(lambda *_: self._search_timer.start())
        _srow.addWidget(self.asset_search, 1)
        self.asset_fs_count = QtWidgets.QLabel(""); self.asset_fs_count.setObjectName("hint")
        _srow.addWidget(self.asset_fs_count)
        _gear = QtWidgets.QToolButton(); _gear.setText("⚙")
        _gear.setToolTip("素材库操作：连接文件夹 / 生成分类索引 / 按分类导出 / 拆分合集")
        _gear.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        _gear.setAutoRaise(True); _gear.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        _gmenu = QtWidgets.QMenu(_gear)
        _gmenu.addAction("📁 连接素材文件夹…", self._connect_asset_dir)
        _gmenu.addAction("🗂 生成分类索引", self._build_asset_manifest)
        _gmenu.addAction("⬇ 按分类导出…", self._export_assets_by_category)
        _gmenu.addSeparator()
        _gmenu.addAction("✂ 拆分合集为单个图标…", self._split_montage_assets)
        _gmenu.addAction("✂ 批量裁透明边（当前分类）…", self._trim_asset_folder)
        _gear.setMenu(_gmenu)
        _srow.addWidget(_gear)
        _pl.addLayout(_srow)
        # 分类树（分类→子分类，点哪个只加载它的直属图 → 海量库不卡）
        self.asset_tree = QtWidgets.QTreeWidget()
        self.asset_tree.setHeaderHidden(True); self.asset_tree.setMaximumHeight(150)
        self.asset_tree.setToolTip("素材分类树（=文件夹结构）。点一个分类→只加载它的直属图；子分类点开再看，不卡。")
        self.asset_tree.itemClicked.connect(self._on_asset_tree_click)
        _pl.addWidget(self.asset_tree)
        # 缩略图大小滑块（持久化，看不清就拖大）
        self._asset_thumb = config.get_asset_thumb_size(140)
        _zrow = QtWidgets.QHBoxLayout()
        _zrow.addWidget(self._hint("缩略图大小"))
        self.asset_zoom = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.asset_zoom.setRange(72, 240); self.asset_zoom.setValue(self._asset_thumb)
        self.asset_zoom.setToolTip("拖动调整素材缩略图大小（看不清就拖大）·会被记住，下次打开保持")
        self.asset_zoom.valueChanged.connect(self._on_asset_thumb_size)
        self.asset_zoom.sliderReleased.connect(lambda: config.set_asset_thumb_size(self._asset_thumb))
        _zrow.addWidget(self.asset_zoom, 1)
        _pl.addLayout(_zrow)
        self.asset_fs_list = AssetListWidget()
        self.asset_fs_list.setObjectName("assetGrid")  # 卡片边界
        self.asset_fs_list.setViewMode(QtWidgets.QListWidget.ViewMode.IconMode)
        self.asset_fs_list.setIconSize(QtCore.QSize(self._asset_thumb, self._asset_thumb))
        self.asset_fs_list.setGridSize(QtCore.QSize(self._asset_thumb + 16, self._asset_thumb + 16))  # 固定大格子：懒加载空 item 不缩格
        self.asset_fs_list.setResizeMode(QtWidgets.QListWidget.ResizeMode.Adjust)
        self.asset_fs_list.setMovement(QtWidgets.QListWidget.Movement.Static)
        # 【不】用 setUniformItemSizes——它会按懒加载首个空 item 把装饰区缓存成 0，缩略图永远很小。
        self.asset_fs_list.setLayoutMode(QtWidgets.QListView.LayoutMode.Batched)
        self.asset_fs_list.setBatchSize(200)
        self.asset_fs_list.setDragEnabled(True)
        self.asset_fs_list.itemClicked.connect(self._fs_asset_clicked)  # 单击=放画布中央（拖动=放到指定位置）
        self.asset_fs_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)  # 右键：放画布/收藏/拆分/定位
        self.asset_fs_list.customContextMenuRequested.connect(self._fs_asset_menu)
        self.asset_fs_list.setSpacing(4); self.asset_fs_list.setMinimumHeight(140)
        self._thumb_timer = QtCore.QTimer(self); self._thumb_timer.setSingleShot(True); self._thumb_timer.setInterval(60)
        self._thumb_timer.timeout.connect(self._lazy_decode_asset_thumbs)  # 滚动去抖 → 只解码可见缩略图
        self.asset_fs_list.verticalScrollBar().valueChanged.connect(lambda *_: self._thumb_timer.start())
        _pl.addWidget(self.asset_fs_list, 1)
        _pl.addWidget(self._hint("首次使用：点右上 ⚙ →「连接素材文件夹」。缩略图单击放画布中央、拖动放到指定位置；右键有更多。"))
        self._asset_groups = []
        self.asset_stack.addWidget(_page_local)

        # ===== 页1：抠出素材（抠图/去背景/拆解收集的元素）=====
        _page_extract = QtWidgets.QWidget()
        _pe = QtWidgets.QVBoxLayout(_page_extract); _pe.setContentsMargins(0, 0, 0, 0); _pe.setSpacing(6)
        self.asset_count = QtWidgets.QLabel("0"); self.asset_count.setVisible(False)  # 隐藏；计数显示在 Tab 文案上
        self.asset_list = QtWidgets.QListWidget()
        self.asset_list.setObjectName("assetGrid")
        self.asset_list.setViewMode(QtWidgets.QListWidget.ViewMode.IconMode)
        self.asset_list.setIconSize(QtCore.QSize(72, 72))
        self.asset_list.setResizeMode(QtWidgets.QListWidget.ResizeMode.Adjust)
        self.asset_list.setMovement(QtWidgets.QListWidget.Movement.Static)
        self.asset_list.setSpacing(4); self.asset_list.setMinimumHeight(140)
        self.asset_list.itemClicked.connect(self._asset_clicked)
        self.asset_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)  # 右键删除/导出单个素材
        self.asset_list.customContextMenuRequested.connect(self._asset_menu)
        for _sk in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):  # Del/退格 删选中素材
            _del_sc = QtGui.QShortcut(QtGui.QKeySequence(_sk), self.asset_list)
            _del_sc.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
            _del_sc.activated.connect(self._delete_selected_asset)
        _pe.addWidget(self.asset_list, 1)
        _erow = QtWidgets.QHBoxLayout()
        b_expa = QtWidgets.QPushButton("导出全部"); b_expa.setToolTip("把素材库每个元素各存为一张透明 PNG")
        b_expa.clicked.connect(self.export_assets)
        b_clra = QtWidgets.QPushButton("清空"); b_clra.setProperty("danger", True)
        b_clra.setToolTip("清空整个素材库（会弹确认）"); b_clra.clicked.connect(self.clear_assets)
        _erow.addWidget(b_expa); _erow.addWidget(b_clra); _pe.addLayout(_erow)
        _pe.addWidget(self._hint("抠图/去背景/自动拆解的元素在此。单击放回画布；右键导出/删除单个。"))
        self.asset_stack.addWidget(_page_extract)

        # —— 历史记录（PS 历史面板）——
        w_hist, hl = make_panel()
        hrow = QtWidgets.QHBoxLayout()  # 顶部镜像撤销/重做按钮，与工具栏同源 enable（共用 _undo_act/_redo_act）
        b_undo_m = QtWidgets.QToolButton(); b_undo_m.setDefaultAction(self._undo_act)
        b_redo_m = QtWidgets.QToolButton(); b_redo_m.setDefaultAction(self._redo_act)
        b_undo_m.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b_redo_m.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        hrow.addWidget(b_undo_m); hrow.addWidget(b_redo_m); hrow.addStretch(1)
        hl.addLayout(hrow)
        self.hist_list = QtWidgets.QListWidget(); self.hist_list.setObjectName("histList")
        self.hist_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)  # 高亮自绘，不用系统选中态
        self.hist_list.itemClicked.connect(
            lambda it: self.jump_to(it.data(QtCore.Qt.ItemDataRole.UserRole)))
        hl.addWidget(self.hist_list, 1)
        hl.addWidget(self._hint("点某步跳到该状态；当前步高亮，其后步骤变灰仍可恢复；做新操作会截断灰色分支。"))

        # —— 矢量属性（B2：元素级选中的 fill/stroke / 文字字体字号色 + 配色助手）——
        w_vec, vl = make_panel()
        self._build_vec_panel(vl)

        # 右侧面板所有按钮统一手型光标（QAbstractButton 同时覆盖 QPushButton/QToolButton/QCheckBox）
        for panel in (w_sel, w_text, w_layer, w_assets, w_hist, w_vec):
            for btn in panel.findChildren(QtWidgets.QAbstractButton):
                btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        # —— Qt-ADS：右侧两组标签（选区与拆解/文字属性 一组，图层/素材库 一组）——
        # 每个面板都可拖标签换组、拖出浮动、四向落位、分列、自动隐藏；不会甩到画布顶部
        A = ads.DockWidgetArea
        self._dw_sel = ads.CDockWidget(self.dock_manager, "选区与拆解"); self._dw_sel.setWidget(w_sel)
        self._dw_text = ads.CDockWidget(self.dock_manager, "文字属性"); self._dw_text.setWidget(w_text)
        self._dw_vec = ads.CDockWidget(self.dock_manager, "矢量属性"); self._dw_vec.setWidget(w_vec)
        self._dw_layer = ads.CDockWidget(self.dock_manager, "图层"); self._dw_layer.setWidget(w_layer)
        self._dw_assets = ads.CDockWidget(self.dock_manager, "素材库"); self._dw_assets.setWidget(w_assets)
        self._dw_hist = ads.CDockWidget(self.dock_manager, "历史记录"); self._dw_hist.setWidget(w_hist)
        area_top = self.dock_manager.addDockWidget(A.RightDockWidgetArea, self._dw_sel)
        self.dock_manager.addDockWidgetTabToArea(self._dw_text, area_top)
        self.dock_manager.addDockWidgetTabToArea(self._dw_vec, area_top)  # 与选区/文字 Tab 同组（area_top）
        area_bot = self.dock_manager.addDockWidget(A.BottomDockWidgetArea, self._dw_layer, area_top)
        self.dock_manager.addDockWidgetTabToArea(self._dw_assets, area_bot)
        self.dock_manager.addDockWidgetTabToArea(self._dw_hist, area_bot)  # 与图层/素材库 Tab 同组
        self._dw_panels = [self._dw_sel, self._dw_text, self._dw_vec, self._dw_layer, self._dw_assets, self._dw_hist]
        # AI 生成 + 对话：PS 式自由浮动工具窗（不进 ADS → 拖动不弹停靠 overlay）；点 ✨ 星 / 插件菜单 开关。
        # 方案 A（用户确认）：生图 + 对话装进同一浮窗的标签页，✨ 一键开关，用户不用到处找。
        self._ai_window = FloatingToolWindow(self, "AI 生成 / 对话", icons.tool_icon("star", "#f6b73c", 16))
        self._ai_panel = ai_panel.AiPanel(self)      # 保留引用：chat「用此提示词」写 _ai_panel.prompt
        self._chat_panel = chat_panel.ChatPanel(self)
        self._ai_tabs = QtWidgets.QTabWidget()
        self._ai_tabs.addTab(self._ai_panel, "生图")
        self._ai_tabs.addTab(self._chat_panel, "对话")
        self._ai_window.set_content(self._ai_tabs)
        self._ai_positioned = False  # 首次打开移到 ✨ 星旁，之后保留用户拖动后的位置
        self._view_menu.addSeparator()
        for d in self._dw_panels:
            self._view_menu.addAction(d.toggleViewAction())
        self._view_menu.addSeparator()
        self._view_menu.addAction("重置面板布局", self._reset_dock_layout)
        self._default_dock_state = self.dock_manager.saveState()  # 存默认排布（AI 已独立为浮窗、不在其中）

    def _reset_dock_layout(self):
        for d in self._dw_panels:  # 显示全部停靠面板（AI 是独立浮窗，不参与停靠重置）
            d.toggleView(True)
        self.dock_manager.restoreState(self._default_dock_state)
        self._applied_default_split = False
        self._apply_default_right_width()  # 重置后也回到合理右列宽（默认存档是 stretch 比例，可能仍偏窄）
        self.op_label.setText("面板布局已重置")

    def _build_statusbar(self):
        self.info_label = QtWidgets.QLabel("未加载图片")
        self.op_label = QtWidgets.QLabel("")
        self.fps_label = QtWidgets.QLabel("FPS -")
        self.fps_label.setVisible(False)  # FPS 浮标默认关（开发脚手架），调试菜单可开
        self.statusBar().addWidget(self.info_label)
        self.statusBar().addWidget(self.op_label, 1)

        # 缩放控件（−/zoom_label/+/适应/1:1）：PS/AI 式底部缩放，迁自原「视图」顶栏。
        # zoom_label 必须在 __init__ 里 zoomChanged.connect(_update_zoom_label) 之前建好——_build_statusbar 先于该 connect 调用，时序成立。
        def _mk_zoom_btn(text, tip, slot, w=34):
            b = QtWidgets.QToolButton()
            b.setText(text); b.setToolTip(tip)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setFixedSize(w, 22)  # 固定尺寸 → 状态栏一排按钮高低胖瘦一致
            b.clicked.connect(slot)
            return b

        self.zoom_label = QtWidgets.QLabel("100%")
        self.zoom_label.setMinimumWidth(52)
        self.zoom_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setStyleSheet("font-family:'Consolas','Menlo',monospace; font-weight:600;")  # 等宽→百分比变化不抖
        self.statusBar().addPermanentWidget(_mk_zoom_btn("−", "缩小", self.view.zoom_out, 28))
        self.statusBar().addPermanentWidget(self.zoom_label)
        self.statusBar().addPermanentWidget(_mk_zoom_btn("+", "放大", self.view.zoom_in, 28))
        self.statusBar().addPermanentWidget(_mk_zoom_btn("适应", "适应窗口 (Ctrl+0)", self.fit_view, 44))
        self.statusBar().addPermanentWidget(_mk_zoom_btn("1:1", "实际大小 100%", self.view.zoom_actual, 40))
        self.statusBar().addPermanentWidget(self.fps_label)

    # ---------- 可复用忙碌进度弹窗（indeterminate；仿 ai_panel 的 setRange(0,0) busy 无限滚动）----------
    def _begin_busy(self, label: str, cancelable: bool = True) -> "QtWidgets.QProgressDialog":
        # 不确定进度（联网单发请求，无逐张进度）→ QProgressDialog(0,0) 自动 busy 无限滚动条。
        # 模态盖住编辑区但事件循环继续转（busy 动画活、worker done 跨线程信号仍回主线程）。
        dlg = QtWidgets.QProgressDialog(label, "取消" if cancelable else None, 0, 0, self)
        dlg.setWindowTitle("请稍候")
        dlg.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)               # 立即弹出（默认 4000ms 会"看起来没反应"）
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)                 # 进度不确定，手动关，避免 Qt 误判 100% 自动关
        dlg.setValue(0)                         # 配合 minimumDuration(0) 强制立刻 show
        dlg.show()
        QtWidgets.QApplication.processEvents()  # 保证这一帧就画出来
        return dlg

    def _end_busy(self, dlg):
        if dlg is not None:
            # 关键：QProgressDialog.reset() 在未达 100% 时会发 canceled 信号 → 误触 _on_seg_cancel，
            # 把正常完成误判成"已取消"并自增 epoch 丢弃结果。先断开 canceled，程序化关闭不再走取消路径。
            try:
                dlg.canceled.disconnect()
            except (RuntimeError, TypeError):
                pass  # 没接过 canceled（如 GrabCut 这类不带取消语义的忙碌弹窗）
            dlg.reset()
            dlg.close()
            dlg.deleteLater()

    def _pick_color(self):
        c = QtWidgets.QColorDialog.getColor(self.brush_color, self, "画笔颜色")
        if c.isValid():
            self.brush_color = c
            self._refresh_color_btn()

    def _refresh_color_btn(self):
        css = _swatch_css(self.brush_color.name(), "#fff")
        self.color_btn.setStyleSheet(css)
        if hasattr(self, "_color_btn_m"):  # 选项栏镜像色块同步当前画笔色
            self._color_btn_m.setStyleSheet(css)

    # ---------- 文字 ----------
    # 文字工具 + 画布内打字方法已抽到 editor_text.TextMixin（行为不变）。

    def set_tool(self, tool: str):
        prev = getattr(self.view, "_tool", None)
        # B5：离开锚点工具 → 清 overlay（恢复整 item 可拖）；离开钢笔 → 取消未完成预览
        if prev == "node" and tool != "node":
            self._clear_node_overlay()
        if prev == "pen" and tool != "pen":
            self._cancel_pen()
        self.view.set_tool(tool)
        if tool == "node":
            self._enter_node_tool()  # 若已选中单个矢量 path 则立刻建 overlay
        if tool != "measure" and getattr(self, "_measure_p0", None) is not None:
            # 离开测量工具：编辑器侧读数/状态随画布线一并复位，避免对不可见的旧测量线设比例/拉直（修 MEDIUM）
            self._measure_p0 = None; self._measure_p1 = None; self._measure_angle = 0.0
            if hasattr(self, "_measure_label"):
                self._measure_label.setText(self._format_measure(None, None))
        if tool in self._tool_actions:
            self._tool_actions[tool].setChecked(True)
        self._sync_flyout(tool)  # flyout 工具组：按钮图标/默认动作跟随所选工具
        if prev == "brush" and tool != "brush":  # 离开选区画笔：清掉未松手的涂抹预览 + 累积掩码（即便切到其它选区工具也要清）
            self._remove_brush_preview(); self._brush_mask = None; self._brush_last = None
        if tool not in ("lasso", "brush", "wand", "rectsel"):  # 离开选区类工具(套索/矩形选框/选区画笔/魔棒)就清掉选区（对齐 onToolChange features.js:1192）
            self._clear_selection()
        if hasattr(self, "_opts_stack"):  # 上下文选项栏切到该工具对应页（QStackedWidget 负责显隐，无需再手动 setVisible）
            page = self._tool_opt_page.get(tool, "blank")
            self._opts_stack.setCurrentIndex(self._opt_pages.get(page, self._blank_idx))
        if hasattr(self, "_resize_handle") and self.active:  # 缩放手柄仅移动工具显示
            self._update_outline()
        if hasattr(self, "size_slider"):  # 进画笔/绘制/橡皮 → 刷新尺寸光圈
            self.view.set_brush_radius(self.size_slider.value() / 2.0)
        self._raise_tool_panel(tool)  # 工具联动：右侧功能区自动跳到对应面板

    # 选中工具时把右侧停靠面板切到对应那一个（PS 式工具选项跟随）：
    # 文字→文字属性；选区/抠图/绘制类→选区与拆解；移动→图层。抓手/缩放无对应面板，不切。
    def _raise_tool_panel(self, tool: str):
        if not hasattr(self, "_dw_sel"):
            return
        panel = {
            "text": self._dw_text,
            "lasso": self._dw_sel, "brush": self._dw_sel, "wand": self._dw_sel,
            "rectsel": self._dw_sel,
            "rect": self._dw_sel, "erase": self._dw_sel, "crop": self._dw_sel,
            "draw": self._dw_sel, "eraser": self._dw_sel,
            "move": self._dw_layer,
        }.get(tool)
        if panel is None:
            return
        try:
            if panel.isClosed():       # 被用户关掉过 → 先显示出来
                panel.toggleView(True)
            panel.setAsCurrentTab()    # 在所属标签组里把它顶到当前页
        except Exception:
            pass

    # ---------- 撤销 / 重做（快照整篇文档） ----------
    def _snapshot_layer(self, l: dict) -> dict:
        # 矢量层（kind='vector'）：把 velems 提升为撤销 SSOT。先 sync_items_to_velems 回灌 item 当前
        # pen/brush/pos/text/transform（视图→模型，单向数据流），再 clone_velems 深拷（QPainterPath 拷贝构造，
        # 见 RISK-1）。撤销时 _restore 用快照 velems 重建 items → 改色/改字/拖动/打组全可回滚（B3）。
        if l.get("kind") == "vector":
            svg_io.sync_items_to_velems(l.get("pairs", []))  # 视图→模型：回灌当前 item 状态
            return {"kind": "vector", "name": l["name"], "uid": l.get("uid"),
                    "group": l.get("group"), "visible": l.get("visible", True),
                    "locked": l.get("locked", False), "opacity": l.get("opacity", 1.0),
                    "z": (l["item"].zValue() if l.get("item") else 0),
                    "velems": svg_io.clone_velems(l["velems"]),  # 深拷快照（不再存 vlayer 引用）
                    "meta": l.get("meta"), "svg_path": l.get("svg_path")}
        return {
            "name": l["name"], "kind": l["kind"], "visible": l["visible"],
            "z": l["item"].zValue(),
            "pos": (l["item"].pos().x(), l["item"].pos().y()),
            "scale": l["item"].scale(),  # 缩放
            "locked": l.get("locked", False),
            "opacity": l.get("opacity", 1.0),  # 不透明度（跨撤销保留）
            # COW：存【引用】不深拷（QImage 隐式共享，O(1)）。唯一就地改像素的路径(_draw_to 画笔/橡皮)在
            # _paint_press 已先 copy 出新副本再画，故快照引用的旧像素不被污染；其余改像素的操作都重新赋值新 QImage。
            # 省掉每次 push 对【全部层】的全图深拷(大画布数十~数百 MB/步 + 历史常驻数 GB)。_restore 仍 .copy() 兜底。
            "image": l["image"],
            "text": l.get("text"),       # 文字层属性（可再编辑）
            "uid": l.get("uid"), "group": l.get("group"),  # 稳定 id + 分组（跨撤销保留）
            # 非破坏蒙版：存【引用】（当前切片蒙版改动都是整体赋新数组、不就地改，故引用安全）；_restore 再 .copy() 兜底。
            # 注意：将来加「在蒙版上涂抹」必须改成涂抹前先 copy（numpy 无隐式共享，否则会改穿所有引用此数组的快照）。
            "mask": l.get("mask"),
        }

    def _snapshot(self) -> dict:
        return {
            "canvas": self.canvas_size,
            "active": self.layers.index(self.active) if self.active in self.layers else -1,
            "layers": [self._snapshot_layer(l) for l in self.layers],
            # 素材库引用快照（素材一旦入库不再就地改像素，QImage 隐式共享/COW，存引用即可，开销小）
            # → 让"导入底图清空素材库""加入/删除素材"等可撤销（修审核 HIGH：撤销静默丢素材）。
            "assets": list(self.assets),
            "source_dpi": self.source_dpi, "source_name": self.source_name,  # 修审核 LOW：撤销回旧文档时 DPI/源名一致
            # 参考线快照（创建/清除入历史 → Ctrl+Z 能恢复被拖出/清掉的参考线）
            "guides_v": list(self.view._guides_v), "guides_h": list(self.view._guides_h),
        }

    def _push_history(self, label: str = "操作"):
        # 做一个新操作（调用方都在改动【前】调用）：截断当前指针之后的重做分支 → append 操作前快照 → 指针前移。
        # 等价旧双栈：_history.append(snap) + _redo.clear()。label 描述【这一步将要做的操作】，供历史面板显示。
        if self._suspend_history:
            return
        del self._history[self._hist_index + 1:]   # 做新操作丢弃后续重做分支（= 旧 _redo.clear）
        self._history.append({"snap": self._snapshot(), "label": label})
        self._hist_index = len(self._history) - 1
        if len(self._history) > HISTORY_CAP:        # 超上限丢最老一条 + 指针左移补偿，保 _hist_index 不变量
            self._history.pop(0)
            self._hist_index -= 1
        self._text_live_pushed = False  # 任何外部 push 都开启新一轮文字样式编辑的历史节点（修审核#4）
        self._vec_live_pushed = False   # 同理：外部 push 后矢量 spin 连改重新开启一个历史节点（B3 RISK-4）
        self._refresh_history()
        self._update_undo_actions()

    def _pop_last_history_if(self, label: str):
        # 撤掉刚 push 的一个空步（仅当它是最末步且 label 匹配，指针正指它）——空矢量改字回退用（B3）。
        if (self._history and self._hist_index == len(self._history) - 1
                and self._history[-1]["label"] == label):
            self._history.pop()
            self._hist_index -= 1
            self._refresh_history()
            self._update_undo_actions()

    def _restore(self, snap: dict):
        self._suspend_history = True
        self._suspend_vec_sel = True  # RISK-7：批量 removeItem 会狂发 selectionChanged，访问已移除 item 会崩 → 抑制
        # B5：node 工具下记住 overlay target 的 (层 uid, 顶层 pair 下标)，重建后据此重选 → undo/redo 不丢编辑上下文
        self._node_restore_key = None
        if self.view._tool == "node" and self._node_overlay is not None:
            lyr = self._node_overlay.get("layer")
            tgt = self._node_overlay.get("target")
            if lyr is not None and tgt is not None:
                self._node_restore_key = (lyr.get("uid"), self._vec_pair_index(lyr, tgt))
        self._clear_node_overlay()    # B5：target item 即将被 removeItem 重建，overlay 必须先清，否则悬空崩
        self._cancel_pen()            # B5：撤销时丢弃任何未完成的钢笔预览（其 item 非 layer，安全直接销毁）
        self._clear_selection()
        self._outline.setParentItem(None); self._outline.hide()
        self._resize_handle.setParentItem(None); self._resize_handle.hide()
        for l in self.layers:
            if l.get("kind") == "vector":
                for it in l.get("items", []):  # 矢量层有多个顶层 item，逐个移除（哨兵 l['item'] 不够）
                    self.scene.removeItem(it)
            else:
                self.scene.removeItem(l["item"])
        self.layers = []
        self.canvas_size = snap["canvas"]
        if self.canvas_size:
            self.scene.setSceneRect(0, 0, self.canvas_size[0], self.canvas_size[1])
        for d in snap["layers"]:
            if d.get("kind") == "vector":
                # 从快照 velems 重建矢量层：旧 items 已在上面批量 removeItem。build_items 从深拷 velems
                # 重建新 items（group VElem 自动重建 QGraphicsItemGroup 子树，结构恢复），重挂 scene + 重建 layer dict。
                velems = d["velems"]
                pairs = svg_io.build_items(velems)
                base_z = d["z"]
                visible = d.get("visible", True)
                locked = d.get("locked", False)
                opacity = d.get("opacity", 1.0)
                items = []
                for it, _ve in pairs:
                    it.setZValue(base_z)
                    self.scene.addItem(it)
                    it.setVisible(visible)
                    it.setOpacity(opacity)  # 恢复不透明度
                    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
                    self._wire_vec_item(it)  # 挂 itemChange 拖动回调（含 group 子项递归）
                    items.append(it)
                layer = {
                    "name": d["name"], "kind": "vector", "items": items, "pairs": pairs,
                    "velems": velems, "meta": d.get("meta"),
                    "item": items[0] if items else None,
                    "visible": visible, "locked": locked, "opacity": opacity,
                    "uid": d.get("uid"), "group": d.get("group"), "svg_path": d.get("svg_path"),
                }
                self.layers.append(layer)
                continue
            img = d["image"].copy()
            item = ImageLayerItem(img)
            item.setZValue(d["z"])
            item.setPos(d["pos"][0], d["pos"][1])
            item.setScale(d.get("scale", 1.0))
            item.setOpacity(d.get("opacity", 1.0))  # 恢复不透明度
            item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not d.get("locked", False))
            item._move_cb = self._push_history
            item._snap_cb = (lambda pos, _it=item: self._snap_drag_pos(_it, pos))  # 统一磁吸(吸到所有元素)
            item._release_cb = self._clear_guides_overlay
            item.setVisible(d["visible"])
            # 非破坏蒙版：防御性 .copy()（与上面 image 同理），让恢复出的活动蒙版与历史格独立，撤销/重做不互相污染
            rmask = d["mask"].copy() if d.get("mask") is not None else None
            item.set_mask(rmask)
            self.scene.addItem(item)
            layer = {"name": d["name"], "kind": d["kind"], "image": img, "item": item,
                     "visible": d["visible"], "locked": d.get("locked", False),
                     "opacity": d.get("opacity", 1.0), "text": d.get("text"),
                     "uid": d.get("uid"), "group": d.get("group"), "mask": rmask}
            item._press_cb = lambda L=layer: self._on_layer_pressed(L)
            self.layers.append(layer)
        uids = [l["uid"] for l in self.layers if l.get("uid")]
        if uids:
            self._layer_uid = max(self._layer_uid, *uids)  # 新层 uid 不与恢复的撞
        idx = snap["active"]
        self.active = self.layers[idx] if 0 <= idx < len(self.layers) else (self.layers[-1] if self.layers else None)
        self.selected_layers = [self.active] if self.active else []  # 重建层后旧选择集失效 → 收敛为 active
        self._active_guides = []  # 撤销/重做后清掉残留的智能参考线
        self.assets = list(snap.get("assets", []))  # 恢复素材库（引用）
        self.source_dpi = snap.get("source_dpi")
        self.source_name = snap.get("source_name")
        self.view._guides_v = list(snap.get("guides_v", []))  # 恢复参考线
        self.view._guides_h = list(snap.get("guides_h", []))
        self.view.viewport().update()
        self._suspend_history = False
        self._suspend_vec_sel = False
        self._vec_live_pushed = False  # 撤销/重做清 spin 连改合并标志，防跨 undo 漏一步(审核 LOW)
        if hasattr(self, "_vec_controls"):  # 撤销/重做后选中态失效 → 矢量面板置灰
            self._set_vec_controls_enabled(False)
        self._update_outline()
        self._refresh_layers()
        self._refresh_assets()
        self._sync_opacity_slider()  # 撤销/重做后不透明度滑块同步到 active 层
        if self.view._tool == "node":  # B5：撤销/重做后若仍在锚点工具 → 重选原 target 的对应新 item 并重建 overlay
            key = getattr(self, "_node_restore_key", None)
            if key is not None and key[1] >= 0:
                uid, pidx = key
                lyr = next((l for l in self._vector_layers() if l.get("uid") == uid), None)
                if lyr is not None and 0 <= pidx < len(lyr.get("pairs", [])):
                    new_it = lyr["pairs"][pidx][0]
                    if isinstance(new_it, QtWidgets.QGraphicsPathItem) \
                            and not isinstance(new_it, QtWidgets.QGraphicsItemGroup):
                        self._suspend_vec_sel = True
                        try:
                            self.scene.clearSelection()
                            new_it.setSelected(True)
                        finally:
                            self._suspend_vec_sel = False
            self._maybe_rebuild_node_overlay()
            self._node_restore_key = None

    def undo(self):
        # 指针 -1：把当前 live 快照换进边界格（= 旧 _redo.append(live)），恢复该格原存的快照（= 旧 _history.pop）。
        if self._hist_index < 0:
            return
        entry = self._history[self._hist_index]
        self._history[self._hist_index] = {"snap": self._snapshot(), "label": entry["label"]}
        self._restore(entry["snap"])
        self._hist_index -= 1
        self._update_undo_actions()
        self.op_label.setText("撤销")
        self._refresh_history()

    def redo(self):
        # 指针 +1：把当前 live 换进下一格（= 旧 _history.append(live)），恢复该格原存的快照（= 旧 _redo.pop）。
        if self._hist_index + 1 >= len(self._history):
            return
        entry = self._history[self._hist_index + 1]
        self._history[self._hist_index + 1] = {"snap": self._snapshot(), "label": entry["label"]}
        self._restore(entry["snap"])
        self._hist_index += 1
        self._update_undo_actions()
        self.op_label.setText("重做")
        self._refresh_history()

    def jump_to(self, i: int):
        # 历史面板点某步：跳到 i（前进或后退到该快照）。不截断（PS 行为：跳回旧步仍可前进/后退，直到做【新操作】才截断）。
        if not (0 <= i < len(self._history)) or i == self._hist_index:
            return
        while self._hist_index > i:   # 后退：逐格交换式 undo（保留中间步可重做）
            self.undo()
        while self._hist_index < i:   # 前进：逐格 redo
            self.redo()
        self.op_label.setText(f"跳到：{self._history[i]['label']}")
        self._update_undo_actions()
        self._refresh_history()

    def _update_undo_actions(self):
        self._undo_act.setEnabled(self._hist_index >= 0)
        self._redo_act.setEnabled(self._hist_index + 1 < len(self._history))

    # ---------- 图层 ----------
    # _add_layer / _layer_thumb / _refresh_layers / _vector_thumb 已抽到 editor_layers.LayersMixin（行为不变）。

    def _refresh_history(self):
        # 历史面板：列出时间线各步 label；当前步(_hist_index)粗体高亮，其后步骤灰显(可恢复)。仿 _refresh_layers。
        if not hasattr(self, "hist_list"):
            return
        self.hist_list.blockSignals(True)
        self.hist_list.clear()
        c = theme.colors()
        accent = QtGui.QColor(c.get("accent", "#2563eb"))
        muted = QtGui.QColor(c.get("muted", "#888888"))
        for i, rec in enumerate(self._history):
            it = QtWidgets.QListWidgetItem(rec["label"])
            it.setData(QtCore.Qt.ItemDataRole.UserRole, i)
            if i == self._hist_index:           # 当前步：粗体 + 强调色
                f = it.font(); f.setBold(True); it.setFont(f)
                it.setForeground(QtGui.QBrush(accent))
            elif i > self._hist_index:          # 其后步：灰显（可点回恢复，做新操作会截断）
                it.setForeground(QtGui.QBrush(muted))
            self.hist_list.addItem(it)
        if 0 <= self._hist_index < self.hist_list.count():
            self.hist_list.scrollToItem(self.hist_list.item(self._hist_index))
        self.hist_list.blockSignals(False)

    # _on_layer_row / _on_layer_pressed / _on_layer_selection_changed / _reselect_rows_by_uid 已抽到 editor_layers.LayersMixin。

    def _layer_scene_rect(self, layer: dict):  # 图层有效场景包围盒（左上角锚 + 统一缩放）
        if layer.get("kind") == "vector":  # 矢量层无 image → 用各 item 场景包围盒并集
            rect = QtCore.QRectF()
            for it in layer.get("items", []):
                rect = rect.united(it.sceneBoundingRect())
            return (rect.x(), rect.y(), rect.width(), rect.height())
        it = layer["item"]
        s = it.scale()
        return (it.pos().x(), it.pos().y(), it.image().width() * s, it.image().height() * s)

    # _align 已抽到 editor_layers.LayersMixin。

    def _nudge_active(self, dx: int, dy: int):  # 方向键微移当前层（itemChange 会自动入撤销，多次微移合并为一步）
        if self.active:
            self._suspend_snap = True   # 方向键 1px 精确微移：禁止智能吸附把它弹到附近参考线/边
            for it in self._layer_items(self.active):  # 矢量层整组同步微移（不再只动哨兵）
                it.setPos(it.pos() + QtCore.QPointF(dx, dy))
            self._suspend_snap = False
            self._clear_guides_overlay()

    # ---------- 标尺 / 参考线 ----------
    def _sync_rulers(self, *_):
        if getattr(self, "_rulers_visible", False):
            self._ruler_top.sync()
            self._ruler_left.sync()

    def _sync_ruler_h(self, *_):  # 横滚只动上标尺刻度（左标尺刻度没变，免无谓重建）
        if getattr(self, "_rulers_visible", False):
            self._ruler_top.sync()

    def _sync_ruler_v(self, *_):  # 竖滚只动左标尺刻度
        if getattr(self, "_rulers_visible", False):
            self._ruler_left.sync()

    def _on_cursor_scene(self, sp: QtCore.QPointF):  # 鼠标移动 → 标尺游标线跟随（只刷窄带，不重建刻度）
        if not getattr(self, "_rulers_visible", False):
            return
        self._ruler_top.set_cursor(sp.x())
        self._ruler_left.set_cursor(sp.y())

    def _on_ruler_drop(self, orient: str, vp_pt: QtCore.QPoint, final: bool):
        # 从标尺拖出参考线：vp_pt=view.viewport() 局部坐标。先移除上一条预览，再加新预览；
        # final=True 落定；落在 viewport 外或画布外则丢弃（大声失败：op_label 说明已取消）。
        in_vp = self.view.viewport().rect().contains(vp_pt)
        sp = self.view.mapToScene(vp_pt)
        lst = self.view._guides_v if orient == "v" else self.view._guides_h
        if getattr(self, "_guide_preview", None) == orient and lst:
            lst.pop()  # 去掉上一条预览
        self._guide_preview = None
        if not final:
            if in_vp:
                self.view.add_guide(orient, sp.x() if orient == "v" else sp.y())
                self._guide_preview = orient
            return
        # 落定：clamp 到画布范围，画布外不留孤儿参考线
        if not in_vp or not self.canvas_size:
            self.op_label.setText("参考线已取消")
            self.view.viewport().update()
            return
        cw, ch = self.canvas_size
        val = sp.x() if orient == "v" else sp.y()
        lim = cw if orient == "v" else ch
        if not (0 <= val <= lim):
            self.op_label.setText("参考线已取消（落在画布外）")
            return
        self._push_history("添加参考线")
        self.view.add_guide(orient, val)
        self.op_label.setText("已添加%s参考线 @ %d px" % ("竖直" if orient == "v" else "水平", int(val)))

    def _toggle_rulers(self, checked: bool):
        self._rulers_visible = bool(checked)
        for r in (self._ruler_top, self._ruler_left, self._ruler_corner):
            r.setVisible(self._rulers_visible)
        config.set_show_rulers(self._rulers_visible)
        self._sync_rulers()
        self.op_label.setText("标尺已%s" % ("显示" if self._rulers_visible else "隐藏"))

    def _toggle_guides(self, checked: bool):
        self.view.set_guides_visible(bool(checked))
        self.op_label.setText("参考线已%s" % ("显示" if checked else "隐藏"))

    def _clear_guides(self):
        if not self.view._guides_v and not self.view._guides_h:
            self.op_label.setText("没有参考线可清除"); return
        self._push_history("清除参考线")
        self.view.clear_guides()
        self.op_label.setText("已清除参考线")

    # 吸附阈值（屏幕像素）：与对齐功能吸附共用；对齐若落地则复用此阈值与 _snap_layer_pos。
    _SNAP_PX = 6

    def _all_drag_items(self):
        """画布上所有可作磁吸目标的顶层元素（栅格层 item + 矢量层各 item），跳过隐藏层。"""
        out = []
        for l in self.layers:
            if not l.get("visible", True):
                continue
            out.extend(self._layer_items(l))
        return out

    def _snap_drag_pos(self, item, new_pos: QtCore.QPointF) -> QtCore.QPointF:
        """统一磁吸：把【正在拖的任意元素】（栅格层/形状/箭头/文字/抠图/AI拆解/素材…）外框的
        左/中/右、上/中/下 吸到 其它元素 + 画布边·中线 + 参考线 + 网格。命中画洋红对齐线（AI/PS 式）。"""
        if getattr(self, "_suspend_snap", False) or self.view._tool != "move" or not self.canvas_size:
            return new_pos
        sbr = item.sceneBoundingRect()
        off = sbr.topLeft() - item.pos()  # 外框相对 pos 的偏移（含 transform/scale），随 pos 平移不变
        bw, bh = sbr.width(), sbr.height()
        cw, ch = self.canvas_size
        tol = self._SNAP_PX / max(1e-6, self.view.current_zoom())
        targets_x = [(x, (0.0, ch)) for x in self.view._guides_v]
        targets_x += [(0.0, (0.0, ch)), (cw / 2.0, (0.0, ch)), (cw, (0.0, ch))]
        targets_y = [(y, (0.0, cw)) for y in self.view._guides_h]
        targets_y += [(0.0, (0.0, cw)), (ch / 2.0, (0.0, cw)), (ch, (0.0, cw))]
        for other in self._all_drag_items():  # 其它每个元素的 左/中/右、上/中/下
            if other is item:
                continue
            r = other.sceneBoundingRect()
            sv = (r.top(), r.bottom()); sh = (r.left(), r.right())
            targets_x += [(r.left(), sv), (r.center().x(), sv), (r.right(), sv)]
            targets_y += [(r.top(), sh), (r.center().y(), sh), (r.bottom(), sh)]
        nl = new_pos.x() + off.x(); nt = new_pos.y() + off.y()
        guides = []; ndx = ndy = 0.0
        best = None
        for edge in (nl, nl + bw / 2.0, nl + bw):
            for t, span in targets_x:
                d = abs(edge - t)
                if d < tol and (best is None or d < best[0]):
                    best = (d, edge, t, span)
        if best is not None:
            ndx = best[2] - best[1]; a, b = best[3]
            guides.append({"orient": "v", "pos": best[2], "span": (min(a, nt) - 20, max(b, nt + bh) + 20)})
        best = None
        for edge in (nt, nt + bh / 2.0, nt + bh):
            for t, span in targets_y:
                d = abs(edge - t)
                if d < tol and (best is None or d < best[0]):
                    best = (d, edge, t, span)
        if best is not None:
            ndy = best[2] - best[1]; a, b = best[3]
            guides.append({"orient": "h", "pos": best[2], "span": (min(a, nl) - 20, max(b, nl + bw) + 20)})
        if getattr(self, "_snap_grid", False):  # 网格吸附：该轴没被元素/参考线吸住时吸到网格
            g = max(2, int(getattr(self.view, "_grid_size", 20)))
            if not any(gd["orient"] == "v" for gd in guides):
                ndx = (round(nl / g) * g) - nl
            if not any(gd["orient"] == "h" for gd in guides):
                ndy = (round(nt / g) * g) - nt
        if guides != self._active_guides:
            self._active_guides = guides
            self.view.viewport().update()
        self._refresh_connectors()  # 矢量元素拖动时连接线跟随（栅格靠 _post_move_cb）
        return QtCore.QPointF(new_pos.x() + ndx, new_pos.y() + ndy)

    def _snap_layer_pos(self, new_pos: QtCore.QPointF) -> QtCore.QPointF:
        # 移动图层时把外框左/中/右、上/中/下吸附到 参考线 / 画布边·中线 / 其它可见层的对应边·中线
        # （itemChange 内调用，返回吸附后的 pos）。命中的目标线存 _active_guides → drawForeground 画洋红虚线。
        if getattr(self, "_suspend_snap", False):  # 程序化移动(对齐/方向键/撤销恢复)不吸附，只在交互拖动时吸附
            return new_pos
        if self.active is None or self.view._tool != "move" or not self.canvas_size:
            return new_pos
        it = self.active["item"]
        bw = it.image().width() * it.scale()
        bh = it.image().height() * it.scale()
        cw, ch = self.canvas_size
        tol = self._SNAP_PX / max(1e-6, self.view.current_zoom())  # 屏幕像素→scene 容差（缩放越大容差越小，体验恒定）
        # 目标竖线 x：参考线 + 画布边/中线 + 其它可见层 左/中/右；横线 y 同理。带 span(用于画线长度)。
        targets_x = [(x, (0.0, ch)) for x in self.view._guides_v]
        targets_x += [(0.0, (0.0, ch)), (cw / 2.0, (0.0, ch)), (cw, (0.0, ch))]
        targets_y = [(y, (0.0, cw)) for y in self.view._guides_h]
        targets_y += [(0.0, (0.0, cw)), (ch / 2.0, (0.0, cw)), (ch, (0.0, cw))]
        for layer in self.layers:
            if layer is self.active or not layer.get("visible", True):
                continue
            ox, oy, ow, oh = self._layer_scene_rect(layer)
            ospan_v = (oy, oy + oh)  # 竖线沿该层 y 跨度画
            ospan_h = (ox, ox + ow)  # 横线沿该层 x 跨度画
            targets_x += [(ox, ospan_v), (ox + ow / 2.0, ospan_v), (ox + ow, ospan_v)]
            targets_y += [(oy, ospan_h), (oy + oh / 2.0, ospan_h), (oy + oh, ospan_h)]
        nx, ny = new_pos.x(), new_pos.y()
        guides = []
        # x 方向：层的 左/中/右 三条候选边对所有竖直目标找最近（< 容差）→ 一次最多吸一条
        best = None
        for edge in (nx, nx + bw / 2.0, nx + bw):
            for t, span in targets_x:
                d = abs(edge - t)
                if d < tol and (best is None or d < best[0]):
                    best = (d, nx + (t - edge), t, span)
        if best is not None:
            nx = best[1]
            a, b = best[3]
            guides.append({"orient": "v", "pos": best[2], "span": (min(a, ny) - 20, max(b, ny + bh) + 20)})
        best = None
        for edge in (ny, ny + bh / 2.0, ny + bh):
            for t, span in targets_y:
                d = abs(edge - t)
                if d < tol and (best is None or d < best[0]):
                    best = (d, ny + (t - edge), t, span)
        if best is not None:
            ny = best[1]
            a, b = best[3]
            guides.append({"orient": "h", "pos": best[2], "span": (min(a, nx) - 20, max(b, nx + bw) + 20)})
        if getattr(self, "_snap_grid", False):  # 网格吸附：该轴没被参考线吸住时取整到网格倍数（参考线优先）
            g = max(2, int(getattr(self.view, "_grid_size", 20)))
            if not any(gd["orient"] == "v" for gd in guides):
                nx = round(nx / g) * g
            if not any(gd["orient"] == "h" for gd in guides):
                ny = round(ny / g) * g
        if guides != self._active_guides:  # 只在变化时重绘，省刷新
            self._active_guides = guides
            self.view.viewport().update()
        return QtCore.QPointF(nx, ny)

    def _clear_guides_overlay(self):
        # 拖动结束清掉智能参考线洋红虚线（layer_item._release_cb 调用）。
        if self._active_guides:
            self._active_guides = []
            self.view.viewport().update()

    # _move_layer / _layer_z 已抽到 editor_layers.LayersMixin。

    def _reorder_layer(self, src_uid, dst_uid, before: bool):
        # 拖拽排序回调（DragLayerList.dropEvent）：把 src 层移到 dst 层的显示「之前/之后」。
        # MVP 限制（fail-loud）：只支持顶层栅格/文字/矢量层之间拖；带 group 的源/目标拒绝（不静默拖飞组成员）。
        by_uid = {l.get("uid"): l for l in self.layers}
        src = by_uid.get(src_uid); dst = by_uid.get(dst_uid)
        if src is None or dst is None or src is dst:
            return
        if src.get("group") or dst.get("group"):
            self.op_label.setText("组内 / 跨组拖拽暂不支持：请先「解组」，或用右键「上移/下移一层」")
            return
        self._push_history("调整层级")  # 改动【前】入历史 → Ctrl+Z 复原顺序
        # self.layers 是 底→顶（index 0=最底/最低 z）；面板倒序显示（顶层在上）。
        # 「显示中 src 在 dst 之前(上方)」= src 的 z 更高 = 在 self.layers 中排在 dst 之后(更大 index)。
        self.layers.remove(src)
        di = self.layers.index(dst)
        insert_at = di + 1 if before else di  # before(上方)=dst 之上=更大 index
        self.layers.insert(insert_at, src)
        for k, l in enumerate(self.layers):  # 重设 z：与面板顺序一致（矢量层整组同 z）
            for it in self._layer_items(l):
                it.setZValue(k)
        self._refresh_layers()  # active 不变（按 uid 持有），高亮随之重建
        self.op_label.setText("拖拽调整层级")

    # _delete_specific_layer / _delete_active / _rename_layer 已抽到 editor_layers.LayersMixin。

    # do_auto_decompose 已抽到 editor_selection.SelectionMixin。

    # ---------- AI 抠图/拆解（可插拔分割后端 → 落地素材库，与 do_auto_decompose 对齐）----------
    def _ai_seg_settings_dialog(self) -> bool:
        """轻量后端配置对话框（仿 ai_panel._save_conn 的清空+掩码回显）。
        保存返回 True；取消返回 False。key 保存后清空输入框，绝不在 UI 长驻明文。"""
        conn = config.get_seg_conn()
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("AI 抠图/拆解 · 后端设置")
        form = QtWidgets.QFormLayout(dlg)
        provider = QtWidgets.QComboBox()
        for val, label in seg_client.SEG_PROVIDERS:
            provider.addItem(label, val)
        pi = provider.findData(conn["provider"])
        if pi >= 0:
            provider.setCurrentIndex(pi)
        form.addRow("后端", provider)
        mode = QtWidgets.QComboBox()
        for val, label in seg_client.SEG_MODES:
            mode.addItem(label, val)
        mi = mode.findData(getattr(self, "_seg_mode", "elements"))
        if mi >= 0:
            mode.setCurrentIndex(mi)
        form.addRow("模式", mode)
        node = QtWidgets.QComboBox()  # grsai 节点（国内/国外）；仅 grsai 后端显示
        for _burl, _nlabel in seg_client.SEG_NODES:
            node.addItem(_nlabel, _burl)
        _ni = node.findData(conn.get("node") or config.grsai_base())
        if _ni >= 0:
            node.setCurrentIndex(_ni)
        form.addRow("节点", node)
        base_url = QtWidgets.QLineEdit(conn["base_url"])
        base_url.setPlaceholderText("分割后端地址，如 https://your-relay.example.com")
        form.addRow("地址", base_url)
        key_input = QtWidgets.QLineEdit()
        key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        key_input.setText(config.read_seg_key() or "")  # 回填已存 Key（密码打码，点眼睛才显），看得出已存
        key_input.setPlaceholderText("API Key（仅存本机 ~/.sciedit，不外传；点 👁 可查看）")
        key_eye = QtWidgets.QToolButton(); key_eye.setText("👁"); key_eye.setCheckable(True)
        key_eye.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        key_eye.setToolTip("显示 / 隐藏 Key（仅本机查看，不外传）")
        key_eye.toggled.connect(lambda on: key_input.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if on else QtWidgets.QLineEdit.EchoMode.Password))
        key_field = QtWidgets.QWidget()  # Key 输入 + 眼睛同一行；用容器整行随后端显隐
        _krow = QtWidgets.QHBoxLayout(key_field); _krow.setContentsMargins(0, 0, 0, 0); _krow.setSpacing(4)
        _krow.addWidget(key_input, 1); _krow.addWidget(key_eye)
        form.addRow("Key", key_field)
        model_input = QtWidgets.QComboBox(); model_input.setEditable(True)
        model_input.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        model_input.setToolTip("选模型或手填；grsai 用图像模型，PPIO 用 qwen/qwen-image-edit")
        form.addRow("模型", model_input)
        prompt_input = QtWidgets.QLineEdit(conn["prompt"])
        prompt_input.setPlaceholderText("留空=去背景；可填任意编辑指令")
        form.addRow("编辑指令", prompt_input)
        endpoint_input = QtWidgets.QLineEdit(conn["endpoint"])
        endpoint_input.setPlaceholderText("留空=默认 /v1/images/edits")
        form.addRow("端点", endpoint_input)
        result_input = QtWidgets.QLineEdit(conn["result_endpoint"])  # 仅 ppio 用
        result_input.setPlaceholderText("PPIO 异步取结果端点，留空=默认")
        form.addRow("结果端点", result_input)
        # 推荐 + 按后端只显示需要填的字段（local/grsai 零配置；ppio/http 才显示地址/端点等）
        hint_lbl = QtWidgets.QLabel(); hint_lbl.setObjectName("hint"); hint_lbl.setWordWrap(True)
        form.insertRow(1, hint_lbl)  # 放在「后端」下面

        def _seg_update_fields(*_):
            p = provider.currentData()
            _prev_model = model_input.currentText().strip()
            model_input.clear()
            if p == "grsai":
                import ai_client as _ac
                for _v, _l in _ac.MODELS:
                    model_input.addItem(_l, _v)
            elif p == "ppio":
                for _v, _l in seg_client.PPIO_MODELS:
                    model_input.addItem(_l, _v)
            # 选中：优先已保存模型；grsai/ppio 若存的是别后端的陈旧模型(或旧版误存的 label)则回落到本
            # 后端默认(第一项)，避免抠图框冒出"文生图"等不适用模型；http 等自由后端仍尊重手填。
            _want = conn["model"] or _prev_model
            _mi = model_input.findData(_want) if _want else -1
            if _mi >= 0:
                model_input.setCurrentIndex(_mi)
            elif _want and p not in ("grsai", "ppio"):
                model_input.setEditText(_want)
            elif model_input.count() > 0:
                model_input.setCurrentIndex(0)
            if p == "ppio":  # 帮用户把 PPIO 默认端点填好（留空也能跑，显式更清楚）
                if not endpoint_input.text().strip():
                    endpoint_input.setText(seg_client._PPIO_SUBMIT_ENDPOINT)
                if not result_input.text().strip():
                    result_input.setText(seg_client._PPIO_RESULT_ENDPOINT)
                endpoint_input.setPlaceholderText("PPIO 提交端点，默认 " + seg_client._PPIO_SUBMIT_ENDPOINT)
                result_input.setPlaceholderText("PPIO 取结果端点，默认 " + seg_client._PPIO_RESULT_ENDPOINT)
            elif p == "http":
                endpoint_input.setPlaceholderText("留空=默认 /v1/images/edits")
            # (地址, Key, 模型, 编辑指令, 端点, 结果端点) 各后端需要哪些
            vis = {
                "local": (0, 0, 0, 0, 0, 0),
                "grsai": (0, 0, 1, 0, 0, 0),
                "rembg": (0, 0, 0, 0, 0, 0),
                "ppio":  (0, 1, 1, 1, 1, 1),
                "http":  (1, 1, 1, 1, 1, 0),
            }.get(p, (1, 1, 1, 1, 1, 1))
            for w, v in zip((base_url, key_field, model_input, prompt_input, endpoint_input, result_input), vis):
                form.setRowVisible(w, bool(v))
            form.setRowVisible(node, p == "grsai")
            hints = {
                "local": "✅ 推荐·开箱即用：本地内置模型，离线去背景，无需地址/Key——直接点 Save。",
                "grsai": "✅ 推荐：复用「生图」里配好的 grsai Key。可在下方选『节点』(国内/国外) 和『模型』，留默认也行——直接 Save。难抠彩色图质量更好。",
                "ppio":  "PPIO Qwen-Image-Edit：填你的 PPIO Key；『模型』默认 qwen/qwen-image-edit；端点留空=默认。异步去背景/编辑。",
                "http":  "自定义中转：填 OpenAI image-edit 兼容的「地址」+「Key」。",
                "rembg": "建议改用上面的「本地内置模型」——同样本地，但无需自己 pip install。",
            }
            hint_lbl.setText(hints.get(p, ""))
            dlg.adjustSize()

        provider.currentIndexChanged.connect(_seg_update_fields)
        _seg_update_fields()
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        for b in dlg.findChildren(QtWidgets.QAbstractButton):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        self._seg_mode = mode.currentData()  # 记住模式供 do_ai_segment 用
        _md = model_input.currentData()  # 存真实模型 id（itemData），不是下拉显示的中文 label
        config.set_seg_conn(
            base_url=base_url.text().strip(), api_key=key_input.text(),
            model=(_md if _md else model_input.currentText().strip()),
            provider=provider.currentData(), node=node.currentData(),
            endpoint=endpoint_input.text().strip(),
            result_endpoint=result_input.text().strip(), prompt=prompt_input.text().strip())
        key_input.clear()  # 不在 UI 留明文
        return True

    # do_ai_segment / _on_seg_cancel 已抽到 editor_selection.SelectionMixin。

    def _on_ai_segment_done(self, cutouts, err):
        self._end_busy(self._seg_dialog)                   # 无条件关弹窗：成功/失败/空/取消/epoch 失配都要关（原则 12）
        self._seg_dialog = None
        sender = self.sender()
        if getattr(sender, "_epoch", -1) != self._seg_epoch:
            # 被取消或被新一次抠图顶替的旧 worker：丢弃结果，不入库、不弹框（避免污染素材库）。
            if sender is self._seg_worker:  # 清被丢弃 worker 引用，生命周期记账一致(审核 LOW)
                self._seg_worker = None
            return
        self._seg_worker = None
        if err:
            QtWidgets.QMessageBox.warning(self, "AI 抠图/拆解", err)  # fail-loud
            self.op_label.setText("AI 抠图失败：" + err)
            return
        if not cutouts:
            self.op_label.setText("AI 抠图：后端未返回元素")
            return
        n = 0
        for b64 in cutouts:
            img = self._b64_to_qimage(b64)
            if img is None or img.isNull():  # 大声失败：跳过无效，计数
                continue
            self.assets.append(img)  # _b64_to_qimage 已转 ARGB32_Premultiplied，无需重复转换
            n += 1
        self._refresh_assets()
        skipped = len(cutouts) - n
        self.op_label.setText("AI 抠图/拆解：%d 个素材已入库%s" % (
            n, ("（跳过 %d 张无效）" % skipped) if skipped else ""))  # 报跳过数（原则 12 Fail Loud）

    # ---------- 图层打组 / 解组（视图层分组，移植 layers.js）----------
    # _toggle_mark 已抽到 editor_layers.LayersMixin。

    def _marked_layers(self):
        return [l for l in self.layers if l.get("uid") in self._marked]

    # do_group / do_ungroup 已抽到 editor_layers.LayersMixin。

    def _set_group_visible(self, gid: str, vis: bool):
        self._push_history("组显隐")
        for l in self.layers:
            if l.get("group") == gid:
                l["visible"] = vis; l["item"].setVisible(vis)
                if not vis and l is self.active:
                    self.active = None
        self._update_outline(); self._refresh_layers()
        self.op_label.setText(f"{self._group_names.get(gid, gid)} {'显示' if vis else '隐藏'}")

    # _toggle_collapse / _set_active / _set_layer_visible / _set_layer_locked / _active_locked 已抽到 editor_layers.LayersMixin。

    # ---------- 图层不透明度（PS 图层面板 Opacity）：滑动预览不入史，松手提交入史 ----------
    def _apply_layer_opacity(self, layer: dict, v01: float):
        # 把 0..1 不透明度作用到该层的所有 item（矢量层整组）。
        for it in self._layer_items(layer):
            it.setOpacity(v01)

    # _on_opacity_preview / _on_opacity_commit 已抽到 editor_layers.LayersMixin。

    def _sync_opacity_slider(self):
        # active 切换 / 撤销重做后：把滑块/标签同步到当前激活层的不透明度（屏蔽 valueChanged 回写）。
        if not hasattr(self, "opacity_slider"):
            return
        v = int(round((self.active.get("opacity", 1.0) if self.active else 1.0) * 100))
        self._suspend_opacity_ui = True
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(v)
        self.opacity_slider.blockSignals(False)
        self.opacity_lbl.setText(f"{v}%")
        self.opacity_slider.setEnabled(self.active is not None)
        self._suspend_opacity_ui = False

    # _update_outline 已抽到 editor_selection.SelectionMixin。

    def _begin_resize(self):
        if self.active:
            self._resizing = True
            self._resize_pushed = False  # 推迟到 _do_resize 首次真正拖动才记历史（裸点手柄不产生空撤销步）

    def _do_resize(self, scene_pos):
        if not self.active:
            return
        if self.active.get("kind") == "vector":
            return  # 矢量层 B1 无缩放手柄（不会到这），安全护栏：避免 image() KeyError
        if not self._resize_pushed:  # 真正发生拖动 → 记一次历史（对齐 WebView didChange 时才 push）
            self._push_history("缩放"); self._resize_pushed = True
        item = self.active["item"]
        pos = item.pos()
        txt = self.active.get("text")
        if self.active.get("kind") == "text" and txt and txt.get("boxW"):  # M8: 文字定宽框 → 改框宽重排(不位图拉伸糊化)
            s = max(0.01, item.scale())
            new_w = max(40, int((scene_pos.x() - pos.x()) / s))
            props = dict(txt); props["boxW"] = new_w
            self.active["text"] = props
            img = self._make_text_image(props)
            self.active["image"] = img; item.set_image(img)  # top-left 锚，重排重绘
            self.op_label.setText(f"文本框宽 {new_w}px（自动重排）")
            return
        w = max(1, item.image().width())
        h = max(1, item.image().height())
        sx = (scene_pos.x() - pos.x()) / w
        sy = (scene_pos.y() - pos.y()) / h
        s = max(0.05, min(40.0, max(sx, sy)))  # 等比缩放（以左上角为锚）
        item.setScale(s)
        self.op_label.setText(f"缩放 {s * 100:.0f}%")

    def _end_resize(self):
        self._resizing = False
        if not self.active:
            return
        item = self.active["item"]
        if self.active.get("kind") == "text" and self.active.get("text", {}).get("boxW"):
            self._update_outline(); self._refresh_layers()  # 重排已在 _do_resize 完成，这里把手柄/缩略图收尾
            self.op_label.setText("文本框已重排")
            return
        s = item.scale()
        if abs(s - 1.0) > 1e-3:  # 把显示缩放“烤”进图像：重采样到实际尺寸，scale 复位 1
            img = self.active["image"]
            nw = max(1, round(img.width() * s))
            nh = max(1, round(img.height() * s))
            scaled = img.scaled(nw, nh, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                                QtCore.Qt.TransformationMode.SmoothTransformation)
            self.active["image"] = scaled
            item.set_image(scaled)
            item.setScale(1.0)
            self._update_outline()
            self._refresh_layers()
        self.op_label.setText("缩放完成（已应用到像素）")

    # delete_layer 已抽到 editor_layers.LayersMixin。

    def import_image(self):
        # 导入图片 = 直接加一层（不清空已有工作；空画布时按图设画布尺寸）。
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "导入图片", self._last_dir("import"), "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)")
        if not path:
            return
        self._remember_dir("import", path)
        t = time.perf_counter()
        img = QtGui.QImage(path)
        if img.isNull():
            QtWidgets.QMessageBox.warning(self, "导入失败", "无法读取该图片")
            return
        dpm = img.dotsPerMeterX()  # M17: 必须在 convertToFormat 丢元数据前取；0=无 DPI 信息
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)  # 顺带丢弃原文件元数据(tEXt/iCCP/C2PA)
        self._push_history("导入底图")  # 先入历史(捕获旧 source_dpi/name)，再覆盖 → 撤销能还原旧文档元数据(修审核 LOW)
        self.source_dpi = (dpm * 0.0254) if dpm > 0 else None
        self.source_name = QtCore.QFileInfo(path).completeBaseName()
        if self.canvas_size is None:  # 空画布 → 按图设画布；已有画布 → 直接追加，不动现有内容
            self.canvas_size = (img.width(), img.height())
            self.scene.setSceneRect(0, 0, img.width(), img.height())
            if getattr(self, "_measure_scale", None) is not None:  # 新文档分辨率变 → 复位旧测量比例，避免跨图误用
                self._measure_scale = None
                if hasattr(self, "_measure_scale_chk"):
                    self._measure_scale_chk.setChecked(False)
        self._add_layer(img, f"图片 {len(self.layers) + 1}", "image")
        self.fit_view()
        self._update_info()
        dpi_txt = f" · 源{self.source_dpi:.0f}DPI" if self.source_dpi else ""
        self.op_label.setText(f"导入 {(time.perf_counter() - t) * 1000:.0f} ms{dpi_txt}")

    def new_transparent_layer(self):
        # 文件菜单：新建【透明】图层（透明画布选项归到文件菜单）
        self._new_layer(transparent=True)

    def new_white_layer(self):
        # 图层面板按钮：新建【白色】图层（科研图白底更直观；透明请用文件菜单）
        self._new_layer(transparent=False)

    def _new_layer(self, transparent: bool):
        self._push_history("新建图层")
        w, h = self.canvas_size or DEFAULT_CANVAS
        if self.canvas_size is None:
            self.canvas_size = (w, h)
            self.scene.setSceneRect(0, 0, w, h)
        img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QtCore.Qt.GlobalColor.transparent if transparent else QtCore.Qt.GlobalColor.white)
        self._add_layer(img, f"图层 {len(self.layers) + 1}", "paint")
        self._update_info()
        self.set_tool("draw")  # 新建层 → 默认绘制工具；brush 现在是选区笔
        self.fit_view()

    def fit_view(self):
        if self.scene.items():
            self.view.fit(self.scene.itemsBoundingRect())
        elif self.canvas_size:
            self.view.fit(QtCore.QRectF(0, 0, self.canvas_size[0], self.canvas_size[1]))

    # ---------- 新建空白 / 画布尺寸 / 导入元素 ----------
    CANVAS_DLG_PRESETS = [
        ("自定义", None),
        ("1K  1024 × 1024", (1024, 1024)),
        ("2K  2048 × 2048", (2048, 2048)),
        ("4K  4096 × 4096", (4096, 4096)),
        ("正方形  2000 × 2000", (2000, 2000)),
        ("A4 纵  2480 × 3508 (300dpi)", (2480, 3508)),
        ("A4 横  3508 × 2480 (300dpi)", (3508, 2480)),
        ("A3 纵  3508 × 4961 (300dpi)", (3508, 4961)),
        ("A3 横  4961 × 3508 (300dpi)", (4961, 3508)),
        ("1080p / 16:9  1920 × 1080", (1920, 1080)),
        ("4:3 横  1600 × 1200", (1600, 1200)),
        ("4:3 竖  1200 × 1600", (1200, 1600)),
    ]

    def _ask_canvas_size(self, title, with_bg, init_w, init_h):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        form = QtWidgets.QFormLayout(dlg)
        combo = QtWidgets.QComboBox()
        for name, _ in self.CANVAS_DLG_PRESETS:
            combo.addItem(name)
        form.addRow("预设", combo)
        w_spin = QtWidgets.QSpinBox(); w_spin.setRange(16, 20000); w_spin.setValue(init_w)
        h_spin = QtWidgets.QSpinBox(); h_spin.setRange(16, 20000); h_spin.setValue(init_h)
        wh = QtWidgets.QHBoxLayout()
        wh.addWidget(QtWidgets.QLabel("宽")); wh.addWidget(w_spin, 1)
        wh.addSpacing(12)
        wh.addWidget(QtWidgets.QLabel("高")); wh.addWidget(h_spin, 1)
        form.addRow("尺寸(px)", wh)

        def on_preset(i):
            sz = self.CANVAS_DLG_PRESETS[i][1]
            w_spin.setEnabled(sz is None); h_spin.setEnabled(sz is None)
            if sz:
                w_spin.setValue(sz[0]); h_spin.setValue(sz[1])
        combo.currentIndexChanged.connect(on_preset)
        on_preset(0)

        bg_white = None
        if with_bg:
            bg_white = QtWidgets.QRadioButton("白色背景"); bg_white.setChecked(True)
            bg_trans = QtWidgets.QRadioButton("透明背景")
            bgrow = QtWidgets.QHBoxLayout(); bgrow.addWidget(bg_white); bgrow.addWidget(bg_trans); bgrow.addStretch(1)
            form.addRow("背景", bgrow)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        bg = ("white" if bg_white.isChecked() else "transparent") if with_bg else None
        return w_spin.value(), h_spin.value(), bg

    def new_blank(self):
        res = self._ask_canvas_size("新建空白", with_bg=True, init_w=1920, init_h=1080)
        if res is None:
            return
        w, h, bg = res
        if not self.layers:           # 当前空 → 直接设画布
            self._set_canvas_blank(w, h, bg)
        else:                         # 当前有内容 → 开新窗口，不清空
            win = EditorWindow()
            win._set_canvas_blank(w, h, bg)
            win.resize(self.size())
            win.show()
            QtCore.QTimer.singleShot(0, win.fit_view)  # 显示后再适应窗口（否则视口未定→缩成1%）

    def _set_canvas_blank(self, w: int, h: int, bg: str = "white"):
        self.canvas_size = (w, h)
        self.scene.setSceneRect(0, 0, w, h)
        if bg == "white":  # 默认白底：加一张白色背景层（科研图通常白底，不突兀）
            img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.white)
            self._add_layer(img, "背景", "image")
        self.info_label.setText(f"{w}×{h} · 空白画布（{'白底' if bg == 'white' else '透明'}）")
        self.fit_view()

    def canvas_size_dialog(self):
        cw, ch = self.canvas_size or DEFAULT_CANVAS
        res = self._ask_canvas_size("画布尺寸（保留所有内容）", with_bg=False, init_w=cw, init_h=ch)
        if res is None:
            return
        w, h, _ = res
        self._push_history("改画布尺寸")
        self.canvas_size = (w, h)
        self.scene.setSceneRect(0, 0, w, h)
        self.info_label.setText(f"{w}×{h} · {len(self.layers)} 层（已改画布，内容保留）")
        self.fit_view()

    def import_element(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "导入元素", self._last_dir("import"), "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)")
        if not path:
            return
        self._remember_dir("import", path)
        img = QtGui.QImage(path)
        if img.isNull():
            QtWidgets.QMessageBox.warning(self, "导入失败", "无法读取该图片")
            return
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        self._push_history("导入元素")  # 先入历史（在改 canvas_size 前）→ 撤销首次导入能还原空画布(canvas_size=None)，与 import_image 一致
        if self.canvas_size is None:
            self.canvas_size = (img.width(), img.height())
            self.scene.setSceneRect(0, 0, img.width(), img.height())
        cw, ch = self.canvas_size
        if img.width() > cw or img.height() > ch:  # 比画布大则缩放适配（只缩不放）
            img = img.scaled(cw, ch, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
        layer = self._add_layer(img, f"元素 {len(self.layers) + 1}", "image")
        off = 30 * (len(self.layers) % 6)  # 层叠偏移，避免完全重叠
        self._suspend_history = True       # 这次 setPos 不再单独入历史
        layer["item"].setPos(off, off)
        self._suspend_history = False
        self.set_tool("move")
        self.fit_view()

    # ---------- 矢量图（SVG）导入 / 导出（B1：与栅格层共存的 kind='vector' 层）----------
    # _vector_layers 已移至 editor_vector.VectorMixin。
    @staticmethod
    def _layer_items(layer: dict) -> list:
        # 矢量层=多个顶层 item；栅格/文字层=单个 item。统一返回列表，供显隐/锁定/层级整组操作。
        if layer.get("kind") == "vector":
            return layer.get("items", [])
        it = layer.get("item")
        return [it] if it is not None else []

    # _wire_vec_item 已移至 editor_vector.VectorMixin。
    def _b5_overlay_items(self) -> list:
        # B5 临时 item 显式清单（锚点 overlay + 钢笔预览）：导出前隐藏用，不靠遍历（推荐显式清单）。
        out = []
        if self._node_overlay is not None:
            out.extend(self._node_overlay.get("items", []))
        if self._pen_state is not None:
            out.extend(self._pen_state.get("preview_items", []))
            out.extend(self._pen_state.get("dots", []))
            r = self._pen_state.get("rubber")
            if r is not None and r not in out:
                out.append(r)
        return out

    # import_svg 已移至 editor_vector.VectorMixin。
    def import_pdf(self, path: str | None = None, page: int = 1):
        """导入 PDF（矢量）→ 外部工具(pdftocairo/pdf2svg/inkscape)转第 page 页为 SVG → 复用 import_svg。

        外部工具以独立子进程调用（非链接），故本程序仍可闭源分发（见 vector-mvp-scope.md）。
        多页 PDF 仅导入第 1 页并大声提示；找不到转换器/转换失败均 fail-loud。
        path 非空时跳过文件对话框（供离屏测试）。
        """
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "导入 PDF（矢量）", self._last_dir("import"), "PDF (*.pdf);;所有文件 (*.*)")
            if not path:
                return
        self._remember_dir("import", path)
        configured = config.get_pdf_converter()
        tmp_svg = pdf_import.make_temp_svg()
        try:
            res = pdf_import.convert_to_svg(path, tmp_svg, page=page, configured_path=configured)
            if not res["ok"]:  # fail-loud：缺工具/转换失败
                QtWidgets.QMessageBox.warning(self, "导入 PDF", res["error"])
                return
            n_layers_before = len(self.layers)
            self.import_svg(tmp_svg)  # 复用全部矢量解析/构建/撤销逻辑
            if len(self.layers) == n_layers_before:  # import_svg 内部已 fail-loud（空/无元素），不再重复
                return
            self.layers[-1]["svg_path"] = path  # 溯源指回原 PDF（而非临时 SVG）
            self.layers[-1]["name"] = f"PDF {len([l for l in self.layers if l.get('kind') == 'vector'])}"
        finally:
            try:
                os.remove(tmp_svg)  # 临时 SVG 已解析成图元，删掉（不静默留临时文件）
            except OSError:
                pass
        # 在 import_svg 的提示后追加 PDF 来源 + 多页提示（fail-loud：不静默吞掉其余页）
        note = f"（来自 PDF·{res['tool']} 转换）"
        n_pages = pdf_import.page_count(path)
        if n_pages and n_pages > 1:
            note += f" PDF 共 {n_pages} 页，仅导入第 {page} 页（其余页暂不支持）"
        elif n_pages is None:
            note += " 若此 PDF 为多页，仅导入了第 1 页"
        self.op_label.setText(self.op_label.text() + note)

    @staticmethod
    def _count_outlined_text(velems) -> int:
        # B1 解析：<text>→editable_text=True；路径化文字是 <path>，本身无 text 标记。
        # 此处统计源里【确实是 <text> 但被标 outlined】的（当前实现 <text> 恒 editable，返回 0；占位供 B2 细化）。
        n = 0
        for ve in velems:
            if ve.type == "text" and not ve.editable_text:
                n += 1
            if ve.type == "group":
                n += EditorWindow._count_outlined_text(ve.children)
        return n

    @staticmethod
    def _count_text(velems) -> int:
        # 可编辑 <text> 元素数（递归）；为 0 说明文字多半已路径化，import_svg 据此提示重导出（§0.2）。
        n = 0
        for ve in velems:
            if ve.type == "text" and ve.editable_text:
                n += 1
            if ve.type == "group":
                n += EditorWindow._count_text(ve.children)
        return n

    @staticmethod
    def _svg_canvas_size(meta, pairs):
        # 优先 width/height（去单位），否则 viewBox 宽高，否则元素并集包围盒，最后兜底 DEFAULT_CANVAS
        def num(s):
            if not s:
                return None
            t = str(s).strip()
            for u in ("px", "pt"):
                if t.endswith(u):
                    t = t[:-len(u)].strip(); break
            try:
                return float(t)
            except ValueError:
                return None
        w = num(meta.get("width")); h = num(meta.get("height"))
        if not w or not h:
            vb = meta.get("viewBox")
            if vb:
                parts = str(vb).replace(",", " ").split()
                if len(parts) == 4:
                    try:
                        w = w or float(parts[2]); h = h or float(parts[3])
                    except ValueError:
                        pass
        if not w or not h:
            rect = QtCore.QRectF()
            for it, _ in pairs:
                rect = rect.united(it.sceneBoundingRect())
            if rect.isValid() and rect.width() > 1 and rect.height() > 1:
                w = w or (rect.x() + rect.width()); h = h or (rect.y() + rect.height())
        return (int(round(w)) if w else DEFAULT_CANVAS[0],
                int(round(h)) if h else DEFAULT_CANVAS[1])

    # export_svg 已移至 editor_vector.VectorMixin。
    # export_pdf 已移至 editor_vector.VectorMixin。
    # _build_vec_panel 已移至 editor_vector.VectorMixin。
    # _set_vec_controls_enabled 已移至 editor_vector.VectorMixin。
    # _selected_velem_items 已移至 editor_vector.VectorMixin。
    # _vector_layer_of_item 已移至 editor_vector.VectorMixin。
    # _on_vec_selection_changed 已移至 editor_vector.VectorMixin。
    # _refresh_vec_path_controls 已移至 editor_vector.VectorMixin。
    # _refresh_vec_text_controls 已移至 editor_vector.VectorMixin。
    # _set_vec_swatch 已移至 editor_vector.VectorMixin。
    # _vec_target_paths 已移至 editor_vector.VectorMixin。
    # _vec_layer_locked_guard 已移至 editor_vector.VectorMixin。
    # _vec_pick_fill 已移至 editor_vector.VectorMixin。
    # _vec_toggle_nofill 已移至 editor_vector.VectorMixin。
    # _vec_pick_stroke 已移至 editor_vector.VectorMixin。
    # _vec_toggle_nostroke 已移至 editor_vector.VectorMixin。
    # _vec_change_stroke_width 已移至 editor_vector.VectorMixin。
    # _vec_target_texts 已移至 editor_vector.VectorMixin。
    # _vec_change_text_font 已移至 editor_vector.VectorMixin。
    # _vec_pick_text_color 已移至 editor_vector.VectorMixin。
    # _map_items_to_okabe 已移至 editor_vector.VectorMixin。
    # _vec_map_palette_selected 已移至 editor_vector.VectorMixin。
    # _vec_map_palette_layer 已移至 editor_vector.VectorMixin。
    # _refresh_vec_after_palette 已移至 editor_vector.VectorMixin。
    # _vec_pair_index 已移至 editor_vector.VectorMixin。
    # do_group_vec_elements 已移至 editor_vector.VectorMixin。
    # do_ungroup_vec_elements 已移至 editor_vector.VectorMixin。
    # _node_editable_path_target 已移至 editor_vector.VectorMixin。
    # _enter_node_tool 已移至 editor_vector.VectorMixin。
    # _maybe_rebuild_node_overlay 已移至 editor_vector.VectorMixin。
    # _build_node_overlay 已移至 editor_vector.VectorMixin。
    # _rebuild_node_overlay 已移至 editor_vector.VectorMixin。
    # _clear_node_overlay 已移至 editor_vector.VectorMixin。
    # _push_node_drag_history_once 已移至 editor_vector.VectorMixin。
    # _on_handle_released 已移至 editor_vector.VectorMixin。
    # _on_anchor_handle_pressed 已移至 editor_vector.VectorMixin。
    # _on_anchor_handle_dragged 已移至 editor_vector.VectorMixin。
    # _on_ctrl_handle_dragged 已移至 editor_vector.VectorMixin。
    # _refresh_node_overlay_positions 已移至 editor_vector.VectorMixin。
    # _refresh_ctrl_lines 已移至 editor_vector.VectorMixin。
    # _commit_node_edit 已移至 editor_vector.VectorMixin。
    # _node_click 已移至 editor_vector.VectorMixin。
    # _node_double_click 已移至 editor_vector.VectorMixin。
    # _add_anchor_at 已移至 editor_vector.VectorMixin。
    # _closest_on_seg 已移至 editor_vector.VectorMixin。
    # _toggle_anchor_corner 已移至 editor_vector.VectorMixin。
    # _delete_selected_anchors 已移至 editor_vector.VectorMixin。
    # _delete_node_target_path 已移至 editor_vector.VectorMixin。
    # _ensure_pen_state 已移至 editor_vector.VectorMixin。
    # _pen_press 已移至 editor_vector.VectorMixin。
    # _pen_drag_to 已移至 editor_vector.VectorMixin。
    # _pen_release 已移至 editor_vector.VectorMixin。
    # _pen_hover 已移至 editor_vector.VectorMixin。
    # _refresh_pen_preview 已移至 editor_vector.VectorMixin。
    # _pen_commit 已移至 editor_vector.VectorMixin。
    # _cancel_pen 已移至 editor_vector.VectorMixin。
    # _register_vec_path 已移至 editor_vector.VectorMixin。
    # _finish_pen 已移至 editor_vector.VectorMixin。
    # _shape_path 已移至 editor_vector.VectorMixin。
    # _shape_start 已移至 editor_vector.VectorMixin。
    # _shape_move 已移至 editor_vector.VectorMixin。
    # _shape_end 已移至 editor_vector.VectorMixin。
    # _note_vec_edit 已移至 editor_vector.VectorMixin。
    # _vec_live_push 已移至 editor_vector.VectorMixin。

    # ---------- 画笔 / 魔棒 事件 ----------
    def _paint_press(self, scene_pos: QtCore.QPointF):
        tool = self.view._tool
        if tool == "wand":
            self._wand_at(scene_pos); return
        if tool.startswith("sh_"):  # 形状工具：矩形/椭圆/直线/箭头
            self._shape_start(scene_pos); return
        if tool == "connector":  # 智能连接线：从对象拖到对象
            self._connector_start(scene_pos); return
        if tool in ("lasso", "brush") and self.selection_mask is not None \
                and self._point_in_selection(scene_pos):
            mods = QtWidgets.QApplication.keyboardModifiers()
            if not (mods & (QtCore.Qt.KeyboardModifier.ShiftModifier | QtCore.Qt.KeyboardModifier.AltModifier)):
                self.do_extract()  # lift-out(简化版)：无修饰键点在已有选区内 → 原位抠成可移动层(对齐 features.js:905-916)
                return
        if tool == "lasso":
            self._lasso_start(scene_pos); return
        if tool == "brush":            # 选区画笔：涂抹累积选区
            self._brush_sel_start(scene_pos); return
        if tool in ("rectsel", "rect", "erase", "crop"):  # 拖框类（矩形选框选区 / 矩形抠出 / 矩形挖洞 / 裁剪）
            self._rect_start(scene_pos); return
        if not self.active:            # draw / eraser → 在图层上画像素
            QtWidgets.QMessageBox.information(self, "提示", "请先「新建透明图层」或在右侧选中一个图层再画")
            return
        if self.active.get("kind") == "vector":  # 矢量层无像素画布 → 不能用像素画笔（fail-loud，不静默崩 KeyError）
            QtWidgets.QMessageBox.information(self, "提示", "矢量层不能用像素画笔/橡皮。请选中一个栅格图层，或用「移动」工具拖动矢量图元。")
            return
        if self._active_locked():
            return
        self._last_scene = scene_pos
        self._push_history("橡皮" if tool == "eraser" else "画笔")  # 记录这一笔之前的状态（撤销用）
        # COW：快照存的是【当前 image 引用】，本笔要就地改像素 → 先 detach 成新副本，使快照旧像素不被涂改(审核 性能)。
        self.active["image"] = self.active["image"].copy()
        self.active["item"].set_image(self.active["image"])  # item 指向新副本（_draw_to 改它，快照引用的旧图保持原样）
        self.active["item"].set_painting(True)  # 关缓存 → 这一笔走脏矩形快刷新
        self._draw_to(scene_pos)

    def _paint_move(self, scene_pos: QtCore.QPointF):
        tool = self.view._tool
        if tool.startswith("sh_"):
            self._shape_move(scene_pos); return
        if tool == "connector":
            self._connector_move(scene_pos); return
        if tool == "lasso":
            self._lasso_move(scene_pos); return
        if tool == "brush":
            self._brush_sel_move(scene_pos); return
        if tool in ("rectsel", "rect", "erase", "crop"):
            self._rect_move(scene_pos); return
        if tool in ("draw", "eraser") and self.active:
            self._draw_to(scene_pos)

    def _paint_end(self):
        tool = self.view._tool
        if tool.startswith("sh_"):
            self._shape_end(); return
        if tool == "connector":
            self._connector_end(); return
        if tool == "lasso":
            self._lasso_end(); return
        if tool == "brush":
            self._brush_sel_end(); return
        if tool == "rectsel":
            self._rectsel_end(); return
        if tool == "rect":
            self._rect_end(); return
        if tool == "erase":
            self._erase_rect_end(); return
        if tool == "crop":
            self._crop_end(); return
        if self.active and tool in ("draw", "eraser"):
            self.active["item"].set_painting(False)  # 恢复缓存 → 之后拖动丝滑

    # _draw_to 已抽到 editor_selection.SelectionMixin。

    # ---------- OpenCV 魔棒 / 去背景 / 抠出 ----------
    # _wand_at 已抽到 editor_selection.SelectionMixin。

    def _set_sel_mode(self, k: str):
        # 选区模式唯一入口：写状态 + 同步右侧原按钮组和选项栏镜像按钮组（两个入口指向同一状态，DRY）。
        self._sel_mode = k
        for grp_name in ("_mode_btns", "_mode_btns_m"):
            grp = getattr(self, grp_name, None)
            if not grp:
                continue
            for key, btn in grp.items():
                btn.blockSignals(True)
                btn.setChecked(key == k)
                btn.blockSignals(False)

    # ----- 选区模式合成 -----
    def _effective_mode(self) -> str:
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:
            return "add"
        if mods & QtCore.Qt.KeyboardModifier.AltModifier:
            return "subtract"
        return self._sel_mode

    # _compose_selection / _set_selection / _point_in_selection 已抽到 editor_selection.SelectionMixin。

    # ----- 套索 / 矩形 -----
    def _need_active_for_sel(self) -> bool:
        if not self.active:
            QtWidgets.QMessageBox.information(self, "提示", "选区作用在图层上：请先选中或新建一个图层")
            return False
        if self.active.get("kind") == "vector":  # 矢量层无像素 → 选区/魔棒不适用（fail-loud，不静默崩）
            QtWidgets.QMessageBox.information(self, "提示", "矢量层没有像素，无法用魔棒/套索/选区工具。请选中一个栅格图层。")
            return False
        return True

    # _lasso_start / _lasso_move / _lasso_end / _rect_start / _rect_move / _rect_end /
    # _rectsel_end / _erase_rect_end 已抽到 editor_selection.SelectionMixin。

    # ---------- 测量 / 标尺工具（PS 标尺：拉测量线读 X/Y/W/H/角度/长度，可设比例、按角度拉直活动层）----------
    def _format_measure(self, p0, p1) -> str:
        if p0 is None or p1 is None:
            return "X: -    Y: -    W: -    H: -    A: -°    L1: -    L2: -"
        x, y = p0.x(), p0.y()
        dx = p1.x() - p0.x(); dy = p1.y() - p0.y()
        l1 = math.hypot(dx, dy)
        a = math.degrees(math.atan2(-dy, dx))  # PS：水平向右=0，逆时针为正
        if abs(a) < 0.05:
            a = 0.0  # 归一化负零，避免水平线显示 -0.0°
        sc = self._measure_scale if (self._measure_scale and self._measure_scale_chk.isChecked()) else None
        if sc:
            upp, unit = sc

            def cv(px):
                return f"{px * upp:.3g}{unit}"
            return (f"X: {x:.0f}  Y: {y:.0f}  W: {cv(dx)}  H: {cv(dy)}  "
                    f"A: {a:.1f}°  L1: {cv(l1)}  L2: -")
        return (f"X: {x:.0f}  Y: {y:.0f}  W: {dx:.0f}  H: {dy:.0f}  "
                f"A: {a:.1f}°  L1: {l1:.1f}  L2: -")

    def _on_measure_changed(self, p0: QtCore.QPointF, p1: QtCore.QPointF):
        self._measure_p0 = QtCore.QPointF(p0); self._measure_p1 = QtCore.QPointF(p1)
        self._measure_angle = math.degrees(math.atan2(-(p1.y() - p0.y()), p1.x() - p0.x()))
        if hasattr(self, "_measure_label"):
            self._measure_label.setText(self._format_measure(p0, p1))

    def _on_measure_scale_toggled(self, _on: bool):
        # 切换"使用测量比例" → 立即按当前线刷新读数（真实单位 ↔ 像素）
        self._measure_label.setText(self._format_measure(self._measure_p0, self._measure_p1))

    def _set_measure_scale(self):
        if self._measure_p0 is None or self._measure_p1 is None:
            self.op_label.setText("请先拖一条测量线，再设比例"); return
        l1 = math.hypot(self._measure_p1.x() - self._measure_p0.x(),
                        self._measure_p1.y() - self._measure_p0.y())
        if l1 < 1e-6:
            self.op_label.setText("测量线太短，无法设比例"); return
        val, ok = QtWidgets.QInputDialog.getDouble(
            self, "设测量比例", f"当前测量线 = {l1:.1f} px，代表多少真实单位？",
            value=1.0, minValue=1e-9, maxValue=1e9, decimals=6)
        if not ok:
            return
        unit, ok2 = QtWidgets.QInputDialog.getText(self, "设测量比例", "单位名（如 mm / µm / nm）：", text="µm")
        if not ok2:
            return
        unit = unit.strip() or "u"
        self._measure_scale = (val / l1, unit)  # units per pixel
        self._measure_scale_chk.setChecked(True)  # 设完即启用换算
        self._measure_label.setText(self._format_measure(self._measure_p0, self._measure_p1))
        self.op_label.setText(f"已设比例：{l1:.1f}px = {val:g}{unit}（{val / l1:.4g}{unit}/px）")

    def _clear_measure(self):
        self._measure_p0 = None; self._measure_p1 = None; self._measure_angle = 0.0
        self.view.clear_measure()
        if hasattr(self, "_measure_label"):
            self._measure_label.setText(self._format_measure(None, None))
        self.op_label.setText("已清除测量线")

    def _straighten_active(self):
        # 按测量线角度旋转活动层使该线水平：位图重采样烤进 image（保持 _layer_scene_rect 无旋转假设）。
        if self._measure_p0 is None or self._measure_p1 is None:
            self.op_label.setText("请先拖一条测量线，再拉直"); return
        if not self.active:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中要拉直的图层"); return
        if self._active_locked():
            return
        ang = self._measure_angle  # 度，水平=0、逆时针正
        if abs(ang) < 0.05:
            self.op_label.setText(f"测量线已近水平（{ang:.2f}°），无需拉直"); return
        item = self.active["item"]; img = self.active["image"]
        center_scene = item.mapToScene(QtCore.QPointF(img.width() / 2.0, img.height() / 2.0))
        self._push_history("拉直")
        # 测量角 ang=atan2(-dy,dx)（屏幕 y 向下）；QTransform().rotate(ang) 把该线转成水平。
        # 已对上/下倾斜线分别验证正确，符号无需再翻。
        t = QtGui.QTransform().rotate(ang)
        rotated = img.transformed(t, QtCore.Qt.TransformationMode.SmoothTransformation)
        self.active["image"] = rotated
        item.set_image(rotated)
        s = item.scale()
        new_tl = QtCore.QPointF(center_scene.x() - rotated.width() * s / 2.0,
                                center_scene.y() - rotated.height() * s / 2.0)  # 旋转后按中心保位
        self._suspend_history = True
        item.setPos(new_tl)
        self._suspend_history = False
        self._clear_measure()
        self._update_outline(); self._refresh_layers()
        self.op_label.setText(f"已按 {ang:.2f}° 拉直活动层")

    # _crop_end 已抽到 editor_selection.SelectionMixin。

    # ----- 选区画笔（涂抹累积选区，对齐 WebView brush 选区笔 features.js:923-944）-----
    # _brush_sel_start 已抽到 editor_selection.SelectionMixin。

    def _brush_stamp(self, sp0, sp1):
        if self._brush_mask is None or not self.active:
            return
        item = self.active["item"]
        a = item.mapFromScene(sp0); b = item.mapFromScene(sp1)
        image_ops.draw_brush_segment(self._brush_mask, a.x(), a.y(), b.x(), b.y(), self.size_slider.value())

    # _brush_sel_move 已抽到 editor_selection.SelectionMixin。

    def _update_brush_preview(self):
        """涂抹中：把已涂的 _brush_mask 轮廓画成预览（accent 虚线描边 + 极淡填充），随涂增长。
        作 active item 子项 → 自动跟随层位移/缩放（同蚂蚁线机制，免手动坐标变换）；松手/Esc/切工具清。"""
        if self._brush_mask is None or not self.active or not self._brush_mask.any():
            return
        path = self._mask_to_path(self._brush_mask)  # 与蚂蚁线共用轮廓提取，避免两份代码漂移
        if self._brush_preview is None:
            item = self.active["item"]
            prev = QtWidgets.QGraphicsPathItem(path, item)
            col = QtGui.QColor(theme.colors()["accent"])
            pen = QtGui.QPen(col, 0); pen.setCosmetic(True); pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            prev.setPen(pen)
            fill = QtGui.QColor(col); fill.setAlpha(48)  # 极淡半透明填充 → 看得到"涂了多少"
            prev.setBrush(QtGui.QBrush(fill))
            prev.setZValue(59)  # 在蚂蚁线(60/61)之下，定稿后被蚂蚁线覆盖也无妨（此时预览已清）
            prev.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            self._brush_preview = prev
        else:
            self._brush_preview.setPath(path)  # 增量：只换轮廓，不重建 item

    def _remove_brush_preview(self):
        if self._brush_preview is not None and self._brush_preview.scene() is not None:
            self.scene.removeItem(self._brush_preview)
        self._brush_preview = None

    # _brush_sel_end 已抽到 editor_selection.SelectionMixin。

    def _rasterize_lasso(self, scene_points):
        item = self.active["item"]; img = self.active["image"]
        pts = []
        for sp in scene_points:
            lp = item.mapFromScene(sp); pts.append([lp.x(), lp.y()])
        return image_ops.polygon_mask(img.height(), img.width(), pts)

    def _rasterize_rect(self, sp0, sp1):
        item = self.active["item"]; img = self.active["image"]
        p0 = item.mapFromScene(sp0); p1 = item.mapFromScene(sp1)
        return image_ops.rect_mask(img.height(), img.width(), int(p0.x()), int(p0.y()), int(p1.x()), int(p1.y()))

    # ----- 拖动预览（场景坐标虚线）-----
    # _start_preview / _update_preview_path / _update_preview_rect / _remove_preview 已抽到 editor_selection.SelectionMixin。

    # ----- 蚂蚁线（选区轮廓，黑白虚线动画，作激活层子项随层移动）-----
    # _mask_to_path / _show_ants / _tick_ants / _remove_ants / _clear_selection / reselect /
    # _layer_alpha_in_active_space / _load_layer_as_selection / _need_selection 已抽到 editor_selection.SelectionMixin。

    # ---------- 非破坏图层蒙版（Phase 1：从选区生成；涂抹工具后续增量）----------
    # _mask_from_selection / _delete_mask 已抽到 editor_layers.LayersMixin。

    # do_remove_bg / do_grabcut / do_delete_selection 已抽到 editor_selection.SelectionMixin。

    def brightness_contrast_dialog(self):
        # PS「图像>调整>亮度/对比度」：实时预览，OK 提交一步可撤销，Cancel/关窗还原原图。
        layer = self.active
        if layer is None or layer.get("image") is None:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中或新建一个图层，再调整亮度/对比度")
            return
        if self._active_locked():
            return
        orig_qimg = layer["image"]                          # QImage 隐式共享，存引用即原图基准
        orig_rgba = image_ops.qimage_to_rgba(orig_qimg)     # 一次转换；滑动时复用，不再 qimage_to_rgba

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("亮度/对比度")
        form = QtWidgets.QFormLayout(dlg)

        def row(lo, hi):
            s = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            s.setRange(lo, hi); s.setValue(0)
            lbl = QtWidgets.QLabel("0"); lbl.setMinimumWidth(38)
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            box = QtWidgets.QWidget(); h = QtWidgets.QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            h.addWidget(s, 1); h.addWidget(lbl)
            return s, lbl, box

        b_slider, b_lbl, b_box = row(-150, 150)
        c_slider, c_lbl, c_box = row(-100, 100)
        form.addRow("亮度", b_box)
        form.addRow("对比度", c_box)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)

        def apply_preview():
            b_lbl.setText(f"{b_slider.value():+d}"); c_lbl.setText(f"{c_slider.value():+d}")
            out = image_ops.adjust_brightness_contrast(orig_rgba, b_slider.value(), c_slider.value())
            img = image_ops.rgba_to_qimage(out)
            layer["image"] = img; layer["item"].set_image(img)  # 仅刷新显示，不入历史
        b_slider.valueChanged.connect(lambda _: apply_preview())
        c_slider.valueChanged.connect(lambda _: apply_preview())

        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            b, c = b_slider.value(), c_slider.value()
            # 关键：先把层还原成原图再 _push_history → 历史快照=【调整前】，Ctrl+Z 回到真原图
            layer["image"] = orig_qimg; layer["item"].set_image(orig_qimg)
            if b == 0 and c == 0:
                self.op_label.setText("亮度/对比度：无变化"); return  # 全 0 不污染历史
            self._push_history("亮度/对比度")
            out = image_ops.adjust_brightness_contrast(orig_rgba, b, c)
            new_img = image_ops.rgba_to_qimage(out)
            layer["image"] = new_img; layer["item"].set_image(new_img)
            self.op_label.setText(f"亮度 {b:+d} / 对比度 {c:+d} 已应用")
        else:  # Cancel / ESC / 关窗 → 还原原图，绝不入历史
            layer["image"] = orig_qimg; layer["item"].set_image(orig_qimg)

    # _crop_selection / _extract_shortcut 已抽到 editor_selection.SelectionMixin。

    def _layer_via_copy(self):
        # Ctrl+J（PS「通过拷贝的图层」）：有选区→复制选区到新层(不改源层)；无选区→复制当前层。
        if self.active is not None and self.active.get("kind") == "vector":
            self.op_label.setText("矢量层暂不支持复制图层 (Ctrl+J)（B3 接）"); return
        if self.selection_mask is not None and self.active is not None:
            was = self.hole_check.isChecked()
            self.hole_check.setChecked(False)  # via copy 永远是复制，不留洞
            try:
                self.do_extract()
            finally:
                self.hole_check.setChecked(was)
        elif self.active is not None:
            self._duplicate_layer(self.active)

    def _duplicate_layer(self, layer: dict):
        self._push_history("复制图层")
        new = self._add_layer(layer["image"].copy(), layer.get("name", "图层") + " 副本", layer.get("kind", "image"))
        self._suspend_history = True
        new["item"].setScale(layer["item"].scale())
        new["item"].setPos(layer["item"].pos() + QtCore.QPointF(12, 12))  # 轻微错开，便于区分
        if layer.get("text"):
            new["text"] = dict(layer["text"])
        self._suspend_history = False
        self.set_tool("move")
        self.op_label.setText("已复制图层（Ctrl+J）")

    # do_extract 已抽到 editor_selection.SelectionMixin。

    # 内存素材库（加入/放回画布/导出/删除/清空）已抽到 editor_assets.AssetsMixin。

    # ---------- 导出 ----------
    @staticmethod
    def _last_dir(key: str) -> str:
        """读上次用的目录（分类记忆：base/element/export/project），对齐 WebView last-*-dir。"""
        return QtCore.QSettings("NanoPro", "SciEditQt").value(f"dir/{key}", "") or ""

    @staticmethod
    def _remember_dir(key: str, path: str):
        if path:
            QtCore.QSettings("NanoPro", "SciEditQt").setValue(f"dir/{key}", QtCore.QFileInfo(path).absolutePath())

    def _save_start(self, key: str, name: str) -> str:
        d = self._last_dir(key)
        return (d + "/" + name) if d else name

    def _update_info(self):
        """状态栏信息：画布尺寸 · 层数 · 印刷达标(单/双栏 DPI)。"""
        if self.canvas_size:
            w, h = self.canvas_size
            self.info_label.setText(f"{w}×{h} · {len(self.layers)} 层 · {self._print_dpi_text(w)}")
        else:
            self.info_label.setText(f"{len(self.layers)} 层")

    # ---------- 翻转 / 旋转（矢量元素用 QTransform 往返导出；栅格转像素，三处导出一致）----------
    def _transform_targets(self):
        """要变换的对象：选中的矢量元素优先；否则活动层。
        返回 ("vector", [items]) / ("raster", layer) / (None, None)。"""
        sel = [it for it in self.scene.selectedItems() if self._vector_layer_of_item(it) is not None]
        if sel:
            return "vector", sel
        if self.active is None:
            return None, None
        if self.active.get("kind") == "vector":
            return "vector", list(self.active.get("items") or [])
        if self.active.get("image") is not None:
            return "raster", self.active
        return None, None

    # _sync_vec_layers 已移至 editor_vector.VectorMixin。

    # _flip_objects / _rotate_objects 已抽到 editor_layers.LayersMixin。

    # _render_composite / _print_dpi_text / _apply_export_dpi 已抽到 editor_export.ExportMixin。

    # ---------- AI 生成结果落地（供 ai_panel 调用，主线程）----------
    def _ai_snapshot_b64(self):
        """当前可见层合成 → PNG base64，作图生图参考。空则 None。"""
        out = self._render_composite()
        return self._qimage_to_b64(out) if out is not None else None

    def _ai_ref_layer_b64(self):
        """当前活动图层像素 → PNG base64，作图生图参考；无活动层则 None。
        用于「只拿这一张图层」迭代重绘（区别于整画布合成）。"""
        if not self.active:
            return None
        return self._qimage_to_b64(self.active["image"])

    def _ai_ref_selection_b64(self):
        """当前选区外接矩形内的画布合成 → PNG base64，作图生图参考；无选区/空则 None。
        取「所见合成」矩形裁到选区 bbox（不抠形状、不留透明洞）——给模型一块干净的局部参考。"""
        if self.selection_mask is None or not self.selection_mask.any():
            return None
        bbox = image_ops.mask_bbox(self.selection_mask)
        if bbox is None:
            return None
        comp = self._render_composite()
        if comp is None:
            return None
        x0, y0, x1, y1 = bbox
        sub = comp.copy(x0, y0, x1 - x0, y1 - y0)
        return self._qimage_to_b64(sub) if not sub.isNull() else None

    def _ai_ref_from_files(self, paths):
        """外部参考图文件 → PNG base64 列表（照搬 sci-figure --ref，支持多张；统一转 PNG）。
        返回 (list, err)：err 非 None 表示某张读不出（fail-loud）。"""
        out = []
        for p in paths:
            img = QtGui.QImage(p)
            if img.isNull():
                return None, "无法读取参考图：%s" % p
            out.append(self._qimage_to_b64(img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)))
        return out, None

    # 本地素材库（连接文件夹/分类/缩略图/搜索/收藏/拆分/导出/放画布）已抽到 editor_assets.AssetsMixin。

    def copy_to_clipboard(self):
        """复制当前激活图层为图片到系统剪贴板（可 Ctrl+V 粘回，或粘到外部应用）。"""
        layer = self.active
        if not layer:
            self.op_label.setText("没有可复制的图层（先选中一个图层）"); return
        if layer.get("kind") != "vector" and layer.get("image") is not None:
            import image_ops
            img = image_ops.masked_qimage(layer["image"], layer.get("mask"))  # 含蒙版的实际显示
        else:
            img = self._render_layer_image(layer)  # 矢量层：隔离渲染成图
        if img is None or img.isNull():
            self.op_label.setText("该图层无可复制内容"); return
        QtWidgets.QApplication.clipboard().setImage(img)
        self.op_label.setText("已复制图层到剪贴板（Ctrl+V 粘回 / 可粘到外部应用）")

    def paste_from_clipboard(self):
        """从系统剪贴板取图片，作为新图层粘到画布（外部应用复制的图也能粘进来）。"""
        img = QtWidgets.QApplication.clipboard().image()
        if img is None or img.isNull():
            self.op_label.setText("剪贴板里没有图片"); return
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba); buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG"); buf.close()
        b64 = bytes(ba.toBase64()).decode("ascii")
        self._push_history("粘贴")
        if self._ai_place_b64(b64, push=False) is not None:  # 复用落图（居中/空画布作底图）
            self.op_label.setText("已粘贴剪贴板图片为新图层")
        else:
            self.op_label.setText("粘贴失败：图片无法解码")

    def _render_layer_image(self, layer):
        """把一个图层（含矢量层）隔离渲染成 QImage（透明底）：临时隐藏其它层 → scene.render 本层包围盒。"""
        items = list(layer.get("items") or [])
        if not items and layer.get("item") is not None:
            items = [layer["item"]]
        if not items:
            return None
        rect = QtCore.QRectF()
        for it in items:
            rect = rect.united(it.sceneBoundingRect())
        if rect.width() < 1 or rect.height() < 1:
            return None
        hidden = []
        for l in self.layers:  # 隔离：临时藏掉其它层，只渲染本层
            if l is layer:
                continue
            sibs = list(l.get("items") or [])
            if l.get("item") is not None:
                sibs.append(l["item"])
            for it in sibs:
                if it.isVisible():
                    it.setVisible(False); hidden.append(it)
        try:
            img = QtGui.QImage(int(rect.width()) + 1, int(rect.height()) + 1,
                               QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(img)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            self.scene.render(p, QtCore.QRectF(0, 0, img.width(), img.height()), rect)
            p.end()
        finally:
            for it in hidden:
                it.setVisible(True)
        return img

    def _ai_place_b64(self, b64: str, push: bool = True):
        """AI 结果 base64 落到画布：无图层→作底图；已有内容→作可移动图层(≤55%居中)。返回 layer 或 None。
        push=False：不入历史（并行生图一个任务多张时只在首张 push 一次，整任务合并为一步撤销，省全文档快照·审核 性能）。"""
        ba = QtCore.QByteArray.fromBase64(str(b64).encode("ascii"))
        img = QtGui.QImage()
        if not img.loadFromData(ba, "PNG") or img.isNull():
            return None
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        if push:
            self._push_history("AI 生成落图")
        if not self.layers:
            self.canvas_size = (img.width(), img.height())
            self.scene.setSceneRect(0, 0, img.width(), img.height())
            layer = self._add_layer(img, "AI 底图", "image")
        else:
            # 照搬 WebView addImageObject(app.js:2670)：fit 到画布 96%、只缩不放大（绝不拉伸）；
            # 多张按 (n%6)*44px 错开摆放，避免完全重叠、便于拖开逐张比较。
            cw, ch = self.canvas_size
            n = max(0, sum(1 for l in self.layers if l.get("kind") == "image") - 1)  # 已有 sprite 数（首张=底图不计）
            fit = min(cw * 0.96 / max(1, img.width()), ch * 0.96 / max(1, img.height()), 1.0)
            layer = self._add_layer(img, f"AI {len(self.layers) + 1}", "image")
            sw, sh = img.width() * fit, img.height() * fit
            off = (n % 6) * 44
            x = max(0.0, min((cw - sw) / 2 + off, max(0.0, cw - sw)))
            y = max(0.0, min((ch - sh) / 2 + off, max(0.0, ch - sh)))
            self._suspend_history = True
            layer["item"].setScale(fit)
            layer["item"].setPos(x, y)
            self._suspend_history = False
        self.set_tool("move"); self.fit_view(); self._update_info()
        return layer

    def _ai_group(self, layers):
        """多张 AI 结果自动打组（对齐 ai.js:226 setGroup）。"""
        layers = [l for l in layers if l in self.layers]
        if len(layers) < 2:
            return
        self._group_seq += 1
        gid = f"g{self._group_seq}"
        self._group_names[gid] = "AI 生成组"
        for l in layers:
            l["group"] = gid
        self._refresh_layers()

    # export_png / export_tiff 已抽到 editor_export.ExportMixin。

    # ---------- 保存 / 加载工程（H14：.nanopro.json，Qt 图层栈模型）----------
    # _qimage_to_b64 / _b64_to_qimage 已抽到 editor_export.ExportMixin。

    @staticmethod
    def _mask_to_b64(mask):
        """非破坏蒙版(uint8 HxW) → 灰度 PNG base64（无损，往返精确）。None→None。"""
        if mask is None:
            return None
        import cv2, base64
        ok, png = cv2.imencode(".png", mask)
        return base64.b64encode(png.tobytes()).decode("ascii") if ok else None

    @staticmethod
    def _b64_to_mask(s):
        """灰度 PNG base64 → 蒙版 uint8 HxW。空/失败→None。"""
        if not s:
            return None
        import cv2, base64
        import numpy as np
        try:
            return cv2.imdecode(np.frombuffer(base64.b64decode(str(s)), np.uint8), cv2.IMREAD_GRAYSCALE)
        except Exception:
            return None

    # save_project / load_project 已抽到 editor_export.ExportMixin。

    # ---------- 压力测试 ----------
    def stress_test(self):
        if not self.layers:
            QtWidgets.QMessageBox.information(self, "压力测试", "请先导入一张图片作底图")
            return
        n, ok = QtWidgets.QInputDialog.getInt(self, "压力测试", "叠加图层数 N：", 20, 1, 300)
        if not ok:
            return
        base = QtGui.QPixmap.fromImage(self.layers[0]["image"])
        small = base.scaled(max(1, base.width() // 3), max(1, base.height() // 3),
                            QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
        rect = self.scene.itemsBoundingRect()
        items = []
        for _ in range(n):
            it = QtWidgets.QGraphicsPixmapItem(small)
            it.setOpacity(0.8)
            it.setPos(rect.x() + random.random() * rect.width(), rect.y() + random.random() * rect.height())
            self.scene.addItem(it)
            items.append(it)
        self.info_label.setText(f"压力测试：场景 {len(self.scene.items())} 项")
        self._anim_phase = 0.0
        self._anim_items = items
        self._anim_base = [it.pos() for it in items]
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(0)

    def _tick(self):
        self._anim_phase += 0.05
        for i, it in enumerate(self._anim_items):
            b = self._anim_base[i]
            it.setPos(b.x() + 30 * math.sin(self._anim_phase + i), b.y() + 30 * math.cos(self._anim_phase + i * 0.7))
