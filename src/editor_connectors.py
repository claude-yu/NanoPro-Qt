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
        """命中该 scene 点的【最上层可见图层】（用外框 bbox 命中，连接线锚到对象框）。
        跳过【铺满画布的背景层】（面积≥95%画布）——白底/背景不作为连接目标，否则悬停空白处也冒锚点。"""
        cw, ch = self.canvas_size or (0, 0)
        bg_area = 0.95 * cw * ch if (cw and ch) else None
        best = None
        for l in self.layers:
            it = l.get("item")
            if it is None or it.scene() is None or not l.get("visible", True):
                continue
            r = it.sceneBoundingRect()
            if not r.contains(pt):
                continue
            if bg_area is not None and (r.width() * r.height()) >= bg_area:
                continue  # 铺满画布的背景层 → 不当连接目标
            if best is None or it.zValue() >= best["item"].zValue():
                best = l
        return best

    def _object_at_scene(self, pt: QtCore.QPointF):
        """命中该 scene 点的【最上层可见对象】→ 返回 (layer, eidx)。
        矢量层多元素时，eidx=命中的【具体元素】下标(导入SVG→连到单个元素而非整层)；栅格/单元素 eidx=None。
        跳过铺满画布(≥95%)的背景层。无命中 → (None, None)。"""
        cw, ch = self.canvas_size or (0, 0)
        bg_area = 0.95 * cw * ch if (cw and ch) else None
        best_l, best_e, best_z = None, None, None
        for l in self.layers:
            if not l.get("visible", True):
                continue
            if l.get("kind") == "vector":
                for i, pair in enumerate(l.get("pairs", [])):
                    it = pair[0]
                    if it is None or it.scene() is None:
                        continue
                    r = it.sceneBoundingRect()
                    if not r.contains(pt) or (bg_area and r.width() * r.height() >= bg_area):
                        continue
                    z = it.zValue()
                    if best_z is None or z >= best_z:
                        best_l, best_e, best_z = l, i, z
            else:
                it = l.get("item")
                if it is None or it.scene() is None:
                    continue
                r = it.sceneBoundingRect()
                if not r.contains(pt) or (bg_area and r.width() * r.height() >= bg_area):
                    continue
                z = it.zValue()
                if best_z is None or z >= best_z:
                    best_l, best_e, best_z = l, None, z
        return best_l, best_e

    def _connector_rect(self, uid, eidx=None):
        """连接线端点对象的当前外框（scene 坐标）；对象已删/脱离场景 → None（连接线据此自删）。
        eidx 非 None 且为矢量层 → 用该层【第 eidx 个元素】的框（元素级连接，导入SVG连到单个元素）。
        否则用整层；栅格层用【不透明内容】紧致框（去素材四周透明留白）。"""
        lyr = next((l for l in self.layers if l.get("uid") == uid), None)
        if lyr is None:
            return None
        if eidx is not None and lyr.get("kind") == "vector":
            pairs = lyr.get("pairs", [])
            if 0 <= eidx < len(pairs):
                eit = pairs[eidx][0]
                if eit is not None and eit.scene() is not None:
                    return eit.sceneBoundingRect()
            # eidx 失效(元素被删/重排) → 回退整层框
        it = lyr.get("item")
        if it is None or it.scene() is None:
            return None
        cb = self._content_bbox_item(lyr)  # item 局部坐标的内容框；矢量/空透明 → None
        if cb is not None and cb.isValid():
            return it.mapToScene(cb).boundingRect()  # 含 pos/scale 映射到 scene
        return it.sceneBoundingRect()

    def _content_bbox_item(self, lyr):
        """该层【不透明内容】在 item 局部坐标(0..w,0..h)的紧致 QRectF；按图 cacheKey 缓存，仅图变时重算。
        矢量层(path 本身就紧)或无 image → None（回退用整框）。"""
        if lyr.get("kind") == "vector":
            return None
        img = lyr.get("image")
        if img is None:
            return None
        key = img.cacheKey()
        cache = lyr.get("_cbbox")
        if cache is not None and cache[0] == key:
            return cache[1]
        import image_ops
        rect = None
        try:
            bb = image_ops.content_bbox(image_ops.qimage_to_rgba(img))
            if bb is not None:
                x0, y0, x1, y1 = bb
                rect = QtCore.QRectF(x0, y0, x1 - x0, y1 - y0)
        except Exception:
            rect = None
        lyr["_cbbox"] = (key, rect)
        return rect

    def _snap_to_anchor(self, scene_pos: QtCore.QPointF, tol_px: float = 12.0):
        """箭头/直线工具：端点靠近某对象的边中点锚点(屏幕<tol_px)时吸过去；否则原样返回。
        让基础箭头/直线也能精确连到对象边的中心（对齐 BioRender：靠近 node 就吸附）。"""
        lyr, eidx = self._object_at_scene(scene_pos)
        if lyr is None:
            return scene_pos
        r = self._connector_rect(lyr.get("uid"), eidx)
        if r is None:
            return scene_pos
        import connector_item
        tol = tol_px / max(1e-6, self.view.current_zoom())
        best, bestd = None, tol
        for a in connector_item.anchor_points(r):
            d = (a - scene_pos).manhattanLength()
            if d <= bestd:
                bestd, best = d, a
        return best if best is not None else scene_pos

    def _anchor_object_at(self, scene_pos: QtCore.QPointF, tol_px: float = 12.0):
        """scene_pos 在某对象边中点锚点容差(屏幕<tol_px)内 → 返回 (layer, eidx)；否则 None。
        用于判断箭头/直线端点是否落在了某对象的锚点上（落上→建跟随式连接线）。"""
        lyr, eidx = self._object_at_scene(scene_pos)
        if lyr is None:
            return None
        r = self._connector_rect(lyr.get("uid"), eidx)
        if r is None:
            return None
        import connector_item
        tol = tol_px / max(1e-6, self.view.current_zoom())
        for a in connector_item.anchor_points(r):
            if (a - scene_pos).manhattanLength() <= tol:
                return (lyr, eidx)
        return None

    def _create_connector(self, src, src_e, dst, dst_e, arrow: bool = True, label: str = "连接线"):
        """从两个对象 (layer,eidx) 建一条【跟随式】连接线；成功返回 ConnectorItem，失败返回 None。
        连接线工具 + 锚定的箭头/直线共用这条管线（箭头 arrow=True、直线 arrow=False）。"""
        import connector_item
        self._push_history(label)
        c = connector_item.ConnectorItem(self, src.get("uid"), dst.get("uid"),
                                         src_eidx=src_e, dst_eidx=dst_e, arrow=arrow)
        self.scene.addItem(c)
        self.connectors.append(c)
        if not c.update_path():  # 极端情况端点框拿不到 → 撤掉，fail-loud
            self.scene.removeItem(c); self.connectors.remove(c)
            return None
        return c

    def _refresh_connectors(self):
        """重算所有连接线端点（对象移动/缩放/对齐后跟随）；端点对象没了的连接线自动移除（大声：不留悬空线）。"""
        if not getattr(self, "connectors", None):
            return
        for c in list(self.connectors):
            if not c.update_path():
                if c.scene() is not None:
                    self.scene.removeItem(c)
                self.connectors.remove(c)

    def _connector_under(self, pt: QtCore.QPointF) -> bool:
        """该 scene 点(带容差)下是否有已存在的连接线 → 用于悬停在连线上时不显示后面对象的锚点。"""
        if not getattr(self, "connectors", None):
            return False
        tol = 7.0 / max(1e-6, self.view.current_zoom())
        rect = QtCore.QRectF(pt.x() - tol, pt.y() - tol, 2 * tol, 2 * tol)
        return any(it in self.connectors for it in self.scene.items(rect))

    def _on_connector_hover(self, scene_pos):
        """连接线工具悬停：命中对象 → 算它 4 个边中点锚点存 self._conn_hover_anchors（drawForeground 画蓝点）。
        多元素矢量层(导入SVG)→ 只显光标下那个【元素】的锚点；悬停在已有连线上 → 不显示后面对象锚点。
        性能：光标几乎没动(<6px)直接返回，避免每帧都遍历(大画布/多层卡)。"""
        last = getattr(self, "_conn_hover_last", None)
        if last is not None and (scene_pos - last).manhattanLength() < 6.0:
            return
        self._conn_hover_last = QtCore.QPointF(scene_pos)
        import connector_item
        anchors = []
        key = None
        if not self._connector_under(scene_pos):  # ② 悬停在已有连线上 → 不显后面对象锚点
            lyr, eidx = self._object_at_scene(scene_pos)
            if lyr is not None:
                r = self._connector_rect(lyr.get("uid"), eidx)
                if r is not None:
                    anchors = connector_item.anchor_points(r)
                    key = (lyr.get("uid"), eidx)
        if key != getattr(self, "_conn_hover_key", None) or anchors != getattr(self, "_conn_hover_anchors", []):
            self._conn_hover_key = key
            self._conn_hover_anchors = anchors
            self.view.viewport().update()

    def _clear_connector_hover(self):
        self._conn_hover_key = None
        self._conn_hover_last = None
        if getattr(self, "_conn_hover_anchors", None):
            self._conn_hover_anchors = []
            self.view.viewport().update()

    def _connector_start(self, sp: QtCore.QPointF):
        self._conn_src, self._conn_src_eidx = self._object_at_scene(sp)
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
        self._clear_connector_hover()  # 一次手势结束：统一清悬停锚点（成功/失败各路径都覆盖），下次移动再按光标重算
        if p0 is None:
            return
        src = getattr(self, "_conn_src", None)
        src_e = getattr(self, "_conn_src_eidx", None)
        self._conn_src = None; self._conn_src_eidx = None
        dst, dst_e = self._object_at_scene(getattr(self, "_conn_p1", p0))
        if src is None or dst is None:
            self.op_label.setText("连接线未建立：起点和终点都要落在一个对象上"); return
        if src is dst and src_e == dst_e:  # 同一对象/同一元素 → 不连（但同层【不同元素】允许，支持SVG内部连接）
            self.op_label.setText("连接线未建立：起点和终点是同一个对象"); return
        if self._create_connector(src, src_e, dst, dst_e, arrow=True) is None:
            self.op_label.setText("连接线建立失败（拿不到对象外框）"); return
        self.op_label.setText("✓ 已连接两个对象（连在边中心）· 移动/缩放自动跟随 · 右键连线改形状/颜色/删除")

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
