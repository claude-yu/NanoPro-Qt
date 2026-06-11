# -*- coding: utf-8 -*-
"""updater —— 应用内「检查更新」（GitHub Releases）。

冻结(exe)用户：检查 releases/latest → 比对版本 → 下载新 Setup.exe → 运行(Inno 原地升级) → 退出本进程让安装覆盖。
  用户设置(API Key/提示词预设/布局)都在 ~/.sciedit/，与程序分离 → 升级安装一律不丢。
源码运行：不下载安装包(对源码无意义)，只提示去 git pull / 打开 Release 页。
纯 urllib，零第三方依赖；不碰任何 key。fail-loud：每步错误都回字符串，不静默。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

from ai_client import _SSL  # 复用 certifi 优先的 SSL 上下文
import config

REPO = "claude-yu/NanoPro-Qt"
_API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO
_UA = "NanoPro-Updater"


def current_version() -> str:
    return getattr(config, "APP_VERSION", "0")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _ver_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums) if nums else (0,)


def is_newer(latest: str, current: str) -> bool:
    """latest 比 current 新？按数字段比较(1.18.1 > 1.18 > 1.17)。"""
    return _ver_tuple(latest) > _ver_tuple(current)


def fetch_latest(timeout: int = 15) -> dict:
    """GET releases/latest → {tag, version, setup_url, notes, html_url} 或 {error}。"""
    try:
        req = urllib.request.Request(
            _API, headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 —— 网络/解析任何异常 → fail-loud
        return {"error": "检查更新失败：%s" % e}
    tag = str(obj.get("tag_name") or "")
    setup_url = None
    for a in (obj.get("assets") or []):
        n = (a.get("name") or "").lower()
        if n.endswith(".exe") and "setup" in n:
            setup_url = a.get("browser_download_url")
            break
    return {"tag": tag, "version": tag.lstrip("vV"), "setup_url": setup_url,
            "notes": str(obj.get("body") or "")[:4000],
            "html_url": obj.get("html_url") or RELEASES_PAGE}


def download(url: str, dest: str, progress_cb=None, should_cancel=None, timeout: int = 60):
    """下载 url → dest（分块写，progress_cb(done,total)）。返回 None 成功 / 错误串。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    if should_cancel and should_cancel():
                        return "已取消"
                    chunk = r.read(1 << 20)  # 1MB/块
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        return None
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(dest):
                os.remove(dest)  # 失败删半截文件
        except OSError:
            pass
        return "下载失败：%s" % e


def run_installer(path: str):
    """运行安装包(Inno 安装向导)。调用方随后应退出本进程，让安装覆盖程序文件。返回 None / 错误串。"""
    try:
        os.startfile(path)  # noqa: S606 —— Windows 默认方式启动安装包（用户机本地文件）
        return None
    except Exception as e:  # noqa: BLE001
        return "启动安装包失败：%s" % e
