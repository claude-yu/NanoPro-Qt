"""AI 连接配置（中转商地址 / Key / 模型）—— 本机安全存储。

移植 nanopro-editor/sciedit.py 的 Key/配置逻辑。Key 只存本机 ~/.sciedit/config.json，
**绝不进仓库 / 不写 QSettings(注册表) / 不进导出/工程文件 / 不打日志**；对外只回 has_key + 掩码尾号。
Qt 是单一可信进程，无需 WebView 的本机 HTTP 代理 + token（那是绕 WebView2 沙箱用的）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE = "https://grsaiapi.com"


# 应用版本（与 NanoPro_Setup.iss MyAppVersion 同步；自动更新据此比对 GitHub Releases）。发版时一起改。
APP_VERSION = "1.18.2"


def _app_dir() -> Path:
    return Path(__file__).resolve().parent


def _env_file_candidates():
    # ~/tools/.env（不硬编码用户名）。源码运行时再兼容「程序目录 .env」老用法；
    # 但【冻结/打包后绝不读包内 .env】——_app_dir() 此时在 _MEIPASS 里，防止任何误放进 src/ 的
    # .env 被卷进发行包后在用户机上被读出（脱敏铁律：Key 只来自用户家目录 ~/.sciedit 或 ~/tools/.env）。
    import sys
    cands = [Path.home() / "tools" / ".env"]
    if not getattr(sys, "frozen", False):
        cands.append(_app_dir() / ".env")
    return cands


def _env_val(name: str):
    """读环境变量：先进程环境，再 .env 文件。缺失返回 None。值只回给调用方，绝不打日志。"""
    v = os.environ.get(name)
    if v:
        return v.strip()
    for p in _env_file_candidates():
        try:
            if p.exists():
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, val = line.split("=", 1)
                    if k.strip() == name:
                        return val.strip().strip('"').strip("'")
        except Exception:
            pass
    return None


def _config_path() -> Path:
    return Path.home() / ".sciedit" / "config.json"


def _load_config() -> dict:
    try:
        p = _config_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_config(d: dict) -> bool:
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(p, 0o600)  # 仅所有者读写：config.json 含 API key，限权防同机他用户读取（Windows 近似 no-op，ACL 已护）
        except OSError:
            pass
        return True
    except Exception:
        return False


def mask_key(k) -> str:
    """掩码 key 供核对/切换：只露前 3 后 4，中间省略，不可还原。空→空串。"""
    if not k:
        return ""
    k = str(k)
    if len(k) <= 8:
        return "…" + k[-2:]
    return k[:3] + "…" + k[-4:]


def _gen_pid_for_base(bu) -> str:
    """老单一 grsai 配置迁移时按地址推断中转站 id。"""
    return "grsai_cn" if "dakka" in (bu or "").lower() else "grsai_global"


def _gen_conns(cfg: dict) -> dict:
    """每个生图中转站一套 {base_url, api_key, model, fmt}（fmt=grsai|openai）。
    首次从老单一字段(base_url/api_key/model) 迁进 grsai 桶（只读返回，不落盘；落盘在 set_connection）。
    迁移一律按 fmt='grsai'——旧版生图只支持 grsai 一种接口（ai_client 当时仅有 grsai），故老配置必为 grsai，无歧义。"""
    conns = cfg.get("gen_conns")
    if isinstance(conns, dict):
        return conns
    conns = {}
    if cfg.get("base_url") or cfg.get("api_key") or cfg.get("model"):
        pid = _gen_pid_for_base(cfg.get("base_url"))
        conns[pid] = {
            "base_url": str(cfg.get("base_url") or ""),
            "api_key": str(cfg.get("api_key") or ""),
            "model": str(cfg.get("model") or ""),
            "fmt": "grsai",
        }
    return conns


def get_gen_active_pid() -> str:
    """上次使用的生图中转站 id（重开恢复到它）。无存档→按老地址推断→默认 grsai_global。"""
    cfg = _load_config()
    return str(cfg.get("gen_active_pid") or _gen_pid_for_base(cfg.get("base_url")) or "grsai_global")


def _gen_active(cfg, pid):
    return str(pid if pid is not None else (cfg.get("gen_active_pid") or _gen_pid_for_base(cfg.get("base_url")) or "grsai_global"))


def read_key(pid=None):
    """某生图中转站的 Key：优先该站桶里存的；grsai 站再回退 .env(GEMINI/GRSAI)。pid=None→当前活动站。
    各站独立——openai 中转站没存 key 就不串用 grsai 的 env key（防错把 grsai key 发去别家）。仅本进程用，绝不回前端/日志。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    pid = _gen_active(cfg, pid)
    bucket = conns.get(pid) or {}
    if bucket.get("api_key"):
        return str(bucket["api_key"]).strip()
    fmt = bucket.get("fmt") or ("grsai" if str(pid).startswith("grsai") else "")
    if fmt == "grsai":  # GEMINI/GRSAI 是 grsai 的 key，只兜底 grsai 格式的站
        for name in ("GEMINI_API_KEY", "GRSAI_API_KEY", "GRSAI_KEY"):
            v = _env_val(name)
            if v:
                return v
    return None


