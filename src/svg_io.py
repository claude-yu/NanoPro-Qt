"""SVG 矢量往返：解析(parse_svg) → 统一元素模型(VElem) → Qt 图元(build_items) → 序列化(serialize_svg)。

设计要点（实测 svgelements 行为后定的坐标系单一模型，见 vector-mvp-scope.md 风险#2）：
- **lxml 走树**：结构(<g> 嵌套/z 顺序)、每节点【自身】transform 字符串、原始属性、<text> 文本，全从 lxml 取，
  局部坐标、不被祖先 transform 污染（svgelements 的 node.transform 对不同节点类型基准不一致，弃用）。
- **svgelements 只当几何/颜色/矩阵计算器**（standalone 调用，无祖先上下文）：
  se.Path(d) / se.Rect(...).segments() → 局部段；se.Matrix(str) → 6 元组；se.Color(str) → #rrggbb。
- **transform 不烘焙进点**：祖先变换走 group/item 的 QTransform，path 段用元素自身局部坐标（往返保 <g> transform）。
- **导出绝不用 QSvgGenerator**（会重绘丢分组+丢可编辑 <text>）；用 lxml 手工构造，保 <text>/<g>/style。

fail-loud：parse_svg 返回 (elems, skipped, meta)，skipped 列出降级/跳过的元素 id，调用方报数，不静默。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import svgelements as se
from lxml import etree
from PySide6 import QtGui

SVG_NS = "http://www.w3.org/2000/svg"
# Okabe-Ito 8 色色盲友好板（含黑，科研期刊通用）。B2 配色助手把选中元素 fill/stroke 映射到最近色。
OKABE_ITO = ["#000000", "#E69F00", "#56B4E9", "#009E73",
             "#F0E442", "#0072B2", "#D55E00", "#CC79A7"]
# 不路径化（保 <text> 可编辑）也不当 path 处理的形状标签 → 走 segments() 路径化
_SHAPE_TAGS = {"rect", "circle", "ellipse", "polygon", "polyline", "line"}
# 解析不出几何 / 引用外部资源 → 只读回退（QGraphicsSvgItem 渲染原始 XML）
_UNSUPPORTED_TAGS = {"image", "use", "foreignObject", "pattern", "mask",
                     "filter", "clipPath", "linearGradient", "radialGradient",
                     "symbol", "marker", "switch"}
# 这些子树即便顶层标签可解析，也整体回退（含滤镜/蒙版引用难以保真）
_FILTER_ATTRS = ("filter", "mask", "clip-path")


@dataclass
class VElem:
    """统一矢量元素模型（解析层纯数据 + QPainterPath，无 QGraphicsItem 依赖，便于离线测试）。"""
    type: str                       # 'path' | 'text' | 'group' | 'unsupported'
    id: Optional[str] = None
    qpath: Optional[QtGui.QPainterPath] = None  # path 类型：元素自身局部坐标
    text: Optional[str] = None
    x: float = 0.0
    y: float = 0.0
    font_family: Optional[str] = None
    font_size: float = 12.0
    editable_text: bool = False     # True=源是 <text>（可改字）；路径化文字源是 <path> → False（§0.2 标灰）
    fill: Optional[str] = None       # '#rrggbb' 或 None（none/未指定→NoBrush）
    stroke: Optional[str] = None
    stroke_width: float = 1.0
    opacity: float = 1.0
    transform: tuple = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # 本节点【自身】矩阵 a,b,c,d,e,f
    children: list = field(default_factory=list)        # 仅 group
    z: int = 0                       # 文档顺序索引
    raw_xml: Optional[str] = None    # unsupported 回退用：序列化的原始节点串
    extra_attrs: dict = field(default_factory=dict)     # group 上要原样保留的属性（如 clip-path/mask/filter，往返不丢裁剪）


# ---------------------------------------------------------------- 颜色/矩阵工具

def _norm_color(v: Optional[str]) -> Optional[str]:
    """SVG 颜色字符串 → '#rrggbb'；'none'/空/解析失败/非纯色 → None（NoBrush/NoPen）。
    防 svgelements 把 url()/currentColor/inherit/transparent 静默当黑色（涂黑 bug）。"""
    if v is None:
        return None
    v = v.strip()
    # 非纯色/继承/引用：svgelements 会当成不透明黑 → 这里返回 None(不填)，元素本身另由 _has_unsupported_ref 路由只读保真
    if (not v or v.lower() in ("none", "transparent", "currentcolor", "inherit",
                               "context-fill", "context-stroke")
            or v.lower().startswith("url(")):
        return None
    try:
        c = se.Color(v)
    except Exception:
        return None
    if c is None or c.value is None:
        return None
    try:
        return c.hexrgb  # 归一化：named/short-hex/rgb() 一律转 #rrggbb（QColor 不吃 rgb()）
    except Exception:
        return None


def _hex_to_rgb(v: str):
    """'#rrggbb'/named/rgb() → (r,g,b) 0–255；解析失败/非纯色返回 None。复用 se.Color。"""
    if v is None:
        return None
    vs = v.strip().lower()
    if (not vs or vs in ("none", "transparent", "currentcolor", "inherit")
            or vs.startswith("url(")):  # 同 _norm_color：非纯色不当黑
        return None
    try:
        c = se.Color(v)
        if c is None or c.value is None:
            return None
        return (c.red, c.green, c.blue)
    except Exception:
        return None


def nearest_okabe(hexcolor: str) -> Optional[str]:
    """任意颜色 → OKABE_ITO 里 sRGB 欧氏距离最近的色（'#RRGGBB'）。

    简单够用，不引入 CIEDE2000（感知均匀，但过度工程，简洁优先，见 palette_plan RISK-6）。
    解析失败返回 None（调用方据此跳过，fail-loud 计数）。
    """
    rgb = _hex_to_rgb(hexcolor)
    if rgb is None:
        return None
    best = None
    best_d = None
    for cand in OKABE_ITO:
        cr = _hex_to_rgb(cand)
        if cr is None:
            continue
        d = (rgb[0] - cr[0]) ** 2 + (rgb[1] - cr[1]) ** 2 + (rgb[2] - cr[2]) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best = cand
    return best


def _parse_style(style: Optional[str]) -> dict:
    """内联 style="fill:#abc;stroke:none" → dict。matplotlib/R 导出多走内联 style。"""
    out: dict = {}
    if not style:
        return out
    for part in style.split(";"):
        if ":" in part:
            k, _, val = part.partition(":")
            out[k.strip()] = val.strip()
    return out


def _attr(el, style: dict, name: str, default=None):
    """presentation 属性取值：内联 style 优先于 同名属性（CSS 优先级，与浏览器一致）。"""
    if name in style:
        return style[name]
    val = el.get(name)
    return val if val is not None else default


def _own_transform_tuple(el) -> tuple:
    """节点自身 transform 字符串 → (a,b,c,d,e,f)；无 transform → 单位阵。"""
    ts = el.get("transform")
    if not ts:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    try:
        m = se.Matrix(ts)
        return (m.a, m.b, m.c, m.d, m.e, m.f)
    except Exception:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# ---------------------------------------------------------------- 段 → QPainterPath

def _segments_to_qpath(segments) -> QtGui.QPainterPath:
    """svgelements 段序列 → QPainterPath（局部坐标，不烘焙祖先 transform）。

    Arc 不用 QPainterPath.arcTo（中心/角度语义≠SVG 椭圆弧），改用 as_cubic_curves() 三次贝塞尔分解保形状。
    """
    qp = QtGui.QPainterPath()
    started = False
    for seg in segments:
        name = type(seg).__name__
        if name == "Move":
            e = seg.end
            qp.moveTo(e.x, e.y)
            started = True
        elif name == "Line":
            if not started:
                qp.moveTo(seg.start.x, seg.start.y); started = True
            qp.lineTo(seg.end.x, seg.end.y)
        elif name == "CubicBezier":
            if not started:
                qp.moveTo(seg.start.x, seg.start.y); started = True
            qp.cubicTo(seg.control1.x, seg.control1.y,
                       seg.control2.x, seg.control2.y, seg.end.x, seg.end.y)
        elif name == "QuadraticBezier":
            if not started:
                qp.moveTo(seg.start.x, seg.start.y); started = True
            qp.quadTo(seg.control.x, seg.control.y, seg.end.x, seg.end.y)
        elif name == "Arc":
            if not started:
                qp.moveTo(seg.start.x, seg.start.y); started = True
            try:
                for cub in seg.as_cubic_curves():
                    qp.cubicTo(cub.control1.x, cub.control1.y,
                               cub.control2.x, cub.control2.y, cub.end.x, cub.end.y)
            except Exception:
                qp.lineTo(seg.end.x, seg.end.y)  # 兜底：直线近似（不静默丢段）
        elif name == "Close":
            qp.closeSubpath()
    return qp


def _path_d_to_qpath(d: str) -> QtGui.QPainterPath:
    """standalone 解析 d 串（无祖先上下文 → 纯局部坐标）→ QPainterPath。"""
    return _segments_to_qpath(se.Path(d))


def _shape_to_qpath(tag: str, attrib: dict) -> Optional[QtGui.QPainterPath]:
    """rect/circle/ellipse/polygon/polyline/line → standalone svgelements 形状 → segments → QPainterPath。"""
    def f(name, dv=0.0):
        try:
            return float(attrib.get(name, dv))
        except (TypeError, ValueError):
            return dv

    try:
        if tag == "rect":
            shp = se.Rect(x=f("x"), y=f("y"), width=f("width"), height=f("height"),
                          rx=f("rx"), ry=f("ry"))
        elif tag == "circle":
            shp = se.Circle(cx=f("cx"), cy=f("cy"), r=f("r"))
        elif tag == "ellipse":
            shp = se.Ellipse(cx=f("cx"), cy=f("cy"), rx=f("rx"), ry=f("ry"))
        elif tag in ("polygon", "polyline"):
            pts = attrib.get("points", "")
            shp = se.Polygon(pts) if tag == "polygon" else se.Polyline(pts)
        elif tag == "line":
            shp = se.SimpleLine(x1=f("x1"), y1=f("y1"), x2=f("x2"), y2=f("y2"))
        else:
            return None
        qp = _segments_to_qpath(shp.segments())
        return qp if qp.elementCount() > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------- 解析树

def _local_tag(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _has_unsupported_ref(el) -> bool:
    """节点引用 filter/mask/clip-path 或 url() 渐变/图案填充 → 只读回退。
    渐变/图案若当 path 处理会被 svgelements 静默涂黑——路由只读(QGraphicsSvgItem 渲原样)+报数，保真且 fail-loud。"""
    style = _parse_style(el.get("style"))
    for a in _FILTER_ATTRS:
        v = el.get(a)
        if v and v.strip().lower() not in ("none", ""):
            return True
    for a in ("filter", "mask", "clip-path"):
        v = style.get(a)
        if v and v.strip().lower() not in ("none", ""):
            return True
    for a in ("fill", "stroke"):  # url(#grad)/url(#pattern) 引用填充 → 只读回退，避免静默涂黑
        v = (style.get(a) or el.get(a) or "").strip().lower()
        if v.startswith("url("):
            return True
    return False


def _raw_xml(el) -> str:
    try:
        return etree.tostring(el, encoding="unicode")
    except Exception:
        return ""


def _resolve_href(el) -> str:
    """<use> 的引用目标 id：xlink:href 优先，回退裸 href；去掉前导 '#'。"""
    ref = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
    return ref.lstrip("#")


def _collect_qpaths(el) -> list:
    """递归从定义子树（<symbol>/<g>/<path>/形状）采集 QPainterPath 几何。

    cairo 把字形定义成 <symbol><path .../></symbol>，但 symbol 可能把 path 再裹一层 <g>，故必须递归。
    每个坏字形吞掉异常（不让一个坏 glyph 中断整份导入），跳过空 path。返回 QPainterPath 列表。
    """
    out: list = []
    tag = _local_tag(el)
    try:
        if tag == "path":
            d = el.get("d") or ""
            if d:
                qp = _path_d_to_qpath(d)
                if qp.elementCount() > 0:
                    out.append(qp)
        elif tag in _SHAPE_TAGS:
            qp = _shape_to_qpath(tag, dict(el.attrib))
            if qp is not None and qp.elementCount() > 0:
                out.append(qp)
    except Exception:
        pass  # 单个坏字形/形状不阻断整体（fail-loud 在上层按 paths 为空报数）
    for ch in el:  # 递归子节点（symbol 常把 path 裹在 <g> 里）
        out.extend(_collect_qpaths(ch))
    return out


def _inherited_fill(el) -> str:
    """向上找祖先的 fill（cairo 把字形颜色放在包裹 <use> 的 <g fill="rgb(..)"> 上，不在 <use> 本身）。
    取第一个非空/非 none 的 fill（内联 style 优先），归一化为 #rrggbb；找不到 → 默认黑。"""
    node = el
    while node is not None:
        style = _parse_style(node.get("style"))
        v = style.get("fill") or node.get("fill")
        if v is not None:
            vs = v.strip().lower()
            if vs and vs != "none":
                c = _norm_color(v)
                if c is not None:
                    return c
        node = node.getparent()
    return "#000000"


