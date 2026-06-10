"""AI 分割/抠图客户端 —— 可插拔后端（grsai 图生图编辑 / PPIO 异步 / HTTP image-edit 兼容 / 本地 rembg）。

provider 一览（默认 grsai）：
- grsai：复用 ai_client.generate_image 做图生图编辑（用户已配好 key，零额外配置）。
- ppio ：PPIO 派欧云 Qwen-Image-Edit，JSON+Bearer，异步（提交→拿 task_id→轮询取图→下载转 b64）。
- http ：OpenAI image-edit 兼容中转（契约见下）。
- rembg：本地 rembg（仅去背景）。

本模块只定义「OpenAI image-edit 兼容风格」的请求契约（POST /v1/images/edits，body 含
image/prompt/response_format/n，返回 data[].b64_json）。**该远端 API 契约未经实跑核实**——
provider 须自行核对其 image-edit/抠图端点的真实字段（端点路径、字段名、返回结构、是否支持多张），
见模块末「未决/不确定点」。端点可经 config.seg_endpoint 覆盖默认 /v1/images/edits；
_extract_cutouts_from_json 做宽松多形态解析以容忍契约差异。

复用 ai_client 的 _make_ssl_context()/_SSL 模式（certifi 优先回退系统默认）。
key 只在本模块用、绝不打日志/进仓库/进工程文件。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

from ai_client import _SSL  # 复用 ai_client 的 SSL 上下文（certifi 优先回退系统默认）

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# —— 面板用的下拉常量（仿 ai_client.MODELS/NODES）——
SEG_MODES = [
    ("foreground", "去背景（抠出主要前景·透明·单张）"),
    # 生成式后端做不到真拆解 → 本管线=AI 干净去背景(透明) + 本地按 alpha 连通域拆成多个独立元素素材。
    ("elements",   "拆解多元素（AI去背景 + 本地按连通域拆成多个独立素材）"),
]
SEG_PROVIDERS = [
    ("grsai",  "grsai 图生图编辑（复用已配置的 grsai，去背景/编辑·出整图）"),
    ("ppio",   "PPIO Qwen-Image-Edit（异步·去背景/编辑·出整图）"),
    ("local",  "本地内置模型（离线去背景·无需安装·u2netp）"),
    ("http",   "HTTP API（OpenAI image-edit 兼容中转）"),
    ("rembg",  "本地 rembg（需 pip install rembg·质量更高·仅去背景）"),
]
# grsai 抠图节点（国内/国外，二选一；key 同一个 grsai 账户 key，仅地址不同）
SEG_NODES = [
    ("https://grsai.dakka.com.cn", "🇨🇳 国内节点 (dakka·不开VPN)"),
    ("https://grsaiapi.com", "🌎 国外节点 (grsaiapi.com·需VPN)"),
]
# PPIO Qwen 图像编辑可选模型（去背景/编辑，异步）。
# 只放「编辑」模型：抠图/去背景必须用 image-edit；文生图(qwen-image)不能编辑现有图，故不在此提供。
PPIO_MODELS = [
    ("qwen/qwen-image-edit", "通义千问图像编辑（去背景/编辑·推荐）"),
]
# 指令文案（送给 image-edit 模型的 prompt/instruction），按 mode 选。
_MODE_PROMPT = {
    "foreground": ("Remove the background completely. Output a PNG with transparent "
                   "background containing only the main foreground subject, edges clean, no halo."),
    "elements":   ("Segment this scientific figure into its distinct visual elements "
                   "(icons, shapes, labels-free objects). For each element output a "
                   "separate PNG cutout with transparent background."),
}

_DEFAULT_ENDPOINT = "/v1/images/edits"

# 默认编辑指令（grsai/ppio 抠图时用户未给 prompt 则用此「去背景」指令）。
_DEFAULT_EDIT_INSTR = ("Remove the background completely; keep only the main subject on a "
                       "fully transparent background, no shadow, clean edges.")

# —— PPIO 派欧云 Qwen-Image-Edit 异步契约（取证默认值，均可经 config 覆盖）——
_PPIO_BASE = "https://api.ppinfra.com"                # base host（ppio.com 仅文档站）
_PPIO_SUBMIT_ENDPOINT = "/v3/async/qwen-image-edit"   # 提交端点（POST）→ {"task_id":...}
_PPIO_RESULT_ENDPOINT = "/v3/async/task-result"        # 取结果端点（GET ?task_id=，回退 POST body）
_PPIO_MODEL = "qwen/qwen-image-edit"                   # 模型名（路径即模型，body 通常无需再传）
_PPIO_POLL_INTERVAL = 2.0                              # 轮询间隔（秒）


def _normalize_b64(img_bytes_or_b64) -> str:
    """bytes→base64 str；已是 str 则原样（去掉可能的 data URL 前缀）。
    fail-loud：非 bytes/str 抛 TypeError（调用方捕获转 error）。"""
    if isinstance(img_bytes_or_b64, (bytes, bytearray)):
        return base64.b64encode(bytes(img_bytes_or_b64)).decode("ascii")
    if isinstance(img_bytes_or_b64, str):
        s = img_bytes_or_b64.strip()
        if s.startswith("data:") and "," in s:  # data:image/png;base64,<b64>
            s = s.split(",", 1)[1].strip()
        return s
    raise TypeError("源图必须是 bytes 或 base64 字符串，收到 %s" % type(img_bytes_or_b64).__name__)


def _data_url(b64: str) -> str:
    """拼 data URL（部分 image-edit API 吃 data URL 而非裸 b64）。"""
    return "data:image/png;base64," + b64


def _download_b64(url, timeout=120):
    """urllib 下载 url → base64（仿 ai_client.generate_image 末尾的下载图案）。
    返回 (b64, err)：err 非 None 表示失败。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return base64.b64encode(r.read()).decode("ascii"), None
    except Exception as e:
        return None, "下载结果图失败：%s" % e


