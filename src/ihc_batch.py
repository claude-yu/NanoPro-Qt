"""IHC / 组织化学定量 —— 批量引擎（纯算法，可 headless 单测）。

对一批图用统一设置（染色/目标通道/阈值/背景排除）跑阳性面积%；天狼星红走红色面积法。
按文件名启发式分组（含 sham→对照，否则模型），供模型 vs 对照比较。结果导出长表 CSV。
与单图面板 ihc_analyzer 同一算法口径（ihc_quant），数值一致。
"""
from __future__ import annotations

import csv
import os
import numpy as np

import ihc_quant as iq

SIRIUS_RED_KEY = "天狼星红 (红色面积)"


def load_rgb(path: str) -> np.ndarray:
    from PIL import Image
    return np.ascontiguousarray(np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8))


def group_of(name: str) -> str:
    """文件名启发式分组（仅便利，用户可在 Excel 改）。"""
    n = name.lower()
    if "sham" in n or "对照" in n or "control" in n:
        return "对照"
    return "模型"


def _compute(rgb: np.ndarray, s: dict):
    """核心：按设置 s 算 (pos_mask, tissue, target_name, thr_label, conc_target|None, ch8_target|None, is_dab)。

    s: {mode:'deconv'|'red', stain, target(可空), thr_mode:'otsu'|'manual', manual_lo, manual_hi, sat_min, exclude_bg}
    与单图面板 ihc_analyzer._build_pos_mask/_metrics_for_rect 同一口径。
    """
    if s.get("mode") == "red":
        # 天狼星红用 sirius 内部 CIELAB 组织掩膜（与单图一致，不受 exclude_bg 影响）；无解卷积通道
        r = iq.sirius_red_area(rgb, sensitivity=s.get("sat_min", 50))
        return r["red_mask"], r["tissue_mask"], "红色胶原", "灵敏度%d" % s.get("sat_min", 50), None, None, False
    tissue = iq.tissue_mask(rgb) if s.get("exclude_bg", True) else np.ones(rgb.shape[:2], bool)
    stain = s.get("stain", "H DAB")
    conc, img8 = iq.colour_deconvolution(rgb, stain)
    names = iq.STAIN_CHANNEL_NAMES.get(stain, ["Stain1", "Stain2", "Residual"])
    target = s.get("target")
    if target not in names:
        target = names[names.index("DAB")] if "DAB" in names else names[0]
    idx = names.index(target)
    ch = img8[idx]
    if s.get("thr_mode", "otsu") == "otsu":
        hi = iq.otsu_threshold(ch[tissue]); lo = 0
        thr_label = "Otsu≤%d" % hi
    else:
        hi = int(s.get("manual_hi", 180)); lo = int(s.get("manual_lo", 0))
        thr_label = "[%d,%d]" % (lo, hi)
    pos = (ch >= lo) & (ch <= hi) & tissue
    return pos, tissue, target, thr_label, conc[idx], ch, (target == "DAB")


def positive_mask(rgb: np.ndarray, s: dict):
    """兼容旧接口（对话框预览叠加用）：(pos_mask, tissue, area_pct, thr_label, target_name, tissue_px)。"""
    pos, tissue, target, thr_label, _cc, _ch, _dab = _compute(rgb, s)
    denom = int(tissue.sum())
    return pos, tissue, (int(pos.sum()) / denom * 100.0) if denom else 0.0, thr_label, target, denom


def full_metrics(rgb: np.ndarray, s: dict) -> dict:
    """全套指标（与单图面板逐口径一致）：area/平均OD/IOD/H-score/IHC分/分级/阳性px/组织px。"""
    return metrics_from(*_compute(rgb, s))


def metrics_from(pos, tissue, target, thr_label, cc, ch8, is_dab) -> dict:
    """从 _compute 的输出算指标（不再解卷积；单图覆盖滑块复用，省一次解卷积）。"""
    sel = pos & tissue
    denom = int(tissue.sum()); pos_px = int(sel.sum())
    area = (pos_px / denom * 100.0) if denom else 0.0
    mean_od = float(cc[sel].mean()) if (cc is not None and pos_px) else None
    iod = float(cc[sel].sum()) if (cc is not None and pos_px) else None
    score = tier = label = h = None
    if is_dab:
        prof = iq.ihc_profiler(ch8, tissue)
        score, tier, label = prof["score"], prof["tier"], prof["label"]
        h = iq.h_score(ch8, mask=tissue)
    return {"target": target, "area_pct": area, "thr_label": thr_label, "mean_od": mean_od, "iod": iod,
            "h_score": h, "score": score, "tier": tier, "label": label or "—",
            "pos_px": pos_px, "tissue_px": denom}