def _walk(el, z_counter: list, skipped: list, id_map: Optional[dict] = None) -> Optional[VElem]:
    tag = _local_tag(el)
    if not tag or isinstance(el, etree._Comment) or isinstance(el, etree._ProcessingInstruction):
        return None
    z = z_counter[0]
    z_counter[0] += 1
    style = _parse_style(el.get("style"))
    transform = _own_transform_tuple(el)

    # 定义类节点不直接绘制（<symbol> 经 <use> 实例化；渐变/裁剪由引用处处理）→ 整体跳过，不进 skipped
    if tag.lower() in ("defs", "symbol", "marker", "clippath", "lineargradient",
                       "radialgradient", "pattern", "metadata", "title", "desc", "style"):
        return None

    # 引用滤镜/蒙版/裁剪 → 整体只读回退（含其子树），fail-loud 报数
    if _has_unsupported_ref(el) and tag != "g":
        skipped.append(el.get("id") or f"<{tag}> #{z}")
        return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                     raw_xml=_raw_xml(el))

    if tag == "g":
        children = []
        for ch in el:
            sub = _walk(ch, z_counter, skipped, id_map)
            if sub is not None:
                children.append(sub)
        # <g clip-path/mask/filter> 的子树仍可解析编辑，但裁剪/蒙版引用要原样保留
        # （否则 matplotlib/R 常见的 <g clip-path="url(#..)"> 往返后丢裁剪 → 数据溢出坐标轴框）
        extra = {a: el.get(a) for a in _FILTER_ATTRS if el.get(a)}
        if extra:
            skipped.append((el.get("id") or f"<g> #{z}") + "（裁剪/蒙版引用只读保留）")  # fail-loud 报数
        return VElem(type="group", id=el.get("id"), transform=transform, z=z,
                     opacity=_opacity(el, style), children=children, extra_attrs=extra)

    if tag == "use":
        # cairo 把每个文字字符渲染成 <use xlink:href="#glyph0-N" x=.. y=..> 引用 <defs> 里的 <symbol> 字形定义。
        # 解析引用目标 → 采集其矢量几何 → 按 use 的 x/y 平移（字形已是渲染点尺寸，不缩放）→ 当一条矢量 path 处理。
        ref = _resolve_href(el)
        target = id_map.get(ref) if id_map else None
        if target is None:  # fail-loud：引用目标找不到 → 只读回退 + 报数
            skipped.append(el.get("id") or ("<use> #%d ->#%s 未找到" % (z, ref)))
            return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                         raw_xml=_raw_xml(el))
        ux = _f(el.get("x"), 0.0)
        uy = _f(el.get("y"), 0.0)
        paths = _collect_qpaths(target)
        if not paths:  # 目标无可绘制几何（如引用 <image>/<text>）→ 只读回退 + 报数
            skipped.append(el.get("id") or ("<use> #%d ->#%s 无几何" % (z, ref)))
            return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                         raw_xml=_raw_xml(el))
        qp = QtGui.QPainterPath()
        for sub_qp in paths:
            qp.addPath(sub_qp)
        if ux or uy:  # SVG y-down：字形基线在 use.y，字形体在负 y，只平移不缩放
            qp.translate(ux, uy)
        # 字形色继承自祖先 <g fill=..>（不在 <use> 上）；解析成功的 <use> 不进 skipped
        return VElem(
            type="path", id=el.get("id"), transform=transform, z=z, qpath=qp,
            fill=_inherited_fill(el), stroke=None,
            stroke_width=_f(_attr(el, style, "stroke-width", "1"), 1.0),
            opacity=_opacity(el, style),
        )

    if tag == "text":
        # 子 <tspan> 难保真 → 取拼接文本，editable_text 仍 True（B1 单行可编辑）
        txt = "".join(el.itertext())
        return VElem(
            type="text", id=el.get("id"), transform=transform, z=z,
            text=txt, x=_f(_attr(el, style, "x"), 0.0), y=_f(_attr(el, style, "y"), 0.0),
            font_family=_attr(el, style, "font-family"),
            font_size=_f(_attr(el, style, "font-size", "12"), 12.0),
            editable_text=True,
            fill=_norm_color(_attr(el, style, "fill", "#000000")),
            opacity=_opacity(el, style),
        )

    if tag == "path":
        d = el.get("d") or ""
        try:
            qp = _path_d_to_qpath(d) if d else QtGui.QPainterPath()
        except Exception:  # 单个 path 的 d 串非法不能让整份 parse_svg 崩(否则一坏点 path 整图导入失败)→降级该元素为只读
            skipped.append(el.get("id") or f"<path> #{z}（d 解析失败）")
            return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                         raw_xml=_raw_xml(el))
        if qp.elementCount() == 0 and d:
            skipped.append(el.get("id") or f"<path> #{z}（d 解析空）")
            return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                         raw_xml=_raw_xml(el))
        return _shape_velem(el, style, transform, z, qp)

    if tag in _SHAPE_TAGS:
        qp = _shape_to_qpath(tag, dict(el.attrib))
        if qp is None:
            skipped.append(el.get("id") or f"<{tag}> #{z}")
            return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                         raw_xml=_raw_xml(el))
        return _shape_velem(el, style, transform, z, qp)

    if tag in _UNSUPPORTED_TAGS:
        skipped.append(el.get("id") or f"<{tag}> #{z}")
        return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                     raw_xml=_raw_xml(el))

    # 未知标签 → 只读回退（不静默丢）
    skipped.append(el.get("id") or f"<{tag}> #{z}")
    return VElem(type="unsupported", id=el.get("id"), transform=transform, z=z,
                 raw_xml=_raw_xml(el))