def _extract_cutouts_from_json(obj, timeout=120):
    """宽松解析分割响应 → (list_b64, err)。契约不确定，兼容多形态：
    - OpenAI images 风格：obj["data"] 列表，每项取 b64_json（有）或 url（需下载）。
    - 直接 {"cutouts":[b64,...]} / {"images":[...]} / {"output":[...]}。
    - 单图 {"image":b64} / {"b64":b64}。
    解析不出任何图 → (None, error 含响应形态片段，截断防泄漏大体积)。"""
    out = []
    if isinstance(obj, dict):
        # OpenAI images 风格：data 列表
        data = obj.get("data")
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict):
                    if it.get("b64_json"):
                        out.append(_normalize_b64(it["b64_json"]))
                    elif it.get("url"):
                        b64, err = _download_b64(it["url"], timeout)
                        if err:
                            return None, err
                        out.append(b64)
                elif isinstance(it, str) and it:
                    out.append(_normalize_b64(it))
        # 直接列表字段：cutouts / images / output（仅当上面没取到时，避免同图多键重复计入素材库）
        if not out:
            for k in ("cutouts", "images", "output"):
                v = obj.get(k)
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, str) and it:
                            if it.startswith("http://") or it.startswith("https://"):
                                b64, err = _download_b64(it, timeout)
                                if err:
                                    return None, err
                                out.append(b64)
                            else:
                                out.append(_normalize_b64(it))
                        elif isinstance(it, dict):
                            if it.get("b64_json") or it.get("b64") or it.get("image"):
                                out.append(_normalize_b64(it.get("b64_json") or it.get("b64") or it.get("image")))
                            elif it.get("url"):
                                b64, err = _download_b64(it["url"], timeout)
                                if err:
                                    return None, err
                                out.append(b64)
        # 单图字段（仅当上面都没取到时）
        if not out:
            for k in ("image", "b64"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    out.append(_normalize_b64(v))
    if not out:
        return None, "分割后端未返回任何图片（响应形态：%.120s）" % str(obj)[:120]
    return out, None


def _segment_http(b64, mode, base_url, key, model, timeout, extra=None, endpoint=None) -> dict:
    """HTTP 后端（OpenAI image-edit 兼容契约，**未经实跑核实**）。
    返回 {"cutouts":[b64,...]} 或 {"error":...}。key 绝不打日志。"""
    if not key:
        return {"error": "未设置分割 API Key：请在 AI 抠图设置里填写并保存"}
    body = {
        "model": model or "",
        "image": _data_url(b64),
        "prompt": _MODE_PROMPT.get(mode, _MODE_PROMPT["foreground"]),
        "response_format": "b64_json",
        "n": 1 if mode == "foreground" else 4,
    }
    if isinstance(extra, dict):
        body.update(extra)
    path = endpoint or _DEFAULT_ENDPOINT  # 端点路径不确定，可被 config 覆盖
    url = base_url.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,  # key 绝不打日志
        "User-Agent": _UA,
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "ignore")
        obj = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return {"error": "分割 HTTP %s：%s" % (e.code, detail)}
    except Exception as e:
        return {"error": "请求失败：%s" % e}
    cutouts, err = _extract_cutouts_from_json(obj, timeout=min(120, timeout))
    if err:
        return {"error": err}
    return {"cutouts": cutouts}


