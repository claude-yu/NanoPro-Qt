"""SciEdit Qt 原型入口。

运行：  python src/main.py
依赖：  PySide6, numpy, opencv-python（见 ../PLAN.md）
目标：  验证 PySide6 + QGraphicsView 在多图层/缩放/拖动下的交互流畅度，对照 WebView 版。
"""
import sys

from PySide6 import QtWidgets

import theme
from editor_window import EditorWindow


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("NanoPro")
    app.setApplicationName("SciEditQt")
    theme.apply(app, theme.load_saved("light"))  # 首次默认浅色，之后用记忆的
    win = EditorWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