def _shape_velem(el, style, transform, z, qp) -> VElem:
    return VElem(
        type="path", id=el.get("id"), transform=transform, z=z, qpath=qp,
        fill=_norm_color(_attr(el, style, "fill", "#000000")),
        stroke=_norm_color(_attr(el, style, "stroke")),
        stroke_width=_f(_attr(el, style, "stroke-width", "1"), 1.0),
        opacity=_opacity(el, style),
    )


def _opacity(el, style) -> float:
    return _f(_attr(el, style, "opacity", "1"), 1.0)


def _f(v, dv: float) -> float:
    if v is None:
        return dv
    s = str(v).strip()
    # 去掉单位后缀（px/pt 等）；font-size:12px → 12
    for unit in ("px", "pt", "em", "%"):
        if s.endswith(unit):
            s = s[:-len(unit)].strip()
            break
    try:
        return float(s)
    except ValueError:
        return dv


def parse_svg(path: str):
    """SVG 文件 → (root_velems: list[VElem], skipped: list[str], meta: dict)。

    meta 含 width/height/viewBox（导出时写回 <svg> 头）。skipped 非空=有降级/跳过项（fail-loud 报数）。
    解析失败抛异常由调用方捕获报错。
    """
    parser = etree.XMLParser(remove_blank_text=False, recover=True, resolve_entities=False)
    tree = etree.parse(path, parser)
    root = tree.getroot()
    if root is None or _local_tag(root) != "svg":
        raise ValueError("不是有效的 SVG（根节点非 <svg>）")
    meta = {
        "width": root.get("width"),
        "height": root.get("height"),
        "viewBox": root.get("viewBox"),
    }
    # id→元素映射：供 <use> 引用解析（含 <defs>/<symbol> 内的字形定义）。遍历整棵树取所有带 id 的节点。
    id_map: dict = {}
    for node in root.iter():
        nid = node.get("id") if isinstance(node.tag, str) else None
        if nid:
            id_map.setdefault(nid, node)  # 首个定义优先（重复 id 取靠前者）
    z_counter = [0]
    skipped: list = []
    elems: list = []
    for ch in root:
        ve = _walk(ch, z_counter, skipped, id_map)
        if ve is not None:
            elems.append(ve)
    return elems, skipped, meta


