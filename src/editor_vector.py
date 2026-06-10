"""VectorMixin —— EditorWindow 的「矢量图（SVG/PDF）导入导出 + 矢量元素编辑 + 锚点(节点)编辑 + 钢笔 + 形状」功能（从 editor_window.py 抽出，行为不变）。

涵盖：
- SVG 导入(import_svg)/导出(export_svg)、混合矢量 PDF 导出(export_pdf)、矢量属性面板(_build_vec_panel)；
- 矢量元素选择/改填充/改描边/描边宽/改字体/改字色/Okabe-Ito 配色映射；
- 矢量元素打组/解组(do_group_vec_elements/do_ungroup_vec_elements)；
- 锚点(节点)编辑：overlay 构建/刷新/提交、anchor/ctrl handle 拖动、加点/删点/角点切换、删路径；
- 钢笔工具：按下/拖控制柄/释放/悬停预览/提交/取消/收尾；
- 形状工具(矩形/椭圆/直线/箭头) + 统一注册管线 _register_vec_path + 矢量层杂项(_vector_layers/_wire_vec_item/_sync_vec_layers/_note_vec_edit/_vec_live_push)。

本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：active/layers/scene/view/canvas_size/op_label/
_layer_uid/_node_overlay/_pen_state/_node_target/_vec_live_pushed/_outline/_resize_handle/_ants/_ants_base/
_sel_preview/_brush_preview/source_name/source_dpi 等成员，以及 _push_history/_set_active/_refresh_layers/
_snap_drag_pos/_layer_items/_b5_overlay_items/_vector_layers/_count_outlined_text/_count_text/_svg_canvas_size/
_last_dir/_remember_dir/_save_start/_hint/set_tool/fit_view 等方法，MRO 解析）。

DEFAULT_CANVAS 仍定义在 editor_window 模块级（空画布初始化用），用到处函数内惰性 import（避免循环依赖）。
"""
from __future__ import annotations

import os

import svg_io
from PySide6 import QtCore, QtGui, QtWidgets


