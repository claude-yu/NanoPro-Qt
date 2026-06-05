"""ConnectorsMixin —— EditorWindow 的「智能连接线」功能（从 editor_window.py 抽出，行为不变）。

机制/通路图：从一个对象拖到另一个对象建带箭头连线，移动/缩放任一对象时连线自动跟随；
右键连线可改形状(直线/曲线/折线)/颜色/虚线/删除。连接线图元见 connector_item.ConnectorItem。
本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：layers/scene/view/connectors/op_label/
_start_preview/_remove_preview/_sel_preview/_push_history）。
连接工具拖拽过程中的临时状态 self._conn_src / _conn_p0 / _conn_p1 由本 mixin 自己在
_connector_start 里建、_connector_end 里清，不需 EditorWindow 预先声明。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class ConnectorsMixin:
    # ----- 智能连接线：从对象拖到对象建带箭头连线，移动/缩放自动跟随（BioRender 式）-----
    def _layer_at_scene(self, pt: QtCore.QPointF):
        """命中该 scene 点的【最上层可见图层】（用外框 bbox 命中，连接线锚到对象框）。"""
        best = None
        for l in self.layers:
            it = l.get("item")
            if it is None or it.scene() is None or not l.get("visible", True):
                continue
            if it.sceneBoundingRect().contains(pt):
                if best is None or it.zValue() >= best["item"].zValue():
                    best = l
        return best

    def _connector_rect(self, uid):
        """连接线端点对象的当前外框（scene 坐标）；对象已删/脱离场景 → None（连接线据此自删）。"""
        lyr = next((l for l in self.layers if l.get("uid") == uid), None)
        if lyr is None:
            return None
        it = lyr.get("item")
        if it is None or it.scene() is None:
            return None
        return it.sceneBoundingRect()

    def _refresh_connectors(self):
        """重算所有连接线端点（对象移动/缩放/对齐后跟随）；端点对象没了的连接线自动移除（大声：不留悬空线）。"""
        if not getattr(self, "connectors", None):
            return
        for c in list(self.connectors):
            if not c.update_path():
                if c.scene() is not None:
                    self.scene.removeItem(c)
                self.connectors.remove(c)

    def _connector_start(self, sp: QtCore.QPointF):
        self._conn_src = self._layer_at_scene(sp)
        self._conn_p0 = sp
        self._conn_p1 = sp
        self._start_preview()
        if self._conn_src is None:
            self.op_label.setText("连接线：请【从一个对象上】按下，拖到另一个对象松手")
        else:
            self.op_label.setText("连接线：拖到目标对象松手 → 建立带箭头连线（自动跟随移动）")

    def _connector_move(self, sp: QtCore.QPointF):
        if self._sel_preview is None or getattr(self, "_conn_p0", None) is None:
            return
        self._conn_p1 = sp
        qp = QtGui.QPainterPath(self._conn_p0)
        qp.lineTo(sp)
        self._sel_preview.setPath(qp)

    def _connector_end(self):
        p0 = getattr(self, "_conn_p0", None)
        self._conn_p0 = None
        self._remove_preview()
        if p0 is None:
            return
        src = getattr(self, "_conn_src", None)
        self._conn_src = None
        dst = self._layer_at_scene(getattr(self, "_conn_p1", p0))
        if src is None or dst is None:
            self.op_label.setText("连接线未建立：起点和终点都要落在一个对象上"); return
        if src is dst:
            self.op_label.setText("连接线未建立：起点和终点是同一个对象"); return
        import connector_item
        self._push_history("连接线")
        c = connector_item.ConnectorItem(self, src.get("uid"), dst.get("uid"))
        self.scene.addItem(c)
        self.connectors.append(c)
        if not c.update_path():  # 极端情况端点框拿不到 → 撤掉，fail-loud
            self.scene.removeItem(c); self.connectors.remove(c)
            self.op_label.setText("连接线建立失败（拿不到对象外框）"); return
        self.op_label.setText("✓ 已连接两个对象 · 移动/缩放自动跟随 · 右键连线改形状(直线/曲线/折线)/颜色/删除")

    def _connector_menu_at(self, scene_pos: QtCore.QPointF, global_pos) -> bool:
        """右键命中某连接线（带容差）→ 弹形状/颜色/虚线/删除菜单；命中返回 True。"""
        if not getattr(self, "connectors", None):
            return False
        tol = 7.0 / max(1e-6, self.view.current_zoom())  # 屏幕约 7px 容差，细线也好点中
        rect = QtCore.QRectF(scene_pos.x() - tol, scene_pos.y() - tol, 2 * tol, 2 * tol)
        hit = next((it for it in self.scene.items(rect) if it in self.connectors), None)
        if hit is None:
            return False
        m = QtWidgets.QMenu(self)
        sm = m.addMenu("连线形状")
        for label, s in (("直线", "straight"), ("曲线（推荐）", "curved"), ("折线", "elbow")):
            a = sm.addAction(("● " if hit.line_shape == s else "○ ") + label)
            a.triggered.connect(lambda _=False, ss=s, cc=hit: (cc.set_shape(ss),
                                self.op_label.setText("连线形状已改")))
        m.addAction("改连线颜色…", lambda cc=hit: self._connector_pick_color(cc))
        da = m.addAction("虚线"); da.setCheckable(True); da.setChecked(hit.dashed)
        da.toggled.connect(lambda v, cc=hit: cc.set_dashed(v))
        m.addSeparator()
        m.addAction("✕ 删除连接线", lambda cc=hit: self._remove_connector(cc))
        m.exec(global_pos)
        return True

    def _connector_pick_color(self, c):
        col = QtWidgets.QColorDialog.getColor(c.color, self, "连线颜色")
        if col.isValid():
            c.set_color(col)

    def _remove_connector(self, c):
        if c in self.connectors:
            if c.scene() is not None:
                self.scene.removeItem(c)
            self.connectors.remove(c)
            self.op_label.setText("已删除连接线（剩 %d 条）" % len(self.connectors))
