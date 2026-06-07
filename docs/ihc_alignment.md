# IHC 定量 —— 对齐 Fiji 的精确常数（ground truth）

> 来源：反编译本机 Fiji.app 的 `plugins/Colour_Deconvolution-3.0.3.jar`
> （CFR 0.152，与 GitHub `fiji/Colour_Deconvolution` 源码一致）+ `dbrant/ihc-profiler` 的
> `IHC_Profiler.txt` 宏。任何数值改动必须回到此处核对，禁止凭印象改。

## 1. OD 变换（RGB 0–255 → 光密度）

`StainMatrix.java` L107, L204-206：
```
log255 = ln(255.0)
od(v) = -255.0 * ln((v + 1.0) / 255.0) / log255      # 自然对数；分子 +1；除数 255（不是 256）；再 /ln255 归一
```
NumPy：`od = -255.0 * np.log((v.astype(float) + 1.0) / 255.0) / np.log(255.0)`

## 2. 内置染色向量表（逐位，16 条；R,G,B × 3 stain；第三 stain (0,0,0)=占位待算残差）

```
H&E              0.644211,0.716556,0.266844 | 0.092789,0.954111,0.283111 | 0,0,0
H&E 2            0.490157,0.768971,0.410402 | 0.046153,0.842068,0.537393 | 0,0,0
H DAB            0.650000,0.704000,0.286000 | 0.268000,0.570000,0.776000 | 0,0,0
Feulgen LightGrn 0.464209,0.830083,0.308272 | 0.947055,0.253738,0.196508 | 0,0,0
Giemsa           0.834750,0.513556,0.196330 | 0.092789,0.954111,0.283111 | 0,0,0
FastRed FastBlue DAB 0.213939,0.851127,0.477940 | 0.748903,0.606242,0.267311 | 0.268,0.570,0.776
Methyl Green DAB 0.980000,0.144316,0.133146 | 0.268000,0.570000,0.776000 | 0,0,0
H&E DAB          0.650000,0.704000,0.286000 | 0.072000,0.990000,0.105000 | 0.268,0.570,0.776
H AEC            0.650000,0.704000,0.286000 | 0.274300,0.679600,0.680300 | 0,0,0
Azan-Mallory     0.853033,0.508733,0.112656 | 0.092899,0.866201,0.490985 | 0.107328,0.367654,0.923748
Masson Trichrome 0.799511,0.591352,0.105287 | 0.099972,0.737386,0.668033 | 0,0,0
Alcian blue & H  0.874622,0.457711,0.158256 | 0.552556,0.754400,0.353744 | 0,0,0
H PAS            0.644211,0.716556,0.266844 | 0.175411,0.972178,0.154589 | 0,0,0
Brilliant_Blue   0.314655,0.660240,0.681965 | 0.383573,0.527114,0.758302 | 0.743354,0.517314,0.424040
RGB              0,1,1 | 1,0,1 | 1,1,0
CMY              1,0,0 | 0,1,0 | 0,0,1
```
首期实现至少内置：**H DAB**（IHC 主力）、**H&E**、**H&E DAB**、**H AEC**。

## 3. 归一化 + 第三向量残差（StainMatrix.java L110-162）

- 每条向量 L2 归一到单位长（`cosx/y/z = MOD / len`，len=0 跳过保持 0）。
- 若 stain2 全 0：`cos[1] = (cosz[0], cosx[0], cosy[0])`（通道轮转）。
- 若 stain3 全 0：逐通道取正交补 `cos[2]_c = sqrt(1 - cos[0]_c^2 - cos[1]_c^2)`（若 >1 则置 0），再整体 L2 归一。
- 任一余弦分量恰为 0 → 置 0.001 防除零。

## 4. 3×3 闭式逆 → q[0..8]（行主序，L169-180）

```
A = cosy1 - cosx1*cosy0/cosx0
V = cosz1 - cosx1*cosz0/cosx0
C = cosz2 - cosy2*V/A + cosx2*(V/A*cosy0/cosx0 - cosz0/cosx0)
q2 = (-cosx2/cosx0 - cosx2/A*cosx1/cosx0*cosy0/cosx0 + cosy2/A*cosx1/cosx0) / C
q1 = -q2*V/A - cosx1/(cosx0*A)
q0 = 1/cosx0 - q1*cosy0/cosx0 - q2*cosz0/cosx0
q5 = (-cosy2/A + cosx2/A*cosy0/cosx0) / C
q4 = -q5*V/A + 1/A
q3 = -q4*cosy0/cosx0 - q5*cosz0/cosx0
q8 = 1/C
q7 = -q8*V/A
q6 = -q7*cosy0/cosx0 - q8*cosz0/cosx0
```

## 5. 逐像素解卷积 + 8-bit 回变换（L207-216）

```
scaled_i = od_R*q[i*3] + od_G*q[i*3+1] + od_B*q[i*3+2]      # stain i 的浓度（矩阵·OD）
output   = exp(-(scaled_i - 255.0) * log255 / 255.0)        # = 255 * 255^(-(c-255)/255)
output   = min(output, 255); pixel = floor(output + 0.5)    # 8-bit 灰度，亮=弱染色（与 ImageJ 一致）
```
注意：输出图是「该 stain 的强度」，**值越小=染色越强**（OD 高→output 低）。做阈值/直方图分区时按此方向。

## 6. IHC Profiler 分区（作用在 DAB 解卷积后的 8-bit 灰度，不是原 RGB；macro L60-114）

