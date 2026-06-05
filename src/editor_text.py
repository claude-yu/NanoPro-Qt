"""TextMixin —— EditorWindow 的「文字工具 + 画布内打字」功能（从 editor_window.py 抽出，行为不变）。

文字面板控件联动（加粗/细体互斥、字色、字号下拉同步、字体）、文字层位图渲染（自适应/定宽断行/
旋转）、命中测试与就地编辑（栅格文字层 + 矢量 <text> 内联改字）、画布内嵌 textarea 打字编辑器
（移植 nanopro-editor）。本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：layers/scene/
view/active/canvas_size/op_label/font_combo/fontsize_combo/fontrot_spin/bold_check/thin_check/
quick_font/text_color/text_color_btn/_suspend_text_live/_text_live_pushed/_suspend_history/
_suspend_fontsize_sync/_text_editor/_text_edit_layer/_text_scene_pos/_text_box_w/_vec_text_edit_before/
set_tool/_push_history/_pop_last_history_if/_add_layer/_set_active/_refresh_layers/_update_outline/
_vector_layers/_vector_layer_of_item/_note_vec_edit 等，MRO 解析）。
_SIZE_PRESETS（字号预设元组）随 _make_size_combo/_font_size 一并移到本 mixin 类体。
_swatch_css / DEFAULT_CANVAS / InlineTextEdit 仍在 editor_window.py（别处也用），用到处函数内惰性 import（避免循环依赖）。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

import theme


class TextMixin:
    # ---------- 文字 ----------
    def _on_bold_toggled(self, v: bool):
        if v and self.thin_check.isChecked():
            self.thin_check.setChecked(False)  # 互斥：加粗时取消细体
        self._text_live_update()

    def _on_thin_toggled(self, v: bool):
        if v and self.bold_check.isChecked():
            self.bold_check.setChecked(False)  # 互斥：细体时取消加粗
        self._text_live_update()

    def _pick_text_color(self):
        c = QtWidgets.QColorDialog.getColor(self.text_color, self, "文字颜色")
        if c.isValid():
            self.text_color = c
            self._refresh_text_color_btn()
            self._text_live_update()  # M6: 改色即时作用于选中文字层

    def _refresh_text_color_btn(self):
        from editor_window import _swatch_css
        fg = "#000" if self.text_color.lightness() > 128 else "#fff"
        css = _swatch_css(self.text_color.name(), fg)
        self.text_color_btn.setStyleSheet(css)
        if hasattr(self, "_text_color_btn_m"):  # 选项栏镜像色块同步当前文字色
            self._text_color_btn_m.setStyleSheet(css)

    # ----- 字号下拉（可下拉选预设 + 手输，PS 式）-----
    _SIZE_PRESETS = (8, 9, 10, 11, 12, 14, 16, 18, 24, 30, 36, 48, 60, 72, 96, 144, 288)

    def _make_size_combo(self) -> QtWidgets.QComboBox:
        c = QtWidgets.QComboBox(); c.setEditable(True)
        c.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for s in self._SIZE_PRESETS:
            c.addItem(str(s), s)
        c.setValidator(QtGui.QIntValidator(8, 480, c))  # 手输限 8–480 整数
        c.lineEdit().setMaxLength(3)
        c.setCurrentText("48")
        c.setToolTip("字号(px)，8–480；下拉选预设或直接手输")
        c.currentTextChanged.connect(self._sync_fontsize)
        c.lineEdit().editingFinished.connect(self._normalize_fontsize)
        return c

    def _font_size(self) -> int:
        try:
            v = int(round(float((self.fontsize_combo.currentText() or "48").strip())))
        except Exception:
            v = 48
        return max(8, min(480, v))

    def _sync_fontsize(self, text):
        # 面板/选项栏两个字号下拉互相同步 + 即时套用到选中文字层。打字途中不夹值（允许输入到 48）。
        if getattr(self, "_suspend_fontsize_sync", False):
            return
        self._suspend_fontsize_sync = True
        try:
            for c in (getattr(self, "fontsize_combo", None), getattr(self, "_fontsize_combo_m", None)):
                if c is not None and c.currentText() != text:
                    c.setCurrentText(text)
        finally:
            self._suspend_fontsize_sync = False
        self._text_live_update()

    def _normalize_fontsize(self):
        # 输入完成后把越界值夹回 8–480（途中不夹，避免打不出多位数）
        v = str(self._font_size())
        for c in (getattr(self, "fontsize_combo", None), getattr(self, "_fontsize_combo_m", None)):
            if c is not None and c.currentText() != v:
                c.blockSignals(True); c.setCurrentText(v); c.blockSignals(False)

    def _text_props_from_panel(self) -> dict:
        return {
            "text": "新文字",  # 文字内容在画布上就地打字（面板不再有「内容」框）；编辑时各层沿用自己的文字
            "family": self.font_combo.currentFont().family(),
            "size": self._font_size(),
            "color": self.text_color.name(),
            "bold": self.bold_check.isChecked(),
            "thin": self.thin_check.isChecked(),
            "rotation": self.fontrot_spin.value(),
        }

    def _load_text_to_panel(self, props: dict):
        self._suspend_text_live = True  # 回填面板不应触发即时重渲染
        try:
            self.font_combo.setCurrentFont(QtGui.QFont(props.get("family", "Arial")))
            self.fontsize_combo.setCurrentText(str(int(props.get("size", 48))))
            self.fontrot_spin.setValue(int(props.get("rotation", 0)))
            self.text_color = QtGui.QColor(props.get("color", "#000000"))
            self._refresh_text_color_btn()
            self.bold_check.setChecked(bool(props.get("bold", False)))
            self.thin_check.setChecked(bool(props.get("thin", False)))
        finally:
            self._suspend_text_live = False

    @staticmethod
    def _wrap_lines(text: str, fm: QtGui.QFontMetrics, max_w: int) -> list:
        """贪心断行到 max_w（移植 wrapParagraph）：空格断词、CJK 逐字回退；保留手动换行。"""
        out = []
        for para in text.split("\n"):
            words, token = [], ""
            for ch in para:
                if ch == " ":
                    if token:
                        words.append(token); token = ""
                    words.append(" ")
                elif ord(ch) > 0x2E7F:  # CJK 及以上 → 每字单独可断
                    if token:
                        words.append(token); token = ""
                    words.append(ch)
                else:
                    token += ch
            if token:
                words.append(token)
            cur = ""
            for wd in words:
                if wd != " " and fm.horizontalAdvance(wd) > max_w:  # 超宽单词(长串/窄框) → 逐字断，防溢出裁切
                    for ch in wd:
                        if not cur or fm.horizontalAdvance(cur + ch) <= max_w:
                            cur += ch
                        else:
                            out.append(cur.rstrip()); cur = ch
                    continue
                trial = cur + wd
                if not cur or fm.horizontalAdvance(trial) <= max_w:
                    cur = trial
                else:
                    out.append(cur.rstrip()); cur = "" if wd == " " else wd
            out.append(cur.rstrip())
        return out or [""]

    def _make_text_image(self, props: dict) -> QtGui.QImage:
        font = QtGui.QFont(props["family"], props["size"])
        font.setBold(bool(props.get("bold", False)))
        if props.get("thin") and not props.get("bold"):
            font.setWeight(QtGui.QFont.Weight.Light)  # 细体
        fm = QtGui.QFontMetrics(font)
        pad = 8
        box_w = props.get("boxW")
        if box_w:  # M7: 定宽文本框 → 贪心断行自动换行
            box_w = max(20, int(box_w))
            lines = self._wrap_lines(props["text"], fm, box_w)
            line_h = fm.lineSpacing()
            w = box_w + pad * 2
            h = max(1, len(lines) * line_h) + pad * 2
            img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(img)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
            p.setFont(font); p.setPen(QtGui.QColor(props["color"]))
            y = pad + fm.ascent()
            for ln in lines:
                p.drawText(pad, y, ln); y += line_h
            p.end()
        else:  # 自适应：按内容外接矩形
            flags = int(QtCore.Qt.AlignmentFlag.AlignLeft)
            rect = fm.boundingRect(QtCore.QRect(0, 0, 8000, 8000), flags, props["text"])
            w = max(1, rect.width() + pad * 2)
            h = max(1, rect.height() + pad * 2)
            img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(img)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
            p.setFont(font); p.setPen(QtGui.QColor(props["color"]))
            p.drawText(QtCore.QRect(pad, pad, w - 2 * pad, h - 2 * pad), flags, props["text"])
            p.end()
        rot = int(props.get("rotation", 0))
        if rot % 360 != 0:  # 旋转：整张文字图按角度旋转，bbox 自动扩展、透明填充
            img = img.transformed(QtGui.QTransform().rotate(rot), QtCore.Qt.TransformationMode.SmoothTransformation)
        return img

    def _text_layer_at(self, scene_pos: QtCore.QPointF):
        """命中测试：返回 scene_pos 处最上面的可见文字层，无则 None。"""
        for layer in reversed(self.layers):
            if layer.get("kind") == "text" and layer["item"].isVisible():
                local = layer["item"].mapFromScene(scene_pos)
                if 0 <= local.x() < layer["image"].width() and 0 <= local.y() < layer["image"].height():
                    return layer
        return None

    def _place_text(self, scene_pos: QtCore.QPointF):  # 文字工具点画布
        hit = self._text_layer_at(scene_pos)  # M9: 单击命中已有文字框 → 重编辑该层(对齐 app.js:1773-1786)；否则新建
        if hit is not None:
            self._open_text_editor(hit["item"].pos(), edit_layer=hit)
        else:
            self._open_text_editor(scene_pos)

    def _place_text_box(self, p0: QtCore.QPointF, p1: QtCore.QPointF):  # 文字工具拖框/单击
        dx = abs(p1.x() - p0.x())
        if dx < 16:  # 仅按框宽判定单击（对齐 app.js:2105-2106 boxW=rect.w; if boxW<16）：命中重编辑/否则默认新建
            self._place_text(p0)
            return
        # 拖框：定宽文本框（PPT 式自动重排，对齐 app.js:1787-1792）
        x0, y0 = min(p0.x(), p1.x()), min(p0.y(), p1.y())
        self._open_text_editor(QtCore.QPointF(x0, y0), box_w=int(max(40, dx)))

    def _edit_text_at(self, scene_pos: QtCore.QPointF):  # 移动工具双击文字层 → 就地重新编辑
        # B2：先看是否双击命中【矢量】text item（区分 editable_text / outlined_text），否则回退栅格文字层逻辑。
        if self._vector_layers() and self._try_edit_vector_text(scene_pos):
            return
        hit = self._text_layer_at(scene_pos)
        if hit is not None:
            self._open_text_editor(hit["item"].pos(), edit_layer=hit)
            return
        # 双击落在矢量 path（很可能是路径化文字的字形）却无可编辑 <text> → §0.2 提示，不静默无反应
        if self._vector_layers():
            vt = self.view.transform()
            for it in self.scene.items(scene_pos, QtCore.Qt.ItemSelectionMode.IntersectsItemShape,
                                       QtCore.Qt.SortOrder.DescendingOrder, vt):
                if isinstance(it, QtWidgets.QGraphicsPathItem) and self._vector_layer_of_item(it) is not None:
                    self.op_label.setText("此处文字可能已被路径化(<path>)，无法改字；如需改字请用 svg.fonttype='none' 重新导出 SVG")
                    return

    def _vector_text_item_at(self, scene_pos: QtCore.QPointF):
        # 命中处最上面的矢量 text item（QGraphicsTextItem）；非 text 的 path 不算（留给改色，不误报路径化文字）。
        vt = self.view.transform()  # 视图 device transform（ItemIgnoresTransformations 命中判定用）
        for it in self.scene.items(scene_pos, QtCore.Qt.ItemSelectionMode.IntersectsItemShape,
                                   QtCore.Qt.SortOrder.DescendingOrder, vt):
            if isinstance(it, QtWidgets.QGraphicsTextItem) and self._vector_layer_of_item(it) is not None:
                return it
        return None

    def _try_edit_vector_text(self, scene_pos: QtCore.QPointF) -> bool:
        """双击命中矢量 text → 按 data(0) 分流：editable_text 内联改字；outlined_text fail-loud。
        返回 True=已处理（命中 text item），False=未命中 text（交回栅格逻辑/改色）。"""
        it = self._vector_text_item_at(scene_pos)
        if it is None:
            return False
        kind = it.data(0)
        if kind == "outlined_text":  # §0.2：路径化文字不可改字，教重导出
            self.op_label.setText("该文字已被路径化(<path>)，不可改字。请用 svg.fonttype='none'(matplotlib) 重新导出 SVG 后再编辑")
            return True
        lyr = self._vector_layer_of_item(it)
        if lyr is not None and lyr.get("locked"):
            self.op_label.setText("该矢量层已锁定，无法改字（点图层锁图标解锁）")
            return True
        # editable_text（或未标记的 <text>）→ 先快照改动前状态（B3 入撤销），再进入内联编辑态
        self._vec_text_edit_before = it.toPlainText()  # 记进入前文本，供空编辑回退判定
        self._push_history("矢量改字")                  # 改 item 文本【前】push → Ctrl+Z 回到原文
        it.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextEditorInteraction)
        it._on_edit_done = self._on_vector_text_edit_done  # EditableTextItem.focusOutEvent 回调
        it.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        cur = it.textCursor(); cur.select(QtGui.QTextCursor.SelectionType.Document); it.setTextCursor(cur)
        self._note_vec_edit("内联改字中：改完点空白/按 Esc 完成")
        return True

    def _on_vector_text_edit_done(self, item):
        # EditableTextItem 失焦回调（已在子类里回 NoTextInteraction）→ 上浮一次 op_label（fail-loud 不静默）。
        # 进入编辑前已 _push_history（改动前快照）；若没真改字则丢掉那个空历史步（_try_edit_vector_text 记了进入前文本）。
        before = getattr(self, "_vec_text_edit_before", None)
        if before is not None and item.toPlainText() == before:
            self._pop_last_history_if("矢量改字")  # 空编辑：撤掉冗余步（简洁优先）
        self._vec_text_edit_before = None
        self.op_label.setText("文字已修改（导出 <text> 文本将更新）")

    def _set_text_layer_image(self, layer: dict, img: QtGui.QImage):
        """给文字层换图并【保持视觉中心不动】——旋转/改字号导致 bbox 变化时不漂移（修旋转绕中心）。"""
        item = layer["item"]
        old = layer["image"]; s = item.scale()
        old_center = item.mapToScene(QtCore.QPointF(old.width() / 2.0, old.height() / 2.0))
        layer["image"] = img
        item.set_image(img)
        self._suspend_history = True
        item.setPos(old_center - QtCore.QPointF(img.width() / 2.0 * s, img.height() / 2.0 * s))
        self._suspend_history = False

    def _on_quick_font_changed(self, *_):  # 常用字体快捷 → 写入 font_combo（触发 _text_live_update 即时套用）
        family = self.quick_font.currentData()
        if family:
            self.font_combo.setCurrentFont(QtGui.QFont(family))
            self._text_live_update()  # setCurrentFont 在字体未变时不发信号，显式套用确保即时生效

    def _text_live_update(self):  # M6: 面板字体/字号/旋转/颜色/字重改动 → 立即重渲染选中的文字层
        if self._suspend_text_live or not self.active or self.active.get("kind") != "text":
            return
        if not self._text_live_pushed:  # 本轮编辑首次改动记一次历史（避免每步都灌历史）
            self._push_history("文字样式"); self._text_live_pushed = True
        props = self._text_props_from_panel()
        cur = self.active.get("text", {})
        props["text"] = cur.get("text", props["text"])  # 文本沿用该层自己的内容
        props["boxW"] = cur.get("boxW")                 # 保留定宽框宽
        self._set_text_layer_image(self.active, self._make_text_image(props))
        self.active["text"] = props
        self._update_outline(); self._refresh_layers()

    # ----- 画布上就地打字（移植 nanopro-editor 的 textarea 内嵌编辑）-----
    def _open_text_editor(self, scene_pos, edit_layer=None, box_w=None):
        from editor_window import DEFAULT_CANVAS, InlineTextEdit
        self._close_text_editor()
        if self.canvas_size is None:
            self.canvas_size = DEFAULT_CANVAS
            self.scene.setSceneRect(0, 0, DEFAULT_CANVAS[0], DEFAULT_CANVAS[1])
        if edit_layer and edit_layer.get("text"):
            self._load_text_to_panel(edit_layer["text"])
            if box_w is None:
                box_w = edit_layer["text"].get("boxW")  # 重编辑沿用原框宽
        self._text_box_w = box_w  # 提交时写进 props（None=自适应）
        props = self._text_props_from_panel()
        self._text_edit_layer = edit_layer
        self._text_scene_pos = scene_pos
        ed = InlineTextEdit(self._commit_text, self._cancel_text, self.view.viewport())
        self._text_editor = ed
        z = max(0.05, self.view.current_zoom())
        f = QtGui.QFont(props["family"], max(6, int(round(props["size"] * z))))
        f.setBold(bool(props.get("bold", False)))
        if props.get("thin") and not props.get("bold"):
            f.setWeight(QtGui.QFont.Weight.Light)
        ed.setFont(f)
        ed.setTextColor(QtGui.QColor(props["color"]))
        ed.setStyleSheet(f"QTextEdit{{background:rgba(255,255,255,0.9); border:1px dashed {theme.colors()['accent']};}}")
        if edit_layer and edit_layer.get("text"):
            ed.setPlainText(edit_layer["text"].get("text", ""))
        ed.setPlaceholderText("输入文字… (Ctrl+Enter 完成 · Esc 取消)")
        vp = self.view.mapFromScene(scene_pos)
        bw = max(80, int(self._text_box_w * z)) if self._text_box_w else max(140, int(360 * z))  # 定宽框→编辑器同宽
        bh = max(36, int(round(props["size"] * 1.7 * z)))
        ed.setGeometry(int(vp.x()), int(vp.y()), bw, bh)
        ed.show(); ed.setFocus()
        self.set_tool("text")
        self.op_label.setText("就地打字：Ctrl+Enter 完成 · Esc 取消")

    def _commit_text(self):
        ed = self._text_editor
        if ed is None:
            return
        self._text_editor = None  # 防 focusOut 重入
        text = ed.toPlainText().strip()
        ed.deleteLater()
        edit_layer = self._text_edit_layer
        self._text_edit_layer = None
        if not text:
            return
        props = self._text_props_from_panel(); props["text"] = text
        props["boxW"] = self._text_box_w  # 定宽框宽（None=自适应）
        img = self._make_text_image(props)
        self._push_history("文字")
        if edit_layer and edit_layer in self.layers:
            self._set_text_layer_image(edit_layer, img); edit_layer["text"] = props  # 居中保持，旋转不漂移
            self._set_active(edit_layer)
        else:
            layer = self._add_layer(img, f"文字 {len(self.layers) + 1}", "text")
            layer["text"] = props
            self._suspend_history = True
            layer["item"].setPos(self._text_scene_pos)
            self._suspend_history = False
        self.set_tool("move")
        self.op_label.setText("文字已应用")

    def _cancel_text(self):
        ed = self._text_editor
        self._text_editor = None; self._text_edit_layer = None
        if ed:
            ed.deleteLater()

    def _close_text_editor(self):
        if self._text_editor is not None:
            ed = self._text_editor; self._text_editor = None
            ed.deleteLater()

    def _apply_text(self):
        if not self.active or self.active.get("kind") != "text":
            QtWidgets.QMessageBox.information(self, "提示", "请先双击选中一个文字层，再「应用到选中」")
            return
        self._push_history("文字")
        props = self._text_props_from_panel()
        cur = self.active.get("text", {})
        props["text"] = cur.get("text", props["text"])  # 保留该层自己的文字内容（只套样式，不改内容）
        props["boxW"] = cur.get("boxW")                 # 保留定宽框宽
        self._set_text_layer_image(self.active, self._make_text_image(props))  # 居中保持
        self.active["text"] = props
        self._update_outline()
        self._refresh_layers()
        self.op_label.setText("文字样式已应用")
