# 矢量编辑子系统 — MVP Scope（2026-06-04 定稿）

> 像 Adobe Illustrator 编辑 SVG/PDF 矢量图：导入→每元素独立可选/改色/改字/改形→打组/对齐→导出。
> 基于 workflow w4t3lotun 实证调研（带来源）。用户已定 scope 杠杆（见 §决策）。

## 0. 两个硬前提（必须贯穿 MVP）
1. **对象是「代码出的矢量图」（matplotlib/R/Illustrator 的 SVG/PDF），不是 AI 生成的栅格 PNG**（PNG 无矢量结构，拆不开）。
2. **文字可编辑性受源文件约束**：matplotlib SVG 默认 `svg.fonttype='path'`（文字=路径，**只能改色/移动、不能改字**）；PDF 默认 `pdf.fonttype=3`（不可编辑）。
   - MVP 必须：**检测**元素是 `<text>` 还是 `<path>`，路径化文字明确标灰提示「不可改字」；并**教用户重新导出**（`svg.fonttype='none'` / `pdf.fonttype=42`）。这是 fail-loud，不是 bug。

## 1. 用户已定 scope（2026-06-04）
- **格式范围**：SVG **+ PDF** 都要。
- **配色助手**：纳入 MVP（一键换色盲友好色板 Okabe-Ito 等）。
- **钢笔/锚点编辑**：纳入 MVP（Illustrator 式贝塞尔手柄微调曲线）。

## 2. MVP 功能清单（必做）
| # | 功能 | 关键实现 | 备注 |
|---|------|---------|------|
| 1 | **SVG 导入** → 拆成独立可编辑元素 | `lxml` 解析 XML + `svgelements`(d→QPainterPath)；`<path>`→`QGraphicsPathItem`、`<text>`→`QGraphicsTextItem`、`<g>`→`QGraphicsItemGroup`(递归保 z-order/transform) | 不支持的 filter/pattern/mask 子树→只读 `QGraphicsSvgItem` 渲染回退 |
| 2 | **PDF 导入** | 见 §决策（授权未定）；matplotlib/R PDF 优先走 PDF→SVG 复用 §1 管线 | 文字可编辑性「看缘分」，检测+告警 |
| 3 | **选中/移动/缩放/旋转**（单/多元素） | QGraphicsItem 通用，复用现有选择/变换 | — |
| 4 | **改色**（fill/stroke）+ **配色助手** | QColorDialog + 属性面板；一键替换为 Okabe-Ito 等色盲友好色板 | 期刊刚需 |
| 5 | **文字编辑**（改字/字体/字号/色） | `QGraphicsTextItem.setTextInteractionFlags(TextEditorInteraction)` 双击内联编辑 | 受 §0.2 约束 |
| 6 | **打组/解组** | `QGraphicsItemGroup` | — |
| 7 | **对齐/分布** | **复用现有 `_align`**（已为栅格层做好，对 QGraphicsItem 通用） | 白捡 |
| 8 | **钢笔/锚点编辑** | 自写 `QGraphicsPathItem` 子类 + 锚点/手柄小 ellipse item(可拖)，拖动重建 QPainterPath | 最复杂(~300-400 LOC)，放最后做 |
| 9 | **撤销/重做** | 复用现有撤销（或 QUndoStack） | — |
| 10 | **导出 SVG/PDF** | **从元素模型自序列化回 SVG**(lxml，保 `<text>`/`<g>`)；PDF 用 `QPdfWriter`+`QPainter` 矢量输出 | **绝不用 QSvgGenerator 重绘整场景**(丢分组+丢可编辑文字) |
| 11 | **与栅格编辑器共存** | `VectorLayer`(QGraphicsScene 子树) vs `RasterLayer`(QGraphicsPixmapItem)，同 scene，选择/对齐/撤销通用；导出含位图层→`<image>` base64 嵌入 | 无架构冲突(调研确证) |

