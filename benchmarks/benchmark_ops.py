"""OpenCV 抠图算法耗时基准（headless，无需 Qt 显示）。
对照：现 WebView 版同类算法在 JS/Worker 里跑。运行：python benchmarks/benchmark_ops.py
"""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from image_ops import magic_wand_mask, feather_mask, extract_by_mask  # noqa: E402


def make_image(w: int, h: int) -> np.ndarray:
    rgba = np.empty((h, w, 4), np.uint8)
    rgba[:, :, :3] = 240            # 浅灰背景（科研图常见白底）
    rgba[:, :, 3] = 255
    cv2.rectangle(rgba, (w // 4, h // 4), (3 * w // 4, 3 * h // 4), (60, 120, 200), -1)  # 前景块
    return rgba


def timeit(fn, *a):
    t = time.perf_counter()
    r = fn(*a)
    return r, (time.perf_counter() - t) * 1000.0


def bench(w: int, h: int, label: str):
    rgba = make_image(w, h)
    mask, t_wand = timeit(magic_wand_mask, rgba, w // 8, h // 8, 30)   # 点背景→选中背景
    fmask, t_feather = timeit(feather_mask, mask, 3)
    out, t_extract = timeit(extract_by_mask, rgba, fmask)
    t = time.perf_counter()
    ok, buf = cv2.imencode(".png", cv2.cvtColor(out, cv2.COLOR_RGBA2BGRA))
    t_png = (time.perf_counter() - t) * 1000.0
    sel = int((mask > 0).sum())
    print(f"{label:>3} {w}x{h}: 魔棒 floodFill={t_wand:6.1f}ms  羽化={t_feather:6.1f}ms  "
          f"提取alpha={t_extract:6.1f}ms  PNG编码={t_png:6.1f}ms  (选中 {sel} px)")


if __name__ == "__main__":
    print("OpenCV", cv2.__version__, "· numpy", np.__version__)
    for w, h, lab in [(1024, 1024, "1K"), (2048, 2048, "2K"), (4096, 4096, "4K")]:
        bench(w, h, lab)