# ---------------------------------------------------------------- VElem → QGraphicsItem

def _qtransform(t: tuple) -> QtGui.QTransform:
    a, b, c, d, e, f = t
    return QtGui.QTransform(a, b, c, d, e, f)


# ---------------------------------------------------------------- velems 深拷（撤销快照用）

def clone_velems(velems: list) -> list:
    """手工深拷 VElem 列表（B3 撤销快照）。

    RISK-1：copy.deepcopy 对 QPainterPath（C++ 对象，无 __deepcopy__）不可靠（共享引用/broken）→
    显式 `QtGui.QPainterPath(ve.qpath)` 拷贝构造（已验证可用）。其余字段：
    - transform 是 tuple（不可变，直接复用）；text/fill/stroke/font_family/id/raw_xml 是 str/None（不可变）；
    - children 递归 clone；qpath 拷贝构造。
    """
    out = []
    for ve in velems:
        out.append(VElem(
            type=ve.type, id=ve.id,
            qpath=QtGui.QPainterPath(ve.qpath) if ve.qpath is not None else None,
            text=ve.text, x=ve.x, y=ve.y,
            font_family=ve.font_family, font_size=ve.font_size,
            editable_text=ve.editable_text,
            fill=ve.fill, stroke=ve.stroke, stroke_width=ve.stroke_width,
            opacity=ve.opacity, transform=ve.transform,
            children=clone_velems(ve.children) if ve.children else [],
            z=ve.z, raw_xml=ve.raw_xml,
            extra_attrs=dict(ve.extra_attrs) if ve.extra_attrs else {},
        ))
    return out


# ---------------------------------------------------------------- 带 itemChange 的矢量 item 子类（B3 拖动入撤销）
# 三个子类逻辑相同（位置首次变化时调 _move_cb，仿 ImageLayerItem），各自 super() 链不同故分开定义。

def make_editable_text_item(text: str):
    """造一个可内联改字 + 拖动入撤销的 QGraphicsTextItem 子类实例（B2/B3）。

    重写 focusOutEvent：失焦时务必把交互标志回 NoTextInteraction（RISK-2：否则吞掉后续点选/拖动）。
    可设 item._on_edit_done 回调（编辑窗）→ focusOut 时通知 op_label / sync。
    B3：重写 itemChange/mousePress/mouseRelease，拖动首帧调 _move_cb 入撤销（仿 ImageLayerItem）。
    """
    from PySide6 import QtCore, QtWidgets

    class EditableTextItem(QtWidgets.QGraphicsTextItem):
        _moved_this_drag = False
        _move_cb = None
        _snap_cb = None

        def focusOutEvent(self, e):
            super().focusOutEvent(e)
            # 失焦 → 必须退出编辑态，恢复光标，清掉文本选中高亮
            self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
            cur = self.textCursor()
            cur.clearSelection()
            self.setTextCursor(cur)
            cb = getattr(self, "_on_edit_done", None)
            if cb is not None:
                cb(self)

        def keyPressEvent(self, e):
            # Esc 在编辑态 → 退出编辑（清焦点触发 focusOut 兜底）
            if e.key() == QtCore.Qt.Key.Key_Escape and \
                    self.textInteractionFlags() != QtCore.Qt.TextInteractionFlag.NoTextInteraction:
                self.clearFocus()
                e.accept()
                return
            super().keyPressEvent(e)

        def itemChange(self, change, value):
            # 编辑态下拖动是移光标（不发 ItemPositionChange），故只在真正移 item 时入撤销，安全。
            if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
                if not self._moved_this_drag:
                    self._moved_this_drag = True
                    if self._move_cb is not None:
                        self._move_cb("移动矢量元素")
                if self._snap_cb is not None:
                    value = self._snap_cb(value)  # 磁吸：吸到其它元素/画布/参考线/网格
            return super().itemChange(change, value)

        def mousePressEvent(self, e):
            self._moved_this_drag = False
            super().mousePressEvent(e)

        def mouseReleaseEvent(self, e):
            self._moved_this_drag = False
            super().mouseReleaseEvent(e)

    return EditableTextItem(text)


def make_vector_path_item(qpath):
    """带 itemChange 的 path item（B3：拖动首帧入撤销）。"""
    from PySide6 import QtWidgets

    class VectorPathItem(QtWidgets.QGraphicsPathItem):
        _moved_this_drag = False
        _move_cb = None
        _snap_cb = None

        def itemChange(self, change, value):
            if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
                if not self._moved_this_drag:
                    self._moved_this_drag = True
                    if self._move_cb is not None:
                        self._move_cb("移动矢量元素")
                if self._snap_cb is not None:
                    value = self._snap_cb(value)  # 磁吸：吸到其它元素/画布/参考线/网格
            return super().itemChange(change, value)

        def mousePressEvent(self, e):
            self._moved_this_drag = False
            super().mousePressEvent(e)

        def mouseReleaseEvent(self, e):
            self._moved_this_drag = False
            super().mouseReleaseEvent(e)

    item = VectorPathItem(qpath)
    # 设备坐标缓存：拖动(平移)时贴缓存好的位图、不每帧重算贝塞尔 → 大量复杂路径(描摹产物常数百条)拖动不卡。
    # 平移不失效缓存；缩放/改色/改节点时 Qt 自动重建缓存（正确）。
    item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
    return item


def make_vector_group_item():
    """带 itemChange 的 group item（B3：整组拖动首帧入撤销）。"""
    from PySide6 import QtWidgets

    class VectorGroupItem(QtWidgets.QGraphicsItemGroup):
        _moved_this_drag = False
        _move_cb = None
        _snap_cb = None

        def itemChange(self, change, value):
            if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
                if not self._moved_this_drag:
                    self._moved_this_drag = True
                    if self._move_cb is not None:
                        self._move_cb("移动矢量元素")
                if self._snap_cb is not None:
                    value = self._snap_cb(value)  # 磁吸：吸到其它元素/画布/参考线/网格
            return super().itemChange(change, value)

        def mousePressEvent(self, e):
            self._moved_this_drag = False
            super().mousePressEvent(e)

        def mouseReleaseEvent(self, e):
            self._moved_this_drag = False
            super().mouseReleaseEvent(e)

    return VectorGroupItem()


# ---------------------------------------------------------------- B5 锚点 overlay item 工厂
# 这些 item 直接 addItem 到 scene（绝不进任何 layer），故天然不入撤销快照（_snapshot 只遍历 self.layers）。
# 均设 ItemIgnoresTransformations（屏幕尺寸恒定）+ 高 ZValue + setData(0,"__node_overlay__") 打标
# （供导出隐藏 + 命中排除）。拖动回调 _drag_cb(item, scene_pos)：editor 据此改锚点模型。

