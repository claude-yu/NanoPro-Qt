"""SelectionMixin —— EditorWindow 的「选区 / 魔棒 / 套索 / 矩形 / 选区画笔 / 蚂蚁线 / 抠出 / 去背景 / 拆解」功能（从 editor_window.py 抽出，行为不变）。

魔棒命中、三态选区合成（new/add/subtract）、套索/矩形选区与抠出/挖洞、选区画笔涂抹、拖动预览虚线、
蚂蚁线动画、选区清除/重选、图层载入为选区、去背景/GrabCut/删除选区像素、自动拆解、AI 抠图分割、抠出（复制/剪切）。
本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：active/layers/scene/view/selection_mask/op_label/
tol_slider/feather_slider/size_slider/hole_check/_sel_mode/_sel_points/_rect_p0/_rect_p1/_brush_mask/_brush_last/
_brush_preview/_brush_preview_clock/_sel_preview/_ants/_ants_base/_ants_timer/_ants_offset/_last_selection/
_seg_worker/_seg_epoch/_seg_dialog/_seg_mode/_outline/_resize_handle/assets/
_need_active_for_sel/_effective_mode/_rasterize_lasso/_rasterize_rect/_brush_stamp/_update_brush_preview/
_remove_brush_preview/_push_history/_set_active/_add_layer/_refresh_layers/_refresh_assets/_clear_selection/
_active_locked/_cancel_pen/_rebuild_node_overlay/_begin_busy/_end_busy/_ai_ref_layer_b64/_ai_snapshot_b64/
_on_ai_segment_done 等，MRO 解析）。
"""
from __future__ import annotations

import ai_panel
import config
import image_ops
import numpy as np
import theme
from PySide6 import QtCore, QtGui, QtWidgets


