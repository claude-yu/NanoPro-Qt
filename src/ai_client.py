"""grsai 文生图/图生图客户端 —— 移植 nanopro-editor/sciedit.py 的模型像素表 + 请求体 + SSE 解析。

Qt 单进程：直连上游 urllib + Bearer key（不走 WebView 的本机代理+token）。
模型族「比例→像素」表是与 grsai 官方 apifox 对齐的硬编码，**禁止自行推导/简化**，错一像素即失败或降质。
SSE 解析拆成纯函数（_parse_sse_line / extract_image_from_text）以便离线单测；真实网络调用部分需真实 Key 实跑。
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request

# —— 面板用的下拉常量（对齐 ai.js）——
MODELS = [
    ("nano-banana-pro", "nano-banana-pro (推荐·高质·真4K)"),
    ("nano-banana-2", "nano-banana-2 (快·真4K)"),
    ("nano-banana-pro-4k-vip", "nano-banana-pro-4k-vip (顶配4K)"),
    ("gpt-image-2-vip", "gpt-image-2-vip (文字最清晰·偏慢)"),
    ("gpt-image-2", "gpt-image-2 (仅1K·最省)"),
    ("nano-banana", "nano-banana (基础·省钱)"),
    ("nano-banana-fast", "nano-banana 快速版"),
    ("nano-banana-2-cl", "nano-banana 2 高清"),
    ("nano-banana-2-cl-4k", "nano-banana 2 4K"),
    ("nano-banana-pro-vt", "nano-banana pro 竖版"),
    ("nano-banana-pro-cl", "nano-banana pro 高清"),
    ("nano-banana-pro-vip", "nano-banana pro VIP"),
    ("flux-kontext-pro", "Flux Kontext Pro"),
    ("flux-kontext-max", "Flux Kontext Max"),
    ("flux-pro-1.1", "Flux Pro 1.1"),
    ("flux-pro-1.1-ultra", "Flux Pro 1.1 Ultra"),
]
NODES = [
    ("https://grsaiapi.com", "🌎 国外/全球节点 (grsaiapi.com·VPN用)"),
    ("https://grsai.dakka.com.cn", "🇨🇳 国内节点 (grsai.dakka.com.cn·不开VPN)"),
    ("", "✏️ 自定义(用下面地址)"),
]
# 生图中转站（每站记各自的地址/Key/模型/接口格式）。fmt: grsai=grsai 私有 /v1/api/generate；
# openai=OpenAI 标准图片接口 /v1/images/generations(文生图)+/v1/images/edits(图生图)。
# (pid, 显示名, 默认地址, fmt)
GEN_PROVIDERS = [
    ("grsai_global", "grsai 国外节点 (grsaiapi.com·VPN)", "https://grsaiapi.com", "grsai"),
    ("grsai_cn", "grsai 国内节点 (dakka·不开VPN)", "https://grsai.dakka.com.cn", "grsai"),
    ("openai_relay", "自定义中转 · OpenAI 图片接口", "", "openai"),
    ("grsai_relay", "自定义中转 · grsai 格式", "", "grsai"),
]
RESOLUTIONS = ["1K", "2K", "4K"]
RATIOS = ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"]

# 图生图风格强制匹配前缀（照搬 sci-figure / sci_figure.py 的 IMAGE_REF_PREFIX）：
# 有参考图时前置，逼模型严格沿用参考图的配色/图标渲染风格/线宽/整体观感，使输出与同一图系一致。
# 纯文生图（无参考图）不加此前缀。
IMAGE_REF_PREFIX = (
    "IMPORTANT: You MUST strictly match the reference image's visual style — "
    "use the SAME color palette, SAME icon rendering style (flat/outlined/gradient), "
    "SAME line weight, and SAME overall aesthetic. "
    "The output should look like it belongs to the same figure series. "
    "Scientific illustration, clean white background, no watermarks. "
    "Figure description: "
)

# gpt-image-2 基础版：每比例单一像素（约 1K 级）。
_GPT2_SIZES = {
    "1:1": "1024x1024", "16:9": "1672x941", "9:16": "941x1672",
    "4:3": "1443x1090", "3:4": "1090x1443", "3:2": "1536x1024", "2:3": "1024x1536",
    "5:4": "1408x1120", "4:5": "1120x1408", "21:9": "1920x832",
}
# gpt-image-2-vip：比例 × 分辨率(1K/2K/4K) → 像素（4K 降 2880²，因单边≤3840 且总像素≤8.29M）。
_GPTVIP_SIZES = {
    "1:1":  {"1K": "1024x1024", "2K": "2048x2048", "4K": "2880x2880"},
    "16:9": {"1K": "1280x720",  "2K": "2048x1152", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280",  "2K": "1152x2048", "4K": "2160x3840"},
    "4:3":  {"1K": "1152x864",  "2K": "2304x1728", "4K": "3264x2448"},
    "3:4":  {"1K": "864x1152",  "2K": "1728x2304", "4K": "2448x3264"},
    "3:2":  {"1K": "1536x1024", "2K": "2048x1360", "4K": "3504x2336"},
    "2:3":  {"1K": "1024x1536", "2K": "1360x2048", "4K": "2336x3504"},
    "5:4":  {"1K": "1120x896",  "2K": "2240x1792", "4K": "3200x2560"},
    "4:5":  {"1K": "896x1120",  "2K": "1792x2240", "4K": "2560x3200"},
    "21:9": {"1K": "1456x624",  "2K": "2912x1248", "4K": "3840x1648"},
}


def _make_ssl_context():
    """https CA：优先 certifi（PyInstaller 冻结后必需），失败回退系统默认。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