NODE_OVERLAY_Z = 9000.0
NODE_OVERLAY_TAG = "__node_overlay__"
PEN_PREVIEW_TAG = "__pen_preview__"


def make_anchor_handle(sp_i: int, a_i: int, corner: bool):
    """锚点小方块（角点=实心方块、平滑点=空心菱形）。setData(1,(sp_i,a_i)) 标识其在模型中的位置。"""
    from PySide6 import QtCore, QtWidgets

    class AnchorHandle(QtWidgets.QGraphicsRectItem):
        _drag_cb = None
        _press_cb = None
        _release_cb = None

        def itemChange(self, change, value):
            if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged \
                    and self._drag_cb is not None and not getattr(self, "_suspend_cb", False):
                self._drag_cb(self, self.scenePos())
            return super().itemChange(change, value)

        def mousePressEvent(self, e):
            if self._press_cb is not None:
                self._press_cb(self)
            super().mousePressEvent(e)

        def mouseReleaseEvent(self, e):
            super().mouseReleaseEvent(e)
            if self._release_cb is not None:
                self._release_cb(self)

    R = 4.0  # 半边长（屏幕 px，因 IgnoresTransformations）
    it = AnchorHandle(QtCore.QRectF(-R, -R, 2 * R, 2 * R))
    it.setData(0, NODE_OVERLAY_TAG)
    it.setData(1, (sp_i, a_i))
    it.setData(2, "anchor")
    _style_overlay_handle(it, corner)
    it.setZValue(NODE_OVERLAY_Z + 2)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
    return it


def _style_overlay_handle(it, corner: bool, selected: bool = False):
    from PySide6 import QtGui as _G
    fill = _G.QColor("#ff00ff") if selected else (_G.QColor("#ffffff") if not corner else _G.QColor("#1e90ff"))
    it.setBrush(_G.QBrush(fill))
    pen = _G.QPen(_G.QColor("#1e3a8a"))
    pen.setWidthF(1.2)
    pen.setCosmetic(True)
    it.setPen(pen)


def make_ctrl_handle(sp_i: int, a_i: int, side: str):
    """控制柄圆点（side='cin'|'cout'）。拖动改对应 Anchor.cin/cout。"""
    from PySide6 import QtCore, QtGui as _G, QtWidgets

    class CtrlHandle(QtWidgets.QGraphicsEllipseItem):
        _drag_cb = None
        _release_cb = None

        def itemChange(self, change, value):
            if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged \
                    and self._drag_cb is not None and not getattr(self, "_suspend_cb", False):
                self._drag_cb(self, self.scenePos())
            return super().itemChange(change, value)

        def mouseReleaseEvent(self, e):
            super().mouseReleaseEvent(e)
            if self._release_cb is not None:
                self._release_cb(self)

    R = 3.5
    it = CtrlHandle(QtCore.QRectF(-R, -R, 2 * R, 2 * R))
    it.setData(0, NODE_OVERLAY_TAG)
    it.setData(1, (sp_i, a_i))
    it.setData(2, "ctrl:" + side)
    it.setBrush(_G.QBrush(_G.QColor("#22d3ee")))
    pen = _G.QPen(_G.QColor("#0e7490")); pen.setWidthF(1.0); pen.setCosmetic(True)
    it.setPen(pen)
    it.setZValue(NODE_OVERLAY_Z + 3)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
    return it


def make_ctrl_line():
    """on↔控制柄连接虚线（cosmetic，不挡点击）。"""
    from PySide6 import QtCore, QtGui as _G, QtWidgets
    it = QtWidgets.QGraphicsLineItem()
    it.setData(0, NODE_OVERLAY_TAG)
    pen = _G.QPen(_G.QColor("#22d3ee"))
    pen.setWidthF(1.0); pen.setCosmetic(True); pen.setStyle(QtCore.Qt.PenStyle.DashLine)
    it.setPen(pen)
    it.setZValue(NODE_OVERLAY_Z + 1)
    it.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
    return it


def make_pen_preview_path():
    """钢笔实时预览 path（橡皮筋 + 已落点）。setData(0,PEN_PREVIEW_TAG) 打标。"""
    from PySide6 import QtCore, QtGui as _G, QtWidgets
    it = QtWidgets.QGraphicsPathItem()
    it.setData(0, PEN_PREVIEW_TAG)
    pen = _G.QPen(_G.QColor("#1e90ff"))
    pen.setWidthF(1.0); pen.setCosmetic(True)
    it.setPen(pen)
    it.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    it.setZValue(NODE_OVERLAY_Z)
    it.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
    return it


def make_pen_anchor_dot():
    """钢笔预览中已落锚点的小圆点。"""
    from PySide6 import QtCore, QtGui as _G, QtWidgets
    R = 3.5
    it = QtWidgets.QGraphicsEllipseItem(QtCore.QRectF(-R, -R, 2 * R, 2 * R))
    it.setData(0, PEN_PREVIEW_TAG)
    it.setBrush(_G.QBrush(_G.QColor("#1e90ff")))
    pen = _G.QPen(_G.QColor("#ffffff")); pen.setWidthF(1.0); pen.setCosmetic(True)
    it.setPen(pen)
    it.setZValue(NODE_OVERLAY_Z + 2)
    it.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    it.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
    return it