class SelectionMixin:
    def do_auto_decompose(self):
        if not self.active:
            self._toast("请先选中一个图层（通常是底图）")
            return
        rgba = image_ops.qimage_to_rgba(self.active["image"])
        # 主线程阻塞的 cv2 操作：只给等待光标（busy 动画无法滚动，因事件循环不转）。
        # TODO: 要真滚动进度条需把 auto_decompose 挪到 QThread worker（超出本次"点按钮弹进度条"需求）。
        self.op_label.setText("自动拆解中…")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            pieces, info = image_ops.auto_decompose(rgba, self.tol_slider.value())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()  # 无论拆解成败都复原，不残留 override cursor
        if not pieces:
            self._toast(info)
            return
        for p in pieces:
            self.assets.append(image_ops.rgba_to_qimage(p))
        self._refresh_assets()
        self.op_label.setText(f"自动拆解：{len(pieces)} 个素材已入库")

    def do_ai_segment(self):
        # 1. 取源图：有活动层→优先活动层；否则画布合成。fail-loud：都没有则提示。
        src = self._ai_ref_layer_b64() or self._ai_snapshot_b64()
        if not src:
            self._toast("请先导入图片或选中一个图层")
            return
        conn = config.get_seg_conn()
        provider = conn["provider"]
        # http/ppio 未配置 → 先弹设置（用户可能在对话框里把后端改成 grsai/rembg）。
        # 注意：ppio 有内置默认地址(api.ppinfra.com)，【只需 Key】——绝不能把"地址为空"当未配置，
        # 否则配好 Key 也每次点都弹设置框、功能永远跑不起来（用户反馈的反复弹窗 bug）。
        need_cfg = ((provider == "http" and (not conn["base_url"] or not conn["has_key"]))
                    or (provider == "ppio" and not conn["has_key"]))
        if need_cfg:
            if not self._ai_seg_settings_dialog():
                return                                     # 用户取消
            conn = config.get_seg_conn()
            provider = conn["provider"]
        # 统一在（可能的）对话框之后，按【最终】provider 解析凭据
        # （修 MEDIUM：对话框里切到 grsai/rembg 时，原先会误用空的 seg 凭据导致 grsai 永远报“未配置 Key”）
        if provider == "grsai":
            gc = config.get_connection()  # 复用 grsai 生图配置（用户已配好 key）
            if not gc.get("has_key"):
                self._toast("grsai 后端未配置 Key，请到 AI 生成面板设置")
                return
            base_url = conn.get("node") or config.grsai_base()
            key = config.read_key()
            model = conn["model"] or gc.get("model") or ""
        elif provider == "rembg":
            base_url, key, model = "", "", ""              # 本地，无需地址/key
        else:                                              # http / ppio
            if provider == "http" and (not conn["base_url"] or not conn["has_key"]):
                self.op_label.setText("AI 抠图：HTTP 后端需填「地址」+「Key」")  # fail-loud
                return
            if provider == "ppio" and not conn["has_key"]:
                self.op_label.setText("AI 抠图：PPIO 后端需填「Key」")  # fail-loud
                return
            # ppio 地址留空 → seg_client 用内置默认 api.ppinfra.com（_segment_ppio 兜底）
            base_url, key, model = config.seg_base(), config.read_seg_key(), conn["model"]
        if (self._seg_worker and self._seg_worker.isRunning()
                and getattr(self._seg_worker, "_epoch", -1) == self._seg_epoch):
            # 仅当【当前这次】抠图仍在进行才拦(防重复提交)，并 fail-loud 提示，不静默吞点击(审核 HIGH)。
            # 已取消但网络未断的旧 worker 不在此列——放行开新一次，旧的 done 由 epoch 丢弃。
            self.op_label.setText("AI 抠图：正在进行中，请等本次完成…")
            return
        mode = getattr(self, "_seg_mode", "elements")      # 默认拆解；设置对话框里可改
        params = {
            "src_b64": src, "mode": mode, "provider": provider,
            "base_url": base_url, "key": key,
            "model": model, "timeout": 180, "endpoint": conn["endpoint"] or None,
            "prompt": conn["prompt"] or None,              # 编辑指令（grsai/ppio；None=默认去背景）
            "result_endpoint": conn["result_endpoint"] or None,  # ppio 异步取结果端点
        }
        self._seg_epoch += 1
        ep = self._seg_epoch
        self._seg_worker = ai_panel.SegWorker(params, self)
        self._seg_worker._epoch = ep                       # 记 epoch：done 回来时比对，丢弃被取消/被顶替的旧 worker
        self._seg_worker.done.connect(self._on_ai_segment_done)  # 默认连接：worker 在别线程→QueuedConnection→槽在主线程跑
        self.op_label.setText("AI 抠图/拆解中…")            # 底部小字兜底（不删，原则 3）
        self._seg_dialog = self._begin_busy("AI 抠图/拆解中…（联网调用，约 10 秒~1 分钟，首次较慢）")
        self._seg_dialog.canceled.connect(self._on_seg_cancel)
        self._seg_worker.start()

    def _on_seg_cancel(self):
        # 取消=放弃（abandon），不真正中断网络（seg_client 阻塞式单发，QThread 内无中断点，强杀不安全）。
        # 自增 epoch 使在途 worker 的 _epoch 失效→其 done 到达时被丢弃。fail-loud：明说后台仍跑完、结果被忽略。
        self._end_busy(self._seg_dialog)
        self._seg_dialog = None
        self._seg_epoch += 1
        self.op_label.setText("AI 抠图：已取消（后台请求仍会跑完，但结果被忽略）")

    def _update_outline(self):
        # M3: 激活层为空或不可见时，一律不挂轮廓/手柄（防止手柄留在看不见的层上）
        if self.active is None or not self.active.get("visible", True):
            self._outline.setParentItem(None); self._outline.hide()
            self._resize_handle.setParentItem(None); self._resize_handle.hide()
            return
        if self.active.get("kind") == "vector":
            # 矢量层：每个图元自带 QGraphicsView 选中虚框；B1 不挂统一轮廓/缩放手柄（整层缩放留 B3）
            self._outline.setParentItem(None); self._outline.hide()
            self._resize_handle.setParentItem(None); self._resize_handle.hide()
            return
        item = self.active["item"]
        self._outline.setParentItem(item)  # 子项 → 自动跟随该层位置/移动/缩放
        self._outline.setRect(0, 0, item.image().width(), item.image().height())
        self._outline.show()
        # 缩放手柄：作子项放在 (w,h)=右下角，随层位置/缩放自动跟随；仅移动工具时显示
        self._resize_handle.setParentItem(item)
        self._resize_handle.setPos(item.image().width(), item.image().height())
        self._resize_handle.setVisible(self.view._tool == "move" and not self.active.get("locked", False))

    def _draw_to(self, scene_pos: QtCore.QPointF):
        layer = self.active
        item = layer["item"]
        p0 = item.mapFromScene(self._last_scene)
        p1 = item.mapFromScene(scene_pos)
        img = layer["image"]
        painter = QtGui.QPainter(img)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if self.view._tool == "eraser":
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
        pen = QtGui.QPen(self.brush_color, self.size_slider.value())
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(p0, p1)
        painter.end()
        r = self.size_slider.value() / 2 + 2  # 只重绘本段覆盖的脏矩形，不整图转换
        dirty = QtCore.QRectF(p0, p1).normalized().adjusted(-r, -r, r, r)
        item.update(dirty)
        self._last_scene = scene_pos

    # ---------- OpenCV 魔棒 / 去背景 / 抠出 ----------
    def _wand_at(self, scene_pos: QtCore.QPointF):
        # 魔棒：点击按颜色选中相近区域。Shift=加选 / Alt=减选 / 否则按当前模式（默认新建）——支持多区域加减选。
        # 走统一的 _set_selection（_effective_mode 实时读修饰键 + _compose_selection 合成），与套索/选区画笔一致。
        if not self._need_active_for_sel():
            return
        item = self.active["item"]
        local = item.mapFromScene(scene_pos)
        x, y = int(local.x()), int(local.y())
        img = self.active["image"]
        if not (0 <= x < img.width() and 0 <= y < img.height()):
            return
        rgba = image_ops.qimage_to_rgba(img)
        mask = image_ops.magic_wand_mask(rgba, x, y, self.tol_slider.value())
        if not mask.any():
            self.op_label.setText("魔棒未选中区域（调容差再试）"); return  # 空命中不清已有选区（减选时尤其重要）
        self._set_selection(mask)  # Shift 加 / Alt 减 / 否则当前模式（默认新建·替换）

    # ----- 选区模式合成 -----
    def _compose_selection(self, existing, new, mode, empty_on_replace_subtract=False):
        # 三态选区合成单一真源（消除 _set_selection / _load_layer_as_selection 的重复）。
        # 替换分支统一"existing 为 None / mode=='new' / 形状不符"三条件；
        # empty_on_replace_subtract 精确区分两站差异：
        #   _set_selection 替换=new(原始掩码，传 False)；
        #   _load_layer_as_selection 无兼容选区时 subtract=清空(传 True)。
        if existing is None or mode == "new" or existing.shape != new.shape:
            return np.zeros_like(new) if (empty_on_replace_subtract and mode == "subtract") else new
        if mode == "add":
            return np.maximum(existing, new)
        return np.where(new > 0, 0, existing).astype(np.uint8)

    def _set_selection(self, new_mask):
        if new_mask is None:
            return
        mode = self._effective_mode()  # 【实时】读 QApplication.keyboardModifiers()
        self.selection_mask = self._compose_selection(self.selection_mask, new_mask, mode, empty_on_replace_subtract=False)
        cnt = int((self.selection_mask > 0).sum())
        if cnt == 0:
            self._clear_selection(); self.op_label.setText("选区为空"); return
        self._show_ants()
        self.op_label.setText(f"选区 {cnt} px [{mode}]")

    def _point_in_selection(self, scene_pos: QtCore.QPointF) -> bool:
        """场景点是否落在当前选区内（映射到激活层局部坐标查 mask）——lift-out 用。"""
        if self.selection_mask is None or self.active is None:
            return False
        local = self.active["item"].mapFromScene(scene_pos)
        x, y = int(local.x()), int(local.y())
        m = self.selection_mask
        return 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and m[y, x] > 0

    def _lasso_start(self, sp):
        if not self._need_active_for_sel():
            return
        self._sel_points = [sp]; self._start_preview()

    def _lasso_move(self, sp):
        if not self._sel_points:
            return
        self._sel_points.append(sp); self._update_preview_path()

    def _lasso_end(self):
        pts = self._sel_points; self._sel_points = []; self._remove_preview()
        if len(pts) >= 3 and self.active:
            self._set_selection(self._rasterize_lasso(pts))

    def _rect_start(self, sp):
        if not self._need_active_for_sel():
            return
        self._rect_p0 = sp; self._rect_p1 = sp; self._start_preview()

    def _rect_move(self, sp):
        if self._rect_p0 is None:
            return
        self._rect_p1 = sp; self._update_preview_rect()

    def _rect_end(self):  # 矩形抠出(cutout)：拖框直接抠成可移动层，不停在选区态。
        # 参考 applyCutout app.js:990-1030；差异：Qt 从【激活层】抠出，WebView 从整张合成快照抠出，
        # 多层框选时内容不同——Qt 图层模型下的有意设计，非逐像素对齐。
        if self._rect_p0 is None:
            return
        p0, p1 = self._rect_p0, self._rect_p1; self._rect_p0 = None; self._remove_preview()
        if not self.active:
            return
        mask = self._rasterize_rect(p0, p1)
        if int((mask > 0).sum()) < 16:  # 框太小，忽略
            return
        self.selection_mask = mask
        self.do_extract()  # 复用抠出（含剪切模式留洞/填底），末尾自动切移动工具

    def _rectsel_end(self):  # 矩形选框选区：拖框→在激活层生成矩形选区(留蚂蚁线)，不抠不挖，可作 GrabCut 种子。
        if self._rect_p0 is None:
            return
        p0, p1 = self._rect_p0, self._rect_p1; self._rect_p0 = None; self._remove_preview()
        if not self.active:
            return
        mask = self._rasterize_rect(p0, p1)  # item.mapFromScene 两角 → rect_mask，与 active image 同尺寸
        if int((mask > 0).sum()) < 16:  # 框太小，忽略（与 _rect_end 同阈值）
            return
        self._set_selection(mask)  # 复用 new/add(Shift)/subtract(Alt) 合成 + _show_ants 蚂蚁线 + 计数提示

    def _erase_rect_end(self):  # 矩形挖洞：拖框→取边缘背景色在【当前层】填充覆盖该区。
        # 注：WebView 的 erase 在整篇合成快照取色、把洞放进全局 holes 叠加层(可整体撤销)；这里是 Qt
        # 图层模型下的单层破坏式填充(改写 active 层像素)，多层场景与 WebView 不同——有意差异，非完全对齐。
        if self._rect_p0 is None:
            return
        p0, p1 = self._rect_p0, self._rect_p1; self._rect_p0 = None; self._remove_preview()
        if not self.active or self._active_locked():
            return
        mask = self._rasterize_rect(p0, p1)
        if int((mask > 0).sum()) < 4:
            return
        self._push_history("矩形挖洞")
        rgba = image_ops.qimage_to_rgba(self.active["image"])
        color = image_ops.fill_color_from_edge(rgba, mask)
        self.active["image"] = image_ops.rgba_to_qimage(image_ops.fill_by_mask(rgba, mask, color))
        self.active["item"].set_image(self.active["image"])
        self._refresh_layers()
        self.op_label.setText("已用背景色填充覆盖该矩形")

    def _crop_end(self):  # 裁剪：把当前层裁到拖出的矩形(层内坐标)（对齐 cropSprite app.js:1054-1092）
        if self._rect_p0 is None:
            return
        p0, p1 = self._rect_p0, self._rect_p1; self._rect_p0 = None; self._remove_preview()
        if not self.active:
            self._toast("请先选中要裁剪的图层")
            return
        if self._active_locked():
            return
        item = self.active["item"]; img = self.active["image"]
        a = item.mapFromScene(p0); b = item.mapFromScene(p1)
        x0, x1 = sorted((int(a.x()), int(b.x()))); y0, y1 = sorted((int(a.y()), int(b.y())))
        x0 = max(0, x0); y0 = max(0, y0); x1 = min(img.width(), x1); y1 = min(img.height(), y1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        scene_tl = item.mapToScene(QtCore.QPointF(x0, y0))  # 裁剪前记下框左上角场景位，保持视觉不动
        self._push_history("裁剪")
        cropped = img.copy(x0, y0, x1 - x0, y1 - y0)
        self.active["image"] = cropped
        item.set_image(cropped)
        self._suspend_history = True
        item.setPos(scene_tl)
        self._suspend_history = False
        self._update_outline(); self._refresh_layers()
        self.op_label.setText(f"已裁剪到 {x1 - x0}×{y1 - y0}")

    # ----- 选区画笔（涂抹累积选区，对齐 WebView brush 选区笔 features.js:923-944）-----
    def _brush_sel_start(self, sp):
        if not self._need_active_for_sel():
            return
        img = self.active["image"]
        self._brush_mask = np.zeros((img.height(), img.width()), np.uint8)
        self._brush_last = sp
        self._brush_stamp(sp, sp)
        self._brush_preview_clock.restart()  # 节流基准
        self._update_brush_preview()          # 起笔即出预览（让"已涂多少"实时可见，松手前不是空白）

    def _brush_sel_move(self, sp):
        if self._brush_mask is None:
            return
        self._brush_stamp(self._brush_last, sp); self._brush_last = sp
        # 节流：mousemove 高频，findContours 扫整层 O(W×H)，每点重算会卡 → 隔 ~60ms 才刷一次预览轮廓。
        if self._brush_preview_clock.elapsed() >= 60:
            self._brush_preview_clock.restart()
            self._update_brush_preview()

    def _brush_sel_end(self):
        m = self._brush_mask; self._brush_mask = None; self._brush_last = None
        self._remove_brush_preview()  # 先清涂抹预览，再转正式蚂蚁线（避免两者叠加）
        if m is None or not m.any() or not self.active:
            return
        # 复用 _set_selection 三态合成（new=替换/add=并/subtract=减 + Shift/Alt 覆盖），
        # 与套索/矩形完全一致（修 #3 Shift=add 失效、#4 new 模式误做 union）。
        self._set_selection(m)

    # ----- 拖动预览（场景坐标虚线）-----
    def _start_preview(self):
        self._remove_preview()
        self._sel_preview = QtWidgets.QGraphicsPathItem()
        pen = QtGui.QPen(QtGui.QColor(theme.colors()["accent"]), 0)
        pen.setCosmetic(True); pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        self._sel_preview.setPen(pen); self._sel_preview.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        self._sel_preview.setZValue(10000)
        self._sel_preview.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.scene.addItem(self._sel_preview)

    def _update_preview_path(self):
        if self._sel_preview is None or not self._sel_points:
            return
        path = QtGui.QPainterPath(self._sel_points[0])
        for p in self._sel_points[1:]:
            path.lineTo(p)
        self._sel_preview.setPath(path)

    def _update_preview_rect(self):
        if self._sel_preview is None or self._rect_p0 is None:
            return
        path = QtGui.QPainterPath()
        path.addRect(QtCore.QRectF(self._rect_p0, self._rect_p1).normalized())
        self._sel_preview.setPath(path)

    def _remove_preview(self):
        if self._sel_preview is not None:
            self.scene.removeItem(self._sel_preview); self._sel_preview = None

    # ----- 蚂蚁线（选区轮廓，黑白虚线动画，作激活层子项随层移动）-----
    @staticmethod
    def _mask_to_path(mask):
        """掩码 → 轮廓 QPainterPath（层内坐标）。蚂蚁线与涂抹预览共用，避免两份轮廓代码漂移。"""
        path = QtGui.QPainterPath()
        for c in image_ops.mask_contours(mask):
            path.moveTo(float(c[0][0]), float(c[0][1]))
            for q in c[1:]:
                path.lineTo(float(q[0]), float(q[1]))
            path.closeSubpath()
        return path

    def _show_ants(self):
        self._remove_ants()
        if self.selection_mask is None or self.active is None:
            return
        path = self._mask_to_path(self.selection_mask)
        item = self.active["item"]
        base = QtWidgets.QGraphicsPathItem(path, item)
        pw = QtGui.QPen(QtGui.QColor("#ffffff"), 0); pw.setCosmetic(True)
        base.setPen(pw); base.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        base.setZValue(60); base.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        ants = QtWidgets.QGraphicsPathItem(path, item)
        pb = QtGui.QPen(QtGui.QColor("#000000"), 0); pb.setCosmetic(True); pb.setDashPattern([4, 4])
        ants.setPen(pb); ants.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        ants.setZValue(61); ants.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._ants_base = base; self._ants = ants
        self._ants_timer.start(80)

    def _tick_ants(self):
        if self._ants is None:
            self._ants_timer.stop(); return
        self._ants_offset = (self._ants_offset + 1) % 8
        pen = self._ants.pen(); pen.setDashOffset(self._ants_offset); self._ants.setPen(pen)

    def _remove_ants(self):
        for it in (self._ants, self._ants_base):
            if it is not None and it.scene() is not None:
                self.scene.removeItem(it)
        self._ants = None; self._ants_base = None

    def _clear_selection(self):
        if self._pen_state is not None:  # B5：Esc/切工具时优先取消未完成的钢笔（丢预览、不入撤销）
            self._cancel_pen()
        if self.view._tool == "node" and self._node_overlay is not None and self._node_overlay["sel"]:
            self._node_overlay["sel"] = set()  # B5：node 工具下 Esc 取消锚点选择（仿 AI），overlay 保留（审核 LOW）
            self._rebuild_node_overlay()
        if self.selection_mask is not None and self.selection_mask.any():
            self._last_selection = self.selection_mask  # 记住最后选区，供「重新选择」恢复（PS Reselect）
        self.selection_mask = None
        self._remove_ants(); self._remove_preview(); self._remove_brush_preview(); self._ants_timer.stop()
        self._sel_points = []; self._rect_p0 = None; self._brush_mask = None; self._brush_last = None

    def reselect(self):
        """重新选择：恢复最后一次取消掉的选区（PS 选择→重新选择 / Shift+Ctrl+D）。"""
        m = self._last_selection
        if m is None:
            self.op_label.setText("没有可恢复的选区"); return
        if self.active is None:
            self.op_label.setText("请先选中一个图层再恢复选区"); return
        img = self.active["image"]
        if m.shape != (img.height(), img.width()):  # 选区是激活层局部坐标，尺寸不符不能套用
            self.op_label.setText("上次选区与当前层尺寸不符，无法恢复"); return
        self.selection_mask = m.copy()
        self._show_ants()
        self.op_label.setText(f"已重新选择上次选区（{int((m > 0).sum())} px）")

    def _layer_alpha_in_active_space(self, layer):
        """把 layer 的非透明像素(alpha>8)映射到【当前激活层局部坐标】的二值掩码。跨层位置/缩放经场景变换换算。"""
        if self.active is None:
            return None
        aimg = self.active["image"]; aw, ah = aimg.width(), aimg.height()
        lrgba = image_ops.qimage_to_rgba(layer["image"])
        m = (lrgba[:, :, 3] > 8).astype(np.uint8) * 255
        ai, li = self.active["item"], layer["item"]
        if layer is self.active or (m.shape == (ah, aw) and li.pos() == ai.pos()
                                    and abs(li.scale() - ai.scale()) < 1e-6):
            return m  # 同坐标系直接用
        mask_qi = image_ops.rgba_to_qimage(np.dstack([m, m, m, m]))  # 白色不透明=选中
        inv, ok = ai.sceneTransform().inverted()
        if not ok:
            return None
        t = li.sceneTransform() * inv  # 行向量：layer-local → scene → active-local
        target = QtGui.QImage(aw, ah, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        target.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(target)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setTransform(t); p.drawImage(0, 0, mask_qi); p.end()
        return (image_ops.qimage_to_rgba(target)[:, :, 3] > 8).astype(np.uint8) * 255

    def _load_layer_as_selection(self, layer, mode="new"):
        """Ctrl 点击图层 → 载入其像素为选区（PS 载入图层选区）。mode: new/add/subtract。"""
        if layer not in self.layers:
            return
        if layer.get("kind") == "vector":  # 矢量层无像素 → 无法载入为选区（fail-loud）
            self.op_label.setText("矢量层没有像素，无法载入为选区"); return
        if mode == "new":
            self._set_active(layer)  # 激活该层（会清选区），选区=它自己的不透明像素（本层坐标）
            m = (image_ops.qimage_to_rgba(layer["image"])[:, :, 3] > 8).astype(np.uint8) * 255
            if not m.any():
                self.op_label.setText("该层没有可载入的不透明像素"); return
            self.selection_mask = m
            self._show_ants()
            self.op_label.setText(f"已载入「{layer.get('name', '图层')}」为选区（{int((m > 0).sum())} px）")
            return
        if self.active is None:
            self.op_label.setText("先选中一个图层再加/减选区"); return
        m = self._layer_alpha_in_active_space(layer)
        if m is None or not m.any():
            self.op_label.setText("无法把该层载入为选区"); return
        # mode 是点击时捕获、经 QTimer.singleShot 延后传入的【显式参数】（绝不改读 _effective_mode），
        # 到此处必为 add/subtract（new 已被上方早退分支拦截）；empty_on_replace_subtract=True 保住
        # "无兼容选区时 subtract→清空"（等价原 np.zeros_like(m) if mode=="subtract" else m）。
        self.selection_mask = self._compose_selection(self.selection_mask, m, mode, empty_on_replace_subtract=True)
        cnt = int((self.selection_mask > 0).sum())
        if cnt == 0:
            self._clear_selection(); self.op_label.setText("选区为空"); return
        self._show_ants()
        self.op_label.setText(f"图层选区{'加选' if mode == 'add' else '减选'} → {cnt} px")

    def _need_selection(self) -> bool:
        if self.selection_mask is None or self.active is None:
            self._toast("先用套索 / 矩形 / 魔棒取一个选区")
            return False
        return True

    def do_remove_bg(self):
        # H7: 自动检测背景 → 前景生成透明素材入库（对齐 removeBackgroundToAsset features.js:823-839），
        # 不删源层像素。有选区=限定在选区内，无选区=整层。
        layer = self.active or (self.layers[-1] if self.layers else None)
        if layer is None:
            self._toast("请先导入图片或新建一个图层")
            return
        rgba = image_ops.qimage_to_rgba(layer["image"])
        bg = image_ops.background_mask(rgba, self.tol_slider.value())
        fg = (~bg) & (rgba[:, :, 3] > 8)
        sel = self.selection_mask
        if sel is not None and sel.shape == fg.shape:  # 有选区 → 只取选区内前景
            fg = fg & (sel > 0)
        if not fg.any():
            self.op_label.setText("未检测到前景（可调高容差再试）"); return
        sprite = image_ops.mask_to_sprite(rgba, (fg.astype(np.uint8) * 255),
                                          erode=True, feather=float(self.feather_slider.value()))
        if sprite is None:
            self.op_label.setText("未检测到前景（可调高容差再试）"); return
        self.assets.append(image_ops.rgba_to_qimage(sprite[0]))
        self._refresh_assets()
        self.op_label.setText(f"去背景：已生成透明素材（共 {len(self.assets)} 个）")

    def do_grabcut(self):
        # GrabCut 抠图：用当前选区作前景种子，对非纯色背景抠主体 → 透明素材入库（仿 do_remove_bg）。
        # 不碰源层、不入历史（与 do_remove_bg 一致；do_delete_selection 才改源层+_push_history）。
        layer = self.active or (self.layers[-1] if self.layers else None)
        if layer is None:
            self._toast("请先导入图片或新建一个图层")
            return
        rgba = image_ops.qimage_to_rgba(layer["image"])
        sel = self.selection_mask
        if sel is None or sel.shape != rgba.shape[:2] or not (sel > 0).any():
            self.op_label.setText("GrabCut：请先取个粗选区再抠")
            self._toast("GrabCut 需要先用魔棒 / 套索 / 选区画笔取一个粗选区")
            return
        # 逐次迭代 + 项目内 ProgressSheet 真进度条（每步 processEvents 刷新；可取消）。
        self.op_label.setText("GrabCut 计算中…")
        ITERS = 8  # 多分几步 → 进度条更顺
        dlg = self._begin_progress("GrabCut 抠图", "正在计算前景区域", ITERS)

        def _cb(done, total):
            if getattr(dlg, "_total", 0) != total:
                dlg._total = total
                dlg.bar.setRange(0, total)
            dlg.step(done, "正在迭代 %d / %d" % (done, total))
            return not dlg.wasCanceled()

        ok = False
        try:
            fg, err = image_ops.grabcut_mask(rgba, seed_mask=sel, iters=ITERS, progress_cb=_cb)
            if dlg.wasCanceled():
                self.op_label.setText("GrabCut 已取消"); return
            if err:
                self.op_label.setText(err); return  # 大声失败，不静默
            sprite = image_ops.mask_to_sprite(rgba, fg, erode=True, feather=float(self.feather_slider.value()))
            if sprite is None:
                self.op_label.setText("GrabCut 抠出的前景为空（试试更贴主体的选区）"); return
            self.assets.append(image_ops.rgba_to_qimage(sprite[0]))
            self._refresh_assets()
            ok = True
        finally:
            self._end_progress(dlg)  # 无论成功/失败/取消都关进度条
        if not ok:
            return
        # 防呆：GrabCut 抠【单主体】，对宽幅多元素整图常只留中间团块、丢掉低对比边缘部分（用户反馈"抠出方的/缺了一块"）。
        # 抠出远小于种子 → 明确提示改用「去背景」/「自动拆解」（整图保比例保全元素），fail-loud 而非让用户以为是 bug。
        seed_px = int((sel > 0).sum())
        kept_px = int((fg > 0).sum())
        pct = round(100 * kept_px / seed_px) if seed_px else 100
        hint = ""
        if seed_px and kept_px < seed_px * 0.6:
            hint = (f"；仅保留所选区约 {pct}%（GrabCut 会丢低对比边缘）"
                    "——要整图保比例/保留所有元素请改用「去背景」或「自动拆解」")
            if kept_px < seed_px * 0.4 and not getattr(self, "_grabcut_dropwarned", False):
                self._grabcut_dropwarned = True  # 一次性提示，不每次弹（仿 _vec_edit_warned）
                self._toast("GrabCut 只保留约 %d%%，整图保比例请改用去背景或自动拆解" % pct, 4200)
        self.op_label.setText(f"GrabCut 抠图：已生成透明素材（共 {len(self.assets)} 个）{hint}")

    def do_delete_selection(self):
        # 旧「去背景」语义独立出来：直接把选区像素抹成透明（删除选区内容）。
        if not self._need_selection() or self._active_locked():
            return
        self._push_history("删除选区")
        m = image_ops.feather_mask(self.selection_mask, self.feather_slider.value())
        rgba = image_ops.qimage_to_rgba(self.active["image"])
        self.active["image"] = image_ops.rgba_to_qimage(image_ops.remove_by_mask(rgba, m))
        self.active["item"].set_image(self.active["image"])
        self._clear_selection()
        self.op_label.setText("已删除选区像素")

    def _crop_selection(self):
        """当前选区抠出并裁到外接矩形 → (cropped_QImage, x0, y0, feathered_mask)；无效返回 None。"""
        if self.selection_mask is None or self.active is None:
            return None
        m = image_ops.feather_mask(self.selection_mask, self.feather_slider.value())
        bbox = image_ops.mask_bbox(m)
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        full = image_ops.extract_by_mask(image_ops.qimage_to_rgba(self.active["image"]), m)
        cropped = np.ascontiguousarray(full[y0:y1, x0:x1])  # 裁到选区 → 抠出物只有选区那么大
        return image_ops.rgba_to_qimage(cropped), x0, y0, m

    def _extract_shortcut(self):
        # Enter：有选区才抠出，无选区静默忽略（不弹窗打断，对齐 features.js:1258-1260）
        if self.selection_mask is not None and self.active is not None:
            self.do_extract()

    def do_extract(self, *_):
        if not self._need_selection():
            return
        res = self._crop_selection()
        if res is None:
            self._toast("选区为空")
            return
        new_img, x0, y0, m = res
        cut = self.hole_check.isChecked()  # 剪切模式：原位填底色覆盖
        if cut and self._active_locked():   # 剪切会改源层，锁定时禁止（复制不改源层，允许）
            return
        self._push_history("抠图（剪切）" if cut else "抠图（复制）")
        src = self.active
        s = src["item"].scale()                                   # 继承源层缩放
        scene_tl = src["item"].mapToScene(QtCore.QPointF(x0, y0))  # 选区左上角的场景坐标(含缩放)
        if cut:  # 剪切：原位用选区外缘平均色填底覆盖（科研白底图→填白，干净；不留透明洞）
            src_rgba = image_ops.qimage_to_rgba(src["image"])
            color = image_ops.fill_color_from_edge(src_rgba, m)
            src["image"] = image_ops.rgba_to_qimage(image_ops.fill_by_mask(src_rgba, m, color))
            src["item"].set_image(src["image"])
        layer = self._add_layer(new_img, f"抠出 {len(self.layers) + 1}", "paint")  # _set_active 会清选区
        self._suspend_history = True
        layer["item"].setScale(s)        # 与源层同缩放
        layer["item"].setPos(scene_tl)   # 放在场景中对应(缩放后)位置
        self._suspend_history = False
        self._update_outline()
        self.set_tool("move")  # 抠出后切移动工具，可直接拖走这块
        self.op_label.setText(f"抠出 {new_img.width()}×{new_img.height()}" + ("（剪切·已填底色覆盖）" if cut else "（复制·源层不变）"))