_SSL = _make_ssl_context()


def friendly_gen_error(raw):
    """把 grsai 的英文/JSON 错误转成简短可操作中文；未知错误截断，避免长串 JSON 撑爆任务面板。"""
    s = (raw or "").strip()
    low = s.lower()
    table = (
        ("insufficient credits", "grsai 余额不足，请到 grsai 账户充值后再试"),
        ("apikey error", "grsai API Key 无效，请到「设置」检查/重填 Key"),
        ("api key", "grsai API Key 无效，请到「设置」检查/重填 Key"),
        ("rate limit", "请求过于频繁，请稍后再试"),
        ("violation", "内容被判违规拦截，请改描述后重试"),
    )
    for k, cn in table:
        if k in low:
            return cn
    return s if len(s) <= 120 else s[:117] + "…"


def build_gen_body(prompt, imgs, resolution, ratio, model) -> dict:
    """按模型族拼 grsai /v1/api/generate 请求体（移植 _build_gen_body）。
    gpt-image 用 aspectRatio 像素串(两版查不同表)；nano-banana 用 imageSize+比例；auto 交模型定。"""
    model = (model or "gpt-image-2-vip").strip()
    res = (resolution or "2K").upper()
    is_auto = (ratio or "").strip().lower() == "auto"
    rt = ratio if ratio in _GPTVIP_SIZES else "1:1"
    level = res if res in ("1K", "2K", "4K") else "2K"
    body = {"model": model, "prompt": prompt or "", "images": imgs or [], "replyType": "stream"}
    if model == "gpt-image-2":
        body["aspectRatio"] = "auto" if is_auto else _GPT2_SIZES.get(rt, _GPT2_SIZES["1:1"])
    elif model.startswith("gpt-image"):
        body["aspectRatio"] = "auto" if is_auto else (_GPTVIP_SIZES[rt].get(level) or _GPTVIP_SIZES[rt]["1K"])
    else:
        body["imageSize"] = level
        body["aspectRatio"] = "auto" if is_auto else (ratio or "1:1")
    return body


def _parse_sse_line(line: str):
    """单行 SSE → 事件 dict 或 None（去掉 data: 前缀、跳过空行/[DONE]/非 json）。纯函数，可测。"""
    line = (line or "").strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except Exception:
        return None


def extract_image_from_text(text: str):
    """把整段流文本当若干行解析，返回 (img_url, last_status, error)。
    error 非 None 表示违规/失败（fail-loud）。移植 generate_image 的逐行解析 + 整段兜底。"""
    img_url = None
    last_status = None
    for raw in text.splitlines():
        ev = _parse_sse_line(raw)
        if ev is None:
            continue
        st = ev.get("status")
        if st:
            last_status = st
        if st == "violation":
            return None, st, "内容被判违规拦截，请改描述后重试"
        if st == "failed":
            return None, st, friendly_gen_error(ev.get("error") or "未知原因")
        rl = ev.get("results") or []
        if rl and isinstance(rl[0], dict) and rl[0].get("url"):
            img_url = rl[0]["url"]
            if st == "succeeded":
                break
    if not img_url:  # 退路：整段当单个 json
        try:
            s = text.strip()
            if s.startswith("data:"):
                s = s[5:].strip()
            j = json.loads(s)
            last_status = j.get("status", last_status)
            if j.get("status") == "failed":
                return None, last_status, friendly_gen_error(j.get("error") or "未知原因")
            rl = j.get("results") or []
            if rl and isinstance(rl[0], dict):
                img_url = rl[0].get("url")
        except Exception:
            pass
    return img_url, last_status, None