```
Region4 High Positive : 0–60
Region3 Positive      : 61–120
Region2 Low Positive  : 121–180
Region1 Negative      : 181–235
Region0 排除(近白背景): 236–255         # 不计入分母
Px = Regionx / (Total − Region0) * 100
Score = P4/100*4 + P3/100*3 + P2/100*2 + P1/100*1     # 0–4 连续分（非 0–300 H-score）
标签：≥2.95 High Positive / 1.95–2.94 Positive / 0.95–1.94 Low Positive / 0–0.94 Negative
覆盖规则：任一 Px > 66 → 直接判该区标签（绕过加权分）
```
**关键**：IHC Profiler 给的是**分类标签 + 0–4 连续分**，没有 0–300 H-score 映射。
我们若额外提供经典 H-score(0–300) 需另立口径并明确标注，不能冒充 IHC Profiler。

## 验证结果（2026-06-07）—— 逐位一致 ✅

`src/ihc_quant.py` 的 `colour_deconvolution(rgb, "H DAB")` vs **Fiji Colour Deconvolution [H DAB]**：
- 测试图 256×256 全色域梯度 + 一块已知 H+DAB 浓度正向造的 IHC 棕染区。
- 三通道（Hematoxylin / DAB / Residual）**max|Δ|=0，0/65536 像素不一致**，逐位相同。
- 复现：`_ihc_calib/`（cd.ijm + test_rgb.png + my/fiji_colour_1-3.png；scratch，不提交）。

**坑记录（双坑）**：
1. Colour Deconvolution 的 `run()` 在 line 122 强制 `checkHeadless` → **不能 `--headless`**；改 `ImageJ-win64.exe --console -macro`（用桌面 display，`setBatchMode(true)` 隐窗，末尾 `run("Quit")` 自退；退出时一个 NPE 不影响产物）。
2. Fiji 的 Colour_1/2/3 saveAs PNG = **mode='P' 调色板索引图**（索引=stain 8-bit 强度，调色板=染色色 LUT 仅供显示）。比对必须读**原始索引**（`np.asarray(Image.open(...))` 不要 `.convert('L')`，否则经调色板转亮度全错）。

**Fiji headless 比对 macro**（`_ihc_calib/cd.ijm`，复用模板）：
```
setBatchMode(true); open(dir+"test_rgb.png"); title=getTitle();
run("Colour Deconvolution", "vectors=[H DAB] hide");
titles=getList("image.titles");  // 存 indexOf(t,"Colour_1/2/3")>=0 的窗口为 PNG
run("Quit");
命令行: ImageJ-win64.exe --console -macro cd.ijm
```

## 复用判断（agent B）
- skimage `rgb_from_hdx` 的 H-DAB 向量 = Fiji 逐位同（双源印证 0.650,0.704,0.286 / 0.268,0.570,0.776）。
- 但 skimage **OD 常数/I0/残差法与 Fiji 不同**（skimage: rgb∈(0,1] clamp 1e-6、natural log/log(1e-6)、3rd=cross；Fiji: 0-255、+1、/255、/ln255、3rd=逐通道正交补）→ 我们**走 Fiji 口径**才能逐位对齐 Fiji。
- 实现纯 NumPy 自抄（BSD-3 skimage + MIT IHC Profiler 可抄），不依赖被打包排除的 skimage。引用 Ruifrok 2001 + skimage colorconv + Varghese 2014。

## 还需验证
- `ihc_profiler` 分区评分：当前为**逐行转写 dbrant/ihc-profiler 宏**（高置信），未对运行中的 IHC Profiler 插件 bit-verify（该插件本机未装）。已对手算直方图单测意图。装插件后可补 bit 校准。

## 纤维化/胶原定量扩展（P1.2，2026-06-07）

DAB-IHC 之外，扩成通用组织化学/纤维化定量（用户真实场景 = 肾纤维化 Masson/天狼星红/HE）。

**算法（ihc_quant.py 新增）**：
- `tissue_mask(rgb, white_level=0.90)`：max(R,G,B)/255 < white_level 视为组织（排除玻片近白背景）。
- `otsu_threshold(vals8, mask)`：类间方差最大化。**边缘**：等高双峰 sigma_b 平台→argmax 取最左缘（仍正确分离）；**单值通道**（均匀强染色小 ROI 经 8-bit 量化塌成一个值）sigma_b 恒 0→曾返回 0 致 100% 染色误报 0% 阳性（对抗审查 MEDIUM）→已修：唯一占用 bin 时直接返回该值（pos=ch≤value 整片选中）。
- `channel_positive_area(ch8, tissue, thr, conc)`：目标通道阳性面积（暗=强 → ch≤thr），thr='otsu'(组织内自动)或 int；mean_od 取阳性像素 conc 均值。
- `sirius_red_area(rgb, sat_min=0.25, ...)`：天狼星红**无解卷积向量** → HSV 红 hue(≥345 或 ≤25)+饱和度阈值（亮场标准口径）。

**面板（ihc_analyzer.py）**：染色下拉加「天狼星红(红色面积)」→ red 模式；deconv 模式有「测量通道」(Masson 默认 Aniline blue 胶原)、阈值(Otsu自动/手动)、排除近白背景。DAB 目标额外给 IHC Profiler 分级。

**真实数据验证（本地肾纤维化样本，200x）**：
| 染色 | 方法 | 结果 |
|---|---|---|
| HE | H&E 解卷积 | 正确分离核/胞质 |
| Masson | Aniline blue 通道 + Otsu+组织内 | 胶原 67.6%（固定180=87.9% 过松，Otsu 更可信，阈值 120） |
| 天狼星红 | HSV 红色胶原% | **纤维化模型 9.29% vs sham 对照 0.30%（31×）** |

天狼星红模型/对照 31 倍差异 = 金标准生物学验证（方法真的抓到纤维化）。复现脚本/通道图在 `_ihc_calib/`（不入库）。