# nano-banana / gpt-image 通用标准比例 → 取与源图最接近的，让抠图输出比例匹配原图（用户反馈：默认 1:1 把宽图压方了）
_STD_RATIOS = {
    "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3,
    "16:9": 16 / 9, "9:16": 9 / 16, "5:4": 5 / 4, "4:5": 4 / 5, "21:9": 21 / 9, "9:21": 9 / 21,
}


def _nearest_ratio_from_b64(b64) -> str:
    """解码源图取宽高 → 最接近的标准比例串（如 '5:4'）。解不出 → 'auto'（交模型保留输入比例，不强压 1:1）。"""
    try:
        import io
        from PIL import Image
        raw = base64.b64decode(_strip_data_url_local(b64))
        w, h = Image.open(io.BytesIO(raw)).size
        if w <= 0 or h <= 0:
            return "auto"
        ar = w / float(h)
        return min(_STD_RATIOS.items(), key=lambda kv: abs(kv[1] - ar))[0]
    except Exception:  # noqa: BLE001 —— 解码/PIL 任何异常 → auto，绝不因取比例失败而中断抠图
        return "auto"


def _strip_data_url_local(b64: str) -> str:
    s = (b64 or "").strip()
    return s.split(",", 1)[1] if (s.startswith("data:") and "," in s) else s


def _clean_cutout_alpha(b64) -> str:
    """让生成式去背景结果【可靠透明】：两种情形都处理，保住内部白内容(白 H 原子/圆圈内白底)与彩色块。

    生成式抠图(grsai/nano-banana)结果不稳定：① 有 alpha 但残留半透明白雾；② 干脆返回不透明白底(无 alpha)。
    - 情形①(有透明)：雾 = 近白(min>=210)且非全不透明(alpha<240) ∪ 极淡(alpha<60) → 全透明。
    - 情形②(基本不透明)：把【连到图像边界】的近白(min>=230) flood 成透明（=去外层白背景）；内部白(H原子/圆圈白底)
      不连边界 → 保留。彩色(min 远<阈)一律不动。
    """
    try:
        import io
        import numpy as np
        import cv2
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(_strip_data_url_local(b64)))).convert("RGBA")
        a = np.asarray(im).copy()
        alpha = a[..., 3]
        rgbmin = a[..., :3].min(axis=2)
        if float((alpha < 200).mean()) < 0.02:           # 情形②：基本不透明（模型返回白底）→ 边界连通近白键出
            nw = (rgbmin >= 230).astype(np.uint8)
            num, lbl = cv2.connectedComponents(nw, connectivity=4)
            border = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1]); border.discard(0)
            if border:
                a[np.isin(lbl, list(border)), 3] = 0
        else:                                             # 情形①：有透明但有白雾 → 清雾
            haze = ((rgbmin >= 210) & (alpha < 240)) | (alpha < 60)
            if haze.any():
                a[haze, 3] = 0
        buf = io.BytesIO(); Image.fromarray(a, "RGBA").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 —— 清理失败不致命，返回原图（fail-open，不中断抠图）
        return b64


