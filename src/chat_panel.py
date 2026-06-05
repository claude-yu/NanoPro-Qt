"""AI 对话生成绘图提示词面板 —— 用户口语描述图意，chat 模型产出英文期刊/biorender 风格提示词。

后台 QThread 直连 chat_client（OpenAI 兼容 /chat/completions），主线程只追加对话/落地提示词。
Chat Key 走 config（本机 ~/.sciedit/config.json 的 chat_api_key，与 grsai 生图 key 隔离），
面板只显掩码、保存后清空输入框，绝不在 UI 长驻明文。「用此提示词」把回复填进 AiPanel.prompt。

UI（2026 改版）：对话区改气泡式（user 右对齐 accent 底 / assistant 左对齐 panel 底，圆角）；
模型选择改可编辑 QComboBox + 「拉取模型」按钮（后台 ModelsWorker 调 /v1/models）；
设置区清爽化（去红 banner，key 状态并进折叠标题 + 输入框 placeholder）。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

import chat_client
import config
import theme


class ChatWorker(QtCore.QThread):
    """后台对话：单次调用 chat_client.chat_complete（流式），delta/done 经信号回主线程。"""
    delta = QtCore.Signal(str)        # 流式增量（主线程追加到对话区）
    done = QtCore.Signal(str, str)    # full_text, err

    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self._p = params   # {messages, key, base_url, model, stream}
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        p = self._p
        res = chat_client.chat_complete(
            p["messages"], p["key"], p["base_url"], p["model"],
            stream=p.get("stream", True),
            on_delta=lambda s: self.delta.emit(s),
            should_cancel=lambda: self._cancel,
        )
        self.done.emit(res.get("text", ""), res.get("error", ""))


class ModelsWorker(QtCore.QThread):
    """后台拉模型：调 chat_client.list_models（GET /models），done 经信号回主线程。仿 ChatWorker。"""
    done = QtCore.Signal(list, str)   # models, err

    def __init__(self, base, key, parent=None):
        super().__init__(parent)
        self._base = base
        self._key = key

    def run(self):
        models, err = chat_client.list_models(self._base, self._key)
        self.done.emit(models or [], err or "")


class ChatPanel(QtWidgets.QWidget):
    """AI 对话生成提示词面板内容（放进 FloatingToolWindow）。"""

    def __init__(self, editor):
        super().__init__()
        self._editor = editor
        self._worker = None
        self._models_worker = None
        self._history = []      # [{role,content}] 仅 user/assistant，system 每次发送时前置
        self._cur_reply = ""    # 当前 AI 流式回复累加
        self._last_prompt = ""  # 最近一条完整 AI 回复（供「用此提示词」）
        self._cur_bubble = None  # 当前正在流式生长的 assistant 气泡 QLabel（None=未起）
        self._has_key = False    # 缓存 key 状态（供折叠标题刷新）
        self._bubbles = []       # 所有气泡 QLabel（resize 时重算 max-width，审核 MEDIUM）
        self._build()
        self._apply_bubble_qss()
        self._load_conn()
        # 关浮窗=隐藏，对话/拉模型继续后台跑；仅【程序退出】才停 worker（防销毁仍 run 的 QThread 硬崩，与 AiPanel 一致·审核 HIGH）。
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_workers)

    def _shutdown_workers(self):
        # 仅 aboutToQuit 调：停+等 ChatWorker/ModelsWorker（ModelsWorker 无 stop，只 wait+兜底 terminate）。
        for w, can_stop in ((self._worker, True), (self._models_worker, False)):
            if w is not None and w.isRunning():
                if can_stop:
                    w.stop()
                if not w.wait(3000):
                    w.terminate(); w.wait(1000)

    def eventFilter(self, obj, e):
        # Ctrl+Enter 发送（普通 Enter 仍换行，保留多行编辑）；与编辑器内联编辑的 Ctrl+Enter=提交同约定。
        if obj is self.input and e.type() == QtCore.QEvent.Type.KeyPress:
            if (e.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
                    and (e.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)):
                self._send(); return True
        return super().eventFilter(obj, e)

    # ---------- UI ----------
    def _build(self):
        self.setMinimumWidth(264)
        outer = QtWidgets.QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        inner = QtWidgets.QWidget(); inner.setObjectName("chatInner")
        scroll.setWidget(inner)
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(7)

        # —— 设置（可折叠：▸ 收起 / ▾ 展开；标题带 provider + key 状态小标，一眼可见无需展开）——
        self.settings_toggle = QtWidgets.QToolButton()
        self.settings_toggle.setCheckable(True); self.settings_toggle.setChecked(False)
        self.settings_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.settings_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.settings_toggle.setObjectName("sectionToggle")  # 折叠头样式走主题 QSS
        self.settings_toggle.toggled.connect(self._toggle_settings)
        lay.addWidget(self.settings_toggle)

        self.settings_box = QtWidgets.QFrame()
        self.settings_box.setObjectName("card")
        bl = QtWidgets.QVBoxLayout(self.settings_box); bl.setContentsMargins(8, 8, 8, 8); bl.setSpacing(5)
        self.provider_combo = QtWidgets.QComboBox()
        for pid, label, _b, _m in chat_client.CHAT_PROVIDERS:
            self.provider_combo.addItem(label, pid)
        self.provider_combo.currentIndexChanged.connect(self._on_provider)
        bl.addWidget(self.provider_combo)
        self.base_url = QtWidgets.QLineEdit()
        self.base_url.setPlaceholderText("API 地址，如 https://api.deepseek.com")
        bl.addWidget(self.base_url)
        krow = QtWidgets.QHBoxLayout()
        self.key_input = QtWidgets.QLineEdit(); self.key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("API Key (sk-…)，留空=不改")
        self.key_input.setToolTip("对话模型 API Key，仅保存在本机 ~/.sciedit，不进程序/仓库/日志；留空=不修改已存的 Key")
        self.key_eye = QtWidgets.QToolButton(); self.key_eye.setText("👁"); self.key_eye.setToolTip("显示/隐藏所填 Key")
        self.key_eye.clicked.connect(self._toggle_key_echo)
        self.save_btn = QtWidgets.QPushButton("保存"); self.save_btn.clicked.connect(self._save_conn)
        krow.addWidget(self.key_input, 1); krow.addWidget(self.key_eye); krow.addWidget(self.save_btn)
        bl.addLayout(krow)
        # 模型：可编辑下拉（选已拉取/缓存 或 手填自定义）+ 「拉取模型」
        mrow = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)  # 输入不自动塞列表，发送时再 remember
        self.model_combo.lineEdit().setPlaceholderText("模型名，如 deepseek-chat（可拉取/手填）")
        self.model_combo.activated.connect(self._remember_model)  # 下拉选了模型→即时记住到当前商家
        self.model_combo.lineEdit().editingFinished.connect(self._remember_model)  # 手填完→记住
        self.btn_pull = QtWidgets.QPushButton("拉取模型")
        self.btn_pull.setToolTip("用当前地址 + Key 调 /v1/models 拉该商家可用模型，填进下拉")
        self.btn_pull.clicked.connect(self._pull_models)
        mrow.addWidget(self.model_combo, 1); mrow.addWidget(self.btn_pull)
        bl.addLayout(mrow)
        hint = QtWidgets.QLabel("选商家自动填地址 → 填 Key → 保存；模型可「拉取」或直接手填")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        bl.addWidget(hint)
        self.settings_box.setVisible(False)
        lay.addWidget(self.settings_box)

        # —— 对话显示区（气泡式：QScrollArea + 垂直堆叠气泡）——
        self.chat_scroll = QtWidgets.QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setObjectName("chatScroll")
        self.chat_scroll.setMinimumHeight(180)
        self.chat_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._msg_host = QtWidgets.QWidget(); self._msg_host.setObjectName("chatBg")
        self._msg_lay = QtWidgets.QVBoxLayout(self._msg_host)
        self._msg_lay.setContentsMargins(6, 6, 6, 6); self._msg_lay.setSpacing(8)  # gap 8px
        # 空态占位（无消息时显示提示，添首条气泡时移除）
        self._placeholder = QtWidgets.QLabel("对话记录会显示在这里")
        self._placeholder.setObjectName("hint")
        self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._msg_lay.addWidget(self._placeholder)
        self._msg_lay.addStretch(1)  # 底部弹簧：气泡从上往下堆，新消息贴底
        self.chat_scroll.setWidget(self._msg_host)
        lay.addWidget(self.chat_scroll, 1)

        # —— 输入框 ——
        self.input = QtWidgets.QTextEdit()
        self.input.setMinimumHeight(60)
        self.input.setPlaceholderText("描述你想要的科研图，例：蛋白-配体对接示意，标出结合口袋和氢键（Ctrl+Enter 发送）")
        self.input.installEventFilter(self)  # Ctrl+Enter 发送（与编辑器内联编辑同约定·审核 LOW）
        lay.addWidget(self.input)

        brow = QtWidgets.QHBoxLayout()
        self.btn_send = QtWidgets.QPushButton("发送"); self.btn_send.setProperty("primary", True)
        self.btn_send.setToolTip("把描述发给对话模型，生成英文绘图提示词"); self.btn_send.clicked.connect(self._send)
        self.btn_stop = QtWidgets.QPushButton("停止"); self.btn_stop.setProperty("danger", True)
        self.btn_stop.setToolTip("停止当前生成"); self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setVisible(False)
        brow.addWidget(self.btn_send); brow.addWidget(self.btn_stop)
        lay.addLayout(brow)

        self.btn_use = QtWidgets.QPushButton("用此提示词 → 填入生成框")
        self.btn_use.setToolTip("把最近一条 AI 回复填进 AI 生成面板的提示词框")
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self._use_prompt)
        lay.addWidget(self.btn_use)

        self.status = QtWidgets.QLabel(""); self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        lay.addWidget(self.status)

        self.provider_combo.setToolTip("选对话模型 provider，自动填好下方地址/模型")
        self.base_url.setToolTip("对话 API 地址，选 provider 后自动填，也可手填自定义中转商")
        self.save_btn.setToolTip("保存地址/Key/模型到本机 ~/.sciedit")
        self.model_combo.setToolTip("对话模型名（可下拉选已拉取/缓存，或直接手填自定义）")
        for b in self.findChildren(QtWidgets.QAbstractButton):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def _apply_bubble_qss(self):
        """气泡 / chatBg / chatScroll 样式已迁到全局主题 QSS（theme.py 的 #bubbleUser/#bubbleAsst/#chatBg/#chatScroll）。
        好处：运行时切深浅主题时，连已渲染的历史气泡也随之换色（原来面板级内联只对新气泡生效，是已知瑕疵）。"""
        return  # 保留方法名（__init__ 仍调用），样式交给全局主题

    def _toggle_settings(self, on: bool):
        self.settings_box.setVisible(on)
        self._refresh_toggle_label()

    def _refresh_toggle_label(self):
        """折叠标题：▸/▾ 设置 · {provider 名} · Key✓/未设。展开收起都带当前 provider + key 状态。"""
        arrow = "▾" if self.settings_box.isVisible() else "▸"
        pid = self.provider_combo.currentData()
        label = self.provider_combo.currentText() or pid or "—"
        # provider 标签取「label 括号前的主名」，过长截断
        name = label.split("(")[0].split("（")[0].strip() or "—"
        if len(name) > 14:
            name = name[:14] + "…"
        key_tag = "Key✓" if self._has_key else "未设 Key"
        self.settings_toggle.setText("%s  设置 · %s · %s" % (arrow, name, key_tag))

    def _toggle_key_echo(self):
        pw = self.key_input.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        self.key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if pw else QtWidgets.QLineEdit.EchoMode.Password)
        self.key_eye.setText("🙈" if pw else "👁")

    @staticmethod
    def _pid_for_base(bu: str) -> str:
        """按地址推断商家 id（save/load/切商家共用同一口径，避免桶键不一致丢模型列表，审核 MEDIUM）。"""
        bu = (bu or "").lower()
        if "deepseek" in bu:
            return "deepseek"
        if "bigmodel" in bu:
            return "glm"
        if "xiaomi" in bu or "mimo" in bu:
            return "xiaomi"
        return "custom"

    def _remember_model(self, *_):
        # 选了模型(或改了地址)即时记住到当前商家桶——切走再切回自动恢复，不必点保存
        try:
            config.remember_chat_conn(self._cur_pid(), self.base_url.text().strip(), self._current_model())
        except Exception:
            pass

    def _on_provider(self):
        new_pid = self._cur_pid()
        prev = getattr(self, "_active_pid", None)
        if prev and prev != new_pid:  # 切走前，先把上一个商家当前选的地址/模型记住（不必点保存）
            try:
                config.remember_chat_conn(prev, self.base_url.text().strip(), self._current_model())
            except Exception:
                pass
        self._active_pid = new_pid
        pid = new_pid
        conn = config.get_chat_conn(pid)  # 该商家上次存的(地址/模型/key 状态)
        if conn.get("saved"):
            # 有存档 → 直接恢复上次设置，不必再选/再填（GLM↔DeepSeek↔小米/中转 各记各的）
            base = conn.get("base_url") or ""
            if not base:  # 桶里只记了模型没记地址 → 回退该商家默认地址（防内置商家地址被清空）
                for _id, _l, b, _m in chat_client.CHAT_PROVIDERS:
                    if _id == pid and b:
                        base = b
                        break
            self.base_url.setText(base)
            self._fill_models(config.get_chat_models(pid), keep_current=False)
            if conn.get("model"):
                self._set_model(conn["model"])
        else:
            # 无存档 → 用商家默认地址/模型
            base = model = None
            for _id, _label, b, m in chat_client.CHAT_PROVIDERS:
                if _id == pid:
                    base, model = b, m
                    break
            if base:  # 具体商家 → 填其默认地址/模型
                self.base_url.setText(base)
                if model:
                    self._set_model(model)
            else:
                # 切到空地址商家(小米/自定义)：若当前地址是【别的具体商家的默认地址】(残留) → 清掉，
                # 避免静默沿用旧端点(审核 MEDIUM)；用户手填的自定义地址(不匹配任何内置默认) → 保留。
                builtin_bases = {b for _i, _l, b, _m in chat_client.CHAT_PROVIDERS if b}
                if self.base_url.text().strip() in builtin_bases:
                    self.base_url.clear()
                    self._set_model("")
            self._fill_models(config.get_chat_models(pid), keep_current=True)
        self.key_input.clear()  # 不在 UI 留明文；key 状态由 _set_key_stat 表明（发送时按商家读各自的 key）
        self._set_key_stat(conn.get("has_key", False), conn.get("key_hint", ""))
        self._refresh_toggle_label()

    # ---------- 模型下拉小工具 ----------
    def _current_model(self) -> str:
        return self.model_combo.currentText().strip()

    def _set_model(self, name: str):
        self.model_combo.setEditText(name or "")

    def _fill_models(self, names, keep_current=True):
        """用 names 重填下拉，保留当前编辑框文本（keep_current）。blockSignals 防触发联动。"""
        cur = self._current_model() if keep_current else ""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if names:
            self.model_combo.addItems([str(n) for n in names])
        self.model_combo.setEditText(cur)
        self.model_combo.blockSignals(False)

    def _cur_pid(self):
        return self.provider_combo.currentData()

    def _set_status(self, msg, err=False):
        self.status.setText(msg or "")
        c = theme.colors()
        self.status.setStyleSheet("font-size:11px; color:%s;" % (c["danger"] if err else c["hint"]))

    def _set_key_stat(self, has, hint=""):
        """key 状态：并进折叠标题（Key✓/未设）+ 输入框 placeholder。去掉了独立红 banner。"""
        self._has_key = bool(has)
        self.key_input.setPlaceholderText((hint + "（已保存·留空=不改）") if (has and hint) else "API Key (sk-…)，留空=不改")
        self._refresh_toggle_label()

    # ---------- 拉取模型 ----------
    def _pull_models(self):
        if self._models_worker and self._models_worker.isRunning():
            return
        base = self.base_url.text().strip() or config.chat_base()
        key = self.key_input.text().strip() or config.read_chat_key(self._cur_pid())  # 该商家的 key（或刚填未存的）
        if not key:  # fail-loud：无 key 不发请求
            self._set_status("先填 Key 并保存，再拉取模型", True); return
        self.btn_pull.setEnabled(False)
        self._set_status("拉取中…")
        self._models_worker = ModelsWorker(base, key, self)
        self._models_worker.done.connect(self._on_models_done)
        self._models_worker.start()

    def _on_models_done(self, models, err):
        self.btn_pull.setEnabled(True)
        self._models_worker = None
        if err and not models:
            self._set_status("❌ 拉取失败：%s" % err, True); return
        if not models:
            self._set_status("⚠ 返回成功但没解析到模型", True); return
        self._fill_models(models, keep_current=True)
        try:
            config.set_chat_models(self._cur_pid(), models)
        except Exception:
            pass  # 持久化失败不致命，下拉已填
        self._set_status("✅ 拉取到 %d 个模型，点下拉选一个" % len(models))
        # 自动展开下拉，直接把拉取到的模型显示出来让用户选（不必再去找那个小箭头）
        self.model_combo.showPopup()

    # ---------- 连接配置 ----------
    def _load_conn(self):
        # 恢复到上次使用的商家 + 它的存档（地址/模型/key 状态）。首次无存档→默认 deepseek，由 _on_provider 填默认。
        try:
            pid = config.get_chat_active_pid()
        except Exception:
            self._set_key_stat(False)
            return
        idx = self.provider_combo.findData(pid)
        if idx < 0:
            idx = self.provider_combo.findData("deepseek")
        if idx >= 0:
            self.provider_combo.blockSignals(True)  # 不触发 _on_provider，下面手动调一次（避免 setCurrentIndex 已是该 idx 时不触发）
            self.provider_combo.setCurrentIndex(idx)
            self.provider_combo.blockSignals(False)
        self._on_provider()  # 据当前商家恢复存档

    def _save_conn(self):
        had = bool(self.key_input.text())
        model = self._current_model()
        pid = self._cur_pid()  # 存进【当前所选商家】的桶 → 切回该商家自动恢复，不必重填
        try:
            r = config.set_chat_conn(pid, self.base_url.text().strip(), self.key_input.text(), model)
        except Exception as e:
            self._set_status("保存失败：%s" % e, True); return
        self.key_input.clear()  # 不在 UI 留明文
        # 记住当前模型（含手填自定义）：并进该商家桶最前（与 conn 同键 pid，统一口径）。
        if model:
            try:
                config.set_chat_models(pid, [model] + config.get_chat_models(pid))
            except Exception:
                pass
        self._set_key_stat(bool(r.get("has_key")), r.get("key_hint", ""))
        self._set_status("已保存（该商家设置单独记住·Key 仅存本机，不进程序/仓库/日志）" if had else "已保存（该商家地址/模型已单独记住）")

    # ---------- 气泡渲染 ----------
    def _add_bubble(self, role: str, text: str):
        """插入一条气泡（user 右对齐 / assistant 左对齐），返回内层 QLabel（供流式 setText 累加）。"""
        if self._placeholder is not None:
            self._placeholder.setParent(None)
            self._placeholder = None
        row = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(row); hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(0)
        bubble = QtWidgets.QLabel(text or "")
        bubble.setObjectName("bubbleUser" if role == "user" else "bubbleAsst")
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.setMaximumWidth(self._bubble_max_w())  # 近似 max-width，resize 时 reflow（_bubbles）
        self._bubbles.append(bubble)
        if role == "user":
            hl.addStretch(1); hl.addWidget(bubble)
        else:
            hl.addWidget(bubble); hl.addStretch(1)
        self._msg_lay.insertWidget(self._msg_lay.count() - 1, row)  # 插在底部 stretch 前
        self._scroll_to_bottom(force=True)  # 新消息：滚到可见
        return bubble

    def _bubble_max_w(self) -> int:
        return max(int(self.width() * 0.78), 220)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self._bubble_max_w()  # 窗口缩小时重算每条气泡 max-width，防溢出被裁(审核 MEDIUM)
        if w == getattr(self, "_last_bubble_w", None):
            return  # 宽度没变(夹在 220 下限或 <1px 抖动)→不重排，免拖动时每像素遍历所有气泡
        self._last_bubble_w = w
        if not hasattr(self, "_resize_timer"):
            self._resize_timer = QtCore.QTimer(self); self._resize_timer.setSingleShot(True)
            self._resize_timer.timeout.connect(self._apply_bubble_widths)
        self._resize_timer.start(50)  # 防抖：拖动停下后一次性套用宽度

    def _apply_bubble_widths(self):
        w = self._bubble_max_w()
        for b in self._bubbles:
            try:
                b.setMaximumWidth(w)
            except RuntimeError:
                pass  # 已删除气泡(错误回滚)跳过

    def _scroll_to_bottom(self, force=False):
        bar = self.chat_scroll.verticalScrollBar()
        # 用户上翻看历史时，流式 delta 不强拽回底；新消息/在底部时才滚(审核 LOW)
        if force or bar.value() >= bar.maximum() - 8:
            QtCore.QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    # ---------- 对话 ----------
    def _send(self):
        if self._worker and self._worker.isRunning():
            return
        txt = self.input.toPlainText().strip()
        if not txt:
            self._set_status("请先填写描述", True); return
        key = self.key_input.text().strip() or config.read_chat_key(self._cur_pid())  # 该商家的 key
        if not key:  # fail-loud：无 key 不发请求
            self._set_status("未设置对话模型 API Key：请在「设置」里填写并保存", True); return
        self._history.append({"role": "user", "content": txt})
        self._add_bubble("user", txt)
        self.input.clear()
        messages = [{"role": "system", "content": chat_client.SYSTEM_PROMPT}] + self._history
        base = self.base_url.text().strip() or config.chat_base()
        model = self._current_model() or "deepseek-chat"
        params = {"messages": messages, "key": key, "base_url": base, "model": model, "stream": True}
        self._cur_reply = ""
        self._cur_bubble = None  # 首个 delta 时才起 assistant 气泡
        self._worker = ChatWorker(params, self)
        self._worker.delta.connect(self._append_ai_delta)
        self._worker.done.connect(self._on_done)
        self.btn_send.setEnabled(False); self.btn_stop.setVisible(True)
        self._set_status("生成中…")
        self._worker.start()

    def _append_ai_delta(self, s: str):
        first = self._cur_bubble is None
        if first:
            self._cur_bubble = self._add_bubble("assistant", "")  # 首个 delta 起空气泡
        self._cur_reply += s
        if first:
            self._flush_delta()  # 首个 token 立刻显示——一收到就有反馈，不等 70ms 节流，体感更快
            return
        # 之后节流：累加文本，最多每 ~70ms 重排一次。每 token setText(全文) 会让换行 QLabel 整段重排
        # → 长回复二次方开销、流式时卡顿(审核 MED)。终值由 _on_done 的 setText 保证精确。
        if not hasattr(self, "_delta_timer"):
            self._delta_timer = QtCore.QTimer(self); self._delta_timer.setSingleShot(True)
            self._delta_timer.timeout.connect(self._flush_delta)
        if not self._delta_timer.isActive():
            self._delta_timer.start(70)

    def _flush_delta(self):
        if self._cur_bubble is None:
            return
        try:
            self._cur_bubble.setText(self._cur_reply)
        except RuntimeError:
            return  # 气泡已删(错误回滚)
        self._scroll_to_bottom()

    def _on_done(self, full: str, err: str):
        if hasattr(self, "_delta_timer"):
            self._delta_timer.stop()  # 收尾：停掉节流定时器，下面 setText 直接落终值
        self.btn_send.setEnabled(True); self.btn_stop.setVisible(False)
        # 流式累加值优先（full 与 _cur_reply 一致；非流式时 full 才是全文）
        text = full or self._cur_reply
        if err and not text:
            self._set_status(err, True)
            # 删掉已建的空 assistant 气泡（fail-loud：错误时 UI 不留假气泡）
            if self._cur_bubble is not None:
                if self._cur_bubble in self._bubbles:
                    self._bubbles.remove(self._cur_bubble)  # 同步从 resize 列表移除，避免触已删气泡
                row = self._cur_bubble.parentWidget()
                if row is not None:
                    row.setParent(None)
                self._cur_bubble = None
            # 回滚刚加入的 user 消息，避免历史里悬挂无回复的 user（脏历史→后续请求可能出错）
            if self._history and self._history[-1].get("role") == "user":
                self._history.pop()
            self._cur_reply = ""
            self._worker = None
            return
        if self._cur_bubble is None and text:  # 非流式：done 才拿到全文，补气泡
            self._cur_bubble = self._add_bubble("assistant", text)
        elif self._cur_bubble is not None:
            self._cur_bubble.setText(text)  # 保证终值
        self._history.append({"role": "assistant", "content": text})
        self._last_prompt = text
        self.btn_use.setEnabled(True)
        if err:  # 有部分文本但也报了错 → fail-loud 提示，但仍保留已得文本
            self._set_status("⚠ %s（已保留部分结果）" % err, True)
        else:
            self._set_status("✓ 已生成提示词，可点「用此提示词」")
        self._cur_bubble = None
        self._worker = None

    def _use_prompt(self):
        if not getattr(self._editor, "_ai_panel", None):
            self._set_status("AI 生成面板未就绪", True); return
        self._editor._ai_panel.prompt.setPlainText(self._last_prompt)
        if hasattr(self._editor, "_open_ai_panel_focus_prompt"):
            self._editor._open_ai_panel_focus_prompt()
        self._set_status("✓ 已填入生成框")

    def _stop(self):
        if self._worker:
            self._worker.stop()
            self._set_status("停止中…")