def grsai_base(pid=None) -> str:
    """某站地址（pid=None→活动站）；归一去尾 /v1。无站存档→老字段/env/默认。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    bucket = conns.get(_gen_active(cfg, pid)) or {}
    b = (bucket.get("base_url") or cfg.get("base_url") or _env_val("GEMINI_BASE_URL")
         or _env_val("GRSAI_BASE_URL") or DEFAULT_BASE)
    b = str(b).rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


def get_gen_fmt(pid=None) -> str:
    """某站接口格式 grsai|openai（pid=None→活动站）。默认 grsai。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    return str((conns.get(_gen_active(cfg, pid)) or {}).get("fmt") or "grsai")


def get_connection(pid=None) -> dict:
    """某站面板回显：地址/模型/格式/是否已设 key（绝不回明文，只回掩码尾号）。pid=None→活动站。
    saved=该站是否已有存档（地址/模型/自有 key 任一非空）。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    pid = _gen_active(cfg, pid)
    bucket = conns.get(pid) or {}
    key = read_key(pid)
    return {
        "base_url": bucket.get("base_url") or "",
        "model": bucket.get("model") or "",
        "fmt": bucket.get("fmt") or "grsai",
        "has_key": bool(key),
        "key_hint": mask_key(key),
        "saved": bool(bucket.get("base_url") or bucket.get("model") or bucket.get("api_key")),
        "pid": pid,
    }


def get_gen_models(pid) -> list:
    """读某站记住的模型名列表（拉取到的 + 手填）。存结构 cfg['gen_models']={pid:[names]}。非敏感。"""
    cfg = _load_config()
    d = cfg.get("gen_models") or {}
    if not isinstance(d, dict):
        return []
    v = d.get(str(pid)) or []
    return [str(x) for x in v if x] if isinstance(v, list) else []


def set_gen_models(pid, models) -> bool:
    """记住某站的模型名列表（去重保序，限 120 条）。非敏感，复用 0o600 的 config.json。"""
    cfg = _load_config()
    d = cfg.get("gen_models")
    if not isinstance(d, dict):
        d = {}
    seen, uniq = set(), []
    for m in (models or []):
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    d[str(pid or "grsai_global")] = uniq[:120]
    cfg["gen_models"] = d
    return _save_config(cfg)


# 提示词预设分类（用户可归类；只保留少量通用类别，绝大多数交用户自建）。
PROMPT_PRESET_CATS = ["通用", "优化", "配色", "风格", "标注"]

# 内置只保留【一个通用基础预设】（英文——nano-banana/gpt-image 等模型对英文提示词响应更好）。
# 其余全交用户自己建/导入（对齐用户要求：内置别太多）。
_BUILTIN_PROMPT_PRESETS = [
    {"name": "通用基础", "category": "通用",
     "text": "clean scientific illustration, white background, clear and readable labels, "
             "high quality, sharp edges, consistent color palette, no watermark"},
]


def prompt_presets_path() -> Path:
    """用户提示词预设的【独立 JSON 文件】路径（对齐大香蕉独立预设文件）：~/.sciedit/prompt_presets.json。
    用户可直接把此文件复制到别处/拷给别人复用，也可经 UI 导入。"""
    return Path.home() / ".sciedit" / "prompt_presets.json"


def _load_user_presets() -> list:
    try:
        p = prompt_presets_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 —— 文件损坏/无法读 → 当空，不崩
        pass
    return []


def _save_user_presets(lst) -> bool:
    try:
        p = prompt_presets_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def get_prompt_presets() -> list:
    """提示词预设 = 1 个内置通用基础 + 用户存的（独立文件 ~/.sciedit/prompt_presets.json）。
    返回 [{name,text,category,builtin}]，内置在前。"""
    out = [dict(p, builtin=True) for p in _BUILTIN_PROMPT_PRESETS]
    for p in _load_user_presets():
        if isinstance(p, dict) and p.get("name") and (p.get("text") or p.get("content")):
            out.append({"name": str(p["name"]), "text": str(p.get("text") or p.get("content")),
                        "category": str(p.get("category") or "通用"), "builtin": False})
    return out


def add_prompt_preset(name, text, category="通用") -> bool:
    """保存一条用户提示词预设到独立 JSON 文件（同名覆盖）。可存对话 AI 给的提示词复用。"""
    name = str(name).strip(); text = str(text).strip()
    if not name or not text:
        return False
    user = [p for p in _load_user_presets() if p.get("name") != name]  # 同名覆盖
    user.append({"name": name, "text": text, "category": str(category or "通用")})
    return _save_user_presets(user)


def delete_prompt_preset(name) -> bool:
    """删除一条用户提示词预设（内置的删不掉，UI 侧拦截）。"""
    return _save_user_presets([p for p in _load_user_presets() if p.get("name") != str(name)])


def import_prompt_presets(items, replace=False) -> int:
    """导入提示词预设（兼容大香蕉 title/content 字段）。replace=True 覆盖全部用户预设，否则按名合并去重。
    返回成功导入条数。"""
    cur = [] if replace else list(_load_user_presets())
    by_name = {p.get("name"): p for p in cur if isinstance(p, dict)}
    n = 0
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("title") or "").strip()
        text = str(it.get("text") or it.get("content") or "").strip()
        if not name or not text:
            continue
        by_name[name] = {"name": name, "text": text, "category": str(it.get("category") or "通用")}
        n += 1
    _save_user_presets(list(by_name.values()))
    return n


def export_prompt_presets() -> list:
    """导出【用户】提示词预设（不含内置）为可写 JSON 的列表。"""
    return [{"name": p.get("name"), "text": str(p.get("text") or p.get("content") or ""),
             "category": p.get("category", "通用")}
            for p in _load_user_presets() if isinstance(p, dict) and p.get("name")]


def get_style_lib():
    """返回已记住的图生图参考图库目录（外部图包/用户自备目录）；未设或已失效返回 None。"""
    cfg = _load_config()
    d = cfg.get("style_lib_dir")
    if d and Path(str(d)).is_dir():
        return str(d)
    return None


def set_style_lib(path) -> bool:
    """记住参考图库目录到 ~/.sciedit/config.json（非敏感，仅路径）。传空则清除记忆。"""
    cfg = _load_config()
    if path:
        cfg["style_lib_dir"] = str(path)
    else:
        cfg.pop("style_lib_dir", None)
    return _save_config(cfg)


def get_asset_dir():
    """返回已记住的本地素材库根目录（biorender 式分类素材文件夹）；未设或已失效返回 None。"""
    cfg = _load_config()
    d = cfg.get("asset_dir")
    if d and Path(str(d)).is_dir():
        return str(d)
    return None


def set_asset_dir(path) -> bool:
    """记住本地素材库根目录到 ~/.sciedit/config.json（非敏感，仅路径）。传空则清除记忆。"""
    cfg = _load_config()
    if path:
        cfg["asset_dir"] = str(path)
    else:
        cfg.pop("asset_dir", None)
    return _save_config(cfg)


def get_asset_thumb_size(default: int = 140) -> int:
    """素材库缩略图大小（滑块值，持久化，下次打开保持）。非敏感。"""
    cfg = _load_config()
    try:
        v = int(cfg.get("asset_thumb_size", default))
        return max(48, min(320, v))
    except Exception:
        return default


def set_asset_thumb_size(px) -> bool:
    cfg = _load_config()
    cfg["asset_thumb_size"] = int(px)
    return _save_config(cfg)


def get_asset_favorites() -> list:
    """收藏的素材绝对路径列表（仅当前仍存在的文件）。非敏感。"""
    cfg = _load_config()
    out = [str(p) for p in cfg.get("asset_favorites", []) if p and Path(str(p)).is_file()]
    return out


def toggle_asset_favorite(path) -> bool:
    """切换某素材的收藏状态；返回切换后【是否已收藏】。"""
    if not path:
        return False
    cfg = _load_config()
    favs = [str(p) for p in cfg.get("asset_favorites", [])]
    s = str(path)
    if s in favs:
        favs.remove(s); now = False
    else:
        favs.append(s); now = True
    cfg["asset_favorites"] = favs[:500]
    _save_config(cfg)
    return now


def push_asset_recent(path, cap: int = 60) -> bool:
    """把刚用过的素材记入「最近使用」(LRU，最新在前，去重，封顶 cap)。非敏感。"""
    if not path:
        return False
    cfg = _load_config()
    s = str(path)
    rec = [str(p) for p in cfg.get("asset_recent", []) if str(p) != s]
    rec.insert(0, s)
    cfg["asset_recent"] = rec[:cap]
    return _save_config(cfg)


def get_asset_recent() -> list:
    """最近使用的素材绝对路径列表（最新在前，仅当前仍存在的文件）。"""
    cfg = _load_config()
    return [str(p) for p in cfg.get("asset_recent", []) if p and Path(str(p)).is_file()]


def get_pdf_converter():
    """返回用户指定的 PDF→SVG 转换器 exe 路径（pdftocairo/pdf2svg/inkscape/mutool 之一）；未设或失效返回 None。
    非敏感（仅本机工具路径）。未设时 pdf_import 会自动在 PATH 上探测。"""
    cfg = _load_config()
    p = cfg.get("pdf_converter")
    if p and Path(str(p)).is_file():
        return str(p)
    return None


def set_pdf_converter(path) -> bool:
    """记住 PDF→SVG 转换器 exe 路径到 ~/.sciedit/config.json（非敏感）。传空则清除，回退自动探测。"""
    cfg = _load_config()
    if path:
        cfg["pdf_converter"] = str(path)
    else:
        cfg.pop("pdf_converter", None)
    return _save_config(cfg)


def get_show_rulers() -> bool:
    """是否显示标尺（非敏感偏好，默认 True）。"""
    cfg = _load_config()
    return bool(cfg.get("show_rulers", True))


def set_show_rulers(on: bool) -> bool:
    """记住"显示标尺"开关到 ~/.sciedit/config.json（非敏感）。"""
    cfg = _load_config()
    cfg["show_rulers"] = bool(on)
    return _save_config(cfg)


# ---------- 窗口/停靠布局记忆（纯几何，非敏感；与 config.json 分开存 layout.json）----------
def _layout_path() -> Path:
    return Path.home() / ".sciedit" / "layout.json"


def load_layout() -> dict:
    """读 ~/.sciedit/layout.json（窗口几何 + 停靠拓扑 + 浮窗位置，全部 base64 字符串）。
    缺失/损坏返回 {}，由调用方回退到默认布局（fail-loud 由调用方负责）。"""
    try:
        p = _layout_path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def save_layout(d: dict) -> bool:
    """把布局 dict 写到 ~/.sciedit/layout.json。明确不写任何 key/prompt 文本，只存窗口几何/停靠拓扑。"""
    try:
        p = _layout_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def set_connection(pid, base_url=None, api_key=None, model=None, fmt=None) -> dict:
    """保存某生图中转站配置到 gen_conns[pid]（每站独立一套，含接口格式 fmt=grsai|openai），并记为活动站。
    非 None 才写对应字段；api_key 非空才覆盖（空串=不改）。key 只存本机，返回 {ok, has_key, key_hint}，绝不回明文。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    pid = str(pid or "grsai_global")
    b = dict(conns.get(pid) or {})
    if base_url is not None:
        b["base_url"] = str(base_url).strip()
    if model is not None:
        b["model"] = str(model).strip()
    if fmt is not None:
        b["fmt"] = str(fmt).strip() or "grsai"
    if api_key:  # 非空才覆盖；空串=不动原 key
        b["api_key"] = str(api_key).strip()
    conns[pid] = b
    cfg["gen_conns"] = conns
    cfg["gen_active_pid"] = pid
    # 同步活动桶到老单一字段（grsai_base()/其它兜底仍读它）
    cfg["base_url"] = b.get("base_url") or ""
    cfg["model"] = b.get("model") or ""
    ok = _save_config(cfg)
    key = read_key(pid)
    return {"ok": ok, "has_key": bool(key), "key_hint": mask_key(key)}


