# Debug benchmark

## 联网真实界面大矩阵

`fetch_web_ui_benchmark.py` 从公开的 UI grounding 预览集下载 10 张真实电脑/网页界面及对应点击题目。所有图片、题目和运行结果统一放在可直接清理的 `cache/web-ui-matrix/`：一题一个目录，每种模型和模式一个明确命名的子目录。

```bash
python3 debug/fetch_web_ui_benchmark.py
.venv/bin/python debug/run_web_ui_matrix.py
```

矩阵包含：

- 单轮正方形网格：`n=8..30`；
- 固定二级变焦：`4x4`、`5x5`、`6x6`，每级截取选中格附近 `2x2` 单元范围再放大；
- 固定三级变焦：同上，再重复一次；
- 模型：DeepSeek 配置、`glm-4.6v`、`MiniMax-M3`；
- 每个成功预测用 OpenCV 在干净原图上绘制一个严格 `10x10` 的纯绿色点，并保留 `result.json` 和完整变焦轨迹；
- 单次报错写入 JSON 后继续。若 DeepSeek 的首次图片能力探测失败，后续配置标记为跳过，避免制造数百个相同错误。

运行进度见 `cache/web-ui-matrix/progress.json`；自动刷新的结果页见 `cache/web-ui-matrix/index.html`。脚本支持断点续跑：已有 `result.json` 的目录不会再次请求 API。

本地视觉模型用相同的 10 题和 29 种模式参赛，并以独立进程运行：

```bash
.venv/bin/python debug/run_web_ui_matrix.py \
  --providers minicpm,gemma4 \
  --progress-file progress-local.json \
  --gallery-file index-local.html
```

本地模型使用此前消融中更稳定的单张编号图协议，结果目录分别命名为 `minicpm-local-grid__*` 和 `gemma4-local-grid__*`。`RogerBen/HY-MT2-1.8B` 的 Ollama 能力只有 `completion`，没有 `vision`，因此不能参加视觉定位；可参赛的小模型是带 548M 视觉投影的 `minicpm-v4.6`。

OCR-first 混合赛道只对每张原图运行一次 RapidOCR 并缓存。唯一文字命中时直接使用 OCR 框中心；零个或多个匹配时才调用对应视觉模型：

```bash
.venv/bin/python debug/run_ocr_first_hybrid.py
```

可编辑的每题 OCR 查询在 `example/web_ui_ocr_queries.json`，缓存位于 `cache/web-ui-matrix/ocr-cache/`，独立进度位于 `progress-ocr-first.json`。

默认策略不信任 OCR 的最终判断：一个或多个匹配都会在低对比度编号网格上绘制半透明 A/B 色块，并始终要求视觉模型根据原图、控件文字和周边界面语义复核。候选图保存在对应结果目录的 `ocr-candidates-grid.png`。只有显式添加 `--direct-ocr` 才允许唯一匹配绕过视觉模型。版本化目录保留旧实验，可用 `--version v5` 开新一轮而不覆盖历史结果。

默认混合链路使用候选 ID 协议：视觉模型只能返回 `{"candidate":"A"}` 或 `{"candidate":null}`，不能返回格号、偏移或像素。选择 A/B 后，本地直接使用对应 OCR 框中心；返回 `null`、无 OCR 候选或候选接口报错时，才进入小变焦。回退先在完整原图使用 `25×25` 网格，裁剪所选格周围 `3×3` 格范围并放大到原图尺寸，最后在局部图使用 `6×6` 网格并取细格中心。参数可通过 `--fallback-grid`、`--fallback-fine-grid` 和 `--fallback-span` 调整；独立进度写入 `progress-ocr-smallzoom.json`。

10 题 `v8-fine25-span3-to6` 实测中，OCR 候选复核严格命中 `31/35`；5 次进入小变焦的尝试严格命中 `0/5`，且第一层 `25×25` 均未保留正确目标区域，其中 GLM 对无文字图标连续两次返回超过 625 上限的无效格号。端到端严格结果为 MiniCPM `8/10`、GLM `8/10`（含 1 个协议错误）、Gemma `7/10`、MiniMax `8/10`。

这里包含 10 张带已知按钮边界的合成 UI 截图，用于测量两级变焦点击准确率。评测只调用本地 Ollama，不操作鼠标。

当前实测：

| 模型 | 严格命中 | 平均耗时 | 结果 |
|---|---:|---:|---|
| `minicpm-v4.6:latest` | **6/10（60%）** | 2.67 秒/图 | [`results/SUMMARY.md`](results/SUMMARY.md) |
| `gemma4:latest` | **6/10（60%）** | 16.266 秒/图 | [`results-gemma4/SUMMARY.md`](results-gemma4/SUMMARY.md) |
| MiniMax `MiniMax-M3`，单轮 16×16 | **10/10（100%）** | 10.035 秒/图 | [`results-minimax-m3-16x16/SUMMARY.md`](results-minimax-m3-16x16/SUMMARY.md) |

MiniCPM 的 4 个失败均为距离按钮框 8–17px 的近失误；Gemma 4 的失败距离为 8–28px。

MiniMax-M3 是强模型对照，不使用两级变焦：直接发送一张 16×16 编号图，模型返回格号和格内偏移。10 图全部命中，但 priority API 仍需 6.828–15.734 秒/图。

实验性的数字中心像素偏置协议同样为 **6/10（60%）**，但平均延迟增加到 7.729 秒。结果见 [`results-center-delta/SUMMARY.md`](results-center-delta/SUMMARY.md)。

测试图总览：[`cases/contact-sheet.png`](cases/contact-sheet.png)。

生成测试集：

```bash
python3 debug/generate_cases.py
```

运行：

```bash
python3 debug/run_benchmark.py --model minicpm-v4.6:latest
```

切换模型并单独保存结果：

```bash
python3 debug/run_benchmark.py \
  --model gemma4:latest \
  --output debug/results-gemma4
```

运行 MiniMax-M3 单轮 16×16 基准（密钥只从外部设置文件读取）：

```bash
python3 debug/run_minimax_benchmark.py \
  --settings /path/to/provider_settings.minimax.json \
  --service-tier priority \
  --detail default
```

中心偏置对照：

```bash
python3 debug/run_benchmark.py \
  --model minicpm-v4.6:latest \
  --center-delta \
  --output debug/results-center-delta
```

输出：

- `cases/manifest.json`：任务和真实按钮边界；
- `results/results.json`：完整机器可读结果；
- `results/SUMMARY.md`：准确率摘要；
- `results/case-*/`：每张图的粗网格、局部裁图、细网格和动作轨迹。
