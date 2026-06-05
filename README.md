# SciEdit / NanoPro（Qt 版）

面向科研制图的桌面级图像编辑器，基于 **PySide6 + QGraphicsView + NumPy/OpenCV**。对标 Photoshop / BioRender 的核心工作流，内置 AI 生成、AI 抠图与本地离线抠图，所有 API Key 仅保存在本机、绝不进程序或仓库。

> 本仓库是 **Qt/PySide6 重写版**。早期的 WebView/pywebview 版本（`ai.js` / `app.js` / `sciedit.py`）是它的前身，二者为独立代码库。

---

## ✨ 功能

**图层系统**
- 显隐 / 层级 / 重命名 / 锁定 / 打组解组 / 不透明度
- **非破坏图层蒙版**：从选区生成，选区内露、外藏，原图像素不动，随时可删（画布显示 / 导出 / 缩略图三处同源）

**PS 式选区（连续工作流）**
- 套索 / 魔棒（按颜色）/ 选区画笔 / 矩形选框
- Shift 加选、Alt 减选；Ctrl 点图层载入该层像素；Ctrl+Shift+D 重新选择；Ctrl+J 经选区抠出
- 选区 → 抠出素材 / 生成蒙版 / 裁剪 / 挖洞

**绘制与文字**
- 画笔 / 橡皮（像素级，脏矩形局部刷新）
- 就地打字、拖框定宽、旋转、即时生效

**矢量（SVG）**
- 导入 SVG，元素级改色 / 改字 / 拖动 / 打组解组（导出 `<g>`）

**AI 生成（OpenAI 兼容 / grsai）**
- 文生图 / 图生图（参考来源：画布合成 / 当前图层 / 当前选区 / 外部图片 / 参考图库）
- 多中转站、国内/国外节点、可拉取/手填模型；并行任务队列；结果自动落图层

**AI 抠图 / 拆解**
- **本地离线**（内置 u2netp ONNX，无需联网、无需额外安装）
- grsai 图生图编辑 / PPIO Qwen-Image-Edit（异步）/ OpenAI image-edit 兼容中转 / 本地 rembg

**AI 对话**
- 接 DeepSeek / 智谱 GLM 等，把口语需求转成可直接用的英文绘图提示词（每商家记住各自模型）

**素材库**
- 抠出素材库（单击放回画布）
- 本地素材文件夹：子文件夹 = 分类（递归读取任意深度）、读 `manifest.json`、一键**生成分类索引**、**按分类导出**

**导出与工程**
- 导出 PNG / TIFF（写 300 DPI 元数据）
- 保存 / 加载工程 `.nanopro.json`（含图层、蒙版、矢量、素材）

**界面**
- 深 / 浅主题，Qt-ADS 停靠面板，撤销 / 重做历史，全局滚轮防误改参数

---

## 🔒 安全与隐私

- **API Key 只保存在本机** `~/.sciedit/config.json`（每个商家的 Key 相互隔离）。
- Key **绝不**进程序日志 / 仓库 / 导出图 / 工程文件 / 窗口布局；界面只回显掩码尾号。
- 打包产物（PyInstaller）只含源码 + 模型 + 运行库，**不打包任何 Key**；克隆者需各自填写自己的 Key。

---

## 🚀 从源码运行

需要 Python 3.13（3.11+ 应可用）。

```bash
pip install -r requirements.txt
python src/main.py
```

首次使用 AI 功能时，在面板「设置」里填入你自己的 API Key（仅存本机）。**AI 抠图可完全离线**：后端选「本地内置模型」，不用 Key、不用联网。

---

## 📦 打包（Windows）

```bash
# 1) PyInstaller 打成独立程序（onedir）→ dist/NanoPro/NanoPro.exe
python -m PyInstaller NanoPro.spec --noconfirm

# 2) Inno Setup 打成双击安装包 → Output/SciEdit_NanoPro_Setup_v1.exe
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" NanoPro_Setup.iss
```

PyInstaller 刻意排除了 torch/CUDA 等巨物（本地抠图仅用 onnxruntime CPU 推理），最终独立程序约 310 MB、安装包约 90 MB。

---

## 🧱 技术栈与结构

PySide6 6.11 · NumPy · OpenCV · onnxruntime · PySide6-QtAds · lxml · certifi

```
src/
  main.py            入口
  editor_window.py   主窗口 / 图层 / 选区 / 工具 / 工程存取
  canvas_view.py     QGraphicsView 画布交互
  layer_item.py      图层项（含非破坏蒙版）
  image_ops.py       选区 / 蒙版 / 合成等 NumPy/OpenCV 运算
  ai_client.py       AI 文生图/图生图客户端（grsai / OpenAI 兼容）
  ai_panel.py        AI 生成面板
  chat_client.py     AI 对话（提示词生成）
  chat_panel.py      对话面板
  seg_client.py      AI 抠图（本地 onnx / grsai / PPIO / rembg / HTTP）
  style_lib.py       参考图库 / manifest 扫描
  asset_lib.py       本地素材库扫描（分类 / 递归 / manifest）
  svg_io.py          SVG 导入导出
  config.py          本机配置 / Key（~/.sciedit）
  theme.py           深浅主题 QSS
  icons.py           矢量图标
  models/u2netp.onnx 本地抠图模型
NanoPro.spec         PyInstaller 配置
NanoPro_Setup.iss    Inno Setup 安装包配置
```

---

## 📝 许可

暂未指定开源许可（默认保留所有权利）。如需开源复用，请先与作者确认。
