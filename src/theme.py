"""主题：深色 / 浅色双主题（QSS 模板 + 调色板），可运行时切换。

颜色集中在 DARK/LIGHT 两个字典；QSS 用 @token@ 占位符，apply() 时整体替换。
其它模块（canvas_view/LayerRow）通过 colors() 读当前主题色，切换后重绘即自适应。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 调色板（对标 SciEdit PS 插件 6.3.2 的蓝调专业风：强调色 = PS 式克制蓝 #0078d4，刻度更克制的圆角）。
# on_accent = 强调底（按钮/气泡/星）上的文字色。
DARK = {
    "window": "#1f2128", "panel": "#23262e", "base": "#16181d", "border": "#343943",
    "text": "#d6d9e0", "muted": "#9aa0ad", "hint": "#a3a9b6", "accent": "#1a8aff", "accent_hover": "#3d9bff",
    "on_accent": "#ffffff",
    "button": "#2c313c", "button_border": "#3a4150", "button_hover": "#353c49", "pressed": "#20242c",
    "menu_bar": "#191b21", "scroll": "#3a4150", "scroll_hover": "#4a5263",
    "spin_btn": "#262b34", "spin_btn_hover": "#323844", "danger": "#e5484d",
    "row_active": "#22344f", "thumb": "#0f1115", "outline": "#22d3ee",
    "canvas_out": "#2a2e3a", "checker_a": "#3a3f4f", "checker_b": "#454b5e", "hud": "#86efac",
}
LIGHT = {
    "window": "#eef0f4", "panel": "#ffffff", "base": "#ffffff", "border": "#d4d8e0",
    "text": "#23262e", "muted": "#6b7280", "hint": "#5b6270", "accent": "#0a6ec9", "accent_hover": "#0078d4",
    "on_accent": "#ffffff",
    "button": "#f7f8fb", "button_border": "#c3c9d4", "button_hover": "#eef1f6", "pressed": "#e2e6ee",
    "menu_bar": "#e4e7ec", "scroll": "#c2c8d2", "scroll_hover": "#aab2bf",
    "spin_btn": "#eef1f6", "spin_btn_hover": "#e2e6ee", "danger": "#dc2626",
    "row_active": "#dbe9fb", "thumb": "#eef0f4", "outline": "#0891b2",
    "canvas_out": "#9aa0ad", "checker_a": "#cfd3da", "checker_b": "#eef0f4", "hud": "#15803d",
}

_THEMES = {"dark": DARK, "light": LIGHT}
_current = "dark"

_TEMPLATE = """
* { font-family: "Microsoft YaHei UI","Segoe UI",sans-serif; font-size: 12px; }
QMainWindow, QWidget { background: @window@; color: @text@; }

