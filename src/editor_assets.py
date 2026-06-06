"""AssetsMixin —— EditorWindow 的「素材库」功能（从 editor_window.py 抽出，行为不变）。

两部分：
 1) 内存素材库（抠出/选区/整层加入 → asset_list 缩略图，放回画布、导出、清空）；
 2) 本地素材库（连接磁盘文件夹 → 分类树 + 缩略图懒解码、搜索、收藏/最近、拆分图标合集、
    按分类导出、拖/点放到画布）。
本 mixin 只含方法，全部操作 self.*（由 EditorWindow 提供：assets/asset_list/asset_count/
asset_tree/asset_fs_list/asset_fs_count/asset_search/_asset_cur_items/_asset_all_items/
_asset_filter/_asset_thumb/_thumb_timer/active/selection_mask/canvas_size/scene/layers/
op_label/_push_history/_add_layer/_refresh_assets/_refresh_layers/_crop_selection/set_tool/
fit_view/_update_info 等，MRO 解析）。
_ASSET_MARK（树节点角色，标记「收藏/最近」虚拟分类）随用它的方法一并移到本 mixin 类体。
"""
from __future__ import annotations

import os

from PySide6 import QtCore, QtGui, QtWidgets

import asset_lib
import config
import icons
import image_ops
import theme


class _AssetScanSignals(QtCore.QObject):
    done = QtCore.Signal(int, object, object, object)  # gen, node, all_items, err


class _AssetScanTask(QtCore.QRunnable):
    """后台线程里扫描素材根目录（asset_lib.scan_asset_tree 是纯文件 I/O，线程安全），
    扫完发信号回主线程填树——避免上万素材的目录遍历卡死启动（实测 20 万文件约 9s）。"""

    def __init__(self, gen: int, root: str, signals: "_AssetScanSignals"):
        super().__init__()
        self._gen = gen
        self._root = root
        self._signals = signals

    def run(self):
        try:
            node, all_items, err = asset_lib.scan_asset_tree(self._root)
        except Exception as e:  # 大声失败：异常也回主线程提示，不静默吞
            node, all_items, err = None, [], "扫描失败：%s" % e
        try:
            self._signals.done.emit(self._gen, node, all_items, err)
        except RuntimeError:
            pass  # 扫描中关窗等 → 信号源(QObject)已销毁，安全丢弃，不刷 traceback


