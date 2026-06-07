"""主题：深色 / 浅色双主题（QSS 模板 + 调色板），可运行时切换。

颜色集中在 DARK/LIGHT 两个字典；QSS 用 @token@ 占位符，apply() 时整体替换。
其它模块（canvas_view/LayerRow）通过 colors() 读当前主题色，切换后重绘即自适应。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 调色板：默认走 Photoshop 式 graphite 工作台。重点是低饱和、低圆角、细分隔线，
# 避免 Qt 表单的白按钮/大卡片/高对比边框带来的廉价感。
# on_accent = 强调底（按钮/气泡/星）上的文字色。
DARK = {
    "window": "#252525", "panel": "#3a3a3a", "base": "#303030", "border": "#4a4a4a",
    "surface_raised": "#444444", "surface_sunken": "#2b2b2b", "hairline": "#555555", "focus_ring": "#8ab4f8",
    "text": "#dedede", "muted": "#a9a9a9", "hint": "#b8b8b8", "accent": "#4a90e2", "accent_hover": "#5a9be8",
    "on_accent": "#ffffff",
    "button": "#444444", "button_border": "#5a5a5a", "button_hover": "#505050", "pressed": "#383838",
    "menu_bar": "#4a4a4a", "scroll": "#5f5f5f", "scroll_hover": "#737373",
    "spin_btn": "#3a3a3a", "spin_btn_hover": "#4a4a4a", "danger": "#e05858",
    "row_active": "#5a5a5a", "thumb": "#2f2f2f", "outline": "#8ab4f8",
    "canvas_out": "#202020", "canvas_border": "#707070", "canvas_shadow": "#121212",
    "checker_a": "#c9c9c9", "checker_b": "#eeeeee", "smart_guide": "#d66bff",
    "measure": "#ff3b9d", "connector_anchor": "#3b82f6", "hud": "#86efac",
    "ihc_accent": "#c06a2a",
}
LIGHT = {
    "window": "#c8c8c8", "panel": "#dedede", "base": "#eeeeee", "border": "#b7b7b7",
    "surface_raised": "#d8d8d8", "surface_sunken": "#c2c2c2", "hairline": "#aaaaaa", "focus_ring": "#2f74c0",
    "text": "#202020", "muted": "#5f5f5f", "hint": "#4f4f4f", "accent": "#2f74c0", "accent_hover": "#3f83cf",
    "on_accent": "#ffffff",
    "button": "#d6d6d6", "button_border": "#a8a8a8", "button_hover": "#e2e2e2", "pressed": "#c5c5c5",
    "menu_bar": "#d0d0d0", "scroll": "#9b9b9b", "scroll_hover": "#838383",
    "spin_btn": "#d0d0d0", "spin_btn_hover": "#dedede", "danger": "#c93f3f",
    "row_active": "#b9cde5", "thumb": "#eeeeee", "outline": "#2f74c0",
    "canvas_out": "#b0b0b0", "canvas_border": "#f6f6f6", "canvas_shadow": "#7a7a7a",
    "checker_a": "#bdbdbd", "checker_b": "#e8e8e8", "smart_guide": "#a020d0",
    "measure": "#db2777", "connector_anchor": "#2563eb", "hud": "#15803d",
    "ihc_accent": "#a65f22",
}

_THEMES = {"dark": DARK, "light": LIGHT}
_current = "dark"

_TEMPLATE = """
* { font-family: "Microsoft YaHei UI","Segoe UI",sans-serif; font-size: 11px; }
QMainWindow, QWidget { background: @window@; color: @text@; }