def _to_item(ve: VElem):
    """单个 VElem → QGraphicsItem。延迟导入 QtWidgets/QtSvg（解析层纯数据不依赖 Qt widget）。"""
    from PySide6 import QtCore, QtWidgets

    if ve.type == "path":
        item = make_vector_path_item(ve.qpath)
        if ve.stroke:
            pen = QtGui.QPen(QtGui.QColor(ve.stroke))
            pen.setWidthF(ve.stroke_width)
            pen.setCosmetic(False)  # 随缩放（矢量描边）
            item.setPen(pen)
        else:
            item.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        item.setBrush(QtGui.QBrush(QtGui.QColor(ve.fill)) if ve.fill
                      else QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        item.setOpacity(ve.opacity)

    elif ve.type == "text":
        # 可编辑 <text> 用子类（focusOut 兜底回 NoTextInteraction，B2 内联改字）；路径化文字理论上是 <path>，
        # 不会进此分支，但保险起见 outlined 也走裸 QGraphicsTextItem。
        if ve.editable_text:
            item = make_editable_text_item(ve.text or "")
        else:
            item = QtWidgets.QGraphicsTextItem(ve.text or "")
        item.setDefaultTextColor(QtGui.QColor(ve.fill or "#000000"))
        font = QtGui.QFont()
        if ve.font_family:
            font.setFamily(ve.font_family.split(",")[0].strip().strip("'\""))
        if ve.font_size > 0:
            font.setPointSizeF(ve.font_size)
        item.setFont(font)
        item.setOpacity(ve.opacity)
        # <text> 的 y 是基线，QGraphicsTextItem.setPos 是左上角 → 减 ascent 校正（风险#3）
        ascent = QtGui.QFontMetricsF(font).ascent()
        item.setPos(ve.x, ve.y - ascent)
        item.setData(2, (float(ve.x), float(ve.y - ascent)))  # 导入基线 pos：导出时减掉，避免与 _emit 写的 x,y 双重偏移→漂移
        # B1 只读渲染+可移动；不开 TextEditorInteraction（改字留 B2）
        item.setData(0, "editable_text" if ve.editable_text else "outlined_text")
        # 存原始 font-family 串（可能是多族 fallback 链，如 "Helvetica, Arial, sans-serif"）：
        # QFont 只保留首族，_sync_one 若直接读回 font.family() 会把链塌成单族，连无编辑往返也丢 fallback(审核 LOW L1)。
        item.setData(3, ve.font_family)

    elif ve.type == "group":
        item = make_vector_group_item()
        for child in ve.children:
            child_item = _to_item(child)
            if child_item is not None:
                item.addToGroup(child_item)
        item.setOpacity(ve.opacity)

    else:  # unsupported → QGraphicsSvgItem 只读渲染原始 XML
        item = _unsupported_item(ve)
        if item is None:
            return None

    # 自身 transform → item.setTransform（祖先变换走父 group，不烘焙进点）
    item.setTransform(_qtransform(ve.transform))
    item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
    item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
    item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
    return item


def _strip_root_transform(raw: str) -> str:
    """剥掉 raw 片段【根元素自身】的 transform 属性（仅供 unsupported 渲染副本，导出不经此路）。
    解析失败原样返回（宽松回退，绝不让单个坏片段中断渲染）。"""
    try:
        node = etree.fromstring(raw.encode("utf-8"))
    except Exception:
        return raw
    if node.get("transform") is not None:
        del node.attrib["transform"]
    return etree.tostring(node, encoding="unicode")


def _unsupported_item(ve: VElem):
    from PySide6 import QtCore
    try:
        from PySide6 import QtSvg, QtSvgWidgets
    except Exception:
        return None
    raw = ve.raw_xml or ""
    if not raw:
        return None
    # L2：渲染副本剥掉根节点自身 transform —— 该 transform 已由 _to_item 的 item.setTransform 应用一次；
    # 若内嵌 XML 里还留着，QSvgRenderer 会再应用一次 → scale/rotate 被双倍(translate 因 viewBox 归一化侥幸单次)。
    # 只剥渲染副本，不动 ve.raw_xml —— 导出仍用 ve.raw_xml 原样(含 transform)，单次正确。
    render_raw = _strip_root_transform(raw)
    doc = (f'<svg xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink">'
           f'{render_raw}</svg>')
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(doc.encode("utf-8")))
    if not renderer.isValid():
        return None
    item = QtSvgWidgets.QGraphicsSvgItem()
    item.setSharedRenderer(renderer)
    item._svg_renderer = renderer  # 保引用，防 GC
    return item


def build_items(velems: list) -> list:
    """root VElem 列表 → 顶层 QGraphicsItem 列表（z 按文档顺序，调用方 setZValue+base）。"""
    items = []
    for ve in velems:
        it = _to_item(ve)
        if it is not None:
            items.append((it, ve))
    return items


# ---------------------------------------------------------------- 回写 item 状态 → VElem

def iter_leaf_items(items):
    """递归展开顶层 item 列表 → 所有叶子（path/text）item（穿过 QGraphicsItemGroup）。

    供 B2 配色助手「整层映射」遍历层内全部 path/text（仿 _sync_one 的递归）。
    """
    from PySide6 import QtWidgets
    out = []
    for it in items:
        if isinstance(it, QtWidgets.QGraphicsItemGroup):
            out.extend(iter_leaf_items(it.childItems()))
        else:
            out.append(it)
    return out


def sync_items_to_velems(pairs):
    """导出前把 item 的实时几何/样式回写进 VElem（velems 是 SSOT，item 是视图）。

    pairs: list[(QGraphicsItem, VElem)]（顶层）。move 工具改的是 item.pos()，必须读回。
    递归处理 group 的子项。
    """
    for item, ve in pairs:
        _sync_one(item, ve)


def _sync_one(item, ve: VElem):
    from PySide6 import QtWidgets
    # 合成 pos + transform：item.pos() 平移并进自身矩阵（导出反映用户拖动）
    t = item.transform()
    pos = item.pos()
    # 最终 = translate(pos) ∘ transform
    a, b, c, d = t.m11(), t.m12(), t.m21(), t.m22()
    e, f = t.dx() + pos.x(), t.dy() + pos.y()
    ve.transform = (a, b, c, d, e, f)
    ve.opacity = item.opacity()

    if ve.type == "path" and isinstance(item, QtWidgets.QGraphicsPathItem):
        # B5 RISK-2：回灌 item 当前形状。锚点编辑/钢笔后 item.path() 变了，必须读回，
        # 否则导出/撤销快照取的是旧形状=静默丢编辑（fail-loud 违规）。拖整 item 时 path() 恒等，安全。
        ve.qpath = QtGui.QPainterPath(item.path())
        pen = item.pen()
        from PySide6 import QtCore
        if pen.style() == QtCore.Qt.PenStyle.NoPen:
            ve.stroke = None
        else:
            ve.stroke = pen.color().name()
            ve.stroke_width = pen.widthF()
        brush = item.brush()
        if brush.style() == QtCore.Qt.BrushStyle.NoBrush:
            ve.fill = None
        else:
            ve.fill = brush.color().name()
    elif ve.type == "text" and isinstance(item, QtWidgets.QGraphicsTextItem):
        ve.text = item.toPlainText()
        ve.fill = item.defaultTextColor().name()
        font = item.font()
        # L1：QFont 只揣首族。若当前首族 == 导入时原始链的首族 → 用户没改字体族 → 原样保留多族 fallback 链；
        # 改过(QFontComboBox 选了别的族)才用新单族覆盖。避免无编辑往返把链塌成单族、丢 fallback 安全网。
        orig_ff = item.data(3)
        cur_first = font.family()
        if orig_ff and cur_first == orig_ff.split(",")[0].strip().strip("'\""):
            ve.font_family = orig_ff
        else:
            ve.font_family = cur_first
        if font.pointSizeF() > 0:
            ve.font_size = font.pointSizeF()
        # _emit 已显式写 <text> 基线 x,y；transform 不能再把导入基线 pos 折进去(否则每轮往返双重偏移→漂移)。
        # transform 只承载用户拖动增量 = 当前 pos - 导入基线 pos。
        base = item.data(2) or (ve.x, ve.y)
        ve.transform = (a, b, c, d, t.dx() + (pos.x() - base[0]), t.dy() + (pos.y() - base[1]))
    elif ve.type == "group" and isinstance(item, QtWidgets.QGraphicsItemGroup):
        kids = [c for c in item.childItems()]
        # 子项顺序与 ve.children 一一对应（build 时同序 addToGroup）。
        # fail-loud：若数目不等(某子项 _to_item 返 None 被丢→错位)，告警而非静默丢尾随兄弟的编辑(审核 LOW)。
        if len(kids) != len(ve.children):
            import logging
            logging.getLogger(__name__).warning(
                "矢量组 sync 数目不符：%d 个 item vs %d 个 velem(组 id=%s)，可能有子项被丢弃、其后兄弟的编辑会丢失",
                len(kids), len(ve.children), ve.id)
        for child_item, child_ve in zip(kids, ve.children):
            _sync_one(child_item, child_ve)


