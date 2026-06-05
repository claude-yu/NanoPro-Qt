"""PDF → SVG 转换（外部命令行工具，子进程调用）。

设计（见 vector-mvp-scope.md：PDF 走【外部 pdf2svg/inkscape】而非 PyMuPDF）：
- 外部工具以**独立可执行子进程**调用（非链接库）→ 即便工具是 GPL（poppler/pdftocairo），
  本程序仍可闭源分发（与 pdf2svg/inkscape 同理）。
- 转出的 SVG 再交给 svg_io.parse_svg / import_svg，复用 B1/B2/B3 全部矢量编辑能力。
- 探测顺序：用户在配置里指定的 exe → pdftocairo(poppler) → pdf2svg → inkscape。
  pdftocairo 最常见（poppler/TeXLive/MiKTeX 都带），作首选回退。
  只收【单文件 SVG 输出、写入精确路径】的工具；mutool convert 的 -o 对分页用 %d 模式、
  不写精确路径（本机无法验证），故不纳入——宁缺毋滥，不上不可验证的通道。
- fail-loud：找不到任何转换器 / 子进程非零退出 / 产出空 SVG，都明确报错，不静默。

安全：子进程一律 list 形式调用（绝不 shell=True，杜绝命令注入）；超时；Windows 不弹控制台窗口。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

# Windows：不弹黑色控制台窗口
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_TIMEOUT = 90  # 秒；大 PDF 转换兜底，超时 fail-loud


def _cmd_pdftocairo(exe, pdf, svg, page):
    # poppler：单页 SVG，-f/-l 限定页码
    return [exe, "-svg", "-f", str(page), "-l", str(page), pdf, svg]


def _cmd_pdf2svg(exe, pdf, svg, page):
    return [exe, pdf, svg, str(page)]


def _cmd_inkscape(exe, pdf, svg, page):
    # inkscape 1.x：--pdf-page 选页，导出 svg
    return [exe, pdf, f"--pdf-page={page}", "--export-type=svg", f"--export-filename={svg}"]


# (工具名, 可执行候选名列表, 命令构造函数)；顺序即探测优先级。
# 三者都把转换结果写入【精确的 out 路径】（pdftocairo -svg / pdf2svg / inkscape --export-filename 均单文件）。
_CONVERTERS = [
    ("pdftocairo", ["pdftocairo"], _cmd_pdftocairo),
    ("pdf2svg", ["pdf2svg"], _cmd_pdf2svg),
    ("inkscape", ["inkscape"], _cmd_inkscape),
]


def find_converter(configured_path: str | None = None):
    """返回 (工具名, exe 绝对路径, 命令构造函数)；都找不到返回 None。

    configured_path：用户在配置里手动指定的转换器 exe（绝对路径），优先级最高。
    其文件名须能匹配已知工具（pdftocairo/pdf2svg/inkscape/mutool），以便用对命令格式。
    """
    if configured_path:
        p = str(configured_path).strip().strip('"')
        if p and os.path.isfile(p):
            stem = os.path.splitext(os.path.basename(p))[0].lower()
            for name, _cands, builder in _CONVERTERS:
                if name in stem:  # 文件名含已知工具名 → 用其命令格式
                    return (name, p, builder)
            # 配置了未知工具名：按 pdftocairo 格式试，但工具名报【实际文件名】（失败信息不误导成 pdftocairo）
            return (os.path.splitext(os.path.basename(p))[0], p, _cmd_pdftocairo)
    for name, cands, builder in _CONVERTERS:
        for c in cands:
            exe = shutil.which(c)
            if exe:
                return (name, exe, builder)
    return None


def available_converters() -> list[str]:
    """当前 PATH 上可用的转换器工具名列表（供 UI 提示/诊断）。"""
    out = []
    for name, cands, _b in _CONVERTERS:
        if any(shutil.which(c) for c in cands):
            out.append(name)
    return out


def page_count(pdf_path: str) -> int | None:
    """尽力估计 PDF 页数（轻量正则，不依赖外部库）。

    未压缩/常规 PDF 可数出；对象流压缩的 PDF 数不出 → 返回 None（调用方据此给"若为多页"提示）。
    """
    try:
        with open(pdf_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    # /Type /Page（排除 /Pages）；容忍空白
    n = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    return n if n >= 1 else None


def convert_to_svg(pdf_path: str, out_svg: str, page: int = 1,
                   configured_path: str | None = None) -> dict:
    """把 PDF 第 page 页转成 SVG（out_svg）。

    返回 {ok, tool, error}。fail-loud：找不到工具 / 子进程失败 / 产出空文件，ok=False + error 说明。
    """
    conv = find_converter(configured_path)
    if conv is None:
        avail = available_converters()
        return {"ok": False, "tool": None,
                "error": ("未找到 PDF→SVG 转换器。请安装以下任一外部工具并加入 PATH："
                          "pdftocairo(poppler) / pdf2svg / inkscape，"
                          "或在配置里指定其 exe 路径。" + (f"（当前可用：{avail}）" if avail else ""))}
    name, exe, builder = conv
    cmd = builder(exe, pdf_path, out_svg, page)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT,
                           creationflags=_NO_WINDOW)  # list 形式，绝不 shell=True
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": name, "error": f"{name} 转换超时（>{_TIMEOUT}s）"}
    except OSError as e:
        return {"ok": False, "tool": name, "error": f"{name} 调用失败：{e}"}
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "ignore").strip()[:300]
        return {"ok": False, "tool": name,
                "error": f"{name} 退出码 {r.returncode}：{err or '(无 stderr)'}"}
    if not os.path.isfile(out_svg) or os.path.getsize(out_svg) == 0:
        return {"ok": False, "tool": name, "error": f"{name} 未产出有效 SVG（文件为空或缺失）"}
    return {"ok": True, "tool": name, "error": None}


def make_temp_svg() -> str:
    """生成一个临时 .svg 路径（调用方转换后用，用完自行删除）。"""
    fd, p = tempfile.mkstemp(suffix=".svg", prefix="sciedit_pdf_")
    os.close(fd)
    return p