def _segment_grsai(b64, prompt, base_url, key, model, timeout=180, should_cancel=None) -> dict:
    """grsai 图生图编辑后端（复用已配置的 grsai · ai_client.generate_image）。
    用户已配好 key/base_url/model，零额外配置。返回 {"cutouts":[b64]} 或 {"error":...}。
    key 绝不打日志。should_cancel(): 真则中止——总限时（可经 UI「+时间」延长）由调用方经此回调强制。"""
    if not key:
        return {"error": "未配置 grsai Key：请在 AI 面板「设置」里填写 grsai key"}
    if not base_url:
        return {"error": "未配置 grsai 地址：请在 AI 面板「设置」里填写"}
    import ai_client  # 延迟导入：与 seg_client 顶层只依赖 ai_client._SSL 对齐
    instr = prompt or _DEFAULT_EDIT_INSTR
    ratio = _nearest_ratio_from_b64(b64)   # 让输出比例匹配原图（否则默认 1:1 把宽图压方，用户反馈）
    # per-read socket 超时放宽到 ≥300s：顶级 4K 模型(nano-banana-pro-4k-vip)慢、grsai 可能久不推流(队列/慢启动)，
    # urlopen 的 timeout 对【每次读】生效，太小→一次 idle 读就抛 "read operation timed out" 整体断（should_cancel/「+时间」
    # 只在读【之间】检查，救不了正阻塞的慢读，用户反馈 180s 顶级 4K 必超时）。总限时仍交 should_cancel（可 +时间 延长）。
    res = ai_client.generate_image(instr, key, base_url, ref_b64=b64, model=model, ratio=ratio,
                                   timeout=max(300, int(timeout)), should_cancel=should_cancel)
    if res.get("error"):
        e = res["error"]
        if "timed out" in e or "超时" in e:  # 把看不懂的 socket 超时翻成可操作提示（fail-loud 但友好）
            return {"error": "AI 抠图超时：顶级 4K 模型较慢或网络拥堵。可：①点「+时间」再等；"
                             "②设置里把「模型」换成低一档（如 nano-banana-pro 非 4K）更快出图；③稍后重试。原始：" + e}
        return {"error": e}  # fail-loud：透传 grsai 的违规/失败/HTTP 错误
    if res.get("b64"):
        return {"cutouts": [res["b64"]]}   # 半透明白雾清理在 segment_image 统一做（grsai+ppio）
    return {"error": "grsai 未返回图片"}