# ---------------------------------------------------------------- 序列化 → SVG（lxml）

def _is_identity(t: tuple) -> bool:
    return (abs(t[0] - 1) < 1e-9 and abs(t[1]) < 1e-9 and abs(t[2]) < 1e-9 and
            abs(t[3] - 1) < 1e-9 and abs(t[4]) < 1e-9 and abs(t[5]) < 1e-9)


def _matrix_str(t: tuple) -> str:
    return "matrix(%s)" % ",".join(_num(v) for v in t)


def _num(v) -> str:
    """紧凑数字：整数省小数，否则保 6 位有效并去尾零。"""
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _qpath_to_d(qp: QtGui.QPainterPath) -> str:
    """QPainterPath → SVG d 串。遍历 elementAt：Move/Line/Curve(C)/隐式闭合。"""
    out = []
    i = 0
    n = qp.elementCount()
    while i < n:
        el = qp.elementAt(i)
        et = el.type
        if et == QtGui.QPainterPath.ElementType.MoveToElement:
            out.append("M %s %s" % (_num(el.x), _num(el.y)))
            i += 1
        elif et == QtGui.QPainterPath.ElementType.LineToElement:
            out.append("L %s %s" % (_num(el.x), _num(el.y)))
            i += 1
        elif et == QtGui.QPainterPath.ElementType.CurveToElement:
            c1 = el
            c2 = qp.elementAt(i + 1)
            ep = qp.elementAt(i + 2)
            out.append("C %s %s %s %s %s %s" % (
                _num(c1.x), _num(c1.y), _num(c2.x), _num(c2.y), _num(ep.x), _num(ep.y)))
            i += 3
        else:
            i += 1
    return " ".join(out)


# ---------------------------------------------------------------- 锚点模型（B5：钢笔/锚点编辑）
# 纯函数，无 QGraphicsItem 依赖，离屏可单测。QPainterPath ↔ 锚点列表往返。
# Qt 闭合语义：QPainterPath 不存显式 Close 元素。closeSubpath() 在遍历里表现为最后一段
# 回到子路径起点（末点≈首点）。本模块用「末点≈首点(<EPS)」判 closed（含 Z 的源会生成重合点）。

_ANCHOR_EPS = 1e-6  # 闭合判定/共线判定的坐标容差


@dataclass
class Anchor:
    """单个锚点（on=锚点本身，cin/cout=两侧贝塞尔控制柄绝对坐标，None=该侧无柄/直线）。"""
    on: tuple                      # (x, y)
    cin: Optional[tuple] = None    # 入向控制柄绝对坐标（None=直线进入）
    cout: Optional[tuple] = None   # 出向控制柄绝对坐标（None=直线离开）
    corner: bool = True            # True=角点(两侧柄独立)；False=平滑点(两侧柄共线，overlay 联动)


def _is_smooth(cin, on, cout) -> bool:
    """cin/on/cout 三点共线且柄分居 on 两侧（向量反向）→ 平滑点。任一柄缺失→角点。"""
    if cin is None or cout is None:
        return False
    vin = (on[0] - cin[0], on[1] - cin[1])    # cin→on
    vout = (cout[0] - on[0], cout[1] - on[1])  # on→cout
    lin = (vin[0] ** 2 + vin[1] ** 2) ** 0.5
    lout = (vout[0] ** 2 + vout[1] ** 2) ** 0.5
    if lin < _ANCHOR_EPS or lout < _ANCHOR_EPS:
        return False
    # 单位向量同向（cross≈0 且 dot>0）= 平滑（in、out 同方向延伸，柄分居两侧）
    cross = vin[0] * vout[1] - vin[1] * vout[0]
    dot = vin[0] * vout[0] + vin[1] * vout[1]
    return abs(cross) < _ANCHOR_EPS * lin * lout * 10 and dot > 0


def path_to_anchors(qp: QtGui.QPainterPath) -> list:
    """QPainterPath → [{"anchors": list[Anchor], "closed": bool}, ...]（每个子路径一段）。

    遍历 elementAt：Move 起新子路径；Line→上一 anchor.cout=None + 新角点；Curve(C1,C2,EP)→
    上一 anchor.cout=C1 + 新 anchor(on=EP, cin=C2)。闭合判定：子路径末点≈首点则 closed=True，
    把末 anchor 的 cin 移给首 anchor.cin 后丢弃末 anchor（首尾合并）。
    """
    subpaths: list = []
    cur: Optional[list] = None
    i = 0
    n = qp.elementCount()
    while i < n:
        el = qp.elementAt(i)
        et = el.type
        if et == QtGui.QPainterPath.ElementType.MoveToElement:
            cur = [Anchor(on=(el.x, el.y))]
            subpaths.append(cur)
            i += 1
        elif et == QtGui.QPainterPath.ElementType.LineToElement:
            if cur is None:
                cur = [Anchor(on=(el.x, el.y))]
                subpaths.append(cur)
            else:
                cur[-1].cout = None
                cur.append(Anchor(on=(el.x, el.y)))
            i += 1
        elif et == QtGui.QPainterPath.ElementType.CurveToElement:
            c1 = el
            c2 = qp.elementAt(i + 1)
            ep = qp.elementAt(i + 2)
            if cur is None:  # 防御：不该发生（Curve 必跟在 Move/Line/Curve 后）
                cur = [Anchor(on=(c1.x, c1.y))]
                subpaths.append(cur)
            cur[-1].cout = (c1.x, c1.y)
            cur.append(Anchor(on=(ep.x, ep.y), cin=(c2.x, c2.y)))
            i += 3
        else:
            i += 1

    out: list = []
    for anchors in subpaths:
        closed = False
        if len(anchors) >= 2:
            first, last = anchors[0], anchors[-1]
            dx = last.on[0] - first.on[0]
            dy = last.on[1] - first.on[1]
            if (dx * dx + dy * dy) ** 0.5 < _ANCHOR_EPS:
                # 首尾重合 → 闭合：末 anchor 的 cin（闭合段的入向柄）移给首 anchor，丢弃末 anchor
                closed = True
                if last.cin is not None:
                    first.cin = last.cin
                anchors = anchors[:-1]
        for a in anchors:  # 标平滑/角点（供 overlay 联动控制柄）
            a.corner = not _is_smooth(a.cin, a.on, a.cout)
        out.append({"anchors": anchors, "closed": closed})
    return out


