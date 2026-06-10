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
# WB 凝胶分析依赖 scipy.signal（find_peaks/savgol，含 Cython 扩展）——collect_all 拿全 dll/数据，防打包后 ImportError。
scipy_datas, scipy_bins, scipy_hidden = collect_all("scipy")
# 只拿 onnxruntime 的原生推理 dll；【绝不】用 collect_all——它会把 onnxruntime.training/transformers 等
# 可选子模块当 hidden import 拖进来，进而把 torch + 整套 CUDA(几个 GB) 打进包。CPU 纯推理用不到 torch。
ort_bins = collect_dynamic_libs("onnxruntime")
# 图像描摹/矢量化：vtracer 是 Rust PyO3 原生扩展（vtracer.cpXX-win_amd64.pyd），__init__ 从 .vtracer 子模块导入实现。
# collect_all 一并拿 .pyd + dist-info(含 MIT LICENSE，顺带满足分发声明)；不收则冻结包 import vtracer 失败 → 静默永久降级自研引擎。
# vtracer wheel 仅 cp310–314 win_amd64：构建解释器越界会让 collect_all 抛隐晦异常 → 显式断言成 fail-loud 报错。
import sys as _sys
assert (3, 10) <= _sys.version_info[:2] <= (3, 14), \
    f"vtracer wheel 仅支持 Python 3.10–3.14（cp310-314 win_amd64），当前 {_sys.version}"
vtracer_datas, vtracer_bins, vtracer_hidden = collect_all("vtracer")

a = Analysis(
    [os.path.join("src", "main.py")],
    pathex=[SRC],
    binaries=ads_bins + cert_bins + ort_bins + scipy_bins + vtracer_bins,
    # 内置抠图模型 u2netp.onnx → 打到 _MEIPASS/models/（seg_client._local_model_path 读它）
    datas=ads_datas + cert_datas + scipy_datas + vtracer_datas + [(os.path.join("src", "models", "u2netp.onnx"), "models"),
                                                  ("NanoPro.ico", "."),  # 运行期 setWindowIcon 读 _MEIPASS/NanoPro.ico
                                                  # potrace.exe（GPLv2，AI锐利档外部子进程引擎）→ _MEIPASS/potrace/；image_trace_potrace.find_potrace_exe 读它。
                                                  # 主程序绝不 import potrace，纯子进程隔臂调用（同荧光 ImarisConvertBioformats）。GPL 合规：随附 COPYING。
                                                  (os.path.join("_potrace_bin", "potrace-1.16.win64", "potrace.exe"), "potrace"),
                                                  (os.path.join("_potrace_bin", "potrace-1.16.win64", "COPYING"), "licenses/potrace"),
                                                  (os.path.join("_potrace_bin", "potrace-1.16.win64", "AUTHORS"), "licenses/potrace")],
    # certifi/onnxruntime 是惰性 import（PyInstaller 静态分析可能漏）→ 显式列出；cv2 同理
    hiddenimports=[
        "cv2", "certifi", "PySide6QtAds", "onnxruntime", "onnxruntime.capi._pybind_state",
        # WB 灰度定量面板/模块是菜单懒加载，PyInstaller 静态分析可能漏掉。
        "wb_analyzer", "wb_quant", "gel_analyzer", "wb_batch", "PIL.Image",
        # IHC 免疫组化定量（菜单懒加载，复刻 Fiji Colour Deconvolution；ihc_analyzer 复用 wb_analyzer.ROIView）。
        "ihc_analyzer", "ihc_quant", "ihc_batch",
        # 图像描摹引擎：crisp/potrace 子模块菜单懒加载；image_trace_potrace 在 trace_to_svg 内惰性 import。
        "image_trace", "image_trace_panel", "image_trace_potrace",
        # 图像描摹/矢量化（菜单懒加载 from image_trace_panel import；vtracer .pyd 子模块静态分析可能漏）。
        "image_trace_panel", "image_trace", "vtracer", "vtracer.vtracer",
        # WB 凝胶分析依赖 scipy.signal（find_peaks/savgol，含 Cython 扩展）
        "scipy", "scipy.signal", "scipy.signal._peak_finding_utils", "scipy.signal._savitzky_golay",
    ] + ads_hidden + cert_hidden + scipy_hidden + vtracer_hidden,
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
        # 注意：scipy 不能排除——WB 凝胶分析 gel_analyzer.find_bands 用 scipy.signal.find_peaks（核心）。
        # skimage 仅 Rolling-ball 背景用且有 try/except 兜底→可排除(优雅降级)；pandas/matplotlib WB 没用。
        "transformers", "rembg", "matplotlib", "pandas", "skimage", "scikit-image", "pymatting",
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