QMenuBar { background: @menu_bar@; color: @text@; padding: 1px; border-bottom: 1px solid #343434; }
QMenuBar::item { padding: 4px 9px; border-radius: 2px; }
QMenuBar::item:selected { background: @button_hover@; }
QMenu { background: @surface_raised@; color: @text@; border: 1px solid @hairline@; padding: 3px; }
QMenu::item { padding: 5px 22px 5px 14px; border-radius: 2px; }
QMenu::item:selected { background: @accent@; color: #ffffff; }
QMenu::item:disabled { color: @muted@; background: transparent; font-weight: 600; padding-top: 7px; padding-bottom: 2px; }
QMenu::item[danger="true"] { color: @danger@; }
QMenu::separator { height: 1px; background: @border@; margin: 4px 8px; }

QToolBar { background: @menu_bar@; border: none; spacing: 3px; padding: 4px 5px; }
QToolBar::separator:vertical { height: 1px; background: @border@; margin: 6px 8px; }
QToolBar::separator:horizontal { width: 1px; background: @border@; margin: 8px 6px; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 2px; padding: 5px; }
QToolButton:hover { background: @button_hover@; }
QToolButton:checked { background: @row_active@; border: 1px solid @accent@; color: @accent@; }
QToolButton:disabled { color: @muted@; }
QToolButton:focus { border: 1px solid @focus_ring@; }

QToolBar#leftToolBar { background: @menu_bar@; border-right: 1px solid #343434; padding: 3px 2px; spacing: 1px; }
QToolBar#leftToolBar QToolButton { min-width: 24px; min-height: 24px; max-width: 24px; max-height: 24px; padding: 2px; border-radius: 2px; }
QToolBar#leftToolBar QToolButton:hover { background: @surface_raised@; border-color: @hairline@; }
QToolBar#leftToolBar QToolButton:checked { background: @row_active@; border-color: @accent@; }
QToolBar#leftToolBar::separator:horizontal { width: 26px; height: 1px; background: @hairline@; margin: 6px 3px; }
QToolButton#flyoutToolButton { min-width: 24px; min-height: 24px; max-width: 24px; max-height: 24px; }

QToolBar#optionsBar { background: @menu_bar@; border-bottom: 1px solid #343434; padding: 3px 8px; spacing: 5px; }
QToolBar#optionsBar QToolButton { min-height: 22px; padding: 2px 6px; border-radius: 2px; }
QToolBar#optionsBar QToolButton:hover { background: @button_hover@; border-color: @hairline@; }
QToolBar#optionsBar QToolButton:checked { background: @row_active@; border-color: @accent@; color: @accent@; }
QFrame#optionsShell { background: @menu_bar@; border: none; border-radius: 0; min-height: 28px; }
QFrame#optionDivider { background: @hairline@; border: none; max-width: 1px; }
QLabel#optionToolIcon { background: transparent; border: none; border-radius: 0; }
QLabel#optionToolTitle { color: @text@; font-weight: 700; padding: 0 2px; }
QLabel#optionLabel { color: @muted@; font-size: 11px; }
QStackedWidget#optionsStack { background: transparent; border: none; }
QWidget#optionPage { background: transparent; }
QToolButton#optionButton { background: @button@; border: 1px solid @button_border@; border-radius: 2px; padding: 2px 7px; }
QToolButton#optionButton:hover { background: @button_hover@; border-color: @accent@; }
QToolButton#optionButton:checked { background: @row_active@; border-color: @accent@; color: @accent@; }

QDockWidget { color: @text@; border: 1px solid #333333; }
QDockWidget::title { background: @surface_raised@; padding: 5px 9px; border-bottom: 1px solid #333333; }

QDialog, QMessageBox { background: @surface_raised@; color: @text@; }
QDialog#toolDialog { border: 1px solid @hairline@; border-radius: 3px; }
QMessageBox QLabel { color: @text@; background: transparent; }
QDialogButtonBox QPushButton { min-width: 78px; min-height: 27px; }

QGroupBox { background: @panel@; border: 1px solid @hairline@; border-radius: 2px; margin-top: 14px; padding: 8px 7px 7px 7px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: @hint@; }

QPushButton { background: @button@; border: 1px solid @button_border@; border-radius: 2px; padding: 5px 10px; color: @text@; }
QPushButton:hover { background: @button_hover@; }
QPushButton:pressed { background: @pressed@; }
QPushButton:focus { border-color: @focus_ring@; }
QPushButton:disabled { color: @muted@; background: @button@; border-color: @border@; }
QPushButton::icon { padding-right: 4px; }
QPushButton[primary="true"] { background: @button@; color: @text@; border: 1px solid @focus_ring@; font-weight: 600; }
QPushButton[primary="true"]:hover { background: @button_hover@; color: @text@; }
QPushButton[primary="true"]:pressed { background: @pressed@; }
QPushButton[danger="true"] { color: @danger@; }
QPushButton[danger="true"]:hover { background: @danger@; color: #ffffff; border-color: @danger@; }

QSpinBox, QDoubleSpinBox { background: @base@; border: 1px solid @button_border@; border-radius: 2px; padding: 3px 6px; color: @text@; }
QSpinBox:focus, QDoubleSpinBox:focus { border-color: @focus_ring@; }
QSpinBox:disabled, QDoubleSpinBox:disabled { color: @muted@; background: @panel@; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { subcontrol-origin: border; width: 17px; background: @spin_btn@; border: none; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: @spin_btn_hover@; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(@chevron_up@); width: 9px; height: 9px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(@chevron@); width: 9px; height: 9px; }
QSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QDoubleSpinBox::up-button { subcontrol-position: top right; }
QDoubleSpinBox::down-button { subcontrol-position: bottom right; }

QLineEdit { background: @base@; border: 1px solid @button_border@; border-radius: 2px; padding: 3px 6px; color: @text@; }
QLineEdit:focus { border-color: @focus_ring@; }
QLineEdit:disabled { color: @muted@; background: @panel@; }
QComboBox, QFontComboBox { background: @base@; border: 1px solid @button_border@; border-radius: 2px; padding: 2px 6px; color: @text@; }
QComboBox:focus, QFontComboBox:focus { border-color: @focus_ring@; }
QComboBox:disabled { color: @muted@; background: @panel@; }
QComboBox::drop-down, QFontComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; border: none; width: 20px; }
QComboBox::down-arrow, QFontComboBox::down-arrow { image: url(@chevron@); width: 11px; height: 11px; }
QComboBox QAbstractItemView { background: @panel@; border: 1px solid @border@; selection-background-color: @accent@; selection-color: #ffffff; outline: none; }
QTextEdit { background: @base@; border: 1px solid @button_border@; border-radius: 2px; color: @text@; }
QTextEdit:focus { border: 1px solid @focus_ring@; }
QCheckBox { spacing: 6px; color: @text@; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid @button_border@; border-radius: 4px; background: @base@; }
QCheckBox::indicator:checked { background: @accent@; border-color: @accent@; }
QCheckBox::indicator:disabled { border-color: @border@; background: @panel@; }
QRadioButton { spacing: 6px; color: @text@; }
QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid @button_border@; border-radius: 8px; background: @base@; }
QRadioButton::indicator:checked { background: @accent@; border: 3px solid @base@; }
QSlider::groove:horizontal { height: 5px; background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @accent@; border-radius: 3px; }
QSlider::handle:horizontal { width: 13px; height: 13px; margin: -5px 0; border-radius: 7px; background: @surface_raised@; border: 1px solid @focus_ring@; }
QSlider::handle:horizontal:hover { background: @button_hover@; }

QListWidget { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 2px; outline: none; }
QListWidget::item { border-radius: 2px; margin: 1px; }
QListWidget::item:hover { background: @button_hover@; }
QListWidget::item:selected { background: @row_active@; }
QListWidget#layerList::item:selected { background: transparent; }
QListWidget#layerList::item:hover { background: transparent; }
QListWidget#assetGrid { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 4px; outline: none; }
QListWidget#assetGrid:focus { border-color: @focus_ring@; }
QListWidget#assetGrid::item { border: 1px solid @hairline@; border-radius: 2px; background: @thumb@; margin: 2px; }
QListWidget#assetGrid::item:hover { border-color: @focus_ring@; background: @button_hover@; }
QListWidget#assetGrid::item:selected { border: 1px solid @focus_ring@; background: @button_hover@; }
QListWidget#assetGrid::item:disabled { border: none; background: transparent; color: @hint@; margin: 7px 2px 2px 2px; padding-left: 2px; }
QTreeWidget#assetTree { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 3px; outline: none; }
QTreeWidget#assetTree:focus { border-color: @focus_ring@; }
QTreeWidget#assetTree::item { min-height: 22px; padding: 2px 5px; border-radius: 2px; }
QTreeWidget#assetTree::item:hover { background: @button_hover@; }
QTreeWidget#assetTree::item:selected { background: @row_active@; color: @text@; }
QLabel#assetPath { color: @text@; font-weight: 600; padding: 2px 1px; }
QLineEdit#assetSearch { background: @surface_raised@; border-radius: 2px; padding: 4px 7px; min-height: 18px; }
QToolButton#assetGear { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; padding: 3px; }
QToolButton#assetGear:hover { background: @button_hover@; border-color: @accent@; }
QFrame#assetHeader { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; }
QFrame#assetPreview { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; }
QLabel#assetPreviewThumb { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 5px; min-height: 156px; }
QLabel#assetPreviewName { color: @text@; font-weight: 600; }
QLabel#assetPreviewMeta { color: @muted@; font-size: 11px; }
QLabel#assetPreviewPath { color: @hint@; font-size: 11px; }
QToolButton#assetPreviewAction { background: @button@; border: 1px solid @button_border@; border-radius: 2px; padding: 3px; }
QToolButton#assetPreviewAction:hover { background: @button_hover@; border-color: @accent@; }
QToolButton#assetPreviewAction:disabled { background: @surface_sunken@; color: @muted@; border-color: @hairline@; }
QPushButton#assetPreviewPrimary { min-height: 26px; border-radius: 2px; }
QToolButton#iconButton { background: @button@; border: 1px solid @button_border@; border-radius: 2px; padding: 3px; }
QToolButton#iconButton:hover { background: @button_hover@; border-color: @accent@; }
QToolButton#iconButton:checked { background: @row_active@; border-color: @accent@; }
QToolButton#iconButton[danger="true"]:hover { background: @danger@; border-color: @danger@; }

/* 素材库顶部分段 Tab（本地库 / 抠出素材）—— BioRender 式 pill 分段 */
QTabBar#assetTabs { qproperty-drawBase: 0; }
QTabBar#assetTabs::tab {
  background: @panel@; color: @muted@; border: 1px solid @border@;
  padding: 5px 9px; margin-right: 2px; border-radius: 2px; min-width: 60px; font-size: 11px;
}
QTabBar#assetTabs::tab:hover { background: @button_hover@; color: @text@; }
QTabBar#assetTabs::tab:selected { background: @row_active@; color: @accent@; border-color: @accent@; font-weight: 600; }

QProgressBar { background: @border@; border: none; border-radius: 4px; }
QProgressBar::chunk { background: @accent@; border-radius: 4px; }

QStatusBar { background: @menu_bar@; color: @muted@; border-top: 1px solid @hairline@; }
QStatusBar::item { border: none; }
QLabel#statusDoc { color: @text@; padding: 0 8px; font-weight: 600; }
QLabel#statusOp { color: @hint@; background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 1px 8px; }
QLabel#statusMeta, QLabel#statusZoom, QLabel#statusLayers { color: @muted@; padding: 0 6px; }
QLabel#statusLayers { color: @hint@; background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; padding: 1px 7px; }
QLabel#statusZoom { font-family: "Consolas","Menlo",monospace; font-weight: 600; color: @text@; }
QToolButton#statusZoomBtn { background: transparent; border: 1px solid transparent; border-radius: 2px; padding: 1px 4px; }
QToolButton#statusZoomBtn:hover { background: @button_hover@; border-color: @hairline@; }
QLabel { background: transparent; color: @text@; }
QLabel#hint { color: @hint@; font-size: 11px; }
QLabel#sectionTitle { color: @text@; font-weight: 600; }
QLabel#countBadge { background: @button@; color: @muted@; border: 1px solid @border@; border-radius: 2px; padding: 1px 8px; font-size: 11px; }
QFrame#card { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; }
QFrame#taskRow { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; }
QFrame#taskRow:hover { border-color: @focus_ring@; background: @button_hover@; }
QLabel#taskThumb { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; }
QFrame#emptyState { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 2px; }
QLabel#emptyIcon { color: @muted@; }
QLabel#emptyTitle { color: @text@; font-weight: 600; }
QLabel#emptyDetail { color: @muted@; font-size: 11px; }
QFrame#guideBanner { background: @row_active@; border: 1px solid @hairline@; border-left: 3px solid @accent@; border-radius: 2px; }
QLabel#guideText { color: @text@; font-size: 11px; }
QPushButton#guideAction { background: @button@; color: @text@; border: 1px solid @focus_ring@; border-radius: 2px; padding: 4px 10px; font-weight: 600; }
QPushButton#guideAction:hover { background: @button_hover@; }
QToolButton#guideClose { color: @muted@; border: 1px solid transparent; border-radius: 2px; padding: 1px 5px; font-weight: 700; }
QToolButton#guideClose:hover { color: @text@; background: @button_hover@; border-color: @hairline@; }

QDialog#progressSheet { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 3px; }
QDialog#progressSheet QLabel#progressTitle { color: @text@; font-weight: 700; font-size: 13px; }
QDialog#progressSheet QLabel#progressDetail { color: @hint@; }
QDialog#progressSheet QLabel#progressCounter { color: @muted@; font-family: "Consolas","Menlo",monospace; }
QDialog#progressSheet QProgressBar#progressBar { background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 2px; min-height: 8px; }
QDialog#progressSheet QProgressBar#progressBar::chunk { background: @accent@; border-radius: 1px; }

/* —— 折叠区标题按钮（聊天/AI 面板「设置」头，统一一处，不再各文件内联）—— */
QToolButton#sectionToggle { border: none; font-weight: 600; padding: 2px; background: transparent; }
QToolButton#sectionToggle:hover { color: @accent@; }

/* —— 图层行 / 分组头：移出内联 setStyleSheet，主题切换自动重新着色（不再残留旧色）—— */
#layerList { outline: none; }
#layerList:focus { border-color: @accent@; }
#layerRow { background: transparent; border: 1px solid transparent; border-left: 2px solid transparent; border-radius: 2px; }
#layerRow:hover { background: @button_hover@; border-color: @button_border@; }
#layerRow[active="true"] { background: @row_active@; border-color: @button_border@; border-left: 2px solid @accent@; }
#layerRow[dragging="true"] { background: @surface_raised@; border-color: @focus_ring@; }
QLabel#layerThumb { background: @thumb@; border: 1px solid @border@; border-radius: 2px; }
QFrame#layerControlDeck { background: @panel@; border: 1px solid @hairline@; border-radius: 2px; }
QLabel#layerControlLabel { color: @text@; font-weight: 600; }
QLabel#layerOpacityValue { color: @text@; font-family: "Consolas","Menlo",monospace; font-weight: 600; }
QSlider#layerOpacitySlider::groove:horizontal { height: 4px; background: @surface_sunken@; border: 1px solid @hairline@; border-radius: 1px; }
QSlider#layerOpacitySlider::sub-page:horizontal { background: @accent@; border-radius: 1px; }
QSlider#layerOpacitySlider::handle:horizontal { width: 11px; height: 11px; margin: -5px 0; border-radius: 2px; background: @surface_raised@; border: 1px solid @focus_ring@; }
QSlider#layerOpacitySlider::handle:horizontal:hover { background: @button_hover@; }
QToolButton#layerLock { border: 1px solid transparent; border-radius: 2px; padding: 3px; }
QToolButton#layerLock:hover { background: @button_hover@; border-color: @button_border@; }
QToolButton#layerLock:checked { background: @row_active@; border-color: @accent@; }
QToolButton#layerAction { color: @muted@; border: 1px solid transparent; border-radius: 2px; padding: 2px; font-size: 10px; }
QToolButton#layerAction:hover { background: @button_hover@; color: @text@; border-color: @button_border@; }
QToolButton#layerAction[danger="true"]:hover { background: @danger@; border-color: @danger@; }
QPushButton#layerExportButton { min-height: 27px; }
#groupHeader { background: @surface_raised@; border: 1px solid @hairline@; border-left: 2px solid transparent; border-radius: 2px; }
#groupHeader:hover { background: @button_hover@; }
#groupHeader[active="true"] { background: @row_active@; border-left: 2px solid @accent@; }
QLabel#groupName { font-weight: 600; }
QLabel#groupMeta { color: @muted@; font-size: 11px; }
QFrame#layerDropLine { background: @focus_ring@; border: none; border-radius: 1px; }
QLabel#layerDragHint { background: @surface_raised@; color: @text@; border: 1px solid @focus_ring@; border-radius: 2px; padding: 4px 8px; font-weight: 600; }

QFrame#canvasEmptyOverlay { background: @surface_raised@; border: 1px solid @hairline@; border-radius: 3px; }
QLabel#canvasEmptyTitle { color: @text@; font-size: 14px; font-weight: 700; }
QLabel#canvasEmptyDetail { color: @muted@; font-size: 11px; }
QPushButton#canvasEmptyPrimary { background: @button@; color: @text@; border-color: @focus_ring@; font-weight: 600; min-height: 28px; }
QPushButton#canvasEmptyPrimary:hover { background: @button_hover@; }
QPushButton#canvasEmptyButton { min-height: 28px; }

/* —— WB 灰度定量面板：Adobe/PS 式紧凑工具区 + 结果面板 —— */
QDialog#wbBatchDialog { background: @window@; }
QFrame#wbTopBar, QFrame#wbActionBar {
  background: @surface_raised@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QLabel#wbPanelTitle {
  color: @text@;
  font-weight: 700;
  padding: 0 8px 0 2px;
}
QLabel#wbFieldLabel {
  color: @muted@;
  font-size: 11px;
  padding-left: 4px;
}
QToolButton#wbZoomButton {
  background: @button@;
  border: 1px solid @button_border@;
  border-radius: 2px;
  padding: 3px 6px;
  min-width: 24px;
  min-height: 23px;
}
QToolButton#wbZoomButton:hover {
  background: @button_hover@;
  border-color: @focus_ring@;
}
QGraphicsView#wbImageView {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QFrame#wbResultsPanel {
  background: @panel@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QLabel#wbSectionTitle {
  color: @text@;
  font-weight: 700;
}
QLabel#wbTag {
  background: @surface_sunken@;
  color: @hint@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  padding: 2px 7px;
  font-size: 10px;
}
QTableWidget#wbTable {
  background: @surface_sunken@;
  alternate-background-color: @panel@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  gridline-color: @border@;
  selection-background-color: @row_active@;
  selection-color: @text@;
  outline: none;
}
QTableWidget#wbTable::item {
  padding: 4px 6px;
  border: none;
}
QTableWidget#wbTable QHeaderView::section {
  background: @surface_raised@;
  color: @hint@;
  border: none;
  border-right: 1px solid @hairline@;
  border-bottom: 1px solid @hairline@;
  padding: 5px 6px;
  font-weight: 600;
}
QLabel#analysisEmpty {
  background: @surface_sunken@;
  color: @muted@;
  border: 1px dashed @border@;
  border-left: 3px solid @accent@;
  border-radius: 2px;
  padding: 7px 9px;
  font-size: 11px;
}
QWidget#wbPlot {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QLabel#wbStatus {
  background: @surface_sunken@;
  color: @hint@;
  border: 1px solid @hairline@;
  border-left: 3px solid @accent@;
  border-radius: 2px;
  padding: 6px 9px;
  font-size: 11px;
}
QSplitter#wbSplit::handle {
  background: @hairline@;
}
QSplitter#wbSplit::handle:hover {
  background: @focus_ring@;
}
QSplitter#wbVSplit::handle:vertical {
  background: @panel@;
  border-top: 1px solid @hairline@;
  border-bottom: 1px solid @hairline@;
}
QSplitter#wbVSplit::handle:vertical:hover {
  background: @focus_ring@;
}
QListWidget#wbBatchList {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  padding: 3px;
  outline: none;
}
QListWidget#wbBatchList::item {
  min-height: 24px;
  padding: 3px 6px;
  border-radius: 2px;
}
QListWidget#wbBatchList::item:hover {
  background: @button_hover@;
}
QListWidget#wbBatchList::item:selected {
  background: @row_active@;
  color: @text@;
}

