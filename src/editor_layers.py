"""LayersMixin —— EditorWindow 的「图层」功能（从 editor_window.py 抽出，行为不变）。

涵盖：图层增删/缩略图/面板刷新（栅格+矢量）、画布选层与多选回填、对齐分布、层级移动（上移下移/置顶置底）、
重命名/标记、打组解组/折叠、激活/显隐/锁定、不透明度滑块预览与提交、图层蒙版（从选区生成/删除）、
翻转旋转（矢量绕中心 + 栅格像素镜像/旋转，含 mask 同步）。
本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：layer_list/opacity_slider/opacity_lbl/
_thumb_cache/scene/view/layers/active/selected_layers/_marked/_collapsed/_group_names/_group_seq/
_layer_uid/canvas_size/selection_mask/op_label/_node_overlay/_outline/_resize_handle/
_suspend_sel_sync/_suspend_history/_suspend_snap/_suspend_opacity_ui/_suspend_vec_sel/_vec_controls/
_push_history/_snap_layer_pos/_clear_guides_overlay/_clear_guides_overlay/_refresh_connectors/
_layer_scene_rect/_layer_items/_vector_layers/_vector_layer_of_item/_update_outline/_clear_selection/
_set_vec_controls_enabled/_sync_opacity_slider/_apply_layer_opacity/_clear_node_overlay/
_delete_selected_anchors/_sync_vec_layers/_transform_targets 等，MRO 解析）。
LayerRow / GroupHeaderRow 仍在 editor_window.py（别处也用），用到处函数内惰性 import（避免循环依赖）。
"""
from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import image_ops
from layer_item import ImageLayerItem