class VectorMixin:
    def _vector_layers(self) -> list:
        return [l for l in self.layers if l.get("kind") == "vector"]

    # _only_vector_visible 已抽到 editor_export.ExportMixin。

    def _wire_vec_item(self, it):
        # 给矢量 item 挂拖动入撤销回调（B3）：首次位移调 _push_history。group 子项递归挂
        # （拖子项时只子项收 itemChange；拖整组时只 group 收 → group 和叶子都要挂）。
        it._move_cb = self._push_history
        it._moved_this_drag = False
        it._snap_cb = (lambda pos, _it=it: self._snap_drag_pos(_it, pos))  # 统一磁吸(吸到所有元素/画布/参考线/网格)
        if isinstance(it, QtWidgets.QGraphicsItemGroup):
            for child in it.childItems():
                self._wire_vec_item(child)
        else:  # 设备坐标缓存：拖动/缩放只贴缓存位图，不每帧重渲染填充路径 → 箭头/形状拖动丝滑（栅格层早已开）
            it.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def import_svg(self, path: str | None = None):
        """导入 SVG → 拆成可选/可移动的独立矢量图元（<path>/<text>/<g>），登记为一条 kind='vector' 层。

        坐标系：item 用元素自身局部坐标 + QTransform（祖先变换走 group），不烘焙；故 <g>/transform 往返可保。
        撤销：整层导入入历史（撤销=移除该矢量层）；内部改色/改字/拖动/打组均入历史（B3，快照深拷 velems 重建）。
        path 非空时跳过文件对话框（供离屏测试）。
        """
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "导入矢量图 (SVG)", self._last_dir("import"), "SVG (*.svg);;所有文件 (*.*)")
            if not path:
                return
        self._remember_dir("import", path)
        try:
            velems, skipped, meta = svg_io.parse_svg(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "导入 SVG 失败", f"解析失败：{e}")
            return
        if not velems:
            self._toast("未解析到任何矢量元素")
            return
        pairs = svg_io.build_items(velems)
        if not pairs:
            self._toast("无法构建任何可编辑图元")
            return
        self._push_history("导入矢量图")  # 导入前快照（此时无该矢量层）→ 撤销=移除该层
        # 空画布 → 按 viewBox/width-height 设画布尺寸（与 import_image 一致）
        if self.canvas_size is None:
            cw, ch = self._svg_canvas_size(meta, pairs)
            self.canvas_size = (cw, ch)
            self.scene.setSceneRect(0, 0, cw, ch)
        base_z = len(self.layers)
        items = []
        for it, _ve in pairs:
            it.setZValue(base_z)  # 整组叠在现有栅格层之上（B1 不交错）
            self.scene.addItem(it)
            self._wire_vec_item(it)  # B3：挂拖动入撤销回调（含 group 子项递归）
            items.append(it)
        self._layer_uid += 1
        layer = {
            "name": f"矢量 {len(self.layers) + 1}", "kind": "vector",
            "items": items, "pairs": pairs, "velems": velems, "meta": meta,
            "item": items[0] if items else None,  # 哨兵：满足现有 l['item'] 读取处（轮廓/激活仅用其 pos/bbox）
            "visible": True, "locked": False, "uid": self._layer_uid, "group": None,
            "svg_path": path,
        }
        self.layers.append(layer)
        self._set_active(layer)
        self.set_tool("move")
        self.fit_view()
        # fail-loud：报元素数 + 跳过数；含路径化文字则提示不可改字（§0.2）
        n_top = len(items)
        outlined = self._count_outlined_text(velems)
        n_text = self._count_text(velems)
        msg = f"导入 SVG：{n_top} 个顶层元素"
        if skipped:
            msg += f"，跳过/只读 {len(skipped)} 个不支持特性（filter/mask/use 等已只读渲染）"
        if outlined:
            msg += f"；含 {outlined} 处路径化文字·不可改字（请用 svg.fonttype='none' 重导出）"
        elif n_text == 0:  # §0.2：无可编辑 <text>，文字多半已路径化 → 教用户重导出
            msg += "；未发现可编辑 <text>，文字可能已被路径化(不可改字)——如需改字请用 svg.fonttype='none' 重新导出"
        self.op_label.setText(msg)

    def export_svg(self, path: str | None = None):
        """导出所有矢量层 → SVG（lxml 序列化，保 <text>/<g>/style；绝不用 QSvgGenerator）。

        导出前把每个 item 的实时几何/样式回灌进 VElem（move 工具改的是 pos，必须读回）。
        B1 不导出栅格层为 <image>（混合导出属 B3）；含栅格层则 fail-loud 提示。
        path 非空时跳过文件对话框（供离屏测试）。
        """
        vlayers = self._vector_layers()
        if not vlayers:
            self._toast("当前没有矢量层，请先导入 SVG")
            return
        if not path:
            name = f"{getattr(self, 'source_name', None) or 'figure'}.svg"
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "导出 SVG", self._save_start("export", name), "SVG (*.svg)")
            if not path:
                return
        self._remember_dir("export", path)
        # 多个矢量层 → 各层 velems 顺序拼接（z 顺序=层顺序）；meta 取第一层
        all_velems = []
        meta = None
        for layer in vlayers:
            svg_io.sync_items_to_velems(layer["pairs"])  # 视图状态回灌模型
            all_velems.extend(layer["velems"])
            if meta is None:
                meta = layer.get("meta")
        if meta is None and self.canvas_size:
            meta = {"width": self.canvas_size[0], "height": self.canvas_size[1],
                    "viewBox": f"0 0 {self.canvas_size[0]} {self.canvas_size[1]}"}
        dropped = []
        try:
            s = svg_io.serialize_svg(all_velems, meta, dropped=dropped)
            with open(path, "w", encoding="utf-8") as f:
                f.write(s)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "导出 SVG", f"序列化/写盘失败：{e}")
            return
        n_raster = sum(1 for l in self.layers if l.get("kind") != "vector")
        msg = f"导出 SVG：{len(vlayers)} 个矢量层 → {path}"
        if n_raster:  # fail-loud：栅格层未含
            msg += f"（本次仅导出矢量层，{n_raster} 个栅格层未含 · 位图嵌入待 B3）"
        if dropped:  # fail-loud：只读元素无法重序列化（救回失败）
            msg += f"；{len(dropped)} 个只读元素无法重序列化已丢弃"
        self.op_label.setText(msg)

    def export_pdf(self, path: str | None = None):
        """导出整张画布 → 混合矢量 PDF（QPdfWriter + scene.render）。

        矢量层(QGraphicsPathItem/QGraphicsTextItem) → PDF 内容流路径算子（放大不糊）；
        栅格层(ImageLayerItem) → 按 300 DPI 嵌为 /Image XObject。
        注意：本构建下 Qt PDF 引擎把文字字形描成矢量轮廓（仍矢量、放大不糊），
        但【不可选中/不可搜索/不可在 Illustrator 改字】——要可编辑文本请用「导出 SVG…」。
        path 非空时跳过文件对话框（供离屏测试）。
        """
        # 空画布 fail-loud（与 export_png/export_tiff 同款文案与行为）
        if self.canvas_size is None or not self.layers:
            self._toast("画布为空")
            return
        if not path:
            name = f"{getattr(self, 'source_name', None) or 'figure'}.pdf"
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "导出 PDF（矢量）", self._save_start("export", name), "PDF (*.pdf)")
            if not path:
                return
        self._remember_dir("export", path)

        cw, ch = self.canvas_size
        DPI = float(getattr(self, "source_dpi", None) or 300.0)  # 与 _apply_export_dpi 同源
        # 像素→点：1 像素 = 1/DPI 英寸 = 72/DPI 点（300 DPI 下 1px=0.24pt）
        pts_w = cw / DPI * 72.0
        pts_h = ch / DPI * 72.0

        # ---- chrome 隐藏（scene 子项级；view 级 chrome 天然不进 scene.render）----
        hidden = []
        for it in (self._outline, self._resize_handle, self._ants, self._ants_base,
                   self._sel_preview, self._brush_preview):
            if it is not None and it.isVisible():
                it.setVisible(False)
                hidden.append(it)
        for it in self._b5_overlay_items():  # B5：锚点 overlay / 钢笔预览不在 self.layers，必须显式隐藏（否则进 PDF）
            if it.isVisible():
                it.setVisible(False)
                hidden.append(it)
        sel = self.scene.selectedItems()  # 矢量层选中虚框：渲染前清，finally 复原
        self.scene.clearSelection()
        painter = None
        try:
            pw = QtGui.QPdfWriter(path)
            pw.setResolution(int(DPI))  # 铁律：必须在构造 QPainter(pw) 之前
            from PySide6.QtGui import QPageSize
            pw.setPageSize(QPageSize(QtCore.QSizeF(pts_w, pts_h), QPageSize.Unit.Point))
            pw.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))  # 零边距，否则画布被压进可打印区→缩放/偏移
            pw.setPdfVersion(QtGui.QPdfWriter.PdfVersion.PdfVersion_1_6)
            pw.setCreator("SciEdit")
            pw.setTitle(getattr(self, "source_name", None) or "figure")
            painter = QtGui.QPainter(pw)
            if not painter.isActive():  # 页面尺寸/路径异常致 begin 失败 → fail-loud（finally 仍复原 chrome）
                QtWidgets.QMessageBox.warning(self, "导出 PDF", "无法初始化 PDF 画笔（页面尺寸或路径异常）")
                return
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            # @DPI 且页面=cw/DPI*72pt 时 pw.width()/height()==cw/ch（设备像素），与 source 1:1 不变形
            target = QtCore.QRectF(0, 0, pw.width(), pw.height())
            self.scene.render(painter, target, QtCore.QRectF(0, 0, cw, ch))
            painter.end()  # 触发写盘
        finally:
            if painter is not None and painter.isActive():  # 异常路径也必须 end，否则活 QPainter 在 teardown 段错误(crash)
                painter.end()
            for it in hidden:
                it.setVisible(True)
            for it in sel:  # 复原交互选择态（不留进 PDF，但保留画布交互）
                it.setSelected(True)

        # ---- fail-loud：QPdfWriter 不抛异常也可能写空文件（盘满/不可写）----
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                head = f.read(5)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "导出 PDF", f"写盘失败：{e}")
            return
        if size == 0 or not head.startswith(b"%PDF"):
            QtWidgets.QMessageBox.warning(self, "导出 PDF", f"导出失败：文件为空或非法 PDF（{size} 字节）")
            return
        n_raster = sum(1 for l in self.layers if l.get("kind") != "vector")
        nvec = len(self._vector_layers())
        self.op_label.setText(
            f"已导出矢量 PDF · 矢量层 {nvec} 保矢量(文字转轮廓·不可搜索/改字,要可编辑文本用导出SVG)"
            f"·栅格层 {n_raster} 嵌入·{int(DPI)}DPI · {cw}×{ch} → {path}")

    # ---------- 矢量元素级编辑（B2 改色/配色/改字 + B3 拖动/打组/解组；均入撤销）----------
    def _build_vec_panel(self, vl):
        """矢量属性面板：path 的 fill/stroke/stroke-width + text 的字体/字号/色 + Okabe-Ito 配色助手。

        改色直接改 item 的 pen/brush（VElem 为 SSOT，导出由 sync_items_to_velems 回灌，单向数据流，与 move 改 pos 一致）。
        """
        vl.addWidget(self._hint("用「移动」工具在画布上点选矢量元素（Ctrl/Shift 加选）。改色/改字/拖动/打组均可 Ctrl+Z 撤销。"))

        # --- 填充 / 描边（path）---
        self._vec_fill_btn = QtWidgets.QPushButton("填充色…")
        self._vec_fill_btn.setToolTip("改选中 path 的填充色")
        self._vec_fill_btn.clicked.connect(self._vec_pick_fill)
        self._vec_nofill_chk = QtWidgets.QCheckBox("无填充")
        self._vec_nofill_chk.setToolTip("勾选→选中 path 设为无填充 (NoBrush)")
        self._vec_nofill_chk.toggled.connect(self._vec_toggle_nofill)
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(self._vec_fill_btn, 1); frow.addWidget(self._vec_nofill_chk)
        vl.addLayout(frow)

        self._vec_stroke_btn = QtWidgets.QPushButton("描边色…")
        self._vec_stroke_btn.setToolTip("改选中 path 的描边色")
        self._vec_stroke_btn.clicked.connect(self._vec_pick_stroke)
        self._vec_nostroke_chk = QtWidgets.QCheckBox("无描边")
        self._vec_nostroke_chk.setToolTip("勾选→选中 path 设为无描边 (NoPen)")
        self._vec_nostroke_chk.toggled.connect(self._vec_toggle_nostroke)
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(self._vec_stroke_btn, 1); srow.addWidget(self._vec_nostroke_chk)
        vl.addLayout(srow)

        swrow = QtWidgets.QHBoxLayout()
        swrow.addWidget(QtWidgets.QLabel("描边宽"))
        self._vec_sw_spin = QtWidgets.QDoubleSpinBox()
        self._vec_sw_spin.setRange(0.0, 100.0); self._vec_sw_spin.setDecimals(2)
        self._vec_sw_spin.setSingleStep(0.5); self._vec_sw_spin.setValue(1.0)
        self._vec_sw_spin.setToolTip("选中 path 的描边宽度 (0–100)")
        self._vec_sw_spin.valueChanged.connect(self._vec_change_stroke_width)
        swrow.addWidget(self._vec_sw_spin, 1)
        vl.addLayout(swrow)

        # --- 文字（text）字体 / 字号 / 色（独立控件，不与栅格文字层 props 耦合）---
        vl.addWidget(self._hint("文字元素（来源 <text>）："))
        self._vec_font_combo = QtWidgets.QFontComboBox()
        self._vec_font_combo.setToolTip("选中矢量文字的字体（即时套用）")
        self._vec_font_combo.currentFontChanged.connect(lambda *_: self._vec_change_text_font())
        vl.addWidget(self._vec_font_combo)
        tfrow = QtWidgets.QHBoxLayout()
        tfrow.addWidget(QtWidgets.QLabel("字号"))
        self._vec_fontsize_spin = QtWidgets.QDoubleSpinBox()
        self._vec_fontsize_spin.setRange(1.0, 480.0); self._vec_fontsize_spin.setDecimals(1)
        self._vec_fontsize_spin.setValue(12.0)
        self._vec_fontsize_spin.setToolTip("选中矢量文字的字号 (pt)")
        self._vec_fontsize_spin.valueChanged.connect(lambda *_: self._vec_change_text_font())
        tfrow.addWidget(self._vec_fontsize_spin, 1)
        self._vec_textcolor_btn = QtWidgets.QPushButton("文字色…")
        self._vec_textcolor_btn.setToolTip("改选中矢量文字的颜色")
        self._vec_textcolor_btn.clicked.connect(self._vec_pick_text_color)
        tfrow.addWidget(self._vec_textcolor_btn)
        vl.addLayout(tfrow)

        # --- 配色助手（Okabe-Ito 色盲友好）---
        vl.addWidget(self._hint("配色助手（色盲友好色板）："))
        prow = QtWidgets.QHBoxLayout()
        self._vec_palette_combo = QtWidgets.QComboBox()
        self._vec_palette_combo.addItem("Okabe-Ito (8 色)", "okabe_ito")  # 留扩展位
        self._vec_palette_combo.setToolTip("色盲友好色板（当前仅 Okabe-Ito）")
        prow.addWidget(self._vec_palette_combo, 1)
        vl.addLayout(prow)
        b_map_sel = QtWidgets.QPushButton("映射选中元素 → 最近色")
        b_map_sel.setToolTip("把选中元素的 fill/stroke 映射到最近的色盲友好色")
        b_map_sel.clicked.connect(self._vec_map_palette_selected)
        vl.addWidget(b_map_sel)
        b_map_layer = QtWidgets.QPushButton("整层映射（当前矢量层）")
        b_map_layer.setToolTip("把 self.active 矢量层的全部 path/text 的 fill/stroke 映射到色盲友好色")
        b_map_layer.clicked.connect(self._vec_map_palette_layer)
        vl.addWidget(b_map_layer)

        # --- 元素打组 / 解组（B3：层内 QGraphicsItemGroup，导出 <g>；区别于图层面板的「图层打组」）---
        vl.addWidget(self._hint("元素打组（同层内 ≥2 个元素 → <g>；非图层打组）："))
        grow = QtWidgets.QHBoxLayout()
        self._vec_group_btn = QtWidgets.QPushButton("元素打组")
        self._vec_group_btn.setToolTip("把选中的 ≥2 个【同一矢量层】元素包成一个组（整体选/拖/对齐，导出 <g>）")
        self._vec_group_btn.clicked.connect(self.do_group_vec_elements)
        self._vec_ungroup_btn = QtWidgets.QPushButton("元素解组")
        self._vec_ungroup_btn.setToolTip("把选中的矢量元素组拆回独立元素")
        self._vec_ungroup_btn.clicked.connect(self.do_ungroup_vec_elements)
        grow.addWidget(self._vec_group_btn); grow.addWidget(self._vec_ungroup_btn)
        vl.addLayout(grow)

        vl.addStretch(1)
        self._vec_controls = [
            self._vec_fill_btn, self._vec_nofill_chk, self._vec_stroke_btn,
            self._vec_nostroke_chk, self._vec_sw_spin, self._vec_font_combo,
            self._vec_fontsize_spin, self._vec_textcolor_btn,
            self._vec_group_btn, self._vec_ungroup_btn,
        ]
        self._set_vec_controls_enabled(False)  # 启动无选中 → 置灰

    def _set_vec_controls_enabled(self, on: bool):
        for c in getattr(self, "_vec_controls", []):
            c.setEnabled(on)

    # ----- 选中元素收集 / 归属层 -----
    def _selected_velem_items(self) -> list:
        """scene.selectedItems() 里只取矢量元素（path/text），严格 isinstance 过滤（RISK-3：排除 ImageLayerItem）。"""
        from PySide6 import QtWidgets as _W
        out = []
        for it in self.scene.selectedItems():
            if isinstance(it, (_W.QGraphicsPathItem, _W.QGraphicsTextItem)):
                out.append(it)
        return out

    def _vector_layer_of_item(self, item):
        """该 item 属哪个矢量层：顶层 items 命中或递归 group 子项命中。找不到返回 None。"""
        from PySide6 import QtWidgets as _W

        def _in(it, container):
            for c in container:
                if c is it:
                    return True
                if isinstance(c, _W.QGraphicsItemGroup) and _in(it, c.childItems()):
                    return True
            return False

        for layer in self._vector_layers():
            if _in(item, layer.get("items", [])):
                return layer
        return None

    def _on_vec_selection_changed(self):
        # 矢量元素选中变化 → 回填矢量属性面板。守卫：撤销批量 removeItem / 回填触发时不重入（RISK-7）。
        if self._suspend_vec_sel:
            return
        if self.view._tool == "node":
            self._maybe_rebuild_node_overlay()  # 锚点工具：选中单 path → 重建 overlay 到新 target
            return
        if self.view._tool != "move" or not self._vector_layers():
            return  # 非 move 工具或无矢量层 → 不干扰栅格流程
        self._vec_live_pushed = False  # 选择变化 → 重置 spin 连改的历史合并标志（B3 RISK-4）
        items = self._selected_velem_items()
        if not items:
            self._set_vec_controls_enabled(False)
            self._last_vec_sel_empty = True
            return
        # 「空选中→非空选中」跃迁 → 把「矢量属性」面板顶到前台（对齐 PS/AI「选中对象即弹属性面板」，
        # 修「选了矢量元素但改色/改描边入口躺在被盖住的 tab 里、用户以为不能改色」）。仅在跃迁时顶，
        # 不每次 selectionChanged 都顶——否则矢量层内反复框选会抢 tab、用户手动切到图层被反复顶回。
        if getattr(self, "_last_vec_sel_empty", True) and hasattr(self, "_dw_vec"):
            try:
                if self._dw_vec.isClosed():
                    self._dw_vec.toggleView(True)
                self._dw_vec.setAsCurrentTab()
            except Exception:  # noqa: BLE001 —— 面板顶置失败不影响选中/回填
                pass
        self._last_vec_sel_empty = False
        self._suspend_vec_sel = True  # 回填控件 → 抑制其 valueChanged/toggled 回写选中 item
        try:
            self._set_vec_controls_enabled(True)
            paths = [it for it in items if isinstance(it, QtWidgets.QGraphicsPathItem)]
            texts = [it for it in items if isinstance(it, QtWidgets.QGraphicsTextItem)]
            self._refresh_vec_path_controls(paths)
            self._refresh_vec_text_controls(texts)
        finally:
            self._suspend_vec_sel = False

    def _refresh_vec_path_controls(self, paths):
        # 回填首个 path 的 fill/stroke/stroke-width；path 控件按是否有 path 选中启用
        has = bool(paths)
        for c in (self._vec_fill_btn, self._vec_nofill_chk, self._vec_stroke_btn,
                  self._vec_nostroke_chk, self._vec_sw_spin):
            c.setEnabled(has)
        if not has:
            return
        it = paths[0]
        brush = it.brush()
        nofill = brush.style() == QtCore.Qt.BrushStyle.NoBrush
        self._vec_nofill_chk.setChecked(nofill)
        self._set_vec_swatch(self._vec_fill_btn, None if nofill else brush.color())
        pen = it.pen()
        nostroke = pen.style() == QtCore.Qt.PenStyle.NoPen
        self._vec_nostroke_chk.setChecked(nostroke)
        self._set_vec_swatch(self._vec_stroke_btn, None if nostroke else pen.color())
        self._vec_sw_spin.setValue(pen.widthF() if not nostroke else self._vec_sw_spin.value())

    def _refresh_vec_text_controls(self, texts):
        has = bool(texts)
        for c in (self._vec_font_combo, self._vec_fontsize_spin, self._vec_textcolor_btn):
            c.setEnabled(has)
        if not has:
            return
        it = texts[0]
        font = it.font()
        self._vec_font_combo.setCurrentFont(font)
        if font.pointSizeF() > 0:
            self._vec_fontsize_spin.setValue(font.pointSizeF())
        self._set_vec_swatch(self._vec_textcolor_btn, it.defaultTextColor())

    @staticmethod
    def _set_vec_swatch(btn, color):
        # 色块按钮：有色→背景着色；None(无填充/无描边)→清样式显示占位文字
        if color is None or not color.isValid():
            btn.setStyleSheet("")
            return
        fg = "#000" if color.lightness() > 128 else "#fff"
        btn.setStyleSheet(_swatch_css(color.name(), fg))

    # ----- 改色（path）-----
    def _vec_target_paths(self):
        return [it for it in self._selected_velem_items()
                if isinstance(it, QtWidgets.QGraphicsPathItem)]

    def _vec_layer_locked_guard(self, items) -> bool:
        # 任一选中元素属锁定层 → fail-loud 拦截改色/改字（对齐 selection_plan ③）
        for it in items:
            lyr = self._vector_layer_of_item(it)
            if lyr is not None and lyr.get("locked"):
                self.op_label.setText("该矢量层已锁定，无法改色/改字（点图层锁图标解锁）")
                return True
        return False

    def _vec_pick_fill(self):
        if self._suspend_vec_sel:
            return
        paths = self._vec_target_paths()
        if not paths or self._vec_layer_locked_guard(paths):
            return
        cur = paths[0].brush().color()
        c = QtWidgets.QColorDialog.getColor(cur if cur.isValid() else QtGui.QColor("#000000"),
                                            self, "填充色")
        if not c.isValid():
            return
        self._push_history("矢量填充色")  # 改 item 前快照（B3 入撤销）
        for it in paths:
            it.setBrush(QtGui.QBrush(c))
        self._vec_nofill_chk.blockSignals(True); self._vec_nofill_chk.setChecked(False); self._vec_nofill_chk.blockSignals(False)
        self._set_vec_swatch(self._vec_fill_btn, c)
        self._note_vec_edit(f"已改 {len(paths)} 个元素的填充")

    def _vec_toggle_nofill(self, on: bool):
        if self._suspend_vec_sel:
            return
        paths = self._vec_target_paths()
        if not paths or self._vec_layer_locked_guard(paths):
            return
        self._push_history("矢量填充色")  # 改 item 前快照（B3 入撤销）
        if on:
            for it in paths:
                it.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
            self._set_vec_swatch(self._vec_fill_btn, None)
            self._note_vec_edit(f"已对 {len(paths)} 个元素设无填充")
        else:
            c = QtGui.QColor("#000000")
            for it in paths:
                it.setBrush(QtGui.QBrush(c))
            self._set_vec_swatch(self._vec_fill_btn, c)
            self._note_vec_edit(f"已对 {len(paths)} 个元素恢复填充（黑）")

    def _vec_pick_stroke(self):
        if self._suspend_vec_sel:
            return
        paths = self._vec_target_paths()
        if not paths or self._vec_layer_locked_guard(paths):
            return
        cur = paths[0].pen().color()
        c = QtWidgets.QColorDialog.getColor(cur if cur.isValid() else QtGui.QColor("#000000"),
                                            self, "描边色")
        if not c.isValid():
            return
        self._push_history("矢量描边")  # 改 item 前快照（B3 入撤销）
        for it in paths:
            pen = QtGui.QPen(it.pen())  # 保留原 widthF / cosmetic
            if pen.style() == QtCore.Qt.PenStyle.NoPen:
                pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
                pen.setWidthF(self._vec_sw_spin.value())
            pen.setColor(c)
            it.setPen(pen)
        self._vec_nostroke_chk.blockSignals(True); self._vec_nostroke_chk.setChecked(False); self._vec_nostroke_chk.blockSignals(False)
        self._set_vec_swatch(self._vec_stroke_btn, c)
        self._note_vec_edit(f"已改 {len(paths)} 个元素的描边")

    def _vec_toggle_nostroke(self, on: bool):
        if self._suspend_vec_sel:
            return
        paths = self._vec_target_paths()
        if not paths or self._vec_layer_locked_guard(paths):
            return
        self._push_history("矢量描边")  # 改 item 前快照（B3 入撤销）
        if on:
            for it in paths:
                it.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
            self._set_vec_swatch(self._vec_stroke_btn, None)
            self._note_vec_edit(f"已对 {len(paths)} 个元素设无描边")
        else:
            c = QtGui.QColor("#000000")
            for it in paths:
                pen = QtGui.QPen(it.pen())
                pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
                pen.setColor(c)
                pen.setWidthF(self._vec_sw_spin.value())
                it.setPen(pen)
            self._set_vec_swatch(self._vec_stroke_btn, c)
            self._note_vec_edit(f"已对 {len(paths)} 个元素恢复描边（黑）")

    def _vec_change_stroke_width(self, v: float):
        if self._suspend_vec_sel:
            return
        paths = self._vec_target_paths()
        if not paths or self._vec_layer_locked_guard(paths):
            return
        self._vec_live_push("矢量描边")  # 连续 valueChanged 本轮首次才 push（B3 RISK-4）
        n = 0
        for it in paths:
            pen = QtGui.QPen(it.pen())
            if pen.style() == QtCore.Qt.PenStyle.NoPen:
                continue  # 无描边的不动（避免凭空冒出黑边）
            pen.setWidthF(v)
            it.setPen(pen)
            n += 1
        if n:
            self._note_vec_edit(f"已改 {n} 个元素的描边宽 → {v:g}")

    # ----- 改字体 / 字号 / 色（text）-----
    def _vec_target_texts(self):
        return [it for it in self._selected_velem_items()
                if isinstance(it, QtWidgets.QGraphicsTextItem)]

    def _vec_change_text_font(self):
        if self._suspend_vec_sel:
            return
        texts = self._vec_target_texts()
        if not texts or self._vec_layer_locked_guard(texts):
            return
        self._vec_live_push("矢量文字")  # font/size 走 valueChanged，连改合并成一步（B3 RISK-4）
        fam = self._vec_font_combo.currentFont().family()
        size = self._vec_fontsize_spin.value()
        for it in texts:
            f = QtGui.QFont(it.font())
            f.setFamily(fam)
            f.setPointSizeF(size)
            it.setFont(f)
        self._note_vec_edit(f"已改 {len(texts)} 个文字的字体/字号")

    def _vec_pick_text_color(self):
        if self._suspend_vec_sel:
            return
        texts = self._vec_target_texts()
        if not texts or self._vec_layer_locked_guard(texts):
            return
        cur = texts[0].defaultTextColor()
        c = QtWidgets.QColorDialog.getColor(cur if cur.isValid() else QtGui.QColor("#000000"),
                                            self, "文字颜色")
        if not c.isValid():
            return
        self._push_history("矢量文字")  # 改 item 前快照（B3 入撤销）
        for it in texts:
            it.setDefaultTextColor(c)
        self._set_vec_swatch(self._vec_textcolor_btn, c)
        self._note_vec_edit(f"已改 {len(texts)} 个文字的颜色")

    # ----- 配色助手（Okabe-Ito）-----
    def _map_items_to_okabe(self, items):
        """对 path/text 列表把 fill/stroke/text-color 映射到最近 Okabe-Ito 色。
        返回 (n_fill, n_stroke, n_skip)：skip=NoFill/NoPen 跳过计数（fail-loud 上浮，对齐十二原则#12）。"""
        n_fill = n_stroke = n_skip = 0
        for it in items:
            if isinstance(it, QtWidgets.QGraphicsPathItem):
                brush = it.brush()
                if brush.style() == QtCore.Qt.BrushStyle.NoBrush:
                    n_skip += 1
                else:
                    nc = svg_io.nearest_okabe(brush.color().name())
                    if nc:
                        it.setBrush(QtGui.QBrush(QtGui.QColor(nc))); n_fill += 1
                    else:
                        n_skip += 1
                pen = it.pen()
                if pen.style() == QtCore.Qt.PenStyle.NoPen:
                    n_skip += 1
                else:
                    nc = svg_io.nearest_okabe(pen.color().name())
                    if nc:
                        np = QtGui.QPen(pen); np.setColor(QtGui.QColor(nc)); it.setPen(np); n_stroke += 1
                    else:
                        n_skip += 1
            elif isinstance(it, QtWidgets.QGraphicsTextItem):
                nc = svg_io.nearest_okabe(it.defaultTextColor().name())
                if nc:
                    it.setDefaultTextColor(QtGui.QColor(nc)); n_fill += 1
                else:
                    n_skip += 1
        return n_fill, n_stroke, n_skip

    def _vec_map_palette_selected(self):
        items = self._selected_velem_items()
        if not items:
            self.op_label.setText("配色助手：未选中任何矢量元素（请先用移动工具点选）")
            return
        if self._vec_layer_locked_guard(items):
            return
        self._push_history("配色助手")  # 改 item 前快照（B3 入撤销）
        nf, ns, nk = self._map_items_to_okabe(items)
        self._refresh_vec_after_palette()
        self._note_vec_edit(f"配色助手：映射 {nf} 个填充 + {ns} 个描边到 Okabe-Ito（{nk} 个 NoFill/NoPen 跳过）")

    def _vec_map_palette_layer(self):
        layer = self.active
        if layer is None or layer.get("kind") != "vector":
            self.op_label.setText("配色助手·整层：当前激活层不是矢量层（请先选中一个矢量层）")
            return
        if layer.get("locked"):
            self.op_label.setText("该矢量层已锁定，无法整层映射（点图层锁图标解锁）")
            return
        items = svg_io.iter_leaf_items(layer.get("items", []))
        if not items:
            self.op_label.setText("配色助手·整层：该层无可映射元素")
            return
        self._push_history("配色助手")  # 改 item 前快照（B3 入撤销）
        nf, ns, nk = self._map_items_to_okabe(items)
        self._refresh_vec_after_palette()
        self._note_vec_edit(f"配色助手·整层「{layer['name']}」：映射 {nf} 个填充 + {ns} 个描边（{nk} 个 NoFill/NoPen 跳过）")

    def _refresh_vec_after_palette(self):
        # 映射后回填面板色块（若有选中），不触发回写
        self._suspend_vec_sel = True
        try:
            paths = self._vec_target_paths()
            texts = self._vec_target_texts()
            self._refresh_vec_path_controls(paths)
            self._refresh_vec_text_controls(texts)
        finally:
            self._suspend_vec_sel = False

    # ----- 元素级打组 / 解组（B3：层内 QGraphicsItemGroup ↔ <g>；区别于图层级 do_group）-----
    def _vec_pair_index(self, layer, item):
        # 在 layer["pairs"] 顶层里找 item 的下标；找不到返回 -1。
        for i, (it, _ve) in enumerate(layer.get("pairs", [])):
            if it is item:
                return i
        return -1

    def do_group_vec_elements(self):
        """选中 ≥2 个【同一矢量层】顶层元素 → 包进一个 QGraphicsItemGroup，建对应 group VElem。入撤销。"""
        items = [it for it in self._selected_velem_items()]
        if len(items) < 2:
            self.op_label.setText("元素打组：请先用移动工具选中 ≥2 个矢量元素")
            return
        # 守卫：必须同一矢量层（跨层会破坏 layer items/velems 一一对应），且层未锁定
        owners = [self._vector_layer_of_item(it) for it in items]
        if any(o is None for o in owners) or len({id(o) for o in owners}) != 1:
            self.op_label.setText("元素打组：选中元素必须属于【同一个】矢量层")
            return
        layer = owners[0]
        if layer.get("locked"):
            self.op_label.setText("该矢量层已锁定，无法打组（点图层锁图标解锁）")
            return
        # 只对【顶层】元素打组（命中 layer.pairs 顶层）；选中的 group 子项不参与（避免破坏既有组）
        idxs = [self._vec_pair_index(layer, it) for it in items]
        if any(i < 0 for i in idxs):
            self.op_label.setText("元素打组：仅支持对顶层元素打组（请勿选中已在某组内的子元素）")
            return
        self._push_history("矢量打组")  # 改 item 树前快照
        pairs = layer["pairs"]
        picked = [pairs[i] for i in idxs]  # [(item, ve), ...]（按选中顺序）
        grp = svg_io.make_vector_group_item()
        self.scene.addItem(grp)
        for it, _ve in picked:
            grp.addToGroup(it)  # Qt 自动把子项 scene 坐标转 group 局部坐标，视觉不变
        grp.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        grp.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        grp.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        grp.setZValue(layer["item"].zValue() if layer.get("item") else 0)
        # RISK-3：group VElem.children 顺序必须与 grp.childItems() 顺序严格一致（sync/rebuild 都按 zip 对应）
        item_to_ve = {id(it): ve for it, ve in picked}
        ordered_kids = list(grp.childItems())
        children_ve = [item_to_ve[id(k)] for k in ordered_kids if id(k) in item_to_ve]
        group_ve = svg_io.VElem(type="group", children=children_ve)
        self._wire_vec_item(grp)  # 挂拖动入撤销（含已 addToGroup 的子项）
        # 维护 layer 三元数据：从顶层移除被组元素，追加 grp / group_ve
        remove_ids = {id(it) for it, _ve in picked}
        layer["pairs"] = [(it, ve) for it, ve in pairs if id(it) not in remove_ids]
        layer["pairs"].append((grp, group_ve))
        layer["items"] = [it for it, _ve in layer["pairs"]]
        layer["velems"] = [ve for _it, ve in layer["pairs"]]
        layer["item"] = layer["items"][0] if layer["items"] else None
        self._suspend_vec_sel = True
        try:
            self.scene.clearSelection()
            grp.setSelected(True)
        finally:
            self._suspend_vec_sel = False
        self._on_vec_selection_changed()
        self.op_label.setText(f"元素打组：{len(picked)} 个元素 → 1 组（导出 <g>）")

    def do_ungroup_vec_elements(self):
        """选中的矢量元素组（或其子项所属组）→ 拆回独立顶层元素。入撤销。"""
        groups = []
        for it in self.scene.selectedItems():
            g = it if isinstance(it, QtWidgets.QGraphicsItemGroup) else None
            if g is None:
                p = it.parentItem()
                if isinstance(p, QtWidgets.QGraphicsItemGroup):
                    g = p
            if g is not None and self._vector_layer_of_item(g) is not None and g not in groups:
                groups.append(g)
        if not groups:
            self.op_label.setText("元素解组：未选中任何矢量元素组")
            return
        # 锁定层拦截
        for g in groups:
            lyr = self._vector_layer_of_item(g)
            if lyr is not None and lyr.get("locked"):
                self.op_label.setText("该矢量层已锁定，无法解组（点图层锁图标解锁）")
                return
        self._push_history("矢量解组")  # 改 item 树前快照
        n_total = 0
        for grp in groups:
            layer = self._vector_layer_of_item(grp)
            gi = self._vec_pair_index(layer, grp)
            if gi < 0:
                continue  # 嵌套组子项（非顶层）暂不处理（MVP 只解顶层组）
            group_ve = layer["pairs"][gi][1]
            kids = list(grp.childItems())  # 与 group_ve.children 同序（打组时已对齐）
            new_pairs = []
            for child_item, child_ve in zip(kids, group_ve.children):
                grp.removeFromGroup(child_item)  # Qt 把 group transform 烘焙进子项 scene 坐标，视觉不变
                # removeFromGroup【后】子项 transform/pos 已含 group 矩阵 → 此刻 sync 才把组变换合进子 ve（解组烘焙）
                svg_io._sync_one(child_item, child_ve)
                child_item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
                child_item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                self._wire_vec_item(child_item)
                new_pairs.append((child_item, child_ve))
                n_total += 1
            self.scene.removeItem(grp)
            # 维护 layer 数据：用拆出的子项替换原 group 槽位
            pairs = list(layer["pairs"])
            pairs[gi:gi + 1] = new_pairs
            layer["pairs"] = pairs
            layer["items"] = [it for it, _ve in pairs]
            layer["velems"] = [ve for _it, ve in pairs]
            layer["item"] = layer["items"][0] if layer["items"] else None
        self._suspend_vec_sel = True
        try:
            self.scene.clearSelection()
        finally:
            self._suspend_vec_sel = False
        self._on_vec_selection_changed()
        self.op_label.setText(f"元素解组：拆出 {n_total} 个元素")

    # ========== B5 锚点工具 overlay（独立 scene item，绝不入 self.layers/快照）==========
    def _node_editable_path_target(self):
        """当前是否恰好选中【一个顶层、非组、可编辑】矢量 path item → 返回 (item, layer)，否则 (None, None)。

        RISK-6：组内 path / outlined_text / unsupported 一律不进锚点编辑（避免破坏 group children 对应）。
        """
        sel = [it for it in self.scene.selectedItems()
               if isinstance(it, QtWidgets.QGraphicsPathItem)
               and not isinstance(it, QtWidgets.QGraphicsItemGroup)]
        if len(sel) != 1:
            return None, None
        it = sel[0]
        layer = self._vector_layer_of_item(it)
        if layer is None or layer.get("locked"):
            return None, None
        if self._vec_pair_index(layer, it) < 0:  # 必须是顶层 pair（组内子项 index=-1）
            return None, None
        return it, layer

    def _enter_node_tool(self):
        # 进锚点工具：若已选中单个可编辑 path 则建 overlay，否则提示。
        it, layer = self._node_editable_path_target()
        if it is None:
            self._clear_node_overlay()
            self.op_label.setText("锚点工具：请先用「移动」选中【一个】矢量 path（非组/非文字），再编辑锚点")
            return
        self._build_node_overlay(it, layer)

    def _maybe_rebuild_node_overlay(self):
        # selectionChanged（node 工具下）：选中单 path → 重建到新 target；否则清。
        it, layer = self._node_editable_path_target()
        if it is None:
            self._clear_node_overlay()
            return
        if self._node_overlay is not None and self._node_overlay["target"] is it:
            return  # 已是当前 target，不重建（避免选择风暴）
        self._build_node_overlay(it, layer)

    def _build_node_overlay(self, path_item, layer):
        self._clear_node_overlay()
        subpaths = svg_io.path_to_anchors(path_item.path())
        # RISK-5：node 工具下禁整 item 拖动，让位给锚点拖动
        path_item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._node_overlay = {
            "items": [], "target": path_item, "layer": layer,
            "subpaths": subpaths, "sel": set(), "drag_pushed": False,
        }
        self._rebuild_node_overlay()
        n_anchors = sum(len(sp["anchors"]) for sp in subpaths)
        self.op_label.setText(f"锚点编辑：{len(subpaths)} 子路径 · {n_anchors} 锚点（拖锚移点·拖柄改曲率·Alt/双击加锚·选锚 Del 删）")

    def _rebuild_node_overlay(self):
        # 据 target 当前 subpaths 造全部小 item。RISK-7：增删 item 包 _suspend_vec_sel 防 selectionChanged 递归。
        ov = self._node_overlay
        if ov is None:
            return
        self._suspend_vec_sel = True
        try:
            for it in ov["items"]:
                self.scene.removeItem(it)
            ov["items"] = []
            target = ov["target"]
            for sp_i, sp in enumerate(ov["subpaths"]):
                anchors = sp["anchors"]
                for a_i, a in enumerate(anchors):
                    on_scene = target.mapToScene(QtCore.QPointF(a.on[0], a.on[1]))  # RISK-8：穿过 item transform
                    # 控制柄连线 + 圆点
                    for side, cval in (("cin", a.cin), ("cout", a.cout)):
                        if cval is None:
                            continue
                        c_scene = target.mapToScene(QtCore.QPointF(cval[0], cval[1]))
                        line = svg_io.make_ctrl_line()
                        line.setLine(on_scene.x(), on_scene.y(), c_scene.x(), c_scene.y())
                        self.scene.addItem(line)
                        ov["items"].append(line)
                        dot = svg_io.make_ctrl_handle(sp_i, a_i, side)
                        dot._suspend_cb = True
                        dot.setPos(c_scene)
                        dot._suspend_cb = False
                        dot._drag_cb = self._on_ctrl_handle_dragged
                        dot._release_cb = self._on_handle_released
                        self.scene.addItem(dot)
                        ov["items"].append(dot)
                    # 锚点方块（最后加 → 叠在柄之上好点选）
                    h = svg_io.make_anchor_handle(sp_i, a_i, a.corner)
                    h._suspend_cb = True
                    h.setPos(on_scene)
                    h._suspend_cb = False
                    h._drag_cb = self._on_anchor_handle_dragged
                    h._press_cb = self._on_anchor_handle_pressed
                    h._release_cb = self._on_handle_released
                    if (sp_i, a_i) in ov["sel"]:
                        svg_io._style_overlay_handle(h, a.corner, selected=True)
                    self.scene.addItem(h)
                    ov["items"].append(h)
        finally:
            self._suspend_vec_sel = False

    def _clear_node_overlay(self):
        ov = self._node_overlay
        if ov is None:
            return
        self._suspend_vec_sel = True
        try:
            for it in ov["items"]:
                self.scene.removeItem(it)
            target = ov.get("target")
            if target is not None and target.scene() is self.scene:
                # 恢复整 item 可拖（按所属层是否锁定）
                lyr = ov.get("layer")
                locked = bool(lyr.get("locked")) if lyr else False
                target.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
        finally:
            self._suspend_vec_sel = False
        self._node_overlay = None

    def _push_node_drag_history_once(self):
        # 一次拖动只 push 一次历史（仿 VectorPathItem._moved_this_drag），松手后由下次 press 复位。
        ov = self._node_overlay
        if ov is not None and not ov["drag_pushed"]:
            self._push_history("编辑锚点")
            ov["drag_pushed"] = True

    def _on_handle_released(self, handle):
        # 拖动松手：若本次确有拖动（drag_pushed）→ commit 回灌 VElem；复位标志供下次拖动。
        ov = self._node_overlay
        if ov is None:
            return
        if ov["drag_pushed"]:
            t = getattr(self, "_node_refresh_timer", None)   # 松手：先把节流尾帧强制落终值(target.path 到位)再 commit，
            if t is not None and t.isActive():               # 否则最后一次 move 落在 16ms 窗口、尾帧未触发就 commit，
                t.stop()                                     # 会把缺最后亚-16ms 位移的旧 path 灌进 VElem/撤销快照
            self._apply_node_refresh()
            self._commit_node_edit()
        ov["drag_pushed"] = False

    def _on_anchor_handle_pressed(self, handle):
        # 点锚点 → 选中（Shift 多选）；并复位本次拖动的历史合并标志。
        ov = self._node_overlay
        if ov is None:
            return
        ov["drag_pushed"] = False
        key = handle.data(1)
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:
            ov["sel"].symmetric_difference_update({key})
        else:
            ov["sel"] = {key}
        # 只刷新选中态着色（不全量重建，避免拖动中 target 丢失）
        for it in ov["items"]:
            if it.data(2) == "anchor":
                k = it.data(1)
                sp_i, a_i = k
                corner = ov["subpaths"][sp_i]["anchors"][a_i].corner
                svg_io._style_overlay_handle(it, corner, selected=(k in ov["sel"]))

    def _on_anchor_handle_dragged(self, handle, scene_pos):
        # 拖锚点：整体平移该 anchor 的 on + cin + cout（柄跟随）。改模型→重建 path→刷新 overlay 位置。
        ov = self._node_overlay
        if ov is None:
            return
        self._push_node_drag_history_once()
        sp_i, a_i = handle.data(1)
        target = ov["target"]
        local = target.mapFromScene(scene_pos)  # RISK-8：scene → item 局部
        a = ov["subpaths"][sp_i]["anchors"][a_i]
        dx, dy = local.x() - a.on[0], local.y() - a.on[1]
        a.on = (local.x(), local.y())
        if a.cin is not None:
            a.cin = (a.cin[0] + dx, a.cin[1] + dy)
        if a.cout is not None:
            a.cout = (a.cout[0] + dx, a.cout[1] + dy)
        self._throttled_node_refresh()

    def _on_ctrl_handle_dragged(self, handle, scene_pos):
        # 拖控制柄：改 Anchor.cin/cout。平滑点联动对侧柄共线反向；角点独立。
        ov = self._node_overlay
        if ov is None:
            return
        self._push_node_drag_history_once()
        sp_i, a_i = handle.data(1)
        side = handle.data(2).split(":", 1)[1]  # "cin"/"cout"
        target = ov["target"]
        local = target.mapFromScene(scene_pos)
        a = ov["subpaths"][sp_i]["anchors"][a_i]
        new_c = (local.x(), local.y())
        if side == "cin":
            a.cin = new_c
        else:
            a.cout = new_c
        if not a.corner:  # 平滑点：对侧柄镜像（关于 on 对称，保持各自原长度）
            other = "cout" if side == "cin" else "cin"
            ov_c = getattr(a, other)
            if ov_c is not None:
                vx, vy = new_c[0] - a.on[0], new_c[1] - a.on[1]
                vlen = (vx * vx + vy * vy) ** 0.5
                olen = ((ov_c[0] - a.on[0]) ** 2 + (ov_c[1] - a.on[1]) ** 2) ** 0.5
                if vlen > 1e-6:
                    mirror = (a.on[0] - vx / vlen * olen, a.on[1] - vy / vlen * olen)
                    if other == "cin":
                        a.cin = mirror
                    else:
                        a.cout = mirror
        self._throttled_node_refresh()

    def _apply_node_refresh(self):
        """把当前锚点模型重建成 path + 刷新 overlay 柄/连线（拖动节流的实际执行体）。"""
        ov = self._node_overlay
        if ov is None:
            return
        ov["target"].setPath(svg_io.anchors_to_path(ov["subpaths"]))
        self._refresh_node_overlay_positions()
        clk = getattr(self, "_node_drag_clock", None)
        if clk is not None:
            clk.restart()

    def _throttled_node_refresh(self):
        """锚点/控制柄拖动视觉刷新节流 ~16ms（模型 a.on/cin/cout 已在调用方每帧即时更新，语义不变；
        只把 O(锚点数) 的 setPath+overlay 重摆限到 ≤60fps，避免上百锚点复杂 path 拖锚发滞）。
        尾帧用 singleShot 兜底 → 松手后视觉一定到位（不需 release 钩子）。"""
        ov = self._node_overlay
        if ov is None:
            return
        clk = getattr(self, "_node_drag_clock", None)
        if clk is None:
            clk = QtCore.QElapsedTimer(); clk.start(); self._node_drag_clock = clk
            self._apply_node_refresh(); return
        if clk.elapsed() >= 16:
            self._apply_node_refresh()
        else:
            t = getattr(self, "_node_refresh_timer", None)
            if t is None:
                t = QtCore.QTimer(self.view); t.setSingleShot(True)
                t.timeout.connect(self._apply_node_refresh)
                self._node_refresh_timer = t
            if not t.isActive():
                t.start(16)

    def _refresh_node_overlay_positions(self):
        # 拖动中只更新已有 overlay item 的位置（不增删，避免悬空）。柄结构未变（增删锚才重建）。
        ov = self._node_overlay
        if ov is None:
            return
        target = ov["target"]
        for it in ov["items"]:
            kind = it.data(2)
            key = it.data(1)
            if key is None and not isinstance(it, QtWidgets.QGraphicsLineItem):
                continue
            if kind == "anchor":
                sp_i, a_i = key
                a = ov["subpaths"][sp_i]["anchors"][a_i]
                p = target.mapToScene(QtCore.QPointF(a.on[0], a.on[1]))
                it._suspend_cb = True; it.setPos(p); it._suspend_cb = False
            elif kind in ("ctrl:cin", "ctrl:cout"):
                sp_i, a_i = key
                a = ov["subpaths"][sp_i]["anchors"][a_i]
                cval = a.cin if kind.endswith("cin") else a.cout
                if cval is not None:
                    p = target.mapToScene(QtCore.QPointF(cval[0], cval[1]))
                    it._suspend_cb = True; it.setPos(p); it._suspend_cb = False
        self._refresh_ctrl_lines()

    def _refresh_ctrl_lines(self):
        # 重摆控制柄连线（on↔ctrl）。线 item 没存 key，按当前模型整体重画。
        ov = self._node_overlay
        if ov is None:
            return
        lines = [it for it in ov["items"] if isinstance(it, QtWidgets.QGraphicsLineItem)]
        target = ov["target"]
        segs = []
        for sp in ov["subpaths"]:
            for a in sp["anchors"]:
                on_s = target.mapToScene(QtCore.QPointF(a.on[0], a.on[1]))
                for cval in (a.cin, a.cout):
                    if cval is not None:
                        c_s = target.mapToScene(QtCore.QPointF(cval[0], cval[1]))
                        segs.append((on_s, c_s))
        for line, (p0, p1) in zip(lines, segs):
            line.setLine(p0.x(), p0.y(), p1.x(), p1.y())

    def _commit_node_edit(self):
        # 拖动松手 / 增删锚后：把 target 当前 qpath/pen/brush 回灌它的 VElem（sync_items_to_velems 含 ve.qpath）。
        ov = self._node_overlay
        if ov is None:
            return
        layer = ov["layer"]
        target = ov["target"]
        idx = self._vec_pair_index(layer, target)
        if idx >= 0:
            svg_io._sync_one(target, layer["pairs"][idx][1])

    def _node_click(self, scene_pos, alt):
        # node 工具下 Alt+点 path 段 → 加锚。
        ov = self._node_overlay
        if ov is None:
            return
        self._add_anchor_at(scene_pos)

    def _node_double_click(self, scene_pos):
        # 双击：命中 AnchorHandle → 切角点/平滑；否则在段上加锚（双击空段加锚）。
        ov = self._node_overlay
        if ov is None:
            return
        vt = self.view.transform()
        hit = None
        for it in self.scene.items(scene_pos, QtCore.Qt.ItemSelectionMode.IntersectsItemShape,
                                   QtCore.Qt.SortOrder.DescendingOrder, vt):
            if it.data(0) == svg_io.NODE_OVERLAY_TAG and it.data(2) == "anchor":
                hit = it
                break
        if hit is not None:
            self._toggle_anchor_corner(hit.data(1))
        else:
            self._add_anchor_at(scene_pos)

    def _add_anchor_at(self, scene_pos):
        # 在 target 离 scene_pos 最近的段上插一个锚（de Casteljau 分裂）。RISK-4：用细分采样找最近段+t。
        ov = self._node_overlay
        if ov is None:
            return
        target = ov["target"]
        local = target.mapFromScene(scene_pos)
        best = None  # (dist, sp_i, seg_k, t)
        for sp_i, sp in enumerate(ov["subpaths"]):
            anchors = sp["anchors"]
            n = len(anchors)
            seg_pairs = [(k, k + 1) for k in range(n - 1)]
            if sp.get("closed") and n >= 2:
                seg_pairs.append((n - 1, 0))  # 闭合段
            for (i0, i1) in seg_pairs:
                prev, cur = anchors[i0], anchors[i1]
                d, t = self._closest_on_seg(prev, cur, (local.x(), local.y()))
                if best is None or d < best[0]:
                    best = (d, sp_i, i0, i1, t)
        if best is None:
            self.op_label.setText("加锚：未找到可插入的路径段")
            return
        _d, sp_i, i0, i1, t = best
        # 命中阈值（屏幕 ~12px → 局部坐标）：太远不加（避免误触）
        zoom = max(self.view.current_zoom(), 1e-6)
        if _d > 12.0 / zoom:
            self.op_label.setText("加锚：请在路径线上 Alt/双击")
            return
        self._push_history("增加锚点")
        anchors = ov["subpaths"][sp_i]["anchors"]
        prev, cur = anchors[i0], anchors[i1]
        new_pc, new_anchor, new_cc = svg_io.split_segment(prev, cur, t)
        if new_pc is not None:
            prev.cout = new_pc
        if new_cc is not None:
            cur.cin = new_cc
        insert_at = i0 + 1 if i1 == i0 + 1 else len(anchors)  # 闭合段 (n-1,0) 插到末尾
        anchors.insert(insert_at, new_anchor)
        target.setPath(svg_io.anchors_to_path(ov["subpaths"]))
        self._commit_node_edit()
        self._rebuild_node_overlay()
        self.op_label.setText("已增加 1 个锚点")

    @staticmethod
    def _closest_on_seg(prev, cur, pt):
        # 段上最近点：32 等分采样找最近 t（够编辑精度，RISK-4 已知近似）。
        def at(t):
            if prev.cout is None and cur.cin is None:
                return (prev.on[0] + (cur.on[0] - prev.on[0]) * t,
                        prev.on[1] + (cur.on[1] - prev.on[1]) * t)
            p0 = prev.on
            p1 = prev.cout if prev.cout is not None else prev.on
            p2 = cur.cin if cur.cin is not None else cur.on
            p3 = cur.on
            mt = 1 - t
            x = mt**3 * p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
            y = mt**3 * p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
            return (x, y)
        best_d = None; best_t = 0.5
        for i in range(1, 32):
            t = i / 32.0
            x, y = at(t)
            d = ((x - pt[0]) ** 2 + (y - pt[1]) ** 2) ** 0.5
            if best_d is None or d < best_d:
                best_d = d; best_t = t
        step = 1 / 32.0  # 粗扫后在 best_t 邻域二分细化，收紧落点（审核 LOW·RISK-4）
        for _ in range(8):
            step *= 0.5
            for cand in (best_t - step, best_t + step):
                if 0.0 <= cand <= 1.0:
                    x, y = at(cand)
                    d = ((x - pt[0]) ** 2 + (y - pt[1]) ** 2) ** 0.5
                    if d < best_d:
                        best_d = d; best_t = cand
        return best_d, best_t

    def _toggle_anchor_corner(self, key):
        ov = self._node_overlay
        if ov is None:
            return
        sp_i, a_i = key
        a = ov["subpaths"][sp_i]["anchors"][a_i]
        self._push_history("切换锚点类型")
        a.corner = not a.corner
        if not a.corner and a.cin is not None and a.cout is not None:
            # 变平滑 → 两侧柄拉成共线（cout 方向对齐 cin→on→cout 的平均方向）
            inv = (a.on[0] - a.cin[0], a.on[1] - a.cin[1])
            outv = (a.cout[0] - a.on[0], a.cout[1] - a.on[1])
            avg = (inv[0] + outv[0], inv[1] + outv[1])
            al = (avg[0] ** 2 + avg[1] ** 2) ** 0.5
            if al > 1e-6:
                u = (avg[0] / al, avg[1] / al)
                lin = (inv[0] ** 2 + inv[1] ** 2) ** 0.5
                lout = (outv[0] ** 2 + outv[1] ** 2) ** 0.5
                a.cin = (a.on[0] - u[0] * lin, a.on[1] - u[1] * lin)
                a.cout = (a.on[0] + u[0] * lout, a.on[1] + u[1] * lout)
                ov["target"].setPath(svg_io.anchors_to_path(ov["subpaths"]))
        self._commit_node_edit()
        self._rebuild_node_overlay()
        self.op_label.setText("角点 ↔ 平滑点：已切换")

    def _delete_selected_anchors(self):
        # 选锚 Del：从 subpaths 删掉选中 Anchor（子路径剩 <2 → 整段删）。删后相邻段断柄简化为直线。
        ov = self._node_overlay
        if ov is None or not ov["sel"]:
            self.op_label.setText("删除锚点：请先点选要删的锚点")
            return
        self._push_history("删除锚点")
        sel = ov["sel"]
        n_del = 0
        new_subpaths = []
        for sp_i, sp in enumerate(ov["subpaths"]):
            anchors = sp["anchors"]
            keep = []
            for a_i, a in enumerate(anchors):
                if (sp_i, a_i) in sel:
                    n_del += 1
                    continue
                keep.append(a)
            # 删点处的相邻段交给 anchors_to_path 按保留的 cin/cout 重连（保留邻柄，不强制断直线）。
            if len(keep) >= 2:
                # 闭合子路径删到剩 <3 锚 → 降为开放（2 点闭合是退化的零面积"闭合线"，审核 LOW）
                new_subpaths.append({"anchors": keep, "closed": sp["closed"] and len(keep) >= 3})
            elif len(keep) == 1:
                new_subpaths.append({"anchors": keep, "closed": False})
            # len 0 → 整段删（不加入）
        ov["subpaths"] = new_subpaths
        ov["sel"] = set()
        if not new_subpaths or all(len(sp["anchors"]) < 2 for sp in new_subpaths):
            # 路径被删空/退化 → 删掉整个 path 元素（fail-loud 报数）
            self._delete_node_target_path()
            self.op_label.setText(f"删除 {n_del} 个锚点：路径已空，整条 path 元素已删除")
            return
        ov["target"].setPath(svg_io.anchors_to_path(new_subpaths))
        self._commit_node_edit()
        self._rebuild_node_overlay()
        self.op_label.setText(f"已删除 {n_del} 个锚点")

    def _delete_node_target_path(self):
        # 锚点删空 → 从其矢量层移除该 path（维护 pairs/items/velems），清 overlay。
        ov = self._node_overlay
        if ov is None:
            return
        layer = ov["layer"]
        target = ov["target"]
        idx = self._vec_pair_index(layer, target)
        self._clear_node_overlay()
        if idx >= 0:
            self.scene.removeItem(target)
            del layer["pairs"][idx]
            layer["items"] = [it for it, _ve in layer["pairs"]]
            layer["velems"] = [ve for _it, ve in layer["pairs"]]
            layer["item"] = layer["items"][0] if layer["items"] else None

    # ========== B5 钢笔工具（画新矢量路径）==========
    def _ensure_pen_state(self):
        if self._pen_state is None:
            rubber = svg_io.make_pen_preview_path()
            self.scene.addItem(rubber)
            self._pen_state = {"anchors": [], "preview_items": [rubber], "rubber": rubber,
                               "dots": [], "press_pos": None, "dragging": False}

    def _pen_press(self, scene_pos, alt):
        self._ensure_pen_state()
        st = self._pen_state
        # 点回起点（屏幕 ~10px）→ 闭合
        if st["anchors"]:
            first = st["anchors"][0]
            f_scene = QtCore.QPointF(first.on[0], first.on[1])
            zoom = max(self.view.current_zoom(), 1e-6)
            if (scene_pos - f_scene).manhattanLength() < 12.0 / zoom and len(st["anchors"]) >= 2:
                self._finish_pen(closed=True)
                return
        st["press_pos"] = scene_pos
        st["dragging"] = False
        # 先落一个角点（拖动时升级为平滑点）
        st["anchors"].append(svg_io.Anchor(on=(scene_pos.x(), scene_pos.y())))
        self._refresh_pen_preview(scene_pos)

    def _pen_drag_to(self, scene_pos):
        st = self._pen_state
        if st is None or st["press_pos"] is None or not st["anchors"]:
            return
        st["dragging"] = True
        a = st["anchors"][-1]
        # 拖动 → 该锚成平滑点：cout=拖到点，cin=对称点
        a.cout = (scene_pos.x(), scene_pos.y())
        a.cin = (2 * a.on[0] - scene_pos.x(), 2 * a.on[1] - scene_pos.y())
        a.corner = False
        self._refresh_pen_preview(scene_pos)

    def _pen_release(self, scene_pos):
        st = self._pen_state
        if st is None:
            return
        st["press_pos"] = None
        st["dragging"] = False
        self._refresh_pen_preview(scene_pos)

    def _pen_hover(self, scene_pos):
        if self._pen_state is None or not self._pen_state["anchors"]:
            return
        self._refresh_pen_preview(scene_pos)

    def _refresh_pen_preview(self, cursor_scene):
        st = self._pen_state
        if st is None:
            return
        # 已落锚组成的 path + 从末锚到光标的橡皮筋段
        subpaths = [{"anchors": list(st["anchors"]), "closed": False}]
        qp = svg_io.anchors_to_path(subpaths)
        if st["anchors"] and cursor_scene is not None:
            last = st["anchors"][-1]
            if last.cout is not None:
                qp.cubicTo(last.cout[0], last.cout[1], cursor_scene.x(), cursor_scene.y(),
                           cursor_scene.x(), cursor_scene.y())
            else:
                qp.lineTo(cursor_scene.x(), cursor_scene.y())
        st["rubber"].setPath(qp)
        # 锚点圆点（每次刷新重建一组；旧的先移除，st["dots"] 是其唯一引用清单，不混进 preview_items 防泄漏）
        for d in st["dots"]:
            if d.scene() is self.scene:
                self.scene.removeItem(d)
        st["dots"] = []
        for a in st["anchors"]:
            dot = svg_io.make_pen_anchor_dot()
            dot.setPos(QtCore.QPointF(a.on[0], a.on[1]))
            self.scene.addItem(dot)
            st["dots"].append(dot)

    def _pen_commit(self):
        # Enter / 双击 → 结束开放路径
        if self._pen_state is None or not self._pen_state["anchors"]:
            self._cancel_pen()
            return
        if len(self._pen_state["anchors"]) < 2:
            self.op_label.setText("钢笔：至少需要 2 个锚点才能成路径（Esc 取消）")
            return
        self._finish_pen(closed=False)

    def _cancel_pen(self):
        st = self._pen_state
        if st is None:
            return
        for it in st["preview_items"]:
            if it.scene() is self.scene:
                self.scene.removeItem(it)
        for d in st["dots"]:
            if d.scene() is self.scene:
                self.scene.removeItem(d)
        if st["rubber"].scene() is self.scene:
            self.scene.removeItem(st["rubber"])
        self._pen_state = None

    def _register_vec_path(self, ve, hist_label: str, name_fn, force_new: bool = False):
        """把一个 path/shape VElem 落地：当前 active 是【未锁定】矢量层→并入；否则新建 kind=vector 层。
        钢笔 / 形状 / 箭头共用这一条注册管线（DRY）。建前 push 历史 → 撤销=移除该元素/层。返回 (item, layer)。
        force_new=True：每个【独立成层】（形状用），使每个矩形/箭头都是单独对象，可被智能连接线分别连接。"""
        from editor_window import DEFAULT_CANVAS  # 空画布初始化用，仍定义在 editor_window 模块级（避免循环依赖）
        self._push_history(hist_label)
        target = None if force_new else (self.active if (self.active and self.active.get("kind") == "vector"
                                                         and not self.active.get("locked")) else None)
        it = svg_io._to_item(ve)
        if target is not None:
            it.setZValue(max((p[0].zValue() for p in target["pairs"]), default=len(self.layers)))  # 叠该层最上
            self.scene.addItem(it)
            self._wire_vec_item(it)
            target["pairs"].append((it, ve))
            target["items"] = [x for x, _ in target["pairs"]]
            target["velems"] = [v for _, v in target["pairs"]]
            target["item"] = target["items"][0]
            self._set_active(target)
            layer = target
        else:
            if self.canvas_size is None:
                self.canvas_size = DEFAULT_CANVAS
                self.scene.setSceneRect(0, 0, DEFAULT_CANVAS[0], DEFAULT_CANVAS[1])
            it.setZValue(len(self.layers))
            self.scene.addItem(it)
            self._wire_vec_item(it)
            self._layer_uid += 1
            cw, ch = self.canvas_size
            layer = {
                "name": name_fn(), "kind": "vector", "items": [it], "pairs": [(it, ve)], "velems": [ve],
                "meta": {"width": cw, "height": ch, "viewBox": f"0 0 {cw} {ch}"},
                "item": it, "visible": True, "locked": False, "uid": self._layer_uid,
                "group": None, "svg_path": None,
            }
            self.layers.append(layer)
            self._set_active(layer)
        self._refresh_layers()
        return it, layer

    def _finish_pen(self, closed: bool):
        st = self._pen_state
        if st is None or len(st["anchors"]) < 2:
            self._cancel_pen()
            return
        n = len(st["anchors"])
        qp = svg_io.anchors_to_path([{"anchors": list(st["anchors"]), "closed": closed}])
        self._cancel_pen()  # 先清预览（销毁所有 __pen_preview__ item）
        ve = svg_io.VElem(type="path", qpath=qp, fill=None, stroke="#000000", stroke_width=1.0)
        self._register_vec_path(
            ve, "钢笔新建路径",
            lambda: f"钢笔路径 {len([l for l in self.layers if l.get('kind') == 'vector']) + 1}")
        self.op_label.setText(f"钢笔新建{'闭合' if closed else '开放'}路径：{n} 个锚点")

    # ----- 形状工具：矩形 / 椭圆 / 直线 / 箭头（拖框画，落地为 path VElem，复用 _register_vec_path）-----
    _SHAPE_NAMES = {"sh_rect": "矩形", "sh_ellipse": "椭圆", "sh_line": "直线", "sh_arrow": "箭头"}

    def _shape_path(self, tool: str, p0: QtCore.QPointF, p1: QtCore.QPointF, constrain: bool):
        """按工具 + 起止点构造 QPainterPath（scene 坐标，与钢笔一致）。constrain(Shift)：方/圆 / 直线吸 45°。
        箭头：直线 + 箭头三角形【烘焙进同一条 path】（几何照搬 Qt Diagram Scene Example）。"""
        import math
        qp = QtGui.QPainterPath()
        if tool in ("sh_rect", "sh_ellipse"):
            if constrain:  # 正方形 / 正圆：以 p0 为角，向拖动方向取等边
                dx, dy = p1.x() - p0.x(), p1.y() - p0.y()
                s = min(abs(dx), abs(dy))
                p1 = QtCore.QPointF(p0.x() + (s if dx >= 0 else -s), p0.y() + (s if dy >= 0 else -s))
            r = QtCore.QRectF(p0, p1).normalized()
            qp.addRect(r) if tool == "sh_rect" else qp.addEllipse(r)
            return qp
        # 直线 / 箭头
        e = QtCore.QPointF(p1)
        if constrain:  # 吸 0/45/90°
            dx, dy = p1.x() - p0.x(), p1.y() - p0.y()
            L = math.hypot(dx, dy)
            snap = math.radians(round(math.degrees(math.atan2(dy, dx)) / 45.0) * 45.0)
            e = QtCore.QPointF(p0.x() + L * math.cos(snap), p0.y() + L * math.sin(snap))
        qp.moveTo(p0); qp.lineTo(e)
        if tool == "sh_arrow":
            line = QtCore.QLineF(e, p0)  # 尖端→起点（与 Qt 示例一致的方向）
            L = line.length()
            if L >= 1.0:
                ang = math.acos(max(-1.0, min(1.0, line.dx() / L)))
                if line.dy() >= 0:
                    ang = (math.pi * 2.0) - ang
                size = max(8.0, min(22.0, L * 0.22))
                a1 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi / 3.0) * size,
                                                math.cos(ang + math.pi / 3.0) * size)
                a2 = line.p1() + QtCore.QPointF(math.sin(ang + math.pi - math.pi / 3.0) * size,
                                                math.cos(ang + math.pi - math.pi / 3.0) * size)
                qp.moveTo(e); qp.lineTo(a1); qp.lineTo(a2); qp.closeSubpath()  # 实心箭头三角形
        return qp

    def _shape_start(self, sp: QtCore.QPointF):
        self._shape_anchor_src = None
        if self.view._tool in ("sh_arrow", "sh_line"):  # 箭头/直线起点吸附到对象边中点(基础箭头也连边中心)
            sp = self._snap_to_anchor(sp)
            self._shape_anchor_src = self._anchor_object_at(sp)  # 起点落在某对象锚点上 → 记下,供两端都中时建跟随连线
        self._shape_p0 = sp; self._shape_p1 = sp
        self._start_preview()

    def _shape_move(self, sp: QtCore.QPointF):
        if getattr(self, "_shape_p0", None) is None or self._sel_preview is None:
            return
        constrain = bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if self.view._tool in ("sh_arrow", "sh_line") and not constrain:  # 箭头/直线终点吸附边中点；按 Shift 约束角度时不吸,免破坏 45°
            sp = self._snap_to_anchor(sp)
        self._shape_p1 = sp
        self._sel_preview.setPath(self._shape_path(self.view._tool, self._shape_p0, sp, constrain))

    def _shape_end(self):
        p0 = getattr(self, "_shape_p0", None)
        if p0 is None:
            return
        p1 = self._shape_p1; self._shape_p0 = None; self._remove_preview()
        tool = self.view._tool
        constrain = bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if QtCore.QLineF(p0, p1).length() < 3:  # 太小=误点，忽略
            self._shape_anchor_src = None
            return
        # 箭头/直线两端的吸锚判定：两端都吸对象→跟随式连线；同对象→取消；否则落地静态形状
        if tool in ("sh_arrow", "sh_line"):
            src = getattr(self, "_shape_anchor_src", None)
            self._shape_anchor_src = None  # 先清，避免后续异常/分支串联到下次手势
            dst = self._anchor_object_at(p1)
            if src is not None and dst is not None:
                if src[0] is dst[0] and src[1] == dst[1]:   # 两端在同一对象/同一元素 → 取消（不画起终点重合的畸形形状）
                    self.op_label.setText("起点和终点在同一对象上，未建连线"); return
                if self._create_connector(src[0], src[1], dst[0], dst[1], arrow=(tool == "sh_arrow")) is not None:
                    self.op_label.setText("✓ 已建跟随式%s（连在边中心·移动自动跟随·右键改形状/颜色/删除）"
                                          % ("箭头" if tool == "sh_arrow" else "连线"))
                    return  # 两端绑定不同对象 → 不再落地静态形状
        qp = self._shape_path(tool, p0, p1, constrain)
        nm = self._SHAPE_NAMES.get(tool, "形状")
        fill = "#333333" if tool == "sh_arrow" else None  # 箭头三角形实心；矩形/椭圆/线=描边轮廓
        ve = svg_io.VElem(type="path", qpath=qp, fill=fill, stroke="#333333", stroke_width=2.0)
        self._register_vec_path(
            ve, f"画{nm}",
            lambda: f"{nm} {len([l for l in self.layers if l.get('kind') == 'vector']) + 1}",
            force_new=True)  # 每个形状独立成层 → 可被智能连接线分别连接（不再并进同一层）
        self.op_label.setText(f"已画{nm}（拖动可移动·右侧矢量属性改色/描边·Shift 约束方圆/角度）")

    # 智能连接线方法已抽到 editor_connectors.ConnectorsMixin（EditorWindow 继承之）。

    # ----- 矢量内部编辑结果上浮（B3 起已入历史，纯 op_label，无「不可撤销」提示）-----
    def _note_vec_edit(self, msg: str):
        self.op_label.setText(msg)

    def _vec_live_push(self, label: str):
        # 连续 valueChanged（描边宽/字号/配色 spin）本轮首次改动才 push 一次历史，合并狂发（B3 RISK-4）。
        # 选择变化 / 外部 push 时 _vec_live_pushed 复位 → 下一轮重新开节点。
        if not self._vec_live_pushed:
            self._push_history(label)
            self._vec_live_pushed = True

    def _sync_vec_layers(self):
        """把矢量 item 当前 transform/pos 回灌进各自 velem（变换后即时同步，供导出/撤销快照取到）。"""
        for l in self._vector_layers():
            svg_io.sync_items_to_velems(l.get("pairs", []))