/* —— IHC / HE / 组织化学定量：同一工作台语言，独立语义选择器 —— */
QDialog#ihcBatchDialog { background: @window@; }
QFrame#ihcTopBar, QFrame#ihcActionBar {
  background: @surface_raised@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QLabel#ihcPanelTitle {
  color: @text@;
  font-weight: 700;
  padding: 0 8px 0 2px;
}
QLabel#ihcFieldLabel {
  color: @muted@;
  font-size: 11px;
  padding-left: 4px;
}
QToolButton#ihcToolButton {
  background: @button@;
  border: 1px solid @button_border@;
  border-radius: 2px;
  padding: 3px 6px;
  min-width: 24px;
  min-height: 23px;
}
QToolButton#ihcToolButton:hover {
  background: @button_hover@;
  border-color: @ihc_accent@;
}
QGraphicsView#ihcImageView {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QFrame#ihcResultsPanel,
QFrame#ihcPreviewPanel {
  background: @panel@;
  border: 1px solid @hairline@;
  border-radius: 2px;
}
QLabel#ihcSectionTitle {
  color: @text@;
  font-weight: 700;
}
QLabel#ihcTag {
  background: @surface_sunken@;
  color: @hint@;
  border: 1px solid @hairline@;
  border-left: 3px solid @ihc_accent@;
  border-radius: 2px;
  padding: 2px 7px;
  font-size: 10px;
}
QLabel#ihcBannerInfo {
  background: @surface_sunken@;
  color: @text@;
  border: 1px solid @hairline@;
  border-left: 3px solid @ihc_accent@;
  border-radius: 2px;
  padding: 6px 8px;
  font-size: 11px;
}
QTableWidget#ihcTable {
  background: @surface_sunken@;
  alternate-background-color: @panel@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  gridline-color: @border@;
  selection-background-color: @row_active@;
  selection-color: @text@;
  outline: none;
}
QTableWidget#ihcTable::item {
  padding: 4px 6px;
  border: none;
}
QTableWidget#ihcTable QHeaderView::section {
  background: @surface_raised@;
  color: @hint@;
  border: none;
  border-right: 1px solid @hairline@;
  border-bottom: 1px solid @hairline@;
  padding: 5px 6px;
  font-weight: 600;
}
QLabel#ihcStatus {
  background: @surface_sunken@;
  color: @hint@;
  border: 1px solid @hairline@;
  border-left: 3px solid @ihc_accent@;
  border-radius: 2px;
  padding: 6px 9px;
  font-size: 11px;
}
QSplitter#ihcSplit::handle {
  background: @hairline@;
}
QSplitter#ihcSplit::handle:hover {
  background: @ihc_accent@;
}
QListWidget#ihcBatchList {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  padding: 3px;
  outline: none;
}
QListWidget#ihcBatchList::item {
  min-height: 24px;
  padding: 3px 6px;
  border-radius: 2px;
}
QListWidget#ihcBatchList::item:hover {
  background: @button_hover@;
}
QListWidget#ihcBatchList::item:selected {
  background: @row_active@;
  color: @text@;
}
QProgressBar#ihcProgress {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  min-height: 8px;
}
QProgressBar#ihcProgress::chunk {
  background: @ihc_accent@;
  border-radius: 1px;
}