def analyze_path(path: str, s: dict) -> dict:
    name = os.path.basename(path)
    try:
        m = full_metrics(load_rgb(path), s)
        return {"path": path, "name": name, "group": group_of(name), "ok": True, "error": "", **m}
    except Exception as ex:   # 大声失败：单图坏不拖垮整批，但标明失败
        return {"path": path, "name": name, "group": group_of(name), "target": "", "area_pct": float("nan"),
                "thr_label": "", "mean_od": None, "iod": None, "h_score": None, "score": None, "tier": None,
                "label": "—", "pos_px": 0, "tissue_px": 0, "ok": False, "error": str(ex)}


def batch_analyze(paths, s: dict, progress=None) -> list:
    out = []
    n = len(paths)
    for i, p in enumerate(paths):
        out.append(analyze_path(p, s))
        if progress:
            progress(i + 1, n)
    return out


def group_summary(results: list) -> dict:
    """按组聚合阳性面积%（均值/标准差/n），供模型 vs 对照速览。"""
    agg = {}
    for r in results:
        if not r["ok"]:
            continue
        agg.setdefault(r["group"], []).append(r["area_pct"])
    return {g: {"n": len(v), "mean": float(np.mean(v)), "std": float(np.std(v, ddof=1)) if len(v) > 1 else 0.0}
            for g, v in agg.items()}


def export_csv(results: list, path: str, s: dict):
    stain_label = s.get("stain_label") or (SIRIUS_RED_KEY if s.get("mode") == "red" else s.get("stain", ""))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["文件", "组", "染色", "测量通道", "阳性面积%", "阈值", "平均OD", "IOD", "H-score(0-300)",
                    "IHC分(0-4)", "分级", "阳性像素", "组织像素", "状态"])
        for r in results:
            w.writerow([r["name"], r["group"], stain_label, r.get("target", ""),
                        "" if not r["ok"] else "%.4f" % r["area_pct"], r.get("thr_label", ""),
                        "" if r.get("mean_od") is None else "%.4f" % r["mean_od"],
                        "" if r.get("iod") is None else "%.4f" % r["iod"],
                        "" if r.get("h_score") is None else "%.2f" % r["h_score"],
                        "" if r.get("score") is None else "%.4f" % r["score"], r.get("label", "—"),
                        r.get("pos_px", 0), r.get("tissue_px", 0),
                        "OK" if r["ok"] else "失败:" + r["error"]])


# ── CLI（headless 批量）。
def _main(argv=None):
    import argparse, glob, json
    ap = argparse.ArgumentParser(description="IHC 批量定量（阳性面积%）")
    ap.add_argument("inputs", nargs="+", help="图片路径或通配")
    ap.add_argument("--stain", default="H DAB")
    ap.add_argument("--red", action="store_true", help="天狼星红红色面积法")
    ap.add_argument("--target", default=None)
    ap.add_argument("--manual", nargs=2, type=int, metavar=("LO", "HI"), help="手动区间，缺省=Otsu")
    ap.add_argument("--no-bg-exclude", action="store_true")
    ap.add_argument("-o", "--out", default=None, help="导出 CSV 路径")
    a = ap.parse_args(argv)
    paths = []
    for x in a.inputs:
        paths.extend(sorted(glob.glob(x)) if any(c in x for c in "*?[") else [x])
    s = {"mode": "red" if a.red else "deconv", "stain": a.stain, "target": a.target,
         "thr_mode": "manual" if a.manual else "otsu",
         "manual_lo": a.manual[0] if a.manual else 0, "manual_hi": a.manual[1] if a.manual else 180,
         "exclude_bg": not a.no_bg_exclude}
    res = batch_analyze(paths, s)
    if a.out:
        export_csv(res, a.out, s)
    print(json.dumps({"n": len(res), "group_summary": group_summary(res),
                      "rows": [{k: r[k] for k in ("name", "group", "area_pct", "thr_label", "ok")} for r in res]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