class LayersMixin:
    def _add_layer(self, image: QtGui.QImage, name: str, kind: str) -> dict:
        item = ImageLayerItem(image)
        item._move_cb = self._push_history
        item._snap_cb = self._snap_layer_pos
        item._release_cb = self._clear_guides_overlay
        item._post_move_cb = self._refresh_connectors  # 移动/对齐/缩放后 → 绑定的连接线自动跟随
        item.setZValue(len(self.layers))
        self.scene.addItem(item)
        self._layer_uid += 1
        layer = {"name": name, "image": image, "item": item, "kind": kind, "visible": True,
                 "uid": self._layer_uid, "group": None}
        item._press_cb = lambda L=layer: self._on_layer_pressed(L)
        self.layers.append(layer)
        self._set_active(layer)  # 内部已刷新图层面板
        return layer

    def _layer_thumb(self, layer: dict) -> QtGui.QPixmap:
        """图层缩略图：栅格层按 image.cacheKey() 缓存（像素没变就直接复用，免全分辨率 SmoothTransformation 重缩放）。
        矢量层无 cacheKey 且数量少 → 每次渲（不入缓存）。"""
        from editor_window import LayerRow
        if layer.get("kind") == "vector":
            return self._vector_thumb(layer)
        uid = layer.get("uid")
        img = layer["image"]
        mask = layer.get("mask")
        # 缓存键含蒙版身份：image.cacheKey() 在仅蒙版变化时不变，故并入 id(mask)（新蒙版数组=新 id→自动失效）
        key = (img.cacheKey(), id(mask) if mask is not None else 0)
        cached = self._thumb_cache.get(uid)
        if cached is not None and cached[0] == key:
            return cached[1]
        pm = QtGui.QPixmap.fromImage(image_ops.masked_qimage(img, mask)).scaled(
            LayerRow.THUMB, LayerRow.THUMB, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        self._thumb_cache[uid] = (key, pm)
        return pm

    def _refresh_layers(self):
        from editor_window import GroupHeaderRow, LayerRow
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        live = {l.get("uid") for l in self.layers}  # 顺手清掉已删除层的缩略图缓存，防字典无限增长
        if len(self._thumb_cache) > len(live):
            self._thumb_cache = {u: v for u, v in self._thumb_cache.items() if u in live}
        shown_groups = set()
        for layer in reversed(self.layers):  # 顶层在上（PS 习惯）
            gid = layer.get("group")
            if gid and gid not in shown_groups:  # 该组第一个成员之前插组头（对齐 layers.js:246）
                shown_groups.add(gid)
                members = [l for l in self.layers if l.get("group") == gid]
                any_vis = any(l.get("visible", True) for l in members)
                any_locked = any(l.get("locked", False) for l in members)
                hdr = GroupHeaderRow(self, gid, self._group_names.get(gid, gid),
                                     len(members), gid in self._collapsed, any_vis, any_locked,
                                     getattr(self, "_selected_group", None) == gid)
                hit = QtWidgets.QListWidgetItem(); hit.setSizeHint(hdr.sizeHint())
                hit.setData(QtCore.Qt.ItemDataRole.UserRole, f"group:{gid}")
                hit.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsDropEnabled)
                self.layer_list.addItem(hit); self.layer_list.setItemWidget(hit, hdr)
            if gid and gid in self._collapsed:
                continue  # 组折叠 → 不渲染成员行
            thumb = self._layer_thumb(layer)  # 走缓存：像素未变直接复用，避免每次刷新全分辨率重缩放
            row = LayerRow(self, layer, thumb, indent=bool(gid), marked=(layer.get("uid") in self._marked))
            row.set_active(layer is self.active)
            it = QtWidgets.QListWidgetItem()
            it.setSizeHint(row.sizeHint())
            it.setData(QtCore.Qt.ItemDataRole.UserRole, layer.get("uid"))  # 行→层 用 uid 寻址（有组头时索引不再线性）
            self.layer_list.addItem(it)
            self.layer_list.setItemWidget(it, row)
        self.layer_list.blockSignals(False)

    def _vector_thumb(self, layer: dict) -> QtGui.QPixmap:
        """矢量层缩略图：把该层所有顶层 item 渲到 THUMB×THUMB（按其场景包围盒缩放）。空则返回占位。"""
        from editor_window import LayerRow
        sz = LayerRow.THUMB
        items = layer.get("items", [])
        rect = QtCore.QRectF()
        for it in items:
            if it.isVisible():
                rect = rect.united(it.sceneBoundingRect())
        pm = QtGui.QPixmap(sz, sz)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        if not rect.isValid() or rect.width() < 1 or rect.height() < 1:
            return pm
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        try:
            self.scene.render(p, QtCore.QRectF(0, 0, sz, sz), rect,
                              QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            p.end()
        return pm

    def _on_layer_row(self, row: int):
        it = self.layer_list.item(row)
        if it is None:
            return
        uid = it.data(QtCore.Qt.ItemDataRole.UserRole)
        lyr = next((l for l in self.layers if l.get("uid") == uid), None)
        if lyr is None:
            return
        if lyr.get("visible", True):
            self._set_active(lyr)
        else:  # 拒绝激活隐藏层（对齐 select() layers.js:196）
            self._refresh_layers()

    def _on_layer_pressed(self, layer: dict):  # 画布上按下某层 → 设为激活层（拖动时跟随）
        if layer not in self.layers:
            return
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & (QtCore.Qt.KeyboardModifier.ShiftModifier | QtCore.Qt.KeyboardModifier.ControlModifier):
            # 画布上 Shift/Ctrl 点 = 多选（加入/移出选择集），用于对齐/分布——不必再去图层面板
            if layer in self.selected_layers:
                if len(self.selected_layers) > 1:
                    self.selected_layers.remove(layer)
            else:
                self.selected_layers.append(layer)
            self.active = layer
            self._reselect_rows_by_uid([l.get("uid") for l in self.selected_layers])
            self._update_outline()
        else:
            self.selected_layers = [layer]  # 无修饰键=单选（PS）
            if layer is not self.active:
                self._set_active(layer)

    def _on_layer_selection_changed(self):
        # 图层面板 Ctrl/Shift 多选变化 → 填 selected_layers（过滤组头/隐藏层），currentItem 设为 active。
        if self._suspend_sel_sync:
            return
        self._selected_group = None
        by_uid = {l.get("uid"): l for l in self.layers}
        sel = []
        for it in self.layer_list.selectedItems():
            lyr = by_uid.get(it.data(QtCore.Qt.ItemDataRole.UserRole))
            if lyr is not None and lyr.get("visible", True):
                sel.append(lyr)
        self.selected_layers = sel
        cur = self.layer_list.currentItem()
        if cur is not None:
            lyr = by_uid.get(cur.data(QtCore.Qt.ItemDataRole.UserRole))
            if lyr is not None and lyr.get("visible", True) and lyr is not self.active:
                self.active = lyr  # 只换 active 引用，不走 _set_active（避免 _refresh_layers 清掉多选高亮）
                self._update_outline()
                self._refresh_layers()
                self._reselect_rows_by_uid([l.get("uid") for l in sel])
                self._sync_opacity_slider()  # 不透明度滑块跟随新激活层

    def _reselect_rows_by_uid(self, uids):
        # _refresh_layers clear() 会丢失 QListWidget 多选高亮 → 用 uid 复原选中态（不触发选择回调）。
        want = set(u for u in uids if u is not None)
        if not want:
            return
        self._suspend_sel_sync = True
        self.layer_list.blockSignals(True)
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            it.setSelected(it.data(QtCore.Qt.ItemDataRole.UserRole) in want)
        self.layer_list.blockSignals(False)
        self._suspend_sel_sync = False

    def _align(self, kind: str):
        # 对齐(6)/分布(2)：基准=多选并集包围盒(≥2 层)或画布(单层)；分布需 ≥3 层、按中心等间距。锁定层跳过(fail-loud)。
        targets = [l for l in self.selected_layers if l in self.layers and l.get("visible", True)]
        if not targets:
            if self.active is not None:
                targets = [self.active]
            else:
                self.op_label.setText("请先选 ≥1 个图层再对齐"); return
        rects = {id(l): self._layer_scene_rect(l) for l in targets}
        if kind in ("dist_h", "dist_v"):
            if len(targets) < 3:
                self.op_label.setText("分布需选 ≥3 个图层"); return
        elif len(targets) >= 2:
            xs = [rects[id(l)][0] for l in targets]
            ys = [rects[id(l)][1] for l in targets]
            rs = [rects[id(l)][0] + rects[id(l)][2] for l in targets]
            bs = [rects[id(l)][1] + rects[id(l)][3] for l in targets]
            uL, uT, uR, uB = min(xs), min(ys), max(rs), max(bs)
        else:  # 单层 → 对画布对齐
            if not self.canvas_size:
                self.op_label.setText("无画布可作对齐基准"); return
            uL, uT, uR, uB = 0.0, 0.0, float(self.canvas_size[0]), float(self.canvas_size[1])

        def new_pos(l):
            x, y, w, h = rects[id(l)]
            if kind == "align_left":     x = uL
            elif kind == "align_hcenter": x = (uL + uR) / 2 - w / 2
            elif kind == "align_right":  x = uR - w
            elif kind == "align_top":    y = uT
            elif kind == "align_vcenter": y = (uT + uB) / 2 - h / 2
            elif kind == "align_bottom": y = uB - h
            return QtCore.QPointF(x, y)

        moves = {}
        if kind in ("dist_h", "dist_v"):
            ax = (kind == "dist_h")
            # 按中心排序，固定首尾，中间层中心等间距（Illustrator 按中心分布）
            def center(l):
                x, y, w, h = rects[id(l)]
                return (x + w / 2) if ax else (y + h / 2)
            ordered = sorted(targets, key=center)
            first_c, last_c = center(ordered[0]), center(ordered[-1])
            n = len(ordered)
            gap = (last_c - first_c) / (n - 1)
            for i, l in enumerate(ordered):
                x, y, w, h = rects[id(l)]
                c = first_c + gap * i
                moves[id(l)] = QtCore.QPointF(c - w / 2, y) if ax else QtCore.QPointF(x, c - h / 2)
        else:
            for l in targets:
                moves[id(l)] = new_pos(l)

        self._push_history("对齐分布")
        self._suspend_history = True
        self._suspend_snap = True   # 程序化对齐：禁止智能吸附劫持，保证落点像素精确
        skipped = 0
        for l in targets:
            if l.get("locked", False):  # 锁定层不移动（PS 行为），计数上报
                skipped += 1; continue
            r = rects[id(l)]
            delta = moves[id(l)] - QtCore.QPointF(r[0], r[1])  # 目标左上 - 当前并集左上
            for it in self._layer_items(l):  # 矢量层=整组所有顶层 item 同步平移（不再只动哨兵→不撕裂）
                it.setPos(it.pos() + delta)
        self._suspend_snap = False
        self._suspend_history = False
        self._clear_guides_overlay()  # 清掉可能残留的洋红虚线
        self._update_outline()
        self._refresh_layers()
        self._reselect_rows_by_uid([l.get("uid") for l in targets])
        label = {"align_left": "左对齐", "align_hcenter": "水平居中", "align_right": "右对齐",
                 "align_top": "顶对齐", "align_vcenter": "垂直居中", "align_bottom": "底对齐",
                 "dist_h": "水平分布", "dist_v": "垂直分布"}[kind]
        msg = f"{label}：{len(targets) - skipped} 层"
        if skipped:
            msg += f"（跳过锁定 {skipped}）"
        self.op_label.setText(msg)

    def _move_layer(self, layer: dict, delta: int):  # delta>0 上移(更高 z)
        if layer not in self.layers:
            return
        i = self.layers.index(layer)
        j = i + delta
        if not (0 <= j < len(self.layers)):
            return
        self._push_history("调整层级")
        self.layers[i], self.layers[j] = self.layers[j], self.layers[i]
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):  # 矢量层整组同 z
                it.setZValue(k)
        self._refresh_layers()
        self.op_label.setText("调整层级")

    def _layer_z(self, where: str, layer: dict = None):
        """图层层级跳转：front=置顶 / back=置底 / forward=上移一层 / backward=下移一层。
        self.layers 底→顶（index 0=最底/最低 z），z=index。默认作用活动层。"""
        layer = layer or self.active
        if layer is None or layer not in self.layers:
            self.op_label.setText("没有可调层级的图层（先选中一层）"); return
        i = self.layers.index(layer); n = len(self.layers)
        if (where in ("front", "forward") and i == n - 1) or (where in ("back", "backward") and i == 0):
            return  # 已在端点
        self._push_history("调整层级")
        self.layers.remove(layer)
        m = len(self.layers)
        j = {"front": m, "back": 0, "forward": min(m, i + 1), "backward": max(0, i - 1)}[where]
        self.layers.insert(j, layer)
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):  # 矢量层整组同 z
                it.setZValue(k)
        self._refresh_layers()
        self.op_label.setText({"front": "已置顶", "back": "已置底",
                               "forward": "上移一层", "backward": "下移一层"}[where])

    def _delete_specific_layer(self, layer: dict):
        if layer not in self.layers:
            return
        self._set_active(layer)
        self.delete_layer()

    def _delete_active(self):  # 底部图标栏「删除」：删当前激活层（delete_layer 已含锁定/空判提示）
        if not self.active:
            QtWidgets.QMessageBox.information(self, "删除图层", "请先在图层面板选中一个图层")
            return
        self.delete_layer()

    def _rename_layer(self, layer: dict):
        name, ok = QtWidgets.QInputDialog.getText(self, "重命名图层", "名称：", text=layer.get("name", ""))
        if ok and name:
            self._push_history("重命名图层")
            layer["name"] = name
            self._refresh_layers()

    def _toggle_mark(self, layer: dict):
        uid = layer.get("uid")
        self._marked.discard(uid) if uid in self._marked else self._marked.add(uid)
        self._refresh_layers()

    def do_group(self):
        # 种子：优先用右键菜单「勾选打组」的层；没勾选则用图层面板 Ctrl/Shift 多选的可见层（PS 式多选打组）。
        members = self._marked_layers()
        if len(members) < 2:
            members = [l for l in getattr(self, "selected_layers", []) if l in self.layers and l.get("visible", True)]
        if len(members) < 2:
            QtWidgets.QMessageBox.information(
                self, "打组", "请先选中至少 2 个图层（图层面板里按住 Ctrl/Shift 多选，或右键「勾选以打组」），再点「打组」")
            return
        self._push_history("打组")  # 打组可撤销 + 防 Ctrl+Z 复活已解散的组（group 在 snapshot 里，对齐 _set_group_visible 约定）
        self._group_seq += 1
        gid = f"g{self._group_seq}"
        self._group_names[gid] = f"组 {self._group_seq}"
        member_ids = {id(l) for l in members}
        top_index = max(self.layers.index(l) for l in members)
        block = [l for l in self.layers if id(l) in member_ids]
        rest = [l for l in self.layers if id(l) not in member_ids]
        insert_at = sum(1 for i, l in enumerate(self.layers) if i <= top_index and id(l) not in member_ids)
        self.layers = rest[:insert_at] + block + rest[insert_at:]
        for l in members:
            l["group"] = gid
        self._selected_group = gid
        self.selected_layers = list(block)
        self._marked.clear()
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):
                it.setZValue(k)
        self._refresh_layers()
        self._reselect_rows_by_uid([l.get("uid") for l in block])
        self.op_label.setText(f"已打组：{len(members)} 个图层 → {self._group_names[gid]}")

    def do_ungroup(self):
        seed = self._marked_layers() or ([self.active] if self.active else [])
        gids = {l.get("group") for l in seed if l.get("group")}
        if getattr(self, "_selected_group", None):
            gids.add(self._selected_group)
        if not gids:
            QtWidgets.QMessageBox.information(self, "解组", "请勾选或选中一个组内的图层，再点「解组」")
            return
        self._push_history("解组")  # 解组可撤销（同上）
        n = 0
        for l in self.layers:
            if l.get("group") in gids:
                l["group"] = None; n += 1
        self._marked.clear()
        if getattr(self, "_selected_group", None) in gids:
            self._selected_group = None
        self._refresh_layers()
        self.op_label.setText(f"已解组：{n} 个图层")

    def _group_members(self, gid: str):
        return [l for l in self.layers if l.get("group") == gid]

    def _select_group(self, gid: str):
        members = [l for l in self._group_members(gid) if l.get("visible", True)]
        self._selected_group = gid
        self.selected_layers = members
        self.active = members[-1] if members else None
        self._clear_selection()
        self._update_outline()
        self._refresh_layers()
        self._reselect_rows_by_uid([l.get("uid") for l in members])
        self._sync_opacity_slider()
        self.op_label.setText(f"已选中组：{self._group_names.get(gid, gid)}")

    def _rename_group(self, gid: str):
        name, ok = QtWidgets.QInputDialog.getText(self, "重命名组", "名称：", text=self._group_names.get(gid, gid))
        if ok and name:
            self._push_history("重命名组")
            self._group_names[gid] = name
            self._refresh_layers()

    def _ungroup_gid(self, gid: str):
        if not self._group_members(gid):
            return
        self._push_history("解组")
        for l in self.layers:
            if l.get("group") == gid:
                l["group"] = None
        self._collapsed.discard(gid)
        self._group_names.pop(gid, None)
        if self._selected_group == gid:
            self._selected_group = None
        self._refresh_layers()
        self.op_label.setText("已解组（保留图层）")

    def _delete_group(self, gid: str):
        members = self._group_members(gid)
        if not members:
            return
        self._push_history("删除组")
        for l in members:
            for it in self._layer_items(l):
                self.scene.removeItem(it)
        self.layers = [l for l in self.layers if l.get("group") != gid]
        self._collapsed.discard(gid)
        self._group_names.pop(gid, None)
        self._marked = {u for u in self._marked if any(l.get("uid") == u for l in self.layers)}
        if self._selected_group == gid:
            self._selected_group = None
        if self.active in members:
            self.active = self.layers[-1] if self.layers else None
            self.selected_layers = [self.active] if self.active else []
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):
                it.setZValue(k)
        self._update_outline()
        self._refresh_layers()
        self.op_label.setText(f"已删除组及内容：{len(members)} 个图层")

    def _set_group_locked(self, gid: str, locked: bool):
        members = self._group_members(gid)
        if not members:
            return
        self._push_history("组锁定")
        for l in members:
            l["locked"] = locked
            for it in self._layer_items(l):
                it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
        self._update_outline()
        self._refresh_layers()
        self.op_label.setText(f"{self._group_names.get(gid, gid)} {'已锁定' if locked else '已解锁'}")

    def _move_layer_to_group(self, src_uid, gid: str):
        src = next((l for l in self.layers if l.get("uid") == src_uid), None)
        if src is None or src.get("group") == gid:
            return
        members = [l for l in self.layers if l.get("group") == gid and l is not src]
        if not members:
            return
        self._push_history("移入组")
        self.layers.remove(src)
        members = [l for l in self.layers if l.get("group") == gid]
        insert_at = self.layers.index(members[-1]) + 1
        src["group"] = gid
        self.layers.insert(insert_at, src)
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):
                it.setZValue(k)
        self._selected_group = gid
        self._refresh_layers()
        self._reselect_rows_by_uid([l.get("uid") for l in self._group_members(gid)])
        self.op_label.setText(f"已移入组：{self._group_names.get(gid, gid)}")

    def _reorder_group(self, src_gid: str, dst_token, before: bool):
        block = [l for l in self.layers if l.get("group") == src_gid]
        if not block:
            return
        dst_gid = dst_token[6:] if isinstance(dst_token, str) and dst_token.startswith("group:") else None
        if dst_gid == src_gid:
            return
        self._push_history("移动组")
        block_ids = {id(l) for l in block}
        rest = [l for l in self.layers if id(l) not in block_ids]
        if dst_gid:
            dst_members = [l for l in rest if l.get("group") == dst_gid]
            if not dst_members:
                return
            idxs = [rest.index(l) for l in dst_members]
            insert_at = max(idxs) + 1 if before else min(idxs)
        else:
            dst = next((l for l in rest if l.get("uid") == dst_token), None)
            if dst is None:
                return
            di = rest.index(dst)
            insert_at = di + 1 if before else di
        self.layers = rest[:insert_at] + block + rest[insert_at:]
        for k, l in enumerate(self.layers):
            for it in self._layer_items(l):
                it.setZValue(k)
        self._selected_group = src_gid
        self._refresh_layers()
        self._reselect_rows_by_uid([l.get("uid") for l in block])
        self.op_label.setText(f"已移动组：{self._group_names.get(src_gid, src_gid)}")

    def _toggle_collapse(self, gid: str):
        self._collapsed.discard(gid) if gid in self._collapsed else self._collapsed.add(gid)
        self._refresh_layers()

    def _set_active(self, layer: dict | None):
        # 切层顺手清矢量元素选择（避免跨层悬留导致面板回填错层）。守卫防 selectionChanged 在重建中重入。
        if self.scene.selectedItems():
            self._suspend_vec_sel = True
            self.scene.clearSelection()
            self._suspend_vec_sel = False
            if hasattr(self, "_vec_controls"):
                self._set_vec_controls_enabled(False)
        self.active = layer
        self._selected_group = None
        if layer not in self.selected_layers:  # 单选路径（画布点击/新建层）→ 选择集收敛为单层
            self.selected_layers = [layer] if layer else []
        self._text_live_pushed = False  # 换层 → 重置文字样式即时编辑的历史标记
        self._clear_selection()  # 选区是基于激活层的，换层即清，避免坐标错位
        self._update_outline()
        self._refresh_layers()
        self._sync_opacity_slider()  # 不透明度滑块跟随新激活层

    def _set_layer_visible(self, layer: dict, vis: bool):
        self._push_history("图层显隐")  # M2: 显隐入撤销历史，可 Ctrl+Z 恢复（对齐 layers.js:187 toggleHide）
        layer["visible"] = vis
        for it in self._layer_items(layer):  # 矢量层整组显隐
            it.setVisible(vis)
        # M3: 隐藏当前激活层 → 取消激活，缩放手柄/轮廓不再挂在看不见的层上（对齐 layers.js:188-190）
        if not vis and layer is self.active:
            self._set_active(None)
        else:
            self._update_outline()
        self.op_label.setText(f"{layer['name']} {'显示' if vis else '隐藏'}")

    def _set_layer_locked(self, layer: dict, locked: bool):
        layer["locked"] = locked
        for it in self._layer_items(layer):  # 矢量层整组锁定/解锁可移动
            it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
        if layer is self.active:
            self._update_outline()  # 锁定 → 隐藏缩放手柄
        self.op_label.setText(f"{layer['name']} {'已锁定（不能移动/涂改）' if locked else '已解锁'}")

    def _active_locked(self) -> bool:
        if self.active and self.active.get("locked"):
            QtWidgets.QMessageBox.information(self, "提示", "该图层已锁定（不能移动/涂改）。点图层上的锁图标解锁。")
            return True
        return False

    def _on_opacity_preview(self, val: int):
        # 滑动中：实时预览，不入历史（仿亮度/对比度 apply_preview）。
        self.opacity_lbl.setText(f"{val}%")
        if self._suspend_opacity_ui:  # 同步滑块到 active 时屏蔽，避免回写
            return
        if self.active is not None:
            self._apply_layer_opacity(self.active, val / 100.0)
            # 键盘方向键/点滑槽(非拖手柄)改值 isSliderDown()==False → 立即提交+入历史，
            # 否则只预览不写 layer["opacity"]→撤销不了+切层丢+存档丢(审核 MEDIUM)。拖手柄时为 True，仍由 sliderReleased 提交。
            if not self.opacity_slider.isSliderDown():
                self._on_opacity_commit()

    def _on_opacity_commit(self):
        # 松手提交：仅当相对该层持久值有变化才 _push_history + 写回 layer["opacity"]。
        if self.active is None:
            return
        new01 = self.opacity_slider.value() / 100.0
        old01 = self.active.get("opacity", 1.0)
        if abs(new01 - old01) < 1e-6:
            return
        # 先把层还原成提交前的值再 _push_history → 快照=【调整前】，Ctrl+Z 回到旧不透明度（仿亮度/对比度）
        self._apply_layer_opacity(self.active, old01)
        self._push_history("图层不透明度")
        self.active["opacity"] = new01
        self._apply_layer_opacity(self.active, new01)
        self.op_label.setText(f"{self.active['name']} 不透明度 {self.opacity_slider.value()}%")

    def delete_layer(self):
        # B5：锚点工具下且有选中锚点 → Del 删【锚点】不删【图层】（最小侵入：开头加一个分支）。
        if self.view._tool == "node" and self._node_overlay is not None and self._node_overlay["sel"]:
            self._delete_selected_anchors()
            return
        if not self.active:
            return
        if self.active.get("locked"):
            QtWidgets.QMessageBox.information(self, "提示", "图层已锁定，先点图层上的锁图标解锁再删除")
            return
        self._clear_node_overlay()  # B5：删层前清锚点 overlay（target 可能属被删层 → 防悬空）
        self._push_history("删除图层")
        self._clear_selection()  # 先清蚂蚁线(它是激活层子项)，再删层，避免悬空引用
        self._outline.setParentItem(None)
        self._resize_handle.setParentItem(None)
        if self.active.get("kind") == "vector":
            for it in self.active.get("items", []):  # 矢量层有多个顶层 item，逐个移除（哨兵不够）
                self.scene.removeItem(it)
        else:
            self.scene.removeItem(self.active["item"])
        self.layers.remove(self.active)
        for i, lyr in enumerate(self.layers):  # 重排 z：矢量层整组同 z
            if lyr.get("kind") == "vector":
                for it in lyr.get("items", []):
                    it.setZValue(i)
            else:
                lyr["item"].setZValue(i)
        self._set_active(self.layers[-1] if self.layers else None)
        self._refresh_connectors()  # 删的对象若是连接线端点 → 该连接线随之移除（不留悬空线）
        if hasattr(self, "_clear_connector_hover"):
            self._clear_connector_hover()  # 悬停对象被删 → 清掉其边中点锚点，不在原位留过期蓝点
        self.op_label.setText(f"删除图层 · 剩 {len(self.layers)} 层")

    def _mask_from_selection(self, layer=None):
        """把当前选区变成该层的非破坏蒙版（选区内露/外藏，原图像素不动）。layer=None→活动层。"""
        layer = layer or self.active
        if layer is None or layer.get("kind") == "vector":
            QtWidgets.QMessageBox.information(self, "图层蒙版", "请先选中一个图片/栅格图层"); return
        sel = self.selection_mask
        if sel is None:
            QtWidgets.QMessageBox.information(
                self, "图层蒙版", "先用 套索/矩形/魔棒/选区画笔 在该层上取一个选区，再生成蒙版"); return
        h, w = layer["image"].height(), layer["image"].width()
        if tuple(sel.shape[:2]) != (h, w):
            QtWidgets.QMessageBox.information(
                self, "图层蒙版", "选区与该图层尺寸不一致——请在要加蒙版的那个图层上取选区"); return
        self._push_history("添加图层蒙版")
        mask = np.where(sel > 0, np.uint8(255), np.uint8(0))  # 选区内露(255)、外藏(0)
        layer["mask"] = mask
        if layer.get("item"):
            layer["item"].set_mask(mask)
        self._thumb_cache.pop(layer.get("uid"), None)  # 缩略图含蒙版 → 失效重建
        self._clear_selection()
        self._update_outline(); self._refresh_layers()
        self.op_label.setText("已从选区生成图层蒙版（非破坏·原图不动·随时可删）")

    def _delete_mask(self, layer=None):
        layer = layer or self.active
        if layer is None or layer.get("mask") is None:
            return
        self._push_history("删除图层蒙版")
        layer["mask"] = None
        if layer.get("item"):
            layer["item"].set_mask(None)
        self._thumb_cache.pop(layer.get("uid"), None)
        self._refresh_layers()
        self.op_label.setText("已删除图层蒙版（恢复整层显示）")

    def _flip_objects(self, horizontal: bool):
        kind, tgt = self._transform_targets()
        if kind is None:
            self.op_label.setText("没有可翻转的对象（先选中矢量元素或一个图层）"); return
        self._push_history("水平翻转" if horizontal else "垂直翻转")
        if kind == "vector":
            for it in tgt:  # 绕各自包围盒中心翻转（QTransform 组合，往返 SVG/PDF）
                c = it.boundingRect().center()
                t = QtGui.QTransform()
                t.translate(c.x(), c.y())
                t.scale(-1 if horizontal else 1, 1 if horizontal else -1)
                t.translate(-c.x(), -c.y())
                it.setTransform(t, True)
            self._sync_vec_layers()
        else:  # 栅格：镜像像素（镜像 mask 保持对齐）；导出/画布一致
            layer = tgt
            layer["image"] = layer["image"].mirrored(horizontal, not horizontal)
            m = layer.get("mask")
            if m is not None:
                import numpy as np
                layer["mask"] = (np.fliplr(m) if horizontal else np.flipud(m)).copy()
                layer["item"].set_mask(layer["mask"])
            layer["item"].set_image(layer["image"])
        self._refresh_layers()
        self.op_label.setText("已" + ("水平" if horizontal else "垂直") + "翻转")

    def _rotate_objects(self, degrees: int):
        kind, tgt = self._transform_targets()
        if kind is None:
            self.op_label.setText("没有可旋转的对象（先选中矢量元素或一个图层）"); return
        self._push_history("旋转 %d°" % degrees)
        if kind == "vector":
            for it in tgt:
                c = it.boundingRect().center()
                t = QtGui.QTransform()
                t.translate(c.x(), c.y()); t.rotate(degrees); t.translate(-c.x(), -c.y())
                it.setTransform(t, True)
            self._sync_vec_layers()
        else:  # 栅格：转像素（90° 会换宽高）；mask 同向旋转保持对齐
            layer = tgt
            layer["image"] = layer["image"].transformed(QtGui.QTransform().rotate(degrees))
            m = layer.get("mask")
            if m is not None:
                import numpy as np
                k = -1 if (degrees % 360) == 90 else 1  # 匹配 QImage 顺时针 90° 方向
                layer["mask"] = np.ascontiguousarray(np.rot90(m, k))
                layer["item"].set_mask(layer["mask"])
            layer["item"].set_image(layer["image"])
        self._refresh_layers()
        self.op_label.setText("已旋转 %d°" % degrees)