def anchors_to_path(subpaths: list) -> QtGui.QPainterPath:
    """逆向重建：[{"anchors":[Anchor,...], "closed":bool}, ...] → QPainterPath。

    相邻 (prev,cur)：两侧无柄→lineTo；否则 cubicTo(prev.cout or prev.on, cur.cin or cur.on, cur.on)。
    closed：用 (last.cout, first.cin, first.on) 接一段回起点 + closeSubpath()。
    """
    qp = QtGui.QPainterPath()
    for sp in subpaths:
        anchors = sp.get("anchors", [])
        if not anchors:
            continue
        a0 = anchors[0]
        qp.moveTo(a0.on[0], a0.on[1])
        for k in range(1, len(anchors)):
            prev = anchors[k - 1]
            cur = anchors[k]
            _emit_seg(qp, prev, cur)
        if sp.get("closed") and len(anchors) >= 2:
            _emit_seg(qp, anchors[-1], anchors[0])
            qp.closeSubpath()
    return qp


def _emit_seg(qp: QtGui.QPainterPath, prev: Anchor, cur: Anchor):
    """从 prev.on 到 cur.on 画一段：无柄=直线，有柄=三次贝塞尔。"""
    if prev.cout is None and cur.cin is None:
        qp.lineTo(cur.on[0], cur.on[1])
    else:
        c1 = prev.cout if prev.cout is not None else prev.on
        c2 = cur.cin if cur.cin is not None else cur.on
        qp.cubicTo(c1[0], c1[1], c2[0], c2[1], cur.on[0], cur.on[1])


def _lerp(p, q, t):
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def split_segment(prev: Anchor, cur: Anchor, t: float):
    """在 prev→cur 段的参数 t∈(0,1) 处插一个新锚（de Casteljau）。

    返回 (new_prev_cout, new_anchor, new_cur_cin)：
    - 直线段：new_anchor 为角点(cin=cout=None)，prev/cur 柄不变（仍 None）。
    - cubic 段：按 de Casteljau 分裂成两段；new_anchor 是平滑点，prev.cout/cur.cin 被替换为新控制点。
    调用方据此就地改 prev.cout、插入 new_anchor、改 cur.cin。
    """
    if prev.cout is None and cur.cin is None:
        new_on = _lerp(prev.on, cur.on, t)
        return None, Anchor(on=new_on, cin=None, cout=None, corner=True), None
    p0 = prev.on
    p1 = prev.cout if prev.cout is not None else prev.on
    p2 = cur.cin if cur.cin is not None else cur.on
    p3 = cur.on
    a = _lerp(p0, p1, t)
    b = _lerp(p1, p2, t)
    c = _lerp(p2, p3, t)
    d = _lerp(a, b, t)
    e = _lerp(b, c, t)
    f = _lerp(d, e, t)  # 分裂点（新锚 on）
    # 左段 cubic: p0, a, d, f ；右段 cubic: f, e, c, p3
    new_prev_cout = a
    new_anchor = Anchor(on=f, cin=d, cout=e, corner=False)
    new_cur_cin = c
    return new_prev_cout, new_anchor, new_cur_cin


def _style_str(ve: VElem) -> dict:
    """VElem 样式 → SVG presentation 属性 dict（fill/stroke/...）。none 显式写出（防被继承覆盖）。"""
    attrs = {}
    if ve.type in ("path",):
        attrs["fill"] = ve.fill if ve.fill else "none"
        attrs["stroke"] = ve.stroke if ve.stroke else "none"
        if ve.stroke:
            attrs["stroke-width"] = _num(ve.stroke_width)
    if ve.opacity is not None and abs(ve.opacity - 1.0) > 1e-9:
        attrs["opacity"] = _num(ve.opacity)
    return attrs


def _emit(ve: VElem, parent_el, dropped=None):
    if ve.type == "group":
        g = etree.SubElement(parent_el, f"{{{SVG_NS}}}g")
        if ve.id:
            g.set("id", ve.id)
        if not _is_identity(ve.transform):
            g.set("transform", _matrix_str(ve.transform))
        if ve.opacity is not None and abs(ve.opacity - 1.0) > 1e-9:
            g.set("opacity", _num(ve.opacity))
        for k, v in (ve.extra_attrs or {}).items():  # 原样写回 clip-path/mask/filter，往返不丢裁剪
            g.set(k, v)
        for child in ve.children:
            _emit(child, g, dropped)

    elif ve.type == "path":
        p = etree.SubElement(parent_el, f"{{{SVG_NS}}}path")
        if ve.id:
            p.set("id", ve.id)
        p.set("d", _qpath_to_d(ve.qpath) if ve.qpath is not None else "")
        for k, v in _style_str(ve).items():
            p.set(k, v)
        if not _is_identity(ve.transform):
            p.set("transform", _matrix_str(ve.transform))

    elif ve.type == "text":
        t = etree.SubElement(parent_el, f"{{{SVG_NS}}}text")
        if ve.id:
            t.set("id", ve.id)
        t.set("x", _num(ve.x))
        t.set("y", _num(ve.y))
        if ve.font_family:
            t.set("font-family", ve.font_family)
        t.set("font-size", _num(ve.font_size))
        t.set("fill", ve.fill if ve.fill else "#000000")
        if ve.opacity is not None and abs(ve.opacity - 1.0) > 1e-9:
            t.set("opacity", _num(ve.opacity))
        if not _is_identity(ve.transform):
            t.set("transform", _matrix_str(ve.transform))
        t.text = ve.text or ""

    elif ve.type == "unsupported" and ve.raw_xml:
        node = None
        try:
            node = etree.fromstring(ve.raw_xml)  # 原样保留（只读回退也不丢）
        except Exception:
            try:  # 救回：源常有未声明 xlink 前缀(matplotlib <image>)，recover 解析器容忍
                node = etree.fromstring(ve.raw_xml, parser=etree.XMLParser(recover=True))
            except Exception:
                node = None
        if node is not None:
            parent_el.append(node)
        elif dropped is not None:  # fail-loud：救不回 → 计数上浮，不静默丢
            dropped.append(ve.id or "unsupported")


def serialize_svg(velems: list, meta: Optional[dict] = None, dropped: Optional[list] = None) -> str:
    """VElem 列表 → SVG 字符串（lxml 手工构造，保 <text>/<g>/style；绝不用 QSvgGenerator）。

    meta（来自 parse_svg）提供 width/height/viewBox，缺失时按元素包围盒兜底/省略。
    dropped（可选 list）：传入则累计无法重序列化的只读元素 id，供调用方 fail-loud 报数。
    """
    meta = meta or {}
    root = etree.Element(f"{{{SVG_NS}}}svg", nsmap={None: SVG_NS})
    if meta.get("width"):
        root.set("width", str(meta["width"]))
    if meta.get("height"):
        root.set("height", str(meta["height"]))
    if meta.get("viewBox"):
        root.set("viewBox", str(meta["viewBox"]))
    for ve in velems:
        _emit(ve, root, dropped)
    return etree.tostring(root, pretty_print=True, xml_declaration=True,
                          encoding="utf-8").decode("utf-8")