def _segment_ppio(b64, prompt, base_url, key, model, submit_endpoint,
                  result_endpoint, timeout, should_cancel=None) -> dict:
    """PPIO 派欧云 Qwen-Image-Edit 异步后端：提交→拿 task_id→轮询取图→下载转 b64。
    返回 {"cutouts":[b64]} 或 {"error":...}。任一步失败 fail-loud（含 HTTP 状态/截断响应）。
    key 绝不打日志。"""
    if not key:
        return {"error": "未设置 PPIO API Key：请在 AI 抠图设置里填写并保存"}
    base = (base_url or _PPIO_BASE).rstrip("/")
    submit_path = submit_endpoint or _PPIO_SUBMIT_ENDPOINT
    result_path = result_endpoint or _PPIO_RESULT_ENDPOINT
    instr = prompt or _DEFAULT_EDIT_INSTR
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,  # key 绝不打日志
        "User-Agent": _UA,
    }
    # —— 1. 提交：data-uri base64 形式的 image（顶层字符串字段，非 vision content 数组）——
    body = {
        "model": model or _PPIO_MODEL,
        "prompt": instr,
        "image": _data_url(b64),          # data:image/png;base64,<b64>
        "seed": -1,
        "output_format": "png",
        "watermark": False,
    }
    submit_url = base + submit_path
    task_id, err = _ppio_post_json(submit_url, body, headers, timeout)
    if err:
        return {"error": err}
    tid = (task_id or {}).get("task_id") if isinstance(task_id, dict) else None
    if not tid:
        return {"error": "PPIO 提交未返回 task_id（响应：%.200s）" % str(task_id)[:200]}
    # —— 2. 轮询取结果：GET ?task_id=（回退 POST body）；至多 timeout 秒 ——
    import time
    deadline = time.time() + max(1, timeout)
    last = None
    while time.time() < deadline:
        if should_cancel is not None and should_cancel():   # 用户取消 / 总限时到（可经 UI「+时间」延长）→ 停止轮询
            return {"error": "已取消或超时（点 +时间 续时后可重试）"}
        obj, err = _ppio_get_result(base + result_path, tid, headers, min(60, timeout))
        if err:
            return {"error": err}
        last = obj
        task = (obj or {}).get("task") or {}
        status = task.get("status") or ""
        if status == "TASK_STATUS_SUCCEED" or status.endswith("SUCCEED"):
            imgs = (obj or {}).get("images") or []
            if imgs and isinstance(imgs[0], dict):
                url = imgs[0].get("image_url")
                if url:  # TTL 仅 3600s，须立即下载转 b64（对齐 grsai/http 的 {"b64":...}）
                    if url.startswith("http://") or url.startswith("https://"):
                        out_b64, derr = _download_b64(url, min(120, timeout))
                        if derr:
                            return {"error": derr}
                        return {"cutouts": [out_b64]}
                    return {"cutouts": [_normalize_b64(url)]}  # 个别返回直接 b64
            # 宽松兜底：契约未核实，images 字段缺失/换名(data/output/b64…)时也尝试通用解析
            cutouts, ferr = _extract_cutouts_from_json(obj, min(120, timeout))
            if cutouts:
                return {"cutouts": cutouts}
            return {"error": "PPIO 成功但未取到图片（响应：%.200s）" % str(obj)[:200]}
        if status == "TASK_STATUS_FAILED" or status.endswith("FAILED"):
            return {"error": "PPIO 任务失败：%s" % (task.get("reason") or "未知原因")}
        # QUEUED/PROCESSING → 续轮询
        time.sleep(_PPIO_POLL_INTERVAL)
    return {"error": "PPIO 轮询超时（%ds 内未完成，最后响应：%.160s）" % (timeout, str(last)[:160])}


def _ppio_post_json(url, body, headers, timeout):
    """PPIO POST JSON → (解析后的 dict, err)。err 非 None 即失败（含 HTTP 状态/截断响应）。"""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return None, "PPIO 提交 HTTP %s：%s" % (e.code, detail)
    except Exception as e:
        return None, "PPIO 提交失败：%s" % e


def _ppio_get_result(url, task_id, headers, timeout):
    """PPIO 取结果：优先 GET ?task_id=，HTTP 405/404 时回退 POST body {"task_id"}。
    返回 (解析后的 dict, err)。err 非 None 即失败（含 HTTP 状态/截断响应）。"""
    import urllib.parse
    get_url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"task_id": task_id})
    try:
        req = urllib.request.Request(get_url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):  # 路由不收 GET → 回退 POST body 形式
            obj, err = _ppio_post_json(url, {"task_id": task_id}, headers, timeout)
            if err:
                return None, err.replace("提交", "取结果")
            return obj, None
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return None, "PPIO 取结果 HTTP %s：%s" % (e.code, detail)
    except Exception as e:
        return None, "PPIO 取结果失败：%s" % e


def _segment_rembg(b64) -> dict:
    """本地 rembg 后端（import 守卫，装了才用）。仅去背景（单张）。
    返回 {"cutouts":[b64]} 或 {"error":...}。"""
    try:
        from rembg import remove
    except Exception:
        return {"error": "未安装 rembg（可在「AI 抠图设置」改用『本地内置模型』，离线无需安装）"}
    try:
        src = base64.b64decode(b64)
        out = remove(src)  # PNG bytes（透明背景）
        return {"cutouts": [base64.b64encode(out).decode("ascii")]}
    except Exception as e:
        return {"error": "rembg 去背景失败：%s" % e}


# ---------- 本地内置模型抠图（onnxruntime + u2netp，离线·无需 rembg/pip）----------
_ORT_SESSION = None  # 缓存 onnxruntime session（懒加载，首张稍慢之后快）


