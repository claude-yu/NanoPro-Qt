# 阶段 0 基线记录

日期：2026-05-31

## 保护现有 WebView 版
- 仓库 `../nanopro-editor`，分支 main，commit `82488e9`，tag/release **v1.4.0-beta**。
- `git status` 工作树**干净**（所有改动已提交+发布）→ 零丢失风险。
- `git diff --check`：无空白/冲突标记。
- **约定**：不删除/覆盖/重构 WebView 版；qt-prototype 与其完全隔离。

## 现有自动化测试
- `python -m unittest discover -s tests -v` → **9 tests, OK**（含打包契约测试 `test_canonical_pyinstaller_spec_is_not_ignored`）。

## 编译检查
- `python -m py_compile sciedit.py` ✓
- `node --check` app.js / features.js / panels.js / layers.js / ai.js / worker.js ✓（全过）

## Qt 原型依赖（阶段 0-4）
全部缺失，需安装（不静默失败，记录命令）：

| 依赖 | 状态 |
|---|---|
| PySide6 | ✗ 缺 |
| opencv-python (cv2) | ✗ 缺 |
| numpy | ✗ 缺 |
| pytest | ✗ 缺 |
| psutil | ✗ 缺 |

安装命令：
```
python -m pip install PySide6 opencv-python numpy pytest psutil
```

## 待办
- 安装上述依赖 → 进入阶段 1（WebView 基准）/ 阶段 2（Qt 最小原型）。
- 测试机配置（CPU/GPU/内存）在跑基准时补记，作为 FPS/耗时数据的参照。
