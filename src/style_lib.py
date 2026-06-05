"""图生图参考图库扫描 —— 软件本体不打包图，只引用一个外部目录（官方图包 / 用户自备目录）。

约定：库目录里若有 `manifest.json`（官方图包自带，结构见 build 脚本）则按主题分组；
否则把目录下所有图片平铺成单组「全部」——这样用户随手指一个装满图的文件夹也能直接用。
纯函数 + 文件系统只读，便于离线单测。
"""
from __future__ import annotations

import json
from pathlib import Path

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _scan_flat(d: Path):
    """无 manifest：目录下所有图片文件 → 单组「全部」（按文件名排序）。"""
    figs = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            figs.append({"file": p.name, "caption": p.stem, "path": str(p)})
    return [{"name": "全部", "keywords": "", "figures": figs}] if figs else []


def scan_library(lib_dir):
    """扫描图库目录 → (themes, err)。
    themes: [{name, keywords, figures:[{file, caption, path}]}]；err 非 None 表示目录无效/无图。
    有 manifest.json 按其主题归类（缺失文件大声跳过、不静默吞）；否则平铺扫描。"""
    if not lib_dir:
        return [], "未指定图库目录"
    d = Path(str(lib_dir))
    if not d.is_dir():
        return [], "图库目录不存在：%s" % lib_dir
    mf = d / "manifest.json"
    if not mf.exists():
        themes = _scan_flat(d)
        return (themes, None) if themes else ([], "该目录下没有图片文件")
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        return [], "manifest.json 解析失败：%s" % e
    themes = []
    missing = 0
    for t in data.get("themes", []):
        figs = []
        for f in t.get("figures", []):
            fp = d / f.get("file", "")
            if not fp.is_file():
                missing += 1  # 大声失败：manifest 列了但文件不在 → 跳过并计数
                continue
            figs.append({"file": f["file"], "caption": f.get("caption", fp.stem), "path": str(fp)})
        if figs:
            themes.append({"name": t.get("name", "未命名"), "keywords": t.get("keywords", ""), "figures": figs})
    if not themes:
        return [], "manifest 里的图片都找不到（缺 %d 个）" % missing
    return themes, ("manifest 缺 %d 张图（已跳过）" % missing if missing else None)


def count_figures(themes) -> int:
    return sum(len(t["figures"]) for t in themes)


def build_manifest(lib_dir):
    """扫描 lib_dir 一键生成 manifest.json，让一堆散图变成正式风格库（能分主题）：
      - 每个【顶层子文件夹】= 一个主题（主题名 = 文件夹名），**递归收其下任意深度**的图（图记为相对路径）；
      - 顶层散图 → 归入「未分类」主题。
    caption 暂用文件名(stem)，keywords 留空——用户可事后手编 manifest 细化。
    返回 (themes_count, figures_count, err)；无图返回 err。会覆盖已有 manifest.json。"""
    if not lib_dir:
        return 0, 0, "未指定目录"
    d = Path(str(lib_dir))
    if not d.is_dir():
        return 0, 0, "目录不存在：%s" % lib_dir
    themes = []
    top = [p.name for p in sorted(d.iterdir(), key=lambda x: x.name.lower())
           if p.is_file() and p.suffix.lower() in IMG_EXTS]
    if top:
        themes.append({"name": "未分类", "keywords": "",
                       "figures": [{"file": f, "caption": Path(f).stem} for f in top]})
    for sub in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if sub.is_dir():
            figs = [{"file": str(p.relative_to(d)).replace("\\", "/"), "caption": p.stem}
                    for p in sorted(sub.rglob("*"), key=lambda x: str(x).lower())
                    if p.is_file() and p.suffix.lower() in IMG_EXTS]  # 递归收深层图，相对路径(跨平台用 /)
            if figs:
                themes.append({"name": sub.name, "keywords": "", "figures": figs})
    nf = sum(len(t["figures"]) for t in themes)
    if not themes:
        return 0, 0, "该目录及其子文件夹下没有图片"
    try:
        (d / "manifest.json").write_text(
            json.dumps({"themes": themes}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return 0, 0, "写 manifest.json 失败：%s" % e
    return len(themes), nf, None


def _has_images(d: Path) -> bool:
    """目录顶层（不递归）是否有图片文件。"""
    try:
        return any(p.is_file() and p.suffix.lower() in IMG_EXTS for p in d.iterdir())
    except Exception:
        return False


def list_style_libs(path):
    """把一个目录解析成「风格库」列表，支持多风格库可扩展：
      [{name, dir}]，每个 = 一个独立风格库（用 scan_library 进一步取其主题/图）。
    规则（优先级从上到下）：
      1) 若该目录下有【含 manifest.json 的子目录】→ 每个这种子目录算一个正式风格库
         （父目录自身若也含 manifest/顶层图，则它也作为一个库排最前）；
         —— 这样 `E:\\ai\\inquring_picture` 下放 `科研简单风/`、`手绘风/`… 各带 manifest，自动并列识别。
      2) 否则该目录自身含 manifest 或顶层图 → 它本身是单个风格库（直接指向一个库目录）；
      3) 再否则把目录下【含图的子目录】平铺为若干库（用户丢的未整理图包，无 manifest 也能用）。
    含 manifest 才被当正式风格库，可避开 qt-prototype/插件等无关含图子目录被误纳。"""
    if not path:
        return []
    d = Path(str(path))
    if not d.is_dir():
        return []
    sub_mf = [{"name": s.name, "dir": str(s)}
              for s in sorted(d.iterdir(), key=lambda x: x.name.lower())
              if s.is_dir() and (s / "manifest.json").exists()]
    if sub_mf:
        if (d / "manifest.json").exists() or _has_images(d):
            sub_mf.insert(0, {"name": d.name + "（本目录）", "dir": str(d)})
        return sub_mf
    if (d / "manifest.json").exists() or _has_images(d):
        return [{"name": d.name, "dir": str(d)}]
    return [{"name": s.name, "dir": str(s)}
            for s in sorted(d.iterdir(), key=lambda x: x.name.lower())
            if s.is_dir() and _has_images(s)]
