"""AI 生成面板（文生图/图生图）—— 移植 nanopro-editor/ai.js 的 UI 与流程。

后台 QThread 直连 grsai（ai_client），主线程只刷进度/落地结果。
Key 走 config（本机 ~/.sciedit/config.json），面板只显掩码、保存后清空输入框，绝不在 UI 长驻明文。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import ai_client
import config
import seg_client
import style_lib
import theme


class StyleLibraryDialog(QtWidgets.QDialog):
    """参考图库浏览/选择：顶部选风格库（可多个），下面按主题分组的缩略图网格，单击一张即选中。
    selected=选中图片绝对路径；change_dir=用户点了「更换图库目录」（调用方据此重新选目录）。"""

    COLS = 4
    THUMB = 132

    def __init__(self, libs, root, parent=None):
        super().__init__(parent)
        self.selected = None
        self.change_dir = False
        self._libs = libs              # [{name, dir}]，每个 = 一个风格库
        self._root = root              # 当前图库根目录（整理新库后据此重扫刷新）
        self.setWindowTitle("参考图库 · 选一张作图生图风格参考")
        self.resize(720, 580)
        outer = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("风格库"))
        self.lib_combo = QtWidgets.QComboBox()
        for lb in libs:
            self.lib_combo.addItem(lb["name"], lb["dir"])
        self.lib_combo.currentIndexChanged.connect(self._on_lib)
        self.lib_combo.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        top.addWidget(self.lib_combo, 1)
        btn_org = QtWidgets.QPushButton("整理图库（生成索引）…")
        btn_org.setToolTip("把一个装满图的文件夹一键变成风格库：按子文件夹分主题、顶层散图归未分类，生成 manifest.json")
        btn_org.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_org.clicked.connect(self._organize)
        top.addWidget(btn_org)
        btn_chg = QtWidgets.QPushButton("更换图库目录…")
        btn_chg.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_chg.clicked.connect(self._change)
        top.addWidget(btn_chg)
        outer.addLayout(top)
        self.info = QtWidgets.QLabel(""); self.info.setObjectName("hint"); self.info.setWordWrap(True)
        outer.addWidget(self.info)

        self.scroll = QtWidgets.QScrollArea(self); self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        row = QtWidgets.QHBoxLayout(); row.addStretch(1)
        cancel = QtWidgets.QPushButton("取消"); cancel.clicked.connect(self.reject)
        cancel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        row.addWidget(cancel)
        outer.addLayout(row)
        self._on_lib()                 # 载入第一个风格库

    def _on_lib(self):
        """切换风格库：扫描该库 → 重建主题缩略图区。"""
        lib_dir = self.lib_combo.currentData()
        themes, err = style_lib.scan_library(lib_dir)
        self.info.setText("目录：%s · 共 %d 张%s" % (
            lib_dir, style_lib.count_figures(themes), ("  ·  " + err) if err else ""))
        inner = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(inner); v.setSpacing(6)
        for t in themes:
            head = QtWidgets.QLabel(t["name"] + (("  ·  " + t["keywords"]) if t.get("keywords") else ""))
            head.setStyleSheet("font-weight:700; margin-top:6px;")
            head.setWordWrap(True)
            v.addWidget(head)
            grid_host = QtWidgets.QWidget(); grid = QtWidgets.QGridLayout(grid_host)
            grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(6)
            for i, f in enumerate(t["figures"]):
                grid.addWidget(self._thumb(f), i // self.COLS, i % self.COLS)
            v.addWidget(grid_host)
        if not themes:
            v.addWidget(QtWidgets.QLabel("（该风格库没有可用图片）"))
        v.addStretch(1)
        self.scroll.setWidget(inner)   # 替换旧内容部件

    def _organize(self):
        """选一个装满图的文件夹 → 按子文件夹分主题生成 manifest.json → 刷新风格库列表并切到它。"""
        start = self.lib_combo.currentData() or self._root or ""
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择要整理的文件夹（按子文件夹分主题，顶层散图归「未分类」）", start)
        if not d:
            return
        if (Path(d) / "manifest.json").exists():
            if QtWidgets.QMessageBox.question(
                    self, "整理图库", "该文件夹已有 manifest.json，覆盖重建索引？") \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        nt, nf, err = style_lib.build_manifest(d)
        if err:
            QtWidgets.QMessageBox.warning(self, "整理图库", err)
            return
        QtWidgets.QMessageBox.information(
            self, "整理图库", "已生成索引：%d 个主题 / %d 张图\n%s" % (nt, nf, d))
        self._root = d                 # 切到刚整理的库（其自身即正式风格库）
        config.set_style_lib(d)        # 记住，下次直接用
        self._reload_libs(select_dir=d)

    def _reload_libs(self, select_dir=None):
        """按 self._root 重扫风格库列表，重建下拉（整理新库后调用）。"""
        self._libs = style_lib.list_style_libs(self._root)
        self.lib_combo.blockSignals(True)
        self.lib_combo.clear()
        for lb in self._libs:
            self.lib_combo.addItem(lb["name"], lb["dir"])
        idx = 0
        if select_dir:
            for i in range(self.lib_combo.count()):
                if self.lib_combo.itemData(i) == select_dir:
                    idx = i; break
        self.lib_combo.setCurrentIndex(idx)
        self.lib_combo.blockSignals(False)
        self._on_lib()

    def _thumb(self, fig):
        b = QtWidgets.QToolButton()
        b.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        b.setIconSize(QtCore.QSize(self.THUMB, self.THUMB))
        b.setFixedWidth(self.THUMB + 16)
        rd = QtGui.QImageReader(fig["path"])         # 用 QImageReader 缩放解码，避免整张大图全解码
        rd.setAutoTransform(True)
        sz = rd.size()
        if sz.isValid() and (sz.width() > self.THUMB or sz.height() > self.THUMB):
            sz.scale(self.THUMB, self.THUMB, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            rd.setScaledSize(sz)
        img = rd.read()
        if not img.isNull():
            b.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(img)))
        cap = fig.get("caption") or Path(fig["path"]).stem
        b.setText(cap if len(cap) <= 16 else cap[:15] + "…")
        b.setToolTip("%s\n%s" % (fig["file"], cap))
        b.clicked.connect(lambda: self._pick(fig["path"]))
        return b

    def _pick(self, path):
        self.selected = path
        self.accept()

    def _change(self):
        self.change_dir = True
        self.reject()


class GenWorker(QtCore.QThread):
    """后台生成：循环 total 张，逐张直连 grsai。进度/结果/完成经信号回主线程。"""
    imageReady = QtCore.Signal(str, int)        # b64, idx（主线程落地）
    done = QtCore.Signal(int, int, str, bool)   # ok, total, last_err, cancelled

    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self._p = params
        self._cancel = False
        self._extend = 0.0
        self.prog = -1.0           # 当前张服务端进度%（主线程 QTimer 读）
        self.cur_deadline = 0.0    # 当前张截止(monotonic)
        self.cur_index = 0
        self._timed_out = False

    def stop(self):
        self._cancel = True

    def extend(self, secs: float):
        self._extend += secs

    def remaining(self) -> int:
        return max(0, int(round(self.cur_deadline + self._extend - time.monotonic())))

    def _should_cancel(self) -> bool:
        if self._cancel:
            return True
        if time.monotonic() > self.cur_deadline + self._extend:
            self._timed_out = True
            return True
        return False

    def run(self):
        p = self._p
        total = p["total"]
        dur = p["dur"]
        ok = 0
        last_err = ""
        cancelled = False
        for i in range(total):
            if self._cancel:
                cancelled = True
                break
            self.cur_index = i
            self.prog = -1.0
            self._timed_out = False
            self.cur_deadline = time.monotonic() + dur
            gen = (ai_client.generate_image_openai if p.get("fmt") == "openai"
                   else ai_client.generate_image)  # 按中转站接口格式分发
            res = gen(
                p["prompt"], p["key"], p["base_url"], ref_b64=p["ref"],
                resolution=p["res"], ratio=p["ratio"], model=p["model"],
                on_progress=self._on_prog, should_cancel=self._should_cancel,
                timeout=120,  # per-read idle 超时(远大于相邻 SSE 间隔)；总限时交 should_cancel+cur_deadline，使 +时间 真生效(审核 LOW)
                negative=p["neg"],
            )
            if res.get("b64"):
                self.imageReady.emit(res["b64"], i)
                ok += 1
            else:
                # 区分"服务端超时"与"用户停止"（_timed_out 真且非用户取消 → 超时文案，对齐 ai.js:217）
                if self._timed_out and not self._cancel:
                    last_err = "已超时（点 +时间 续时后重试）"
                else:
                    last_err = res.get("error", "未知错误")
                if self._cancel:
                    cancelled = True
                    break
        if self._cancel:
            cancelled = True
        self.done.emit(ok, total, last_err, cancelled)

    def _on_prog(self, pct: float):
        self.prog = pct


class SegWorker(QtCore.QThread):
    """后台 AI 分割/抠图：单次调用 seg_client（无逐张循环/进度，bar 用 busy 无限滚动即可）。
    结果经信号回主线程落地素材库。比 GenWorker 简单——抠图是一次性 JSON 响应。"""
    done = QtCore.Signal(list, str)   # cutouts(list[b64]), err

    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self._p = params   # {src_b64, mode, provider, base_url, key, model, timeout, endpoint, prompt?, result_endpoint?}

    def run(self):
        p = self._p
        res = seg_client.segment_image(
            p["src_b64"], mode=p["mode"], provider=p["provider"],
            base_url=p["base_url"], key=p["key"], model=p["model"],
            timeout=p["timeout"], endpoint=p["endpoint"],
            prompt=p.get("prompt"), result_endpoint=p.get("result_endpoint"))
        if res.get("error"):
            self.done.emit([], res["error"])
        else:
            self.done.emit(res.get("cutouts") or [], "")


class GenModelsWorker(QtCore.QThread):
    """后台拉生图模型：调 OpenAI 兼容 /v1/models（复用 chat_client.list_models）。grsai 站可能 404→优雅报错。"""
    done = QtCore.Signal(list, str)

    def __init__(self, base, key, parent=None):
        super().__init__(parent)
        self._base = base
        self._key = key

    def run(self):
        try:
            import chat_client
            models, err = chat_client.list_models(self._base, self._key)
        except Exception as e:
            models, err = [], str(e)
        self.done.emit(models or [], err or "")


class Task:
    """一个独立生图任务（大香蕉式并行队列的元素）。

    提交时把全部参数快照进 params（之后用户改 UI 不影响在跑/排队任务）。
    每个任务自带 GenWorker + 自己的落地图层(layer_ids，≥2 张时自己打组) + 自己的行控件 UI 句柄。
    """
    __slots__ = ("id", "params", "worker", "state", "ok", "total", "last_err",
                 "layer_ids", "removed", "row", "bar", "bar_text", "state_label",
                 "btn_extend", "btn_stop", "btn_remove")

    def __init__(self, tid: int, params: dict):
        self.id = tid
        self.params = params       # 提交时快照：{prompt,key,base_url,ref,res,ratio,model,total,dur,neg}
        self.worker = None         # GenWorker，running 时持有，done 后置 None
        self.state = "queued"      # queued|running|done|failed|cancelled
        self.ok = 0
        self.total = params["total"]
        self.last_err = ""
        self.layer_ids = []        # 本任务落地的图层（≥2 自己打组）
        self.removed = False       # 行已被移除（done 回调据此跳过 UI 更新，防访问已删控件）
        # UI 句柄（_build_task_row 填充）
        self.row = None
        self.bar = None
        self.bar_text = None
        self.state_label = None
        self.btn_extend = None
        self.btn_stop = None
        self.btn_remove = None


class AiPanel(QtWidgets.QWidget):
    """AI 生成面板内容（放进 ADS CDockWidget）。"""

    MAX_CONCURRENCY = 3   # 最大并行生图任务数（仿大香蕉 createConcurrencyPool；超出排队，完成一个起下一个）

    def __init__(self, editor):
        super().__init__()
        self._editor = editor
        self._tasks = []       # 所有任务（queued/running/done/failed/cancelled），取代单 self._worker
        self._next_id = 0      # 任务自增 id
        self._ext_refs = []    # 外部参考图文件路径（图生图·外部图片来源）
        self._builtin_ref = None  # 从参考图库选中的单张图路径（图生图·参考图库来源）
        self._has_key = False  # key 状态缓存（并进折叠标题，对齐对话面板）
        self._models_worker = None  # 「拉取模型」后台 worker
        self._tick = QtCore.QTimer(self); self._tick.setInterval(250); self._tick.timeout.connect(self._on_tick)
        self._build()
        self._load_conn()
        # 收尾时机（按用户意图修正）：关浮窗=只隐藏（FloatingToolWindow.closeEvent 是 e.ignore()+hide()），
        # AiPanel 不销毁、任务【继续后台跑】——这正是"一直在后台"。故【不】挂 closeEvent 停 worker。
        # 仅【整个程序退出】才停：进程结束后线程无法存活，不停会销毁仍 run 的 QThread 硬崩(审核 CRITICAL)。
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_workers)

    def _shutdown_workers(self):
        # 仅程序退出(aboutToQuit)时调：停+等【所有】在跑 GenWorker，最后兜底 terminate，防销毁 running QThread 崩溃。
        # 用 findChildren 而非 self._tasks：能一并排空被 _remove 摘除但仍在后台跑的孤儿 worker(都 parent=self·审核 LOW)。
        for w in self.findChildren(GenWorker):
            if w.isRunning():
                w.stop()
                if not w.wait(3000):
                    w.terminate(); w.wait(1000)
        for w in self.findChildren(GenModelsWorker):  # 拉取模型 worker 也排空
            if w.isRunning() and not w.wait(2000):
                w.terminate(); w.wait(1000)

    # ---------- UI ----------
    def _build(self):
        self.setMinimumWidth(264)  # 不至于太窄
        # 外层只放一个滚动区：窗口压短时内容竖向滚动，底部「文生图/图生图」按钮永远够得着
        # （不缩放控件——缩字缩按钮反伤可用性；对齐 PS 面板压短即出滚动条的做法）
        outer = QtWidgets.QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)  # 内层宽度跟随，够高时铺满、压短时才滚动
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        inner = QtWidgets.QWidget(); inner.setObjectName("aiInner")
        scroll.setWidget(inner)
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(7)

        # —— 设置（可折叠：▸ 收起 / ▾ 展开）；key 状态并进标题，去掉旧独立红 banner(对齐对话面板·审核 MEDIUM)——
        self.settings_toggle = QtWidgets.QToolButton()
        self.settings_toggle.setText("▸  设置（中转商地址 / Key / 模型）")
        self.settings_toggle.setCheckable(True); self.settings_toggle.setChecked(False)
        self.settings_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.settings_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.settings_toggle.setObjectName("sectionToggle")  # 折叠头样式走主题 QSS（与聊天面板统一）
        self.settings_toggle.toggled.connect(self._toggle_settings)
        lay.addWidget(self.settings_toggle)
        self.settings_box = QtWidgets.QFrame()
        self.settings_box.setObjectName("card")  # 卡片样式描边，视觉成组
        bl = QtWidgets.QVBoxLayout(self.settings_box); bl.setContentsMargins(8, 8, 8, 8); bl.setSpacing(5)
        self.provider_combo = QtWidgets.QComboBox()  # 中转站（grsai 节点 / OpenAI 图片中转 / grsai 格式中转）
        for pid, label, _b, _f in ai_client.GEN_PROVIDERS:
            self.provider_combo.addItem(label, pid)
        self.provider_combo.setToolTip("选生图中转站，自动填好地址；每站的地址/Key/模型/接口格式各自记住")
        self.provider_combo.currentIndexChanged.connect(self._on_provider)
        bl.addWidget(self.provider_combo)
        self.base_url = QtWidgets.QLineEdit(); self.base_url.setPlaceholderText("API 地址（自定义中转填这里，如 https://你的中转.com/v1）")
        bl.addWidget(self.base_url)
        krow = QtWidgets.QHBoxLayout()
        self.key_input = QtWidgets.QLineEdit(); self.key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("API Key (sk-…)，留空=不改")
        self.key_input.setToolTip("你的 API Key，仅保存在本机 ~/.sciedit，不进程序/仓库/日志；留空=不修改已存的 Key")
        self.key_eye = QtWidgets.QToolButton(); self.key_eye.setText("👁"); self.key_eye.setToolTip("显示/隐藏所填 Key")
        self.key_eye.clicked.connect(self._toggle_key_echo)
        self.save_btn = QtWidgets.QPushButton("保存"); self.save_btn.clicked.connect(self._save_conn)
        krow.addWidget(self.key_input, 1); krow.addWidget(self.key_eye); krow.addWidget(self.save_btn)
        bl.addLayout(krow)
        mrow = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setEditable(True)  # 可手填未知/新模型 id（label≠val，故下方用 _current_model 映射回 val）
        self.model_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)  # 手填不污染列表
        for val, label in ai_client.MODELS:
            self.model_combo.addItem(label, val)
        self.model_combo.currentIndexChanged.connect(self._sync_by_model)
        self.model_combo.activated.connect(self._remember_model)  # 选了模型→即时记住到当前中转站
        self.model_combo.lineEdit().editingFinished.connect(self._remember_model)  # 手填完→记住
        self.btn_pull = QtWidgets.QPushButton("拉取模型")
        self.btn_pull.setToolTip("用当前地址+Key 调 /v1/models 拉该中转站可用模型（OpenAI 兼容站支持；grsai 私有站可能不支持→手填即可）")
        self.btn_pull.clicked.connect(self._pull_models)
        mrow.addWidget(self.model_combo, 1); mrow.addWidget(self.btn_pull)
        bl.addLayout(mrow)
        tip = QtWidgets.QLabel("选中转站自动填地址 → 填 Key → 保存；模型可「拉取」或手填 · Key 存本机 ~/.sciedit")
        tip.setObjectName("hint"); tip.setWordWrap(True)
        bl.addWidget(tip)
        # 没 Key 时引导去真实地址注册获取（grsai 控制台已核实地址）
        self.key_hint_link = QtWidgets.QLabel()
        self.key_hint_link.setObjectName("hint"); self.key_hint_link.setWordWrap(True)
        self.key_hint_link.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.key_hint_link.setOpenExternalLinks(True)  # 点链接用系统浏览器打开
        self.key_hint_link.setVisible(False)
        bl.addWidget(self.key_hint_link)
        self.settings_box.setVisible(False)  # 默认收起省空间
        lay.addWidget(self.settings_box)

        lay.addWidget(QtWidgets.QLabel("正面提示词（描述你要的图）"))
        self.prompt = QtWidgets.QTextEdit()
        self.prompt.setPlaceholderText("描述要生成的科研图，例：clean schematic of protein-ligand docking, white background, vector style, clear labels")
        self.prompt.setMinimumHeight(70)
        self.prompt.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        lay.addWidget(self.prompt, 1)

        self.neg_toggle = QtWidgets.QToolButton()
        self.neg_toggle.setText("▸  负面提示词（已默认填好不友好词，点开可改）")
        self.neg_toggle.setCheckable(True); self.neg_toggle.setChecked(False)
        self.neg_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.neg_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.neg_toggle.setObjectName("sectionToggle")
        self.neg_toggle.toggled.connect(self._toggle_neg)
        lay.addWidget(self.neg_toggle)
        self.neg_box = QtWidgets.QFrame(); self.neg_box.setObjectName("card")
        _nbl = QtWidgets.QVBoxLayout(self.neg_box); _nbl.setContentsMargins(8, 8, 8, 8); _nbl.setSpacing(4)
        self.neg_prompt = QtWidgets.QTextEdit()
        self.neg_prompt.setPlaceholderText("不想出现的元素，如 text, watermark, blurry, low quality")
        self.neg_prompt.setPlainText("lowres, text, watermark, signature, blurry, low quality, jpeg artifacts, distorted, deformed, extra elements, cluttered background")
        self.neg_prompt.setMinimumHeight(44)
        self.neg_prompt.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        self.neg_prompt.setToolTip("不希望出现在图里的元素（英文为佳）；已预填常见不友好词，可删改；留空=不限制")
        _nbl.addWidget(self.neg_prompt)
        self.neg_box.setVisible(False)
        lay.addWidget(self.neg_box)

        row1 = QtWidgets.QHBoxLayout()
        row1.addWidget(QtWidgets.QLabel("分辨率"))
        self.res_combo = QtWidgets.QComboBox(); self.res_combo.addItems(ai_client.RESOLUTIONS); self.res_combo.setCurrentText("2K")
        row1.addWidget(self.res_combo, 1)
        row1.addWidget(QtWidgets.QLabel("张数"))
        self.count_combo = QtWidgets.QComboBox(); self.count_combo.addItems(["1", "2", "3", "4"])
        row1.addWidget(self.count_combo)
        lay.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("比例"))
        self.ratio_combo = QtWidgets.QComboBox(); self.ratio_combo.addItems(ai_client.RATIOS); self.ratio_combo.setCurrentText("1:1")
        row2.addWidget(self.ratio_combo, 1)
        lay.addLayout(row2)

        # —— 图生图参考来源（照搬 sci-figure 方法：参考图强制风格匹配）——
        # 画布合成=所见可见层；当前图层=只拿活动层；当前选区=合成裁到选区 bbox；外部图片=从文件选（可多张，等同 sci-figure --ref）。
        row3 = QtWidgets.QHBoxLayout()
        row3.addWidget(QtWidgets.QLabel("参考来源"))
        self.ref_source = QtWidgets.QComboBox()
        self.ref_source.addItem("画布合成（所见）", "composite")
        self.ref_source.addItem("当前图层", "layer")
        self.ref_source.addItem("当前选区", "selection")
        self.ref_source.addItem("外部图片…", "external")
        self.ref_source.addItem("参考图库…", "library")
        self.ref_source.currentIndexChanged.connect(self._on_ref_source)
        row3.addWidget(self.ref_source, 1)
        self.btn_pick_ref = QtWidgets.QPushButton("选图片…"); self.btn_pick_ref.setEnabled(False)
        self.btn_pick_ref.clicked.connect(self._pick_ref)
        row3.addWidget(self.btn_pick_ref)
        lay.addLayout(row3)
        self.ref_hint = QtWidgets.QLabel(""); self.ref_hint.setObjectName("hint"); self.ref_hint.setWordWrap(True)
        lay.addWidget(self.ref_hint)
        self.ref_source.setToolTip("图生图的参考图从哪来；外部图片=临时选文件(可多张)，参考图库=指向一个图包目录浏览选图")
        self.btn_pick_ref.setToolTip("外部图片：选一张/多张文件；参考图库：浏览图包目录选一张。均强制匹配其配色与风格（等同 sci-figure --ref）")

        self.base_url.setToolTip("API 服务地址，选中转站后自动填，也可手填自定义中转站")
        self.save_btn.setToolTip("保存地址/Key/模型到本机 ~/.sciedit")
        self.model_combo.setToolTip("生成模型；gpt-image-2 基础版仅支持 1K 分辨率")
        self.prompt.setToolTip("用（英文为佳）描述要生成的科研图，越具体越好：主体/背景/风格/标注")
        self.res_combo.setToolTip("输出分辨率；越高越慢")
        self.count_combo.setToolTip("一次生成几张(1–4)，≥2 张会自动打成一组")
        self.ratio_combo.setToolTip("画面宽高比")
        brow = QtWidgets.QHBoxLayout()
        self.btn_t2i = QtWidgets.QPushButton("文生图"); self.btn_t2i.setProperty("primary", True)
        self.btn_t2i.setToolTip("纯文字生成图像（不参考当前画布）"); self.btn_t2i.clicked.connect(lambda: self.run(False))
        self.btn_i2i = QtWidgets.QPushButton("图生图")
        self.btn_i2i.setToolTip("按上面「参考来源」取参考图生成（强制匹配其风格/配色）"); self.btn_i2i.clicked.connect(lambda: self.run(True))
        brow.addWidget(self.btn_t2i); brow.addWidget(self.btn_i2i)
        lay.addLayout(brow)

        # —— 任务列表（取代单条进度条；每点一次生图=并行多开一个任务行）——
        thead = QtWidgets.QHBoxLayout()
        tlbl = QtWidgets.QLabel("任务"); tlbl.setStyleSheet("font-weight:700;")
        thead.addWidget(tlbl)
        self.task_count_lbl = QtWidgets.QLabel(""); self.task_count_lbl.setObjectName("hint")
        thead.addWidget(self.task_count_lbl); thead.addStretch(1)
        self.btn_clear_done = QtWidgets.QPushButton("清完成")
        self.btn_clear_done.setToolTip("移除已完成/失败/已停止的任务行（在跑/排队的不动）")
        self.btn_clear_done.clicked.connect(self._clear_done)
        thead.addWidget(self.btn_clear_done)
        lay.addLayout(thead)
        self.task_list_box = QtWidgets.QWidget()
        self.task_list_lay = QtWidgets.QVBoxLayout(self.task_list_box)
        self.task_list_lay.setContentsMargins(0, 0, 0, 0); self.task_list_lay.setSpacing(5)
        self._task_hint = QtWidgets.QLabel("点「文生图 / 图生图」后任务会出现在这里")  # 空态占位(对齐对话面板·审核 LOW)
        self._task_hint.setObjectName("hint"); self._task_hint.setWordWrap(True)
        self._task_hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.task_list_lay.addWidget(self._task_hint)
        lay.addWidget(self.task_list_box)
        self._refresh_task_header()

        self.status = QtWidgets.QLabel(""); self.status.setWordWrap(True)
        self.status.setObjectName("hint")  # 走主题 QSS，深浅自适应
        lay.addWidget(self.status)
        self._sync_by_model()
        for b in self.findChildren(QtWidgets.QAbstractButton):  # 全部按钮手型光标
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def _toggle_neg(self, on: bool):
        self.neg_box.setVisible(on)
        self.neg_toggle.setText(("▾  " if on else "▸  ") + "负面提示词（已默认填好不友好词，点开可改）")

    def _toggle_settings(self, on: bool):
        self.settings_box.setVisible(on)  # 真折叠：隐藏后不占高度
        self._refresh_toggle_label()

    def _refresh_toggle_label(self):
        # 折叠标题：▸/▾ 设置 · {当前模型} · Key✓/未设 Key —— 收起也能一眼看到模型 + key 状态，替代旧独立红 banner。
        arrow = "▾" if self.settings_box.isVisible() else "▸"
        tag = "Key✓" if self._has_key else "未设 Key"
        model = (self._current_model() or "模型?") if hasattr(self, "model_combo") else "模型?"
        self.settings_toggle.setText("%s  设置 · %s · %s" % (arrow, model, tag))

    def _toggle_key_echo(self):
        pw = self.key_input.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        self.key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if pw else QtWidgets.QLineEdit.EchoMode.Password)
        self.key_eye.setText("🙈" if pw else "👁")

    def _cur_pid(self):
        return self.provider_combo.currentData()

    def _cur_fmt(self) -> str:
        pid = self._cur_pid()
        for p, _l, _b, f in ai_client.GEN_PROVIDERS:
            if p == pid:
                return f
        return "grsai"

    def _remember_model(self, *_):
        # 选了模型(或改了地址)即时记住到当前中转站桶——切走再切回自动恢复，不必点保存
        try:
            config.remember_gen_conn(self._cur_pid(), self.base_url.text().strip(), self._current_model())
        except Exception:
            pass

    def _on_provider(self):
        new_pid = self._cur_pid()
        prev = getattr(self, "_active_pid", None)
        if prev and prev != new_pid:  # 切走前，先把上一个站当前选的地址/模型记住（不必点保存）
            try:
                config.remember_gen_conn(prev, self.base_url.text().strip(), self._current_model())
            except Exception:
                pass
        self._active_pid = new_pid
        pid = new_pid
        conn = config.get_connection(pid)  # 该站上次存的(地址/模型/格式/key 状态)
        if conn.get("saved"):
            base = conn.get("base_url") or ""
            if not base:  # 桶里只记了模型没记地址 → 回退该站默认地址（grsai 节点有默认）
                for p, _l, b, _f in ai_client.GEN_PROVIDERS:
                    if p == pid and b:
                        base = b
                        break
            self.base_url.setText(base)
            self._fill_models(config.get_gen_models(pid), keep_current=False)
            if conn.get("model"):
                self._set_model(conn["model"])
        else:
            default_base = ""
            for p, _l, b, _f in ai_client.GEN_PROVIDERS:
                if p == pid:
                    default_base = b
                    break
            self.base_url.setText(default_base)  # grsai 站填默认地址；自定义中转留空让用户填
            self._fill_models(config.get_gen_models(pid), keep_current=True)
        self.key_input.clear()
        self._set_key_stat(conn.get("has_key", False), conn.get("key_hint", ""))
        self._sync_by_model()
        if hasattr(self, "settings_toggle"):
            self._refresh_toggle_label()

    def _set_model(self, name):
        self.model_combo.setEditText(name or "")

    def _fill_models(self, names, keep_current=True):
        """重填模型下拉：grsai 站含内置 grsai 模型(nano-banana/gpt-image…)，openai 站只放拉取/手填的。"""
        cur = self._current_model() if keep_current else ""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if self._cur_fmt() == "grsai":
            for val, label in ai_client.MODELS:
                self.model_combo.addItem(label, val)
        for n in (names or []):  # 拉取/缓存的（去重）
            if self.model_combo.findData(n) < 0 and self.model_combo.findText(n) < 0:
                self.model_combo.addItem(str(n), str(n))
        self.model_combo.setEditText(cur)
        self.model_combo.blockSignals(False)

    def _pull_models(self):
        if self._models_worker and self._models_worker.isRunning():
            return
        pid = self._cur_pid()
        base = self.base_url.text().strip() or config.grsai_base(pid)
        key = self.key_input.text().strip() or config.read_key(pid)
        if not key:  # fail-loud：无 key 不发请求
            self._set_status("先填 Key 并保存，再拉取模型", True); return
        self.btn_pull.setEnabled(False)
        self._set_status("拉取中…")
        self._models_worker = GenModelsWorker(base, key, self)
        self._models_worker.done.connect(self._on_models_done)
        self._models_worker.start()

    def _on_models_done(self, models, err):
        self.btn_pull.setEnabled(True)
        self._models_worker = None
        if err and not models:
            self._set_status("❌ 拉取失败：%s（grsai 私有站可能不支持 /models，手填模型即可）" % err, True); return
        if not models:
            self._set_status("⚠ 返回成功但没解析到模型，手填即可", True); return
        self._fill_models(models, keep_current=True)
        try:
            config.set_gen_models(self._cur_pid(), models)
        except Exception:
            pass
        self._set_status("✅ 拉取到 %d 个模型，点下拉选一个" % len(models))
        self.model_combo.showPopup()  # 自动展开让用户直接选

    def _current_model(self):
        # 可编辑下拉取模型 id：选中已知项时把显示的 label 映射回 val；手填未知 id 时原样返回。
        txt = self.model_combo.currentText().strip()
        idx = self.model_combo.findText(txt)
        return (self.model_combo.itemData(idx) or txt) if idx >= 0 else txt

    def _sync_by_model(self):
        # OpenAI 图片接口尺寸由比例决定(~1024–1536)，没有 1K/2K/4K 分档 → 禁用分辨率，避免选了 4K 却静默降级(fail-loud)
        if self._cur_fmt() == "openai":
            self.res_combo.setEnabled(False)
            self.res_combo.setToolTip("OpenAI 图片接口尺寸由比例决定，不分 1K/2K/4K（该项对此中转站无效）")
        elif self._current_model() == "gpt-image-2":  # gpt-image-2 基础版仅 1K（对齐 syncByModel ai.js:83-87）
            self.res_combo.setCurrentText("1K"); self.res_combo.setEnabled(False)
            self.res_combo.setToolTip("gpt-image-2 基础版仅支持 1K 分辨率")
        else:
            self.res_combo.setEnabled(True)
            self.res_combo.setToolTip("输出分辨率；越高越慢")
        if hasattr(self, "settings_toggle"):
            self._refresh_toggle_label()  # 模型变 → 折叠标题里的模型名跟着更新

    def _on_ref_source(self):
        src = self.ref_source.currentData()
        self.btn_pick_ref.setEnabled(src in ("external", "library"))
        if src == "external":
            self.btn_pick_ref.setText("选图片…")
            self.ref_hint.setText("已选 %d 张外部参考图" % len(self._ext_refs) if self._ext_refs else "")
        elif src == "library":
            self.btn_pick_ref.setText("选图…")
            self.ref_hint.setText("参考图库：%s" % Path(self._builtin_ref).name if self._builtin_ref else "")
        else:                              # 切回画布/图层/选区 → 清掉外部图/图库选择
            self._ext_refs = []
            self._builtin_ref = None
            self.ref_hint.setText("")

    def _pick_ref(self):
        if self.ref_source.currentData() == "library":
            self._pick_lib_ref()
        else:
            self._pick_ext_refs()

    def _pick_ext_refs(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择外部参考图（可多张）", "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
        if paths:
            self._ext_refs = list(paths)
            self.ref_hint.setText("已选 %d 张外部参考图" % len(paths))

    def _pick_lib_ref(self):
        """指定/复用图库目录 → 解析出风格库(可多个) → 弹缩略图对话框选一张。
        目录无效或用户要换则循环重选。指向父目录(下含多个风格库子目录)时对话框顶部可切换风格库。"""
        while True:
            root = config.get_style_lib()
            if not root:
                root = QtWidgets.QFileDialog.getExistingDirectory(
                    self, "选择图库目录（可指含多个风格库子目录的父目录，或单个风格库/图片文件夹）")
                if not root:
                    return
                config.set_style_lib(root)
            libs = style_lib.list_style_libs(root)
            if not libs:                   # 目录里没有任何可用风格库/图 → 提示并清记忆重选
                QtWidgets.QMessageBox.warning(self, "参考图库", "该目录下没有可用的风格库或图片")
                config.set_style_lib("")
                continue
            dlg = StyleLibraryDialog(libs, root, self)
            dlg.exec()
            if dlg.change_dir:             # 用户点了「更换图库目录」→ 清记忆重选
                config.set_style_lib("")
                continue
            if dlg.selected:
                self._builtin_ref = dlg.selected
                self.ref_hint.setText("参考图库：%s" % Path(self._builtin_ref).name)
            return

    def _gather_ref(self):
        """按「参考来源」取图生图参考；返回 (ref, err)。ref 为 b64 或 b64 列表；err 非 None 表示该来源不可用。"""
        src = self.ref_source.currentData()
        if src == "external":
            if not self._ext_refs:
                return None, "请先点「选图片…」选择外部参考图"
            return self._editor._ai_ref_from_files(self._ext_refs)
        if src == "library":
            if not self._builtin_ref:
                return None, "请先点「选图…」从参考图库选一张图"
            return self._editor._ai_ref_from_files([self._builtin_ref])
        if src == "layer":
            ref = self._editor._ai_ref_layer_b64()
            return (ref, None) if ref else (None, "未选中图层，无法以图层作参考")
        if src == "selection":
            ref = self._editor._ai_ref_selection_b64()
            return (ref, None) if ref else (None, "无选区，先用套索/矩形/魔棒取选区")
        ref = self._editor._ai_snapshot_b64()  # composite
        return (ref, None) if ref else (None, "画布为空，无法做图生图（先导入或文生图一张）")

    def _set_status(self, msg, err=False):
        self.status.setText(msg or "")
        c = theme.colors()
        self.status.setStyleSheet("font-size:11px; color:%s;" % (c["danger"] if err else c["hint"]))

    def _set_key_stat(self, has, hint=""):
        # key 状态并进折叠标题（Key✓/未设 Key），去掉旧独立红 banner（对齐对话面板）。
        self._has_key = bool(has)
        # 掩码尾号只放输入框 placeholder，顶部不显明文/尾号（对齐 ai.js:69-70）
        self.key_input.setPlaceholderText((hint + "（已保存·留空=不改）") if (has and hint) else "API Key (sk-…)，留空=不改")
        self._update_key_hint()
        self._refresh_toggle_label()

    def _update_key_hint(self):
        """没 Key 时引导去真实地址获取：grsai 站给官方控制台链接，自定义 OpenAI 中转给通用提示。"""
        if not hasattr(self, "key_hint_link"):
            return
        if self._has_key:
            self.key_hint_link.setVisible(False)
            return
        if self._cur_fmt() == "grsai":
            self.key_hint_link.setText(
                '还没有 API Key？<a href="https://grsai.com/zh/dashboard/api-keys">前往 grsai 控制台注册获取 →</a>'
                '<br><span style="color:gray">（grsai.com 打不开可试 grsai.ai；国内直连节点 grsai.dakka.com.cn）</span>')
        else:
            self.key_hint_link.setText("自定义中转：请到你的中转站官网注册并获取 API Key，填入上方")
        self.key_hint_link.setVisible(True)

    # ---------- 连接配置 ----------
    def _load_conn(self):
        # 恢复到上次使用的中转站 + 它的存档（地址/模型/格式/key 状态）；首次→默认 grsai 国外节点
        try:
            pid = config.get_gen_active_pid()
        except Exception:
            self._set_key_stat(False); return
        idx = self.provider_combo.findData(pid)
        if idx < 0:
            idx = 0
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.blockSignals(False)
        self._on_provider()  # 据当前站恢复存档

    def _save_conn(self):
        had = bool(self.key_input.text())
        model = self._current_model()
        pid = self._cur_pid()  # 存进【当前所选中转站】的桶（含接口格式）→ 切回该站自动恢复
        try:
            r = config.set_connection(pid, self.base_url.text().strip(), self.key_input.text(), model, self._cur_fmt())
        except Exception as e:
            self._set_status("保存失败：%s" % e, True); return
        self.key_input.clear()  # 不在 UI 留明文
        if model:  # 记住该站当前模型（含手填）
            try:
                config.set_gen_models(pid, [model] + config.get_gen_models(pid))
            except Exception:
                pass
        self._set_key_stat(bool(r.get("has_key")), r.get("key_hint", ""))
        self._set_status("已保存（该中转站设置单独记住·Key 仅存本机，不进程序/仓库/日志）" if had
                         else "已保存（该中转站地址/模型已单独记住）")

    # ---------- 生成（大香蕉式并行任务队列）----------
    def run(self, use_ref: bool):
        """每点一次文生图/图生图 = 提交一个独立任务（提交时快照全部参数）。

        不再单 worker 守卫、不再禁用按钮——可连点多次并行跑；改 model/prompt 只影响下一个任务。
        无 key / 空 prompt / 参考图取不到 → fail-loud（红字 return，不建 Task）。
        """
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            self._set_status("请先填写描述", True); return
        neg = self.neg_prompt.toPlainText().strip()
        pid = self._cur_pid()  # 用【当前所选中转站】的地址/Key/格式（不必先保存为活动站）
        key = self.key_input.text().strip() or config.read_key(pid)
        if not key:
            self._set_status("未设置 API Key：请在「设置」里填写并保存", True); return
        ref = None
        if use_ref:
            ref, err = self._gather_ref()
            if err:
                self._set_status(err, True); return
        model = self._current_model()
        total = max(1, min(4, int(self.count_combo.currentText())))
        base = 180.0 if ("vip" in model or model.startswith("gpt-image")) else 120.0  # vip/gpt-image 慢→单张3min
        dur = base + (total - 1) * 60.0
        params = {   # 提交时快照：之后改 UI 不影响本任务
            "prompt": prompt, "key": key,
            "base_url": self.base_url.text().strip() or config.grsai_base(pid),
            "fmt": self._cur_fmt(),  # grsai|openai → GenWorker 据此分发到不同请求
            "ref": ref, "res": self.res_combo.currentText(),
            "ratio": self.ratio_combo.currentText(), "model": model,
            "total": total, "dur": dur, "neg": neg,
        }
        self._next_id += 1
        t = Task(self._next_id, params)
        self._tasks.append(t)
        self._build_task_row(t)
        self._refresh_task_header()
        self._set_status("已提交任务 #%d（并发上限 %d，超出排队）" % (t.id, self.MAX_CONCURRENCY))
        self._pump()

    def _pump(self):
        """并发调度：running 数 < 上限且有 queued → 起最早的 queued（对齐大香蕉 pool 的 firenext）。"""
        # 数真正在跑的 GenWorker（含 _remove 摘除但后台还没到取消点的孤儿）。
        # 只数 self._tasks 会漏掉孤儿 → _pump 误判有空位多起一个，瞬时并发超过 MAX_CONCURRENCY(审核 LOW L4)。
        running = sum(1 for w in self.findChildren(GenWorker) if w.isRunning())
        for t in self._tasks:
            if running >= self.MAX_CONCURRENCY:
                break
            if t.state == "queued" and not t.removed:
                self._start_task(t)
                running += 1
        # 还有 running 任务就保证 tick 在跑；全 done 则停（幂等）
        if any(t.state == "running" for t in self._tasks):
            if not self._tick.isActive():
                self._tick.start()
        else:
            self._tick.stop()

    def _start_task(self, t: Task):
        w = GenWorker(t.params, self)
        w.imageReady.connect(lambda b64, idx, tt=t: self._on_image(b64, idx, tt))
        w.done.connect(lambda ok, total, err, cancelled, tt=t: self._on_done(ok, total, err, cancelled, tt))
        t.worker = w
        t.state = "running"
        self._update_task_row(t)
        w.start()

    # ---------- 任务行 UI ----------
    def _build_task_row(self, t: Task):
        row = QtWidgets.QFrame(); row.setObjectName("card")
        h = QtWidgets.QHBoxLayout(row); h.setContentsMargins(6, 5, 6, 5); h.setSpacing(6)
        # 缩略图：图生图首张 ref 解码小图；纯文生图占位「T」
        thumb = QtWidgets.QLabel(); thumb.setFixedSize(40, 40)
        thumb.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("border:1px solid rgba(128,128,128,0.4); border-radius:4px;")
        pm = self._ref_thumb_pixmap(t.params.get("ref"))
        if pm is not None:
            thumb.setPixmap(pm)
        else:
            thumb.setText("T")
        h.addWidget(thumb)
        # 中列：提示词摘要 + 设置标签 + 进度条/状态
        mid = QtWidgets.QVBoxLayout(); mid.setContentsMargins(0, 0, 0, 0); mid.setSpacing(2)
        p = t.params
        full = p["prompt"]
        psum = full if len(full) <= 28 else full[:27] + "…"
        plbl = QtWidgets.QLabel("#%d  %s" % (t.id, psum)); plbl.setToolTip(full)
        plbl.setStyleSheet("font-size:11px;")
        mid.addWidget(plbl)
        slbl = QtWidgets.QLabel("%s · %s · %s · ×%d" % (p["model"], p["res"], p["ratio"], p["total"]))
        slbl.setObjectName("hint"); slbl.setStyleSheet("font-size:10px;")
        mid.addWidget(slbl)
        prow = QtWidgets.QHBoxLayout(); prow.setContentsMargins(0, 0, 0, 0); prow.setSpacing(4)
        bar = QtWidgets.QProgressBar(); bar.setRange(0, 100); bar.setTextVisible(False); bar.setFixedHeight(6)
        bar_text = QtWidgets.QLabel(""); bar_text.setStyleSheet("font-size:10px;")
        state_label = QtWidgets.QLabel(""); state_label.setStyleSheet("font-size:10px;")
        state_label.setWordWrap(True)
        state_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        prow.addWidget(bar, 1); prow.addWidget(bar_text); prow.addWidget(state_label)
        mid.addLayout(prow)
        h.addLayout(mid, 1)
        # 右列：+时间 / 停止 / 移除
        btn_extend = QtWidgets.QToolButton(); btn_extend.setText("+时间")
        btn_extend.setToolTip("该任务当前张快超时时 +60 秒，可多次点")
        btn_extend.clicked.connect(lambda: self._extend(t))
        btn_stop = QtWidgets.QToolButton(); btn_stop.setText("停止"); btn_stop.setProperty("danger", True)
        btn_stop.setToolTip("停止该任务；已成功的张数仍会落到画布")
        btn_stop.clicked.connect(lambda: self._stop(t))
        btn_remove = QtWidgets.QToolButton(); btn_remove.setText("×")
        btn_remove.setToolTip("移除该任务（在跑则先停止）")
        btn_remove.clicked.connect(lambda: self._remove(t))
        for b in (btn_extend, btn_stop, btn_remove):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        h.addWidget(btn_extend); h.addWidget(btn_stop); h.addWidget(btn_remove)
        self.task_list_lay.addWidget(row)
        t.row = row; t.bar = bar; t.bar_text = bar_text; t.state_label = state_label
        t.btn_extend = btn_extend; t.btn_stop = btn_stop; t.btn_remove = btn_remove
        self._update_task_row(t)

    def _ref_thumb_pixmap(self, ref):
        """图生图首张 ref(b64 或 b64 列表) → 40x40 缩略 QPixmap；无 ref/解码失败 → None。"""
        if not ref:
            return None
        b64 = ref[0] if isinstance(ref, (list, tuple)) else ref
        if not b64:
            return None
        ba = QtCore.QByteArray.fromBase64(str(b64).encode("ascii"))
        img = QtGui.QImage()
        if not img.loadFromData(ba) or img.isNull():
            return None
        pm = QtGui.QPixmap.fromImage(img).scaled(
            40, 40, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        return pm

    def _update_task_row(self, t: Task):
        """刷新某行的状态文字 + 停止/+时间 按钮可用性（按 t.state）。进度由 _on_tick 刷。"""
        if t.removed or t.row is None:
            return
        running = t.state == "running"
        t.btn_extend.setEnabled(running)
        t.btn_stop.setEnabled(running or t.state == "queued")
        labels = {"queued": "排队中", "running": "生成中", "done": "✓ 完成",
                  "failed": "失败", "cancelled": "已停止"}
        txt = labels.get(t.state, t.state)
        if t.state in ("done", "cancelled", "failed"):
            txt += " %d/%d" % (t.ok, t.total)
            if t.state == "failed" and t.last_err:
                _e = t.last_err if len(t.last_err) <= 80 else t.last_err[:79] + "…"
                txt += "：" + _e
        c = theme.colors()
        col = c["danger"] if t.state == "failed" else c.get("hint", "#888")
        t.state_label.setText(txt); t.state_label.setStyleSheet("font-size:10px; color:%s;" % col)
        if t.state == "done":
            t.bar.setRange(0, 100); t.bar.setValue(100); t.bar_text.setText("")
        elif t.state == "queued":
            t.bar.setRange(0, 100); t.bar.setValue(0); t.bar_text.setText("")

    def _refresh_task_header(self):
        n_run = sum(1 for t in self._tasks if t.state == "running")
        n_q = sum(1 for t in self._tasks if t.state == "queued")
        self.task_count_lbl.setText("跑 %d · 排队 %d · 共 %d" % (n_run, n_q, len(self._tasks)))
        if hasattr(self, "_task_hint"):
            self._task_hint.setVisible(len(self._tasks) == 0)  # 空列表显占位，有任务则隐(审核 LOW)

    def _on_tick(self):
        """全局 250ms：遍历 running 任务刷各自行进度/剩余秒。"""
        for t in self._tasks:
            if t.state != "running" or t.removed or t.worker is None or t.bar is None:
                continue
            if t.btn_stop is not None and not t.btn_stop.isEnabled():
                continue  # 已点停止(按钮禁用)的行不再被倒计时覆盖，保持"停止中…"(审核 LOW)
            w = t.worker
            rem = w.remaining()
            prog = w.prog
            if prog >= 0:
                if t.bar.maximum() == 0:
                    t.bar.setRange(0, 100)
                t.bar.setValue(min(100, int(round(prog))))
            elif t.bar.maximum() != 0:
                t.bar.setRange(0, 0)       # 服务端未回进度 → busy 滚动
            t.bar_text.setText("%d/%d·%ds%s" % (
                min(w.cur_index + 1, t.total), t.total, rem,
                ("·%d%%" % int(round(prog))) if prog >= 0 else ""))

    def _on_image(self, b64: str, idx: int, t: Task):
        """该任务某张生成完成 → 主线程落地（多任务并发但落地都在主线程串行，安全）。"""
        if t.removed:  # 已移除任务的在途结果不落地，避免幽灵图层污染画布(审核 MEDIUM)
            return
        first = not t.layer_ids  # 一个任务多张只在【首张】入历史 → 整任务合并为一步撤销，避免每张全文档快照(审核 性能)
        layer = self._editor._ai_place_b64(b64, push=first)
        if layer is not None:
            t.layer_ids.append(layer)  # 注：存的是 _ai_place_b64 返回的【图层 dict】(按身份给 _ai_group 用)，非 int id

    def _on_done(self, ok: int, total: int, last_err: str, cancelled: bool, t: Task):
        """该任务结束：≥2 张自己打组、行更新、置终态、worker 释放，再 pump 推进队列。"""
        t.ok = ok; t.last_err = last_err
        t.worker = None
        if not t.removed and len(t.layer_ids) >= 2:   # 本任务多张结果自己打组（不跨任务；已移除任务不打组，审核 LOW）
            self._editor._ai_group(t.layer_ids)
        t.state = "cancelled" if cancelled else ("done" if ok == total else "failed")
        if not t.removed:                  # 行还在才更新 UI（防访问已删控件）
            self._update_task_row(t)
        self._refresh_task_header()
        self._pump()                        # firenext：起下一个排队任务

    def _stop(self, t: Task):
        """单独停某任务：running → worker.stop()；queued 未起 → 直接置 cancelled。不影响其它任务。"""
        if t.state == "running" and t.worker:
            t.worker.stop()
            # 立即反馈：禁用按钮 + 冻结进度条文字（worker 要到下个取消点才真退出，否则用户以为没反应·审核 LOW）
            if t.btn_stop is not None:
                t.btn_stop.setEnabled(False)
            if t.btn_extend is not None:
                t.btn_extend.setEnabled(False)
            t.state_label.setText("停止中…")
            if t.bar_text is not None:
                t.bar_text.setText("停止中…")
        elif t.state == "queued":
            t.state = "cancelled"
            self._update_task_row(t)
            self._refresh_task_header()
            self._pump()

    def _extend(self, t: Task):
        if t.state == "running" and t.worker:
            t.worker.extend(60.0)
            self._set_status("任务 #%d 已 +60s 续时" % t.id)

    def _remove(self, t: Task):
        """移除某任务行：running 先 stop（标记 removed，worker 自然结束后 _on_done 据 removed 跳过 UI），从表删行。"""
        if t.state == "running" and t.worker:
            t.worker.stop()       # 不 terminate；worker 下一轮 should_cancel 退出后自然 done
        t.removed = True
        if t in self._tasks:
            self._tasks.remove(t)
        if t.row is not None:
            self.task_list_lay.removeWidget(t.row)
            t.row.deleteLater()
            t.row = None
        self._refresh_task_header()
        self._pump()

    def _clear_done(self):
        """清完成：移除所有 done/failed/cancelled 行（在跑/排队的不动）。"""
        for t in list(self._tasks):
            if t.state in ("done", "failed", "cancelled"):
                self._remove(t)
