"""ExportMixin —— EditorWindow 的「导出 + 工程保存/加载」功能（从 editor_window.py 抽出，行为不变）。

位图导出（PNG/TIFF）、合成渲染、DPI 元数据、QImage↔base64、以及 .nanopro.json 工程的
保存/加载往返。本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：layers/canvas_size/
connectors/op_label/view/assets/source_dpi/source_name/_group_names/_group_seq/selection_mask/
_vector_layers/_save_start/_remember_dir/_last_dir/_restore/_push_history/_refresh_assets/
_refresh_layers/fit_view/_update_info/_mask_to_b64/_b64_to_mask 等，MRO 解析）。
PROJECT_VERSION 工程格式版本常量随 save_project/load_project 一并移到本模块（只有它们用）。
"""
from __future__ import annotations

import json
import time

from PySide6 import QtCore, QtGui, QtWidgets

import image_ops

PROJECT_VERSION = 3  # .nanopro.json 工程格式版本（v3 新增非破坏蒙版 mask_b64）；旧 v2 仍可加载（字段都 .get 兜底）


class ExportMixin:
    def _only_vector_visible(self) -> bool:
        """有矢量层、且没有任何可见的栅格层 → 位图导出(PNG/TIFF)会得到全透明空白。"""
        has_visible_raster = any(
            l.get("kind") != "vector" and l.get("item") is not None and l["item"].isVisible()
            for l in self.layers)
        return bool(self._vector_layers()) and not has_visible_raster

    def _render_composite(self):
        """底→顶合成所有【可见】层(含位置+缩放)到一张透明 QImage；空则 None。导出/魔棒取色共用。"""
        if not self.layers or not self.canvas_size:
            return None
        w, h = self.canvas_size
        out = QtGui.QImage(w, h, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(out)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        for layer in self.layers:
            if layer.get("kind") == "vector":
                continue  # 矢量层无 image，B1 不并进位图合成（混合栅格+矢量导出属 B3）
            item = layer["item"]
            if item.isVisible():
                p.save()
                p.translate(item.pos().x(), item.pos().y())
                p.scale(item.scale(), item.scale())
                p.setOpacity(float(layer.get("opacity", 1.0)))  # 否则 PNG/TIFF/PDF 忽略层不透明度，导出与画布不符
                p.drawImage(0, 0, image_ops.masked_qimage(layer["image"], layer.get("mask")))  # 应用非破坏蒙版(画布同源)
                p.restore()
        for c in getattr(self, "connectors", []):  # 智能连接线（scene 坐标=画布坐标）画进合成，导出不丢
            if c.isVisible():
                p.setPen(c.pen()); p.setBrush(c.brush())
                p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                p.drawPath(c.path())
        p.end()
        return out

    @staticmethod
    def _print_dpi_text(width_px: int) -> str:
        """按 W 像素在期刊单栏 8.5cm / 双栏 17.8cm 下印刷的有效 DPI 与达标度(目标 300 DPI)。"""
        def evl(cm):
            dpi = width_px / (cm / 2.54)
            tag = "达标" if dpi >= 300 else ("偏低" if dpi >= 200 else "不足")
            return f"{dpi:.0f}DPI {tag}"
        return f"印刷：单栏8.5cm={evl(8.5)} · 双栏17.8cm={evl(17.8)}"

    def _apply_export_dpi(self, img: QtGui.QImage):
        """给导出图写入 DPI 元数据：有源图 DPI 用源 DPI，否则按 300 DPI（投稿默认）。"""
        dpi = getattr(self, "source_dpi", None) or 300.0
        dpm = int(round(dpi / 0.0254))
        img.setDotsPerMeterX(dpm); img.setDotsPerMeterY(dpm)

    def export_png(self):
        if self._only_vector_visible():  # E：纯矢量层合成是全透明 → PNG 会空白却报成功，先拦下引导走 SVG
            QtWidgets.QMessageBox.information(self, "导出 PNG", "当前只有可见的矢量图层，PNG 会是空白。\n请改用「导出 SVG…」。")
            return
        out = self._render_composite()
        if out is None:
            QtWidgets.QMessageBox.information(self, "导出", "画布为空")
            return
        name = f"{getattr(self, 'source_name', None) or 'figure'}_edited.png"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 PNG", self._save_start("export", name), "PNG (*.png)")
        if not path:
            return
        self._remember_dir("export", path)
        t = time.perf_counter()
        self._apply_export_dpi(out)
        if not out.save(path, "PNG"):  # G：保存失败（路径/权限）不能静默报成功
            QtWidgets.QMessageBox.warning(self, "导出 PNG", "保存失败（请检查路径/权限）")
            return
        w, h = self.canvas_size
        nvec = len(self._vector_layers())
        extra = f" · {nvec} 个矢量层未含（请用「导出 SVG…」）" if nvec else ""
        self.op_label.setText(f"导出 PNG {w}×{h} · {(time.perf_counter() - t) * 1000:.0f} ms · {self._print_dpi_text(w)}{extra}")

    def export_tiff(self):
        # H13: 导出 TIFF（投稿期刊常要求）。Qt 原生支持 QImage.save(..,'TIFF')，无需手写 IFD。
        if self._only_vector_visible():  # E：纯矢量层 TIFF 同样会空白，先拦下
            QtWidgets.QMessageBox.information(self, "导出 TIFF", "当前只有可见的矢量图层，TIFF 会是空白。\n请改用「导出 SVG…」。")
            return
        out = self._render_composite()
        if out is None:
            QtWidgets.QMessageBox.information(self, "导出", "画布为空")
            return
        name = f"{getattr(self, 'source_name', None) or 'figure'}_edited.tiff"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 TIFF", self._save_start("export", name), "TIFF (*.tiff *.tif)")
        if not path:
            return
        self._remember_dir("export", path)
        self._apply_export_dpi(out)
        ok = out.save(path, "TIFF")
        w, h = self.canvas_size
        if ok:
            self.op_label.setText(f"导出 TIFF {w}×{h} · {self._print_dpi_text(w)}")
        else:
            QtWidgets.QMessageBox.warning(self, "导出 TIFF", "保存失败（请检查路径/权限）")

    @staticmethod
    def _qimage_to_b64(img: QtGui.QImage) -> str:
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba); buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG"); buf.close()
        return bytes(ba.toBase64()).decode("ascii")

    @staticmethod
    def _b64_to_qimage(s: str) -> QtGui.QImage:
        ba = QtCore.QByteArray.fromBase64(str(s).encode("ascii"))
        img = QtGui.QImage()
        img.loadFromData(ba)  # 不强制 PNG：自动按字节探测，兼容 AI 抠图返回的 JPEG/WEBP
        return img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)

    def save_project(self):
        if not self.layers:
            QtWidgets.QMessageBox.information(self, "保存工程", "画布为空，没有可保存的内容")
            return
        import json
        default = f"{self.source_name or 'project'}.nanopro.json"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存工程", self._save_start("project", default), "工程 (*.nanopro.json *.json)")
        if not path:
            return
        self._remember_dir("project", path)
        # 矢量层（kind='vector'）无 image，工程格式 .nanopro.json 是栅格层栈 → B1 跳过，fail-loud 报数（矢量入工程属 B3）
        raster = [l for l in self.layers if l.get("kind") != "vector"]
        nvec = len(self.layers) - len(raster)
        data = {
            "format": "sciedit-qt", "version": PROJECT_VERSION,
            "canvas": list(self.canvas_size) if self.canvas_size else None,
            "source_dpi": self.source_dpi, "source_name": self.source_name,
            "group_names": dict(self._group_names), "group_seq": self._group_seq,  # 组名/计数随工程持久化(否则加载后分组丢失)
            "layers": [{
                "name": l["name"], "kind": l["kind"], "visible": l["visible"],
                "locked": l.get("locked", False),
                "opacity": l.get("opacity", 1.0),
                "pos": [l["item"].pos().x(), l["item"].pos().y()],
                "scale": l["item"].scale(),
                "text": l.get("text"),
                "uid": l.get("uid"), "group": l.get("group"),  # 稳定 id + 所属组(否则加载后图层面板按 uid 寻址全部命中首层)
                "png_b64": self._qimage_to_b64(l["image"]),
                "mask_b64": self._mask_to_b64(l.get("mask")),  # 非破坏蒙版(灰度 PNG)；无蒙版=None
            } for l in raster],
            "assets": [self._qimage_to_b64(a) for a in self.assets],
            "guides_v": list(self.view._guides_v), "guides_h": list(self.view._guides_h),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "保存工程", f"保存失败：{e}")
            return
        msg = f"工程已保存：{len(raster)} 层 / {len(self.assets)} 素材 → {path}"
        if nvec:
            msg += f"（{nvec} 个矢量层未存入工程 · 请单独「导出 SVG…」，矢量入工程待 B3）"
        self.op_label.setText(msg)

    def load_project(self):
        import json
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "加载工程", self._last_dir("project"), "工程 (*.nanopro.json *.json);;所有文件 (*.*)")
        if not path:
            return
        self._remember_dir("project", path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            lds = data.get("layers", [])
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "加载工程", f"加载失败：{e}")
            return
        # F：先校验文件格式与画布，再动场景。_restore 一进去就 setSceneRect/批量重建，
        #   若 data 不是本工程格式（缺 format/canvas/layers），等崩在 setSceneRect 时场景已被拆毁 → 不可恢复。
        if not isinstance(data, dict) or data.get("format") != "sciedit-qt":
            QtWidgets.QMessageBox.warning(self, "加载工程", "不是有效的 SciEdit 工程文件（format 不符）")
            return
        ver = data.get("version", 0)  # L3：版本过新则拒绝（已写入的 version 字段从此有意义，而非死字段）
        if isinstance(ver, (int, float)) and ver > PROJECT_VERSION:
            QtWidgets.QMessageBox.warning(self, "加载工程",
                                          f"工程版本过新（v{int(ver)}），当前最高支持 v{PROJECT_VERSION}，请升级软件后再打开")
            return
        cv = data.get("canvas")
        if not (isinstance(cv, (list, tuple)) and len(cv) == 2
                and all(isinstance(n, (int, float)) and n > 0 for n in cv)):
            QtWidgets.QMessageBox.warning(self, "加载工程", "工程画布尺寸无效或缺失")
            return
        if not isinstance(lds, list):
            QtWidgets.QMessageBox.warning(self, "加载工程", "工程图层数据损坏")
            return
        # 构造 _restore 用的快照（复用从零重建逻辑），base64→QImage
        snap = {"canvas": (cv[0], cv[1]), "active": -1, "layers": [],
                "guides_v": list(data.get("guides_v", [])), "guides_h": list(data.get("guides_h", []))}
        # 旧工程（无 uid）补发新 uid——在快照阶段就分配好，_restore→_refresh_layers 才能按正确 uid 建面板；
        # 种子取文件内已有最大 uid，避免新发的与旧的相撞。
        _existing = [ld.get("uid") for ld in lds if isinstance(ld.get("uid"), int)]
        _next_uid = max(_existing) if _existing else 0
        skipped = 0
        for ld in lds:
            img = self._b64_to_qimage(ld.get("png_b64", ""))
            if img.isNull():
                skipped += 1; continue
            uid = ld.get("uid")
            if not isinstance(uid, int):
                _next_uid += 1; uid = _next_uid
            snap["layers"].append({
                "name": ld.get("name", "图层"), "kind": ld.get("kind", "image"),
                "visible": bool(ld.get("visible", True)), "locked": bool(ld.get("locked", False)),
                "opacity": float(ld.get("opacity", 1.0)),
                "z": len(snap["layers"]), "pos": ld.get("pos", [0, 0]),
                "scale": float(ld.get("scale", 1.0)), "image": img, "text": ld.get("text"),
                "uid": uid, "group": ld.get("group"),  # 随工程恢复稳定 id + 分组
                "mask": self._b64_to_mask(ld.get("mask_b64")),  # 非破坏蒙版(v3)；旧 v2 无此键→None
            })
        snap["active"] = len(snap["layers"]) - 1
        # 组名/计数在 _restore 前恢复（_restore→_refresh_layers 会按 group 渲染组头）
        gn = data.get("group_names")
        self._group_names = dict(gn) if isinstance(gn, dict) else {}
        gs = data.get("group_seq")
        self._group_seq = int(gs) if isinstance(gs, (int, float)) else 0
        self._push_history("打开工程")   # 加载前状态入历史 → Ctrl+Z 可回退
        try:
            self._restore(snap)    # 复用重建逻辑（uid 已在快照阶段分配好）
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "加载工程", f"重建场景失败：{e}")
            return
        self.assets = [im for im in (self._b64_to_qimage(a) for a in data.get("assets", [])) if not im.isNull()]
        self.source_dpi = data.get("source_dpi"); self.source_name = data.get("source_name")
        self._refresh_assets(); self.fit_view(); self._update_info()
        msg = f"工程已加载：{len(self.layers)} 层 / {len(self.assets)} 素材"
        if skipped:  # fail-loud：明确报告跳过数
            msg += f"（跳过 {skipped} 个无法解码的层）"
        self.op_label.setText(msg)