/* —— AI 任务面板：任务行文字/状态不再用内联 style，主题切换一致 —— */
QLabel#librarySectionTitle,
QLabel#taskSectionTitle {
  color: @text@;
  font-weight: 700;
  padding-top: 5px;
}
QLabel#taskTitle {
  color: @text@;
  font-size: 11px;
  font-weight: 600;
}
QLabel#taskMeta,
QLabel#taskProgressText,
QLabel#taskState {
  color: @muted@;
  font-size: 10px;
}
QLabel#taskState[error="true"],
QLabel#aiStatus[error="true"] {
  color: @danger@;
}
QLabel#aiStatus {
  color: @hint@;
  font-size: 11px;
}
QProgressBar#taskProgress {
  background: @surface_sunken@;
  border: 1px solid @hairline@;
  border-radius: 2px;
  min-height: 6px;
  max-height: 6px;
}
QProgressBar#taskProgress::chunk {
  background: @accent@;
  border-radius: 1px;
}

/* —— AI 浮窗的 ✨ 星按钮：用强调色 token，切主题随之变色 —— */
QToolButton#pluginStar { background: @accent@; color: #ffd84a; border: 1px solid @focus_ring@; border-radius: 2px; font-size: 20px; font-weight: 800; font-family: "Segoe UI","Microsoft YaHei UI",sans-serif; }
QToolButton#pluginStar:hover { background: @accent_hover@; border-color: #ffd84a; color: #ffe477; }