def remember_gen_conn(pid, base_url, model) -> bool:
    """轻量记住某生图中转站的地址+模型（不动 key/fmt、不改活动站）——选模型/切站时即时记住，不必点保存。
    无变化则不写盘。"""
    cfg = _load_config()
    conns = _gen_conns(cfg)
    pid = str(pid or "grsai_global")
    b = dict(conns.get(pid) or {})
    nb, nm = str(base_url or "").strip(), str(model or "").strip()
    if b.get("base_url", "") == nb and b.get("model", "") == nm:
        return True
    b["base_url"] = nb
    b["model"] = nm
    conns[pid] = b
    cfg["gen_conns"] = conns
    return _save_config(cfg)


# ---------- AI 对话生成提示词配置（OpenAI 兼容 chat，与 grsai 生图 key 完全隔离）----------
def _chat_pid_for_base(bu) -> str:
    """按地址推断商家 id（与 chat_panel._pid_for_base 同口径，仅老存档迁移/兜底用）。"""
    bu = (bu or "").lower()
    if "deepseek" in bu:
        return "deepseek"
    if "bigmodel" in bu:
        return "glm"
    if "xiaomi" in bu or "mimo" in bu:
        return "xiaomi"
    return "custom"


def _chat_conns(cfg: dict) -> dict:
    """取 chat_conns（每商家一套 {base_url, model, api_key}，切回不必重填）。
    首次从老的单一 chat_* 字段迁进对应商家桶（只读返回，不落盘；落盘在 set_chat_conn）。"""
    conns = cfg.get("chat_conns")
    if isinstance(conns, dict):
        return conns
    conns = {}
    if cfg.get("chat_base_url") or cfg.get("chat_api_key") or cfg.get("chat_model"):
        pid = _chat_pid_for_base(cfg.get("chat_base_url"))
        conns[pid] = {
            "base_url": str(cfg.get("chat_base_url") or ""),
            "model": str(cfg.get("chat_model") or ""),
            "api_key": str(cfg.get("chat_api_key") or ""),
        }
    return conns