class AssetsMixin:
    # ---------- 素材库 ----------
    def _notify(self, text: str):
        if hasattr(self, "_toast"):
            self._toast(text)
        else:
            self.op_label.setText(text)

    def _asset_rel_category(self, path: str) -> str:
        root = config.get_asset_dir()
        rel_dir = os.path.dirname(path)
        if root:
            try:
                rel_dir = os.path.relpath(os.path.dirname(path), root)
            except ValueError:
                pass
        if rel_dir in (".", ""):
            return "未分类"
        return rel_dir.replace("\\", " / ").replace("/", " / ")

    @staticmethod
    def _asset_short_name(name: str, limit: int = 18) -> str:
        stem, _ext = os.path.splitext(name)
        stem = stem or name
        if len(stem) <= limit:
            return stem
        keep = max(6, (limit - 1) // 2)
        return stem[:keep] + "…" + stem[-keep:]

    def _sync_preview_actions(self, enabled: bool, path: str = ""):
        self._asset_preview_path = path if enabled else ""
        for name in ("asset_preview_place", "asset_preview_fav"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(enabled)
        if enabled and hasattr(self, "asset_preview_fav"):
            is_fav = path in set(config.get_asset_favorites())
            color = theme.colors()["accent"] if is_fav else theme.colors()["muted"]
            self.asset_preview_fav.setText("")
            self.asset_preview_fav.setIcon(icons.tool_icon("star", color, 18))
            self.asset_preview_fav.setToolTip("取消收藏" if is_fav else "收藏")

    def _set_asset_preview_empty(self, title: str, meta: str):
        if all(hasattr(self, name) for name in ("asset_preview_thumb", "asset_preview_name", "asset_preview_meta")):
            self.asset_preview_thumb.setPixmap(self._asset_placeholder_icon(False).pixmap(QtCore.QSize(112, 112)))
            self.asset_preview_name.setText(title)
            self.asset_preview_meta.setText(meta)
            self.asset_preview_name.setToolTip("")
            self.asset_preview_meta.setToolTip("")
            if hasattr(self, "asset_preview_path"):
                self.asset_preview_path.setText("")
                self.asset_preview_path.setToolTip("")
        self._sync_preview_actions(False)

    def _asset_placeholder_icon(self, failed: bool = False) -> QtGui.QIcon:
        """轻量占位缩略图：素材懒解码前也不让网格出现空白卡片。"""
        size = 84
        if hasattr(self, "asset_fs_list"):
            size = max(48, self.asset_fs_list.iconSize().width())
        dpr = max(1.0, self.devicePixelRatioF())
        import theme
        c = theme.colors()
        key = ("failed" if failed else "idle", size, round(dpr, 2), c["thumb"], c["border"], c["muted"])
        cache = getattr(self, "_asset_placeholder_cache", {})
        if key in cache:
            return cache[key]

        pm = QtGui.QPixmap(int(round(size * dpr)), int(round(size * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(0.5, 0.5, size - 1, size - 1)
        p.setPen(QtGui.QPen(QtGui.QColor(c["border"]), 1))
        p.setBrush(QtGui.QColor(c["thumb"]))
        p.drawRoundedRect(r, 8, 8)
        pen = QtGui.QPen(QtGui.QColor(c["danger"] if failed else c["muted"]), 1.4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx, cy = size / 2.0, size / 2.0
        if failed:
            p.drawLine(QtCore.QPointF(cx - 10, cy - 10), QtCore.QPointF(cx + 10, cy + 10))
            p.drawLine(QtCore.QPointF(cx + 10, cy - 10), QtCore.QPointF(cx - 10, cy + 10))
        else:
            p.drawRoundedRect(QtCore.QRectF(cx - 15, cy - 13, 30, 26), 4, 4)
            p.drawLine(QtCore.QPointF(cx - 9, cy + 2), QtCore.QPointF(cx - 2, cy - 5))
            p.drawLine(QtCore.QPointF(cx - 2, cy - 5), QtCore.QPointF(cx + 10, cy + 7))
            p.drawEllipse(QtCore.QPointF(cx + 7, cy - 7), 2.2, 2.2)
        p.end()

        icon = QtGui.QIcon(pm)
        cache[key] = icon
        self._asset_placeholder_cache = cache
        return icon

    def add_to_assets(self):
        if self.selection_mask is not None and self.active:   # 有选区 → 加裁切的选区
            res = self._crop_selection()
            if res is None:
                self._notify("选区为空")
                return
            self.assets.append(self._trim_transparent_qimage(res[0]))  # 裁透明边 → 素材=真正的图
        elif self.active:                                     # 无选区 → 把整个选中图层加入（裁掉透明留白）
            self.assets.append(self._trim_transparent_qimage(self.active["image"].copy()))
        else:
            self._notify("请先选中一个图层，或用套索/矩形/魔棒取选区")
            return
        self._refresh_assets()
        self._notify(f"已加入素材库（共 {len(self.assets)} 个）")

    def _refresh_assets(self):
        self.asset_list.clear()
        for i, im in enumerate(self.assets):
            thumb = QtGui.QPixmap.fromImage(im).scaled(
                72, 72, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(thumb), "")
            it.setData(QtCore.Qt.ItemDataRole.UserRole, i)
            it.setSizeHint(QtCore.QSize(84, 84))
            self.asset_list.addItem(it)
        n = len(self.assets)
        self.asset_count.setText(str(n))
        if hasattr(self, "asset_tabbar"):  # 计数显示在「抠出素材 N」Tab 文案上（BioRender 式）
            self.asset_tabbar.setTabText(1, "抠出素材 %d" % n)

    def _asset_clicked(self, item):
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier):
            return
        i = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if i is None or not (0 <= i < len(self.assets)):
            return
        im = self.assets[i]
        if self.canvas_size is None:
            self.canvas_size = (im.width(), im.height())
            self.scene.setSceneRect(0, 0, im.width(), im.height())
        prev = len(self.layers)  # 用添加前计数算层叠 → 第一个素材 off=0（真正居中）
        self._push_history("放回素材")
        layer = self._add_layer(im.copy(), f"素材 {len(self.layers) + 1}", "paint")
        cw, ch = self.canvas_size
        scale = min(cw * 0.55 / max(1, im.width()), ch * 0.55 / max(1, im.height()), 1.0)  # 只缩不放至 ≤55% 画布
        off = 24 * (prev % 5)  # 轻微层叠，连点不完全重叠
        sw, sh = im.width() * scale, im.height() * scale
        self._suspend_history = True
        layer["item"].setScale(scale)
        # 居中 + 双向 clamp：保证整块落在画布内（对齐 features.js:423-424 clamp，防大素材被推出画布裁切）
        layer["item"].setPos(max(0.0, min((cw - sw) / 2 + off, cw - sw)),
                             max(0.0, min((ch - sh) / 2 + off, ch - sh)))
        self._suspend_history = False
        self.set_tool("move")
        self._notify("素材已放回画布")

    def _asset_menu(self, pos):  # 素材右键菜单：导出此素材 / 删除此素材（对齐 WebView exportItem/remove）
        it = self.asset_list.itemAt(pos)
        if it is None:
            return
        i = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if i is None or not (0 <= i < len(self.assets)):
            return
        menu = QtWidgets.QMenu(self)
        tc = theme.colors()
        section = QtGui.QAction("放置", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction(icons.tool_icon("move", tc["text"], 16), "放回画布", lambda: self._asset_clicked(it))
        menu.addSeparator()
        section = QtGui.QAction("文件", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction(icons.tool_icon("new_layer", tc["text"], 16), "导出此素材…", lambda: self._export_one_asset(i))
        menu.addSeparator()
        section = QtGui.QAction("危险操作", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction(icons.tool_icon("trash", tc["danger"], 16), "删除此素材", lambda: self._delete_asset(i))
        menu.exec(self.asset_list.mapToGlobal(pos))

    def _delete_selected_asset(self):  # Del 键：删除当前选中的素材
        it = self.asset_list.currentItem()
        if it is None:
            return
        i = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if i is not None and 0 <= i < len(self.assets):
            self._delete_asset(i)

    def _delete_asset(self, i: int):
        if 0 <= i < len(self.assets):
            self.assets.pop(i)
            self._refresh_assets()
            self._notify(f"已删除该素材（剩 {len(self.assets)} 个）")

    def _export_one_asset(self, i: int):
        if not (0 <= i < len(self.assets)):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出素材", f"asset_{i + 1}.png", "PNG (*.png)")
        if path:
            self.assets[i].save(path, "PNG")
            self._notify(f"已导出素材到 {path}")

    def export_assets(self):
        if not self.assets:
            self._notify("素材库为空")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择导出文件夹")
        if not folder:
            return
        n = 0
        for i, im in enumerate(self.assets):
            if im.save(f"{folder}/asset_{i + 1}.png", "PNG"):
                n += 1
        self._notify(f"已导出 {n}/{len(self.assets)} 个透明 PNG 到 {folder}")

    def clear_assets(self):
        if not self.assets:
            return
        if QtWidgets.QMessageBox.question(  # M24: 破坏性操作必须确认
                self, "清空素材库", f"确定清空素材库？将删除全部 {len(self.assets)} 个素材，不可撤销。",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.assets = []
        self._refresh_assets()
        self._notify("素材库已清空")

    # ---------- 本地素材库（连接文件夹 / 分类 / 拖到画布建层）----------
    def _connect_asset_dir(self):
        """选一个本地素材根目录 → 记住到 config → 扫描刷新分类与缩略图。"""
        start = config.get_asset_dir() or ""
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择本地素材文件夹（子文件夹=分类，顶层散图归「未分类」）", start)
        if not d:
            return
        config.set_asset_dir(d)
        self._load_asset_dir()

    def _load_asset_dir(self):
        """按已记住的素材目录【后台线程】扫描成分类树 → 窗口秒开，不卡（上万素材的遍历放工作线程）。
        扫描期间树里显「正在加载…」，扫完由 _on_asset_dir_scanned 在主线程填充。"""
        root = config.get_asset_dir()
        self.asset_tree.clear()
        self.asset_fs_list.clear()
        self._asset_cur_items = []; self._asset_all_items = []
        # 代际计数：扫描进行中又换目录 → 旧结果回来时 gen 不匹配，丢弃（防竞态覆盖）
        self._asset_scan_gen = getattr(self, "_asset_scan_gen", 0) + 1
        if not root:
            if hasattr(self, "asset_path_label"):
                self.asset_path_label.setText("未连接素材文件夹")
            if hasattr(self, "asset_fs_count"):
                self.asset_fs_count.setText("")
            self._set_asset_preview_empty("未连接素材库", "点击右上角齿轮连接素材文件夹")
            return
        busy = QtWidgets.QTreeWidgetItem(["正在加载素材库…"])
        busy.setIcon(0, icons.tool_icon("clock", theme.colors()["muted"], 16))
        busy.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)  # 占位，不可点
        self.asset_tree.addTopLevelItem(busy)
        self.op_label.setText("素材库加载中…（大库首次扫描需几秒，不影响其它操作）")
        if not hasattr(self, "_asset_scan_sig"):
            self._asset_scan_sig = _AssetScanSignals()
            self._asset_scan_sig.done.connect(self._on_asset_dir_scanned)
        QtCore.QThreadPool.globalInstance().start(
            _AssetScanTask(self._asset_scan_gen, root, self._asset_scan_sig))

    def _on_asset_dir_scanned(self, gen, node, all_items, err):
        """后台扫描完成（主线程）：gen 过期则丢弃；否则填分类树 + 选根节点显图。"""
        if gen != getattr(self, "_asset_scan_gen", 0):
            return  # 已被更新的一次加载取代，丢弃旧结果
        self.asset_tree.clear()
        if err:
            self._notify("素材库：%s" % err)
            self._set_asset_preview_empty("素材库加载失败", str(err))
            return
        if node is None:
            self._notify("素材库：该文件夹下没有图片素材")
            self._set_asset_preview_empty("没有图片素材", "请检查连接的文件夹或图片格式")
            return
        self._asset_all_items = all_items
        self._populate_asset_tree(node)
        self._expand_asset_tree_depth(2)
        top = self._first_asset_node_with_items()
        if top is not None:
            self.asset_tree.setCurrentItem(top)
            self._asset_cur_items = top.data(0, QtCore.Qt.ItemDataRole.UserRole) or []
            self._set_asset_path_label(top)
        self._refresh_asset_thumbs()
        if all_items:
            self._complete_guide("asset_library", "asset_guide")
        self._notify("素材库：%d 张，已按文件夹分类（点分类树浏览/搜索）" % len(all_items))

    _ASSET_MARK = QtCore.Qt.ItemDataRole.UserRole + 1  # 树节点角色：标记「收藏/最近」虚拟分类

    def _populate_asset_tree(self, node):
        """扫描树 → QTreeWidget。item 文本=「名（直属N）」，data 存该节点【直属】items。
        顶部加「收藏 / 最近使用」虚拟分类（点击时实时从 config 取，永远最新）。"""
        def add(parent, nd):
            label = "%s（%d）" % (nd["name"], len(nd["items"])) if nd["items"] else nd["name"]
            it = QtWidgets.QTreeWidgetItem([label])
            it.setData(0, QtCore.Qt.ItemDataRole.UserRole, nd["items"])
            (self.asset_tree.addTopLevelItem if parent is None else parent.addChild)(it)
            for ch in nd["children"]:
                add(it, ch)
            return it
        fav = QtWidgets.QTreeWidgetItem(["收藏"]); fav.setData(0, self._ASSET_MARK, "fav")
        fav.setIcon(0, icons.tool_icon("star", theme.colors()["accent"], 16))
        rec = QtWidgets.QTreeWidgetItem(["最近使用"]); rec.setData(0, self._ASSET_MARK, "recent")
        rec.setIcon(0, icons.tool_icon("clock", theme.colors()["muted"], 16))
        self.asset_tree.addTopLevelItem(fav); self.asset_tree.addTopLevelItem(rec)
        add(None, node).setExpanded(True)  # 根展开，露出一级分类

    def _expand_asset_tree_depth(self, depth: int):
        """默认展开到 BioRender 式浏览深度：顶层领域和二级主题直接可见，物理批次不暴露。"""
        def walk(item, level):
            if item is None:
                return
            item.setExpanded(level < depth)
            for i in range(item.childCount()):
                walk(item.child(i), level + 1)
        for i in range(self.asset_tree.topLevelItemCount()):
            walk(self.asset_tree.topLevelItem(i), 0)

    def _first_asset_node_with_items(self):
        def walk(item):
            if item is None:
                return None
            if not item.data(0, self._ASSET_MARK) and (item.data(0, QtCore.Qt.ItemDataRole.UserRole) or []):
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found is not None:
                    return found
            return None
        for i in range(self.asset_tree.topLevelItemCount()):
            found = walk(self.asset_tree.topLevelItem(i))
            if found is not None:
                return found
        return None

    def _asset_item_path(self, item):
        parts = []
        cur = item
        while cur is not None:
            mark = cur.data(0, self._ASSET_MARK)
            text = cur.text(0)
            if mark:
                return text
            if text:
                parts.append(text.split("（", 1)[0])
            cur = cur.parent()
        parts.reverse()
        if parts and parts[0].startswith("00_"):
            parts = parts[1:]
        return " / ".join(parts) if parts else "素材库"

    def _set_asset_path_label(self, item):
        if hasattr(self, "asset_path_label"):
            self.asset_path_label.setText(self._asset_item_path(item))

    def _items_from_paths(self, paths):
        import os
        return [{"file": os.path.basename(p), "path": p} for p in paths]

    def _on_asset_tree_click(self, item, _col=0):
        """点分类 → 只加载它的【直属】图（不递归）→ 每次只显几十张，海量库不卡。
        点「收藏/最近」虚拟分类 → 实时从 config 取对应素材。"""
        mark = item.data(0, self._ASSET_MARK)
        if mark == "fav":
            self._asset_cur_items = self._items_from_paths(config.get_asset_favorites())
        elif mark == "recent":
            self._asset_cur_items = self._items_from_paths(config.get_asset_recent())
        else:
            self._asset_cur_items = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or []
        self._set_asset_path_label(item)
        self.asset_search.blockSignals(True); self.asset_search.clear(); self.asset_search.blockSignals(False)
        self._asset_filter = ""
        self._refresh_asset_thumbs()

    def _split_montage_assets(self):
        """把当前分类里的「图标合集」大图批量按空白沟槽拆成单个图标 PNG → 输出到新文件夹。
        拆开后一图一图标，缩略图又大又清楚——根治“合集里每个图标太小看不清”。大声失败：报拆/跳/败计数。"""
        import os
        items = list(self._asset_cur_items or []) or list(self._asset_all_items or [])
        if not items:
            self._notify("当前分类没有素材，先连接素材文件夹并选择分类")
            return
        out = QtWidgets.QFileDialog.getExistingDirectory(self, "选择拆分结果的输出文件夹（会写入若干单个图标 PNG）")
        if not out:
            return
        prog = self._begin_progress("拆分合集", "正在分析当前分类素材", len(items))
        sheets = pieces = failed = skipped = 0
        try:
            for k, it in enumerate(items):
                if prog.wasCanceled():
                    break
                path = it.get("path")
                name = os.path.basename(path) if path else "未知文件"
                prog.step(k, name, failed)
                qi = QtGui.QImage(path) if path else QtGui.QImage()
                if qi.isNull():
                    failed += 1
                    prog.step(k + 1, name, failed)
                    continue
                qi = qi.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                try:
                    boxes = image_ops.split_montage(image_ops.qimage_to_rgba(qi))
                except Exception:
                    failed += 1
                    prog.step(k + 1, name, failed)
                    continue
                if len(boxes) <= 1:                       # 切不出多块=本就是单图 → 跳过(计数，不静默)
                    skipped += 1
                    prog.step(k + 1, name, failed)
                    continue
                sheets += 1
                stem = os.path.splitext(os.path.basename(path))[0]
                for idx, (x, y, w, h) in enumerate(boxes):
                    crop = qi.copy(int(x), int(y), int(w), int(h))
                    dst = os.path.join(out, "%s_%02d.png" % (stem, idx + 1))
                    if crop.save(dst, "PNG"):
                        pieces += 1
                    else:
                        failed += 1
                prog.step(k + 1, name, failed)
        finally:
            self._end_progress(prog)
        msg = ("拆分完成：\n  合集大图 %d 张 → 切出单个图标 %d 个\n"
               "  跳过(本就是单图，无需拆) %d 张\n  失败 %d\n  输出目录：%s"
               % (sheets, pieces, skipped, failed, out))
        if pieces and QtWidgets.QMessageBox.question(
                self, "拆分合集", msg + "\n\n是否立即连接这个文件夹作为素材库（即看拆分后的清晰图标）？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                ) == QtWidgets.QMessageBox.StandardButton.Yes:
            config.set_asset_dir(out); self._load_asset_dir()
        else:
            self._notify(msg.split("\n", 1)[0])

    def _trim_asset_folder(self):
        """把【当前分类】的素材批量裁掉四周透明留白 → 输出到新文件夹（非破坏，不动原图）。
        能裁的存紧致 PNG；无透明边/无 alpha 的原样复制 → 输出文件夹=一套裁紧的库。大声失败：报计数。"""
        import os, shutil
        items = list(self._asset_cur_items or []) or list(self._asset_all_items or [])
        if not items:
            self._notify("当前分类没有素材，先在分类树里点一个分类")
            return
        if len(items) > 3000 and QtWidgets.QMessageBox.question(
                self, "批量裁透明边",
                "当前视图有 %d 张，批量处理会较久且占磁盘。建议先点一个【子分类】缩小范围。\n仍要继续吗？" % len(items),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        out = QtWidgets.QFileDialog.getExistingDirectory(self, "选择裁剪结果的输出文件夹（非破坏，不改原图）")
        if not out:
            return
        prog = self._begin_progress("批量裁透明边", "正在处理当前分类素材", len(items))
        trimmed = same = failed = 0
        try:
            for k, it in enumerate(items):
                if prog.wasCanceled():
                    break
                path = it.get("path")
                name = os.path.basename(path) if path else "未知文件"
                prog.step(k, name, failed)
                qi = QtGui.QImage(path) if path else QtGui.QImage()
                if qi.isNull():
                    failed += 1
                    prog.step(k + 1, name, failed)
                    continue
                stem = os.path.splitext(os.path.basename(path))[0]
                qa = qi.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                try:
                    bb = image_ops.content_bbox(image_ops.qimage_to_rgba(qa))
                except Exception:
                    bb = None
                if bb is None or (bb[0] == 0 and bb[1] == 0 and bb[2] == qi.width() and bb[3] == qi.height()):
                    try:  # 无透明边/无 alpha → 原样复制（保格式不膨胀），让输出文件夹是【完整】一套库
                        shutil.copy2(path, os.path.join(out, os.path.basename(path)))
                        same += 1
                    except Exception:
                        failed += 1
                    prog.step(k + 1, name, failed)
                    continue
                x0, y0, x1, y1 = bb
                if qa.copy(x0, y0, x1 - x0, y1 - y0).save(os.path.join(out, stem + ".png"), "PNG"):
                    trimmed += 1
                else:
                    failed += 1
                prog.step(k + 1, name, failed)
        finally:
            self._end_progress(prog)
        msg = ("批量裁透明边完成：\n  裁紧 %d 张（透明边已去）\n  无需裁/无透明边 %d 张（原样复制）\n  失败 %d\n  输出：%s"
               % (trimmed, same, failed, out))
        if (trimmed + same) and QtWidgets.QMessageBox.question(
                self, "批量裁透明边", msg + "\n\n是否连接这个文件夹作为素材库（即用裁紧后的素材）？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                ) == QtWidgets.QMessageBox.StandardButton.Yes:
            config.set_asset_dir(out); self._load_asset_dir()
        else:
            self._notify(msg.split("\n", 1)[0])

    def _build_asset_manifest(self):
        """给当前素材根目录一键生成 manifest.json（按子文件夹分类，递归收深层图），再重扫刷新。"""
        root = config.get_asset_dir()
        if not root:
            self._notify("请先连接素材文件夹")
            return
        import style_lib
        self.op_label.setText("生成分类索引中…")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            nt, nf, err = style_lib.build_manifest(root)
        except Exception as e:  # 大声失败：异常不再被 Qt 静默吞掉(用户反馈"点了没反应")
            nt = nf = 0
            err = "生成失败：%s" % e
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if err:
            QtWidgets.QMessageBox.warning(self, "生成分类索引", "%s\n\n目录：%s" % (err, root))
            self.op_label.setText("生成分类索引失败：%s" % err)
            return
        self._load_asset_dir()  # 重扫：此时读到刚生成的 manifest
        msg = "已生成分类索引：%d 个分类 / %d 张图" % (nt, nf)
        if nt <= 1:  # 顶层平铺 → 只有一个「未分类」，明确告知怎么才能分类
            msg += "\n\n注意：只有 1 个分类——图都在顶层、没分到子文件夹。\n要分多类，请把图按主题放进不同子文件夹后再点一次。"
        self._notify(msg.split("\n")[0])
        if nt <= 1:
            self._notify("只有 1 个分类：请把素材放进子文件夹后再生成索引")

    def _export_assets_by_category(self):
        """把本地素材库按【分类】导出到选定目录：每个分类一个子文件夹、复制其中每张图。
        flat+manifest 的图包可借此物理拆成分类文件夹。重名文件追加 _2/_3… 不静默覆盖（大声失败）。"""
        root = config.get_asset_dir()
        groups, err = asset_lib.scan_assets(root) if root else ([], "未连接素材文件夹")
        if err or not groups:
            self._notify(err or "请先连接素材文件夹并确保里面有素材")
            return
        out = QtWidgets.QFileDialog.getExistingDirectory(self, "选择导出目标文件夹（将按分类建子文件夹）")
        if not out:
            return
        import os as _os
        import re as _re
        import shutil as _shutil
        n_ok = n_fail = 0
        for g in groups:
            safe = _re.sub(r'[\\/:*?"<>|]', "_", str(g["name"])).strip() or "未命名"
            sub = _os.path.join(out, safe)
            _os.makedirs(sub, exist_ok=True)
            for it in g["items"]:
                try:
                    base = _os.path.basename(it["path"])
                    dst = _os.path.join(sub, base)
                    if _os.path.exists(dst):  # 重名不覆盖：追加 _2/_3…
                        stem, ext = _os.path.splitext(base)
                        k = 2
                        while _os.path.exists(_os.path.join(sub, "%s_%d%s" % (stem, k, ext))):
                            k += 1
                        dst = _os.path.join(sub, "%s_%d%s" % (stem, k, ext))
                    _shutil.copy2(it["path"], dst)
                    n_ok += 1
                except Exception:
                    n_fail += 1
        msg = "按分类导出完成：%d 个分类 / %d 张图 → %s" % (len(groups), n_ok, out)
        if n_fail:
            msg += "；%d 张失败" % n_fail  # 大声失败：不把失败藏起来
        self._notify(msg)
        if n_fail:
            self._notify("按分类导出完成，但有 %d 张失败" % n_fail)

    def _refresh_asset_thumbs(self):
        """渲染：有搜索→全树过滤；否则→当前选中分类的【直属】图。只挂项不解码 + 懒解可见 → 不卡。"""
        lst = self.asset_fs_list
        lst.setUpdatesEnabled(False)
        lst.clear()
        flt = getattr(self, "_asset_filter", "")
        if flt:  # 搜索：文件名 + 路径(含分类文件夹名)都匹配 → 输入分类名也能搜出来
            items = [it for it in self._asset_all_items
                     if flt in it["file"].lower() or flt in it["path"].lower()]
        else:
            items = list(self._asset_cur_items or [])
        total = len(items)
        if total == 0:
            self._set_asset_preview_empty("没有可预览素材", "请选择子分类或换个搜索词")
        desired_grid = QtCore.QSize(self._asset_thumb + 16, self._asset_thumb + (38 if flt else 16))
        if lst.gridSize() != desired_grid:
            lst.setGridSize(desired_grid)
        CAP = 800  # 单视图最多这么多（再多 QListWidget 本身就卡）→ 点子分类或搜索缩小
        capped = total > CAP
        if capped:
            items = items[:CAP]
        favs = set(config.get_asset_favorites())  # 收藏的置顶标星
        placeholder = self._asset_placeholder_icon(False)
        for it in items:
            lw = QtWidgets.QListWidgetItem()  # 不解码 icon，只挂路径(拖拽/单击用)+提示
            lw.setIcon(placeholder)
            fav_tag = "已收藏 · " if it["path"] in favs else ""
            cat = self._asset_rel_category(it["path"])
            if flt:
                lw.setText(self._asset_short_name(it["file"]))
            lw.setToolTip("%s%s\n分类：%s\n%s" % (fav_tag, it["file"], cat, it["path"]))
            lw.setData(QtCore.Qt.ItemDataRole.UserRole, it["path"])
            lst.addItem(lw)
        lst.setUpdatesEnabled(True)
        if hasattr(self, "asset_fs_count"):
            if capped:
                self.asset_fs_count.setText("%d/%d" % (CAP, total))
                self.asset_fs_count.setToolTip("该视图共 %d 张，显前 %d——点子分类或搜索缩小（一类放上万张会卡，建议拆子文件夹）" % (total, CAP))
            elif flt:
                self.asset_fs_count.setText("搜索 %d" % total); self.asset_fs_count.setToolTip("")
            else:
                self.asset_fs_count.setText("%d 张" % total); self.asset_fs_count.setToolTip("")
        self._thumb_timer.start()  # 布局就绪后解码首屏可见（去抖定时器）

    def _apply_asset_search(self):
        self._asset_filter = self.asset_search.text().strip().lower()
        if hasattr(self, "asset_path_label"):
            if self._asset_filter:
                self.asset_path_label.setText("全库搜索")
            elif self.asset_tree.currentItem() is not None:
                self._set_asset_path_label(self.asset_tree.currentItem())
            else:
                self.asset_path_label.setText("选择分类")
        self._refresh_asset_thumbs()

    def _asset_hovered(self, item):
        path = item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        if path:
            self._update_asset_preview(path)

    def _asset_selection_changed(self):
        item = self.asset_fs_list.currentItem() if hasattr(self, "asset_fs_list") else None
        if item is not None:
            self._asset_hovered(item)

    def _update_asset_preview(self, path: str):
        if not all(hasattr(self, name) for name in ("asset_preview_thumb", "asset_preview_name", "asset_preview_meta")):
            return
        import os as _os
        reader = QtGui.QImageReader(path)
        reader.setAutoTransform(True)
        sz = reader.size()
        folder = _os.path.basename(_os.path.dirname(path)) or "素材"
        rel_dir = self._asset_rel_category(path)
        meta = folder
        if sz.isValid():
            meta = "%s · %d×%d" % (meta, sz.width(), sz.height())
            target = QtCore.QSize(220, 150)
            scaled = QtCore.QSize(sz)
            scaled.scale(target, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(scaled)
        img = reader.read()
        if img.isNull():
            icon = self._asset_placeholder_icon(True)
            pm = icon.pixmap(QtCore.QSize(112, 112))
            meta = meta + " · 预览失败"
        else:
            pm = QtGui.QPixmap.fromImage(img).scaled(
                220, 150, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        self.asset_preview_thumb.setPixmap(pm)
        self.asset_preview_name.setText(_os.path.basename(path))
        self.asset_preview_meta.setText(meta)
        self.asset_preview_name.setToolTip(path)
        self.asset_preview_meta.setToolTip(path)
        if hasattr(self, "asset_preview_path"):
            self.asset_preview_path.setText(rel_dir)
            self.asset_preview_path.setToolTip(path)
        self._sync_preview_actions(True, path)

    def _place_preview_asset(self):
        path = getattr(self, "_asset_preview_path", "")
        if not path:
            return
        cs = self.canvas_size
        center = QtCore.QPointF(cs[0] / 2, cs[1] / 2) if cs else QtCore.QPointF(0, 0)
        self._place_asset(center, path)

    def _toggle_preview_favorite(self):
        path = getattr(self, "_asset_preview_path", "")
        if not path:
            return
        self._toggle_fs_favorite(path)
        self._sync_preview_actions(True, path)

    def _lazy_decode_asset_thumbs(self):
        """只解码当前可见(+上下各 30 预读)项的缩略图；已解码的跳过（item 持 icon + 解码标记）。"""
        lst = self.asset_fs_list
        n = lst.count()
        if n == 0:
            return
        vp = lst.viewport().rect()
        grid = lst.gridSize()
        cell_w = max(1, grid.width())
        cell_h = max(1, grid.height())
        cols = max(1, vp.width() // cell_w)
        sb = lst.verticalScrollBar()
        visible_rows = max(1, vp.height() // cell_h + 2)
        total_rows = max(1, (n + cols - 1) // cols)
        ratio = sb.value() / max(1, sb.maximum())
        first_row = int(max(0, total_rows - visible_rows) * ratio)
        prefetch_rows = 3
        a = max(0, (first_row - prefetch_rows) * cols)
        b = min(n - 1, (first_row + visible_rows + prefetch_rows) * cols - 1)
        base = max(48, lst.iconSize().width())   # 逻辑像素的目标缩略图边长
        dpr = max(1.0, self.devicePixelRatioF())  # 高分屏：按物理像素解码再标 dpr → 大且清晰，不糊不缩
        TH = int(round(base * dpr))               # 实际解码到的物理像素边长
        DR = QtCore.Qt.ItemDataRole.UserRole + 1  # 解码完成标记
        DSZ = QtCore.Qt.ItemDataRole.UserRole + 2  # 已解码时的目标边长（变大后需重解）
        for r in range(a, b + 1):
            lw = lst.item(r)
            if lw is None or (lw.data(DR) and lw.data(DSZ) == base):
                continue
            rd = QtGui.QImageReader(lw.data(QtCore.Qt.ItemDataRole.UserRole))  # 缩放解码，不全解大图
            rd.setAutoTransform(True)
            sz = rd.size()
            if sz.isValid() and (sz.width() > TH or sz.height() > TH):
                sz.scale(TH, TH, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                rd.setScaledSize(sz)
            img = rd.read()
            if not img.isNull():
                pm = QtGui.QPixmap.fromImage(img)
                if pm.width() < TH and pm.height() < TH:  # 小图标放大到格子大小，看得清（大图已缩放解码）
                    pm = pm.scaled(TH, TH, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                   QtCore.Qt.TransformationMode.SmoothTransformation)
                pm.setDevicePixelRatio(dpr)  # 标记物理/逻辑比 → Qt 按逻辑 base 显示且清晰
                lw.setIcon(QtGui.QIcon(pm))
            else:
                lw.setIcon(self._asset_placeholder_icon(True))
                lw.setToolTip("缩略图读取失败<br>%s" % lw.data(QtCore.Qt.ItemDataRole.UserRole))
            lw.setData(DR, True); lw.setData(DSZ, base)

    def _on_asset_thumb_size(self, val):
        """滑块改变缩略图大小：调 iconSize/gridSize + 清解码标记重新解码（直接解决“太小”）。"""
        self._asset_thumb = int(val)
        lst = self.asset_fs_list
        lst.setIconSize(QtCore.QSize(self._asset_thumb, self._asset_thumb))
        lst.setGridSize(QtCore.QSize(self._asset_thumb + 16, self._asset_thumb + 16))
        DR = QtCore.Qt.ItemDataRole.UserRole + 1
        for r in range(lst.count()):  # 作废旧解码标记 → 下次懒解按新尺寸重解，否则停在旧小图
            it = lst.item(r)
            if it is not None:
                it.setData(DR, False)
        self._thumb_timer.start()

    def _trim_transparent_qimage(self, img):
        """裁掉 QImage 四周透明留白 → 紧致 QImage（图层框=真正的图，仿 BioRender；选择/磁吸/连接线全受益）。
        本就紧致或全透明/无 alpha → 原样返回。"""
        if img is None or img.isNull():
            return img
        try:
            rgba = image_ops.qimage_to_rgba(img.convertToFormat(QtGui.QImage.Format.Format_RGBA8888))
            bb = image_ops.content_bbox(rgba)
        except Exception:
            return img
        if bb is None:
            return img
        x0, y0, x1, y1 = bb
        if x0 == 0 and y0 == 0 and x1 == img.width() and y1 == img.height():
            return img
        return img.copy(x0, y0, x1 - x0, y1 - y0)

    def _trim_active_layer(self):
        """把当前图片/抠图图层【就地】裁掉四周透明留白 → 框=真正的图（内容保持原位）。
        用于裁紧画布上已放好的旧素材。注意：不透明水印(如 pngtree 文字)裁不掉，那种用裁剪工具手动裁。"""
        lyr = self.active
        if not lyr or lyr.get("kind") == "vector" or lyr.get("image") is None:
            self.op_label.setText("请先选中一个图片/抠图图层，再裁透明边"); return
        img = lyr["image"]
        try:
            rgba = image_ops.qimage_to_rgba(img.convertToFormat(QtGui.QImage.Format.Format_RGBA8888))
            bb = image_ops.content_bbox(rgba)
        except Exception:
            bb = None
        if bb is None:
            self.op_label.setText("该层全透明，无法裁剪"); return
        x0, y0, x1, y1 = bb
        if x0 == 0 and y0 == 0 and x1 == img.width() and y1 == img.height():
            self.op_label.setText("该层已经紧贴内容，无需裁剪（若仍有大框，多半是不透明水印——用裁剪工具手动裁）"); return
        self._push_history("裁透明边")
        item = lyr["item"]
        new_tl = item.mapToScene(QtCore.QPointF(x0, y0))  # 内容左上角的 scene 位置（含 pos/scale）→ 裁后保持原位
        new_img = img.copy(x0, y0, x1 - x0, y1 - y0)
        lyr["image"] = new_img
        item.set_image(new_img)
        lyr["_cbbox"] = None  # 内容框缓存失效
        self._suspend_history = True
        item.setPos(new_tl)
        self._suspend_history = False
        self._refresh_connectors()
        if hasattr(self, "_update_outline"):
            self._update_outline()
        self._notify("已裁透明边：%d×%d → %d×%d" % (img.width(), img.height(), x1 - x0, y1 - y0))

    def _place_asset(self, scene_pos: QtCore.QPointF, path: str):
        """素材拖到画布 drop 处：裁掉四周透明留白后建图层，使图居中落在 scene_pos（clamp 不越界）。"""
        img = QtGui.QImage(path)
        if img.isNull():
            self.op_label.setText("无法读取素材：%s" % path)
            return
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img = self._trim_transparent_qimage(img)  # 裁透明边 → 图层框=真正的图（不再是大方框）
        self._push_history("放置素材")
        if not self.layers:  # 空画布 → 按图尺寸初始化（仿 _ai_place_b64 空画布分支）
            self.canvas_size = (img.width(), img.height())
            self.scene.setSceneRect(0, 0, img.width(), img.height())
            layer = self._add_layer(img, "素材", "image")
        else:
            cw, ch = self.canvas_size
            layer = self._add_layer(img, "素材", "image")
            # 智能缩放：大素材缩到 ≤85% 画布（只缩不放），避免铺满/超框；落点居中在 drop 处并双向 clamp（像 PS/BioRender）
            scale = min(cw * 0.85 / max(1, img.width()), ch * 0.85 / max(1, img.height()), 1.0)
            sw, sh = img.width() * scale, img.height() * scale
            x = max(0.0, min(scene_pos.x() - sw / 2, max(0.0, cw - sw)))
            y = max(0.0, min(scene_pos.y() - sh / 2, max(0.0, ch - sh)))
            self._suspend_history = True
            layer["item"].setScale(scale)
            layer["item"].setPos(x, y)
            self._suspend_history = False
        config.push_asset_recent(path)  # 记入「最近使用」
        self.set_tool("move"); self.fit_view(); self._update_info()

    def _fs_asset_clicked(self, item):
        """本地素材【单击】→ 放到画布中央（与拖到指定位置并存，对齐「抠出素材」单击放回画布）。"""
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier):
            return
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path:
            return
        cs = self.canvas_size  # 空画布时为 None：_place_asset 会按图尺寸初始化、忽略此处坐标
        center = QtCore.QPointF(cs[0] / 2, cs[1] / 2) if cs else QtCore.QPointF(0, 0)
        self._place_asset(center, path)

    def _fs_asset_menu(self, pos):
        """本地素材右键菜单：放画布 / 收藏 / 拆分此合集 / 在资源管理器显示 / 复制路径。"""
        it = self.asset_fs_list.itemAt(pos)
        if it is None:
            return
        path = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path:
            return
        is_fav = path in set(config.get_asset_favorites())
        menu = QtWidgets.QMenu(self)
        tc = theme.colors()
        section = QtGui.QAction("放置", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction(icons.tool_icon("move", tc["text"], 16), "放到画布中央", lambda: self._fs_asset_clicked(it))
        menu.addAction(icons.tool_icon("star", tc["accent"], 16), "取消收藏" if is_fav else "收藏", lambda: self._toggle_fs_favorite(path))
        menu.addSeparator()
        section = QtGui.QAction("整理", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction(icons.tool_icon("crop", tc["text"], 16), "拆分此合集为单个图标…", lambda: self._split_one_montage(path))
        menu.addSeparator()
        section = QtGui.QAction("文件", menu); section.setEnabled(False); menu.addAction(section)
        menu.addAction("在资源管理器中显示", lambda: self._reveal_in_explorer(path))
        menu.addAction("复制文件路径", lambda: QtWidgets.QApplication.clipboard().setText(str(path)))
        menu.exec(self.asset_fs_list.mapToGlobal(pos))

    def _toggle_fs_favorite(self, path):
        now = config.toggle_asset_favorite(path)
        self._notify(("已收藏 " if now else "已取消收藏 ") + os.path.basename(path))
        self._refresh_asset_thumbs()  # 刷新星标显示
        if getattr(self, "_asset_preview_path", "") == path:
            self._sync_preview_actions(True, path)

    def _reveal_in_explorer(self, path):
        import subprocess
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            self.op_label.setText("无法打开资源管理器：%s" % e)

    def _split_one_montage(self, path):
        """拆分单张合集图 → 存到同目录的 <stem>_拆分/ 子文件夹，完成后重扫露出新子分类。"""
        qi = QtGui.QImage(path)
        if qi.isNull():
            self._notify("无法读取该图")
            return
        qi = qi.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        boxes = image_ops.split_montage(image_ops.qimage_to_rgba(qi))
        if len(boxes) <= 1:
            self._notify("这张图切不出多个图标，可能本就是单个图标")
            return
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(os.path.dirname(path), stem + "_拆分")
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "拆分合集", "无法创建输出文件夹：%s" % e)
            return
        n = 0
        for idx, (x, y, w, h) in enumerate(boxes):
            if qi.copy(int(x), int(y), int(w), int(h)).save(os.path.join(out, "%s_%02d.png" % (stem, idx + 1)), "PNG"):
                n += 1
        self._notify("已拆成 %d 个单个图标：%s_拆分" % (n, stem))
        self._load_asset_dir()  # 重扫，露出新子文件夹