/* —— 聊天气泡：移到主题，切主题时已渲染气泡也随之换色 —— */
QLabel#bubbleUser { background: @accent@; color: @on_accent@; border-radius: 3px; padding: 6px 10px; }
QLabel#bubbleAsst { background: @panel@; color: @text@; border: 1px solid @border@; border-radius: 3px; padding: 6px 10px; }
QWidget#chatBg { background: @base@; }
QScrollArea#chatScroll { background: @base@; border: 1px solid @border@; border-radius: 2px; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: @scroll@; border-radius: 2px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: @scroll_hover@; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: @scroll@; border-radius: 2px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QToolTip { background: @panel@; color: @text@; border: 1px solid @accent@; padding: 4px 7px; border-radius: 2px; }
QLabel#toast { background: @panel@; color: @text@; border: 1px solid @accent@; border-radius: 3px; padding: 7px 12px; font-weight: 600; }
"""


_ADS_TEMPLATE = """
ads--CDockContainerWidget { background: @window@; }
ads--CDockContainerWidget QSplitter::handle { background: @hairline@; }
ads--CDockContainerWidget QSplitter::handle:hover { background: @focus_ring@; }
ads--CDockAreaWidget { background: @panel@; border: 1px solid #333333; }
ads--CDockAreaTitleBar { background: @surface_raised@; border-bottom: 1px solid #333333; }
ads--CDockWidgetTab { background: @surface_raised@; border: none; border-radius: 2px; padding: 3px 8px; margin: 1px; }
ads--CDockWidgetTab:hover { background: @button_hover@; }
ads--CDockWidgetTab[activeTab="true"] { background: @panel@; border-top: 2px solid @accent@; }
ads--CDockWidgetTab QLabel { color: @muted@; }
ads--CDockWidgetTab[activeTab="true"] QLabel { color: @text@; }
ads--CTitleBarButton { background: transparent; border: none; }
ads--CTitleBarButton:hover { background: @button_hover@; border-radius: 2px; }
ads--CDockWidget { background: @panel@; color: @text@; }
ads--CFloatingDockContainer { background: @window@; border: 1px solid @focus_ring@; }
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
