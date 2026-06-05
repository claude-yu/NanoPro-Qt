"""AI 对话生成绘图提示词客户端 —— OpenAI 兼容 /chat/completions（deepseek/glm/小米/自定义中转）。

与 grsai 文生图 key 完全独立（chat_api_key vs api_key，见 config）。urllib 直连 + Bearer key。
SSE 解析拆成纯函数（_parse_chat_sse_line / extract_text_from_sse / _endpoint）以便离线单测；
真实网络调用部分需真实 Key 实跑。复用 ai_client 的模块级 _SSL（不重写 CA 上下文）。

诚信红线（呼应全局 plotting.md AI 配图规则）：SYSTEM_PROMPT 约束模型只用通用标签、绝不编造残基号/数值/距离。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from ai_client import _SSL  # 复用 ai_client 已构造的模块级 https CA 上下文，不重写

# —— 面板用的 provider 下拉常量 ——
# (provider_id, label, default_base_url, default_model)；base_url 末尾不带 /chat/completions（请求时拼接）。
CHAT_PROVIDERS = [
    # label 只放【商家名】，不再嵌固定模型（模型现由「拉取模型」/可编辑下拉单独选）。第4元=该商家默认模型，
    # 选商家时自动填进模型框，但用户可改/拉取。
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
    ("glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    # 小米 MiMo：官方 OpenAI 兼容端点未核实 → 默认留空强制手填，避免回填一个看似可用实则错误的地址。
    ("xiaomi", "小米 MiMo（需手填官方地址）", "", ""),
    ("custom", "自定义中转…", "", ""),  # 空=用面板手填地址/模型
]

# 内置 system 角色：把用户口语图意需求转成可直接喂文生图模型的英文提示词。
SYSTEM_PROMPT = (
    "You are a friendly, helpful assistant for researchers designing scientific figures. You also turn a "
    "described figure into a ready-to-use English prompt for a text-to-image model. Read the user's intent "
    "first and respond accordingly — do NOT mechanically emit a figure prompt for every message:\n"
    "- Greeting, small talk, or a question about what you can do → reply briefly and naturally IN THE "
    "USER'S LANGUAGE (Chinese if they wrote Chinese). Invite them to describe the figure they want.\n"
    "- A vague or ambiguous figure request → ask ONE short clarifying question (in the user's language) "
    "instead of guessing.\n"
    "- A clear figure description → output ONLY one English image-generation prompt: no preamble, no "
    "explanation, no markdown fences. Tailor it SPECIFICALLY to that subject; vary the wording, composition "
    "and emphasis for each figure — do NOT reuse a fixed boilerplate opening. Target a clean journal-style "
    "flat-vector / biorender look (white background, clear English labels, no watermark), but phrase it "
    "naturally for each case rather than from a template.\n"
    "INTEGRITY (critical): use only generic labels such as Protein, Ligand, Binding Pocket, H-bond. NEVER "
    "invent residue numbers, numeric values, distances, or any specific data — those must come from the "
    "user's real analysis, not from you."
)


def _endpoint(base: str) -> str:
    """把 provider base_url 拼成完整 /chat/completions 端点（纯函数，可测）。
    启发式：含 /chat → 视为已是全路径，原样返回；含 /v1 或 /v4 → 只补 /chat/completions；
    纯根域名 → 补 /v1/chat/completions（deepseek 等根域名情形）。"""
    b = (base or "").rstrip("/")
    if "/chat" in b:
        return b
    if "/v1" in b or "/v4" in b:
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def _models_endpoint(base: str) -> str:
    """把 provider base_url 拼成 /models 端点（纯函数，可测）。仿 _endpoint 启发式。
    参考 sciedit-ps 的 base.match(/\\/v\\d+$/)?'/models':'/v1/models'：
    base 已含 /v1|/v3|/v4 段（如 GLM 的 .../paas/v4）→ 直接 +/models；纯根域名 → +/v1/models。
    空 base → 空串（由 list_models 提前拦截，不发裸请求）。"""
    b = (base or "").rstrip("/")
    if not b:
        return ""
    # 仅当结尾是版本段(/v数字)才直接 +/models；用正则避免 'in' 误命中 /v10 或中段 /v1/chat（审核 LOW，仿参考 /\v\d+$/）
    if re.search(r"/v\d+$", b):
        return b + "/models"
    return b + "/v1/models"


def parse_models(obj) -> list:
    """解析模型列表响应 → 排序去重的模型名列表（纯函数，离屏可测）。
    支持三种格式（参考 sciedit-ps / 大香蕉）：
      - dict 带 data[]（OpenAI 风格，取 m.id or m.name）
      - dict 带 models[]
      - 直接数组（元素为 str 或 dict）
    元素可为 str（直接用）或 dict（取 id or name）；filter(Boolean) + 去重保序 + sort。坏输入返回 []，不抛。"""
    if isinstance(obj, dict):
        items = obj.get("data") or obj.get("models") or []
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    if not isinstance(items, list):
        return []
    out = []
    for m in items:
        if isinstance(m, str):
            name = m
        elif isinstance(m, dict):
            name = m.get("id") or m.get("name")
        else:
            name = None
        if name:
            out.append(str(name).strip())
    seen, uniq = set(), []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return sorted(uniq)


def list_models(base_url, key, timeout=20):
    """GET {models 端点} + Bearer key → (models, err)。fail-loud：
    无 key → ([], "未设置 API Key…")（不发请求）；无 base → ([], "未填写 API 地址")。
    成功 → (models, None)；解析成功但空 → ([], "返回成功但没解析到模型")；
    HTTP 错 → ([], "HTTP {code} — {body[:200]}")；其它异常 → ([], "拉取失败：…")。
    key 只在本函数用、绝不打日志（同 chat_complete 约定）。"""
    if not key:
        return [], "未设置 API Key：先填 Key 再拉取模型"
    url = _models_endpoint(base_url)
    if not url:
        return [], "未填写 API 地址"
    headers = {
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            body = r.read().decode("utf-8", "ignore")
        try:
            obj = json.loads(body)
        except Exception:
            return [], "返回成功但没解析到模型"
        models = parse_models(obj)
        if not models:
            return [], "返回成功但没解析到模型"
        return models, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return [], "HTTP %s — %s" % (e.code, detail)
    except Exception as e:
        return [], "拉取失败：%s" % e


def build_chat_body(messages, model, stream=False, temperature=0.7) -> dict:
    """拼 OpenAI 标准 /chat/completions 请求体（纯函数）。
    messages 由调用方传（含 {"role":"system","content":SYSTEM_PROMPT} + 历史 + 当前 user）。"""
    return {
        "model": (model or "").strip(),
        "messages": messages or [],
        "stream": bool(stream),
        "temperature": temperature,
    }


def _parse_chat_sse_line(line: str):
    """单行 SSE → 事件 dict 或 None（去 data: 前缀、跳过空行/[DONE]/非 json）。纯函数，可测。"""
    line = (line or "").strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except Exception:
        return None


def extract_text_from_sse(text: str):
    """整段流按行解析 → (content, error)。逐行取 choices[0].delta.content 拼接（流式）；
    遇 error 字段则返回 (已拼接, 错误文案)（fail-loud）。非流式整段 json（choices[0].message.content）兜底。"""
    parts = []
    for raw in (text or "").splitlines():
        ev = _parse_chat_sse_line(raw)
        if ev is None:
            continue
        if ev.get("error"):
            err = ev["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return "".join(parts), "对话失败：%s" % (msg or "未知原因")
        ch = ev.get("choices") or []
        if ch and isinstance(ch[0], dict):
            # 流式 delta.content；非流式整段 json 的 message.content 兜底（同一行也能命中）
            piece = (ch[0].get("delta") or {}).get("content")
            if not piece:
                piece = (ch[0].get("message") or {}).get("content")
            if piece:
                parts.append(piece)
    joined = "".join(parts)
    if joined:
        return joined, None
    # parts 为空：非流式响应可能是 pretty-print 多行 JSON（逐行解析不出）→ 整段再 json.loads 一次兜底
    try:
        obj = json.loads((text or "").strip())
    except Exception:
        obj = None
    if isinstance(obj, dict):
        if obj.get("error"):
            err = obj["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return "", "对话失败：%s" % (msg or "未知原因")
        ch = obj.get("choices") or []
        if ch and isinstance(ch[0], dict):
            c = (ch[0].get("message") or {}).get("content") or (ch[0].get("delta") or {}).get("content")
            if c:
                return c, None
    return "", "对话失败：未解析到回复内容（响应为空或格式异常）"  # fail-loud，不静默返回空


def chat_complete(messages, key, base_url, model, stream=False,
                  on_delta=None, should_cancel=None, timeout=120) -> dict:
    """调 OpenAI 兼容 /chat/completions。返回 {"text":拼接全文} 或 {"error":...}。
    无 key → fail-loud return error，不发请求。key 只在本函数用、绝不打日志。
    stream=True：逐行读流，每行取 delta.content 调 on_delta(piece)；should_cancel() 真则中止。
    stream=False：一次性读 body，取 message.content。"""
    if not key:
        return {"error": "未设置对话模型 API Key：请在对话面板「设置」里填写并保存"}
    url = _endpoint(base_url or "https://api.deepseek.com")
    body = build_chat_body(messages, model, stream=stream)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "Accept": "text/event-stream" if stream else "application/json",
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            if stream:
                parts = []
                for raw in r:  # 逐行读流
                    if should_cancel and should_cancel():
                        return {"error": "已停止"}
                    ev = _parse_chat_sse_line(raw.decode("utf-8", "ignore"))
                    if ev is None:
                        continue
                    if ev.get("error"):
                        err = ev["error"]
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        return {"error": "对话失败：%s" % (msg or "未知原因")}
                    ch = ev.get("choices") or []
                    if ch and isinstance(ch[0], dict):
                        piece = (ch[0].get("delta") or {}).get("content")
                        if piece:
                            parts.append(piece)
                            if on_delta:
                                on_delta(piece)
                return {"text": "".join(parts)}
            full = r.read().decode("utf-8", "ignore")
            content, err = extract_text_from_sse(full)
            if err:
                return {"error": err}
            return {"text": content}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return {"error": "对话 HTTP %s：%s" % (e.code, detail)}
    except Exception as e:
        return {"error": "请求失败：%s" % e}
