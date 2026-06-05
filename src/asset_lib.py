"""本地素材库扫描 —— 连接一个本地素材根目录（biorender 式分类）。

约定：root 下每个【顶层子文件夹】= 一个分类（name = 文件夹名），该分类**递归收下面任意深度**的图
（子文件夹里再套文件夹也读得到）；root 顶层散图 → 归入「未分类」分类。纯函数 + 文件系统只读，便于离线单测。
风格对齐 style_lib.py（共用 IMG_EXTS 语义、(结果, err) 大声失败）。
"""
from __future__ import annotations

from pathlib import Path

from style_lib import IMG_EXTS  # 复用同一份扩展名集合，避免两处维护漂移


def _scan_items(d: Path):
    """目录顶层（不递归）的图片文件 → [{file, path}]（按文件名排序）。"""
    items = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            items.append({"file": p.name, "path": str(p)})
    return items


def _scan_items_recursive(d: Path):
    """目录下【所有层级】的图片文件 → [{file, path}]（按相对路径排序）。
    某分类文件夹里再套子文件夹也一并收进来——回答“其他文件夹里的素材也能读么”=能。"""
    items = []
    for p in sorted(d.rglob("*"), key=lambda x: str(x).lower()):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            items.append({"file": p.name, "path": str(p)})
    return items


def scan_asset_tree(root):
    """扫描成【文件夹树】（BioRender 式分类→子分类）：每个文件夹一个节点
    {name, path, items:[直属图 {file,path}], children:[子节点]}。
    点某节点只加载它的【直属】图(不递归) → 每次只显几十张，上万素材也不卡。
    返回 (root_node, all_items, err)；all_items=全树拍平(供全局搜索)。坏子目录跳过，不抛。"""
    if not root:
        return None, [], "未指定素材文件夹"
    d = Path(str(root))
    if not d.is_dir():
        return None, [], "素材文件夹不存在：%s" % root
    all_items = []

    def build(folder):
        items, children = [], []
        try:
            entries = sorted(folder.iterdir(), key=lambda x: x.name.lower())
        except Exception:
            entries = []
        for p in entries:
            try:
                if p.is_file() and p.suffix.lower() in IMG_EXTS:
                    rec = {"file": p.name, "path": str(p)}
                    items.append(rec); all_items.append(rec)
                elif p.is_dir():
                    ch = build(p)
                    if ch["items"] or ch["children"]:  # 跳过空文件夹
                        children.append(ch)
            except Exception:
                continue
        return {"name": folder.name, "path": str(folder), "items": items, "children": children}

    node = build(d)
    if not node["items"] and not node["children"]:
        return None, [], "该文件夹下没有图片素材"
    return node, all_items, None


def scan_assets(root):
    """扫描本地素材根目录 → (groups, err)。
    groups: [{name, items:[{file, path}]}]；name=分类名（顶层散图归「未分类」）。
    err 非 None 表示目录无效/无图（大声失败，不静默吞）。"""
    if not root:
        return [], "未指定素材文件夹"
    d = Path(str(root))
    if not d.is_dir():
        return [], "素材文件夹不存在：%s" % root
    # 有 manifest.json → 按其主题分组（与「AI 参考图库」同源）：图物理平铺、但 manifest 给了分类
    # 也能显示分类（如自带索引的图包 科研简单风）。解析失败/为空 → 落回下面的物理(子文件夹)扫描。
    if (d / "manifest.json").exists():
        import style_lib
        themes, _merr = style_lib.scan_library(root)
        if themes:
            return [{"name": t["name"],
                     "items": [{"file": Path(f["file"]).name, "path": f["path"]} for f in t["figures"]]}
                    for t in themes], None
    groups = []
    top = _scan_items(d)
    if top:
        groups.append({"name": "未分类", "items": top})
    for sub in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if sub.is_dir():
            items = _scan_items_recursive(sub)  # 该顶层文件夹=一个分类，递归收其下任意深度的图
            if items:
                groups.append({"name": sub.name, "items": items})
    if not groups:
        return [], "该文件夹及其子文件夹下没有图片素材"
    return groups, None