def _local_model_path() -> str:
    """内置模型路径：冻结后在 _MEIPASS/models/，源码运行在 src/models/。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "models", "u2netp.onnx")


def _local_session():
    global _ORT_SESSION
    if _ORT_SESSION is None:
        import onnxruntime as ort
        path = _local_model_path()
        if not os.path.exists(path):
            raise FileNotFoundError("内置抠图模型缺失：%s" % path)
        so = ort.SessionOptions()
        so.log_severity_level = 3
        _ORT_SESSION = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
    return _ORT_SESSION


def _segment_local_onnx(b64) -> dict:
    """内置本地抠图：onnxruntime 跑 u2netp，numpy/cv2 做前后处理（对齐 u2net 标准流水线）。
    离线、无需安装 rembg/scipy/skimage。仅去背景（单张）。返回 {"cutouts":[b64]} 或 {"error":...}。"""
    try:
        import numpy as np
        import cv2
        sess = _local_session()
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": "本地抠图初始化失败（onnxruntime 缺失？）：%s" % e}
    try:
        arr = np.frombuffer(base64.b64decode(b64), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR HxWx3
        if img is None:
            return {"error": "源图解码失败"}
        h, w = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        inp = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_LANCZOS4).astype(np.float32)
        mx = float(inp.max())
        if mx > 0:
            inp = inp / mx
        mean = np.array([0.485, 0.456, 0.406], np.float32)
        std = np.array([0.229, 0.224, 0.225], np.float32)
        inp = (inp - mean) / std
        inp = inp.transpose(2, 0, 1)[None].astype(np.float32)  # 1x3x320x320
        name = sess.get_inputs()[0].name
        pred = sess.run(None, {name: inp})[0][0, 0]  # 320x320 显著度
        mi, ma = float(pred.min()), float(pred.max())
        pred = (pred - mi) / (ma - mi) if ma > mi else pred * 0.0
        mask = cv2.resize((pred * 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_LANCZOS4)
        rgba = np.dstack([rgb, mask]).astype(np.uint8)            # RGBA
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)            # cv2 写 PNG 用 BGRA
        ok, png = cv2.imencode(".png", bgra)
        if not ok:
            return {"error": "结果编码失败"}
        return {"cutouts": [base64.b64encode(png.tobytes()).decode("ascii")]}
    except Exception as e:
        return {"error": "本地抠图失败：%s" % e}


def segment_image(img_bytes_or_b64, mode="foreground", provider="grsai",
                  base_url="", key="", model="", timeout=180, endpoint=None,
                  prompt=None, result_endpoint=None, should_cancel=None) -> dict:
    """主入口（供 editor_window 后台线程调用）。
    返回 {"cutouts":[b64,...]} 或 {"error":...}。fail-loud：无后端配置即报错。
    prompt：编辑指令（grsai/ppio 用，None=默认去背景）；endpoint/result_endpoint：
    ppio 提交/取结果端点（None=取证默认值，可经 config 覆盖）。"""
    try:
        b64 = _normalize_b64(img_bytes_or_b64)
    except Exception as e:
        return {"error": "源图无效：%s" % e}
    if provider in ("grsai", "ppio"):
        # 生成式后端（grsai/ppio）易残留半透明白雾 → 统一清成干净透明背景（local/rembg 出干净 matte，不做）。
        if provider == "grsai":
            res = _segment_grsai(b64, prompt, base_url, key, model, timeout, should_cancel=should_cancel)
        else:
            res = _segment_ppio(b64, prompt, base_url, key, model, endpoint,
                                result_endpoint, timeout, should_cancel=should_cancel)
        if res.get("cutouts"):
            res["cutouts"] = [_clean_cutout_alpha(c) for c in res["cutouts"]]
        return res
    if provider == "local":
        return _segment_local_onnx(b64)  # 内置 onnxruntime+u2netp，离线去背景
    if provider == "rembg":
        res = _segment_rembg(b64)  # elements 模式在 rembg 上退化为单张去背景
        if res.get("error") and "未安装" in res["error"]:
            res = _segment_local_onnx(b64)  # rembg 没装→内置本地模型兜底，不让用户卡在报错
        return res
    if provider == "http":
        if not base_url:
            return {"error": "未配置分割后端地址（AI 抠图设置里填写）"}
        return _segment_http(b64, mode, base_url, key, model, timeout, endpoint=endpoint)
    return {"error": "未知分割后端：%s" % provider}
