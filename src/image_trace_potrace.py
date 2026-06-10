# -*- coding: utf-8 -*-
"""image_trace_potrace —— 「AI 级锐利·平滑曲线」描摹引擎：外部 potrace.exe 逐色子进程管线。

对扁平科研图标(BioRender 式:纯色大色块+深色细描边线稿)出 Adobe Illustrator 图像描摹级矢量
(全局曲线优化+自适应角点：95% 贝塞尔曲线，时钟圆角/文字可读，孔洞天然正确)。9-agent 研发实测：
暗线恢复 0.95(vtracer 0.74/crisp 0.89)、渐变区平滑无发花、孔洞正确、0.8s/图、svg_io 0 回退。

【许可合规（铁律，务必遵守）】potrace 是 GPLv2 —— 本模块**只通过子进程 + 文件 I/O 调用 potrace.exe**，
**绝不 `import potrace`/potracer 代码**（那会让闭源主程序变 GPL 衍生作品）。和荧光模块
ImarisConvertBioformats 一个套路（GPL 二进制在独立进程，主程序隔臂不传染）。potrace.exe 缺失时
trace_to_svg 自动降级 crisp/diy（fail-loud），绝不偷偷 import potracer。

【极性约定（踩过坑，钉死别改）】potrace.exe 吃 PBM(P4)，约定 **1=黑=前景被描，不取反**（PBM 原生）。
（注：纯 Python potracer 的 Bitmap 内部会 invert，需传 ~mask —— 但那只是研发测试用，产品只走 exe。）

【组装铁律】每个 potrace 形状各出一个【顶层独立 `<path fill=.. fill-rule=evenodd/>`】，**不再包 `<g>`**。
（原同色一组 `<g>` 会被 svg_io 解析成一个 QGraphicsItemGroup → 拖动抓整个同色组、最大那层=拖整张图，
单素材拖不动；去掉 `<g>` 后每形状 = 一个独立可拖/可改色 item，对齐 crisp/vtracer。）
**绝不把一层的多个独立形状 d 串拼成一条 `<path>`** —— 不重叠/嵌套形状会 winding 互相抵消
（ai_ml_19 实测拼接后暗层全消失 recov=0）。注意「独立 `<path>`」≠「包 `<g>`」：可独立 `<path>` 且顶层平铺。

依赖：cv2/numpy/PIL（主程序已装）。子进程层 0 第三方依赖。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import numpy as np


# ---------------------------------------------------------------- potrace.exe 定位
def find_potrace_exe():
    """返回 (exe_path, kind)。kind in {'exe', None}。产品只用 'exe'；找不到返回 (None, None) → 调用方降级。"""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:                                                   # PyInstaller 冻结：随包 datas
        candidates.append(os.path.join(base, "potrace", "potrace.exe"))
    here = os.path.dirname(os.path.abspath(__file__))          # 开发期：仓库 _potrace_bin
    candidates.append(os.path.join(here, "..", "_potrace_bin", "potrace-1.16.win64", "potrace.exe"))
    onpath = shutil.which("potrace")                           # PATH
    if onpath:
        candidates.append(onpath)
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c), "exe"
    return None, None


# ---------------------------------------------------------------- PBM(P4) 写出（1=前景，不取反）
def _write_pbm_p4(mask_bool: np.ndarray, path: str):
    h, w = mask_bool.shape
    bits = np.packbits(mask_bool.astype(np.uint8), axis=1)     # 每行 MSB first，自动右补 0
    with open(path, "wb") as f:
        f.write(b"P4\n%d %d\n" % (w, h))
        f.write(bits.tobytes())


def _potrace_via_exe(mask_bool, exe_path, *, turdsize=2, turnpolicy="minority",
                     alphamax=1.0, opttolerance=0.2, opticurve=True, unit=10):
    """bool mask → PBM → potrace.exe -s → 返回 SVG 文本（空层返回 None）。"""
    if not mask_bool.any():
        return None
    with tempfile.TemporaryDirectory(prefix="nanopro_pot_") as td:
        pbm = os.path.join(td, "in.pbm"); svg = os.path.join(td, "out.svg")
        _write_pbm_p4(mask_bool, pbm)
        args = [exe_path, "-s", "-t", str(turdsize), "-z", turnpolicy,
                "-a", str(alphamax), "-O", str(opttolerance), "-u", str(unit)]
        if not opticurve:
            args.append("-n")
        args += ["-o", svg, pbm]
        flags = 0x08000000 if os.name == "nt" else 0           # CREATE_NO_WINDOW 防黑框
        r = subprocess.run(args, capture_output=True, creationflags=flags, timeout=60)
        if r.returncode != 0:
            raise RuntimeError("potrace rc=%d: %s" % (r.returncode, r.stderr.decode("utf-8", "replace")[:200]))
        with open(svg, "r", encoding="utf-8") as f:
            return f.read()


# ---------------------------------------------------------------- 解析 potrace SVG → image 坐标 d 串
def _parse_transform(g_attr):
    tx = ty = 0.0; sx = sy = 1.0
    m = re.search(r"translate\(([-\d.eE]+)[,\s]+([-\d.eE]+)\)", g_attr)
    if m:
        tx, ty = float(m.group(1)), float(m.group(2))
    m = re.search(r"scale\(([-\d.eE]+)[,\s]+([-\d.eE]+)\)", g_attr)
    if m:
        sx, sy = float(m.group(1)), float(m.group(2))
    else:
        m = re.search(r"scale\(([-\d.eE]+)\)", g_attr)
        if m:
            sx = sy = float(m.group(1))
    return tx, ty, sx, sy


def _bake_d(d, tx, ty, sx, sy):
    """把 transform translate(tx,ty) scale(sx,sy) 烘焙进 d 串绝对坐标 + 相对命令转绝对（image 坐标）。"""
    toks = re.findall(r"[MmLlCcZzHhVv]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", d)
    i = 0; cx = cy = 0.0; cmd = None; out = []

    def emit_pt(x, y):
        return (x * sx + tx, y * sy + ty)

    while i < len(toks):
        t = toks[i]
        if t in "MmLlCcZzHhVv":
            cmd = t; i += 1
            if cmd in "Zz":
                out.append("Z")
            continue
        if cmd in ("M", "m"):
            x = float(toks[i]); y = float(toks[i + 1]); i += 2
            if cmd == "m":
                x += cx; y += cy
            cx, cy = x, y
            X, Y = emit_pt(x, y); out.append("M%.3f %.3f" % (X, Y))
            cmd = "l" if cmd == "m" else "L"
        elif cmd in ("L", "l"):
            x = float(toks[i]); y = float(toks[i + 1]); i += 2
            if cmd == "l":
                x += cx; y += cy
            cx, cy = x, y
            X, Y = emit_pt(x, y); out.append("L%.3f %.3f" % (X, Y))
        elif cmd in ("H", "h"):
            x = float(toks[i]); i += 1
            if cmd == "h":
                x += cx
            cx = x
            X, Y = emit_pt(cx, cy); out.append("L%.3f %.3f" % (X, Y))
        elif cmd in ("V", "v"):
            y = float(toks[i]); i += 1
            if cmd == "v":
                y += cy
            cy = y
            X, Y = emit_pt(cx, cy); out.append("L%.3f %.3f" % (X, Y))
        elif cmd in ("C", "c"):
            x1 = float(toks[i]); y1 = float(toks[i + 1]); x2 = float(toks[i + 2]); y2 = float(toks[i + 3])
            x = float(toks[i + 4]); y = float(toks[i + 5]); i += 6
            if cmd == "c":
                x1 += cx; y1 += cy; x2 += cx; y2 += cy; x += cx; y += cy
            cx, cy = x, y
            X1, Y1 = emit_pt(x1, y1); X2, Y2 = emit_pt(x2, y2); X, Y = emit_pt(x, y)
            out.append("C%.3f %.3f %.3f %.3f %.3f %.3f" % (X1, Y1, X2, Y2, X, Y))
        else:
            i += 1
    return " ".join(out)


def _potrace_svg_to_image_d(svg_text):
    """potrace -s SVG → 烘焙 transform 后的 path d 串列表（image 像素坐标）。空 → []。"""
    if not svg_text:
        return []
    root = ET.fromstring(svg_text.encode("utf-8") if isinstance(svg_text, str) else svg_text)
    out = []
    for g in root.iter("{http://www.w3.org/2000/svg}g"):
        tx, ty, sx, sy = _parse_transform(g.get("transform", ""))
        for p in g.findall("{http://www.w3.org/2000/svg}path"):
            d = p.get("d", "")
            if d.strip():
                out.append(_bake_d(d, tx, ty, sx, sy))
    return out


# ---------------------------------------------------------------- 饱和度感知调色板（保住少数派彩色原子）
def _medcut_pal(arr, n):
    """arr:(M,3) uint8 → median-cut 调色板 (≤n,3) uint8（每簇取成员中位作代表色）。空→(0,3)。"""
    from PIL import Image as _PIL
    n = max(1, int(n))
    if len(arr) == 0:
        return np.zeros((0, 3), np.uint8)
    strip = arr.reshape(1, -1, 3).astype(np.uint8)
    q = _PIL.fromarray(strip).quantize(colors=n, method=_PIL.Quantize.MEDIANCUT, dither=_PIL.Dither.NONE)
    qa = np.asarray(q).reshape(-1)
    reps = [np.median(arr[qa == i], axis=0) for i in np.unique(qa)]
    return np.array(reps, dtype=np.uint8)


def _sat_aware_palette(px, spx, n, s_thr=70):
    """色像素按【饱和度】拆成饱和簇 / 淡彩簇分别量化，饱和簇保底抢槽位 → 少数派彩色（分子绿苯环/红氧/黄硫）
    不被占 80% 的近白+淡蓝背景吞掉调色板预算。返回 (K,3) uint8 调色板。

    根因（实测分子对接图）：median-cut 按【population×volume】劈盒，淡蓝蛋白表面+近白是绝对多数 → 20 槽全
    耗在蓝/白上，占 2.9% 的饱和原子(绿/红/黄)零槽位 → 全并进最近的蓝/暗 = 描出来颜色全错。拆分后饱和簇独立
    量化，保证每个明显色相留代表色。饱和像素太少(纯灰度/扁平淡图) → 退回单池 median-cut，不强切。
    """
    sat_sel = spx >= s_thr
    n_sat = int(sat_sel.sum()); n_all = len(px)
    if n_sat < max(80, int(n_all * 0.005)):           # 几乎无饱和色 → 单池量化（不强切，避免淡图/灰度被乱分）
        return _medcut_pal(px, n)
    frac = n_sat / float(n_all)
    # 饱和簇保底：至少 5 色、至多 n-3；按饱和占比放大(×3)抢预算，防淡彩独吞（frac 小也保 0.35×n）
    k_sat = int(np.clip(round(n * max(frac * 3.0, 0.35)), 5, max(5, n - 3)))
    pal_sat = _medcut_pal(px[sat_sel], k_sat)
    rest = px[~sat_sel]
    pal_mute = _medcut_pal(rest, n - len(pal_sat)) if len(rest) >= 30 else np.zeros((0, 3), np.uint8)
    return np.vstack([pal_sat, pal_mute]).astype(np.uint8) if len(pal_mute) else pal_sat.astype(np.uint8)


def _nearest_palette(px, pal):
    """每像素 → 最近调色板色 idx（sRGB 欧氏，分块防大图内存爆）。px:(M,3), pal:(K,3) → (M,) int32。"""
    if len(pal) == 0:
        return np.zeros(len(px), np.int32)
    palf = pal.astype(np.int32)
    out = np.empty(len(px), np.int32)
    step = 200000
    for i in range(0, len(px), step):
        chunk = px[i:i + step].astype(np.int32)
        d = ((chunk[:, None, :] - palf[None, :, :]) ** 2).sum(2)
        out[i:i + step] = d.argmin(1)
    return out


# ---------------------------------------------------------------- 三层分离（与 _trace_crisp 同配方）
def _separate_layers(rgb, n_colors=20):
    """RGB(H,W,3) → (dark_mask bool, [(rep_rgb, color_mask bool)], drops)。"""
    import cv2
    rgb = np.ascontiguousarray(rgb[..., :3])
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    sat = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1]
    # 深色层 = 真描边/线稿/黑字（低饱和的黑/深灰）。极暗(gray<80)算描边、中等暗(80-110)在低饱和(sat<130)时算描边；
    # 但【高饱和(sat>=150)】的像素无论多暗都【不】入暗层——深品红尖端(sat~196)/深蓝氮原子/深绿碳是【有颜色的填充】，
    # 不是黑线稿，塞进暗层会被单色化成黑(实测 DNA 深品红尖端、分子深色原子丢色)。纯黑线 sat~0 仍入暗层(黑线 100% 保留实测)。
    dark_mask = ((gray < 80) | ((gray < 110) & (sat < 130))) & (sat < 150)
    # 背景只认【近白】(各通道都很亮，min>=228)——【不能】用"低饱和"判背景：淡蓝蛋白表面/淡彩填充
    # 饱和度也低，会被误当背景丢掉=描出来没颜色(实测蛋白-配体图 lowsat 占 85%，淡蓝表面全被吃)。
    bg_cand = (rgb.min(axis=2) >= 228) & ~dark_mask
    nlab, lab = cv2.connectedComponents(bg_cand.astype(np.uint8), connectivity=4)
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1]); border.discard(0)
    bg_mask = np.isin(lab, list(border)) & bg_cand if border else np.zeros_like(dark_mask)
    # 近纯白(min>=245)不消耗彩色调色板预算：specular 高光/白 H 原子/抗锯齿白即使不连边界也排除（透出白底，
    # 视觉等同高光）—— 否则它们和淡蓝一起把 median-cut 槽位耗光(实测分子图 20 色里 ~10 个肉眼无差的近白)。
    # 保留淡彩(min 228–244)给彩色层（淡蓝表面要有色）。
    near_white = rgb.min(axis=2) >= 245
    color_region = ~dark_mask & ~bg_mask & ~near_white
    drops = {"bg_dropped_px": int(bg_mask.sum()), "empty_color_blocks": 0}

    ys, xs = np.where(color_region)
    if len(ys) == 0:
        return dark_mask, [], drops
    sm = cv2.bilateralFilter(rgb, 7, 60, 60)                   # 平滑渐变（保边）→ 消 posterize 发花
    cpx = sm[ys, xs].astype(np.uint8)                          # 量化源（平滑后）
    cs = sat[ys, xs]
    pal = _sat_aware_palette(cpx, cs, max(2, n_colors))        # 饱和度感知：保住少数派彩色
    labels = _nearest_palette(cpx, pal)                        # 每像素最近调色板色
    lab2d = np.full((rgb.shape[0], rgb.shape[1]), -1, np.int32)
    lab2d[ys, xs] = labels
    if len(pal) <= 254:                                        # 合并碎岛（消 posterize 发花）；偏移 +1 保护 -1 非色区
        shift = cv2.medianBlur(np.clip(lab2d + 1, 0, 255).astype(np.uint8), 7)
        lab2d = shift.astype(np.int32) - 1
        lab2d[~color_region] = -1                              # 中值可能渗入边沿 → 非色区还原
    layers = []
    for k in range(len(pal)):
        m = lab2d == k
        npx = int(m.sum())
        if npx < 30:
            if npx > 0:
                drops["empty_color_blocks"] += 1
            continue
        ys2, xs2 = np.where(m)                                  # 代表色=该簇原图中位（饱和/淡彩已分离，中位本身够饱和，不需 satp70）
        rep = np.median(rgb[ys2, xs2], axis=0).astype(int)
        layers.append(((int(rep[0]), int(rep[1]), int(rep[2])), m))
    layers.sort(key=lambda t: -int(t[1].sum()))               # 大色块先画
    return dark_mask, layers, drops


def _build_svg(w, h, traced_dark, traced_colors, color_layers, dark_rgb):
    """每个 potrace 形状 = 一个【顶层独立 <path fill fill-rule=evenodd/>】平铺（不包同色 <g>，绝不拼整层）。
    去 <g> 是为了让 svg_io 出 N 个独立可拖 item（包 <g> 会被建成 QGraphicsItemGroup→整组拖动，单素材拖不动）。"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
             'width="%d" height="%d" viewBox="0 0 %d %d">' % (w, h, w, h)]

    def emit(rgb, dlist):
        if not dlist:
            return
        fill = "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2])
        # 每个 potrace 形状各出一个【顶层独立 <path>】，不再包 <g>（同色一组）：
        # svg_io.parse_svg 把每个 <g> 建成一个 QGraphicsItemGroup → 拖动抓到整个同色组（横跨全图该色全部形状），
        # 最大那层(淡背景)=拖整张图 → 单素材拖不动。去掉 <g> 后每形状 = 一个独立可拖/可改色 item（对齐 crisp/vtracer）。
        # fill 仍写在每个 <path> 上（svg_io 按 path 自身读 fill，不继承）；fill-rule=evenodd 保孔洞；
        # 每形状独立 <path>（绝不拼成一条）保 winding 不抵消（暗线不消失）。
        for d in dlist:
            if d.strip():
                parts.append('<path d="%s" fill="%s" fill-rule="evenodd"/>' % (d, fill))

    for (rgb, _), dl in zip(color_layers, traced_colors):     # 彩色层在底
        emit(rgb, dl)
    if traced_dark:                                           # 深色描边压顶
        emit(dark_rgb, traced_dark)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- 主入口（与 _trace_diy 同签名）