## 3. 不在 MVP（明确排除）
- SVG 滤镜/渐变网格/混合模式/蒙版的**可编辑**还原（只读渲染或丢弃）。
- 任意来源旧 PDF 的可靠文字编辑（缺嵌字/ToUnicode 则乱码，检测+告警即可）。
- 生成式 AI 配色（Firefly 那种）、Image Trace 矢量化、笔刷/3D 特效、完美往返保真（layer 名/transform 元数据无损需自定义容器格式）。
- Pathfinder 布尔运算（放进阶；如要可用 QPainterPath.united/subtracted 或 Shapely）。

## 4. 技术栈
- 解析：`lxml`(C 后端) + `svgelements`(meerk40t，MIT) 或 `svgpathtools`。
- 渲染/编辑：`QGraphicsPathItem`/`QGraphicsTextItem`/`QGraphicsItemGroup`/`QGraphicsSvgItem`(回退)。
- 导出：`lxml` 序列化 SVG；`QPdfWriter`/`QPrinter`+`QPainter` 出 PDF。
- 撤销：复用现有 `_push_history`/历史面板，或新 `QUndoStack`（待实现时定）。
- 颜色：`QColorDialog` + Okabe-Ito 色板常量。

## 5. ✅ 已决：PDF 导入走「外部 pdf2svg/inkscape」（2026-06-04 用户定）
独立进程调用外部工具把 PDF→SVG，再复用 §1 SVG 管线。**授权不传染 app、可闭源分发给同门/对外**。代价：用户机需装 pdf2svg 或 inkscape（可随软件带/文档说明）；matplotlib/R PDF 转换保真好。实现：B4 批；找不到外部工具时 fail-loud 提示安装。**不用 PyMuPDF（AGPL）**。

### （存档）当时的库选型对比
| 方案 | 授权 | 优劣 |
|------|------|------|
| **PyMuPDF(fitz)** | **AGPL-3.0**（传染性！分发/SaaS 须整 app 开源，或买商业授权） | 最省事(`get_svg_image()` 直接转 SVG/`get_drawings()` 取矢量) |
| **pikepdf** | MPL-2.0（宽松，可闭源分发） | 内容流级，较底层、工作量大 |
| **外部 pdf2svg / inkscape CLI** | GPL(工具)但**独立进程调用不传染** app | PDF→SVG 复用 §1 管线最干净，但需用户机装该工具 |
| **pypdf / pdfminer.six** | 宽松 | 主要取文字，矢量绘制提取弱 |

→ 若 app 要闭源给同门/对外：**别用 PyMuPDF**，建议 **外部 pdf2svg/inkscape**（独立进程）或 pikepdf。若 app 不分发/内部用：PyMuPDF 最省事。

## 6. 落地分批（实现顺序，每批 Workflow 设计→实现→审核）
1. **B1 SVG 核心**：导入解析→可编辑 path/text/group + 选择/移动 + 导出 SVG（往返不塌）。← 先验证地基。
2. **B2 改色+配色助手+文字编辑**。
3. **B3 打组/对齐复用 + 与栅格层共存**。
4. **B4 PDF 导入**（授权定了再做）。
5. **B5 钢笔/锚点编辑**（最难，单独一批）。

## 7. MVP 验收检查点（可验证）
1. 导入 `svg.fonttype='none'` 的 matplotlib SVG → 每个 `<text>` 可选中、双击改字 → 导出后 SVG 文本已变。
2. 选中一条曲线改 stroke 色 → 导出 `<path>` 的 stroke 已变。
3. 框选多元素→Group→整体拖→Ungroup→导出 `<g>` 出现/消失。
4. 选 3 元素→左对齐/水平等距→坐标按规则更新。
5. import→edit→export→再 import，元素仍独立可选（不塌成一张图）。
6. 一键 Okabe-Ito → 选中元素配色替换为色盲友好色。
7. 钢笔：拖某锚点手柄 → 曲线形状改变 → 导出 `<path>` 的 `d` 已变。