def get_chat_active_pid() -> str:
    """上次使用的对话商家 id（重开软件恢复到它）。无存档→按老地址推断→默认 deepseek。"""
    cfg = _load_config()
    return str(cfg.get("chat_active_pid") or _chat_pid_for_base(cfg.get("chat_base_url")) or "deepseek")


def read_chat_key(pid=None):
    """某商家的对话 Key：优先该商家桶里存的；无则回退该商家对应的 .env 变量（不串用别家 key）。
    pid=None→当前活动商家。各商家 Key 互相独立。与 grsai 的 api_key 完全隔离。仅本进程用，绝不回前端/日志。"""
    cfg = _load_config()
    conns = _chat_conns(cfg)
    if pid is None:
        pid = cfg.get("chat_active_pid") or _chat_pid_for_base(cfg.get("chat_base_url"))
    pid = str(pid or "custom")
    bucket = conns.get(pid) or {}
    if bucket.get("api_key"):
        return str(bucket["api_key"]).strip()
    env_names = {"deepseek": ("DEEPSEEK_API_KEY",), "glm": ("GLM_API_KEY",)}.get(pid, ("OPENAI_API_KEY",))
    for name in env_names:
        v = _env_val(name)
        if v:
            return v
    return None


def chat_base() -> str:
    cfg = _load_config()
    b = cfg.get("chat_base_url") or _env_val("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    return str(b).rstrip("/")


def get_chat_conn(pid=None) -> dict:
    """某商家面板回显：地址/模型/是否已设 key（绝不回明文，只回掩码尾号）。pid=None→当前活动商家。
    saved=该商家是否已有存档（地址/模型/自有 key 任一非空）——区别于仅靠 .env 推出的 has_key。"""
    cfg = _load_config()
    conns = _chat_conns(cfg)
    if pid is None:
        pid = cfg.get("chat_active_pid") or _chat_pid_for_base(cfg.get("chat_base_url"))
    pid = str(pid or "custom")
    bucket = conns.get(pid) or {}
    key = read_chat_key(pid)
    return {
        "base_url": bucket.get("base_url") or "",
        "model": bucket.get("model") or "",
        "has_key": bool(key),
        "key_hint": mask_key(key),
        "saved": bool(bucket.get("base_url") or bucket.get("model") or bucket.get("api_key")),
        "pid": pid,
    }


def set_chat_conn(pid, base_url=None, api_key=None, model=None) -> dict:
    """保存某商家的对话配置到 ~/.sciedit/config.json 的 chat_conns[pid]（每商家独立一套，切回不必重填），
    并记为活动商家。非 None 才写对应字段；api_key 非空才覆盖（空串=不改）。返回 {ok, has_key, key_hint}，绝不回明文。"""
    cfg = _load_config()
    conns = _chat_conns(cfg)
    pid = str(pid or "custom")
    b = dict(conns.get(pid) or {})
    if base_url is not None:
        b["base_url"] = str(base_url).strip()
    if model is not None:
        b["model"] = str(model).strip()
    if api_key:  # 非空才覆盖；空串=不动原 key
        b["api_key"] = str(api_key).strip()
    conns[pid] = b
    cfg["chat_conns"] = conns
    cfg["chat_active_pid"] = pid
    # 同步活动桶到老单一字段（chat_base() 兜底默认仍读它）
    cfg["chat_base_url"] = b.get("base_url") or ""
    cfg["chat_model"] = b.get("model") or ""
    ok = _save_config(cfg)
    key = read_chat_key(pid)
    return {"ok": ok, "has_key": bool(key), "key_hint": mask_key(key)}


def remember_chat_conn(pid, base_url, model) -> bool:
    """轻量记住某对话商家的地址+模型（不动 key、不改活动站）——选模型/切商家时即时记住，不必点保存。
    无变化则不写盘。"""
    cfg = _load_config()
    conns = _chat_conns(cfg)
    pid = str(pid or "custom")
    b = dict(conns.get(pid) or {})
    nb, nm = str(base_url or "").strip(), str(model or "").strip()
    if b.get("base_url", "") == nb and b.get("model", "") == nm:
        return True
    b["base_url"] = nb
    b["model"] = nm
    conns[pid] = b
    cfg["chat_conns"] = conns
    return _save_config(cfg)


def get_chat_models(provider_id=None) -> list:
    """读某 provider 记住的模型名列表（拉取到的 + 用户手填自定义）。
    存结构 cfg['chat_models'] = {provider_id: [names...]}。非敏感（仅模型名）。
    provider_id 缺省→合并全部桶去重（兜底）。老 config 无此键→[]，不崩。"""
    cfg = _load_config()
    d = cfg.get("chat_models") or {}
    if not isinstance(d, dict):
        return []
    if provider_id:
        v = d.get(str(provider_id)) or []
        return [str(x) for x in v if x] if isinstance(v, list) else []
    out = []
    for v in d.values():
        if isinstance(v, list):
            out += [str(x) for x in v if x]
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def set_chat_models(provider_id, models) -> bool:
    """记住某 provider 的模型名列表（去重保序，限 80 条防膨胀）。
    models 空列表=清空该桶。非敏感，同 config.json 复用 _save_config 的 0o600。"""
    cfg = _load_config()
    d = cfg.get("chat_models")
    if not isinstance(d, dict):
        d = {}
    pid = str(provider_id or "custom")
    seen, uniq = set(), []
    for m in (models or []):
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    d[pid] = uniq[:80]
    cfg["chat_models"] = d
    return _save_config(cfg)


# ---------- AI 分割/抠图后端配置（与生图端点不同，独立字段）----------
def seg_base() -> str:
    """分割后端地址；**不回退到 grsai_base——抠图端点与生图端点不同**。未设回 ""。"""
    cfg = _load_config()
    return str(cfg.get("seg_base_url") or "").rstrip("/")


def read_seg_key():
    """分割 Key：优先面板存的 seg_api_key；回退 .env 的 SEG_API_KEY。仅本进程用，绝不回前端/日志。"""
    cfg = _load_config()
    if cfg.get("seg_api_key"):
        return str(cfg["seg_api_key"]).strip()
    return _env_val("SEG_API_KEY")


def get_seg_conn() -> dict:
    """面板回显：地址/后端/模型/端点/是否已设 key（绝不回传明文，只回掩码尾号）。"""
    cfg = _load_config()
    key = read_seg_key()
    return {
        "base_url": cfg.get("seg_base_url") or "",
        "node": cfg.get("seg_node") or "",
        "provider": cfg.get("seg_provider") or "grsai",  # 未配置→grsai（用户已有 grsai key，开箱即用）
        "model": cfg.get("seg_model") or "",
        "endpoint": cfg.get("seg_endpoint") or "",
        "result_endpoint": cfg.get("seg_result_endpoint") or "",
        "prompt": cfg.get("seg_prompt") or "",
        "has_key": bool(key),
        "key_hint": mask_key(key),
    }


def set_seg_conn(base_url=None, api_key=None, model=None, provider=None, endpoint=None,
                 result_endpoint=None, prompt=None, node=None) -> dict:
    """保存分割后端配置到 ~/.sciedit/config.json（与 api_key 同文件不同字段）。
    非 None 才写对应字段；api_key 非空才覆盖（空串=不改）。返回 {ok, has_key, key_hint}，绝不回明文。
    result_endpoint：ppio 异步取结果端点；prompt：编辑指令。"""
    cfg = _load_config()
    if base_url is not None:
        cfg["seg_base_url"] = str(base_url).strip()
    if node is not None:
        cfg["seg_node"] = str(node).strip()  # grsai 国内/国外 节点选择，非密钥
    if model is not None:
        cfg["seg_model"] = str(model).strip()
    if provider is not None:
        cfg["seg_provider"] = str(provider).strip()
    if endpoint is not None:
        cfg["seg_endpoint"] = str(endpoint).strip()
    if result_endpoint is not None:
        cfg["seg_result_endpoint"] = str(result_endpoint).strip()
    if prompt is not None:
        cfg["seg_prompt"] = str(prompt).strip()
    if api_key:  # 非空才覆盖；空串=不动原 key
        cfg["seg_api_key"] = str(api_key).strip()
    ok = _save_config(cfg)
    key = read_seg_key()
    return {"ok": ok, "has_key": bool(key), "key_hint": mask_key(key)}