def trace_potrace(rgb, params, exe_path) -> tuple:
    """RGB 位图 → (svg_str, n_colors, drops)。potrace.exe 子进程逐色。exe_path 由 find_potrace_exe 给。"""
    import cv2
    h, w = rgb.shape[:2]
    n_colors = params.n_colors if (params.n_colors and params.n_colors >= 2) else 20
    if params.palette:
        n_colors = len(params.palette)
    d_t = int(getattr(params, "potrace_turdsize", 12)); d_o = float(getattr(params, "potrace_opttolerance", 0.8))
    c_t = int(getattr(params, "color_turdsize", 10)); c_o = float(getattr(params, "color_opttolerance", 0.6))
    amax = float(getattr(params, "alphamax", 1.0))

    dark_mask, color_layers, drops = _separate_layers(rgb, n_colors=n_colors)
    drops["dropped_small_contours"] = drops.pop("empty_color_blocks", 0)

    def run(mask, t, o):
        txt = _potrace_via_exe(mask, exe_path, turdsize=t, turnpolicy="minority",
                               alphamax=amax, opttolerance=o)
        return _potrace_svg_to_image_d(txt)

    dm = cv2.dilate(dark_mask.astype(np.uint8), np.ones((2, 2), np.uint8)).astype(bool)  # 救 1-2px 细线
    dark_rgb = tuple(np.median(rgb[dark_mask], axis=0).astype(int)) if dark_mask.any() else (34, 34, 34)
    traced_dark = run(dm, d_t, d_o)
    traced_colors = [run(m, c_t, c_o) for _, m in color_layers]

    svg = _build_svg(w, h, traced_dark, traced_colors, color_layers, dark_rgb)
    n_actual = (1 if traced_dark else 0) + sum(1 for t in traced_colors if t)
    return svg, max(1, n_actual), drops