QMenuBar { background: @menu_bar@; color: @text@; padding: 2px; }
QMenuBar::item { padding: 4px 10px; border-radius: 5px; }
QMenuBar::item:selected { background: @button_hover@; }
QMenu { background: @panel@; color: @text@; border: 1px solid @border@; padding: 4px; }
QMenu::item { padding: 5px 22px 5px 14px; border-radius: 5px; }
QMenu::item:selected { background: @accent@; color: #ffffff; }
QMenu::separator { height: 1px; background: @border@; margin: 4px 8px; }

QToolBar { background: @menu_bar@; border: none; spacing: 4px; padding: 8px 6px; }
QToolBar::separator:vertical { height: 1px; background: @border@; margin: 6px 8px; }
QToolBar::separator:horizontal { width: 1px; background: @border@; margin: 8px 6px; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 7px; }
QToolButton:hover { background: @button_hover@; }
QToolButton:checked { background: @row_active@; border: 1px solid @accent@; color: @accent@; }
QToolButton:disabled { color: @muted@; }
QToolButton:focus { border: 1px solid @accent@; }

QDockWidget { color: @text@; }
QDockWidget::title { background: @panel@; padding: 7px 12px; }

QGroupBox { background: @panel@; border: 1px solid @border@; border-radius: 9px; margin-top: 16px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: @muted@; }

QPushButton { background: @button@; border: 1px solid @button_border@; border-radius: 6px; padding: 6px 12px; color: @text@; }
QPushButton:hover { background: @button_hover@; }
QPushButton:pressed { background: @pressed@; }
QPushButton:focus { border-color: @accent@; }
QPushButton:disabled { color: @muted@; background: @button@; border-color: @border@; }
QPushButton[primary="true"] { background: @accent@; color: #ffffff; border: 1px solid @accent@; font-weight: 600; }
QPushButton[primary="true"]:hover { background: @accent_hover@; }
QPushButton[primary="true"]:pressed { background: @pressed@; }
QPushButton[danger="true"] { color: @danger@; }
QPushButton[danger="true"]:hover { background: @danger@; color: #ffffff; border-color: @danger@; }

QSpinBox, QDoubleSpinBox { background: @base@; border: 1px solid @button_border@; border-radius: 5px; padding: 4px 6px; color: @text@; }
QSpinBox:focus, QDoubleSpinBox:focus { border-color: @accent@; }
QSpinBox:disabled, QDoubleSpinBox:disabled { color: @muted@; background: @panel@; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { subcontrol-origin: border; width: 17px; background: @spin_btn@; border: none; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: @spin_btn_hover@; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(@chevron_up@); width: 9px; height: 9px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(@chevron@); width: 9px; height: 9px; }
QSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QDoubleSpinBox::up-button { subcontrol-position: top right; }
QDoubleSpinBox::down-button { subcontrol-position: bottom right; }

QLineEdit { background: @base@; border: 1px solid @button_border@; border-radius: 5px; padding: 4px 6px; color: @text@; }
QLineEdit:focus { border-color: @accent@; }
QLineEdit:disabled { color: @muted@; background: @panel@; }
QComboBox, QFontComboBox { background: @base@; border: 1px solid @button_border@; border-radius: 5px; padding: 3px 6px; color: @text@; }
QComboBox:focus, QFontComboBox:focus { border-color: @accent@; }
QComboBox:disabled { color: @muted@; background: @panel@; }
QComboBox::drop-down, QFontComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; border: none; width: 20px; }
QComboBox::down-arrow, QFontComboBox::down-arrow { image: url(@chevron@); width: 11px; height: 11px; }
QComboBox QAbstractItemView { background: @panel@; border: 1px solid @border@; selection-background-color: @accent@; selection-color: #ffffff; outline: none; }
QTextEdit { background: @base@; border: 1px solid @button_border@; border-radius: 5px; color: @text@; }
QTextEdit:focus { border: 1px solid @accent@; }
QCheckBox { spacing: 6px; color: @text@; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid @button_border@; border-radius: 4px; background: @base@; }
QCheckBox::indicator:checked { background: @accent@; border-color: @accent@; }
QCheckBox::indicator:disabled { border-color: @border@; background: @panel@; }

QListWidget { background: @base@; border: 1px solid @border@; border-radius: 7px; padding: 3px; outline: none; }
QListWidget::item { border-radius: 6px; margin: 1px; }
QListWidget::item:hover { background: @button_hover@; }
QListWidget::item:selected { background: @row_active@; }
QListWidget#layerList::item:selected { background: transparent; }
QListWidget#layerList::item:hover { background: transparent; }
QListWidget#assetGrid { background: @base@; }
QListWidget#assetGrid::item { border: 1px solid @border@; border-radius: 5px; background: @thumb@; }
QListWidget#assetGrid::item:hover { border-color: @accent@; }
QListWidget#assetGrid::item:selected { border: 1px solid @accent@; background: @row_active@; }

QProgressBar { background: @border@; border: none; border-radius: 4px; }
QProgressBar::chunk { background: @accent@; border-radius: 4px; }

QStatusBar { background: @menu_bar@; color: @muted@; }
QStatusBar::item { border: none; }
QLabel { background: transparent; color: @text@; }
QLabel#hint { color: @hint@; font-size: 11px; }
QLabel#sectionTitle { color: @text@; font-weight: 600; }
QLabel#countBadge { background: @button@; color: @muted@; border: 1px solid @border@; border-radius: 8px; padding: 1px 9px; font-size: 11px; }
QFrame#card { background: @panel@; border: 1px solid @border@; border-radius: 7px; }

/* —— 折叠区标题按钮（聊天/AI 面板「设置」头，统一一处，不再各文件内联）—— */
QToolButton#sectionToggle { border: none; font-weight: 600; padding: 2px; background: transparent; }
QToolButton#sectionToggle:hover { color: @accent@; }

/* —— 图层行 / 分组头：移出内联 setStyleSheet，主题切换自动重新着色（不再残留旧色）—— */
#layerRow { border-left: 3px solid transparent; border-radius: 6px; }
#layerRow[active="true"] { background: @row_active@; border-left: 3px solid @accent@; }
QLabel#layerThumb { background: @thumb@; border: 1px solid @border@; border-radius: 5px; }
#groupHeader { background: @row_active@; border-radius: 6px; }
QLabel#groupName { font-weight: 600; }

/* —— AI 浮窗的 ✨ 星按钮：用强调色 token，切主题随之变色 —— */
QToolButton#pluginStar { background: @accent@; color: @on_accent@; border: none; border-radius: 19px; font-size: 19px; }
QToolButton#pluginStar:hover { background: @accent_hover@; }

/* —— 聊天气泡：移到主题，切主题时已渲染气泡也随之换色 —— */
QLabel#bubbleUser { background: @accent@; color: @on_accent@; border-radius: 9px; border-bottom-right-radius: 3px; padding: 7px 11px; }
QLabel#bubbleAsst { background: @panel@; color: @text@; border: 1px solid @border@; border-radius: 9px; border-bottom-left-radius: 3px; padding: 7px 11px; }
QWidget#chatBg { background: @base@; }
QScrollArea#chatScroll { background: @base@; border: 1px solid @border@; border-radius: 7px; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: @scroll@; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: @scroll_hover@; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: @scroll@; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QToolTip { background: @panel@; color: @text@; border: 1px solid @accent@; padding: 4px 7px; border-radius: 5px; }
"""


_ADS_TEMPLATE = """
ads--CDockContainerWidget { background: @window@; }
ads--CDockContainerWidget QSplitter::handle { background: @border@; }
ads--CDockContainerWidget QSplitter::handle:hover { background: @accent@; }
ads--CDockAreaWidget { background: @panel@; border: 1px solid @border@; }
ads--CDockAreaTitleBar { background: @menu_bar@; border-bottom: 1px solid @border@; }
ads--CDockWidgetTab { background: @menu_bar@; border: none; padding: 3px 6px; }
ads--CDockWidgetTab[activeTab="true"] { background: @panel@; border-top: 2px solid @accent@; }
ads--CDockWidgetTab QLabel { color: @muted@; }
ads--CDockWidgetTab[activeTab="true"] QLabel { color: @text@; }
ads--CTitleBarButton { background: transparent; border: none; }
ads--CTitleBarButton:hover { background: @button_hover@; border-radius: 4px; }
ads--CDockWidget { background: @panel@; color: @text@; }
ads--CFloatingDockContainer { background: @window@; border: 1px solid @border@; }
"""


_qss_cache: dict = {}      # id(调色板) → 已替换好的完整 QSS；切主题时免每次重跑 25 次 str.replace
_ads_qss_cache: dict = {}


def ads_qss(c: dict) -> str:
    s = _ads_qss_cache.get(id(c))
    if s is None:
        s = _ADS_TEMPLATE
        for k, v in c.items():
            s = s.replace(f"@{k}@", v)
        _ads_qss_cache[id(c)] = s
    return s


def colors() -> dict:
    return _THEMES[_current]


def current() -> str:
    return _current


def _settings() -> QtCore.QSettings:
    return QtCore.QSettings("NanoPro", "SciEditQt")


def load_saved(default: str = "light") -> str:
    name = _settings().value("theme", default)
    return name if name in _THEMES else default


def save(name: str):
    _settings().setValue("theme", name)


def _qss(c: dict) -> str:
    s = _qss_cache.get(id(c))  # DARK/LIGHT 是模块级常量字典，id 稳定可作键
    if s is None:
        s = _TEMPLATE
        for k, v in c.items():
            s = s.replace(f"@{k}@", v)
        _qss_cache[id(c)] = s
    return s


def _chevron_urls(c: dict):
    """生成当前主题色的下/上箭头 PNG（缓存到 ~/.sciedit/cache），返回 (down_url, up_url) 正斜杠路径。失败返回 ('','')。
    深浅主题各一套，箭头色取 muted，深底/浅底都清晰。"""
    try:
        from pathlib import Path
        import icons
        cache = Path.home() / ".sciedit" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        col = c.get("muted", "#9aa0ad")
        pd = cache / f"chevron_{_current}.png"
        pu = cache / f"chevron_up_{_current}.png"
        if (icons.save_chevron_png(str(pd), col, 20, up=False)
                and icons.save_chevron_png(str(pu), col, 20, up=True)):
            return str(pd).replace("\\", "/"), str(pu).replace("\\", "/")
    except Exception:
        pass
    return "", ""


def apply(app: QtWidgets.QApplication, name: str = "dark"):
    global _current
    _current = name if name in _THEMES else "dark"
    c = colors()
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(c["window"]))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(c["text"]))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(c["base"]))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(c["panel"]))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(c["text"]))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(c["button"]))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(c["text"]))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(c["accent"]))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#ffffff"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(c["panel"]))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(c["text"]))
    # 禁用态色组：让禁用按钮/输入框文字仍可辨（否则 Fusion 兜底成与底色相近的浑浊灰）
    dis = QtGui.QColor(c["muted"]); dis.setAlpha(150)
    for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text, QtGui.QPalette.ColorRole.ButtonText):
        pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, dis)
    app.setPalette(pal)
    s = _qss(c)
    down, up = _chevron_urls(c)
    if down and up:  # 注入下拉/上下按钮箭头图标（可编辑下拉 + QSpinBox 上下按钮都有清晰箭头）
        s = s.replace("@chevron@", down).replace("@chevron_up@", up)
    else:   # 兜底：箭头生成失败 → 去掉会压制原生箭头的 arrow/drop-down 规则，让 Fusion 画原生箭头
        s = "\n".join(ln for ln in s.splitlines()
                      if "@chevron@" not in ln and "@chevron_up@" not in ln and "::drop-down" not in ln)
    app.setStyleSheet(s)
