# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：SciEdit / NanoPro Qt 版 → 独立 Windows 程序（无需安装 Python）。
# 构建：  python -m PyInstaller NanoPro.spec --noconfirm
# 产物：  dist/NanoPro/NanoPro.exe（onedir，启动快、稳）
import os
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

SRC = os.path.join(os.getcwd(), "src")

# Qt-ADS（停靠面板）自带编译扩展 + dll、certifi 自带 cacert.pem、onnxruntime 原生 dll —— collect_all 一并打进去。
ads_datas, ads_bins, ads_hidden = collect_all("PySide6QtAds")
cert_datas, cert_bins, cert_hidden = collect_all("certifi")
# 只拿 onnxruntime 的原生推理 dll；【绝不】用 collect_all——它会把 onnxruntime.training/transformers 等
# 可选子模块当 hidden import 拖进来，进而把 torch + 整套 CUDA(几个 GB) 打进包。CPU 纯推理用不到 torch。
ort_bins = collect_dynamic_libs("onnxruntime")

a = Analysis(
    [os.path.join("src", "main.py")],
    pathex=[SRC],
    binaries=ads_bins + cert_bins + ort_bins,
    # 内置抠图模型 u2netp.onnx → 打到 _MEIPASS/models/（seg_client._local_model_path 读它）
    datas=ads_datas + cert_datas + [(os.path.join("src", "models", "u2netp.onnx"), "models")],
    # certifi/onnxruntime 是惰性 import（PyInstaller 静态分析可能漏）→ 显式列出；cv2 同理
    hiddenimports=["cv2", "certifi", "PySide6QtAds", "onnxruntime", "onnxruntime.capi._pybind_state"] + ads_hidden + cert_hidden,
    hookspath=[],
    runtime_hooks=[],
    # 砍掉确定用不到的大块 Qt 模块，减体积、加快启动（QtSvg/QtSvgWidgets 要保留——矢量渲染在用）
    excludes=[
        "tkinter",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
        "PySide6.QtSerialPort", "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtHelp",
        "PySide6.QtDesigner", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtRemoteObjects",
        # onnxruntime 现在要打进来(本地内置抠图)，但【排除】它会牵出的巨物：torch + CUDA(几个 GB)、numba/llvmlite。
        # CPU 纯 InferenceSession.run 用不到这些；它们只被 onnxruntime 的训练/transformers/量化等可选子模块用。
        "torch", "torchvision", "torchaudio", "numba", "llvmlite",
        "onnxruntime.training", "onnxruntime.transformers", "onnxruntime.quantization", "onnxruntime.tools",
        "transformers", "rembg", "matplotlib", "scipy", "pandas", "skimage", "scikit-image", "pymatting",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NanoPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # 窗口程序，无黑色控制台
    icon="NanoPro.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="NanoPro",
)