def generate_image(prompt, key, base_url, ref_b64=None, resolution="2K", ratio="1:1",
                   model="gpt-image-2-vip", on_progress=None, should_cancel=None, timeout=300,
                   negative=None) -> dict:
    """文生图/图生图（移植 sciedit.py generate_image，去掉本机代理→直连）。
    返回 {'b64':...} 或 {'error':...}。on_progress(percent) 回报进度；should_cancel() 真则中止。
    逐行读流保活，避免高分 gpt-image 被中转掐断。key 只在本函数用、绝不打日志。"""
    if not key:
        return {"error": "未设置 API Key：请在 AI 面板「设置」里填写并保存（或在 .env 设 GEMINI_API_KEY）"}
    imgs = []
    if isinstance(ref_b64, str) and ref_b64:
        imgs = [ref_b64]
    elif isinstance(ref_b64, (list, tuple)):
        imgs = [x for x in ref_b64 if x]
    if imgs:  # 图生图：前置风格匹配前缀（照搬 sci-figure），逼模型沿用参考图风格
        prompt = IMAGE_REF_PREFIX + (prompt or "")
    if negative:  # 负面词并入正向 prompt（grsai 无独立 negativePrompt 字段，Gemini/GPT-image 通行做法）
        prompt = (prompt or "") + "\n\nAvoid the following elements: " + negative
    body = build_gen_body(prompt, imgs, resolution, ratio, model)
    body["replyType"] = "stream"
    url = (base_url or "https://grsaiapi.com").rstrip("/") + "/v1/api/generate"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "Accept": "text/event-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    img_url = None
    last_status = None
    chunks = []
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            for raw in r:  # 逐行读流，连接保活
                if should_cancel and should_cancel():
                    return {"error": "已停止"}
                chunks.append(raw)
                ev = _parse_sse_line(raw.decode("utf-8", "ignore"))
                if ev is None:
                    continue
                st = ev.get("status")
                if st:
                    last_status = st
                if st == "violation":
                    return {"error": "内容被判违规拦截，请改描述后重试"}
                if st == "failed":
                    return {"error": friendly_gen_error(ev.get("error") or "未知原因")}
                if on_progress and isinstance(ev.get("progress"), (int, float)):
                    on_progress(float(ev["progress"]))
                rl = ev.get("results") or []
                if rl and isinstance(rl[0], dict) and rl[0].get("url"):
                    img_url = rl[0]["url"]
                    if st == "succeeded":
                        break
        if not img_url:  # 退路：整段当单 json 解析
            full = b"".join(chunks).decode("utf-8", "ignore")
            img_url, last_status, err = extract_image_from_text(full)
            if err:
                return {"error": err}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return {"error": "grsai HTTP %s：%s" % (e.code, friendly_gen_error(detail))}
    except Exception as e:
        return {"error": "请求失败：%s" % e}
    if not img_url:
        return {"error": "grsai 未返回图片 URL（status=%s）" % last_status}
    try:  # python 下载结果图 → base64（180s：4K 顶级图较大、走 VPN/国外节点下载慢，120s 易掐断）
        ireq = urllib.request.Request(img_url, headers={"User-Agent": headers["User-Agent"]})
        with urllib.request.urlopen(ireq, timeout=180, context=_SSL) as ir:
            return {"b64": base64.b64encode(ir.read()).decode("ascii")}
    except Exception as e:
        return {"error": "下载结果图失败：%s" % e}


# ============================================================ OpenAI 标准图片接口（中转站 fmt=openai）
# 与 grsai 完全不同：/v1/images/generations(文生图) + /v1/images/edits(图生图 multipart)，返回 data[].b64_json/url。
_OPENAI_SIZES = {  # 比例 → OpenAI 安全尺寸（gpt-image-1 主用方/竖/横三档；auto 交服务端）
    "1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536",
    "16:9": "1536x1024", "9:16": "1024x1536", "4:3": "1536x1024", "3:4": "1024x1536",
    "5:4": "1536x1024", "4:5": "1024x1536", "21:9": "1536x1024",
}


