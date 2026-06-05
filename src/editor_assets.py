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
import image_ops


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
        self._signals.done.emit(self._gen, node, all_items, err)


class AssetsMixin:
    # ---------- 素材库 ----------
    def add_to_assets(self):
        if self.selection_mask is not None and self.active:   # 有选区 → 加裁切的选区
            res = self._crop_selection()
            if res is None:
                QtWidgets.QMessageBox.information(self, "提示", "选区为空")
                return
            self.assets.append(res[0])
        elif self.active:                                     # 无选区 → 把整个选中图层加入
            self.assets.append(self.active["image"].copy())
        else:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中一个图层，或用套索/矩形/魔棒取选区")
            return
        self._refresh_assets()
        self.op_label.setText(f"已加入素材库（共 {len(self.assets)} 个）")

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
        self.op_label.setText("素材已放回画布")

    def _asset_menu(self, pos):  # 素材右键菜单：导出此素材 / 删除此素材（对齐 WebView exportItem/remove）
        it = self.asset_list.itemAt(pos)
        if it is None:
            return
        i = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if i is None or not (0 <= i < len(self.assets)):
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("放回画布", lambda: self._asset_clicked(it))
        menu.addAction("导出此素材…", lambda: self._export_one_asset(i))
        menu.addSeparator()
        menu.addAction("✕ 删除此素材", lambda: self._delete_asset(i))
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
            self.op_label.setText(f"已删除该素材（剩 {len(self.assets)} 个）")

    def _export_one_asset(self, i: int):
        if not (0 <= i < len(self.assets)):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出素材", f"asset_{i + 1}.png", "PNG (*.png)")
        if path:
            self.assets[i].save(path, "PNG")
            self.op_label.setText(f"已导出素材到 {path}")

    def export_assets(self):
        if not self.assets:
            QtWidgets.QMessageBox.information(self, "导出", "素材库为空")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择导出文件夹")
        if not folder:
            return
        n = 0
        for i, im in enumerate(self.assets):
            if im.save(f"{folder}/asset_{i + 1}.png", "PNG"):
                n += 1
        self.op_label.setText(f"已导出 {n}/{len(self.assets)} 个透明 PNG 到 {folder}")

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
        self.op_label.setText("素材库已清空")

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
            return
        busy = QtWidgets.QTreeWidgetItem(["⏳ 正在加载素材库…"])
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
            self.op_label.setText("素材库：%s" % err)
            return
        if node is None:
            self.op_label.setText("素材库：该文件夹下没有图片素材")
            return
        self._asset_all_items = all_items
        self._populate_asset_tree(node)
        top = self.asset_tree.topLevelItem(0)
        if top is not None:
            self.asset_tree.setCurrentItem(top)
            self._asset_cur_items = top.data(0, QtCore.Qt.ItemDataRole.UserRole) or []
        self._refresh_asset_thumbs()
        self.op_label.setText("素材库：%d 张，已按文件夹分类（点分类树浏览/搜索）" % len(all_items))

    _ASSET_MARK = QtCore.Qt.ItemDataRole.UserRole + 1  # 树节点角色：标记「收藏/最近」虚拟分类

    def _populate_asset_tree(self, node):
        """扫描树 → QTreeWidget。item 文本=「名（直属N）」，data 存该节点【直属】items。
        顶部加「⭐收藏 / 🕘最近使用」虚拟分类（点击时实时从 config 取，永远最新）。"""
        def add(parent, nd):
            label = "%s（%d）" % (nd["name"], len(nd["items"])) if nd["items"] else nd["name"]
            it = QtWidgets.QTreeWidgetItem([label])
            it.setData(0, QtCore.Qt.ItemDataRole.UserRole, nd["items"])
            (self.asset_tree.addTopLevelItem if parent is None else parent.addChild)(it)
            for ch in nd["children"]:
                add(it, ch)
            return it
        fav = QtWidgets.QTreeWidgetItem(["⭐ 收藏"]); fav.setData(0, self._ASSET_MARK, "fav")
        rec = QtWidgets.QTreeWidgetItem(["🕘 最近使用"]); rec.setData(0, self._ASSET_MARK, "recent")
        self.asset_tree.addTopLevelItem(fav); self.asset_tree.addTopLevelItem(rec)
        add(None, node).setExpanded(True)  # 根展开，露出一级分类

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
        self.asset_search.blockSignals(True); self.asset_search.clear(); self.asset_search.blockSignals(False)
        self._asset_filter = ""
        self._refresh_asset_thumbs()

    def _split_montage_assets(self):
        """把当前分类里的「图标合集」大图批量按空白沟槽拆成单个图标 PNG → 输出到新文件夹。
        拆开后一图一图标，缩略图又大又清楚——根治“合集里每个图标太小看不清”。大声失败：报拆/跳/败计数。"""
        import os
        items = list(self._asset_cur_items or []) or list(self._asset_all_items or [])
        if not items:
            QtWidgets.QMessageBox.information(self, "拆分合集", "当前分类没有素材。先连接素材文件夹并在分类树里点一个分类。")
            return
        out = QtWidgets.QFileDialog.getExistingDirectory(self, "选择拆分结果的输出文件夹（会写入若干单个图标 PNG）")
        if not out:
            return
        prog = QtWidgets.QProgressDialog("正在拆分合集…", "取消", 0, len(items), self)
        prog.setWindowModality(QtCore.Qt.WindowModality.WindowModal); prog.setMinimumDuration(0)
        sheets = pieces = failed = skipped = 0
        for k, it in enumerate(items):
            if prog.wasCanceled():
                break
            prog.setValue(k); QtWidgets.QApplication.processEvents()
            path = it.get("path")
            qi = QtGui.QImage(path) if path else QtGui.QImage()
            if qi.isNull():
                failed += 1; continue
            qi = qi.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            try:
                boxes = image_ops.split_montage(image_ops.qimage_to_rgba(qi))
            except Exception:
                failed += 1; continue
            if len(boxes) <= 1:                       # 切不出多块=本就是单图 → 跳过(计数，不静默)
                skipped += 1; continue
            sheets += 1
            stem = os.path.splitext(os.path.basename(path))[0]
            for idx, (x, y, w, h) in enumerate(boxes):
                crop = qi.copy(int(x), int(y), int(w), int(h))
                dst = os.path.join(out, "%s_%02d.png" % (stem, idx + 1))
                if crop.save(dst, "PNG"):
                    pieces += 1
                else:
                    failed += 1
        prog.setValue(len(items))
        msg = ("拆分完成：\n  合集大图 %d 张 → 切出单个图标 %d 个\n"
               "  跳过(本就是单图，无需拆) %d 张\n  失败 %d\n  输出目录：%s"
               % (sheets, pieces, skipped, failed, out))
        if pieces and QtWidgets.QMessageBox.question(
                self, "拆分合集", msg + "\n\n是否立即连接这个文件夹作为素材库（即看拆分后的清晰图标）？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                ) == QtWidgets.QMessageBox.StandardButton.Yes:
            config.set_asset_dir(out); self._load_asset_dir()
        else:
            QtWidgets.QMessageBox.information(self, "拆分合集", msg)

    def _build_asset_manifest(self):
        """给当前素材根目录一键生成 manifest.json（按子文件夹分类，递归收深层图），再重扫刷新。"""
        root = config.get_asset_dir()
        if not root:
            QtWidgets.QMessageBox.information(self, "生成分类索引", "请先点上面「连接素材文件夹」选一个目录")
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
        self.op_label.setText(msg.split("\n")[0])
        QtWidgets.QMessageBox.information(self, "生成分类索引", msg)  # 明确弹窗反馈，不只底部小字

    def _export_assets_by_category(self):
        """把本地素材库按【分类】导出到选定目录：每个分类一个子文件夹、复制其中每张图。
        flat+manifest 的图包可借此物理拆成分类文件夹。重名文件追加 _2/_3… 不静默覆盖（大声失败）。"""
        root = config.get_asset_dir()
        groups, err = asset_lib.scan_assets(root) if root else ([], "未连接素材文件夹")
        if err or not groups:
            QtWidgets.QMessageBox.information(self, "按分类导出", err or "请先「连接素材文件夹」并确保里面有素材")
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
        self.op_label.setText(msg)
        QtWidgets.QMessageBox.information(self, "按分类导出", msg)

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
        CAP = 800  # 单视图最多这么多（再多 QListWidget 本身就卡）→ 点子分类或搜索缩小
        capped = total > CAP
        if capped:
            items = items[:CAP]
        favs = set(config.get_asset_favorites())  # 收藏的置顶标星
        for it in items:
            lw = QtWidgets.QListWidgetItem()  # 不解码 icon，只挂路径(拖拽/单击用)+提示
            star = "⭐ " if it["path"] in favs else ""
            # 悬停大图预览：HTML tooltip 内嵌原图(宽240) + 文件名 → 鼠标停上去就看清是什么
            lw.setToolTip("%s<img src='file:///%s' width='240'><br>%s"
                          % (star, it["path"].replace("\\", "/"), it["file"]))
            lw.setData(QtCore.Qt.ItemDataRole.UserRole, it["path"])
            lst.addItem(lw)
        lst.setUpdatesEnabled(True)
        if hasattr(self, "asset_fs_count"):
            if capped:
                self.asset_fs_count.setText("共%d·显%d" % (total, CAP))
                self.asset_fs_count.setToolTip("该视图共 %d 张，显前 %d——点子分类或搜索缩小（一类放上万张会卡，建议拆子文件夹）" % (total, CAP))
            elif flt:
                self.asset_fs_count.setText("找到%d" % total); self.asset_fs_count.setToolTip("")
            else:
                self.asset_fs_count.setText(str(total)); self.asset_fs_count.setToolTip("")
        self._thumb_timer.start()  # 布局就绪后解码首屏可见（去抖定时器）

    def _apply_asset_search(self):
        self._asset_filter = self.asset_search.text().strip().lower()
        self._refresh_asset_thumbs()

    def _lazy_decode_asset_thumbs(self):
        """只解码当前可见(+上下各 30 预读)项的缩略图；已解码的跳过（item 持 icon + 解码标记）。"""
        lst = self.asset_fs_list
        n = lst.count()
        if n == 0:
            return
        vp = lst.viewport().rect()
        first = lst.indexAt(vp.topLeft()); last = lst.indexAt(vp.bottomRight())
        a = first.row() if first.isValid() else 0
        b = last.row() if last.isValid() else (a + 60)   # 布局未就绪/视口超出 → 只解码前一截，绝不误解全部
        a = max(0, a - 30); b = min(n - 1, b + 30)
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

    def _place_asset(self, scene_pos: QtCore.QPointF, path: str):
        """素材拖到画布 drop 处：按原尺寸建图层，使图居中落在 scene_pos（clamp 不越界）。"""
        img = QtGui.QImage(path)
        if img.isNull():
            self.op_label.setText("无法读取素材：%s" % path)
            return
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
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
        menu.addAction("放到画布中央", lambda: self._fs_asset_clicked(it))
        menu.addAction("取消收藏 ⭐" if is_fav else "收藏 ⭐", lambda: self._toggle_fs_favorite(path))
        menu.addSeparator()
        menu.addAction("✂ 拆分此合集为单个图标…", lambda: self._split_one_montage(path))
        menu.addAction("在资源管理器中显示", lambda: self._reveal_in_explorer(path))
        menu.addAction("复制文件路径", lambda: QtWidgets.QApplication.clipboard().setText(str(path)))
        menu.exec(self.asset_fs_list.mapToGlobal(pos))

    def _toggle_fs_favorite(self, path):
        now = config.toggle_asset_favorite(path)
        self.op_label.setText(("已收藏 ⭐ " if now else "已取消收藏 ") + os.path.basename(path))
        self._refresh_asset_thumbs()  # 刷新星标显示

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
            QtWidgets.QMessageBox.information(self, "拆分合集", "无法读取该图")
            return
        qi = qi.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        boxes = image_ops.split_montage(image_ops.qimage_to_rgba(qi))
        if len(boxes) <= 1:
            QtWidgets.QMessageBox.information(self, "拆分合集", "这张图切不出多个图标（可能本就是单个图标，无需拆分）")
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
        QtWidgets.QMessageBox.information(
            self, "拆分合集", "已把这张合集拆成 %d 个单个图标 →\n%s\n\n（分类树里会出现「%s_拆分」子分类）" % (n, out, stem))
        self._load_asset_dir()  # 重扫，露出新子文件夹