def openai_size(ratio) -> str:
    r = (ratio or "").strip().lower()
    if r in ("", "auto"):
        return "auto"
    return _OPENAI_SIZES.get(ratio, "1024x1024")


def _strip_data_url(b64: str) -> str:
    s = (b64 or "").strip()
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1]
    return s


def build_multipart_edit(model, prompt, size, imgs):
    """拼 /v1/images/edits 的 multipart/form-data：全部参考图(b64→bytes) + model/prompt/size/n。
    多张→字段名 image[]（gpt-image-1 多图编辑）；单张→image（dall-e-2 等通用）。返回 (body_bytes, content_type)。纯函数，可离线测。"""
    boundary = "----SciEditBoundary7MA4YWxkTrZu0gW"
    segs = []

    def add_field(name, value):
        segs.append(("--%s\r\n" % boundary).encode()
                    + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
                    + str(value).encode("utf-8") + b"\r\n")

    add_field("model", model)
    add_field("prompt", prompt or "")
    if size and size != "auto":
        add_field("size", size)
    add_field("n", "1")
    field_name = "image[]" if len(imgs) > 1 else "image"
    for i, b64 in enumerate(imgs):
        img_bytes = base64.b64decode(_strip_data_url(b64))
        head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="ref%d.png"\r\nContent-Type: image/png\r\n\r\n'
                % (boundary, field_name, i)).encode()
        segs.append(head + img_bytes + b"\r\n")
    segs.append(("--%s--\r\n" % boundary).encode())
    return b"".join(segs), "multipart/form-data; boundary=" + boundary


def generate_image_openai(prompt, key, base_url, ref_b64=None, resolution="2K", ratio="1:1",
                          model="gpt-image-1", on_progress=None, should_cancel=None, timeout=300,
                          negative=None) -> dict:
    """OpenAI 兼容图片接口生图。返回 {'b64':...} 或 {'error':...}。
    无 SSE 进度 → on_progress 只报 10/90 两点；负面词并入 prompt（OpenAI 无独立 negativePrompt 字段）。"""
    if not key:
        return {"error": "未设置 API Key：请在 AI 面板「设置」里填写并保存"}
    imgs = []
    if isinstance(ref_b64, str) and ref_b64:
        imgs = [ref_b64]
    elif isinstance(ref_b64, (list, tuple)):
        imgs = [x for x in ref_b64 if x]
    if imgs:  # 图生图：前置风格匹配前缀（与 grsai 路径一致）
        prompt = IMAGE_REF_PREFIX + (prompt or "")
    if negative:
        prompt = (prompt or "") + "\n\nAvoid the following elements: " + negative
    model = (model or "gpt-image-1").strip()
    size = openai_size(ratio)
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    if should_cancel and should_cancel():
        return {"error": "已停止"}
    if on_progress:
        on_progress(10.0)
    try:
        if imgs:  # 图生图 → /v1/images/edits（multipart）
            data, ctype = build_multipart_edit(model, prompt, size, imgs)
            headers = {"Authorization": "Bearer " + key, "Content-Type": ctype, "Accept": "application/json"}
            req = urllib.request.Request(root + "/v1/images/edits", data=data, method="POST", headers=headers)
        else:     # 文生图 → /v1/images/generations（json）
            body = {"model": model, "prompt": prompt or "", "n": 1}
            if size and size != "auto":
                body["size"] = size
            headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
            req = urllib.request.Request(root + "/v1/images/generations",
                                         data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read()
        j = json.loads(raw.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        return {"error": "OpenAI 图片 HTTP %s：%s" % (e.code, detail)}
    except Exception as e:
        return {"error": "请求失败：%s" % e}
    if on_progress:
        on_progress(90.0)
    data = j.get("data") or []
    if not data or not isinstance(data[0], dict):
        return {"error": "OpenAI 图片接口未返回图片：%s" % (str(j.get("error") or j)[:200])}
    d0 = data[0]
    if d0.get("b64_json"):
        return {"b64": d0["b64_json"]}
    if d0.get("url"):
        try:
            ireq = urllib.request.Request(d0["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(ireq, timeout=120, context=_SSL) as ir:
                return {"b64": base64.b64encode(ir.read()).decode("ascii")}
        except Exception as e:
            return {"error": "下载结果图失败：%s" % e}
    return {"error": "OpenAI 图片接口返回里既无 b64_json 也无 url"}
